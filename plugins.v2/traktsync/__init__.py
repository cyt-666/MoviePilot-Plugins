import datetime
import requests
import json
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Optional, Any, List, Dict, Tuple, Type

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.media import MediaChain
from app.db.user_oper import UserOper
from pydantic import BaseModel, Field

from app.schemas.types import MediaType, EventType, ChainEventType, SystemConfigKey
from app.schemas.event import DiscoverSourceEventData, DiscoverMediaSource
from app.agent.tools.base import MoviePilotTool

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.db.subscribe_oper import SubscribeOper
from app.core.config import settings
from app.core.event import Event
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.plugins import _PluginBase

lock = Lock()


class GetTraktRecommendationsInput(BaseModel):
    """获取Trakt榜单推荐的输入参数模型"""
    explanation: Optional[str] = Field(None, description="Clear explanation of why this tool is being used in the current context")
    list_type: str = Field(
        "popular_movies",
        description=(
            "Trakt list type to fetch. Values: "
            "'popular_movies' for popular movies, "
            "'popular_shows' for popular TV shows, "
            "'trending_movies' for trending movies, "
            "'trending_shows' for trending TV shows, "
            "'recommended_movies' for recommended movies, "
            "'recommended_shows' for recommended TV shows, "
            "'anticipated_movies' for most anticipated movies, "
            "'anticipated_shows' for most anticipated TV shows"
        )
    )
    page: int = Field(1, description="Page number for pagination (default: 1, 20 items per page)")


class GetTraktRecommendationsTool(MoviePilotTool):
    """获取Trakt榜单推荐工具"""
    name: str = "get_trakt_recommendations"
    description: str = (
        "Get media recommendations from Trakt lists. Returns popular, trending, "
        "recommended, or anticipated movies and TV shows from Trakt. "
        "Supports pagination with 20 items per page."
    )
    args_schema: Type[BaseModel] = GetTraktRecommendationsInput
    _plugin: Any = None

    def __init__(self, session_id: str, user_id: str, plugin_instance=None):
        super().__init__(session_id=session_id, user_id=user_id)
        self._plugin = plugin_instance

    def get_tool_message(self, **kwargs) -> Optional[str]:
        list_type = kwargs.get("list_type", "popular_movies")
        page = kwargs.get("page", 1)
        type_map = {
            "popular_movies": "Trakt 热门电影",
            "popular_shows": "Trakt 热门剧集",
            "trending_movies": "Trakt 趋势电影",
            "trending_shows": "Trakt 趋势剧集",
            "recommended_movies": "Trakt 推荐电影",
            "recommended_shows": "Trakt 推荐剧集",
            "anticipated_movies": "Trakt 待映电影",
            "anticipated_shows": "Trakt 待映剧集",
        }
        desc = type_map.get(list_type, list_type)
        return f"获取Trakt榜单: {desc} (第{page}页)"

    async def run(self, list_type: str = "popular_movies", page: int = 1, **kwargs) -> str:
        page = max(1, page or 1)
        parts = list_type.rsplit("_", 1)
        if len(parts) != 2 or parts[0] not in ("popular", "trending", "recommended", "anticipated") or parts[1] not in ("movies", "shows"):
            return (
                f"无效的list_type: {list_type}。支持的值: "
                "popular_movies, popular_shows, trending_movies, trending_shows, "
                "recommended_movies, recommended_shows, anticipated_movies, anticipated_shows"
            )
        list_category, media_type = parts
        if not self._plugin:
            return "Trakt插件实例未初始化"
        try:
            results = self._plugin._get_trakt_recommendations(list_category, media_type, page)
        except Exception as e:
            return f"获取Trakt推荐失败: {str(e)}"

        if not results:
            return "未找到Trakt推荐内容。"

        simplified = []
        for r in results:
            if not isinstance(r, dict):
                continue
            simplified.append({
                "title": r.get("title"),
                "en_title": r.get("en_title"),
                "year": r.get("year"),
                "type": r.get("type"),
                "tmdb_id": r.get("tmdb_id"),
                "vote_average": r.get("vote_average"),
                "poster_path": r.get("poster_path"),
                "detail_link": r.get("detail_link"),
            })
        result_json = json.dumps(simplified, ensure_ascii=False, indent=2)
        has_more = len(results) >= 20
        payload_msg = f"第 {page} 页，当前页 {len(simplified)} 条结果。"
        if has_more:
            payload_msg += f" 可能有更多数据，可使用 page={page + 1} 获取下一页。"
        return f"{payload_msg}\n\n{result_json}"


