"""
Emby 异步图片刮削插件。

接收 MoviePilot 内置 Emby Webhook 解析后的 library.new 事件，延迟合并任务，
只使用 MoviePilot 刮削链补齐缺失图片，不创建或修改 NFO，完成后刷新 Emby 对应项目。
"""
import datetime
import time
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app import schemas
from app.chain.media import MediaChain, ScrapingConfig
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import (
    EventType,
    NotificationType,
    ScrapingMetadata,
    ScrapingPolicy,
    ScrapingTarget,
)
from app.utils.http import RequestUtils
from app.utils.url import UrlUtils


class _ImageOnlyMediaChain(MediaChain):
    """独立于主程序单例的图片专用刮削链。"""

    @staticmethod
    def _image_only_config() -> ScrapingConfig:
        config = {
            f"{target.name.lower()}_nfo": ScrapingPolicy.SKIP.value
            for target in ScrapingTarget
        }
        for target in ScrapingTarget:
            for metadata in ScrapingMetadata:
                if metadata != ScrapingMetadata.NFO:
                    config[f"{target.name.lower()}_{metadata.name.lower()}"] = (
                        ScrapingPolicy.MISSINGONLY.value
                    )
        return ScrapingConfig(config)

    def __init__(self):
        super().__init__()
        self.scraping_policies = self._image_only_config()

    def on_config_changed(self):
        """不跟随 MP 全局刮削策略，始终保持仅补缺失图片。"""
        self.scraping_policies = self._image_only_config()


