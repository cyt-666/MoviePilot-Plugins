import datetime
import requests
import json
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.media import MediaChain
from app.db.user_oper import UserOper
from app.schemas.types import MediaType, EventType, SystemConfigKey

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import Event
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.plugins import _PluginBase

lock = Lock()


class TraktSync(_PluginBase):

    plugin_name = "Trakt Watchlist Sync"

    plugin_desc = "同步Trakt的watch list并添加订阅"

    plugin_icon = "https://raw.githubusercontent.com/cyt-666/MoviePilot-Plugins/main/icons/trakt.png"

    plugin_author = "cyt-666"

    plugin_version = "0.1.5"

    author_url = "https://github.com/cyt-666"

    plugin_config_prefix = "traktsync_"

    plugin_order = 3
    
    auth_level = 2


    _device_code_url = "https://api.trakt.tv/oauth/device/code"


    _token_url = "https://api.trakt.tv/oauth/device/token"

    _refresh_token_url = "https://api.trakt.tv/oauth/token"

    _watchlist_url = "https://api.trakt.tv/sync/watchlist"



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
            "client_secret": self._client_secret
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
            }
        ]

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
            "client_secret": ""
        }


    def device_code_request(self) -> dict:
        data = {
            "client_id": self._client_id,
        }
        headers = {
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(self._device_code_url, json=data, headers=headers)
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
            response = requests.post(self._token_url, json=data, headers=headers)
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
            response = requests.post(self._refresh_token_url, json=data, headers=headers)
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
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Trakt get watchlist failed: {e}")
            return None
    

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
        logger.info(f"Trakt get watchlist: {[w.get('id') for w in watchlist]}")
        if not watchlist:
            logger.error("Trakt get watchlist failed")
            return
        history = self.get_data("history")
        for item in watchlist:
            not_in_no_exists = False
            s_type = "movie"
            if item.get("type") != "movie":
                s_type = "show"
            else:
                s_type = "movie"
            trakt_media_info = item.get(s_type)
            if str(item.get("id")) in history.keys():
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
                            subscribe = self.subscribechain.subscribeoper.get(sub_id)
                            if subscribe:
                                self.subscribechain.finish_subscribe_or_not(subscribe=subscribe,
                                                                            meta=meta,
                                                                            mediainfo=mediainfo,
                                                                            downloads=[],
                                                                            lefts=no_exists)
                            logger.info(f'{mediainfo.title_year} 添加订阅成功')
                            action = "subscribe"
                            not_in_no_exists = True
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

    