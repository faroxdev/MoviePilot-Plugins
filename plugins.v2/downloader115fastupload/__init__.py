"""
下载器任务完成后使用 fake115uploader 尝试 115 秒传。

仅当整个任务秒传成功后，才通过 MoviePilot 的统一下载器接口删除
对应种子和数据；秒传失败时保留原任务和文件。
"""
import datetime
import json
import platform
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.chain.download import DownloadChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType, TorrentQueryStatus


class Downloader115FastUpload(_PluginBase):
    """下载完成后尝试 115 秒传。"""

    plugin_name = "下载器115秒传"
    plugin_desc = "监控下载器任务；115 秒传成功后删除种子和文件，失败则保留。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "1.3.1"
    plugin_author = "faroxdev"
    author_url = "https://github.com/faroxdev"
    plugin_config_prefix = "downloader115fastupload_"
    plugin_order = 23
    auth_level = 1

    _enabled: bool = False
    _notify: bool = True
    _downloader: str = ""
    _binary_path: str = ""
    _runtime_binary_path: str = ""
    _config_path: str = ""
    _cookie: str = ""
    _cid: str = "0"
    _scan_interval: int = 30
    _retry_interval: int = 3600
    _settle_seconds: int = 10
    _timeout_minutes: int = 120
    _http_retry: int = 1
    _categories: str = ""
    _include_tags: str = ""
    _exclude_tags: str = ""
    _process_existing: bool = False
    _retry_failed: bool = False
    _onlyonce: bool = False
    _scheduler: Optional[BackgroundScheduler] = None
    _tasks: Dict[str, dict] = {}
    _history: List[dict] = []
    _lock = RLock()
    _scan_running: bool = False
    _active_process: Optional[subprocess.Popen] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", True))
            self._downloader = str(config.get("downloader", "") or "").strip()
            self._binary_path = str(config.get("binary_path", "") or "").strip()
            self._config_path = str(config.get("config_path", "") or "").strip()
            self._cookie = str(config.get("cookie", "") or "").strip()
            self._cid = str(config.get("cid", "0") or "0").strip()
            self._scan_interval = self._to_int(
                config.get("scan_interval"), 30, 5, 3600
            )
            self._retry_interval = self._to_int(
                config.get("retry_interval"), 3600, 60, 604800
            )
            self._settle_seconds = self._to_int(
                config.get("settle_seconds"), 10, 0, 3600
            )
            self._timeout_minutes = self._to_int(
                config.get("timeout_minutes"), 120, 1, 1440
            )
            self._http_retry = self._to_int(config.get("http_retry"), 1, 0, 20)
            self._categories = str(config.get("categories", "") or "")
            self._include_tags = str(config.get("include_tags", "") or "")
            self._exclude_tags = str(config.get("exclude_tags", "") or "")
            self._process_existing = bool(config.get("process_existing", False))
            self._retry_failed = bool(config.get("retry_failed", False))
            self._onlyonce = bool(config.get("onlyonce", False))

        self._runtime_binary_path = self._resolve_binary_path()
        saved_tasks = self.get_data("tasks") or {}
        saved_history = self.get_data("history") or []
        self._tasks = saved_tasks if isinstance(saved_tasks, dict) else {}
        self._history = saved_history if isinstance(saved_history, list) else []
        # 兼容 1.1.0 保存的失败任务，为其补上独立重试时间。
        migration_now = time.time()
        for task in self._tasks.values():
            if task.get("status") == "upload_failed" and not task.get("retry_at"):
                task["retry_at"] = migration_now + self._retry_interval

        if self._enabled or self._onlyonce or self._process_existing or self._retry_failed:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.scan,
                trigger="interval",
                seconds=self._scan_interval,
                id="Downloader115FastUploadScan",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            for key, task in self._tasks.items():
                if task.get("status") == "upload_failed" and task.get("retry_at"):
                    self._schedule_retry(key, float(task["retry_at"]))
            self._scheduler.add_job(
                func=self.scan,
                trigger="date",
                run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                + datetime.timedelta(seconds=3),
                id="Downloader115FastUploadInitialScan",
                replace_existing=True,
            )
            self._onlyonce = False
            self.__update_config()
            self._scheduler.start()

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
        except Exception as err:
            logger.debug(f"下载器115秒传：停止调度器时忽略异常：{err}")
        finally:
            self._scheduler = None

        with self._lock:
            process = self._active_process
        if process and process.poll() is None:
            logger.warning("下载器115秒传：插件停止，正在终止 fake115uploader")
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/downloader115_scan",
            "event": EventType.PluginAction,
            "desc": "立即检查下载器 115 秒传任务",
            "category": "下载",
            "data": {"action": "downloader115_scan"},
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/scan",
            "endpoint": self.api_scan,
            "methods": ["POST"],
            "auth": "apikey",
            "summary": "立即检查下载器 115 秒传任务",
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        downloader_options = [
            {"title": conf.name, "value": conf.name}
            for conf in DownloaderHelper().get_configs().values()
        ]
        return [{
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [{
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VAlert",
                            "props": {
                                "type": "warning",
                                "variant": "tonal",
                                "text": (
                                    "仅调用 fake115uploader 的 -f 秒传模式。整个任务成功后才删除 "
                                    "下载任务和数据；任何文件无法秒传时均保留本地任务。首次启用默认只"
                                    "观察新完成任务，不处理已有做种任务。Release 已内置 Linux "
                                    "amd64/arm64 二进制，通常无需填写程序路径。"
                                ),
                            },
                        }],
                    }],
                },
                {
                    "component": "VRow",
                    "content": [
                        self._col_switch("enabled", "启用插件"),
                        self._col_switch("notify", "发送结果通知"),
                        self._col_switch("onlyonce", "立即检查"),
                        self._col_switch("process_existing", "处理已有完成任务"),
                        self._col_switch("retry_failed", "重试失败任务"),
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VSelect",
                                "props": {
                                    "model": "downloader",
                                    "label": "MoviePilot 下载器",
                                    "items": downloader_options,
                                    "clearable": True,
                                },
                            }],
                        },
                        self._col_text(
                            "binary_path", "fake115uploader 路径",
                            "留空自动使用插件内置版本", 8
                        ),
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        self._col_text(
                            "config_path", "fake115uploader 配置文件",
                            "/config/fake115uploader.json", 7
                        ),
                        self._col_text("cid", "115 目标目录 CID", "0", 2),
                        self._col_text("http_retry", "HTTP 重试次数", "1", 3, "number"),
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "cookie",
                                    "label": "115 Cookie（可选，填写后覆盖配置文件）",
                                    "type": "password",
                                    "clearable": True,
                                    "hint": "建议使用挂载的配置文件；不填则读取该文件中的 cookies",
                                    "persistent-hint": True,
                                },
                            }],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                    self._col_text("scan_interval", "检查间隔（秒）", "30", 3, "number"),
                    self._col_text("retry_interval", "失败重试间隔（秒）", "3600", 3, "number"),
                    self._col_text("settle_seconds", "完成后等待（秒）", "10", 3, "number"),
                        self._col_text("timeout_minutes", "单任务超时（分钟）", "120", 3, "number"),
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        self._col_text(
                            "categories", "仅处理分类", "电影,电视剧（留空为全部）", 4
                        ),
                        self._col_text(
                            "include_tags", "至少包含一个标签", "115,秒传（留空为全部）", 4
                        ),
                        self._col_text(
                            "exclude_tags", "排除标签", "保种,勿删", 4
                        ),
                    ],
                },
            ],
        }], {
            "enabled": False,
            "notify": True,
            "downloader": "",
            "binary_path": "",
            "config_path": "",
            "cookie": "",
            "cid": "0",
            "scan_interval": 30,
            "retry_interval": 3600,
            "settle_seconds": 10,
            "timeout_minutes": 120,
            "http_retry": 1,
            "categories": "",
            "include_tags": "",
            "exclude_tags": "",
            "process_existing": False,
            "retry_failed": False,
            "onlyonce": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        items = []
        for item in reversed(self._history[-100:]):
            status = item.get("status")
            color = "success" if status == "成功" else (
                "error" if status == "秒传失败" else "warning"
            )
            items.append({
                "component": "VListItem",
                "props": {
                    "title": f"{item.get('title')} · {status}",
                    "subtitle": (
                        f"{item.get('time')} | {item.get('downloader')} | "
                        f"{item.get('message')}"
                    ),
                },
                "content": [{
                    "component": "template",
                    "props": {"v-slot:prepend": ""},
                    "content": [{
                        "component": "VIcon",
                        "props": {"color": color},
                        "text": "mdi-cloud-upload",
                    }],
                }],
            })
        if not items:
            items = [{
                "component": "VCardText",
                "props": {"class": "text-center"},
                "text": "暂无秒传记录",
            }]
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [{"component": "VList", "content": items}],
        }]

    def api_scan(self) -> dict:
        return self.scan()

    @eventmanager.register(EventType.PluginAction)
    def remote_scan(self, event: Event):
        if not event or (event.event_data or {}).get("action") != "downloader115_scan":
            return
        self.scan()

    @eventmanager.register(EventType.DownloadAdded)
    def download_added(self, event: Event):
        """MP 添加的任务立即纳入观察，避免短任务在两次轮询间完成而被漏掉。"""
        if not self._enabled or not event or not event.event_data:
            return
        event_data = event.event_data
        downloader = str(event_data.get("downloader") or "").strip()
        torrent_hash = str(event_data.get("hash") or "").strip()
        if downloader != self._downloader or not torrent_hash:
            return
        key = self._task_key(downloader, torrent_hash)
        with self._lock:
            self._tasks[key] = {
                "status": "watching",
                "title": torrent_hash,
                "updated_at": time.time(),
            }
            self._save_state()
        logger.info(f"下载器115秒传：新下载任务已纳入观察：{torrent_hash}")

    def scan(self) -> dict:
        with self._lock:
            if self._scan_running:
                return {"success": False, "message": "已有检查任务正在运行"}
            self._scan_running = True

        processed = 0
        try:
            service = DownloaderHelper().get_service(name=self._downloader)
            if not service or not service.instance:
                message = "未找到已连接的下载器，请检查插件配置"
                logger.error(f"下载器115秒传：{message}")
                return {"success": False, "message": message}

            chain = DownloadChain()
            torrents = chain.list_torrents(
                status=TorrentQueryStatus.ALL,
                downloader=self._downloader,
                include_all_tags=True,
            )
            if torrents is None:
                message = "读取下载器任务失败"
                logger.error(f"下载器115秒传：{message}")
                return {"success": False, "message": message}

            now = time.time()
            present_keys = set()
            for torrent_item in torrents or []:
                torrent = self._torrent_dict(torrent_item)
                torrent_hash = str(torrent.get("hash") or "").strip()
                if not torrent_hash or not self._matches_filters(torrent):
                    continue
                key = self._task_key(self._downloader, torrent_hash)
                present_keys.add(key)
                task = self._tasks.get(key)
                completed = self._is_completed(torrent)

                if not completed:
                    self._tasks[key] = {
                        "status": "watching",
                        "title": torrent.get("name") or torrent_hash,
                        "updated_at": now,
                    }
                    continue

                if not task:
                    if self._process_existing:
                        task = {
                            "status": "ready",
                            "completed_at": now - self._settle_seconds,
                        }
                        self._tasks[key] = task
                    else:
                        self._tasks[key] = {
                            "status": "ignored_existing",
                            "title": torrent.get("name") or torrent_hash,
                            "updated_at": now,
                        }
                        continue

                status = task.get("status")
                if status == "watching":
                    task["status"] = "ready"
                    task["completed_at"] = now
                    task["updated_at"] = now
                    self._tasks[key] = task
                    status = "ready"
                    logger.info(
                        f"下载器115秒传：检测到下载完成，等待文件稳定："
                        f"{torrent.get('name')} ({torrent_hash})"
                    )
                elif status == "ignored_existing" and self._process_existing:
                    task["status"] = "ready"
                    task["completed_at"] = now - self._settle_seconds
                    task["updated_at"] = now
                    self._tasks[key] = task
                    status = "ready"
                elif status == "upload_failed" and self._retry_failed:
                    task["status"] = "ready"
                    task["completed_at"] = now - self._settle_seconds
                    status = "ready"
                elif status == "upload_failed":
                    retry_at = float(task.get("retry_at", 0) or 0)
                    if retry_at <= now:
                        task["status"] = "ready"
                        task["completed_at"] = now - self._settle_seconds
                        task["updated_at"] = now
                        self._tasks[key] = task
                        status = "ready"
                        logger.info(
                            f"下载器115秒传：失败任务到达重试时间：{torrent.get('name')}"
                        )
                elif status == "delete_failed":
                    self._delete_after_upload(chain, torrent, key)
                    processed += 1
                    continue

                if status != "ready":
                    continue
                if now - float(task.get("completed_at", now)) < self._settle_seconds:
                    continue
                self._process_torrent(chain, torrent, key)
                processed += 1

            # 已从下载器删除的成功任务无需永久占用状态空间。
            for key, task in list(self._tasks.items()):
                if (
                    key.startswith(f"{self._downloader}:")
                    and key not in present_keys
                    and task.get("status") in {"deleted", "ignored_existing"}
                ):
                    self._tasks.pop(key, None)
            self._process_existing = False
            self._retry_failed = False
            self._save_state()
            self.__update_config()
            message = f"检查完成，本次处理 {processed} 个任务"
            logger.info(f"下载器115秒传：{message}")
            return {"success": True, "message": message}
        except Exception as err:
            logger.exception(f"下载器115秒传：检查异常：{err}")
            return {"success": False, "message": str(err)}
        finally:
            with self._lock:
                self._scan_running = False

    def _process_torrent(self, chain: DownloadChain, torrent: dict, key: str):
        torrent_hash = str(torrent.get("hash") or "")
        title = str(torrent.get("name") or torrent_hash)
        path = self._local_content_path(torrent)
        logger.info(f"下载器115秒传：开始尝试秒传：{title}，路径：{path}")

        if not path.exists():
            self._record_failure(
                key, torrent, "本地路径不存在", f"MoviePilot 无法访问：{path}"
            )
            return

        failed_files = self._tasks.get(key, {}).get("failed_files") or []
        if failed_files:
            remaining = []
            outputs = []
            for index, item in enumerate(failed_files):
                if not isinstance(item, dict) or not item.get("path"):
                    remaining.append(item)
                    outputs.append("失败记录缺少文件路径")
                    continue
                retry_path = Path(str(item["path"]))
                if not retry_path.exists():
                    remaining.append(item)
                    outputs.append(f"文件不存在：{retry_path}")
                    continue
                try:
                    ok, output, result = self._run_upload_attempt(
                        retry_path, title, cid=item.get("cid")
                    )
                except Exception as err:
                    remaining.extend(failed_files[index:])
                    outputs.append(str(err))
                    break
                if not ok:
                    remaining.extend(self._result_failed_files(result) or [item])
                    outputs.append(self._last_output(output))
            if remaining:
                summary = " | ".join(filter(None, outputs)) or "部分文件仍无法秒传"
                self._record_failure(
                    key, torrent, "秒传失败", summary, failed_files=remaining
                )
                return
        else:
            try:
                ok, output, result = self._run_upload_attempt(path, title)
            except Exception as err:
                self._record_failure(key, torrent, "程序不可用", str(err))
                return
            if not ok:
                summary = self._last_output(output) or "fake115uploader 返回失败"
                self._record_failure(
                    key,
                    torrent,
                    "秒传失败",
                    summary,
                    failed_files=self._result_failed_files(result),
                )
                return

        self._tasks[key] = {
            "status": "uploaded",
            "title": title,
            "path": str(path),
            "updated_at": time.time(),
        }
        self._save_state()
        logger.info(f"下载器115秒传：整个任务秒传成功，准备删除下载任务及文件：{title}")
        self._delete_after_upload(chain, torrent, key)

    def _delete_after_upload(self, chain: DownloadChain, torrent: dict, key: str):
        torrent_hash = str(torrent.get("hash") or "")
        title = str(torrent.get("name") or torrent_hash)
        try:
            deleted = chain.remove_torrents(
                hashs=[torrent_hash],
                delete_file=True,
                downloader=self._downloader,
            )
        except Exception as err:
            deleted = False
            delete_message = str(err)
        else:
            delete_message = "下载器返回删除失败"

        if not deleted:
            self._tasks[key] = {
                **self._tasks.get(key, {}),
                "status": "delete_failed",
                "updated_at": time.time(),
            }
            self._append_history(
                torrent, "删除失败",
                f"秒传已成功，但未能删除下载任务和文件：{delete_message}"
            )
            logger.error(
                f"下载器115秒传：{title} 秒传成功，但删除下载任务和文件失败；"
                "后续检查会只重试删除"
            )
            self._notify_result(
                title, False, "秒传成功，但删除 下载任务和文件失败"
            )
            return

        self._tasks[key] = {
            **self._tasks.get(key, {}),
            "status": "deleted",
            "updated_at": time.time(),
        }
        self._cancel_retry(key)
        message = "秒传成功，已删除 下载任务和对应文件"
        self._append_history(torrent, "成功", message)
        logger.info(f"下载器115秒传：{title} {message}")
        self._notify_result(title, True, message)

    def _run_uploader(self, command: List[str], title: str) -> Tuple[bool, str]:
        output_lines: List[str] = []
        line_queue: queue.Queue = queue.Queue()
        safe_command = ["******" if command[index - 1:index] == ["-k"] else value
                        for index, value in enumerate(command)]
        logger.info(f"下载器115秒传：执行命令：{safe_command}")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as err:
            return False, f"无法启动 fake115uploader：{err}"

        with self._lock:
            self._active_process = process

        def read_output():
            try:
                for line in iter(process.stdout.readline, ""):
                    line_queue.put(line.rstrip())
            finally:
                line_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + self._timeout_minutes * 60
        reader_done = False
        try:
            while process.poll() is None or not reader_done:
                if time.monotonic() > deadline and process.poll() is None:
                    process.kill()
                    logger.error(f"下载器115秒传：{title} 超过超时限制，已终止进程")
                try:
                    line = line_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if line is None:
                    reader_done = True
                    continue
                clean_line = self._sanitize_output(line)
                output_lines.append(clean_line)
                if len(output_lines) > 500:
                    output_lines = output_lines[-500:]
                logger.info(f"下载器115秒传 [{title}] {clean_line}")
            return process.returncode == 0, "\n".join(output_lines)
        finally:
            with self._lock:
                if self._active_process is process:
                    self._active_process = None

    def _run_upload_attempt(
        self, path: Path, title: str, cid: Any = None
    ) -> Tuple[bool, str, Optional[dict]]:
        with tempfile.TemporaryDirectory(prefix="mp-115-result-") as result_dir:
            command = self._build_command(path, Path(result_dir), cid=cid)
            ok, output = self._run_uploader(command, title)
            return ok, output, self._read_upload_result(Path(result_dir))

    def _build_command(
        self, path: Path, result_dir: Path, cid: Any = None
    ) -> List[str]:
        if not self._runtime_binary_path:
            raise RuntimeError(
                "找不到可用的 fake115uploader；请安装 Release 版插件，"
                "或在配置中填写容器内的可执行文件路径"
            )
        command = [self._runtime_binary_path, "-f"]
        if self._config_path:
            command.extend(["-l", self._config_path])
        if self._cookie:
            if not self._config_path:
                command.append("-n")
            command.extend(["-k", self._cookie])
        command.extend(["-r", str(result_dir)])
        target_cid = self._cid if cid is None else str(cid)
        if target_cid:
            command.extend(["-c", target_cid])
        if self._http_retry:
            command.extend(["-http-retry", str(self._http_retry)])
        if path.is_dir():
            command.append("-recursive")
        command.append(str(path))
        return command

    @staticmethod
    def _read_upload_result(result_dir: Path) -> Optional[dict]:
        files = sorted(result_dir.glob("* result.json"), key=lambda item: item.stat().st_mtime)
        if not files:
            return None
        try:
            result = json.loads(files[-1].read_text(encoding="utf-8"))
            return result if isinstance(result, dict) else None
        except Exception as err:
            logger.warning(f"下载器115秒传：读取上传结果失败：{err}")
            return None

    @staticmethod
    def _result_failed_files(result: Optional[dict]) -> List[dict]:
        """提取并去重失败文件。

        >>> Downloader115FastUpload._result_failed_files({"failed": [
        ...     {"path": "/a.mkv", "cid": 1}, {"path": "/a.mkv", "cid": 1}
        ... ]})
        [{'path': '/a.mkv', 'cid': 1}]
        """
        failed = result.get("failed") if isinstance(result, dict) else None
        if not isinstance(failed, list):
            return []
        unique = {}
        for item in failed:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            normalized = {"path": str(item["path"]), "cid": item.get("cid", 0)}
            unique[(normalized["path"], str(normalized["cid"]))] = normalized
        return list(unique.values())

    def _resolve_binary_path(self) -> str:
        """显式配置优先，其次选择 Release 内置架构，最后尝试容器 PATH。"""
        if self._binary_path:
            configured = Path(self._binary_path).expanduser()
            if configured.is_file():
                return self._make_executable(configured)
            from_path = shutil.which(self._binary_path)
            if from_path:
                return from_path
            logger.error(
                f"下载器115秒传：配置的 fake115uploader 不存在：{self._binary_path}"
            )
            return ""

        machine = platform.machine().strip().lower()
        arch_map = {
            "x86_64": "amd64",
            "amd64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }
        arch = arch_map.get(machine)
        if arch:
            bundled = (
                Path(__file__).resolve().parent
                / "bin"
                / f"fake115uploader-linux-{arch}"
            )
            if bundled.is_file():
                resolved = self._make_executable(bundled)
                logger.info(
                    f"下载器115秒传：使用内置 fake115uploader：{resolved}"
                )
                return resolved

        from_path = shutil.which("fake115uploader")
        if from_path:
            logger.info(
                f"下载器115秒传：使用 PATH 中的 fake115uploader：{from_path}"
            )
            return from_path

        logger.warning(
            f"下载器115秒传：当前系统 {platform.system()}/{machine} 没有可用的"
            "内置 fake115uploader；请填写外部程序路径"
        )
        return ""

    @staticmethod
    def _make_executable(path: Path) -> str:
        try:
            mode = path.stat().st_mode
            path.chmod(
                mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
        except Exception as err:
            logger.error(f"下载器115秒传：设置执行权限失败：{path}：{err}")
            return ""
        return str(path)

    @staticmethod
    def _local_content_path(torrent: dict) -> Path:
        raw_path = torrent.get("content_path") or torrent.get("path")
        if not raw_path:
            raw_path = str(
                Path(str(torrent.get("save_path") or ""))
                / str(torrent.get("name") or "")
            )
        return Path(str(raw_path))

    @staticmethod
    def _torrent_dict(torrent: Any) -> dict:
        """兼容 MP 统一下载器返回的 Pydantic 模型和字典。"""
        if isinstance(torrent, dict):
            return torrent
        if hasattr(torrent, "model_dump"):
            return torrent.model_dump()
        if hasattr(torrent, "dict"):
            return torrent.dict()
        return vars(torrent)

    def _matches_filters(self, torrent: dict) -> bool:
        categories = self._split_values(self._categories)
        if categories and str(torrent.get("category") or "") not in categories:
            return False
        torrent_tags = self._split_values(str(torrent.get("tags") or ""))
        include_tags = self._split_values(self._include_tags)
        exclude_tags = self._split_values(self._exclude_tags)
        if include_tags and not torrent_tags.intersection(include_tags):
            return False
        if exclude_tags and torrent_tags.intersection(exclude_tags):
            return False
        return True

    @staticmethod
    def _is_completed(torrent: dict) -> bool:
        state = str(torrent.get("state") or "").strip().lower()
        if state == "completed":
            return True
        if state in {"downloading", "paused"}:
            return False
        try:
            progress = float(torrent.get("progress") or 0)
        except (TypeError, ValueError):
            return False
        # MP 统一模型的进度单位是百分比；兼容少数模块直接返回 0~1 的情况。
        return progress >= 100 or (progress >= 1 and state not in {"", "unknown"})

    def _record_failure(
        self,
        key: str,
        torrent: dict,
        status: str,
        message: str,
        failed_files: Optional[List[dict]] = None,
    ):
        task = {
            "status": "upload_failed",
            "title": torrent.get("name") or torrent.get("hash"),
            "message": message,
            "retry_at": time.time() + self._retry_interval,
            "retry_count": int(self._tasks.get(key, {}).get("retry_count", 0)) + 1,
            "updated_at": time.time(),
        }
        if failed_files:
            task["failed_files"] = failed_files
            message = f"{len(failed_files)} 个文件待重试：{message}"
            task["message"] = message
        # ponytail: 无结果文件时只能重试整个种子；若上游提供原子结果流再细化。
        self._tasks[key] = task
        self._append_history(torrent, status, message)
        logger.error(
            f"下载器115秒传：{torrent.get('name')} {status}，保留下载任务和文件：{message}"
        )
        self._schedule_retry(key, self._tasks[key]["retry_at"])
        self._notify_result(str(torrent.get("name") or ""), False, message)

    def _schedule_retry(self, key: str, retry_at: float):
        if not self._scheduler:
            return
        run_date = datetime.datetime.fromtimestamp(
            retry_at, tz=pytz.timezone(settings.TZ)
        )
        self._scheduler.add_job(
            func=self._retry_task,
            trigger="date",
            run_date=run_date,
            args=[key],
            id=self._retry_job_id(key),
            replace_existing=True,
        )

    def _retry_task(self, key: str):
        task = self._tasks.get(key)
        if not task or task.get("status") != "upload_failed":
            return
        if float(task.get("retry_at", 0) or 0) > time.time():
            self._schedule_retry(key, float(task["retry_at"]))
            return
        self.scan()

    def _cancel_retry(self, key: str):
        if not self._scheduler:
            return
        try:
            self._scheduler.remove_job(self._retry_job_id(key))
        except Exception:
            pass

    @staticmethod
    def _retry_job_id(key: str) -> str:
        return "Downloader115FastUploadRetry-" + re.sub(r"[^A-Za-z0-9_-]", "_", key)

    def _append_history(self, torrent: dict, status: str, message: str):
        self._history.append({
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "downloader": self._downloader,
            "hash": torrent.get("hash"),
            "title": torrent.get("name") or torrent.get("hash"),
            "status": status,
            "message": message[:1000],
        })
        self._history = self._history[-300:]
        self._save_state()

    def _notify_result(self, title: str, success: bool, message: str):
        if not self._notify:
            return
        self.post_message(
            mtype=NotificationType.Plugin,
            title=f"【下载器115秒传】{'完成' if success else '失败'}",
            text=f"{title}\n{message}",
        )

    def _save_state(self):
        self.save_data("tasks", self._tasks)
        self.save_data("history", self._history[-300:])

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "downloader": self._downloader,
            "binary_path": self._binary_path,
            "config_path": self._config_path,
            "cookie": self._cookie,
            "cid": self._cid,
            "scan_interval": self._scan_interval,
            "retry_interval": self._retry_interval,
            "settle_seconds": self._settle_seconds,
            "timeout_minutes": self._timeout_minutes,
            "http_retry": self._http_retry,
            "categories": self._categories,
            "include_tags": self._include_tags,
            "exclude_tags": self._exclude_tags,
            "process_existing": False,
            "retry_failed": False,
            "onlyonce": False,
        })

    @staticmethod
    def _task_key(downloader: str, torrent_hash: str) -> str:
        return f"{downloader}:{torrent_hash.lower()}"

    @staticmethod
    def _split_values(value: str) -> set:
        return {
            item.strip()
            for item in re.split(r"[,，;\n]+", value or "")
            if item.strip()
        }

    @staticmethod
    def _last_output(output: str) -> str:
        lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
        return " | ".join(lines[-5:])[-1000:]

    @staticmethod
    def _sanitize_output(value: str) -> str:
        return re.sub(
            r"(?i)(cookie(?:s)?(?:的值为)?[\"'=:\s]+).*$",
            r"\1******",
            value,
        )

    @staticmethod
    def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _col_switch(model: str, label: str) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 2},
            "content": [{
                "component": "VSwitch",
                "props": {"model": model, "label": label},
            }],
        }

    @staticmethod
    def _col_text(
        model: str, label: str, placeholder: str, md: int, field_type: str = "text"
    ) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{
                "component": "VTextField",
                "props": {
                    "model": model,
                    "label": label,
                    "placeholder": placeholder,
                    "type": field_type,
                    "clearable": field_type != "number",
                },
            }],
        }
