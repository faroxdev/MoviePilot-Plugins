"""
NextFind 订阅同步插件
拦截 MoviePilot 自带订阅处理，并定时将订阅信息同步到 NextFind。
"""
import datetime
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationType

lock = Lock()


class NextFindSubSync(_PluginBase):
    """NextFind 订阅同步"""

    plugin_name = "NextFind订阅同步"
    plugin_desc = "拦截 MoviePilot 自带订阅处理，并定时将订阅同步到 NextFind。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "1.0.1"
    plugin_author = "frh-hh"
    author_url = "https://github.com/frh-hh"
    plugin_config_prefix = "nextfindsubsync_"
    plugin_order = 21
    auth_level = 1

    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = False
    _cron: str = "0 */6 * * *"
    _nextfind_url: str = ""
    _api_key: str = ""
    _block_system_subscribe: bool = True
    _exclude_subscribes: List[int] = []
    _timeout: int = 20
    _force_resync: bool = False
    _scheduler: Optional[BackgroundScheduler] = None

    _VIRTUAL_SITE_ID = -2
    _VIRTUAL_SITE_NAME = "NextFind"

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._notify = bool(config.get("notify", False))
            self._cron = (config.get("cron", self._cron) or "").strip() or self._cron
            self._nextfind_url = self._normalize_openapi_url(config.get("nextfind_url", ""))
            self._api_key = (config.get("api_key", "") or "").strip()
            self._block_system_subscribe = bool(config.get("block_system_subscribe", True))
            self._exclude_subscribes = config.get("exclude_subscribes", []) or []
            self._timeout = int(config.get("timeout", self._timeout) or self._timeout)
            self._force_resync = bool(config.get("force_resync", False))

        if self._block_system_subscribe:
            self._apply_block_to_all_subscribes()

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.sync_subscribes,
                    trigger="date",
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                )
                if self._scheduler.get_jobs():
                    self._scheduler.start()
                self._onlyonce = False
                self.__update_config()

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception:
            pass

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/nextfind_sub_sync",
            "event": EventType.PluginAction,
            "desc": "NextFind订阅同步",
            "category": "订阅",
            "data": {"action": "nextfind_sub_sync"}
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as e:
            logger.warning(f"NextFind 订阅同步 Cron 表达式无效：{self._cron}，使用默认 6 小时间隔。错误：{e}")
            return [{
                "id": "NextFindSubSync",
                "name": "NextFind订阅同步服务",
                "trigger": "interval",
                "func": self.sync_subscribes,
                "kwargs": {"hours": 6}
            }]
        return [{
            "id": "NextFindSubSync",
            "name": "NextFind订阅同步服务",
            "trigger": trigger,
            "func": self.sync_subscribes,
            "kwargs": {}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/sync_subscribes",
            "endpoint": self.api_sync_subscribes,
            "methods": ["GET"],
            "summary": "同步订阅到 NextFind"
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
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
                                    "text": "将 MoviePilot 订阅同步到 NextFind。开启屏蔽后会把订阅站点改为 NextFind 虚拟站点，避免 MoviePilot 默认订阅下载。"
                                }
                            }]
                        }]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [{"component": "VSwitch", "props": {"model": "block_system_subscribe", "label": "屏蔽系统订阅"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即同步"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VCronField", "props": {"model": "cron", "label": "同步周期", "placeholder": "0 */6 * * *", "clearable": True}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "nextfind_url", "label": "NextFind OpenAPI 地址", "placeholder": "http://IP:端口/api/openapi", "clearable": True}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "api_key", "label": "NextFind API Key", "type": "password", "clearable": True}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [{"component": "VTextField", "props": {"model": "timeout", "label": "超时秒数", "type": "number", "placeholder": "20"}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "force_resync", "label": "重新同步已成功项"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 9}, "content": [{"component": "VTextField", "props": {"model": "exclude_subscribes", "label": "排除订阅ID", "placeholder": "多个 ID 用英文逗号分隔", "clearable": True}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "cron": "0 */6 * * *",
            "nextfind_url": "",
            "api_key": "",
            "block_system_subscribe": True,
            "exclude_subscribes": [],
            "timeout": 20,
            "force_resync": False
        }

    def get_page(self) -> Optional[List[dict]]:
        history = self.get_data("history") or []
        items = []
        for item in list(reversed(history))[:100]:
            color = "success" if item.get("status") == "成功" else "error"
            items.append({
                "component": "VListItem",
                "props": {"title": item.get("title"), "subtitle": item.get("message")},
                "content": [{"component": "template", "props": {"v-slot:prepend": ""}, "content": [{"component": "VIcon", "props": {"color": color}, "text": "mdi-cloud-sync"}]}]
            })
        if not items:
            items = [{"component": "VCardText", "props": {"class": "text-center"}, "text": "暂无同步记录"}]
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [{"component": "VList", "content": items}]
        }]

    def api_sync_subscribes(self, apikey: str) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API key 错误"}
        return self.sync_subscribes()

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        if not event:
            return
        event_data = event.event_data or {}
        if event_data.get("action") != "nextfind_sub_sync":
            return
        self.post_message(
            mtype=NotificationType.Plugin,
            channel=event_data.get("channel"),
            title="【NextFind订阅同步】开始执行",
            text="已收到远程命令，正在同步订阅。",
            userid=event_data.get("user")
        )
        self.sync_subscribes()

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event):
        if not self._block_system_subscribe:
            return
        subscribe_id = self._get_subscribe_id_from_event(event)
        if not subscribe_id:
            return
        try:
            self._set_subscribe_sites_to_nextfind(subscribe_id)
            logger.info(f"NextFind 订阅同步：新增订阅已改为 NextFind 虚拟站点（subscribe_id={subscribe_id}）")
        except Exception as e:
            logger.error(f"NextFind 订阅同步：新增订阅站点拦截失败：{e}")

    def sync_subscribes(self) -> dict:
        with lock:
            if not self._nextfind_url or not self._api_key:
                message = "NextFind URL 或 API Key 未配置"
                logger.error(message)
                return {"success": False, "message": message}

            if self._block_system_subscribe:
                self._apply_block_to_all_subscribes()

            with SessionFactory() as db:
                subscribes = SubscribeOper(db=db).list("N,R") or []

            exclude_ids = self._normalize_exclude_ids(self._exclude_subscribes)
            synced_keys = set(self.get_data("synced_keys") or [])
            history = self.get_data("history") or []

            total = success = skipped = failed = 0
            for subscribe in subscribes:
                if getattr(subscribe, "id", None) in exclude_ids:
                    skipped += 1
                    continue
                if getattr(subscribe, "type", None) not in [MediaType.MOVIE.value, MediaType.TV.value]:
                    skipped += 1
                    continue

                total += 1
                key = self._subscribe_key(subscribe)
                if key in synced_keys and not self._force_resync:
                    skipped += 1
                    continue

                ok, message = self._sync_one_subscribe(subscribe)
                record = {
                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "title": self._display_title(subscribe),
                    "status": "成功" if ok else "失败",
                    "message": message
                }
                history.append(record)
                if ok:
                    success += 1
                    synced_keys.add(key)
                else:
                    failed += 1

            self.save_data("synced_keys", list(synced_keys))
            self.save_data("history", history[-300:])
            self._force_resync = False
            self.__update_config()

            summary = f"NextFind 订阅同步完成：处理 {total}，成功 {success}，跳过 {skipped}，失败 {failed}"
            logger.info(summary)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title="【NextFind订阅同步】执行完成", text=summary)
            return {"success": failed == 0, "message": summary}

    def _sync_one_subscribe(self, subscribe) -> Tuple[bool, str]:
        tmdb_id = getattr(subscribe, "tmdbid", None)
        title = getattr(subscribe, "name", "") or ""
        media_type = self._to_nextfind_media_type(getattr(subscribe, "type", ""))

        if not tmdb_id:
            tmdb_id, title = self._search_tmdb_id(title, media_type)
        if not tmdb_id:
            return False, "订阅缺少 TMDB ID，且 NextFind 搜索未匹配到资源"

        payload = {"tmdb_id": int(tmdb_id), "title": title, "media_type": media_type}
        try:
            response = requests.post(
                f"{self._nextfind_url}/subscriptions/add",
                json=payload,
                headers={"X-API-Key": self._api_key},
                timeout=self._timeout
            )
            if 200 <= response.status_code < 300:
                return True, f"已添加到 NextFind：tmdb_id={tmdb_id}"
            return False, f"NextFind 返回 HTTP {response.status_code}: {response.text[:300]}"
        except Exception as e:
            return False, f"请求 NextFind 失败：{e}"

    def _search_tmdb_id(self, title: str, media_type: str) -> Tuple[Optional[int], str]:
        search_type = "电影" if media_type == "movie" else "电视剧"
        try:
            response = requests.get(
                f"{self._nextfind_url}/search",
                params={"query": title, "type": search_type, "page": 1},
                headers={"X-API-Key": self._api_key},
                timeout=self._timeout
            )
            if not (200 <= response.status_code < 300):
                logger.warning(f"NextFind 搜索失败：HTTP {response.status_code} {response.text[:300]}")
                return None, title
            data = response.json()
            candidates = self._extract_search_items(data)
            for item in candidates:
                item_type = item.get("media_type") or item.get("type")
                if item_type in [media_type, search_type, None]:
                    tmdb_id = item.get("tmdb_id") or item.get("tmdbid") or item.get("id")
                    if tmdb_id:
                        return int(tmdb_id), item.get("title") or item.get("name") or title
        except Exception as e:
            logger.warning(f"NextFind 搜索异常：{e}")
        return None, title

    @staticmethod
    def _extract_search_items(data: Any) -> List[dict]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if not isinstance(data, dict):
            return []
        for key in ["data", "results", "items", "list"]:
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = NextFindSubSync._extract_search_items(value)
                if nested:
                    return nested
        return []

    @staticmethod
    def _to_nextfind_media_type(media_type: str) -> str:
        return "tv" if media_type == MediaType.TV.value else "movie"

    @staticmethod
    def _subscribe_key(subscribe) -> str:
        media_type = getattr(subscribe, "type", "")
        tmdb_id = getattr(subscribe, "tmdbid", None)
        if tmdb_id:
            return f"{media_type}:{tmdb_id}"
        return f"{media_type}:{getattr(subscribe, 'name', '')}:{getattr(subscribe, 'year', '')}"

    @staticmethod
    def _display_title(subscribe) -> str:
        name = getattr(subscribe, "name", "") or ""
        year = getattr(subscribe, "year", "") or ""
        if getattr(subscribe, "type", "") == MediaType.TV.value:
            return f"{name} ({year}) S{getattr(subscribe, 'season', None) or 1}"
        return f"{name} ({year})" if year else name

    @staticmethod
    def _normalize_exclude_ids(value: Any) -> set:
        if isinstance(value, str):
            parts = [x.strip() for x in value.split(",")]
        elif isinstance(value, list):
            parts = value
        else:
            parts = []
        out = set()
        for item in parts:
            try:
                out.add(int(item))
            except Exception:
                pass
        return out

    @staticmethod
    def _normalize_openapi_url(value: Any) -> str:
        url = (str(value or "")).strip().rstrip("/")
        if not url:
            return ""
        return url if url.endswith("/api/openapi") else f"{url}/api/openapi"

    def _apply_block_to_all_subscribes(self):
        try:
            with SessionFactory() as db:
                self._ensure_nextfind_site(db)
                subscribe_oper = SubscribeOper(db=db)
                subscribes = subscribe_oper.list() or []
                exclude_ids = self._normalize_exclude_ids(self._exclude_subscribes)
                for subscribe in subscribes:
                    if getattr(subscribe, "id", None) in exclude_ids:
                        continue
                    self._set_subscribe_sites_to_nextfind(getattr(subscribe, "id"), db=db)
        except Exception as e:
            logger.error(f"NextFind 订阅同步：屏蔽系统订阅失败：{e}")

    def _set_subscribe_sites_to_nextfind(self, subscribe_id: int, db=None):
        if not subscribe_id:
            return
        if db is None:
            with SessionFactory() as db_session:
                self._ensure_nextfind_site(db_session)
                storage = self._guess_sites_storage_format(db_session, int(subscribe_id))
                value = str(self._VIRTUAL_SITE_ID) if storage == "str" else [self._VIRTUAL_SITE_ID]
                SubscribeOper(db=db_session).update(int(subscribe_id), {"sites": value})
            return
        storage = self._guess_sites_storage_format(db, int(subscribe_id))
        value = str(self._VIRTUAL_SITE_ID) if storage == "str" else [self._VIRTUAL_SITE_ID]
        SubscribeOper(db=db).update(int(subscribe_id), {"sites": value})

    def _ensure_nextfind_site(self, db):
        row = db.execute(text("SELECT id FROM site WHERE id=:id"), {"id": self._VIRTUAL_SITE_ID}).fetchone()
        if row:
            return
        db.execute(
            text(
                "INSERT INTO site (id, name, url, is_active, limit_interval, limit_count, limit_seconds, timeout) "
                "VALUES (:id,:name,:url,:is_active,:limit_interval,:limit_count,:limit_seconds,:timeout)"
            ),
            {
                "id": self._VIRTUAL_SITE_ID,
                "name": self._VIRTUAL_SITE_NAME,
                "url": self._nextfind_url or "http://nextfind.local",
                "is_active": True,
                "limit_interval": 10000000,
                "limit_count": 1,
                "limit_seconds": 10000000,
                "timeout": 1
            }
        )
        db.commit()
        logger.info("NextFind 订阅同步：已添加 NextFind 虚拟站点(id=-2)")

    @staticmethod
    def _guess_sites_storage_format(db, subscribe_id: int) -> str:
        subscribe = SubscribeOper(db=db).get(int(subscribe_id))
        sites = getattr(subscribe, "sites", None) if subscribe else None
        return "str" if isinstance(sites, str) else "list"

    @staticmethod
    def _get_subscribe_id_from_event(event: Event) -> Optional[int]:
        data = event.event_data or {}
        subscribe_id = data.get("subscribe_id") or data.get("id")
        if not subscribe_id and isinstance(data.get("subscribe"), dict):
            subscribe_id = data["subscribe"].get("id")
        try:
            return int(subscribe_id) if subscribe_id is not None else None
        except Exception:
            return None

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "cron": self._cron,
            "nextfind_url": self._nextfind_url,
            "api_key": self._api_key,
            "block_system_subscribe": self._block_system_subscribe,
            "exclude_subscribes": self._exclude_subscribes,
            "timeout": self._timeout,
            "force_resync": self._force_resync
        })
