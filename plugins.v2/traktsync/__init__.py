import datetime
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, List, Optional, Tuple, Type
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

from app import schemas
from app.agent.tools.base import MoviePilotTool
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.core.metainfo import MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.event import DiscoverMediaSource, DiscoverSourceEventData
from app.schemas.types import ChainEventType, MediaType


_token_refresh_lock = Lock()
_sync_lock = Lock()
_calendar_refresh_lock = Lock()


class TraktRequestError(RuntimeError):
    """不携带请求头和响应正文的 Trakt 请求错误。"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class GetTraktListsInput(BaseModel):
    """Trakt 榜单查询参数。"""

    explanation: Optional[str] = Field(
        None,
        description="Clear explanation of why this tool is being used in the current context",
    )
    category: str = Field(
        "popular",
        description="Allowed values: popular, trending, anticipated, watched, collected, recommended, boxoffice",
    )
    media_type: str = Field("movies", description="Allowed values: movies, shows")
    period: str = Field(
        "weekly",
        description="Allowed values: daily, weekly, monthly, yearly, all. Only used by watched and collected.",
    )
    page: int = Field(1, ge=1, description="Page number, starting at 1")
    limit: int = Field(20, ge=1, le=100, description="Items per page, from 1 to 100")
    force_refresh: bool = Field(False, description="Ignore a fresh cache entry and request Trakt now")


class GetTraktPersonalDataInput(BaseModel):
    """Trakt 个人数据查询参数。"""

    explanation: Optional[str] = Field(
        None,
        description="Clear explanation of why this tool is being used in the current context",
    )
    data_type: str = Field(
        "watchlist",
        description="Allowed values: watchlist, collection, history, up_next, stats",
    )
    media_type: str = Field(
        "all",
        description="Allowed values: movies, shows, seasons, episodes, all",
    )
    page: int = Field(1, ge=1, description="Page number, starting at 1")
    limit: int = Field(20, ge=1, le=100, description="Items per page, from 1 to 100")
    start_at: Optional[str] = Field(None, description="History start time in RFC3339 format")
    end_at: Optional[str] = Field(None, description="History end time in RFC3339 format")
    force_refresh: bool = Field(False, description="Ignore a fresh cache entry and request Trakt now")


class GetTraktCustomListsInput(BaseModel):
    """Trakt 自定义列表查询参数。"""

    explanation: Optional[str] = Field(
        None,
        description="Clear explanation of why this tool is being used in the current context",
    )
    list_id: Optional[int] = Field(
        None,
        ge=1,
        description="Omit to list personal lists; provide a Trakt list ID to read its items",
    )
    media_type: str = Field(
        "all",
        description="Allowed values: movies, shows, seasons, episodes, all",
    )
    page: int = Field(1, ge=1, description="Page number, starting at 1")
    limit: int = Field(20, ge=1, le=100, description="Items per page, from 1 to 100")
    force_refresh: bool = Field(False, description="Ignore a fresh cache entry and request Trakt now")


class GetTraktCalendarInput(BaseModel):
    """Trakt 日历查询参数。"""

    explanation: Optional[str] = Field(
        None,
        description="Clear explanation of why this tool is being used in the current context",
    )
    target: str = Field("my", description="Allowed values: my, all")
    calendar_type: str = Field(
        "shows",
        description=(
            "Allowed values: shows, movies, new_shows, season_premieres, "
            "finales, dvd"
        ),
    )
    start_date: Optional[str] = Field(
        None,
        description="Calendar start date in YYYY-MM-DD format; defaults to today",
    )
    days: int = Field(14, ge=1, le=33, description="Number of days, from 1 to 33")
    page: int = Field(1, ge=1, description="Local page number, starting at 1")
    limit: int = Field(20, ge=1, le=100, description="Items per page, from 1 to 100")
    force_refresh: bool = Field(False, description="Ignore fresh calendar caches and request Trakt now")


class CacheRefreshRequest(BaseModel):
    """缓存刷新请求。"""

    scope: str = Field("all", description="Allowed values: all, public, personal")


class CustomListSelectionRequest(BaseModel):
    """自定义列表选择请求。"""

    list_id: int = Field(..., ge=1)
    selected: bool = Field(...)


class HistoryDeleteRequest(BaseModel):
    """同步历史删除请求。"""

    id: str = Field(...)


def _json_output(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _tool_failure(code: str, message: str) -> str:
    return _json_output(
        {
            "success": False,
            "meta": {"error": {"code": code, "message": message}},
            "data": [],
        }
    )


class GetTraktListsTool(MoviePilotTool):
    """查询公开或个性化 Trakt 榜单。"""

    name: str = "get_trakt_lists"
    description: str = (
        "Get Trakt movie and show charts: popular, trending, anticipated, watched, "
        "collected, recommended, and movie box office. Recommended requires an "
        "administrator and the configured Trakt OAuth account."
    )
    args_schema: Type[BaseModel] = GetTraktListsInput
    _plugin: Any = None

    def __init__(self, session_id: str, user_id: str, plugin_instance=None):
        super().__init__(session_id=session_id, user_id=user_id)
        self._plugin = plugin_instance

    def get_tool_message(self, **kwargs) -> Optional[str]:
        category = kwargs.get("category", "popular")
        media_type = kwargs.get("media_type", "movies")
        page = kwargs.get("page", 1)
        return f"查询 Trakt {category} {media_type} 榜单（第 {page} 页）"

    async def run(
        self,
        category: str = "popular",
        media_type: str = "movies",
        period: str = "weekly",
        page: int = 1,
        limit: int = 20,
        force_refresh: bool = False,
        **kwargs,
    ) -> str:
        if not self._plugin:
            return _tool_failure("plugin_unavailable", "TraktSync 插件实例未初始化")
        if (category or "").lower() == "recommended" and not await self.is_admin_user():
            return _tool_failure("admin_required", "Trakt 个性化推荐仅允许管理员查询")
        try:
            payload = await self.run_blocking(
                "plugin",
                self._plugin.get_trakt_lists,
                category,
                media_type,
                period,
                page,
                limit,
                force_refresh,
            )
        except Exception:
            return _tool_failure("query_failed", "Trakt 榜单查询失败")
        return _json_output(payload)


class GetTraktPersonalDataTool(MoviePilotTool):
    """查询管理员 Trakt 账户的个人数据。"""

    name: str = "get_trakt_personal_data"
    description: str = (
        "Get the configured administrator Trakt account watchlist, collection, "
        "history, up-next progress, or stats. Read-only."
    )
    args_schema: Type[BaseModel] = GetTraktPersonalDataInput
    require_admin: bool = True
    _plugin: Any = None

    def __init__(self, session_id: str, user_id: str, plugin_instance=None):
        super().__init__(session_id=session_id, user_id=user_id)
        self._plugin = plugin_instance

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"查询 Trakt 个人数据：{kwargs.get('data_type', 'watchlist')}"

    async def run(
        self,
        data_type: str = "watchlist",
        media_type: str = "all",
        page: int = 1,
        limit: int = 20,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        force_refresh: bool = False,
        **kwargs,
    ) -> str:
        if not self._plugin:
            return _tool_failure("plugin_unavailable", "TraktSync 插件实例未初始化")
        try:
            payload = await self.run_blocking(
                "plugin",
                self._plugin.get_trakt_personal_data,
                data_type,
                media_type,
                page,
                limit,
                start_at,
                end_at,
                force_refresh,
            )
        except Exception:
            return _tool_failure("query_failed", "Trakt 个人数据查询失败")
        return _json_output(payload)


class GetTraktCustomListsTool(MoviePilotTool):
    """查询管理员 Trakt 账户的自定义列表。"""

    name: str = "get_trakt_custom_lists"
    description: str = (
        "List the configured administrator Trakt account personal lists, or browse "
        "movie, show, season, and episode items from one list. Read-only."
    )
    args_schema: Type[BaseModel] = GetTraktCustomListsInput
    require_admin: bool = True
    _plugin: Any = None

    def __init__(self, session_id: str, user_id: str, plugin_instance=None):
        super().__init__(session_id=session_id, user_id=user_id)
        self._plugin = plugin_instance

    def get_tool_message(self, **kwargs) -> Optional[str]:
        list_id = kwargs.get("list_id")
        return "查询 Trakt 自定义列表" if not list_id else f"查询 Trakt 列表 {list_id} 的条目"

    async def run(
        self,
        list_id: Optional[int] = None,
        media_type: str = "all",
        page: int = 1,
        limit: int = 20,
        force_refresh: bool = False,
        **kwargs,
    ) -> str:
        if not self._plugin:
            return _tool_failure("plugin_unavailable", "TraktSync 插件实例未初始化")
        try:
            payload = await self.run_blocking(
                "plugin",
                self._plugin.get_trakt_custom_lists,
                list_id,
                media_type,
                page,
                limit,
                force_refresh,
            )
        except Exception:
            return _tool_failure("query_failed", "Trakt 自定义列表查询失败")
        return _json_output(payload)


class GetTraktCalendarTool(MoviePilotTool):
    """查询 Trakt 播出和发行日历。"""

    name: str = "get_trakt_calendar"
    description: str = (
        "Get Trakt personal or public calendars for shows, movies, new shows, "
        "season premieres, finales, and DVD releases. Personal calendars require "
        "an administrator and the configured Trakt OAuth account. Read-only."
    )
    args_schema: Type[BaseModel] = GetTraktCalendarInput
    _plugin: Any = None

    def __init__(self, session_id: str, user_id: str, plugin_instance=None):
        super().__init__(session_id=session_id, user_id=user_id)
        self._plugin = plugin_instance

    def get_tool_message(self, **kwargs) -> Optional[str]:
        target = kwargs.get("target", "my")
        calendar_type = kwargs.get("calendar_type", "shows")
        return f"查询 Trakt {target} {calendar_type} 日历"

    async def run(
        self,
        target: str = "my",
        calendar_type: str = "shows",
        start_date: Optional[str] = None,
        days: int = 14,
        page: int = 1,
        limit: int = 20,
        force_refresh: bool = False,
        **kwargs,
    ) -> str:
        if not self._plugin:
            return _tool_failure("plugin_unavailable", "TraktSync 插件实例未初始化")
        if (target or "").lower() == "my" and not await self.is_admin_user():
            return _tool_failure("admin_required", "Trakt 个人日历仅允许管理员查询")
        try:
            payload = await self.run_blocking(
                "plugin",
                self._plugin.get_trakt_calendar,
                target,
                calendar_type,
                start_date,
                days,
                page,
                limit,
                force_refresh,
            )
        except Exception:
            return _tool_failure("query_failed", "Trakt 日历查询失败")
        return _json_output(payload)


class TraktSync(_PluginBase):
    plugin_name = "Trakt Watchlist Sync"
    plugin_desc = "同步 Trakt Watchlist 和自定义列表，并提供榜单、个人数据及日历 MCP 查询"
    plugin_icon = "https://raw.githubusercontent.com/cyt-666/MoviePilot-Plugins/main/icons/trakt.png"
    plugin_author = "cyt-666"
    plugin_version = "0.6.1"
    author_url = "https://github.com/cyt-666/MoviePilot-Plugins"
    plugin_config_prefix = "traktsync_"
    plugin_order = 3
    auth_level = 2

    _device_code_url = "https://api.trakt.tv/oauth/device/code"
    _token_url = "https://api.trakt.tv/oauth/device/token"
    _refresh_token_url = "https://api.trakt.tv/oauth/token"
    _trakt_api_base = "https://api.trakt.tv"

    _request_timeout = (10, 30)
    _public_cache_ttl = 6 * 3600
    _personal_cache_ttl = 15 * 60
    _calendar_cache_ttl = 15 * 60
    _custom_list_catalog_ttl = 3600
    _account_ttl = 3600
    _trakt_page_size = 20
    _trakt_recommendation_limit = 100

    _cache_prefix = "trakt_cache_v2_"
    _discover_cache_prefix = "trakt_discover_v2_"
    _account_key = "trakt_account_v1"
    _device_auth_key = "trakt_device_authorization_v1"
    _custom_list_catalog_key = "trakt_custom_lists_catalog_v1"
    _selected_lists_key = "trakt_selected_lists_v1"
    _sync_state_key = "trakt_sync_state_v2"
    _sync_status_key = "trakt_sync_status_v1"
    _legacy_history_migration_key = "trakt_sync_history_migration_v1"
    _calendar_snapshot_prefix = "trakt_calendar_snapshot_v1_"
    _calendar_page_prefix = "trakt_calendar_page_v1_"
    _calendar_status_prefix = "trakt_calendar_status_v1_"

    _calendar_show_types = {
        "shows",
        "new_shows",
        "season_premieres",
        "finales",
    }
    _calendar_paths = {
        "shows": "shows",
        "movies": "movies",
        "new_shows": "shows/new",
        "season_premieres": "shows/premieres",
        "finales": "shows/finales",
        "dvd": "dvd",
    }
    _calendar_state_labels = {
        "in_library": "已入库",
        "downloading": "下载中",
        "pending_library": "待入库",
        "unaired": "未播出",
        "subscribed": "已订阅",
        "missing": "缺失",
        "unknown": "状态未知",
    }

    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None
    downloadchain = None
    subscribechain = None
    mediachain = None
    token: dict = {}

    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False
    _client_id: str = ""
    _client_secret: str = ""
    _media_type: str = "all"

    _enable_popular_movies: bool = False
    _enable_popular_shows: bool = False
    _enable_trending_movies: bool = False
    _enable_trending_shows: bool = False
    _enable_recommended_movies: bool = False
    _enable_recommended_shows: bool = False
    _enable_anticipated_movies: bool = False
    _enable_anticipated_shows: bool = False
    _enable_watched_movies: bool = False
    _enable_watched_shows: bool = False
    _enable_collected_movies: bool = False
    _enable_collected_shows: bool = False
    _enable_boxoffice_movies: bool = False

    _chart_switches = (
        ("enable_popular_movies", "_enable_popular_movies", "热门电影"),
        ("enable_trending_movies", "_enable_trending_movies", "趋势电影"),
        ("enable_recommended_movies", "_enable_recommended_movies", "推荐电影"),
        ("enable_anticipated_movies", "_enable_anticipated_movies", "待映电影"),
        ("enable_watched_movies", "_enable_watched_movies", "观看电影（周榜）"),
        ("enable_collected_movies", "_enable_collected_movies", "收藏电影（周榜）"),
        ("enable_boxoffice_movies", "_enable_boxoffice_movies", "电影票房"),
        ("enable_popular_shows", "_enable_popular_shows", "热门剧集"),
        ("enable_trending_shows", "_enable_trending_shows", "趋势剧集"),
        ("enable_recommended_shows", "_enable_recommended_shows", "推荐剧集"),
        ("enable_anticipated_shows", "_enable_anticipated_shows", "待映剧集"),
        ("enable_watched_shows", "_enable_watched_shows", "观看剧集（周榜）"),
        ("enable_collected_shows", "_enable_collected_shows", "收藏剧集（周榜）"),
    )

    def init_plugin(self, config: dict = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.mediachain = MediaChain()
        self.token = {}

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._cron = config.get("cron") or ""
        self._notify = bool(config.get("notify"))
        self._media_type = config.get("media_type") or "all"
        self._client_id = config.get("client_id") or ""
        self._client_secret = config.get("client_secret") or ""
        for config_key, attr_name, _ in self._chart_switches:
            setattr(self, attr_name, bool(config.get(config_key, False)))

        if not self._client_id:
            logger.error("Trakt Client ID 未设置")
            return

        self.token = self.get_data("token") or {}
        if not self.token and self._client_secret:
            code = self.device_code_request()
            if code:
                interval = max(int(code.get("interval") or 5), 1)
                expires_in = max(int(code.get("expires_in") or 600), interval)
                self.save_data(
                    self._device_auth_key,
                    {
                        "verification_url": code.get("verification_url"),
                        "user_code": code.get("user_code"),
                        "expires_at": int(time.time()) + expires_in,
                    },
                )
                token_thread = Thread(
                    target=self._threaded_token_request,
                    args=(code.get("device_code"), interval, expires_in // interval),
                    daemon=True,
                )
                token_thread.start()
                logger.info("Trakt 设备授权已启动，请在插件详情页查看授权状态")
            else:
                logger.error("Trakt 设备授权请求失败")
        elif not self.token:
            logger.warning("Trakt Client Secret 未设置，仅公开榜单可用")

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.sync_watchlist,
                    trigger="date",
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3),
                )
                if self._scheduler.get_jobs():
                    self._scheduler.start()
            if self._onlyonce:
                self._onlyonce = False
                self.__update_config()

        self._start_calendar_prefetch_if_needed()

    def _start_calendar_prefetch_if_needed(self):
        """启用且已有 OAuth 时，在缺少页面快照的情况下预取一次。"""
        if not self._enabled or not self.token:
            return
        account = (self.get_data(self._account_key) or {}).get("data") or {}
        if self.get_data(self._calendar_page_data_key(account.get("uuid"))) is not None:
            return
        if not _calendar_refresh_lock.acquire(blocking=False):
            return
        try:
            Thread(
                target=self.refresh_calendar_page,
                kwargs={"force_refresh": False, "lock_acquired": True},
                daemon=True,
            ).start()
        except Exception:
            _calendar_refresh_lock.release()
            raise

    def _threaded_token_request(self, device_code: str, interval: int, count: int):
        """轮询设备授权结果，不在日志中输出授权码或凭证。"""
        if not device_code:
            return
        for _ in range(int(count)):
            time.sleep(interval)
            self.token = self.token_request(device_code)
            if self.token:
                self.del_data(self._device_auth_key)
                try:
                    self._get_account(force_refresh=True)
                except TraktRequestError:
                    logger.warning("Trakt OAuth 已授权，但账户资料暂未刷新")
                logger.info("Trakt OAuth 授权成功")
                self._start_calendar_prefetch_if_needed()
                return
        logger.error("Trakt OAuth 授权超时")

    def __update_config(self):
        config = {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "media_type": self._media_type,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        for config_key, attr_name, _ in self._chart_switches:
            config[config_key] = bool(getattr(self, attr_name, False))
        self.update_config(config)

    @staticmethod
    def _switch_col(model: str, label: str, md: int = 3) -> Dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [
                {
                    "component": "VSwitch",
                    "props": {"model": model, "label": label},
                }
            ],
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        chart_rows = []
        for start in range(0, len(self._chart_switches), 4):
            chart_rows.append(
                {
                    "component": "VRow",
                    "content": [
                        self._switch_col(config_key, label)
                        for config_key, _, label in self._chart_switches[start : start + 4]
                    ],
                }
            )

        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._switch_col("enabled", "启用插件", 4),
                            self._switch_col("notify", "发送通知", 4),
                            self._switch_col("onlyonce", "立即运行一次", 4),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "5位 cron 表达式，留空则每30分钟",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "media_type",
                                            "label": "Watchlist 同步类型",
                                            "items": [
                                                {"title": "全部", "value": "all"},
                                                {"title": "电影", "value": "movie"},
                                                {"title": "电视剧", "value": "show"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "client_id",
                                            "label": "Client ID",
                                            "placeholder": "Trakt Client ID",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "client_secret",
                                            "label": "Client Secret",
                                            "type": "password",
                                            "placeholder": "Trakt Client Secret",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VDivider", "props": {"class": "my-2"}},
                                    {
                                        "component": "div",
                                        "props": {"class": "text-h6 mb-2"},
                                        "text": "Trakt 榜单探索（新榜单默认关闭）",
                                    },
                                ],
                            }
                        ],
                    },
                    *chart_rows,
                ],
            }
        ]
        defaults = {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "*/30 * * * *",
            "media_type": "all",
            "client_id": "",
            "client_secret": "",
        }
        defaults.update({config_key: False for config_key, _, _ in self._chart_switches})
        return form, defaults

    @staticmethod
    def _action_button(
        text: str,
        api: str,
        params: Optional[dict] = None,
        icon: str = "mdi-refresh",
        color: str = "primary",
    ) -> Dict[str, Any]:
        return {
            "component": "VBtn",
            "props": {
                "variant": "tonal",
                "color": color,
                "class": "mr-2 mb-2",
                "prepend-icon": icon,
            },
            "text": text,
            "events": {
                "click": {
                    "api": f"plugin/TraktSync/{api}",
                    "method": "post",
                    "params": params or {},
                }
            },
        }

    def _calendar_page_card(
        self,
        account_uuid: Optional[str],
        account_connected: bool,
    ) -> dict:
        """使用本地快照构建个人剧集日历卡片。"""
        page_record = self.get_data(self._calendar_page_data_key(account_uuid)) or {}
        refresh_status = self.get_data(
            self._calendar_status_data_key(account_uuid)
        ) or {}
        items = [
            self._calendar_item_with_normalized_poster(item)
            for item in (page_record.get("data") or [])
        ]
        start_date = page_record.get("start_date")
        days = int(page_record.get("days") or 14)
        fetched_at = page_record.get("fetched_at") or "-"
        status_state = refresh_status.get("state") or "never"
        status_message = refresh_status.get("message") or "尚未刷新"
        status_time = (
            refresh_status.get("finished_at")
            or refresh_status.get("started_at")
            or "-"
        )

        if start_date:
            try:
                start = datetime.date.fromisoformat(start_date)
                end = start + datetime.timedelta(days=max(days - 1, 0))
                range_text = f"{start.strftime('%m-%d')} - {end.strftime('%m-%d')}"
            except ValueError:
                range_text = f"未来 {days} 天"
        else:
            range_text = "未来 14 天"
        today = self._calendar_today()
        today_count = sum(1 for item in items if item.get("local_date") == today)

        if not account_connected:
            description = "请先完成 Trakt OAuth 授权后刷新个人剧集日历。"
        elif not items and page_record:
            description = "当前日期范围内没有个人剧集播出日程。"
        elif not items:
            description = "暂无本地日历快照，请点击“刷新日历”。"
        else:
            description = (
                f"{range_text} · 共 {len(items)} 集 · 今天 {today_count} 集 · "
                f"最后刷新 {fetched_at}"
            )

        content = [
            {
                "component": "div",
                "props": {
                    "class": "d-flex flex-wrap align-center justify-space-between px-4 pt-3"
                },
                "content": [
                    {"component": "div", "props": {"class": "text-h6"}, "text": "个人剧集日历"},
                    self._action_button(
                        "刷新日历",
                        "calendar/refresh",
                        icon="mdi-calendar-refresh",
                    ),
                ],
            },
            {"component": "VCardText", "text": description},
            {
                "component": "VCardText",
                "props": {"class": "pt-0 text-medium-emphasis"},
                "text": f"刷新状态：{status_state}；{status_message}；时间：{status_time}",
            },
        ]

        state_colors = {
            "in_library": "success",
            "downloading": "info",
            "pending_library": "warning",
            "unaired": "secondary",
            "subscribed": "primary",
            "missing": "error",
            "unknown": "default",
        }
        weekday_names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        grouped = {}
        for item in items:
            grouped.setdefault(item.get("local_date") or "待定", []).append(item)
        for local_date in sorted(grouped):
            if local_date == "待定":
                date_title = "待定"
            else:
                try:
                    date_value = datetime.date.fromisoformat(local_date)
                    date_title = (
                        f"{date_value.strftime('%m月%d日')} "
                        f"{weekday_names[date_value.weekday()]}"
                    )
                except ValueError:
                    date_title = local_date
            episode_cards = []
            for item in sorted(grouped[local_date], key=self._calendar_sort_key):
                season = self._as_int(item.get("season")) or 0
                episode = self._as_int(item.get("episode")) or 0
                state = item.get("moviepilot_state") or "unknown"
                state_label = item.get("moviepilot_state_label") or self._calendar_state_labels[
                    "unknown"
                ]
                if item.get("moviepilot_state_stale"):
                    state_label = f"{state_label}（旧状态）"
                subtitle_parts = [f"S{season:02d}E{episode:02d}"]
                if item.get("episode_title"):
                    subtitle_parts.append(str(item.get("episode_title")))
                detail_parts = []
                if item.get("local_time"):
                    detail_parts.append(str(item.get("local_time")))
                if item.get("network"):
                    detail_parts.append(str(item.get("network")))
                row_content = []
                if item.get("poster"):
                    row_content.append(
                        {
                            "component": "VImg",
                            "props": {
                                "src": item.get("poster"),
                                "height": 120,
                                "width": 80,
                                "cover": True,
                            },
                        }
                    )
                row_content.append(
                    {
                        "component": "div",
                        "props": {"class": "flex-grow-1 pa-3"},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "text-subtitle-1 font-weight-bold"},
                                "text": item.get("show_title") or "未知剧集",
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-body-2 mt-1"},
                                "text": " · ".join(subtitle_parts),
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-caption text-medium-emphasis mt-1"},
                                "text": " · ".join(detail_parts) or "播出时间待定",
                            },
                            {
                                "component": "VChip",
                                "props": {
                                    "size": "small",
                                    "variant": "tonal",
                                    "color": state_colors.get(state, "default"),
                                    "class": "mt-2",
                                },
                                "text": state_label,
                            },
                        ],
                    }
                )
                episode_cards.append(
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal"},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "d-flex flex-row"},
                                "content": row_content,
                            }
                        ],
                    }
                )
            content.append(
                {
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "mx-4 mb-3"},
                    "content": [
                        {"component": "VCardTitle", "text": date_title},
                        {
                            "component": "div",
                            "props": {"class": "grid gap-3 grid-info-card px-4 pb-4"},
                            "content": episode_cards,
                        },
                    ],
                }
            )

        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-3"},
            "content": content,
        }

    def get_page(self) -> List[dict]:
        """详情页只读取插件本地数据，远程操作均通过 Bearer API 触发。"""
        account_record = self.get_data(self._account_key) or {}
        account = account_record.get("data") or {}
        device_auth = self.get_data(self._device_auth_key) or {}
        sync_status = self.get_data(self._sync_status_key) or {}
        catalog_record = self.get_data(
            self._custom_list_catalog_data_key(account.get("uuid"))
        ) or {}
        catalog = catalog_record.get("data") or []
        selected_ids = set(self._selected_list_ids())

        if account:
            account_text = (
                f"已连接：{account.get('username') or account.get('slug') or 'Trakt 用户'}"
                f"（UUID：{account.get('uuid', '-') }）"
            )
        elif device_auth and int(device_auth.get("expires_at") or 0) > int(time.time()):
            account_text = (
                f"等待授权：请访问 {device_auth.get('verification_url') or 'Trakt'}，"
                f"输入代码 {device_auth.get('user_code') or '-'}"
            )
        else:
            account_text = "尚未检测到已授权的 Trakt 账户"

        sync_state = sync_status.get("state") or "never"
        sync_message = sync_status.get("message") or "尚未同步"
        sync_finished = sync_status.get("finished_at") or sync_status.get("started_at") or "-"
        status_text = f"状态：{sync_state}；{sync_message}；时间：{sync_finished}"

        pages: List[dict] = [
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {"component": "VCardTitle", "text": "Trakt 账户与同步状态"},
                    {"component": "VCardText", "text": account_text},
                    {"component": "VCardText", "props": {"class": "pt-0"}, "text": status_text},
                    {
                        "component": "VCardActions",
                        "props": {"class": "flex-wrap"},
                        "content": [
                            self._action_button(
                                "立即同步",
                                "sync_now",
                                icon="mdi-sync",
                            ),
                            self._action_button(
                                "刷新全部缓存",
                                "cache/refresh",
                                {"scope": "all"},
                                icon="mdi-cached",
                            ),
                            self._action_button(
                                "刷新自定义列表",
                                "custom_lists/refresh",
                                icon="mdi-playlist-refresh",
                            ),
                        ],
                    },
                ],
            }
        ]
        pages.append(
            self._calendar_page_card(
                account.get("uuid"),
                account_connected=bool(account),
            )
        )

        list_cards = []
        for item in catalog:
            list_id = item.get("list_id")
            selected = list_id in selected_ids
            list_cards.append(
                {
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {"component": "VCardTitle", "text": item.get("name") or f"列表 {list_id}"},
                        {
                            "component": "VCardSubtitle",
                            "text": (
                                f"{item.get('privacy') or '-'} · "
                                f"{item.get('item_count', 0)} 项 · "
                                f"{'已选为订阅源' if selected else '未选择'}"
                            ),
                        },
                        {
                            "component": "VCardText",
                            "text": item.get("description") or "暂无描述",
                        },
                        {
                            "component": "VCardActions",
                            "content": [
                                self._action_button(
                                    "取消选择" if selected else "选择并同步",
                                    "custom_lists/select",
                                    {"list_id": list_id, "selected": not selected},
                                    icon="mdi-playlist-check" if not selected else "mdi-playlist-remove",
                                    color="warning" if selected else "primary",
                                )
                            ],
                        },
                    ],
                }
            )

        pages.append(
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {"component": "VCardTitle", "text": "自定义列表订阅源"},
                    {
                        "component": "VCardText",
                        "text": (
                            "只会为列表中的电影和整剧创建订阅；"
                            "季度和单集仅可通过 MCP 浏览。"
                            if list_cards
                            else "暂无本地列表缓存，请点击“刷新自定义列表”。"
                        ),
                    },
                    {
                        "component": "div",
                        "props": {"class": "grid gap-3 grid-info-card px-4 pb-4"},
                        "content": list_cards,
                    },
                ],
            }
        )

        histories = self.get_data("history") or {}
        history_items = []
        for history_id, history in histories.items():
            item = dict(history)
            item["id"] = str(history_id)
            history_items.append(item)
        history_items.sort(key=lambda row: row.get("time") or "", reverse=True)

        history_cards = []
        for history in history_items:
            title = history.get("title") or "-"
            if history.get("season") is not None:
                title = f"{title} 第{history.get('season')}季"
            action_map = {"subscribe": "订阅", "exist": "已存在", "download": "下载"}
            history_cards.append(
                {
                    "component": "VCard",
                    "props": {"variant": "outlined"},
                    "content": [
                        {
                            "component": "VDialogCloseBtn",
                            "events": {
                                "click": {
                                    "api": "plugin/TraktSync/history/delete",
                                    "method": "post",
                                    "params": {"id": history.get("id")},
                                }
                            },
                        },
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-row"},
                            "content": [
                                {
                                    "component": "VImg",
                                    "props": {
                                        "src": history.get("poster"),
                                        "height": 120,
                                        "width": 80,
                                        "cover": True,
                                    },
                                },
                                {
                                    "component": "div",
                                    "content": [
                                        {"component": "VCardTitle", "text": title},
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "py-0"},
                                            "text": f"来源：{history.get('source') or 'watchlist'}",
                                        },
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "py-0"},
                                            "text": (
                                                "操作："
                                                f"{action_map.get(history.get('action'), history.get('action'))}"
                                            ),
                                        },
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "py-0"},
                                            "text": f"时间：{history.get('time') or '-'}",
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                }
            )

        pages.append(
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "text": "同步历史"},
                    {
                        "component": "VCardText",
                        "text": (
                            "历史记录仅用于展示，实际去重由每个来源的独立同步状态完成。"
                        )
                        if history_cards
                        else "暂无同步历史。",
                    },
                    {
                        "component": "div",
                        "props": {"class": "grid gap-3 grid-info-card px-4 pb-4"},
                        "content": history_cards,
                    },
                ],
            }
        )
        return pages

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/trakt_discover",
                "endpoint": self._trakt_discover_endpoint,
                "methods": ["GET"],
                "summary": "Trakt 榜单探索数据",
                "auth": "bear",
            },
            {
                "path": "/sync_now",
                "endpoint": self.api_sync_now,
                "methods": ["POST"],
                "summary": "立即执行完整 Trakt 同步",
                "auth": "bear",
            },
            {
                "path": "/cache/refresh",
                "endpoint": self.api_cache_refresh,
                "methods": ["POST"],
                "summary": "刷新 Trakt 缓存",
                "auth": "bear",
            },
            {
                "path": "/calendar/refresh",
                "endpoint": self.api_calendar_refresh,
                "methods": ["POST"],
                "summary": "刷新 Trakt 个人剧集日历",
                "auth": "bear",
            },
            {
                "path": "/custom_lists/refresh",
                "endpoint": self.api_refresh_custom_lists,
                "methods": ["POST"],
                "summary": "刷新 Trakt 自定义列表目录",
                "auth": "bear",
            },
            {
                "path": "/custom_lists/select",
                "endpoint": self.api_select_custom_list,
                "methods": ["POST"],
                "summary": "选择或取消自定义列表订阅源",
                "auth": "bear",
            },
            {
                "path": "/history/delete",
                "endpoint": self.api_delete_history,
                "methods": ["POST"],
                "summary": "删除 Trakt 同步历史",
                "auth": "bear",
            },
        ]

    def api_sync_now(self):
        if _sync_lock.locked():
            logger.warning("Trakt 手动刷新同步未启动：已有同步任务正在运行")
            return schemas.Response(success=False, message="Trakt 同步任务正在运行")
        self.save_data(
            self._sync_status_key,
            {
                "state": "queued",
                "message": "手动刷新同步已进入后台队列",
                "started_at": self._now_iso(),
            },
        )
        logger.info("Trakt 手动刷新同步已进入后台队列")
        Thread(target=self.sync_sources, kwargs={"force": True}, daemon=True).start()
        return schemas.Response(success=True, message="已启动 Trakt 强制刷新同步")

    def api_cache_refresh(self, request: CacheRefreshRequest):
        scope = (request.scope or "all").lower()
        if scope not in ("all", "public", "personal"):
            return schemas.Response(success=False, message="scope 仅支持 all、public、personal")
        removed = self._clear_cached_data(scope)
        return schemas.Response(success=True, message=f"已清理 {removed} 条 Trakt 缓存")

    def api_calendar_refresh(self):
        if not _calendar_refresh_lock.acquire(blocking=False):
            logger.warning("Trakt 日历手动刷新未启动：已有刷新任务正在运行")
            return schemas.Response(success=False, message="Trakt 日历正在刷新")
        account = (self.get_data(self._account_key) or {}).get("data") or {}
        self.save_data(
            self._calendar_status_data_key(account.get("uuid")),
            {
                "state": "queued",
                "message": "手动刷新日历已进入后台队列",
                "started_at": self._now_iso(),
            },
        )
        logger.info("Trakt 日历手动刷新已进入后台队列")
        try:
            Thread(
                target=self.refresh_calendar_page,
                kwargs={"force_refresh": True, "lock_acquired": True},
                daemon=True,
            ).start()
        except Exception:
            _calendar_refresh_lock.release()
            raise
        return schemas.Response(success=True, message="已启动 Trakt 日历后台刷新")

    def api_refresh_custom_lists(self):
        payload = self.get_trakt_custom_lists(
            list_id=None,
            page=1,
            limit=100,
            force_refresh=True,
        )
        if not payload.get("success"):
            error = payload.get("meta", {}).get("error", {}).get("message") or "刷新失败"
            return schemas.Response(success=False, message=error)
        total = (
            payload.get("meta", {})
            .get("pagination", {})
            .get("item_count")
        )
        refreshed_count = total if total is not None else len(payload.get("data") or [])
        return schemas.Response(
            success=True,
            message=f"已刷新 {refreshed_count} 个 Trakt 自定义列表",
        )

    def api_select_custom_list(self, request: CustomListSelectionRequest):
        catalog_key = self._custom_list_catalog_data_key()
        catalog = (self.get_data(catalog_key) or {}).get("data") or []
        known_ids = {int(item.get("list_id")) for item in catalog if item.get("list_id") is not None}
        if int(request.list_id) not in known_ids:
            return schemas.Response(
                success=False,
                message="列表不在当前 Trakt 账户的本地目录中，请先刷新",
            )

        selected = set(self._selected_list_ids())
        was_selected = int(request.list_id) in selected
        if request.selected:
            selected.add(int(request.list_id))
        else:
            selected.discard(int(request.list_id))
        self.save_data(self._selected_lists_key, sorted(selected))
        self._update_catalog_selection(selected)

        if request.selected and not was_selected:
            Thread(
                target=self.sync_sources,
                kwargs={"force": True, "only_list_id": int(request.list_id)},
                daemon=True,
            ).start()
            return schemas.Response(success=True, message="已选择列表并启动后台同步")
        if not request.selected and was_selected:
            return schemas.Response(success=True, message="已取消列表，现有 MoviePilot 订阅不会删除")
        return schemas.Response(success=True, message="列表选择状态未变化")

    def api_delete_history(self, request: HistoryDeleteRequest):
        histories = self.get_data("history") or {}
        if request.id not in histories:
            return schemas.Response(success=False, message="未找到历史记录")
        histories.pop(request.id, None)
        self.save_data("history", histories)
        return schemas.Response(success=True, message="删除成功")

    @eventmanager.register(ChainEventType.DiscoverSource)
    def _on_discover_source(self, event: Event):
        if not self._client_id:
            return
        movie_items = []
        show_items = []
        category_titles = {
            "popular": "热门",
            "trending": "趋势",
            "recommended": "推荐",
            "anticipated": "待映",
            "watched": "观看周榜",
            "collected": "收藏周榜",
            "boxoffice": "票房",
        }
        for config_key, attr_name, _ in self._chart_switches:
            if not getattr(self, attr_name, False):
                continue
            category, media_type = config_key.removeprefix("enable_").rsplit("_", 1)
            item = {
                "value": f"{category}_{media_type}",
                "title": category_titles.get(category, category),
            }
            (movie_items if media_type == "movies" else show_items).append(item)

        if not movie_items and not show_items:
            return
        all_items = movie_items + show_items
        chips = []
        for title, items in (("电影", movie_items), ("剧集", show_items)):
            if not items:
                continue
            if movie_items and show_items:
                chips.append(
                    {
                        "component": "div",
                        "props": {"class": "text-subtitle-2 font-weight-bold mr-2 align-self-center"},
                        "text": title,
                    }
                )
            chips.extend(
                {
                    "component": "VChip",
                    "props": {"value": item["value"], "filter": True},
                    "text": item["title"],
                }
                for item in items
            )
            if title == "电影" and movie_items and show_items:
                chips.append(
                    {
                        "component": "VDivider",
                        "props": {"vertical": True, "class": "mx-2"},
                    }
                )

        event_data: DiscoverSourceEventData = event.event_data
        event_data.extra_sources.append(
            DiscoverMediaSource(
                name="Trakt",
                mediaid_prefix="trakt",
                api_path="plugin/TraktSync/trakt_discover",
                filter_params={"list_type": all_items[0]["value"]},
                filter_ui=[
                    {
                        "component": "VChipGroup",
                        "props": {"model": "list_type", "mandatory": "force"},
                        "content": chips,
                    }
                ],
            )
        )

    def get_agent_tools(self) -> list:
        if not self._client_id:
            return []
        plugin_ref = self

        class BoundListsTool(GetTraktListsTool):
            def __init__(tool_self, session_id, user_id):
                super().__init__(session_id, user_id, plugin_instance=plugin_ref)

        class BoundPersonalTool(GetTraktPersonalDataTool):
            def __init__(tool_self, session_id, user_id):
                super().__init__(session_id, user_id, plugin_instance=plugin_ref)

        class BoundCustomListsTool(GetTraktCustomListsTool):
            def __init__(tool_self, session_id, user_id):
                super().__init__(session_id, user_id, plugin_instance=plugin_ref)

        class BoundCalendarTool(GetTraktCalendarTool):
            def __init__(tool_self, session_id, user_id):
                super().__init__(session_id, user_id, plugin_instance=plugin_ref)

        return [
            BoundListsTool,
            BoundPersonalTool,
            BoundCustomListsTool,
            BoundCalendarTool,
        ]

    def refresh_calendar_page(
        self,
        force_refresh: bool = False,
        lock_acquired: bool = False,
    ) -> Dict[str, Any]:
        """后台刷新详情页使用的未来 14 天个人剧集日历快照。"""
        acquired_here = False
        if not lock_acquired:
            acquired_here = _calendar_refresh_lock.acquire(blocking=False)
            if not acquired_here:
                logger.info("Trakt 日历定时刷新已跳过：已有刷新任务正在运行")
                return {"success": False, "message": "日历刷新任务正在运行"}
        started = time.monotonic()
        account_uuid = None
        status_key = self._calendar_status_data_key()
        try:
            account = (self.get_data(self._account_key) or {}).get("data") or {}
            account_uuid = account.get("uuid")
            status_key = self._calendar_status_data_key(account_uuid)
            started_at = self._now_iso()
            self.save_data(
                status_key,
                {
                    "state": "running",
                    "message": "正在刷新未来 14 天个人剧集日历",
                    "started_at": started_at,
                },
            )
            logger.info(
                "Trakt 日历刷新开始："
                f"模式={'强制刷新' if force_refresh else '定时刷新'}，范围=未来14天"
            )
            start_date = self._calendar_today()
            payload = self.get_trakt_calendar(
                target="my",
                calendar_type="shows",
                start_date=start_date,
                days=14,
                page=1,
                limit=100,
                force_refresh=force_refresh,
            )
            if not payload.get("success"):
                message = (
                    payload.get("meta", {})
                    .get("error", {})
                    .get("message")
                    or "日历查询失败"
                )
                raise TraktRequestError(message)

            account = (self.get_data(self._account_key) or {}).get("data") or {}
            account_uuid = account.get("uuid")
            resolved_status_key = self._calendar_status_data_key(account_uuid)
            if resolved_status_key != status_key:
                self.del_data(status_key)
                status_key = resolved_status_key
            snapshot_key = self._calendar_snapshot_key(
                account_uuid,
                "shows",
                start_date,
                14,
            )
            snapshot = self.get_data(snapshot_key) or {}
            page_key = self._calendar_page_data_key(account_uuid)
            previous_page = self.get_data(page_key) or {}
            stale = bool(payload.get("meta", {}).get("stale"))
            if snapshot.get("data") is None:
                raise TraktRequestError("日历刷新未生成本地快照")
            if not stale or not previous_page:
                self.save_data(
                    page_key,
                    {
                        "timestamp": time.time(),
                        "fetched_at": snapshot.get("fetched_at") or self._now_iso(),
                        "start_date": start_date,
                        "days": 14,
                        "timezone": str(settings.TZ),
                        "data": snapshot.get("data") or [],
                        "meta": payload.get("meta") or {},
                    },
                )

            duration = round(time.monotonic() - started, 2)
            total = len(snapshot.get("data") or [])
            state = "stale" if stale else "success"
            message = (
                f"Trakt 实时请求失败，继续保留旧日历（{total} 集）"
                if stale
                else f"已刷新 {total} 集未来播出日程"
            )
            self.save_data(
                status_key,
                {
                    "state": state,
                    "message": message,
                    "started_at": started_at,
                    "finished_at": self._now_iso(),
                    "duration_seconds": duration,
                    "item_count": total,
                },
            )
            logger.info(
                f"Trakt 日历刷新完成：状态={state}，条目={total}，耗时={duration}秒"
            )
            return {"success": True, "state": state, "item_count": total}
        except Exception as exc:
            duration = round(time.monotonic() - started, 2)
            message = self._safe_error(str(exc)) or "日历刷新失败"
            self.save_data(
                status_key,
                {
                    "state": "failed",
                    "message": message,
                    "finished_at": self._now_iso(),
                    "duration_seconds": duration,
                },
            )
            logger.error(f"Trakt 日历刷新失败：{message}，耗时={duration}秒")
            return {"success": False, "message": message}
        finally:
            if lock_acquired or acquired_here:
                _calendar_refresh_lock.release()

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        sync_service = {
            "id": "TraktSync",
            "name": "Trakt Watchlist 与自定义列表增量同步",
            "trigger": (
                CronTrigger.from_crontab(self._cron)
                if self._cron
                else "interval"
            ),
            "func": self.sync_watchlist,
            "kwargs": {} if self._cron else {"minutes": 30},
        }
        calendar_service = {
            "id": "TraktSyncCalendar",
            "name": "Trakt 个人剧集日历刷新",
            "trigger": "interval",
            "func": self.refresh_calendar_page,
            "kwargs": {"hours": 1},
        }
        return [sync_service, calendar_service]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as exc:
            logger.error(f"退出 TraktSync 插件失败：{exc}")

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _safe_error(message: str) -> str:
        """错误信息只保留状态和端点，不返回响应正文或凭证。"""
        value = str(message).replace("\n", " ")
        value = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer ***", value)
        value = re.sub(
            r"(?i)(access_token|refresh_token|client_secret|authorization)"
            r"\s*[:=]\s*[^\s,;&]+",
            r"\1=***",
            value,
        )
        return value[:500]

    @staticmethod
    def _failure_payload(code: str, message: str, **meta) -> Dict[str, Any]:
        return {
            "success": False,
            "meta": {
                **meta,
                "error": {"code": code, "message": TraktSync._safe_error(message)},
            },
            "data": [],
        }

    def _oauth_post(self, url: str, data: dict) -> Optional[dict]:
        try:
            response = requests.post(
                url,
                json=data,
                headers={"Content-Type": "application/json"},
                proxies=settings.PROXY,
                timeout=self._request_timeout,
            )
            response.raise_for_status()
            return json.loads(response.text or "{}")
        except Exception:
            return None

    def device_code_request(self) -> Optional[dict]:
        return self._oauth_post(self._device_code_url, {"client_id": self._client_id})

    def token_request(self, code: str) -> Optional[dict]:
        result = self._oauth_post(
            self._token_url,
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
            },
        )
        if not result:
            return None
        result["expired_at"] = int(result.get("created_at") or time.time()) + int(
            result.get("expires_in") or 24 * 3600
        )
        self.save_data("token", result)
        return result

    def refresh_token_request(self, refresh_token: str) -> Optional[dict]:
        result = self._oauth_post(
            self._refresh_token_url,
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            },
        )
        if not result:
            logger.error("Trakt OAuth token 刷新失败")
            return None
        result["expired_at"] = int(result.get("created_at") or time.time()) + int(
            result.get("expires_in") or 24 * 3600
        )
        self.save_data("token", result)
        return result

    def _get_valid_trakt_token(
        self,
        force_refresh: bool = False,
        rejected_access_token: Optional[str] = None,
    ) -> Optional[dict]:
        token = self.get_data("token") or self.token
        if not token:
            return None
        expired_at = token.get("expired_at")
        needs_refresh = force_refresh or (
            expired_at is not None and float(expired_at) <= time.time()
        )
        if not needs_refresh and token.get("access_token"):
            self.token = token
            return token

        with _token_refresh_lock:
            latest = self.get_data("token") or self.token or {}
            if (
                force_refresh
                and rejected_access_token
                and latest.get("access_token")
                and latest.get("access_token") != rejected_access_token
            ):
                self.token = latest
                return latest
            latest_expired_at = latest.get("expired_at")
            latest_expired = (
                latest_expired_at is not None
                and float(latest_expired_at) <= time.time()
            )
            if (
                not force_refresh
                and not latest_expired
                and latest.get("access_token")
            ):
                self.token = latest
                return latest
            refresh_token = latest.get("refresh_token")
            if not refresh_token:
                return None
            refreshed = self.refresh_token_request(refresh_token)
            if refreshed and refreshed.get("access_token"):
                self.token = refreshed
                return refreshed
            return None

    @staticmethod
    def _response_json(response) -> Any:
        if getattr(response, "status_code", None) == 204:
            return None
        text = getattr(response, "text", "")
        if not text:
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:
            raise TraktRequestError("Trakt API 返回了无效 JSON") from exc

    @staticmethod
    def _response_pagination(response) -> Dict[str, Optional[int]]:
        headers = getattr(response, "headers", {}) or {}

        def as_int(name: str) -> Optional[int]:
            value = headers.get(name)
            if value is None:
                value = headers.get(name.lower())
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "page": as_int("X-Pagination-Page"),
            "limit": as_int("X-Pagination-Limit"),
            "page_count": as_int("X-Pagination-Page-Count"),
            "item_count": as_int("X-Pagination-Item-Count"),
        }

    def _trakt_request(
        self,
        path: str,
        *,
        params: Optional[dict] = None,
        requires_auth: bool = False,
        method: str = "GET",
        json_body: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """统一处理代理、超时、请求头、OAuth 401 刷新和分页头。"""
        if not self._client_id:
            raise TraktRequestError("Trakt Client ID 未配置")
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self._trakt_api_base}{path}"
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self._client_id,
        }
        access_token = None
        if requires_auth:
            token = self._get_valid_trakt_token()
            if not token:
                raise TraktRequestError("Trakt OAuth 未授权或已失效", status_code=401)
            access_token = token.get("access_token")
            headers["Authorization"] = f"Bearer {access_token}"

        def send(request_headers: dict):
            kwargs = {
                "headers": request_headers,
                "params": params or {},
                "proxies": settings.PROXY,
                "timeout": self._request_timeout,
            }
            if method.upper() == "GET":
                return requests.get(url, **kwargs)
            kwargs["json"] = json_body
            return requests.post(url, **kwargs)

        try:
            response = send(headers)
            if requires_auth and getattr(response, "status_code", None) == 401:
                refreshed = self._get_valid_trakt_token(
                    force_refresh=True,
                    rejected_access_token=access_token,
                )
                if not refreshed:
                    raise TraktRequestError("Trakt OAuth 刷新失败", status_code=401)
                retry_headers = {
                    **headers,
                    "Authorization": f"Bearer {refreshed.get('access_token')}",
                }
                response = send(retry_headers)

            status_code = getattr(response, "status_code", 0)
            if status_code >= 400:
                raise TraktRequestError(
                    f"Trakt API 请求失败：HTTP {status_code} {path}",
                    status_code=status_code,
                )
            response.raise_for_status()
            return {
                "data": self._response_json(response),
                "pagination": self._response_pagination(response),
                "status_code": status_code,
            }
        except TraktRequestError:
            raise
        except Exception as exc:
            raise TraktRequestError(f"Trakt API 请求失败：{path}") from exc

    @staticmethod
    def _normalized_params(params: Optional[dict]) -> dict:
        result = {}
        for key, value in sorted((params or {}).items()):
            if value is None:
                continue
            result[str(key)] = value
        return result

    @staticmethod
    def _account_cache_component(account_uuid: Optional[str]) -> str:
        value = str(account_uuid or "missing")
        safe_value = "".join(
            character
            for character in value
            if character.isalnum() or character in ("-", "_")
        )
        return safe_value or "missing"

    def _custom_list_catalog_data_key(
        self,
        account_uuid: Optional[str] = None,
    ) -> str:
        if not account_uuid:
            account = (self.get_data(self._account_key) or {}).get("data") or {}
            account_uuid = account.get("uuid")
        return (
            f"{self._custom_list_catalog_key}_"
            f"{self._account_cache_component(account_uuid)}"
        )

    def _calendar_snapshot_key(
        self,
        account_uuid: Optional[str],
        calendar_type: str,
        start_date: str,
        days: int,
    ) -> str:
        digest = hashlib.sha256(
            f"{calendar_type}:{start_date}:{days}".encode("utf-8")
        ).hexdigest()[:20]
        return (
            f"{self._calendar_snapshot_prefix}"
            f"{self._account_cache_component(account_uuid)}_{digest}"
        )

    def _calendar_page_data_key(self, account_uuid: Optional[str] = None) -> str:
        if not account_uuid:
            account = (self.get_data(self._account_key) or {}).get("data") or {}
            account_uuid = account.get("uuid")
        return (
            f"{self._calendar_page_prefix}"
            f"{self._account_cache_component(account_uuid)}"
        )

    def _calendar_status_data_key(self, account_uuid: Optional[str] = None) -> str:
        if not account_uuid:
            account = (self.get_data(self._account_key) or {}).get("data") or {}
            account_uuid = account.get("uuid")
        return (
            f"{self._calendar_status_prefix}"
            f"{self._account_cache_component(account_uuid)}"
        )

    def _cache_key(
        self,
        scope: str,
        account_uuid: Optional[str],
        namespace: str,
        path: str,
        params: Optional[dict],
    ) -> str:
        normalized = {
            "path": path,
            "params": self._normalized_params(params),
        }
        digest = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:24]
        account_component = (
            self._account_cache_component(account_uuid)
            if scope == "personal"
            else "shared"
        )
        return f"{self._cache_prefix}{scope}_{account_component}_{digest}"

    def _cached_request(
        self,
        namespace: str,
        path: str,
        *,
        params: Optional[dict],
        requires_auth: bool,
        ttl: int,
        force_refresh: bool,
        account_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = "personal" if requires_auth else "public"
        key = self._cache_key(scope, account_uuid, namespace, path, params)
        cached = self.get_data(key) or {}
        now = time.time()
        fresh = bool(cached) and now - float(cached.get("timestamp") or 0) < ttl
        if fresh and not force_refresh:
            return {
                "data": cached.get("data"),
                "pagination": cached.get("pagination") or {},
                "cache": {
                    "cached": True,
                    "stale": False,
                    "fetched_at": cached.get("fetched_at"),
                },
            }
        try:
            response = self._trakt_request(
                path,
                params=params,
                requires_auth=requires_auth,
            )
            record = {
                "timestamp": now,
                "fetched_at": self._now_iso(),
                "scope": scope,
                "account_uuid": account_uuid if requires_auth else None,
                "namespace": namespace,
                "path": path,
                "params": self._normalized_params(params),
                "data": response.get("data"),
                "pagination": response.get("pagination") or {},
            }
            self.save_data(key, record)
            return {
                "data": record["data"],
                "pagination": record["pagination"],
                "cache": {
                    "cached": False,
                    "stale": False,
                    "fetched_at": record["fetched_at"],
                },
            }
        except TraktRequestError as exc:
            if cached:
                return {
                    "data": cached.get("data"),
                    "pagination": cached.get("pagination") or {},
                    "cache": {
                        "cached": True,
                        "stale": True,
                        "fetched_at": cached.get("fetched_at"),
                        "fallback_reason": self._safe_error(str(exc)),
                    },
                }
            raise

    def _all_data_rows(self) -> list:
        try:
            rows = self.get_data()
            return rows if isinstance(rows, list) else []
        except (TypeError, AttributeError):
            return []

    def _clear_cached_data(self, scope: str = "all") -> int:
        prefixes = []
        if scope in ("all", "public"):
            prefixes.extend(
                [
                    f"{self._cache_prefix}public_",
                    f"{self._discover_cache_prefix}public_",
                ]
            )
        if scope in ("all", "personal"):
            prefixes.extend(
                [
                    f"{self._cache_prefix}personal_",
                    f"{self._discover_cache_prefix}personal_",
                    f"{self._custom_list_catalog_key}_",
                    self._calendar_snapshot_prefix,
                    self._calendar_page_prefix,
                    self._calendar_status_prefix,
                ]
            )
        keys = []
        for row in self._all_data_rows():
            key = getattr(row, "key", None)
            if key and any(key.startswith(prefix) for prefix in prefixes):
                keys.append(key)
        removed = 0
        for key in set(keys):
            if self.get_data(key) is not None:
                self.del_data(key)
                removed += 1
        return removed

    @staticmethod
    def _sanitize_payload(value: Any) -> Any:
        sensitive = {
            "access_token",
            "refresh_token",
            "client_secret",
            "authorization",
            "email",
            "email_verified",
        }
        if isinstance(value, dict):
            return {
                key: TraktSync._sanitize_payload(item)
                for key, item in value.items()
                if str(key).lower() not in sensitive
                and "token" not in str(key).lower()
            }
        if isinstance(value, list):
            return [TraktSync._sanitize_payload(item) for item in value]
        return value

    def _account_from_settings(self, settings_payload: dict) -> dict:
        user = (settings_payload or {}).get("user") or {}
        ids = user.get("ids") or {}
        account_uuid = ids.get("uuid")
        if not account_uuid:
            raise TraktRequestError("Trakt /users/settings 未返回账户 UUID")
        return {
            "uuid": str(account_uuid),
            "username": user.get("username"),
            "slug": ids.get("slug") or user.get("username"),
            "name": user.get("name"),
            "joined_at": user.get("joined_at"),
        }

    def _get_account(self, force_refresh: bool = False) -> Tuple[dict, dict]:
        cached = self.get_data(self._account_key) or {}
        if (
            cached
            and not force_refresh
            and time.time() - float(cached.get("timestamp") or 0) < self._account_ttl
        ):
            return cached.get("data") or {}, {
                "cached": True,
                "stale": False,
                "fetched_at": cached.get("fetched_at"),
            }
        try:
            response = self._trakt_request("/users/settings", requires_auth=True)
            account = self._account_from_settings(response.get("data") or {})
            old_account = cached.get("data") or {}
            old_uuid = old_account.get("uuid")
            if old_uuid and old_uuid != account.get("uuid"):
                self._clear_cached_data("personal")
                self.del_data(self._sync_state_key)
                self.del_data(self._selected_lists_key)
                self.save_data(
                    self._legacy_history_migration_key,
                    {
                        "account_uuid": account.get("uuid"),
                        "completed_at": self._now_iso(),
                        "reason": "account_switch",
                    },
                )
                logger.info("检测到 Trakt 账户切换，已清理旧账户缓存和同步状态")
            record = {
                "timestamp": time.time(),
                "fetched_at": self._now_iso(),
                "data": account,
            }
            self.save_data(self._account_key, record)
            return account, {
                "cached": False,
                "stale": False,
                "fetched_at": record["fetched_at"],
            }
        except TraktRequestError as exc:
            if cached.get("data"):
                return cached["data"], {
                    "cached": True,
                    "stale": True,
                    "fetched_at": cached.get("fetched_at"),
                    "fallback_reason": self._safe_error(str(exc)),
                }
            raise

    @staticmethod
    def _pagination_meta(
        pagination: dict,
        page: int,
        limit: int,
        item_count: int,
        *,
        total: Optional[int] = None,
        has_more: Optional[bool] = None,
    ) -> dict:
        current_page = pagination.get("page") or page
        current_limit = pagination.get("limit") or limit
        page_count = pagination.get("page_count")
        total_items = pagination.get("item_count")
        if total is not None:
            total_items = total
            page_count = (total + limit - 1) // limit if total else 0
        if has_more is None:
            has_more = (
                current_page < page_count
                if page_count is not None
                else item_count >= current_limit
            )
        return {
            "page": current_page,
            "limit": current_limit,
            "page_count": page_count,
            "item_count": total_items,
            "current_count": item_count,
            "has_more": bool(has_more),
        }

    @staticmethod
    def _normalize_item(
        raw_item: dict,
        media_type_hint: Optional[str] = None,
    ) -> dict:
        wrapper = raw_item if isinstance(raw_item, dict) else {}
        item_type = str(wrapper.get("type") or "").rstrip("s")
        media = None
        if item_type in ("movie", "show", "season", "episode") and isinstance(
            wrapper.get(item_type),
            dict,
        ):
            media = wrapper[item_type]
        else:
            for candidate in ("movie", "show", "season", "episode"):
                if isinstance(wrapper.get(candidate), dict):
                    item_type = candidate
                    media = wrapper[candidate]
                    break
        if media is None:
            media = wrapper
        if item_type not in ("movie", "show", "season", "episode"):
            hinted_type = (media_type_hint or "").rstrip("s")
            item_type = (
                hinted_type
                if hinted_type in ("movie", "show", "season", "episode")
                else "show" if media.get("network") else "movie"
            )

        ids = media.get("ids") or {}
        show = wrapper.get("show") if isinstance(wrapper.get("show"), dict) else {}
        show_ids = show.get("ids") or {}
        title = media.get("title") or show.get("title")
        season_number = media.get("number") if item_type == "season" else media.get("season")
        episode_number = media.get("number") if item_type == "episode" else None
        metrics = {}
        for key in (
            "watchers",
            "watcher_count",
            "plays",
            "play_count",
            "collected_count",
            "collector_count",
            "list_count",
            "revenue",
            "rank",
            "votes",
            "rating",
        ):
            if wrapper.get(key) is not None:
                metrics[key] = wrapper.get(key)

        normalized = {
            "type": item_type,
            "title": title,
            "year": media.get("year") or show.get("year"),
            "trakt_id": ids.get("trakt"),
            "tmdb_id": ids.get("tmdb"),
            "imdb_id": ids.get("imdb"),
            "tvdb_id": ids.get("tvdb"),
            "season": season_number,
            "episode": episode_number,
            "show_title": show.get("title"),
            "show_trakt_id": show_ids.get("trakt"),
            "listed_at": wrapper.get("listed_at"),
            "watched_at": wrapper.get("watched_at"),
            "last_watched_at": wrapper.get("last_watched_at"),
            "collected_at": wrapper.get("collected_at"),
            "released": media.get("released") or media.get("first_aired"),
            "runtime": media.get("runtime"),
            "list_item_id": wrapper.get("id") if wrapper.get("listed_at") else None,
            "history_id": wrapper.get("id") if wrapper.get("watched_at") else None,
            "action": wrapper.get("action"),
            "notes": wrapper.get("notes"),
            "progress": TraktSync._sanitize_payload(wrapper.get("progress")),
            "next_episode": TraktSync._sanitize_payload(wrapper.get("next_episode")),
            "seasons": TraktSync._sanitize_payload(wrapper.get("seasons")),
            "metrics": metrics,
        }
        return {key: value for key, value in normalized.items() if value not in (None, {}, "")}

    @staticmethod
    def _moviepilot_timezone():
        """返回 MoviePilot 配置时区，无效配置时安全退回 UTC。"""
        try:
            return ZoneInfo(str(settings.TZ))
        except (ZoneInfoNotFoundError, ValueError, TypeError, SystemError):
            return datetime.timezone.utc

    def _calendar_today(self) -> str:
        return datetime.datetime.now(self._moviepilot_timezone()).date().isoformat()

    @staticmethod
    def _parse_trakt_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
        if not value or not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed

    def _calendar_local_parts(self, value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        parsed = self._parse_trakt_datetime(value)
        if not parsed:
            return None, None
        local_value = parsed.astimezone(self._moviepilot_timezone())
        return local_value.date().isoformat(), local_value.strftime("%H:%M")

    @staticmethod
    def _normalize_image_url(value: Any) -> Optional[str]:
        """将 Trakt 省略协议的 CDN 图片地址转换为浏览器可加载的 URL。"""
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if re.match(r"^https?://", normalized, flags=re.IGNORECASE):
            return normalized
        if normalized.startswith("//"):
            return f"https:{normalized}"
        if normalized.startswith(("/", "./", "../")):
            return normalized
        if re.match(r"^[a-z][a-z0-9+.-]*:", normalized, flags=re.IGNORECASE):
            return None
        return f"https://{normalized}"

    @classmethod
    def _first_image_url(cls, images: Any) -> Optional[str]:
        """兼容 Trakt 图片数组以及历史字典结构，并补全 CDN URL。"""
        if isinstance(images, str):
            return cls._normalize_image_url(images)
        if isinstance(images, list):
            for item in images:
                resolved = cls._first_image_url(item)
                if resolved:
                    return resolved
            return None
        if isinstance(images, dict):
            for key in ("poster", "thumb", "fanart", "banner", "screenshot"):
                resolved = cls._first_image_url(images.get(key))
                if resolved:
                    return resolved
            for key in ("full", "medium", "thumb"):
                resolved = cls._first_image_url(images.get(key))
                if resolved:
                    return resolved
        return None

    def _calendar_item_with_normalized_poster(self, item: Any) -> dict:
        """兼容升级前已保存的无协议日历海报地址。"""
        result = dict(item) if isinstance(item, dict) else {}
        if "poster" not in result:
            return result
        poster = self._normalize_image_url(result.get("poster"))
        if poster:
            result["poster"] = poster
        else:
            result.pop("poster", None)
        return result

    @staticmethod
    def _calendar_event_id(*parts: Any) -> str:
        normalized = ":".join(str(part) for part in parts if part not in (None, ""))
        return normalized or hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:20]

    def _normalize_calendar_item(self, raw_item: dict, calendar_type: str) -> dict:
        wrapper = raw_item if isinstance(raw_item, dict) else {}
        if calendar_type in self._calendar_show_types:
            show = wrapper.get("show") if isinstance(wrapper.get("show"), dict) else {}
            episode = (
                wrapper.get("episode")
                if isinstance(wrapper.get("episode"), dict)
                else {}
            )
            show_ids = show.get("ids") or {}
            episode_ids = episode.get("ids") or {}
            first_aired = wrapper.get("first_aired") or episode.get("first_aired")
            local_date, local_time = self._calendar_local_parts(first_aired)
            season = episode.get("season")
            episode_number = episode.get("number")
            event_id = self._calendar_event_id(
                "show",
                show_ids.get("trakt") or show_ids.get("tmdb") or show.get("title"),
                season,
                episode_number,
            )
            normalized = {
                "event_id": event_id,
                "type": "episode",
                "first_aired": first_aired,
                "local_date": local_date,
                "local_time": local_time,
                "show_title": show.get("title"),
                "show_year": show.get("year"),
                "show_trakt_id": show_ids.get("trakt"),
                "show_tmdb_id": show_ids.get("tmdb"),
                "show_imdb_id": show_ids.get("imdb"),
                "show_tvdb_id": show_ids.get("tvdb"),
                "network": show.get("network"),
                "episode_title": episode.get("title"),
                "season": season,
                "episode": episode_number,
                "episode_trakt_id": episode_ids.get("trakt"),
                "episode_tmdb_id": episode_ids.get("tmdb"),
                "episode_tvdb_id": episode_ids.get("tvdb"),
                "overview": episode.get("overview") or show.get("overview"),
                "runtime": episode.get("runtime") or show.get("runtime"),
                "poster": self._first_image_url(show.get("images")),
            }
        else:
            movie = wrapper.get("movie") if isinstance(wrapper.get("movie"), dict) else {}
            ids = movie.get("ids") or {}
            released = wrapper.get("released") or movie.get("released")
            event_id = self._calendar_event_id(
                "movie",
                ids.get("trakt") or ids.get("tmdb") or movie.get("title"),
                released,
            )
            normalized = {
                "event_id": event_id,
                "type": "movie",
                "released": released,
                "local_date": released,
                "title": movie.get("title"),
                "year": movie.get("year"),
                "trakt_id": ids.get("trakt"),
                "tmdb_id": ids.get("tmdb"),
                "imdb_id": ids.get("imdb"),
                "tvdb_id": ids.get("tvdb"),
                "overview": movie.get("overview"),
                "runtime": movie.get("runtime"),
                "poster": self._first_image_url(movie.get("images")),
            }
        return {
            key: self._sanitize_payload(value)
            for key, value in normalized.items()
            if value not in (None, "")
        }

    @staticmethod
    def _calendar_sort_key(item: dict) -> Tuple[str, str, int, int]:
        timestamp = item.get("first_aired") or item.get("released") or ""
        title = item.get("show_title") or item.get("title") or ""
        return (
            str(timestamp),
            str(title).casefold(),
            int(item.get("season") or 0),
            int(item.get("episode") or 0),
        )

    @staticmethod
    def _object_value(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @staticmethod
    def _as_int(value: Any) -> Optional[int]:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _marked_numbers(value: Any, marker: str) -> set:
        if value is None:
            return set()
        if isinstance(value, int) and not isinstance(value, bool):
            return {value}
        return {
            int(number)
            for number in re.findall(
                rf"(?i){re.escape(marker)}\s*0*(\d+)(?!\d)",
                str(value),
            )
        }

    @classmethod
    def _number_set(cls, value: Any, marker: str) -> set:
        marked = cls._marked_numbers(value, marker)
        if marked:
            return marked
        plain = cls._as_int(value)
        return {plain} if plain is not None else set()

    def _load_calendar_downloads(self) -> List[dict]:
        """一次性读取下载任务并用下载历史补充媒体标识。"""
        torrents = self.downloadchain.list_torrents(include_all_tags=False) or []
        hashes = [
            self._object_value(torrent, "hash")
            for torrent in torrents
            if self._object_value(torrent, "hash")
        ]
        history_map = DownloadHistoryOper().get_by_hashes(hashes) if hashes else {}
        records = []
        for torrent in torrents:
            torrent_hash = self._object_value(torrent, "hash")
            history = history_map.get(torrent_hash) if torrent_hash else None
            media = self._object_value(torrent, "media") or {}
            tmdb_id = self._object_value(media, "tmdbid")
            if tmdb_id is None:
                tmdb_id = self._object_value(history, "tmdbid")
            season_value = self._object_value(media, "season")
            if season_value is None:
                season_value = self._object_value(history, "seasons")
            episode_value = self._object_value(media, "episode")
            if episode_value is None:
                episode_value = self._object_value(history, "episodes")
            title_values = [
                self._object_value(torrent, "season_episode"),
                self._object_value(torrent, "title"),
                self._object_value(torrent, "name"),
                self._object_value(media, "title"),
                self._object_value(history, "title"),
                season_value,
                episode_value,
            ]
            state = self._object_value(torrent, "state")
            state = self._object_value(state, "value", state)
            records.append(
                {
                    "tmdb_id": self._as_int(tmdb_id),
                    "season": season_value,
                    "episode": episode_value,
                    "text": " ".join(
                        str(value) for value in title_values if value not in (None, "")
                    ),
                    "state": str(state or "").lower(),
                }
            )
        return records

    def _calendar_download_matches(self, item: dict, download: dict) -> bool:
        show_tmdb_id = self._as_int(item.get("show_tmdb_id"))
        task_tmdb_id = self._as_int(download.get("tmdb_id"))
        text = str(download.get("text") or "")
        if task_tmdb_id is not None:
            if show_tmdb_id is None or task_tmdb_id != show_tmdb_id:
                return False
        else:
            show_title = str(item.get("show_title") or "").strip().casefold()
            if not show_title or show_title not in text.casefold():
                return False

        season = self._as_int(item.get("season"))
        episode = self._as_int(item.get("episode"))
        if season is None or episode is None:
            return False

        season_numbers = self._number_set(download.get("season"), "S")
        if not season_numbers:
            season_numbers = self._marked_numbers(text, "S")
        if not season_numbers or season not in season_numbers:
            return False

        episode_numbers = self._number_set(download.get("episode"), "E")
        if not episode_numbers:
            episode_numbers = self._marked_numbers(text, "E")
        return not episode_numbers or episode in episode_numbers

    def _calendar_is_subscribed(self, item: dict, subscriptions: List[Any]) -> bool:
        show_tmdb_id = self._as_int(item.get("show_tmdb_id"))
        season = self._as_int(item.get("season"))
        if show_tmdb_id is None or season is None:
            return False
        for subscribe in subscriptions:
            if self._as_int(self._object_value(subscribe, "tmdbid")) != show_tmdb_id:
                continue
            subscribed_season = self._object_value(subscribe, "season")
            if subscribed_season in (None, ""):
                return True
            if season in self._number_set(subscribed_season, "S"):
                return True
        return False

    def _query_calendar_library(self, item: dict) -> dict:
        """识别一部剧并返回其媒体库季集信息。"""
        tmdb_id = self._as_int(item.get("show_tmdb_id"))
        title = item.get("show_title") or ""
        if tmdb_id is None:
            raise ValueError("剧集缺少 TMDB ID")
        meta = MetaInfo(title=title)
        meta.type = MediaType.TV
        mediainfo = self.chain.recognize_media(meta=meta, tmdbid=tmdb_id)
        if not mediainfo:
            raise ValueError("MoviePilot 未识别到剧集")
        exists_info = self.chain.media_exists(mediainfo=mediainfo)
        seasons = self._object_value(exists_info, "seasons", {}) or {}
        normalized_seasons = {}
        for raw_season, raw_episodes in seasons.items():
            season = self._as_int(raw_season)
            if season is None:
                continue
            normalized_seasons[season] = {
                episode
                for episode in (
                    self._as_int(raw_episode) for raw_episode in (raw_episodes or [])
                )
                if episode is not None
            }
        poster = None
        get_poster = getattr(mediainfo, "get_poster_image", None)
        if callable(get_poster):
            poster = get_poster()
        return {"seasons": normalized_seasons, "poster": poster}

    @staticmethod
    def _calendar_episode_in_library(item: dict, library: dict) -> bool:
        try:
            season = int(item.get("season"))
            episode = int(item.get("episode"))
        except (TypeError, ValueError):
            return False
        return episode in (library.get("seasons") or {}).get(season, set())

    def _calendar_previous_state(self, item: dict, previous: dict) -> dict:
        previous_item = previous.get(item.get("event_id")) or {}
        state = previous_item.get("moviepilot_state")
        result = dict(item)
        if not result.get("poster") and previous_item.get("poster"):
            result["poster"] = previous_item.get("poster")
        if state:
            result["moviepilot_state"] = state
            result["moviepilot_state_label"] = previous_item.get(
                "moviepilot_state_label"
            ) or self._calendar_state_labels.get(state, "状态未知")
        else:
            result["moviepilot_state"] = "unknown"
            result["moviepilot_state_label"] = self._calendar_state_labels["unknown"]
        result["moviepilot_state_stale"] = True
        return result

    def _set_calendar_state(self, item: dict, state: str) -> dict:
        result = dict(item)
        result["moviepilot_state"] = state
        result["moviepilot_state_label"] = self._calendar_state_labels[state]
        result.pop("moviepilot_state_stale", None)
        return result

    def _enrich_calendar_states(
        self,
        items: List[dict],
        previous_items: Optional[List[dict]] = None,
    ) -> Tuple[List[dict], dict]:
        """按剧集批量加载 MoviePilot 数据并计算逐集状态。"""
        previous = {
            item.get("event_id"): item
            for item in (previous_items or [])
            if item.get("event_id")
        }
        downloads = []
        subscriptions = []
        downloads_failed = False
        subscriptions_failed = False
        try:
            downloads = self._load_calendar_downloads()
        except Exception as exc:
            downloads_failed = True
            logger.warning(
                f"Trakt 日历读取 MoviePilot 下载任务失败：{self._safe_error(str(exc))}"
            )
        try:
            subscriptions = SubscribeOper().list() or []
        except Exception as exc:
            subscriptions_failed = True
            logger.warning(
                f"Trakt 日历读取 MoviePilot 订阅失败：{self._safe_error(str(exc))}"
            )

        unique_shows = {}
        for item in items:
            tmdb_id = self._as_int(item.get("show_tmdb_id"))
            if tmdb_id is not None and tmdb_id not in unique_shows:
                unique_shows[tmdb_id] = item

        library_results = {}
        library_failures = set()
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(self._query_calendar_library, item): tmdb_id
                for tmdb_id, item in unique_shows.items()
            }
            for future in as_completed(futures):
                tmdb_id = futures[future]
                try:
                    library_results[tmdb_id] = future.result()
                except Exception as exc:
                    library_failures.add(tmdb_id)
                    logger.warning(
                        "Trakt 日历查询 MoviePilot 媒体库失败："
                        f"tmdb={tmdb_id}，{self._safe_error(str(exc))}"
                    )

        now = datetime.datetime.now(datetime.timezone.utc)
        enriched = []
        stale_count = 0
        for item in items:
            tmdb_id = self._as_int(item.get("show_tmdb_id"))
            season = self._as_int(item.get("season"))
            episode = self._as_int(item.get("episode"))
            if tmdb_id is None or not season or episode is None or tmdb_id in library_failures:
                result = self._calendar_previous_state(item, previous)
                stale_count += 1
                enriched.append(result)
                continue

            library = library_results.get(tmdb_id) or {"seasons": {}}
            item_with_poster = dict(item)
            if not item_with_poster.get("poster") and library.get("poster"):
                item_with_poster["poster"] = library.get("poster")
            if self._calendar_episode_in_library(item_with_poster, library):
                enriched.append(self._set_calendar_state(item_with_poster, "in_library"))
                continue
            if downloads_failed:
                result = self._calendar_previous_state(item_with_poster, previous)
                stale_count += 1
                enriched.append(result)
                continue

            matching_downloads = [
                download
                for download in downloads
                if self._calendar_download_matches(item_with_poster, download)
            ]
            if any(
                download.get("state") not in ("completed", "seeding")
                for download in matching_downloads
            ):
                enriched.append(self._set_calendar_state(item_with_poster, "downloading"))
                continue
            if matching_downloads:
                enriched.append(
                    self._set_calendar_state(item_with_poster, "pending_library")
                )
                continue

            first_aired = self._parse_trakt_datetime(item_with_poster.get("first_aired"))
            if first_aired is None or first_aired > now:
                enriched.append(self._set_calendar_state(item_with_poster, "unaired"))
                continue
            if subscriptions_failed:
                result = self._calendar_previous_state(item_with_poster, previous)
                stale_count += 1
                enriched.append(result)
                continue
            if self._calendar_is_subscribed(item_with_poster, subscriptions):
                enriched.append(self._set_calendar_state(item_with_poster, "subscribed"))
            else:
                enriched.append(self._set_calendar_state(item_with_poster, "missing"))

        return enriched, {
            "moviepilot_status_stale": stale_count > 0,
            "moviepilot_status_stale_count": stale_count,
            "moviepilot_status_lookup_errors": (
                len(library_failures)
                + int(downloads_failed)
                + int(subscriptions_failed)
            ),
        }

    def get_trakt_calendar(
        self,
        target: str = "my",
        calendar_type: str = "shows",
        start_date: Optional[str] = None,
        days: int = 14,
        page: int = 1,
        limit: int = 20,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """查询 Trakt 日历，并按需附加 MoviePilot 逐集状态。"""
        target = (target or "").lower()
        calendar_type = (calendar_type or "").lower()
        try:
            if target not in ("my", "all"):
                raise ValueError("target 仅支持 my、all")
            if calendar_type not in self._calendar_paths:
                raise ValueError(
                    "calendar_type 仅支持 shows、movies、new_shows、"
                    "season_premieres、finales、dvd"
                )
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 33:
                raise ValueError("days 必须在 1 到 33 之间")
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise ValueError("page 必须大于等于 1")
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
                or limit > 100
            ):
                raise ValueError("limit 必须在 1 到 100 之间")
            start_date = start_date or self._calendar_today()
            try:
                parsed_start = datetime.date.fromisoformat(start_date)
            except (TypeError, ValueError) as exc:
                raise ValueError("start_date 必须是 YYYY-MM-DD 格式") from exc
            if parsed_start.isoformat() != start_date:
                raise ValueError("start_date 必须是 YYYY-MM-DD 格式")

            account_meta = None
            account_uuid = None
            if target == "my":
                account, account_meta = self._get_account(force_refresh=force_refresh)
                account_uuid = account.get("uuid")
                if not account_uuid:
                    raise TraktRequestError("Trakt 账户缺少 UUID")

            include_moviepilot_status = (
                target == "my" and calendar_type in self._calendar_show_types
            )
            snapshot_key = None
            snapshot = {}
            if include_moviepilot_status:
                snapshot_key = self._calendar_snapshot_key(
                    account_uuid,
                    calendar_type,
                    start_date,
                    days,
                )
                snapshot = self.get_data(snapshot_key) or {}
                snapshot_fresh = (
                    bool(snapshot.get("data") is not None)
                    and time.time() - float(snapshot.get("timestamp") or 0)
                    < self._calendar_cache_ttl
                )
                if snapshot_fresh and not force_refresh:
                    all_data = snapshot.get("data") or []
                    cache_meta = {
                        "cached": True,
                        "stale": bool((snapshot.get("cache") or {}).get("stale")),
                        "fetched_at": snapshot.get("fetched_at"),
                    }
                    if (snapshot.get("cache") or {}).get("fallback_reason"):
                        cache_meta["fallback_reason"] = snapshot["cache"][
                            "fallback_reason"
                        ]
                    status_meta = snapshot.get("status_meta") or {}
                else:
                    all_data = None
            else:
                all_data = None

            if all_data is None:
                endpoint = (
                    f"/calendars/{target}/{self._calendar_paths[calendar_type]}/"
                    f"{start_date}/{days}"
                )
                try:
                    response = self._cached_request(
                        f"calendar:{target}:{calendar_type}",
                        endpoint,
                        params={"extended": "full,images"},
                        requires_auth=target == "my",
                        ttl=self._calendar_cache_ttl,
                        force_refresh=force_refresh,
                        account_uuid=account_uuid,
                    )
                    normalized_items = [
                        self._normalize_calendar_item(item, calendar_type)
                        for item in (response.get("data") or [])
                    ]
                    normalized_items.sort(key=self._calendar_sort_key)
                    cache_meta = response.get("cache") or {}
                    status_meta = {}
                    if include_moviepilot_status:
                        normalized_items, status_meta = self._enrich_calendar_states(
                            normalized_items,
                            previous_items=snapshot.get("data") or [],
                        )
                        fetched_at = self._now_iso()
                        snapshot = {
                            "timestamp": time.time(),
                            "fetched_at": fetched_at,
                            "account_uuid": account_uuid,
                            "calendar_type": calendar_type,
                            "start_date": start_date,
                            "days": days,
                            "data": normalized_items,
                            "cache": cache_meta,
                            "status_meta": status_meta,
                        }
                        self.save_data(snapshot_key, snapshot)
                        cache_meta = {
                            "cached": False,
                            "stale": bool(cache_meta.get("stale")),
                            "fetched_at": fetched_at,
                            **(
                                {"fallback_reason": cache_meta.get("fallback_reason")}
                                if cache_meta.get("fallback_reason")
                                else {}
                            ),
                        }
                    all_data = normalized_items
                except TraktRequestError as exc:
                    if include_moviepilot_status and snapshot.get("data") is not None:
                        all_data = snapshot.get("data") or []
                        cache_meta = {
                            "cached": True,
                            "stale": True,
                            "fetched_at": snapshot.get("fetched_at"),
                            "fallback_reason": self._safe_error(str(exc)),
                        }
                        status_meta = snapshot.get("status_meta") or {}
                    else:
                        raise

            all_data = [
                self._calendar_item_with_normalized_poster(item)
                for item in all_data
            ]
            start = (page - 1) * limit
            page_data = all_data[start : start + limit]
            meta = {
                "target": target,
                "calendar_type": calendar_type,
                "start_date": start_date,
                "days": days,
                "timezone": str(settings.TZ),
                "pagination": self._pagination_meta(
                    {},
                    page,
                    limit,
                    len(page_data),
                    total=len(all_data),
                ),
                "moviepilot_status_included": include_moviepilot_status,
                **cache_meta,
                **status_meta,
            }
            if account_meta is not None:
                meta["account_cache"] = account_meta
            return {
                "success": True,
                "meta": meta,
                "data": self._sanitize_payload(page_data),
            }
        except ValueError as exc:
            return self._failure_payload("invalid_parameters", str(exc))
        except TraktRequestError as exc:
            return self._failure_payload(
                "trakt_request_failed",
                str(exc),
                target=target,
                calendar_type=calendar_type,
                start_date=start_date,
                days=days,
            )

    @staticmethod
    def _list_endpoint(category: str, media_type: str, period: str) -> str:
        prefix = "/movies" if media_type == "movies" else "/shows"
        if category in ("popular", "trending", "anticipated"):
            return f"{prefix}/{category}"
        if category in ("watched", "collected"):
            return f"{prefix}/{category}/{period}"
        if category == "recommended":
            return f"/recommendations/{media_type}"
        if category == "boxoffice":
            return "/movies/boxoffice"
        raise ValueError(f"不支持的榜单分类：{category}")

    def get_trakt_lists(
        self,
        category: str = "popular",
        media_type: str = "movies",
        period: str = "weekly",
        page: int = 1,
        limit: int = 20,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        category = (category or "").lower()
        media_type = (media_type or "").lower()
        period = (period or "weekly").lower()
        try:
            if category not in (
                "popular",
                "trending",
                "anticipated",
                "watched",
                "collected",
                "recommended",
                "boxoffice",
            ):
                raise ValueError("category 参数无效")
            if media_type not in ("movies", "shows"):
                raise ValueError("media_type 仅支持 movies 或 shows")
            if period not in ("daily", "weekly", "monthly", "yearly", "all"):
                raise ValueError("period 参数无效")
            if page < 1:
                raise ValueError("page 必须大于等于 1")
            if limit < 1 or limit > 100:
                raise ValueError("limit 必须在 1 到 100 之间")
            if category == "boxoffice" and (media_type != "movies" or page != 1):
                raise ValueError("boxoffice 只支持 movies 且 page 必须为 1")

            endpoint = self._list_endpoint(category, media_type, period)
            requires_auth = category == "recommended"
            account_uuid = None
            account_meta = None
            if requires_auth:
                account, account_meta = self._get_account(force_refresh=force_refresh)
                account_uuid = account.get("uuid")

            if category == "recommended":
                params = {
                    "ignore_collected": "true",
                    "ignore_watchlisted": "true",
                    "extended": "full",
                    "limit": self._trakt_recommendation_limit,
                }
            elif category == "boxoffice":
                params = {"extended": "full"}
            else:
                params = {"page": page, "limit": limit, "extended": "full"}

            period_key = (
                period if category in ("watched", "collected") else "none"
            )
            response = self._cached_request(
                f"lists:{category}:{media_type}:{period_key}",
                endpoint,
                params=params,
                requires_auth=requires_auth,
                ttl=self._personal_cache_ttl if requires_auth else self._public_cache_ttl,
                force_refresh=force_refresh,
                account_uuid=account_uuid,
            )
            raw_items = response.get("data") or []
            pagination = response.get("pagination") or {}
            if category == "recommended":
                start = (page - 1) * limit
                raw_page = raw_items[start : start + limit]
                pagination_meta = self._pagination_meta(
                    {},
                    page,
                    limit,
                    len(raw_page),
                    total=len(raw_items),
                    has_more=start + limit < len(raw_items),
                )
            elif category == "boxoffice":
                raw_page = raw_items[:limit]
                pagination_meta = self._pagination_meta(
                    {},
                    1,
                    limit,
                    len(raw_page),
                    total=len(raw_items),
                    has_more=False,
                )
            else:
                raw_page = raw_items
                pagination_meta = self._pagination_meta(
                    pagination,
                    page,
                    limit,
                    len(raw_page),
                )

            meta = {
                "category": category,
                "media_type": media_type,
                "period": period if category in ("watched", "collected") else None,
                "pagination": pagination_meta,
                **response.get("cache", {}),
            }
            if account_meta:
                meta["account_cache"] = account_meta
            return {
                "success": True,
                "meta": meta,
                "data": [
                    self._normalize_item(item, media_type)
                    for item in raw_page
                ],
            }
        except ValueError as exc:
            return self._failure_payload("invalid_parameters", str(exc))
        except TraktRequestError as exc:
            return self._failure_payload(
                "trakt_request_failed",
                str(exc),
                category=category,
                media_type=media_type,
            )

    @staticmethod
    def _parse_rfc3339(value: Optional[str], field_name: str) -> Optional[datetime.datetime]:
        if not value:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} 必须是 RFC3339 时间") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{field_name} 必须包含时区")
        return parsed

    @staticmethod
    def _personal_media_path(data_type: str, media_type: str) -> Tuple[str, dict]:
        singular = {
            "movies": "movie",
            "shows": "show",
            "seasons": "season",
            "episodes": "episode",
        }
        if data_type == "watchlist":
            item_type = "all" if media_type == "all" else singular[media_type]
            return f"/sync/watchlist/{item_type}/rank/asc", {}
        if data_type == "collection":
            item_type = "media" if media_type == "all" else media_type
            return f"/sync/collection/{item_type}", {}
        if data_type == "history":
            if media_type == "all":
                return "/sync/history", {}
            return f"/sync/history/{singular[media_type]}", {}
        if data_type == "up_next":
            return "/sync/progress/up_next", {}
        raise ValueError(f"不支持的个人数据类型：{data_type}")

    def get_trakt_personal_data(
        self,
        data_type: str = "watchlist",
        media_type: str = "all",
        page: int = 1,
        limit: int = 20,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        data_type = (data_type or "").lower()
        media_type = (media_type or "all").lower()
        try:
            if data_type not in ("watchlist", "collection", "history", "up_next", "stats"):
                raise ValueError("data_type 参数无效")
            if media_type not in ("movies", "shows", "seasons", "episodes", "all"):
                raise ValueError("media_type 参数无效")
            if page < 1:
                raise ValueError("page 必须大于等于 1")
            if limit < 1 or limit > 100:
                raise ValueError("limit 必须在 1 到 100 之间")
            start_time = self._parse_rfc3339(start_at, "start_at")
            end_time = self._parse_rfc3339(end_at, "end_at")
            if start_time and end_time and start_time > end_time:
                raise ValueError("start_at 不能晚于 end_at")
            if data_type != "history" and (start_at or end_at):
                raise ValueError("start_at/end_at 仅适用于 history")
            if data_type == "stats" and page != 1:
                raise ValueError("stats 只支持 page=1")

            account, account_meta = self._get_account(force_refresh=force_refresh)
            account_uuid = account.get("uuid")
            if data_type == "stats":
                slug = account.get("slug") or account.get("username")
                if not slug:
                    raise TraktRequestError("Trakt 账户缺少 slug")
                endpoint = f"/users/{slug}/stats"
                params = {}
            else:
                endpoint, params = self._personal_media_path(data_type, media_type)
                local_pagination = data_type == "collection" and media_type == "shows"
                params = {"extended": "full"}
                if not local_pagination:
                    params.update({"page": page, "limit": limit})
                if data_type == "history":
                    if start_at:
                        params["start_at"] = start_at
                    if end_at:
                        params["end_at"] = end_at
                if data_type == "up_next":
                    params.update({"sort_by": "activity", "sort_how": "desc"})

            response = self._cached_request(
                f"personal:{data_type}:{media_type}",
                endpoint,
                params=params,
                requires_auth=True,
                ttl=self._personal_cache_ttl,
                force_refresh=force_refresh,
                account_uuid=account_uuid,
            )
            raw_data = response.get("data")
            if data_type == "stats":
                stats = self._sanitize_payload(raw_data or {})
                if media_type != "all":
                    stats = stats.get(media_type) or {}
                data = stats
                pagination = self._pagination_meta(
                    {},
                    1,
                    1,
                    1 if stats else 0,
                    total=1 if stats else 0,
                    has_more=False,
                )
            else:
                raw_items = raw_data or []
                if data_type == "collection" and media_type == "shows":
                    start = (page - 1) * limit
                    raw_page = raw_items[start : start + limit]
                else:
                    raw_page = raw_items
                data = [
                    self._normalize_item(
                        item,
                        None if media_type == "all" else media_type,
                    )
                    for item in raw_page
                ]
                if data_type == "up_next" and media_type != "all":
                    wanted_type = media_type.rstrip("s")
                    data = [item for item in data if item.get("type") == wanted_type]
                if data_type == "collection" and media_type == "shows":
                    pagination = self._pagination_meta(
                        {},
                        page,
                        limit,
                        len(data),
                        total=len(raw_items),
                        has_more=(page * limit) < len(raw_items),
                    )
                else:
                    pagination = self._pagination_meta(
                        response.get("pagination") or {},
                        page,
                        limit,
                        len(data),
                    )

            return {
                "success": True,
                "meta": {
                    "data_type": data_type,
                    "media_type": media_type,
                    "pagination": pagination,
                    "account_cache": account_meta,
                    **response.get("cache", {}),
                },
                "data": data,
            }
        except ValueError as exc:
            return self._failure_payload("invalid_parameters", str(exc))
        except TraktRequestError as exc:
            return self._failure_payload(
                "trakt_request_failed",
                str(exc),
                data_type=data_type,
                media_type=media_type,
            )

    @staticmethod
    def _normalize_custom_list(raw_list: dict, selected_ids: set) -> dict:
        ids = (raw_list or {}).get("ids") or {}
        list_id = ids.get("trakt") or raw_list.get("id")
        return {
            "list_id": int(list_id) if list_id is not None else None,
            "name": raw_list.get("name"),
            "description": raw_list.get("description"),
            "privacy": raw_list.get("privacy"),
            "display_numbers": raw_list.get("display_numbers"),
            "allow_comments": raw_list.get("allow_comments"),
            "sort_by": raw_list.get("sort_by"),
            "sort_how": raw_list.get("sort_how"),
            "item_count": raw_list.get("item_count") or 0,
            "updated_at": raw_list.get("updated_at"),
            "selected_for_sync": int(list_id) in selected_ids if list_id is not None else False,
        }

    def _selected_list_ids(self) -> List[int]:
        values = self.get_data(self._selected_lists_key) or []
        selected = []
        for value in values:
            try:
                selected.append(int(value))
            except (TypeError, ValueError):
                continue
        return sorted(set(selected))

    def _update_catalog_selection(self, selected_ids: set):
        catalog_key = self._custom_list_catalog_data_key()
        record = self.get_data(catalog_key) or {}
        if not record.get("data"):
            return
        record["data"] = [
            {
                **item,
                "selected_for_sync": item.get("list_id") in selected_ids,
            }
            for item in record["data"]
        ]
        self.save_data(catalog_key, record)

    def _fetch_all_pages(
        self,
        path: str,
        *,
        params: Optional[dict] = None,
        requires_auth: bool = True,
        max_pages: int = 100,
    ) -> List[dict]:
        """同步任务和目录刷新使用的无缓存完整分页读取。"""
        base_params = dict(params or {})
        limit = min(max(int(base_params.pop("limit", 100)), 1), 100)
        page = 1
        results = []
        while page <= max_pages:
            request_params = {**base_params, "page": page, "limit": limit}
            response = self._trakt_request(
                path,
                params=request_params,
                requires_auth=requires_auth,
            )
            items = response.get("data") or []
            if not isinstance(items, list):
                raise TraktRequestError(f"Trakt API 返回的分页数据格式无效：{path}")
            results.extend(items)
            pagination = response.get("pagination") or {}
            page_count = pagination.get("page_count")
            if page_count is not None:
                if page >= page_count:
                    break
            elif len(items) < limit:
                break
            page += 1
        return results

    def _load_custom_list_catalog(
        self,
        force_refresh: bool = False,
    ) -> Tuple[List[dict], dict, dict]:
        account, account_meta = self._get_account(force_refresh=force_refresh)
        account_uuid = account.get("uuid")
        catalog_key = self._custom_list_catalog_data_key(account_uuid)
        cached = self.get_data(catalog_key) or {}
        fresh = (
            cached.get("account_uuid") == account_uuid
            and time.time() - float(cached.get("timestamp") or 0)
            < self._custom_list_catalog_ttl
        )
        if fresh and not force_refresh:
            data = cached.get("data") or []
            selected_ids = set(self._selected_list_ids())
            data = [
                {**item, "selected_for_sync": item.get("list_id") in selected_ids}
                for item in data
            ]
            return data, {
                "cached": True,
                "stale": False,
                "fetched_at": cached.get("fetched_at"),
            }, account_meta

        slug = account.get("slug") or account.get("username")
        if not slug:
            raise TraktRequestError("Trakt 账户缺少 slug")
        try:
            raw_lists = self._fetch_all_pages(
                f"/users/{slug}/lists",
                params={"extended": "full"},
                requires_auth=True,
            )
            selected_ids = set(self._selected_list_ids())
            data = [
                self._normalize_custom_list(item, selected_ids)
                for item in raw_lists
            ]
            data = [item for item in data if item.get("list_id") is not None]
            record = {
                "timestamp": time.time(),
                "fetched_at": self._now_iso(),
                "account_uuid": account_uuid,
                "data": data,
            }
            self.save_data(catalog_key, record)
            return data, {
                "cached": False,
                "stale": False,
                "fetched_at": record["fetched_at"],
            }, account_meta
        except TraktRequestError as exc:
            if cached.get("account_uuid") == account_uuid and cached.get("data") is not None:
                data = cached.get("data") or []
                selected_ids = set(self._selected_list_ids())
                data = [
                    {**item, "selected_for_sync": item.get("list_id") in selected_ids}
                    for item in data
                ]
                return data, {
                    "cached": True,
                    "stale": True,
                    "fetched_at": cached.get("fetched_at"),
                    "fallback_reason": self._safe_error(str(exc)),
                }, account_meta
            raise

    def get_trakt_custom_lists(
        self,
        list_id: Optional[int] = None,
        media_type: str = "all",
        page: int = 1,
        limit: int = 20,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        media_type = (media_type or "all").lower()
        try:
            if media_type not in ("movies", "shows", "seasons", "episodes", "all"):
                raise ValueError("media_type 参数无效")
            if page < 1:
                raise ValueError("page 必须大于等于 1")
            if limit < 1 or limit > 100:
                raise ValueError("limit 必须在 1 到 100 之间")
            if list_id is not None and int(list_id) < 1:
                raise ValueError("list_id 必须为正整数")

            catalog, catalog_cache, account_meta = self._load_custom_list_catalog(
                force_refresh=force_refresh
            )
            if list_id is None:
                start = (page - 1) * limit
                data = catalog[start : start + limit]
                return {
                    "success": True,
                    "meta": {
                        "list_id": None,
                        "media_type": None,
                        "pagination": self._pagination_meta(
                            {},
                            page,
                            limit,
                            len(data),
                            total=len(catalog),
                            has_more=start + limit < len(catalog),
                        ),
                        "account_cache": account_meta,
                        **catalog_cache,
                    },
                    "data": data,
                }

            list_id = int(list_id)
            list_info = next(
                (item for item in catalog if item.get("list_id") == list_id),
                None,
            )
            if not list_info:
                raise ValueError("list_id 不属于当前 Trakt 账户")
            account = (self.get_data(self._account_key) or {}).get("data") or {}
            slug = account.get("slug") or account.get("username")
            account_uuid = account.get("uuid")
            type_path = {
                "movies": "movie",
                "shows": "show",
                "seasons": "season",
                "episodes": "episode",
                "all": "movie,show,season,episode",
            }[media_type]
            endpoint = f"/users/{slug}/lists/{list_id}/items/{type_path}"
            params = {
                "page": page,
                "limit": limit,
                "extended": "full",
                "sort_by": list_info.get("sort_by") or "rank",
                "sort_how": list_info.get("sort_how") or "asc",
            }
            response = self._cached_request(
                f"custom-list:{list_id}:{media_type}",
                endpoint,
                params=params,
                requires_auth=True,
                ttl=self._personal_cache_ttl,
                force_refresh=force_refresh,
                account_uuid=account_uuid,
            )
            raw_items = response.get("data") or []
            data = [
                self._normalize_item(
                    item,
                    None if media_type == "all" else media_type,
                )
                for item in raw_items
            ]
            return {
                "success": True,
                "meta": {
                    "list_id": list_id,
                    "list_name": list_info.get("name"),
                    "selected_for_sync": list_info.get("selected_for_sync", False),
                    "media_type": media_type,
                    "pagination": self._pagination_meta(
                        response.get("pagination") or {},
                        page,
                        limit,
                        len(data),
                    ),
                    "catalog_cache": catalog_cache,
                    "account_cache": account_meta,
                    **response.get("cache", {}),
                },
                "data": data,
            }
        except ValueError as exc:
            return self._failure_payload("invalid_parameters", str(exc))
        except TraktRequestError as exc:
            return self._failure_payload(
                "trakt_request_failed",
                str(exc),
                list_id=list_id,
                media_type=media_type,
            )

    def _trakt_item_to_mediainfo(self, item: dict, media_type: str):
        tmdb_id = item.get("tmdb_id")
        if not tmdb_id:
            return None
        title = item.get("title") or ""
        meta = MetaInfo(title=title)
        meta.type = MediaType.MOVIE if media_type == "movies" else MediaType.TV
        try:
            return self.chain.recognize_media(meta=meta, tmdbid=tmdb_id)
        except Exception as exc:
            logger.error(f"Trakt 媒体识别失败：{title} (tmdb:{tmdb_id}) - {exc}")
            return None

    def _discover_result_key(
        self,
        category: str,
        media_type: str,
        page: int,
        account_uuid: Optional[str],
    ) -> str:
        scope = "personal" if category == "recommended" else "public"
        account_digest = (
            self._account_cache_component(account_uuid)
            if scope == "personal"
            else "shared"
        )
        digest = hashlib.sha256(
            f"{category}:{media_type}:{page}:weekly".encode("utf-8")
        ).hexdigest()[:20]
        return f"{self._discover_cache_prefix}{scope}_{account_digest}_{digest}"

    def _trakt_discover_endpoint(self, list_type: str = "popular_movies", page: int = 1):
        parts = (list_type or "").rsplit("_", 1)
        if (
            len(parts) != 2
            or parts[0]
            not in (
                "popular",
                "trending",
                "recommended",
                "anticipated",
                "watched",
                "collected",
                "boxoffice",
            )
            or parts[1] not in ("movies", "shows")
        ):
            return []
        category, media_type = parts
        page = max(int(page or 1), 1)
        if category == "boxoffice" and page != 1:
            return []
        account_uuid = None
        if category == "recommended":
            account_record = self.get_data(self._account_key) or {}
            account_uuid = (account_record.get("data") or {}).get("uuid")
        cache_key = self._discover_result_key(
            category,
            media_type,
            page,
            account_uuid,
        )
        cached = self.get_data(cache_key) or {}
        ttl = self._personal_cache_ttl if category == "recommended" else self._public_cache_ttl
        if time.time() - float(cached.get("timestamp") or 0) < ttl:
            return cached.get("data") or []

        payload = self.get_trakt_lists(
            category=category,
            media_type=media_type,
            period="weekly",
            page=page,
            limit=self._trakt_page_size,
        )
        if not payload.get("success"):
            logger.error(
                f"Trakt 探索榜单获取失败：{category}/{media_type} "
                f"{payload.get('meta', {}).get('error', {}).get('message')}"
            )
            return []
        results = []
        for item in payload.get("data") or []:
            mediainfo = self._trakt_item_to_mediainfo(item, media_type)
            if mediainfo:
                results.append(mediainfo.to_dict())
        self.save_data(
            cache_key,
            {
                "timestamp": time.time(),
                "fetched_at": self._now_iso(),
                "data": results,
            },
        )
        return results

    def _get_last_activities(self) -> dict:
        response = self._trakt_request("/sync/last_activities", requires_auth=True)
        data = response.get("data")
        if not isinstance(data, dict):
            raise TraktRequestError("Trakt last_activities 返回格式无效")
        return data

    @staticmethod
    def _activity_value(activities: Optional[dict], source_type: str) -> Optional[str]:
        if not activities:
            return None
        if source_type == "watchlist":
            return (activities.get("watchlist") or {}).get("updated_at")
        if source_type == "lists":
            return (activities.get("lists") or {}).get("updated_at")
        return None

    def _watchlist_sync_types(self) -> List[str]:
        if self._media_type == "movie":
            return ["movie"]
        if self._media_type == "show":
            return ["show"]
        return ["movie", "show"]

    @staticmethod
    def _stable_media_key(item: dict) -> Optional[str]:
        item_type = item.get("type")
        trakt_id = item.get("trakt_id")
        if item_type not in ("movie", "show") or trakt_id is None:
            return None
        return f"{item_type}:{trakt_id}"

    @staticmethod
    def _normalize_history_media_type(value: Any) -> Optional[str]:
        normalized = str(value or "").strip().casefold()
        if normalized in ("movie", "movies", "电影"):
            return "movie"
        if normalized in ("show", "shows", "tv", "电视剧", "剧集"):
            return "show"
        return None

    @classmethod
    def _media_signature(cls, item_type: Any, tmdb_id: Any) -> Optional[str]:
        normalized_type = cls._normalize_history_media_type(item_type)
        normalized_tmdb_id = str(tmdb_id or "").strip()
        if not normalized_type or not normalized_tmdb_id:
            return None
        return f"{normalized_type}:tmdb:{normalized_tmdb_id}"

    @staticmethod
    def _history_source_matches(history_source: Any, source: str) -> bool:
        if history_source:
            history_source = str(history_source)
            return history_source == source or (
                history_source == "watchlist" and source.startswith("watchlist:")
            )
        return source.startswith("watchlist:")

    def _history_signatures_for_source(self, source: str) -> set:
        """提取旧版成功历史，用于首次建立新的来源同步状态。"""
        signatures = set()
        for history in (self.get_data("history") or {}).values():
            if not isinstance(history, dict) or not self._history_source_matches(
                history.get("source"), source
            ):
                continue
            signature = self._media_signature(
                history.get("type"), history.get("tmdbid")
            )
            if signature:
                signatures.add(signature)
        return signatures

    def _history_entry_matches(
        self,
        history: dict,
        source: str,
        stable_key: str,
        mediainfo,
    ) -> bool:
        source_key = f"{source}:{stable_key}"
        if history.get("source_key") == source_key:
            return True
        if not self._history_source_matches(history.get("source"), source):
            return False
        history_signature = self._media_signature(
            history.get("type"), history.get("tmdbid")
        )
        media_type = getattr(mediainfo, "type", None)
        current_signature = self._media_signature(
            getattr(media_type, "value", media_type),
            getattr(mediainfo, "tmdb_id", None),
        )
        return bool(history_signature and history_signature == current_signature)

    def _record_sync_history(
        self,
        source: str,
        stable_key: str,
        mediainfo,
        action: str,
    ) -> bool:
        histories = self.get_data("history") or {}
        if action == "exist" and any(
            self._history_entry_matches(history, source, stable_key, mediainfo)
            for history in histories.values()
            if isinstance(history, dict)
        ):
            return False
        media_type = getattr(mediainfo, "type", None)
        media_type_value = getattr(media_type, "value", str(media_type or ""))
        history_id = f"{source}:{stable_key}:{time.time_ns()}"
        histories[history_id] = {
            "title": getattr(mediainfo, "title_year", None)
            or getattr(mediainfo, "title", None)
            or stable_key,
            "type": media_type_value,
            "year": getattr(mediainfo, "year", None),
            "poster": (
                mediainfo.get_poster_image()
                if callable(getattr(mediainfo, "get_poster_image", None))
                else None
            ),
            "overview": getattr(mediainfo, "overview", None),
            "tmdbid": getattr(mediainfo, "tmdb_id", None),
            "action": action,
            "source": source,
            "source_key": f"{source}:{stable_key}",
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data("history", histories)
        return True

    def _subscription_exists(self, mediainfo, meta) -> bool:
        if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
            return True
        season = getattr(meta, "begin_season", None)
        legacy_id_fields = (
            ("tmdbid", "tmdb_id"),
            ("doubanid", "douban_id"),
            ("bangumiid", "bangumi_id"),
            ("anilistid", "anilist_id"),
        )
        for query_field, media_field in legacy_id_fields:
            media_id = getattr(mediainfo, media_field, None)
            if media_id in (None, ""):
                continue
            if SubscribeOper().exists(
                **{query_field: media_id, "season": season}
            ):
                return True
        return False

    def _process_subscription_item(
        self,
        raw_item: dict,
        source: str,
        stable_key: str,
    ) -> bool:
        item = self._normalize_item(raw_item)
        item_type = item.get("type")
        if item_type not in ("movie", "show"):
            return True
        tmdb_id = item.get("tmdb_id")
        title = item.get("title")
        if not tmdb_id or not title:
            logger.error(f"Trakt 同步条目缺少 TMDB ID 或标题：{stable_key}")
            return False
        try:
            meta = MetaInfo(title=title)
            meta.type = MediaType.MOVIE if item_type == "movie" else MediaType.TV
            meta.begin_season = None
            mediainfo = self.chain.recognize_media(meta=meta, tmdbid=tmdb_id)
            if not mediainfo:
                logger.error(f"Trakt 同步媒体识别失败：{stable_key}")
                return False
            library_exists, no_exists = self.downloadchain.get_no_exists_info(
                meta=meta,
                mediainfo=mediainfo,
            )
            if library_exists or self._subscription_exists(mediainfo, meta):
                action = "exist"
            else:
                sub_id, message = self.add_subscribe_season(
                    mediainfo,
                    meta,
                    "trakt",
                    "Trakt Sync",
                )
                if not sub_id:
                    logger.error(f"Trakt 添加订阅失败：{stable_key}，{message or '未知原因'}")
                    return False
                subscribe = SubscribeOper().get(sub_id)
                if subscribe:
                    self.subscribechain.finish_subscribe_or_not(
                        subscribe=subscribe,
                        meta=meta,
                        mediainfo=mediainfo,
                        downloads=[],
                        lefts=no_exists,
                    )
                action = "subscribe"
                media_title = (
                    getattr(mediainfo, "title_year", None)
                    or getattr(mediainfo, "title", None)
                    or stable_key
                )
                logger.info(
                    f"Trakt 新增 MoviePilot 订阅：来源={source}，"
                    f"媒体={media_title}，标识={stable_key}"
                )
            self._record_sync_history(source, stable_key, mediainfo, action)
            return True
        except Exception as exc:
            logger.error(f"Trakt 同步条目处理失败：{stable_key} - {exc}")
            return False

    def _sync_subscription_source(
        self,
        *,
        source: str,
        raw_items: List[dict],
        previous_state: Optional[dict],
        activity_at: Optional[str],
        force: bool,
        bootstrap_signatures: Optional[set] = None,
    ) -> Tuple[dict, dict]:
        previous_state = previous_state or {}
        current = {}
        current_signatures = {}
        for raw_item in raw_items:
            normalized = self._normalize_item(raw_item)
            stable_key = self._stable_media_key(normalized)
            if stable_key:
                current[stable_key] = raw_item
                signature = self._media_signature(
                    normalized.get("type"), normalized.get("tmdb_id")
                )
                if signature:
                    current_signatures[stable_key] = signature

        original_processed = set(previous_state.get("processed") or [])
        migrated = {
            stable_key
            for stable_key, signature in current_signatures.items()
            if signature in (bootstrap_signatures or set())
        }.difference(original_processed)
        previous_processed = original_processed.union(migrated)
        current_keys = set(current)
        retained = previous_processed.intersection(current_keys)
        # force 只强制刷新远程来源；已成功处理的条目仍保持幂等。
        candidates = current_keys.difference(retained)
        succeeded = set()
        failed = set()
        for stable_key in sorted(candidates):
            if self._process_subscription_item(current[stable_key], source, stable_key):
                succeeded.add(stable_key)
            else:
                failed.add(stable_key)

        processed = retained.union(succeeded)
        state = {
            "activity_at": activity_at,
            "processed": sorted(processed),
            "pending": sorted(failed),
            "last_sync_at": self._now_iso(),
            "last_result": {
                "current": len(current_keys),
                "checked": len(candidates),
                "succeeded": len(succeeded),
                "failed": len(failed),
                "removed": len(previous_processed.difference(current_keys)),
                "migrated": len(migrated),
            },
        }
        return state, state["last_result"]

    @staticmethod
    def _log_source_result(source: str, result: dict):
        logger.info(
            "Trakt 来源同步完成："
            f"来源={source}，当前={int(result.get('current') or 0)}，"
            f"迁移={int(result.get('migrated') or 0)}，"
            f"检查={int(result.get('checked') or 0)}，"
            f"成功={int(result.get('succeeded') or 0)}，"
            f"失败={int(result.get('failed') or 0)}，"
            f"移除={int(result.get('removed') or 0)}"
        )

    @staticmethod
    def _source_can_skip(
        source_state: Optional[dict],
        activity_at: Optional[str],
        activities_available: bool,
        force: bool,
    ) -> bool:
        if force or not activities_available or not source_state or not activity_at:
            return False
        if source_state.get("pending") or source_state.get("fetch_failed"):
            return False
        return source_state.get("activity_at") == activity_at

    def _sync_watchlist_sources(
        self,
        *,
        state: dict,
        activities: Optional[dict],
        force: bool,
        summary: dict,
        bootstrap_legacy: bool = False,
    ):
        activity_at = self._activity_value(activities, "watchlist")
        activities_available = activities is not None
        for media_type in self._watchlist_sync_types():
            source = f"watchlist:{media_type}"
            previous = state["sources"].get(source)
            if self._source_can_skip(
                previous,
                activity_at,
                activities_available,
                force,
            ):
                summary["skipped"].append(source)
                logger.info(f"Trakt 来源未变化，跳过同步：来源={source}")
                continue
            try:
                raw_items = self._fetch_all_pages(
                    f"/sync/watchlist/{media_type}/rank/asc",
                    params={"extended": "full"},
                    requires_auth=True,
                )
                source_state, result = self._sync_subscription_source(
                    source=source,
                    raw_items=raw_items,
                    previous_state=previous,
                    activity_at=activity_at,
                    force=force,
                    bootstrap_signatures=(
                        self._history_signatures_for_source(source)
                        if bootstrap_legacy and not (previous or {}).get("processed")
                        else None
                    ),
                )
                state["sources"][source] = source_state
                summary["sources"][source] = result
                self._log_source_result(source, result)
                if result.get("failed"):
                    summary["failed_sources"].append(source)
            except TraktRequestError as exc:
                error_message = self._safe_error(str(exc))
                failed_state = dict(previous or {})
                failed_state["fetch_failed"] = True
                state["sources"][source] = failed_state
                summary["failed_sources"].append(source)
                summary["errors"].append(
                    {"source": source, "message": error_message}
                )
                logger.error(
                    f"Trakt 来源拉取失败：来源={source}，错误={error_message}"
                )

    def _sync_custom_list_sources(
        self,
        *,
        state: dict,
        activities: Optional[dict],
        force: bool,
        only_list_id: Optional[int],
        summary: dict,
        bootstrap_legacy: bool = False,
    ):
        selected_ids = set(self._selected_list_ids())
        if only_list_id is not None:
            selected_ids = (
                {int(only_list_id)}
                if int(only_list_id) in selected_ids
                else set()
            )
        if not selected_ids:
            return

        lists_activity = self._activity_value(activities, "lists")
        activities_available = activities is not None
        catalog_record = self.get_data(
            self._custom_list_catalog_data_key()
        ) or {}
        catalog = catalog_record.get("data") or []
        catalog_by_id = {
            item.get("list_id"): item
            for item in catalog
            if item.get("list_id") is not None
        }
        need_catalog_refresh = (
            force
            or not activities_available
            or not catalog
            or state.get("lists_activity_at") != lists_activity
        )
        if need_catalog_refresh:
            try:
                catalog, _, _ = self._load_custom_list_catalog(force_refresh=True)
                catalog_by_id = {
                    item.get("list_id"): item
                    for item in catalog
                    if item.get("list_id") is not None
                }
            except TraktRequestError as exc:
                error_message = self._safe_error(str(exc))
                summary["errors"].append(
                    {
                        "source": "custom_lists:catalog",
                        "message": error_message,
                    }
                )
                logger.warning(
                    f"Trakt 自定义列表目录刷新失败：错误={error_message}"
                )

        account = (self.get_data(self._account_key) or {}).get("data") or {}
        slug = account.get("slug") or account.get("username")
        if not slug:
            summary["failed_sources"].append("custom_lists")
            summary["errors"].append(
                {"source": "custom_lists", "message": "Trakt 账户缺少 slug"}
            )
            logger.error("Trakt 自定义列表同步失败：账户缺少 slug")
            return

        for list_id in sorted(selected_ids):
            source = f"custom_list:{list_id}"
            previous = state["sources"].get(source)
            list_info = catalog_by_id.get(list_id) or {}
            activity_at = list_info.get("updated_at") or lists_activity
            if self._source_can_skip(
                previous,
                activity_at,
                activities_available,
                force,
            ):
                summary["skipped"].append(source)
                logger.info(f"Trakt 来源未变化，跳过同步：来源={source}")
                continue
            try:
                raw_items = self._fetch_all_pages(
                    f"/users/{slug}/lists/{list_id}/items/movie,show",
                    params={
                        "extended": "full",
                        "sort_by": list_info.get("sort_by") or "rank",
                        "sort_how": list_info.get("sort_how") or "asc",
                    },
                    requires_auth=True,
                )
                source_state, result = self._sync_subscription_source(
                    source=source,
                    raw_items=raw_items,
                    previous_state=previous,
                    activity_at=activity_at,
                    force=force,
                    bootstrap_signatures=(
                        self._history_signatures_for_source(source)
                        if bootstrap_legacy and not (previous or {}).get("processed")
                        else None
                    ),
                )
                state["sources"][source] = source_state
                summary["sources"][source] = result
                self._log_source_result(source, result)
                if result.get("failed"):
                    summary["failed_sources"].append(source)
            except TraktRequestError as exc:
                error_message = self._safe_error(str(exc))
                failed_state = dict(previous or {})
                failed_state["fetch_failed"] = True
                state["sources"][source] = failed_state
                summary["failed_sources"].append(source)
                summary["errors"].append(
                    {"source": source, "message": error_message}
                )
                logger.error(
                    f"Trakt 来源拉取失败：来源={source}，错误={error_message}"
                )

        if not summary["errors"] or all(
            error.get("source") != "custom_lists:catalog"
            for error in summary["errors"]
        ):
            state["lists_activity_at"] = lists_activity

    def sync_sources(
        self,
        force: bool = False,
        only_list_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """增量同步 Watchlist 和已选自定义列表。"""
        if not _sync_lock.acquire(blocking=False):
            logger.warning("Trakt 同步请求已跳过：已有同步任务正在运行")
            return {"success": False, "message": "Trakt 同步任务正在运行"}
        started_at = self._now_iso()
        mode = "手动刷新" if force else "定时增量"
        scope = (
            f"custom_list:{int(only_list_id)}"
            if only_list_id is not None
            else "watchlist_and_selected_lists"
        )
        logger.info(f"Trakt 同步开始：模式={mode}，范围={scope}")
        summary = {
            "sources": {},
            "skipped": [],
            "failed_sources": [],
            "errors": [],
        }
        self.save_data(
            self._sync_status_key,
            {
                "state": "running",
                "message": "正在同步 Trakt 来源",
                "started_at": started_at,
            },
        )
        try:
            account, _ = self._get_account(force_refresh=True)
            account_uuid = account.get("uuid")
            try:
                activities = self._get_last_activities()
            except TraktRequestError as exc:
                activities = None
                summary["errors"].append(
                    {
                        "source": "last_activities",
                        "message": self._safe_error(str(exc)),
                    }
                )
                logger.warning("Trakt last_activities 获取失败，本次退回完整来源刷新")

            state = self.get_data(self._sync_state_key) or {}
            if state.get("account_uuid") != account_uuid:
                state = {"account_uuid": account_uuid, "sources": {}}
            state.setdefault("sources", {})
            migration = self.get_data(self._legacy_history_migration_key) or {}
            bootstrap_legacy = migration.get("account_uuid") != account_uuid

            if only_list_id is None:
                self._sync_watchlist_sources(
                    state=state,
                    activities=activities,
                    force=force,
                    summary=summary,
                    bootstrap_legacy=bootstrap_legacy,
                )
            self._sync_custom_list_sources(
                state=state,
                activities=activities,
                force=force,
                only_list_id=only_list_id,
                summary=summary,
                bootstrap_legacy=bootstrap_legacy,
            )
            if activities is not None and not summary["failed_sources"]:
                state["last_activities"] = activities
            state["updated_at"] = self._now_iso()
            self.save_data(self._sync_state_key, state)

            expected_watchlist_sources = {
                f"watchlist:{media_type}"
                for media_type in self._watchlist_sync_types()
            }
            if (
                bootstrap_legacy
                and only_list_id is None
                and not expected_watchlist_sources.intersection(
                    summary["failed_sources"]
                )
            ):
                self.save_data(
                    self._legacy_history_migration_key,
                    {
                        "account_uuid": account_uuid,
                        "completed_at": self._now_iso(),
                        "reason": "legacy_history_bootstrap",
                    },
                )

            success = not summary["failed_sources"]
            checked = sum(
                int(result.get("checked") or 0)
                for result in summary["sources"].values()
            )
            failed = sum(
                int(result.get("failed") or 0)
                for result in summary["sources"].values()
            )
            migrated = sum(
                int(result.get("migrated") or 0)
                for result in summary["sources"].values()
            )
            summary["migrated"] = migrated
            message = (
                f"同步完成：迁移 {migrated} 项，检查 {checked} 项，失败 {failed} 项，"
                f"跳过 {len(summary['skipped'])} 个未变化来源"
            )
            self.save_data(
                self._sync_status_key,
                {
                    "state": "success" if success else "partial",
                    "message": message,
                    "started_at": started_at,
                    "finished_at": self._now_iso(),
                    "summary": summary,
                },
            )
            if success:
                logger.info(f"Trakt {message}")
            else:
                logger.warning(f"Trakt {message}")
            return {"success": success, "message": message, "summary": summary}
        except TraktRequestError as exc:
            message = self._safe_error(str(exc))
            self.save_data(
                self._sync_status_key,
                {
                    "state": "failed",
                    "message": message,
                    "started_at": started_at,
                    "finished_at": self._now_iso(),
                },
            )
            logger.error(f"Trakt 同步失败：{message}")
            return {"success": False, "message": message}
        except Exception as exc:
            message = self._safe_error(str(exc))
            self.save_data(
                self._sync_status_key,
                {
                    "state": "failed",
                    "message": message,
                    "started_at": started_at,
                    "finished_at": self._now_iso(),
                },
            )
            logger.error(f"Trakt 同步异常：{message}")
            return {"success": False, "message": message}
        finally:
            _sync_lock.release()

    def sync_watchlist(self):
        """兼容原定时任务入口，实际同步所有已启用来源。"""
        return self.sync_sources(force=False)

    def add_subscribe_season(self, mediainfo, meta, nickname, real_name):
        return self.subscribechain.add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=meta.begin_season,
            exist_ok=True,
            username=real_name or "Trakt Sync Plugin",
        )

    def add_subscribe_episode(
        self,
        mediainfo,
        season,
        episodes,
        nickname,
        real_name,
    ):
        return self.subscribechain.add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=season,
            exist_ok=True,
            episode_group=episodes,
            username=real_name or "Trakt Sync Plugin",
        )