class EmbyAsyncImages(_PluginBase):
    """Emby 异步图片刮削。"""

    plugin_name = "Emby异步图片刮削"
    plugin_desc = "Emby 快速入库后，由 MoviePilot 异步补齐缺失图片；不创建或修改 NFO。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/emby.png"
    plugin_version = "1.0.1"
    plugin_author = "frh-hh"
    author_url = "https://github.com/frh-hh"
    plugin_config_prefix = "embyasyncimages_"
    plugin_order = 22
    auth_level = 1

    _enabled: bool = False
    _notify: bool = False
    _movie_delay_seconds: int = 0
    _tv_delay_seconds: int = 10
    _scan_interval: int = 15
    _max_retries: int = 5
    _storage: str = "local"
    _path_mappings: str = ""
    _scrape_episode_images: bool = False
    _refresh_emby: bool = True
    _onlyonce: bool = False
    _scheduler: Optional[BackgroundScheduler] = None
    _pending: Dict[str, dict] = {}
    _lock = RLock()
    _running: bool = False

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", False))
            # 兼容 1.0.0 的统一延迟配置：新字段不存在时，电视剧沿用旧值，电影仍立即处理。
            legacy_delay = self._to_int(config.get("delay_seconds"), 10, 0, 3600)
            self._movie_delay_seconds = self._to_int(
                config.get("movie_delay_seconds"), 0, 0, 3600
            )
            self._tv_delay_seconds = self._to_int(
                config.get("tv_delay_seconds"), legacy_delay, 0, 3600
            )
            self._scan_interval = self._to_int(config.get("scan_interval"), 15, 5, 300)
            self._max_retries = self._to_int(config.get("max_retries"), 5, 0, 20)
            self._storage = (config.get("storage", "local") or "local").strip()
            self._path_mappings = config.get("path_mappings", "") or ""
            self._scrape_episode_images = bool(config.get("scrape_episode_images", False))
            self._refresh_emby = bool(config.get("refresh_emby", True))
            self._onlyonce = bool(config.get("onlyonce", False))

        saved_pending = self.get_data("pending") or {}
        self._pending = saved_pending if isinstance(saved_pending, dict) else {}

        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self._drain_queue,
                trigger="interval",
                seconds=self._scan_interval,
                id="EmbyAsyncImagesQueue",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            if self._onlyonce:
                self._scheduler.add_job(
                    func=self._force_drain_queue,
                    trigger="date",
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3),
                    id="EmbyAsyncImagesRunOnce",
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
            logger.debug(f"Emby 异步图片刮削停止调度器时忽略异常：{err}")
        finally:
            self._scheduler = None

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/emby_image_scrape",
            "event": EventType.PluginAction,
            "desc": "立即处理 Emby 图片刮削队列",
            "category": "媒体库",
            "data": {"action": "emby_async_images_run"},
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run_pending",
            "endpoint": self.api_run_pending,
            "methods": ["POST"],
            "auth": "apikey",
            "summary": "立即处理 Emby 图片刮削队列",
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                "type": "info",
                                "variant": "tonal",
                                "text": (
                                    "监听 Emby library.new Webhook，只补齐缺失图片。电影默认立即处理；"
                                    "电视剧配合 Emby 的剧集分组通知，默认等待 10 秒确保路径就绪。"
                                    "插件不创建、不覆盖 NFO，也不会覆盖已有图片。"
                                ),
                            },
                        }],
                    }],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VSwitch",
                                "props": {"model": "enabled", "label": "启用插件"},
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VSwitch",
                                "props": {"model": "notify", "label": "失败时通知"},
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VSwitch",
                                "props": {"model": "refresh_emby", "label": "完成后刷新 Emby"},
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VSwitch",
                                "props": {"model": "onlyonce", "label": "立即处理队列"},
                            }],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "movie_delay_seconds",
                                    "label": "电影等待（秒）",
                                    "type": "number",
                                    "hint": "默认 0，收到通知后立即处理",
                                    "persistent-hint": True,
                                },
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "tv_delay_seconds",
                                    "label": "电视剧等待（秒）",
                                    "type": "number",
                                    "hint": "默认 10；Emby 开启按剧集分组通知后无需长时间合并",
                                    "persistent-hint": True,
                                },
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "scan_interval",
                                    "label": "兜底检查（秒）",
                                    "type": "number",
                                    "hint": "任务会按等待时间精确唤醒；这里仅用于重启恢复和兜底",
                                    "persistent-hint": True,
                                },
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "max_retries",
                                    "label": "失败重试次数",
                                    "type": "number",
                                },
                            }],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "storage",
                                    "label": "MP 存储类型",
                                    "placeholder": "local",
                                },
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "path_mappings",
                                    "label": "Emby → MP 路径映射",
                                    "placeholder": "/cloud/symedia => /media\nD:\\\\Media => /media",
                                    "rows": 3,
                                    "hint": "每行一条，最长前缀优先；两边路径相同时可以留空",
                                    "persistent-hint": True,
                                },
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VSwitch",
                                "props": {
                                    "model": "scrape_episode_images",
                                    "label": "补齐单集缩略图",
                                    "hint": "大量剧集会产生更多请求，默认关闭",
                                    "persistent-hint": True,
                                },
                            }],
                        },
                    ],
                },
            ],
        }], {
            "enabled": False,
            "notify": False,
            "movie_delay_seconds": 0,
            "tv_delay_seconds": 10,
            "scan_interval": 15,
            "max_retries": 5,
            "storage": "local",
            "path_mappings": "",
            "scrape_episode_images": False,
            "refresh_emby": True,
            "onlyonce": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        history = list(reversed(self.get_data("history") or []))[:100]
        pending_count = len(self._pending)
        rows: List[dict] = [{
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "text": f"当前待处理任务：{pending_count}",
            },
        }]
        if history:
            rows.append({
                "component": "VList",
                "content": [{
                    "component": "VListItem",
                    "props": {
                        "title": item.get("title") or "未知项目",
                        "subtitle": f"{item.get('time', '')} · {item.get('message', '')}",
                    },
                    "content": [{
                        "component": "template",
                        "props": {"v-slot:prepend": ""},
                        "content": [{
                            "component": "VIcon",
                            "props": {
                                "icon": "mdi-image-check" if item.get("success") else "mdi-image-off",
                                "color": "success" if item.get("success") else "error",
                            },
                        }],
                    }],
                } for item in history],
            })
        else:
            rows.append({
                "component": "VCardText",
                "props": {"class": "text-center"},
                "text": "暂无处理记录",
            })
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": rows,
        }]

    def api_run_pending(self) -> dict:
        return self._force_drain_queue()

    @eventmanager.register(EventType.PluginAction)
    def remote_run(self, event: Event):
        if not event:
            return
        event_data = event.event_data or {}
        if event_data.get("action") != "emby_async_images_run":
            return
        result = self._force_drain_queue()
        self.post_message(
            mtype=NotificationType.Plugin,
            channel=event_data.get("channel"),
            title="【Emby异步图片刮削】队列处理",
            text=result.get("message"),
            userid=event_data.get("user"),
        )

    @eventmanager.register(EventType.WebhookMessage)
    def on_webhook(self, event: Event):
        """接收 MP 已解析的 Emby Webhook，快速入队后立即返回。"""
        if not self._enabled or not event or not event.event_data:
            return
        info = event.event_data
        if self._event_value(info, "channel") != "emby":
            return
        if (self._event_value(info, "event") or "").lower() != "library.new":
            return

        item_type = (self._event_value(info, "item_type") or "").upper()
        if item_type not in {"MOV", "TV"}:
            return
        item_id = str(self._event_value(info, "item_id") or "").strip()
        item_path = str(self._event_value(info, "item_path") or "").strip()
        server_name = str(self._event_value(info, "server_name") or "").strip()
        if not item_id and not item_path:
            logger.warning("Emby 异步图片刮削：Webhook 缺少 item_id 和 item_path，已忽略")
            return

        key = f"{server_name}:{item_type}:{item_id or item_path}"
        now = time.time()
        delay_seconds = (
            self._movie_delay_seconds if item_type == "MOV" else self._tv_delay_seconds
        )
        task = {
            "key": key,
            "server_name": server_name,
            "item_type": item_type,
            "media_type": self._event_value(info, "media_type"),
            "item_id": item_id,
            "item_path": item_path,
            "item_name": self._event_value(info, "item_name") or item_path,
            "tmdb_id": self._event_value(info, "tmdb_id"),
            "due_at": now + delay_seconds,
            "attempts": 0,
            "queued_at": now,
        }

        with self._lock:
            old_task = self._pending.get(key)
            if old_task:
                task["attempts"] = old_task.get("attempts", 0)
                task["queued_at"] = old_task.get("queued_at", now)
                if not task.get("item_path"):
                    task["item_path"] = old_task.get("item_path")
            self._pending[key] = task
            self._save_pending()
        self._schedule_queue_wakeup(key=key, due_at=task["due_at"])
        logger.info(
            f"Emby 异步图片刮削：任务已入队，{task['item_name']}，"
            f"等待 {delay_seconds} 秒后处理"
        )

    def _force_drain_queue(self) -> dict:
        with self._lock:
            now = time.time()
            for task in self._pending.values():
                task["due_at"] = now
            self._save_pending()
        count = self._drain_queue()
        return {"success": True, "message": f"已处理 {count} 个到期任务"}

    def _drain_queue(self) -> int:
        with self._lock:
            if self._running:
                return 0
            self._running = True
        handled = 0
        try:
            while True:
                with self._lock:
                    now = time.time()
                    due_tasks = [
                        task for task in self._pending.values()
                        if float(task.get("due_at", 0)) <= now
                    ]
                    due_tasks.sort(key=lambda item: item.get("due_at", 0))
                    task = due_tasks[0] if due_tasks else None
                if not task:
                    break
                handled += 1
                self._process_task(task)
            return handled
        finally:
            with self._lock:
                self._running = False

    def _process_task(self, task: dict):
        key = task.get("key")
        try:
            path = self._resolve_task_path(task)
            mapped_path = self._map_path(path)
            fileitem = StorageChain().get_file_item(
                storage=self._storage,
                path=Path(mapped_path),
            )
            if not fileitem:
                raise RuntimeError(
                    f"MP 无法访问路径：[{self._storage}]{mapped_path}，请检查挂载和路径映射"
                )

            image_count = self._scrape_images(fileitem=fileitem, item_type=task.get("item_type"))
            if self._refresh_emby and task.get("item_id"):
                self._refresh_emby_item(task)

            message = f"只补缺失图片完成（处理 {image_count} 个图片层级），NFO 未改动"
            self._append_history(task, True, message)
            logger.info(f"Emby 异步图片刮削：{task.get('item_name')} {message}")
            with self._lock:
                self._pending.pop(key, None)
                self._save_pending()
        except Exception as err:
            attempts = int(task.get("attempts", 0)) + 1
            task["attempts"] = attempts
            if attempts > self._max_retries:
                message = f"超过最大重试次数：{err}"
                logger.error(f"Emby 异步图片刮削：{task.get('item_name')} {message}")
                self._append_history(task, False, message)
                with self._lock:
                    self._pending.pop(key, None)
                    self._save_pending()
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【Emby异步图片刮削】任务失败",
                        text=f"{task.get('item_name')}：{message}",
                    )
            else:
                retry_delay = min(300 * (2 ** (attempts - 1)), 3600)
                task["due_at"] = time.time() + retry_delay
                with self._lock:
                    self._pending[key] = task
                    self._save_pending()
                self._schedule_queue_wakeup(key=key, due_at=task["due_at"])
                logger.warning(
                    f"Emby 异步图片刮削：{task.get('item_name')} 处理失败：{err}；"
                    f"{retry_delay} 秒后第 {attempts}/{self._max_retries} 次重试"
                )

    def _scrape_images(self, fileitem: schemas.FileItem, item_type: str) -> int:
        """只调用 MP 图片刮削函数，不经过任何 NFO 代码路径。"""
        media_chain = MediaChain()
        context = media_chain.recognize_by_path(fileitem.path, obtain_images=True)
        if not context or not context.media_info:
            raise RuntimeError(f"MP 无法识别媒体：{fileitem.path}")

        image_chain = _ImageOnlyMediaChain()
        # 防止运行期间 MP 配置重载影响专用策略。
        image_chain.scraping_policies = image_chain._image_only_config()
        mediainfo = context.media_info
        processed = 0

        if item_type == "MOV":
            parent = StorageChain().get_parent_item(fileitem) if fileitem.type == "file" else None
            image_chain._scrape_images_generic(
                current_fileitem=fileitem,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.MOVIE,
                parent_fileitem=parent,
                overwrite=False,
            )
            return 1

        if fileitem.type != "dir":
            parent = StorageChain().get_parent_item(fileitem)
            if not parent:
                raise RuntimeError(f"无法确定电视剧根目录：{fileitem.path}")
            fileitem = parent

        image_chain._scrape_images_generic(
            current_fileitem=fileitem,
            mediainfo=mediainfo,
            item_type=ScrapingTarget.TV,
            overwrite=False,
        )
        processed += 1

        children = StorageChain().list_files(fileitem=fileitem, recursion=False) or []
        for child in children:
            if child.type == "dir":
                season = self._season_number(child.name)
                if season is None:
                    continue
                image_chain._scrape_images_generic(
                    current_fileitem=child,
                    mediainfo=mediainfo,
                    item_type=ScrapingTarget.SEASON,
                    parent_fileitem=fileitem,
                    overwrite=False,
                    season_number=season,
                )
                processed += 1
                if self._scrape_episode_images:
                    processed += self._scrape_episode_images_in_directory(
                        image_chain=image_chain,
                        media_chain=media_chain,
                        directory=child,
                        mediainfo=mediainfo,
                    )
            elif self._scrape_episode_images:
                processed += self._scrape_episode_image(
                    image_chain=image_chain,
                    media_chain=media_chain,
                    fileitem=child,
                    parent=fileitem,
                    mediainfo=mediainfo,
                )
        return processed

    def _scrape_episode_images_in_directory(
            self,
            image_chain: _ImageOnlyMediaChain,
            media_chain: MediaChain,
            directory: schemas.FileItem,
            mediainfo: Any,
    ) -> int:
        count = 0
        files = StorageChain().list_files(fileitem=directory, recursion=False) or []
        for fileitem in files:
            if fileitem.type != "file":
                continue
            count += self._scrape_episode_image(
                image_chain=image_chain,
                media_chain=media_chain,
                fileitem=fileitem,
                parent=directory,
                mediainfo=mediainfo,
            )
        return count

    @staticmethod
    def _scrape_episode_image(
            image_chain: _ImageOnlyMediaChain,
            media_chain: MediaChain,
            fileitem: schemas.FileItem,
            parent: schemas.FileItem,
            mediainfo: Any,
    ) -> int:
        meta = MetaInfoPath(Path(fileitem.path))
        if not meta.begin_episode:
            return 0
        episode_info = media_chain.recognize_media(
            meta=meta,
            tmdbid=mediainfo.tmdb_id,
            episode_group=mediainfo.episode_group,
        )
        if not episode_info:
            return 0
        image_chain._scrape_images_generic(
            current_fileitem=fileitem,
            mediainfo=episode_info,
            item_type=ScrapingTarget.EPISODE,
            parent_fileitem=parent,
            overwrite=False,
            season_number=meta.begin_season,
            episode_number=meta.begin_episode,
        )
        return 1

    def _resolve_task_path(self, task: dict) -> str:
        """电视剧优先从 MP 已配置的 Emby 实例查询 Series 根目录。"""
        if task.get("item_type") == "TV" and task.get("server_name") and task.get("item_id"):
            service = MediaServerHelper().get_service(name=task.get("server_name"), type_filter="emby")
            if service and service.instance:
                item = service.instance.get_iteminfo(task.get("item_id"))
                if item and item.path:
                    return item.path
        path = task.get("item_path")
        if not path:
            raise RuntimeError("Webhook 中没有可用媒体路径")
        return path

    def _map_path(self, source_path: str) -> str:
        source_norm = self._normalize_path(source_path)
        mappings = sorted(
            self._parse_path_mappings(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for source, target in mappings:
            source_key = self._normalize_path(source).rstrip("/")
            target_key = self._normalize_path(target).rstrip("/")
            if source_norm == source_key:
                return target_key or "/"
            if source_norm.startswith(source_key + "/"):
                return target_key + source_norm[len(source_key):]
        return source_norm

    def _parse_path_mappings(self) -> List[Tuple[str, str]]:
        mappings: List[Tuple[str, str]] = []
        for raw_line in self._path_mappings.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            for separator in ("=>", "->", "="):
                if separator in line:
                    source, target = line.split(separator, 1)
                    if source.strip() and target.strip():
                        mappings.append((source.strip(), target.strip()))
                    break
        return mappings

    @staticmethod
    def _normalize_path(path: str) -> str:
        return (path or "").replace("\\", "/").rstrip("/") or "/"

    @staticmethod
    def _season_number(name: str) -> Optional[int]:
        if name in settings.RENAME_FORMAT_S0_NAMES:
            return 0
        meta = MetaInfo(name or "")
        return meta.begin_season

    @staticmethod
    def _event_value(info: Any, key: str) -> Any:
        if isinstance(info, dict):
            return info.get(key)
        return getattr(info, key, None)

    def _refresh_emby_item(self, task: dict):
        service = MediaServerHelper().get_service(
            name=task.get("server_name"),
            type_filter="emby",
        )
        if not service or not service.config:
            raise RuntimeError(f"MP 中未找到 Emby 服务：{task.get('server_name')}")
        config = service.config.config or {}
        host = config.get("host")
        apikey = config.get("apikey")
        if not host or not apikey:
            raise RuntimeError("MP 中的 Emby 地址或 API Key 不完整")

        host = UrlUtils.standardize_base_url(host)
        url = f"{host}emby/Items/{task.get('item_id')}/Refresh"
        params = {
            "Recursive": "true" if task.get("item_type") == "TV" else "false",
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default",
            "ReplaceAllMetadata": "false",
            "ReplaceAllImages": "false",
            "api_key": apikey,
        }
        response = RequestUtils(content_type="application/json").post_res(
            url=url,
            params=params,
        )
        if not response:
            raise RuntimeError(f"刷新 Emby 项目失败：{task.get('item_id')}")

    def _append_history(self, task: dict, success: bool, message: str):
        history = self.get_data("history") or []
        history.append({
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": task.get("item_name") or task.get("item_path"),
            "success": success,
            "message": message,
        })
        self.save_data("history", history[-300:])

    def _save_pending(self):
        self.save_data("pending", self._pending)

    def _schedule_queue_wakeup(self, key: str, due_at: float):
        """按任务到期时间精确唤醒；周期任务仅作为持久化队列兜底。"""
        if not self._scheduler or not self._scheduler.running:
            return
        timezone = pytz.timezone(settings.TZ)
        run_at = datetime.datetime.fromtimestamp(
            max(float(due_at), time.time() + 0.1),
            tz=timezone,
        )
        try:
            self._scheduler.add_job(
                func=self._drain_queue,
                trigger="date",
                run_date=run_at,
                id=f"EmbyAsyncImagesWakeup-{abs(hash(key))}",
                max_instances=1,
                replace_existing=True,
            )
        except Exception as err:
            logger.warning(f"Emby 异步图片刮削：创建任务唤醒失败，将由周期检查兜底：{err}")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "movie_delay_seconds": self._movie_delay_seconds,
            "tv_delay_seconds": self._tv_delay_seconds,
            "scan_interval": self._scan_interval,
            "max_retries": self._max_retries,
            "storage": self._storage,
            "path_mappings": self._path_mappings,
            "scrape_episode_images": self._scrape_episode_images,
            "refresh_emby": self._refresh_emby,
            "onlyonce": False,
        })

    @staticmethod
    def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default