class TraktSync(_PluginBase):

    plugin_name = "Trakt Watchlist Sync"

    plugin_desc = "同步Trakt的watch list并添加订阅，提供Trakt榜单推荐数据"

    plugin_icon = "https://raw.githubusercontent.com/cyt-666/MoviePilot-Plugins/main/icons/trakt.png"

    plugin_author = "cyt-666"

    plugin_version = "0.4.5"

    author_url = "https://github.com/cyt-666/MoviePilot-Plugins"

    plugin_config_prefix = "traktsync_"

    plugin_order = 3
    
    auth_level = 2


    _device_code_url = "https://api.trakt.tv/oauth/device/code"


    _token_url = "https://api.trakt.tv/oauth/device/token"

    _refresh_token_url = "https://api.trakt.tv/oauth/token"

    _watchlist_url = "https://api.trakt.tv/sync/watchlist"

    _trakt_api_base = "https://api.trakt.tv"

    # 公开端点，仅需 trakt-api-key (client_id)，无需 OAuth token
    _trakt_list_endpoints = {
        ("popular", "movies"): "/movies/popular",
        ("popular", "shows"): "/shows/popular",
        ("trending", "movies"): "/movies/trending",
        ("trending", "shows"): "/shows/trending",
        ("recommended", "movies"): "/movies/recommended/weekly",
        ("recommended", "shows"): "/shows/recommended/weekly",
        ("anticipated", "movies"): "/movies/anticipated",
        ("anticipated", "shows"): "/shows/anticipated",
    }



    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None
    downloadchain = None
    searchchain = None
    subscribechain = None
    mediachain = None
    useroper = None

    token:dict = {}


     # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False

    _client_id: str = ""
    _client_secret: str = ""

    _media_type: str = ""

    # Trakt 榜单推荐开关
    _enable_popular_movies: bool = False
    _enable_popular_shows: bool = False
    _enable_trending_movies: bool = False
    _enable_trending_shows: bool = False
    _enable_recommended_movies: bool = False
    _enable_recommended_shows: bool = False
    _enable_anticipated_movies: bool = False
    _enable_anticipated_shows: bool = False

    def _threaded_token_request(self, device_code: str, interval: int, count: int):
        """
        在单独的线程中请求 Trakt token。
        """
        for i in range(int(count)):
            time.sleep(interval)
            self.token = self.token_request(device_code)
            if self.token:
                logger.info("Trakt token acquired successfully in thread.")
                break
        if not self.token:
            logger.error("Trakt token request failed in thread.")

    def init_plugin(self, config: dict = None):

        self.downloadchain = DownloadChain()
        self.searchchain = SearchChain()
        self.subscribechain = SubscribeChain()
        self.mediachain = MediaChain()
        self.useroper = UserOper()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._media_type = config.get("media_type")
            self._client_id = config.get("client_id")
            self._client_secret = config.get("client_secret")
            self._enable_popular_movies = config.get("enable_popular_movies", False)
            self._enable_popular_shows = config.get("enable_popular_shows", False)
            self._enable_trending_movies = config.get("enable_trending_movies", False)
            self._enable_trending_shows = config.get("enable_trending_shows", False)
            self._enable_recommended_movies = config.get("enable_recommended_movies", False)
            self._enable_recommended_shows = config.get("enable_recommended_shows", False)
            self._enable_anticipated_movies = config.get("enable_anticipated_movies", False)
            self._enable_anticipated_shows = config.get("enable_anticipated_shows", False)

            if not self._client_id or not self._client_secret:
                logger.error("Trakt Client ID 或 Client Secret 未设置")
                return
            
            self.token = self.get_data("token")

            if not self.token:
                code = self.device_code_request()
                if not code:
                    logger.error("Trakt device code request failed")
                    return
                interval = code.get("interval")
                expires_in = code.get("expires_in")
                count = expires_in / interval
                user_code = code.get("user_code")
                device_code = code.get("device_code")
                verification_url = code.get("verification_url")
                logger.info(f"Please visit {verification_url} to authorize the app, use code {user_code} in {expires_in} seconds")
                
                # 创建并启动线程
                token_thread = Thread(target=self._threaded_token_request, args=(device_code, interval, count))
                token_thread.daemon = True # 设置为守护线程，主程序退出时线程也会退出
                token_thread.start()
                logger.info("Trakt token acquisition started in a separate thread.")

            if self._enabled or self._onlyonce:
                if self._onlyonce:
                    self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                    logger.info(f"Trakt Watchlist Sync服务启动，立即运行一次")
                    self._scheduler.add_job(func=self.sync_watchlist, trigger='date',
                                            run_date=datetime.datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                            )

                    # 启动任务
                    if self._scheduler.get_jobs():
                        self._scheduler.print_jobs()
                        self._scheduler.start()

                if self._onlyonce:
                    # 关闭一次性开关
                    self._onlyonce = False
                    # 保存配置
                    self.__update_config()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "media_type": self._media_type,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "enable_popular_movies": self._enable_popular_movies,
            "enable_popular_shows": self._enable_popular_shows,
            "enable_trending_movies": self._enable_trending_movies,
            "enable_trending_shows": self._enable_trending_shows,
            "enable_recommended_movies": self._enable_recommended_movies,
            "enable_recommended_shows": self._enable_recommended_shows,
            "enable_anticipated_movies": self._enable_anticipated_movies,
            "enable_anticipated_shows": self._enable_anticipated_shows,
        })  
    

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        # 数据按时间降序排序
        for key in historys.keys():
            historys[key]["id"] = key
        historys = list(historys.values())
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            id = history.get("id")
            title = history.get("title")
            if "season" in history.keys():
                title = f"{title} 第{history.get('season')}季"
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            tmdbid = history.get("tmdbid")
            action = "下载" if history.get("action") == "download" else "订阅" if history.get("action") == "subscribe" \
                else "已订阅" if history.get("action") == "exist" else history.get("action")
            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            "component": "VDialogCloseBtn",
                            "props": {
                                'innerClass': 'absolute top-0 right-0',
                            },
                            'events': {
                                'click': {
                                    'api': 'plugin/TraktSync/delete_history',
                                    'method': 'get',
                                    'params': {
                                        'id': id,
                                        'apikey': settings.API_TOKEN
                                    }
                                }
                            },
                        },
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': poster,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VCardTitle',
                                            'props': {
                                                'class': 'ps-1 pe-5 break-words whitespace-break-spaces'
                                            },
                                            'content': [
                                                {
                                                    'component': 'span',
                                                    'props': {
                                                        'class': 'text-blue-500 hover:text-blue-700'
                                                    },
                                                    'text': title
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'类型：{mtype}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'时间：{time_str}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'操作：{action}'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]
    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除Trakt同步历史记录"
            },
            {
                "path": "/trakt_discover",
                "endpoint": self._trakt_discover_endpoint,
                "methods": ["GET"],
                "summary": "Trakt榜单探索数据",
                "auth": "bear",
            },
        ]

    @eventmanager.register(ChainEventType.DiscoverSource)
    def _on_discover_source(self, event: Event):
        """
        注册Trakt榜单为探索数据源（单个Tab，分组芯片筛选器）
        """
        logger.info("TraktSync _on_discover_source 事件触发")
        if not self._client_id:
            logger.warning("TraktSync client_id 未配置，跳过探索源注册")
            return

        # 收集已启用的榜单，按媒体类型分组
        movie_items = []
        show_items = []
        list_type_map = {
            "_enable_popular_movies": ("popular_movies", "热门"),
            "_enable_trending_movies": ("trending_movies", "趋势"),
            "_enable_recommended_movies": ("recommended_movies", "推荐"),
            "_enable_anticipated_movies": ("anticipated_movies", "待映"),
            "_enable_popular_shows": ("popular_shows", "热门"),
            "_enable_trending_shows": ("trending_shows", "趋势"),
            "_enable_recommended_shows": ("recommended_shows", "推荐"),
            "_enable_anticipated_shows": ("anticipated_shows", "待映"),
        }
        for config_attr, (value, title) in list_type_map.items():
            if getattr(self, config_attr, False):
                if "movies" in value:
                    movie_items.append({"value": value, "title": title})
                else:
                    show_items.append({"value": value, "title": title})

        if not movie_items and not show_items:
            logger.info("TraktSync 未启用任何榜单，跳过探索源注册")
            return

        all_items = movie_items + show_items
        default_list_type = all_items[0]["value"]

        # 构建筛选器UI：分组标签 + 统一芯片组
        filter_ui = []
        if movie_items and show_items:
            # 两组都有时，加标签分隔
            movie_chips = [
                {
                    "component": "VChip",
                    "props": {"value": item["value"], "filter": True},
                    "text": item["title"],
                }
                for item in movie_items
            ]
            show_chips = [
                {
                    "component": "VChip",
                    "props": {"value": item["value"], "filter": True},
                    "text": item["title"],
                }
                for item in show_items
            ]
            filter_ui = [
                {
                    "component": "VChipGroup",
                    "props": {
                        "model": "list_type",
                        "mandatory": "force",
                    },
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "text-subtitle-2 font-weight-bold mr-2 align-self-center"},
                            "text": "电影",
                        },
                        *movie_chips,
                        {
                            "component": "VDivider",
                            "props": {"vertical": True, "class": "mx-2"},
                        },
                        {
                            "component": "div",
                            "props": {"class": "text-subtitle-2 font-weight-bold mr-2 align-self-center"},
                            "text": "剧集",
                        },
                        *show_chips,
                    ],
                },
            ]
        else:
            # 只有一组时不需要标签
            filter_ui = [
                {
                    "component": "VChipGroup",
                    "props": {
                        "model": "list_type",
                        "mandatory": "force",
                    },
                    "content": [
                        {
                            "component": "VChip",
                            "props": {"value": item["value"], "filter": True},
                            "text": item["title"],
                        }
                        for item in all_items
                    ],
                },
            ]

        event_data: DiscoverSourceEventData = event.event_data
        event_data.extra_sources.append(
            DiscoverMediaSource(
                name="Trakt",
                mediaid_prefix="trakt",
                api_path="plugin/TraktSync/trakt_discover",
                filter_params={"list_type": default_list_type},
                filter_ui=filter_ui,
            )
        )
        logger.info(f"TraktSync 添加探索源: Trakt (电影={len(movie_items)}, 剧集={len(show_items)})")

    def get_agent_tools(self) -> list:
        """
        返回Trakt推荐工具供智能体调用
        """
        if not self._client_id:
            return []
        plugin_ref = self
        class BoundTool(GetTraktRecommendationsTool):
            def __init__(tool_self, session_id, user_id):
                super().__init__(session_id, user_id, plugin_instance=plugin_ref)
        return [BoundTool]

    def get_state(self) -> bool:
        return self._enabled
    

    def delete_history(self, id: str, apikey: str):
        """
        删除Trakt同步历史记录
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        # 历史记录
        historys = self.get_data('history')
        if not historys:
            return schemas.Response(success=False, message="未找到历史记录")
        # 删除指定记录
        historys.pop(id)
        self.save_data('history', historys)
        return schemas.Response(success=True, message="删除成功")


    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'media_type',
                                            'label': '媒体类型', 
                                            'items': [
                                                {'title': '全部', 'value': 'all'},
                                                {'title': '电影', 'value': 'movie'},
                                                {'title': '电视剧', 'value': 'show'}
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'client_id',
                                            'label': 'Client ID',
                                            'placeholder': 'Trakt Client ID'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'client_secret',
                                            'label': 'Client Secret',
                                            'placeholder': 'Trakt Client Secret'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VDivider',
                                        'props': {'class': 'my-2'}
                                    },
                                    {
                                        'component': 'div',
                                        'props': {'class': 'text-h6 mb-2'},
                                        'text': 'Trakt榜单探索（启用的榜单会出现在筛选器中）'
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_popular_movies',
                                            'label': '热门电影',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_trending_movies',
                                            'label': '趋势电影',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_recommended_movies',
                                            'label': '推荐电影',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_anticipated_movies',
                                            'label': '待映电影',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_popular_shows',
                                            'label': '热门剧集',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_trending_shows',
                                            'label': '趋势剧集',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_recommended_shows',
                                            'label': '推荐剧集',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_anticipated_shows',
                                            'label': '待映剧集',
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "*/30 * * * *",
            "media_type": "all",
            "client_id": "",
            "client_secret": "",
            "enable_popular_movies": False,
            "enable_popular_shows": False,
            "enable_trending_movies": False,
            "enable_trending_shows": False,
            "enable_recommended_movies": False,
            "enable_recommended_shows": False,
            "enable_anticipated_movies": False,
            "enable_anticipated_shows": False,
        }


    def device_code_request(self) -> dict:
        data = {
            "client_id": self._client_id,
        }
        headers = {
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(self._device_code_url, json=data, headers=headers, proxies=settings.PROXY)
            response.raise_for_status()
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Trakt device code request failed: {e}")
            return None
    


    def token_request(self, code: str) -> dict:
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
        }
        headers = {
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(self._token_url, json=data, headers=headers, proxies=settings.PROXY)
            response.raise_for_status()
            result = json.loads(response.text)
            result["expired_at"] = result.get("created_at") + 24 * 3600
            self.save_data("token", result)
            return json.loads(response.text)
        except Exception as e:
            # logger.error(f"Trakt token request failed: {e}")
            return None
        
    def refresh_token_request(self, refresh_token: str) -> dict:
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        }
        headers = {
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(self._refresh_token_url, json=data, headers=headers, proxies=settings.PROXY)
            response.raise_for_status()
            result = json.loads(response.text)
            result["expired_at"] = result.get("created_at") + 24 * 3600
            self.save_data("token", result)
            return result
        except Exception as e:
            logger.error(f"Trakt refresh token request failed: {e}")
            return None
        
    def get_watchlist(self, access_token: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "trakt-api-version": "2",
            "trakt-api-key": self._client_id,
        }
        url = f"{self._watchlist_url}/{self._media_type}/title/asc"
        try:
            response = requests.get(url, headers=headers, proxies=settings.PROXY)
            response.raise_for_status()
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Trakt get watchlist failed: {e}")
            return None

    def _fetch_trakt_list(self, list_type: str, media_type: str, page: int = 1, limit: int = 20) -> Optional[list]:
        """
        从Trakt公开API获取榜单数据（无需OAuth token）
        :param list_type: popular/trending/recommended/anticipated
        :param media_type: movies/shows
        :param page: 页码
        :param limit: 每页数量
        """
        logger.info(f"TraktSync _fetch_trakt_list: {list_type}/{media_type} page={page}")
        endpoint = self._trakt_list_endpoints.get((list_type, media_type))
        if not endpoint:
            logger.error(f"未知的Trakt榜单类型: {list_type} {media_type}")
            return None
        url = f"{self._trakt_api_base}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self._client_id,
        }
        params = {"page": page, "limit": limit}
        try:
            response = requests.get(url, headers=headers, params=params, proxies=settings.PROXY)
            response.raise_for_status()
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Trakt获取{list_type} {media_type}失败: {e}")
            return None

    def _trakt_item_to_mediainfo(self, trakt_item: dict, media_type_str: str):
        """
        将Trakt条目转换为MediaInfo
        :param trakt_item: Trakt API返回的条目（已提取出movie/show对象）
        :param media_type_str: "movies" 或 "shows"
        """
        ids = trakt_item.get("ids", {})
        tmdb_id = ids.get("tmdb")
        if not tmdb_id:
            return None
        title = trakt_item.get("title", "")
        meta = MetaInfo(title=title)
        meta.type = MediaType.MOVIE if media_type_str == "movies" else MediaType.TV
        try:
            mediainfo = self.chain.recognize_media(meta=meta, tmdbid=tmdb_id)
            return mediainfo
        except Exception as e:
            logger.error(f"识别媒体失败: {title} (tmdb:{tmdb_id}) - {e}")
            return None

    def _get_trakt_recommendations(self, list_type: str, media_type: str, page: int = 1) -> List[dict]:
        """
        获取Trakt榜单推荐数据（带缓存，6小时TTL）
        :param list_type: popular/trending/recommended/anticipated
        :param media_type: movies/shows
        :param page: 页码
        """
        cache_key = f"trakt_cache_{list_type}_{media_type}_{page}"
        cached = self.get_data(cache_key)
        if cached and time.time() - cached.get("timestamp", 0) < 6 * 3600:
            return cached.get("data", [])

        raw_items = self._fetch_trakt_list(list_type, media_type, page=page)
        if not raw_items:
            return []

        results = []
        for item in raw_items:
            # trending/recommended 返回格式: {watchers: N, movie/show: {...}}
            # popular/anticipated 返回格式: {title, year, ids: {...}}
            media_obj = item.get("movie") or item.get("show") or item
            mediainfo = self._trakt_item_to_mediainfo(media_obj, media_type)
            if mediainfo:
                results.append(mediainfo.to_dict())

        self.save_data(cache_key, {"data": results, "timestamp": time.time()})
        return results

    def _trakt_discover_endpoint(self, list_type: str = "popular_movies", page: int = 1):
        """
        统一的Trakt榜单探索端点
        :param list_type: 榜单类型，如 popular_movies, trending_shows 等
        :param page: 页码
        """
        parts = list_type.rsplit("_", 1)
        if len(parts) != 2 or parts[0] not in ("popular", "trending", "recommended", "anticipated") or parts[1] not in ("movies", "shows"):
            return []
        list_category, media_type = parts
        logger.info(f"TraktSync 探索端点被调用: {list_category}/{media_type} page={page}")
        return self._get_trakt_recommendations(list_category, media_type, page)

    def sync_watchlist(self):
        token = self.get_data("token")
        if not token:
            logger.error("Trakt token not found")
            return
        if token.get("expired_at") < time.time():
            token = self.refresh_token_request(token.get("refresh_token"))
        if not token:
            logger.error("Trakt token refresh failed")
            return
        watchlist = self.get_watchlist(token.get("access_token"))
        if not watchlist:
            logger.error("Trakt get watchlist failed")
            return
        logger.info(f"Trakt get watchlist: {[w.get('id') for w in watchlist]}")
        history = self.get_data("history")
        for item in watchlist:
            not_in_no_exists = True
            s_type = "movie"
            if item.get("type") != "movie":
                s_type = "show"
            else:
                s_type = "movie"
            trakt_media_info = item.get(s_type)
            if history and str(item.get("id")) in history.keys():
                logger.info(f'{trakt_media_info.get("title")} 已经同步过，直接跳过')
                continue
            meta = MetaInfo(title=trakt_media_info.get("title"))
            meta.type = MediaType.MOVIE if s_type == "movie" else MediaType.TV
            if trakt_media_info.get("ids").get("tmdb") is not None:
                mediainfo = self.chain.recognize_media(meta=meta, tmdbid=trakt_media_info.get("ids").get("tmdb"))
                exist_flag, no_exists = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                if exist_flag:
                    logger.info(f'{mediainfo.title_year}已经被订阅')
                    action = "exist"
                else:
                    if meta.type == MediaType.MOVIE:
                        exist_flag = self.subscribechain.exists(mediainfo=mediainfo, meta=meta)
                        if exist_flag:
                            logger.info(f'{mediainfo.title_year} 已经订阅')
                            action = "exist"
                            continue
                        sub_id, message = self.add_subscribe_season(mediainfo, meta, "trakt", "trakt_sync")
                        subscribe = SubscribeOper().get(sub_id)
                        if subscribe:
                            self.subscribechain.finish_subscribe_or_not(subscribe=subscribe,
                                                                        meta=meta,
                                                                        mediainfo=mediainfo,
                                                                        downloads=[],
                                                                        lefts=no_exists)
                        logger.info(f'{mediainfo.title_year} 添加订阅成功')
                        action = "subscribe"
                    else:
                        for no_exist in no_exists.values():
                            for season in no_exist.keys():
                                if item.get("type") == "episode" and season != item.get("episode").get("season"):
                                    continue
                                if item.get("type") == "season" and season != item.get("season").get("number"):
                                    continue
                                meta.begin_season = season
                                exist_flag = self.subscribechain.exists(mediainfo=mediainfo, meta=meta)
                                if exist_flag:
                                    logger.info(f'{mediainfo.title_year} 第{season}季 已经订阅')
                                    action = "exist"
                                    continue
                                sub_id, message = self.add_subscribe_season(mediainfo, meta, "trakt", "trakt_sync")
                                # 更新订阅信息
                                logger.info(f'根据缺失剧集更新订阅信息 {mediainfo.title_year} ...')
                                subscribe = SubscribeOper().get(sub_id)
                                if subscribe:
                                    self.subscribechain.finish_subscribe_or_not(subscribe=subscribe,
                                                                                meta=meta,
                                                                                mediainfo=mediainfo,
                                                                                downloads=[],
                                                                                lefts=no_exists)
                                logger.info(f'{mediainfo.title_year} 添加订阅成功')
                                action = "subscribe"
                                not_in_no_exists = False
            else:
                logger.error(f'{meta.title} 没有TMDB ID')
                continue
            if not_in_no_exists:
                action = "exist"
            if not history:
                history = {}
            tmp = {
                "title": mediainfo.title_year,
                "type": mediainfo.type.value,
                "year": mediainfo.year,
                "poster": mediainfo.get_poster_image(),
                "overview": mediainfo.overview,
                "tmdbid": mediainfo.tmdb_id,
                "action": action,
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            if item.get("type") == "episode":
                tmp["season"] = item.get("episode").get("season")
            if item.get("type") == "season":
                tmp["season"] = item.get("season").get("number")

            history[item.get("id")] = tmp
        self.save_data("history", history)
    
    def add_subscribe_season(self, mediainfo, meta, nickname, real_name):
        return self.subscribechain.add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=meta.begin_season,
            exist_ok=True,
            username=real_name or f"Trakt Sync Plugin"
        )
    def add_subscribe_episode(self, mediainfo, season, episodes, nickname, real_name):
        return self.subscribechain.add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=season,
            exist_ok=True,
            episode_group=episodes,
            username=real_name or f"Trakt Sync Plugin"
        )
    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        logger.info(f"Trakt Sync Plugin service registering")
        if self._enabled and self._cron:
            return [
                {
                    "id": "TraktSync",
                    "name": "Trakt Watchlist Sync",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sync_watchlist,
                    "kwargs": {}
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": "TraktSync",
                    "name": "Trakt Watchlist Sync",
                    "trigger": "interval",
                    "func": self.sync_watchlist,
                    "kwargs": {"minutes": 30}
                }
            ]
        return []
    
    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    