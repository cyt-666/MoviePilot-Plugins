import asyncio
import datetime
import importlib.util
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def _install_module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    if "." in name:
        parent_name, child_name = name.rsplit(".", 1)
        parent = sys.modules.setdefault(parent_name, types.ModuleType(parent_name))
        setattr(parent, child_name, module)
    return module


class _Dummy:
    def __init__(self, *args, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _ResponseSchema(_Dummy):
    pass


class _MoviePilotTool:
    def __init__(self, *args, **kwargs):
        self._agent_context = {}

    @staticmethod
    async def run_blocking(bucket, func, *args, **kwargs):
        return func(*args, **kwargs)

    async def is_admin_user(self):
        return bool(self._agent_context.get("is_admin"))


class _EventManager:
    @staticmethod
    def register(*args, **kwargs):
        return lambda func: func


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _CronTrigger:
    @staticmethod
    def from_crontab(value):
        return value


class _MediaKind:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return isinstance(other, _MediaKind) and self.value == other.value


class _MediaType:
    MOVIE = _MediaKind("movie")
    TV = _MediaKind("tv")


class _MetaInfo:
    def __init__(self, title=""):
        self.title = title
        self.type = None
        self.begin_season = None


class _SubscribeOper:
    subscriptions = {}
    exists_result = False
    exists_calls = []
    listed = []
    list_calls = 0

    def get(self, sub_id):
        return self.subscriptions.get(sub_id)

    def exists(self, **kwargs):
        self.exists_calls.append(kwargs)
        return self.exists_result

    def list(self):
        type(self).list_calls += 1
        return list(type(self).listed)


class _DownloadHistoryOper:
    histories = {}
    calls = []

    def get_by_hashes(self, hashes):
        type(self).calls.append(list(hashes))
        return {
            hash_value: type(self).histories[hash_value]
            for hash_value in hashes
            if hash_value in type(self).histories
        }


def _load_plugin_module_with_stubs():
    _install_module("requests")
    _install_module("pytz", timezone=lambda value: value)
    _install_module("apscheduler")
    _install_module("apscheduler.schedulers")
    _install_module("apscheduler.schedulers.background", BackgroundScheduler=_Dummy)
    _install_module("apscheduler.triggers")
    _install_module("apscheduler.triggers.cron", CronTrigger=_CronTrigger)
    _install_module(
        "pydantic",
        BaseModel=_Dummy,
        Field=lambda default=None, **kwargs: default,
    )

    app = _install_module("app")
    schemas = _install_module("app.schemas", Response=_ResponseSchema)
    app.schemas = schemas
    _install_module("app.chain")
    _install_module("app.chain.media", MediaChain=_Dummy)
    _install_module("app.chain.download", DownloadChain=_Dummy)
    _install_module("app.chain.subscribe", SubscribeChain=_Dummy)
    _install_module("app.db")
    _install_module(
        "app.db.downloadhistory_oper",
        DownloadHistoryOper=_DownloadHistoryOper,
    )
    _install_module("app.db.subscribe_oper", SubscribeOper=_SubscribeOper)

    class _ChainEventType:
        DiscoverSource = "discover_source"

    _install_module(
        "app.schemas.types",
        MediaType=_MediaType,
        ChainEventType=_ChainEventType,
    )
    _install_module(
        "app.schemas.event",
        DiscoverSourceEventData=_Dummy,
        DiscoverMediaSource=_Dummy,
    )
    _install_module("app.agent")
    _install_module("app.agent.tools")
    _install_module("app.agent.tools.base", MoviePilotTool=_MoviePilotTool)

    settings = types.SimpleNamespace(
        PROXY={"https": "http://proxy.invalid"},
        TZ="Asia/Shanghai",
        API_TOKEN="do-not-expose",
    )
    _install_module("app.core")
    _install_module("app.core.config", settings=settings)
    _install_module("app.core.event", Event=_Dummy, eventmanager=_EventManager())
    _install_module("app.core.metainfo", MetaInfo=_MetaInfo)
    _install_module("app.log", logger=_Logger())
    _install_module("app.plugins", _PluginBase=_Dummy)

    plugin_path = Path(__file__).parents[1] / "plugins.v2" / "traktsync" / "__init__.py"
    spec = importlib.util.spec_from_file_location("traktsync_plugin_under_test", plugin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_plugin_module():
    original_modules = sys.modules.copy()
    try:
        return _load_plugin_module_with_stubs()
    finally:
        added_modules = set(sys.modules) - set(original_modules)
        for name in added_modules:
            sys.modules.pop(name, None)
        sys.modules.update(original_modules)


class _Response:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self.text = "" if payload is None else json.dumps(payload)
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _MediaInfo:
    def __init__(self, tmdb_id=1, title="Test", media_type=None):
        self.tmdb_id = tmdb_id
        self.title = title
        self.title_year = f"{title} (2026)"
        self.year = 2026
        self.type = media_type or _MediaType.MOVIE
        self.overview = "overview"

    def get_poster_image(self):
        return "poster.jpg"

    def to_dict(self):
        return {"tmdb_id": self.tmdb_id, "title": self.title}


class _Store:
    def __init__(self):
        self.data = {}

    def attach(self, plugin):
        def get_data(key=None):
            if key is None:
                return [
                    types.SimpleNamespace(key=item_key, value=value)
                    for item_key, value in self.data.items()
                ]
            return self.data.get(key)

        plugin.get_data = get_data
        plugin.save_data = lambda key, value: self.data.__setitem__(key, value)
        plugin.del_data = lambda key: self.data.pop(key, None)
        return self


def _oauth_token(access_token="access-token", expired_at=None):
    return {
        "access_token": access_token,
        "refresh_token": "refresh-token",
        "expired_at": expired_at or time.time() + 3600,
    }


def _movie_item(trakt_id=1, tmdb_id=101, title="Movie"):
    return {
        "type": "movie",
        "movie": {
            "title": title,
            "year": 2026,
            "ids": {"trakt": trakt_id, "tmdb": tmdb_id},
        },
    }


def _show_item(trakt_id=2, tmdb_id=202, title="Show"):
    return {
        "type": "show",
        "show": {
            "title": title,
            "year": 2026,
            "network": "Network",
            "ids": {"trakt": trakt_id, "tmdb": tmdb_id},
        },
    }


def _calendar_show_item(
    show_trakt_id=2,
    show_tmdb_id=202,
    title="Show",
    season=1,
    episode=1,
    episode_trakt_id=2001,
    first_aired="2026-01-01T12:00:00Z",
):
    return {
        "first_aired": first_aired,
        "show": {
            "title": title,
            "year": 2026,
            "network": "Network",
            "overview": "Show overview",
            "runtime": 45,
            "images": {"poster": ["poster.jpg"]},
            "ids": {"trakt": show_trakt_id, "tmdb": show_tmdb_id},
        },
        "episode": {
            "title": f"Episode {episode}",
            "season": season,
            "number": episode,
            "ids": {"trakt": episode_trakt_id, "tmdb": episode_trakt_id + 1},
        },
    }


def _calendar_movie_item(
    trakt_id=3,
    tmdb_id=303,
    title="Calendar Movie",
    released="2026-01-02",
):
    return {
        "released": released,
        "movie": {
            "title": title,
            "year": 2026,
            "overview": "Movie overview",
            "runtime": 120,
            "images": {"poster": ["movie-poster.jpg"]},
            "ids": {"trakt": trakt_id, "tmdb": tmdb_id, "imdb": "tt303"},
        },
    }


class TraktSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_plugin_module()

    def setUp(self):
        self.plugin = self.module.TraktSync()
        self.plugin._client_id = "client-id"
        self.plugin._client_secret = "client-secret"
        self.store = _Store().attach(self.plugin)
        self.plugin.token = {}
        _SubscribeOper.exists_result = False
        _SubscribeOper.exists_calls.clear()
        _SubscribeOper.listed = []
        _SubscribeOper.list_calls = 0
        _DownloadHistoryOper.histories = {}
        _DownloadHistoryOper.calls.clear()

    def test_all_chart_endpoints_and_period(self):
        cases = {
            ("popular", "movies"): "/movies/popular",
            ("popular", "shows"): "/shows/popular",
            ("trending", "movies"): "/movies/trending",
            ("trending", "shows"): "/shows/trending",
            ("anticipated", "movies"): "/movies/anticipated",
            ("anticipated", "shows"): "/shows/anticipated",
            ("watched", "movies"): "/movies/watched/monthly",
            ("watched", "shows"): "/shows/watched/monthly",
            ("collected", "movies"): "/movies/collected/monthly",
            ("collected", "shows"): "/shows/collected/monthly",
            ("recommended", "movies"): "/recommendations/movies",
            ("recommended", "shows"): "/recommendations/shows",
            ("boxoffice", "movies"): "/movies/boxoffice",
        }
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid"}, {"cached": False})
        )
        self.plugin._cached_request = Mock(
            return_value={
                "data": [],
                "pagination": {},
                "cache": {"cached": False, "stale": False},
            }
        )

        for (category, media_type), expected_path in cases.items():
            with self.subTest(category=category, media_type=media_type):
                self.plugin._cached_request.reset_mock()
                payload = self.plugin.get_trakt_lists(
                    category=category,
                    media_type=media_type,
                    period="monthly",
                )
                self.assertTrue(payload["success"])
                self.assertEqual(
                    expected_path,
                    self.plugin._cached_request.call_args.args[1],
                )

    def test_chart_wrapper_and_server_pagination_headers(self):
        self.plugin._cached_request = Mock(
            return_value={
                "data": [
                    {
                        "watchers": 9,
                        "movie": {
                            "title": "Movie",
                            "year": 2026,
                            "ids": {"trakt": 1, "tmdb": 2, "imdb": "tt2"},
                        },
                    }
                ],
                "pagination": {
                    "page": 2,
                    "limit": 20,
                    "page_count": 4,
                    "item_count": 61,
                },
                "cache": {"cached": True, "stale": False},
            }
        )

        payload = self.plugin.get_trakt_lists("trending", "movies", page=2, limit=20)

        self.assertEqual(True, payload["success"])
        self.assertEqual("Movie", payload["data"][0]["title"])
        self.assertEqual("movie", payload["data"][0]["type"])
        self.assertEqual(9, payload["data"][0]["metrics"]["watchers"])
        self.assertTrue(payload["meta"]["pagination"]["has_more"])
        self.assertTrue(payload["meta"]["cached"])

    def test_recommended_fetches_100_and_pages_locally(self):
        raw_items = [
            {"title": f"Movie {index}", "ids": {"trakt": index, "tmdb": index}}
            for index in range(1, 46)
        ]
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid"}, {"cached": False})
        )
        self.plugin._cached_request = Mock(
            return_value={
                "data": raw_items,
                "pagination": {},
                "cache": {"cached": False, "stale": False},
            }
        )

        page_two = self.plugin.get_trakt_lists(
            "recommended", "movies", page=2, limit=20
        )
        page_three = self.plugin.get_trakt_lists(
            "recommended", "movies", page=3, limit=20
        )

        self.assertEqual(list(range(21, 41)), [row["tmdb_id"] for row in page_two["data"]])
        self.assertEqual(list(range(41, 46)), [row["tmdb_id"] for row in page_three["data"]])
        self.assertTrue(page_two["meta"]["pagination"]["has_more"])
        self.assertFalse(page_three["meta"]["pagination"]["has_more"])
        params = self.plugin._cached_request.call_args.kwargs["params"]
        self.assertEqual(100, params["limit"])
        self.assertNotIn("page", params)

    def test_boxoffice_is_movies_first_page_only(self):
        shows = self.plugin.get_trakt_lists("boxoffice", "shows")
        second_page = self.plugin.get_trakt_lists("boxoffice", "movies", page=2)
        self.assertFalse(shows["success"])
        self.assertFalse(second_page["success"])
        self.assertEqual("invalid_parameters", shows["meta"]["error"]["code"])

    def test_parameter_validation(self):
        invalid_payloads = [
            self.plugin.get_trakt_lists("missing", "movies"),
            self.plugin.get_trakt_lists("popular", "people"),
            self.plugin.get_trakt_lists("watched", "movies", "century"),
            self.plugin.get_trakt_lists("popular", "movies", page=0),
            self.plugin.get_trakt_lists("popular", "movies", limit=101),
        ]
        self.assertTrue(all(not payload["success"] for payload in invalid_payloads))

    def test_calendar_endpoints_cover_all_types_and_targets(self):
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid"}, {"cached": False})
        )
        self.plugin._cached_request = Mock(
            return_value={
                "data": [],
                "pagination": {},
                "cache": {"cached": False, "stale": False},
            }
        )
        self.plugin._enrich_calendar_states = Mock(return_value=([], {}))
        expected = {
            "shows": "shows",
            "movies": "movies",
            "new_shows": "shows/new",
            "season_premieres": "shows/premieres",
            "finales": "shows/finales",
            "dvd": "dvd",
        }

        for target in ("all", "my"):
            for calendar_type, suffix in expected.items():
                with self.subTest(target=target, calendar_type=calendar_type):
                    self.plugin._cached_request.reset_mock()
                    payload = self.plugin.get_trakt_calendar(
                        target=target,
                        calendar_type=calendar_type,
                        start_date="2026-08-01",
                        days=14,
                        force_refresh=True,
                    )
                    self.assertTrue(payload["success"])
                    self.assertEqual(
                        f"/calendars/{target}/{suffix}/2026-08-01/14",
                        self.plugin._cached_request.call_args.args[1],
                    )
                    self.assertEqual(
                        target == "my",
                        self.plugin._cached_request.call_args.kwargs[
                            "requires_auth"
                        ],
                    )
                    self.assertEqual(
                        {"extended": "full,images"},
                        self.plugin._cached_request.call_args.kwargs["params"],
                    )

    def test_calendar_normalizes_and_pages_locally(self):
        raw_items = [
            _calendar_movie_item(
                trakt_id=index,
                tmdb_id=1000 + index,
                title=f"Movie {index}",
                released=f"2026-08-{index:02d}",
            )
            for index in range(1, 26)
        ]
        self.plugin._cached_request = Mock(
            return_value={
                "data": raw_items,
                "pagination": {},
                "cache": {"cached": True, "stale": False, "fetched_at": "now"},
            }
        )

        payload = self.plugin.get_trakt_calendar(
            target="all",
            calendar_type="movies",
            start_date="2026-08-01",
            days=25,
            page=2,
            limit=10,
        )

        self.assertTrue(payload["success"])
        self.assertEqual(list(range(11, 21)), [item["trakt_id"] for item in payload["data"]])
        self.assertEqual(25, payload["meta"]["pagination"]["item_count"])
        self.assertTrue(payload["meta"]["pagination"]["has_more"])
        self.assertEqual("movie-poster.jpg", payload["data"][0]["poster"])
        params = self.plugin._cached_request.call_args.kwargs["params"]
        self.assertNotIn("page", params)
        self.assertNotIn("limit", params)

    def test_calendar_parameter_validation_and_default_date(self):
        self.plugin._calendar_today = Mock(return_value="2026-08-01")
        self.plugin._cached_request = Mock(
            return_value={
                "data": [],
                "pagination": {},
                "cache": {"cached": False, "stale": False},
            }
        )

        defaulted = self.plugin.get_trakt_calendar(target="all", calendar_type="shows")
        invalid = [
            self.plugin.get_trakt_calendar(target="missing"),
            self.plugin.get_trakt_calendar(target="all", calendar_type="awards"),
            self.plugin.get_trakt_calendar(target="all", days=0),
            self.plugin.get_trakt_calendar(target="all", days=34),
            self.plugin.get_trakt_calendar(target="all", start_date="2026/08/01"),
            self.plugin.get_trakt_calendar(target="all", page=0),
            self.plugin.get_trakt_calendar(target="all", limit=101),
        ]

        self.assertTrue(defaulted["success"])
        self.assertEqual("2026-08-01", defaulted["meta"]["start_date"])
        self.assertTrue(all(not payload["success"] for payload in invalid))
        self.assertTrue(
            all(
                payload["meta"]["error"]["code"] == "invalid_parameters"
                for payload in invalid
            )
        )

    def test_calendar_groups_air_time_in_moviepilot_timezone(self):
        self.plugin._moviepilot_timezone = Mock(
            return_value=datetime.timezone(datetime.timedelta(hours=8))
        )

        item = self.plugin._normalize_calendar_item(
            _calendar_show_item(first_aired="2026-07-31T18:30:00Z"),
            "shows",
        )

        self.assertEqual("2026-08-01", item["local_date"])
        self.assertEqual("02:30", item["local_time"])

    def test_calendar_personal_show_status_is_not_exposed_publicly(self):
        raw = [_calendar_show_item()]
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid"}, {"cached": False})
        )
        self.plugin._cached_request = Mock(
            return_value={
                "data": raw,
                "pagination": {},
                "cache": {"cached": False, "stale": False},
            }
        )
        self.plugin._enrich_calendar_states = Mock(
            side_effect=lambda items, previous_items=None: (
                [
                    {
                        **items[0],
                        "moviepilot_state": "missing",
                        "moviepilot_state_label": "缺失",
                    }
                ],
                {"moviepilot_status_stale": False},
            )
        )

        personal = self.plugin.get_trakt_calendar(
            target="my",
            calendar_type="shows",
            start_date="2026-08-01",
        )
        public = self.plugin.get_trakt_calendar(
            target="all",
            calendar_type="shows",
            start_date="2026-08-01",
        )
        self.plugin._cached_request.return_value = {
            "data": [_calendar_movie_item()],
            "pagination": {},
            "cache": {"cached": False, "stale": False},
        }
        personal_movie = self.plugin.get_trakt_calendar(
            target="my",
            calendar_type="movies",
            start_date="2026-08-01",
        )

        self.assertEqual("missing", personal["data"][0]["moviepilot_state"])
        self.assertTrue(personal["meta"]["moviepilot_status_included"])
        self.assertNotIn("moviepilot_state", public["data"][0])
        self.assertFalse(public["meta"]["moviepilot_status_included"])
        self.assertNotIn("moviepilot_state", personal_movie["data"][0])
        self.assertFalse(personal_movie["meta"]["moviepilot_status_included"])

    def test_calendar_moviepilot_six_state_precedence_and_batch_reads(self):
        raw_items = [
            _calendar_show_item(
                show_tmdb_id=200 + index,
                show_trakt_id=100 + index,
                title=f"Show {index}",
                episode=index,
                episode_trakt_id=1000 + index,
                first_aired=(
                    "2099-01-01T00:00:00Z"
                    if index == 4
                    else "2020-01-01T00:00:00Z"
                ),
            )
            for index in range(1, 7)
        ]
        items = [
            self.plugin._normalize_calendar_item(item, "shows")
            for item in raw_items
        ]
        downloads = [
            types.SimpleNamespace(
                hash="active",
                state="downloading",
                title="Show 2 S01E02",
                name="Show 2 S01E02",
                season_episode="S01E02",
                media={
                    "tmdbid": 202,
                    "title": "Show 2",
                    "season": "S01",
                    "episode": "E02",
                },
            ),
            types.SimpleNamespace(
                hash="complete",
                state="seeding",
                title="Show 3 S01E03",
                name="Show 3 S01E03",
                season_episode="S01E03",
                media={
                    "tmdbid": 203,
                    "title": "Show 3",
                    "season": "S01",
                    "episode": "E03",
                },
            ),
        ]
        self.plugin.downloadchain = types.SimpleNamespace(
            list_torrents=Mock(return_value=downloads)
        )
        _SubscribeOper.listed = [types.SimpleNamespace(tmdbid=205, season=1)]

        def recognize_media(meta, tmdbid):
            return _MediaInfo(tmdb_id=tmdbid, title=meta.title, media_type=_MediaType.TV)

        def media_exists(mediainfo):
            seasons = {1: [1]} if mediainfo.tmdb_id == 201 else {}
            return types.SimpleNamespace(seasons=seasons) if seasons else None

        self.plugin.chain = types.SimpleNamespace(
            recognize_media=Mock(side_effect=recognize_media),
            media_exists=Mock(side_effect=media_exists),
        )

        enriched, meta = self.plugin._enrich_calendar_states(items)
        states = [item["moviepilot_state"] for item in enriched]

        self.assertEqual(
            [
                "in_library",
                "downloading",
                "pending_library",
                "unaired",
                "subscribed",
                "missing",
            ],
            states,
        )
        self.assertFalse(meta["moviepilot_status_stale"])
        self.assertEqual(1, _SubscribeOper.list_calls)
        self.plugin.downloadchain.list_torrents.assert_called_once_with(
            include_all_tags=False
        )
        self.assertEqual([["active", "complete"]], _DownloadHistoryOper.calls)
        self.assertEqual(6, self.plugin.chain.recognize_media.call_count)
        self.assertEqual(6, self.plugin.chain.media_exists.call_count)

    def test_calendar_status_queries_library_once_per_unique_show(self):
        items = [
            self.plugin._normalize_calendar_item(
                _calendar_show_item(
                    show_tmdb_id=202,
                    episode=episode,
                    episode_trakt_id=2000 + episode,
                    first_aired="2020-01-01T00:00:00Z",
                ),
                "shows",
            )
            for episode in (1, 2)
        ]
        self.plugin.downloadchain = types.SimpleNamespace(
            list_torrents=Mock(return_value=[])
        )
        media = _MediaInfo(tmdb_id=202, title="Show", media_type=_MediaType.TV)
        self.plugin.chain = types.SimpleNamespace(
            recognize_media=Mock(return_value=media),
            media_exists=Mock(return_value=None),
        )

        enriched, _ = self.plugin._enrich_calendar_states(items)

        self.assertEqual(["missing", "missing"], [item["moviepilot_state"] for item in enriched])
        self.plugin.chain.recognize_media.assert_called_once()
        self.plugin.chain.media_exists.assert_called_once()
        self.assertEqual(1, _SubscribeOper.list_calls)

    def test_calendar_status_failure_preserves_previous_or_unknown(self):
        item = self.plugin._normalize_calendar_item(
            _calendar_show_item(first_aired="2020-01-01T00:00:00Z"),
            "shows",
        )
        season_zero = self.plugin._normalize_calendar_item(
            _calendar_show_item(
                show_tmdb_id=303,
                season=0,
                episode=1,
                episode_trakt_id=3001,
                first_aired="2020-01-01T00:00:00Z",
            ),
            "shows",
        )
        previous = [
            {
                **item,
                "moviepilot_state": "subscribed",
                "moviepilot_state_label": "已订阅",
            }
        ]
        self.plugin.downloadchain = types.SimpleNamespace(
            list_torrents=Mock(return_value=[])
        )
        self.plugin.chain = types.SimpleNamespace(
            recognize_media=Mock(side_effect=RuntimeError("media server failed")),
            media_exists=Mock(),
        )

        enriched, meta = self.plugin._enrich_calendar_states(
            [item, season_zero],
            previous_items=previous,
        )

        self.assertEqual("subscribed", enriched[0]["moviepilot_state"])
        self.assertTrue(enriched[0]["moviepilot_state_stale"])
        self.assertEqual("unknown", enriched[1]["moviepilot_state"])
        self.assertTrue(enriched[1]["moviepilot_state_stale"])
        self.assertTrue(meta["moviepilot_status_stale"])

    def test_calendar_download_matching_supports_title_and_whole_season(self):
        item = {
            "show_title": "Fallback Show",
            "show_tmdb_id": 202,
            "season": 2,
            "episode": 7,
        }
        whole_season = {
            "tmdb_id": None,
            "season": None,
            "episode": None,
            "text": "Fallback Show S02 1080p",
        }
        other_episode = {
            "tmdb_id": 202,
            "season": "S02",
            "episode": "E08",
            "text": "Fallback Show S02E08",
        }

        self.assertTrue(
            self.plugin._calendar_download_matches(item, whole_season)
        )
        self.assertFalse(
            self.plugin._calendar_download_matches(item, other_episode)
        )

    def test_unified_request_uses_proxy_timeout_and_pagination_headers(self):
        self.module.requests.get = Mock(
            return_value=_Response(
                200,
                [{"title": "Movie"}],
                {
                    "X-Pagination-Page": "2",
                    "X-Pagination-Limit": "20",
                    "X-Pagination-Page-Count": "5",
                    "X-Pagination-Item-Count": "99",
                },
            )
        )

        result = self.plugin._trakt_request(
            "/movies/popular",
            params={"page": 2, "limit": 20},
        )

        request = self.module.requests.get.call_args
        self.assertEqual("https://api.trakt.tv/movies/popular", request.args[0])
        self.assertEqual(
            {"https": "http://proxy.invalid"},
            request.kwargs["proxies"],
        )
        self.assertEqual((10, 30), request.kwargs["timeout"])
        self.assertNotIn("Authorization", request.kwargs["headers"])
        self.assertEqual(5, result["pagination"]["page_count"])
        self.assertEqual(99, result["pagination"]["item_count"])

    def test_oauth_401_refreshes_once_and_retries(self):
        self.store.data["token"] = _oauth_token("old-token")
        self.module.requests.get = Mock(
            side_effect=[
                _Response(401, {}),
                _Response(200, [{"title": "Movie"}]),
            ]
        )

        def refresh(_):
            token = _oauth_token("new-token")
            self.store.data["token"] = token
            return token

        self.plugin.refresh_token_request = Mock(side_effect=refresh)

        result = self.plugin._trakt_request(
            "/recommendations/movies",
            requires_auth=True,
        )

        self.assertEqual([{"title": "Movie"}], result["data"])
        self.plugin.refresh_token_request.assert_called_once_with("refresh-token")
        calls = self.module.requests.get.call_args_list
        self.assertEqual("Bearer old-token", calls[0].kwargs["headers"]["Authorization"])
        self.assertEqual("Bearer new-token", calls[1].kwargs["headers"]["Authorization"])

    def test_token_refresh_lock_deduplicates_concurrent_refresh(self):
        self.store.data["token"] = _oauth_token(
            "expired-token",
            expired_at=time.time() - 1,
        )
        calls = []

        def refresh(_):
            calls.append(1)
            time.sleep(0.03)
            token = _oauth_token("new-token")
            self.store.data["token"] = token
            return token

        self.plugin.refresh_token_request = refresh
        barrier = threading.Barrier(3)
        results = []

        def worker():
            barrier.wait()
            results.append(self.plugin._get_valid_trakt_token())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(1, len(calls))
        self.assertEqual(["new-token", "new-token"], sorted(row["access_token"] for row in results))

    def test_429_and_5xx_are_not_retried_and_use_stale_cache(self):
        params = {"page": 1, "limit": 20}
        key = self.plugin._cache_key(
            "public",
            None,
            "lists:popular:movies:weekly",
            "/movies/popular",
            params,
        )
        self.store.data[key] = {
            "timestamp": time.time() - 999999,
            "fetched_at": "old",
            "data": [{"title": "Cached"}],
            "pagination": {},
        }
        for status_code in (429, 500):
            with self.subTest(status_code=status_code):
                self.plugin._trakt_request = Mock(
                    side_effect=self.module.TraktRequestError(
                        f"HTTP {status_code}",
                        status_code=status_code,
                    )
                )
                result = self.plugin._cached_request(
                    "lists:popular:movies:weekly",
                    "/movies/popular",
                    params=params,
                    requires_auth=False,
                    ttl=60,
                    force_refresh=False,
                )
                self.assertEqual([{"title": "Cached"}], result["data"])
                self.assertTrue(result["cache"]["cached"])
                self.assertTrue(result["cache"]["stale"])
                self.plugin._trakt_request.assert_called_once()

    def test_cache_ttl_force_refresh_and_account_isolation(self):
        params = {"page": 1}
        public_key = self.plugin._cache_key(
            "public", None, "list", "/movies/popular", params
        )
        account_one_key = self.plugin._cache_key(
            "personal", "uuid-one", "list", "/recommendations/movies", params
        )
        account_two_key = self.plugin._cache_key(
            "personal", "uuid-two", "list", "/recommendations/movies", params
        )
        self.assertNotEqual(account_one_key, account_two_key)
        self.assertIn("uuid-one", account_one_key)
        self.store.data[public_key] = {
            "timestamp": time.time(),
            "fetched_at": "fresh",
            "data": [1],
            "pagination": {},
        }
        self.plugin._trakt_request = Mock(return_value={"data": [2], "pagination": {}})

        cached = self.plugin._cached_request(
            "list",
            "/movies/popular",
            params=params,
            requires_auth=False,
            ttl=3600,
            force_refresh=False,
        )
        refreshed = self.plugin._cached_request(
            "list",
            "/movies/popular",
            params=params,
            requires_auth=False,
            ttl=3600,
            force_refresh=True,
        )

        self.assertEqual([1], cached["data"])
        self.assertEqual([2], refreshed["data"])
        self.plugin._trakt_request.assert_called_once()

    def test_cache_ttl_contracts_and_fresh_catalog_hit(self):
        self.assertEqual(6 * 3600, self.plugin._public_cache_ttl)
        self.assertEqual(15 * 60, self.plugin._personal_cache_ttl)
        self.assertEqual(15 * 60, self.plugin._calendar_cache_ttl)
        self.assertEqual(3600, self.plugin._custom_list_catalog_ttl)
        self.plugin._get_account = Mock(
            return_value=(
                {
                    "uuid": "account-uuid",
                    "slug": "tester",
                },
                {"cached": True},
            )
        )
        catalog_key = self.plugin._custom_list_catalog_data_key("account-uuid")
        self.store.data[catalog_key] = {
            "timestamp": time.time(),
            "fetched_at": "fresh",
            "account_uuid": "account-uuid",
            "data": [{"list_id": 7, "name": "Mine"}],
        }
        self.plugin._fetch_all_pages = Mock()

        catalog, cache_meta, _ = self.plugin._load_custom_list_catalog()

        self.assertEqual([7], [item["list_id"] for item in catalog])
        self.assertTrue(cache_meta["cached"])
        self.assertFalse(cache_meta["stale"])
        self.plugin._fetch_all_pages.assert_not_called()

    def test_personal_calendar_snapshot_ttl_force_and_account_isolation(self):
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-one"}, {"cached": True})
        )
        snapshot_key = self.plugin._calendar_snapshot_key(
            "account-one", "shows", "2026-08-01", 14
        )
        other_key = self.plugin._calendar_snapshot_key(
            "account-two", "shows", "2026-08-01", 14
        )
        self.assertNotEqual(snapshot_key, other_key)
        self.store.data[snapshot_key] = {
            "timestamp": time.time(),
            "fetched_at": "fresh",
            "data": [{"event_id": "cached", "moviepilot_state": "missing"}],
            "cache": {"stale": False},
            "status_meta": {"moviepilot_status_stale": False},
        }
        self.plugin._cached_request = Mock(
            return_value={
                "data": [_calendar_show_item()],
                "pagination": {},
                "cache": {"cached": False, "stale": False},
            }
        )
        self.plugin._enrich_calendar_states = Mock(return_value=([], {}))

        cached = self.plugin.get_trakt_calendar(
            "my", "shows", "2026-08-01", 14
        )
        refreshed = self.plugin.get_trakt_calendar(
            "my", "shows", "2026-08-01", 14, force_refresh=True
        )

        self.assertEqual("cached", cached["data"][0]["event_id"])
        self.assertTrue(cached["meta"]["cached"])
        self.assertEqual([], refreshed["data"])
        self.plugin._cached_request.assert_called_once()

    def test_personal_calendar_falls_back_to_stale_snapshot(self):
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid"}, {"cached": True})
        )
        key = self.plugin._calendar_snapshot_key(
            "account-uuid", "shows", "2026-08-01", 14
        )
        self.store.data[key] = {
            "timestamp": time.time() - 9999,
            "fetched_at": "old",
            "data": [{"event_id": "old", "moviepilot_state": "subscribed"}],
            "cache": {"stale": False},
            "status_meta": {"moviepilot_status_stale": False},
        }
        self.plugin._cached_request = Mock(
            side_effect=self.module.TraktRequestError("HTTP 500", status_code=500)
        )

        payload = self.plugin.get_trakt_calendar(
            "my", "shows", "2026-08-01", 14
        )

        self.assertTrue(payload["success"])
        self.assertTrue(payload["meta"]["stale"])
        self.assertEqual("old", payload["data"][0]["event_id"])

    def test_account_switch_clears_personal_cache_selection_and_sync_state(self):
        self.store.data[self.plugin._account_key] = {
            "timestamp": 1,
            "data": {"uuid": "old-uuid", "slug": "old"},
        }
        personal_key = f"{self.plugin._cache_prefix}personal_old_key"
        self.store.data[personal_key] = {"data": [1]}
        self.store.data[self.plugin._sync_state_key] = {"account_uuid": "old-uuid"}
        self.store.data[self.plugin._selected_lists_key] = [7]
        catalog_key = self.plugin._custom_list_catalog_data_key("old-uuid")
        self.store.data[catalog_key] = {"data": []}
        calendar_snapshot_key = self.plugin._calendar_snapshot_key(
            "old-uuid", "shows", "2026-08-01", 14
        )
        calendar_page_key = self.plugin._calendar_page_data_key("old-uuid")
        calendar_status_key = self.plugin._calendar_status_data_key("old-uuid")
        self.store.data[calendar_snapshot_key] = {"data": []}
        self.store.data[calendar_page_key] = {"data": []}
        self.store.data[calendar_status_key] = {"state": "success"}
        self.plugin._trakt_request = Mock(
            return_value={
                "data": {
                    "user": {
                        "username": "new",
                        "ids": {"uuid": "new-uuid", "slug": "new"},
                    }
                }
            }
        )

        account, _ = self.plugin._get_account(force_refresh=True)

        self.assertEqual("new-uuid", account["uuid"])
        self.assertNotIn(personal_key, self.store.data)
        self.assertNotIn(self.plugin._sync_state_key, self.store.data)
        self.assertNotIn(self.plugin._selected_lists_key, self.store.data)
        self.assertNotIn(catalog_key, self.store.data)
        self.assertNotIn(calendar_snapshot_key, self.store.data)
        self.assertNotIn(calendar_page_key, self.store.data)
        self.assertNotIn(calendar_status_key, self.store.data)

    def test_token_expiry_uses_trakt_expires_in(self):
        created_at = 1_700_000_000
        expires_in = 7_776_000
        self.module.requests.post = Mock(
            return_value=_Response(
                200,
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "created_at": created_at,
                    "expires_in": expires_in,
                },
            )
        )

        token = self.plugin.refresh_token_request("refresh-token")

        self.assertEqual(created_at + expires_in, token["expired_at"])
        self.assertEqual(token, self.store.data["token"])
        request = self.module.requests.post.call_args
        self.assertEqual({"https": "http://proxy.invalid"}, request.kwargs["proxies"])
        self.assertEqual((10, 30), request.kwargs["timeout"])

    def test_personal_data_endpoints_and_history_filters(self):
        self.plugin._get_account = Mock(
            return_value=(
                {"uuid": "account-uuid", "slug": "tester"},
                {"cached": False},
            )
        )
        self.plugin._cached_request = Mock(
            return_value={
                "data": [],
                "pagination": {},
                "cache": {"cached": False, "stale": False},
            }
        )
        cases = {
            ("watchlist", "all"): "/sync/watchlist/all/rank/asc",
            ("watchlist", "movies"): "/sync/watchlist/movie/rank/asc",
            ("collection", "all"): "/sync/collection/media",
            ("collection", "shows"): "/sync/collection/shows",
            ("history", "all"): "/sync/history",
            ("history", "episodes"): "/sync/history/episode",
            ("up_next", "all"): "/sync/progress/up_next",
            ("stats", "all"): "/users/tester/stats",
        }

        for (data_type, media_type), expected_path in cases.items():
            with self.subTest(data_type=data_type, media_type=media_type):
                self.plugin._cached_request.reset_mock()
                payload = self.plugin.get_trakt_personal_data(
                    data_type,
                    media_type,
                    start_at="2026-01-01T00:00:00Z" if data_type == "history" else None,
                    end_at="2026-02-01T00:00:00Z" if data_type == "history" else None,
                )
                self.assertTrue(payload["success"])
                self.assertEqual(
                    expected_path,
                    self.plugin._cached_request.call_args.args[1],
                )
                params = self.plugin._cached_request.call_args.kwargs["params"]
                if data_type == "history":
                    self.assertEqual("2026-01-01T00:00:00Z", params["start_at"])
                    self.assertEqual("2026-02-01T00:00:00Z", params["end_at"])

    def test_personal_data_rfc3339_validation(self):
        invalid = self.plugin.get_trakt_personal_data(
            "history",
            "all",
            start_at="2026-01-01 00:00:00",
        )
        reversed_range = self.plugin.get_trakt_personal_data(
            "history",
            "all",
            start_at="2026-02-01T00:00:00Z",
            end_at="2026-01-01T00:00:00Z",
        )
        wrong_type = self.plugin.get_trakt_personal_data(
            "watchlist",
            "all",
            start_at="2026-01-01T00:00:00Z",
        )
        self.assertFalse(invalid["success"])
        self.assertFalse(reversed_range["success"])
        self.assertFalse(wrong_type["success"])

    def test_history_episode_keeps_episode_identity_and_parent_show(self):
        normalized = self.plugin._normalize_item(
            {
                "id": 99,
                "watched_at": "2026-01-01T00:00:00Z",
                "type": "episode",
                "episode": {
                    "title": "Pilot",
                    "season": 1,
                    "number": 2,
                    "ids": {"trakt": 20, "tmdb": 21},
                },
                "show": {
                    "title": "Series",
                    "ids": {"trakt": 30, "tmdb": 31},
                },
            }
        )

        self.assertEqual("episode", normalized["type"])
        self.assertEqual("Pilot", normalized["title"])
        self.assertEqual(1, normalized["season"])
        self.assertEqual(2, normalized["episode"])
        self.assertEqual("Series", normalized["show_title"])
        self.assertEqual(99, normalized["history_id"])

    def test_show_collection_is_paged_locally(self):
        self.plugin._get_account = Mock(
            return_value=(
                {"uuid": "account-uuid", "slug": "tester"},
                {"cached": False},
            )
        )
        raw_items = [_show_item(index, 1000 + index) for index in range(1, 46)]
        self.plugin._cached_request = Mock(
            return_value={
                "data": raw_items,
                "pagination": {},
                "cache": {"cached": False, "stale": False},
            }
        )

        payload = self.plugin.get_trakt_personal_data(
            "collection",
            "shows",
            page=2,
            limit=20,
        )

        self.assertEqual(list(range(21, 41)), [row["trakt_id"] for row in payload["data"]])
        self.assertEqual(45, payload["meta"]["pagination"]["item_count"])
        self.assertTrue(payload["meta"]["pagination"]["has_more"])
        params = self.plugin._cached_request.call_args.kwargs["params"]
        self.assertNotIn("page", params)
        self.assertNotIn("limit", params)

    def test_personal_stats_are_filtered_and_sanitized(self):
        self.plugin._get_account = Mock(
            return_value=(
                {"uuid": "account-uuid", "slug": "tester"},
                {"cached": True},
            )
        )
        self.plugin._cached_request = Mock(
            return_value={
                "data": {
                    "movies": {"plays": 12},
                    "shows": {"watched": 3},
                    "email": "hidden@example.com",
                    "access_token": "secret",
                },
                "pagination": {},
                "cache": {"cached": True, "stale": False},
            }
        )

        payload = self.plugin.get_trakt_personal_data("stats", "movies")
        serialized = json.dumps(payload)

        self.assertEqual({"plays": 12}, payload["data"])
        self.assertNotIn("hidden@example.com", serialized)
        self.assertNotIn("secret", serialized)

    def test_custom_list_catalog_pagination_and_selected_state(self):
        catalog = [
            {
                "list_id": index,
                "name": f"List {index}",
                "selected_for_sync": index == 2,
            }
            for index in range(1, 6)
        ]
        self.plugin._load_custom_list_catalog = Mock(
            return_value=(
                catalog,
                {"cached": True, "stale": False},
                {"cached": True},
            )
        )

        payload = self.plugin.get_trakt_custom_lists(page=2, limit=2)

        self.assertEqual([3, 4], [row["list_id"] for row in payload["data"]])
        self.assertEqual(5, payload["meta"]["pagination"]["item_count"])
        self.assertTrue(payload["meta"]["pagination"]["has_more"])

    def test_custom_list_item_endpoint_supports_all_media_types(self):
        self.plugin._load_custom_list_catalog = Mock(
            return_value=(
                [
                    {
                        "list_id": 7,
                        "name": "Mine",
                        "sort_by": "rank",
                        "sort_how": "asc",
                        "selected_for_sync": True,
                    }
                ],
                {"cached": False, "stale": False},
                {"cached": False},
            )
        )
        self.store.data[self.plugin._account_key] = {
            "data": {
                "uuid": "account-uuid",
                "slug": "tester",
            }
        }
        self.plugin._cached_request = Mock(
            return_value={
                "data": [_movie_item()],
                "pagination": {"page": 1, "limit": 20, "page_count": 1},
                "cache": {"cached": False, "stale": False},
            }
        )
        expected_types = {
            "movies": "movie",
            "shows": "show",
            "seasons": "season",
            "episodes": "episode",
            "all": "movie,show,season,episode",
        }

        for media_type, path_type in expected_types.items():
            with self.subTest(media_type=media_type):
                self.plugin._cached_request.reset_mock()
                payload = self.plugin.get_trakt_custom_lists(
                    list_id=7,
                    media_type=media_type,
                )
                self.assertTrue(payload["success"])
                self.assertEqual(
                    f"/users/tester/lists/7/items/{path_type}",
                    self.plugin._cached_request.call_args.args[1],
                )

    def test_custom_list_rejects_foreign_list_id(self):
        self.plugin._load_custom_list_catalog = Mock(
            return_value=(
                [{"list_id": 1, "name": "Mine"}],
                {"cached": True},
                {"cached": True},
            )
        )
        payload = self.plugin.get_trakt_custom_lists(list_id=999)
        self.assertFalse(payload["success"])
        self.assertEqual("invalid_parameters", payload["meta"]["error"]["code"])

    def test_last_activities_unchanged_skips_watchlist(self):
        self.plugin._media_type = "movie"
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid", "slug": "tester"}, {})
        )
        self.plugin._get_last_activities = Mock(
            return_value={"watchlist": {"updated_at": "2026-01-01T00:00:00Z"}}
        )
        self.store.data[self.plugin._sync_state_key] = {
            "account_uuid": "account-uuid",
            "sources": {
                "watchlist:movie": {
                    "activity_at": "2026-01-01T00:00:00Z",
                    "processed": ["movie:1"],
                    "pending": [],
                }
            },
        }
        self.plugin._fetch_all_pages = Mock()

        result = self.plugin.sync_sources()

        self.assertTrue(result["success"])
        self.assertEqual(["watchlist:movie"], result["summary"]["skipped"])
        self.plugin._fetch_all_pages.assert_not_called()

    def test_sync_logs_start_source_statistics_and_final_summary(self):
        self.plugin._media_type = "movie"
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid", "slug": "tester"}, {})
        )
        self.plugin._get_last_activities = Mock(
            return_value={"watchlist": {"updated_at": "2026-02-01T00:00:00Z"}}
        )
        self.plugin._fetch_all_pages = Mock(return_value=[_movie_item()])
        self.plugin._process_subscription_item = Mock(return_value=True)

        with patch.object(self.module.logger, "info") as log_info:
            result = self.plugin.sync_sources(force=True)

        self.assertTrue(result["success"])
        messages = [call.args[0] for call in log_info.call_args_list]
        self.assertTrue(any("模式=手动刷新" in message for message in messages))
        self.assertTrue(
            any(
                "Trakt 来源同步完成" in message
                and "来源=watchlist:movie" in message
                and "检查=1" in message
                for message in messages
            )
        )
        self.assertTrue(any("Trakt 同步完成" in message for message in messages))
        serialized = "\n".join(messages)
        self.assertNotIn("account-uuid", serialized)
        self.assertNotIn("access-token", serialized)

    def test_last_activities_change_fetches_source(self):
        self.plugin._media_type = "movie"
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid", "slug": "tester"}, {})
        )
        self.plugin._get_last_activities = Mock(
            return_value={"watchlist": {"updated_at": "2026-02-01T00:00:00Z"}}
        )
        self.store.data[self.plugin._sync_state_key] = {
            "account_uuid": "account-uuid",
            "sources": {
                "watchlist:movie": {
                    "activity_at": "2026-01-01T00:00:00Z",
                    "processed": [],
                    "pending": [],
                }
            },
        }
        self.plugin._fetch_all_pages = Mock(return_value=[_movie_item()])
        self.plugin._process_subscription_item = Mock(return_value=True)

        result = self.plugin.sync_sources()

        self.assertTrue(result["success"])
        self.plugin._fetch_all_pages.assert_called_once()
        state = self.store.data[self.plugin._sync_state_key]["sources"]["watchlist:movie"]
        self.assertEqual("2026-02-01T00:00:00Z", state["activity_at"])
        self.assertEqual(["movie:1"], state["processed"])

    def test_legacy_history_bootstraps_source_state_without_reprocessing(self):
        self.plugin._media_type = "movie"
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid", "slug": "tester"}, {})
        )
        self.plugin._get_last_activities = Mock(
            return_value={"watchlist": {"updated_at": "2026-02-01T00:00:00Z"}}
        )
        self.plugin._fetch_all_pages = Mock(return_value=[_movie_item()])
        self.plugin._process_subscription_item = Mock(return_value=True)
        self.store.data["history"] = {
            "legacy-list-item-id": {
                "title": "Movie (2026)",
                "type": "movie",
                "tmdbid": 101,
                "action": "subscribe",
            }
        }

        result = self.plugin.sync_sources()

        self.assertTrue(result["success"])
        self.assertEqual(1, result["summary"]["migrated"])
        self.plugin._process_subscription_item.assert_not_called()
        source_state = self.store.data[self.plugin._sync_state_key]["sources"][
            "watchlist:movie"
        ]
        self.assertEqual(["movie:1"], source_state["processed"])
        self.assertEqual(
            "account-uuid",
            self.store.data[self.plugin._legacy_history_migration_key][
                "account_uuid"
            ],
        )

    def test_force_refresh_does_not_reprocess_completed_items(self):
        previous = {
            "activity_at": "old",
            "processed": ["movie:1"],
            "pending": [],
        }
        self.plugin._process_subscription_item = Mock(return_value=True)

        state, result = self.plugin._sync_subscription_source(
            source="watchlist:movie",
            raw_items=[_movie_item(), _movie_item(trakt_id=2, tmdb_id=102)],
            previous_state=previous,
            activity_at="new",
            force=True,
        )

        self.assertEqual(["movie:1", "movie:2"], state["processed"])
        self.assertEqual(1, result["checked"])
        self.plugin._process_subscription_item.assert_called_once()
        self.assertEqual(
            "movie:2",
            self.plugin._process_subscription_item.call_args.args[2],
        )

    def test_last_activities_failure_falls_back_to_full_sync(self):
        self.plugin._media_type = "movie"
        self.plugin._get_account = Mock(
            return_value=({"uuid": "account-uuid", "slug": "tester"}, {})
        )
        self.plugin._get_last_activities = Mock(
            side_effect=self.module.TraktRequestError("activities failed")
        )
        self.plugin._fetch_all_pages = Mock(return_value=[])

        result = self.plugin.sync_sources()

        self.assertTrue(result["success"])
        self.plugin._fetch_all_pages.assert_called_once()
        self.assertEqual(
            "last_activities",
            result["summary"]["errors"][0]["source"],
        )

    def test_source_state_removal_and_readdition(self):
        previous = {
            "activity_at": "old",
            "processed": ["movie:1"],
            "pending": [],
        }
        self.plugin._process_subscription_item = Mock(return_value=True)

        removed_state, removed_result = self.plugin._sync_subscription_source(
            source="custom_list:7",
            raw_items=[],
            previous_state=previous,
            activity_at="changed",
            force=False,
        )
        readded_state, _ = self.plugin._sync_subscription_source(
            source="custom_list:7",
            raw_items=[_movie_item()],
            previous_state=removed_state,
            activity_at="changed-again",
            force=False,
        )

        self.assertEqual([], removed_state["processed"])
        self.assertEqual(1, removed_result["removed"])
        self.assertEqual(["movie:1"], readded_state["processed"])
        self.plugin._process_subscription_item.assert_called_once()

    def test_failed_item_remains_pending_and_prevents_skip(self):
        self.plugin._process_subscription_item = Mock(return_value=False)
        state, result = self.plugin._sync_subscription_source(
            source="watchlist:movie",
            raw_items=[_movie_item()],
            previous_state=None,
            activity_at="same",
            force=False,
        )

        self.assertEqual(["movie:1"], state["pending"])
        self.assertEqual([], state["processed"])
        self.assertEqual(1, result["failed"])
        self.assertFalse(
            self.plugin._source_can_skip(
                state,
                "same",
                activities_available=True,
                force=False,
            )
        )

    def test_fetch_failure_preserves_source_state_and_is_retried(self):
        self.plugin._media_type = "movie"
        previous = {
            "activity_at": "same",
            "processed": ["movie:1"],
            "pending": [],
        }
        state = {
            "account_uuid": "account-uuid",
            "sources": {"watchlist:movie": dict(previous)},
        }
        summary = {
            "sources": {},
            "skipped": [],
            "failed_sources": [],
            "errors": [],
        }
        self.plugin._fetch_all_pages = Mock(
            side_effect=self.module.TraktRequestError("HTTP 500")
        )

        self.plugin._sync_watchlist_sources(
            state=state,
            activities={"watchlist": {"updated_at": "same"}},
            force=True,
            summary=summary,
        )

        failed_state = state["sources"]["watchlist:movie"]
        self.assertEqual(previous["activity_at"], failed_state["activity_at"])
        self.assertEqual(previous["processed"], failed_state["processed"])
        self.assertTrue(failed_state["fetch_failed"])
        self.assertFalse(
            self.plugin._source_can_skip(
                failed_state,
                "same",
                activities_available=True,
                force=False,
            )
        )

    def test_season_and_episode_items_never_subscribe(self):
        season = {
            "type": "season",
            "season": {"number": 1, "ids": {"trakt": 10, "tmdb": 11}},
        }
        episode = {
            "type": "episode",
            "episode": {
                "title": "Episode",
                "season": 1,
                "number": 2,
                "ids": {"trakt": 20, "tmdb": 21},
            },
        }
        self.plugin._process_subscription_item = Mock(return_value=True)

        state, result = self.plugin._sync_subscription_source(
            source="custom_list:7",
            raw_items=[season, episode],
            previous_state=None,
            activity_at="now",
            force=True,
        )

        self.assertEqual([], state["processed"])
        self.assertEqual(0, result["checked"])
        self.plugin._process_subscription_item.assert_not_called()

    def test_existing_item_does_not_append_duplicate_history(self):
        media = _MediaInfo(tmdb_id=101)
        self.plugin.chain = types.SimpleNamespace(
            recognize_media=Mock(return_value=media)
        )
        self.plugin.downloadchain = types.SimpleNamespace(
            get_no_exists_info=Mock(return_value=(False, {}))
        )
        self.plugin.subscribechain = types.SimpleNamespace(
            exists=Mock(return_value=True),
            add=Mock(),
            finish_subscribe_or_not=Mock(),
        )
        self.store.data["history"] = {
            "legacy-list-item-id": {
                "title": "Movie (2026)",
                "type": "movie",
                "tmdbid": 101,
                "action": "subscribe",
            }
        }

        result = self.plugin._process_subscription_item(
            _movie_item(),
            "watchlist:movie",
            "movie:1",
        )

        self.assertTrue(result)
        self.assertEqual(1, len(self.store.data["history"]))
        self.plugin.subscribechain.add.assert_not_called()

    def test_new_subscription_logs_source_and_stable_media_key(self):
        media = _MediaInfo(tmdb_id=101)
        self.plugin.chain = types.SimpleNamespace(
            recognize_media=Mock(return_value=media)
        )
        self.plugin.downloadchain = types.SimpleNamespace(
            get_no_exists_info=Mock(return_value=(False, {}))
        )
        self.plugin.subscribechain = types.SimpleNamespace(
            exists=Mock(return_value=False),
            add=Mock(return_value=(123, "ok")),
            finish_subscribe_or_not=Mock(),
        )

        with patch.object(self.module.logger, "info") as log_info:
            result = self.plugin._process_subscription_item(
                _movie_item(),
                "watchlist:movie",
                "movie:1",
            )

        self.assertTrue(result)
        log_info.assert_called_once_with(
            "Trakt 新增 MoviePilot 订阅：来源=watchlist:movie，"
            "媒体=Test (2026)，标识=movie:1"
        )

    def test_sync_now_logs_queue_and_starts_background_thread(self):
        with (
            patch.object(self.module.logger, "info") as log_info,
            patch.object(self.module, "Thread") as thread_class,
        ):
            response = self.plugin.api_sync_now()

        self.assertTrue(response.success)
        log_info.assert_called_once_with("Trakt 手动刷新同步已进入后台队列")
        thread_class.assert_called_once_with(
            target=self.plugin.sync_sources,
            kwargs={"force": True},
            daemon=True,
        )
        thread_class.return_value.start.assert_called_once_with()

    def test_calendar_refresh_api_queues_background_task_and_releases_lock(self):
        started = []

        class _ImmediateThread:
            def __init__(thread_self, target, kwargs, daemon):
                thread_self.target = target
                thread_self.kwargs = kwargs

            def start(thread_self):
                started.append(thread_self.kwargs)
                thread_self.target(**thread_self.kwargs)

        self.plugin.refresh_calendar_page = Mock(
            side_effect=lambda force_refresh, lock_acquired: (
                self.module._calendar_refresh_lock.release()
                or {"success": True}
            )
        )
        with (
            patch.object(self.module, "Thread", _ImmediateThread),
            patch.object(self.module.logger, "info") as log_info,
        ):
            response = self.plugin.api_calendar_refresh()

        self.assertTrue(response.success)
        self.assertEqual(
            [{"force_refresh": True, "lock_acquired": True}],
            started,
        )
        self.assertFalse(self.module._calendar_refresh_lock.locked())
        self.assertTrue(
            any("日历手动刷新已进入后台队列" in call.args[0] for call in log_info.call_args_list)
        )

    def test_calendar_refresh_api_rejects_concurrent_task(self):
        self.module._calendar_refresh_lock.acquire()
        try:
            response = self.plugin.api_calendar_refresh()
        finally:
            self.module._calendar_refresh_lock.release()

        self.assertFalse(response.success)
        self.assertIn("正在刷新", response.message)

    def test_calendar_refresh_keeps_old_page_on_stale_result(self):
        self.store.data[self.plugin._account_key] = {
            "data": {"uuid": "account-uuid"}
        }
        page_key = self.plugin._calendar_page_data_key("account-uuid")
        self.store.data[page_key] = {"data": [{"event_id": "old"}]}
        self.plugin._calendar_today = Mock(return_value="2026-08-01")
        snapshot_key = self.plugin._calendar_snapshot_key(
            "account-uuid", "shows", "2026-08-01", 14
        )
        self.store.data[snapshot_key] = {
            "fetched_at": "old",
            "data": [{"event_id": "stale"}],
        }
        self.plugin.get_trakt_calendar = Mock(
            return_value={
                "success": True,
                "meta": {"stale": True},
                "data": [{"event_id": "stale"}],
            }
        )

        result = self.plugin.refresh_calendar_page(force_refresh=True)

        self.assertTrue(result["success"])
        self.assertEqual("stale", result["state"])
        self.assertEqual([{"event_id": "old"}], self.store.data[page_key]["data"])
        status = self.store.data[
            self.plugin._calendar_status_data_key("account-uuid")
        ]
        self.assertEqual("stale", status["state"])

    def test_calendar_refresh_logs_summary_without_credentials(self):
        self.store.data[self.plugin._account_key] = {
            "data": {"uuid": "account-uuid"}
        }
        self.store.data["token"] = _oauth_token("secret-access-token")
        self.plugin._calendar_today = Mock(return_value="2026-08-01")
        snapshot_key = self.plugin._calendar_snapshot_key(
            "account-uuid", "shows", "2026-08-01", 14
        )
        self.store.data[snapshot_key] = {
            "fetched_at": "now",
            "data": [{"event_id": "episode"}],
        }
        self.plugin.get_trakt_calendar = Mock(
            return_value={"success": True, "meta": {"stale": False}, "data": []}
        )

        with patch.object(self.module.logger, "info") as log_info:
            result = self.plugin.refresh_calendar_page(force_refresh=True)

        self.assertTrue(result["success"])
        serialized = "\n".join(call.args[0] for call in log_info.call_args_list)
        self.assertIn("条目=1", serialized)
        self.assertNotIn("account-uuid", serialized)
        self.assertNotIn("secret-access-token", serialized)

    def test_calendar_has_independent_hourly_service(self):
        self.plugin._enabled = True
        self.plugin._cron = "*/15 * * * *"

        services = self.plugin.get_service()
        by_id = {service["id"]: service for service in services}

        self.assertEqual({"TraktSync", "TraktSyncCalendar"}, set(by_id))
        self.assertEqual({"hours": 1}, by_id["TraktSyncCalendar"]["kwargs"])
        self.assertEqual(
            self.plugin.refresh_calendar_page,
            by_id["TraktSyncCalendar"]["func"],
        )

    def test_calendar_prefetch_runs_once_only_when_snapshot_missing(self):
        started = []

        class _ImmediateThread:
            def __init__(thread_self, target, kwargs, daemon):
                thread_self.target = target
                thread_self.kwargs = kwargs

            def start(thread_self):
                started.append(thread_self.kwargs)
                self.module._calendar_refresh_lock.release()

        self.plugin._enabled = True
        self.plugin.token = _oauth_token()
        with patch.object(self.module, "Thread", _ImmediateThread):
            self.plugin._start_calendar_prefetch_if_needed()

        self.store.data[self.plugin._calendar_page_data_key()] = {"data": []}
        with patch.object(self.module, "Thread", _ImmediateThread):
            self.plugin._start_calendar_prefetch_if_needed()

        self.assertEqual(
            [{"force_refresh": False, "lock_acquired": True}],
            started,
        )
        self.assertFalse(self.module._calendar_refresh_lock.locked())

    def test_legacy_subscription_identity_fallback_prevents_duplicate(self):
        media = _MediaInfo(tmdb_id=101)
        self.plugin.chain = types.SimpleNamespace(
            recognize_media=Mock(return_value=media)
        )
        self.plugin.downloadchain = types.SimpleNamespace(
            get_no_exists_info=Mock(return_value=(False, {}))
        )
        self.plugin.subscribechain = types.SimpleNamespace(
            exists=Mock(return_value=False),
            add=Mock(),
            finish_subscribe_or_not=Mock(),
        )
        _SubscribeOper.exists_result = True

        result = self.plugin._process_subscription_item(
            _movie_item(),
            "watchlist:movie",
            "movie:1",
        )

        self.assertTrue(result)
        self.plugin.subscribechain.add.assert_not_called()
        self.assertEqual(
            {"tmdbid": 101, "season": None},
            _SubscribeOper.exists_calls[0],
        )

    def test_cross_source_duplicate_adds_only_one_moviepilot_subscription(self):
        media = _MediaInfo(tmdb_id=101)
        self.plugin.chain = types.SimpleNamespace(
            recognize_media=Mock(return_value=media)
        )
        self.plugin.downloadchain = types.SimpleNamespace(
            get_no_exists_info=Mock(return_value=(False, {}))
        )
        self.plugin.subscribechain = types.SimpleNamespace(
            exists=Mock(side_effect=[False, True]),
            add=Mock(return_value=(123, "ok")),
            finish_subscribe_or_not=Mock(),
        )
        _SubscribeOper.subscriptions.clear()

        first = self.plugin._process_subscription_item(
            _movie_item(),
            "watchlist:movie",
            "movie:1",
        )
        second = self.plugin._process_subscription_item(
            _movie_item(),
            "custom_list:7",
            "movie:1",
        )

        self.assertTrue(first)
        self.assertTrue(second)
        self.plugin.subscribechain.add.assert_called_once()
        history_sources = {
            item["source"] for item in self.store.data["history"].values()
        }
        self.assertEqual({"watchlist:movie", "custom_list:7"}, history_sources)

    def test_selected_custom_list_sync_uses_movies_and_shows_only(self):
        self.store.data[self.plugin._selected_lists_key] = [7]
        self.store.data[self.plugin._account_key] = {
            "data": {"uuid": "account-uuid", "slug": "tester"}
        }
        self.store.data[
            self.plugin._custom_list_catalog_data_key("account-uuid")
        ] = {
            "data": [
                {
                    "list_id": 7,
                    "updated_at": "now",
                    "sort_by": "rank",
                    "sort_how": "asc",
                }
            ]
        }
        self.plugin._fetch_all_pages = Mock(return_value=[])
        state = {"account_uuid": "account-uuid", "sources": {}}
        summary = {
            "sources": {},
            "skipped": [],
            "failed_sources": [],
            "errors": [],
        }

        self.plugin._sync_custom_list_sources(
            state=state,
            activities={"lists": {"updated_at": "now"}},
            force=True,
            only_list_id=7,
            summary=summary,
        )

        self.assertEqual(
            "/users/tester/lists/7/items/movie,show",
            self.plugin._fetch_all_pages.call_args.args[0],
        )

    def test_custom_list_selection_is_add_only_and_starts_new_sync(self):
        self.store.data[self.plugin._custom_list_catalog_data_key()] = {
            "data": [{"list_id": 7, "name": "Mine"}]
        }
        started = []

        class _Thread:
            def __init__(thread_self, target, kwargs, daemon):
                thread_self.target = target
                thread_self.kwargs = kwargs

            def start(thread_self):
                started.append(thread_self.kwargs)

        original_thread = self.module.Thread
        self.module.Thread = _Thread
        try:
            selected = self.plugin.api_select_custom_list(
                self.module.CustomListSelectionRequest(list_id=7, selected=True)
            )
            deselected = self.plugin.api_select_custom_list(
                self.module.CustomListSelectionRequest(list_id=7, selected=False)
            )
        finally:
            self.module.Thread = original_thread

        self.assertTrue(selected.success)
        self.assertTrue(deselected.success)
        self.assertEqual([{"force": True, "only_list_id": 7}], started)
        self.assertEqual([], self.store.data[self.plugin._selected_lists_key])
        self.assertIn("不会删除", deselected.message)

    def test_four_mcp_tools_and_admin_declarations(self):
        tools = self.plugin.get_agent_tools()
        names = [tool.name for tool in tools]

        self.assertEqual(
            [
                "get_trakt_lists",
                "get_trakt_personal_data",
                "get_trakt_custom_lists",
                "get_trakt_calendar",
            ],
            names,
        )
        self.assertTrue(self.module.GetTraktPersonalDataTool.require_admin)
        self.assertTrue(self.module.GetTraktCustomListsTool.require_admin)
        self.assertFalse(
            hasattr(self.module.GetTraktListsTool, "require_admin")
            and self.module.GetTraktListsTool.require_admin
        )
        self.assertFalse(
            hasattr(self.module.GetTraktCalendarTool, "require_admin")
            and self.module.GetTraktCalendarTool.require_admin
        )
        source = (
            Path(__file__).parents[1]
            / "plugins.v2"
            / "traktsync"
            / "__init__.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("get_trakt_recommendations", source)

    def test_mcp_schemas_expose_expected_parameters(self):
        list_fields = self.module.GetTraktListsInput.__annotations__
        personal_fields = self.module.GetTraktPersonalDataInput.__annotations__
        custom_fields = self.module.GetTraktCustomListsInput.__annotations__
        calendar_fields = self.module.GetTraktCalendarInput.__annotations__

        self.assertEqual(
            {
                "explanation",
                "category",
                "media_type",
                "period",
                "page",
                "limit",
                "force_refresh",
            },
            set(list_fields),
        )
        self.assertTrue(
            {
                "data_type",
                "media_type",
                "page",
                "limit",
                "start_at",
                "end_at",
                "force_refresh",
            }.issubset(personal_fields)
        )
        self.assertTrue(
            {"list_id", "media_type", "page", "limit", "force_refresh"}.issubset(
                custom_fields
            )
        )
        self.assertEqual(
            {
                "explanation",
                "target",
                "calendar_type",
                "start_date",
                "days",
                "page",
                "limit",
                "force_refresh",
            },
            set(calendar_fields),
        )

    def test_recommended_runtime_admin_gate_and_public_access(self):
        backend = types.SimpleNamespace(
            get_trakt_lists=Mock(
                return_value={"success": True, "meta": {}, "data": []}
            )
        )
        tool = self.module.GetTraktListsTool(
            session_id="session",
            user_id="user",
            plugin_instance=backend,
        )

        denied = json.loads(
            asyncio.run(tool.run(category="Recommended", media_type="movies"))
        )
        public = json.loads(
            asyncio.run(tool.run(category="popular", media_type="movies"))
        )

        self.assertFalse(denied["success"])
        self.assertEqual("admin_required", denied["meta"]["error"]["code"])
        self.assertTrue(public["success"])
        backend.get_trakt_lists.assert_called_once()

    def test_calendar_runtime_admin_gate_and_public_access(self):
        backend = types.SimpleNamespace(
            get_trakt_calendar=Mock(
                return_value={"success": True, "meta": {}, "data": []}
            )
        )
        tool = self.module.GetTraktCalendarTool(
            session_id="session",
            user_id="user",
            plugin_instance=backend,
        )

        denied = json.loads(asyncio.run(tool.run(target="my")))
        public = json.loads(
            asyncio.run(tool.run(target="all", calendar_type="movies"))
        )

        self.assertFalse(denied["success"])
        self.assertEqual("admin_required", denied["meta"]["error"]["code"])
        self.assertTrue(public["success"])
        backend.get_trakt_calendar.assert_called_once()

    def test_sensitive_fields_are_removed_recursively(self):
        payload = {
            "access_token": "access",
            "refresh_token": "refresh",
            "client_secret": "client-secret",
            "email": "mail@example.com",
            "nested": {
                "authorization": "Bearer secret",
                "name": "safe",
            },
        }

        sanitized = self.plugin._sanitize_payload(payload)
        serialized = json.dumps(sanitized)

        self.assertEqual({"nested": {"name": "safe"}}, sanitized)
        for secret in (
            "access",
            "refresh",
            "client-secret",
            "mail@example.com",
            "Bearer secret",
        ):
            self.assertNotIn(secret, serialized)

    def test_safe_error_redacts_bearer_and_oauth_fields(self):
        safe = self.plugin._safe_error(
            "failed Authorization: Bearer abc.def access_token=one "
            "refresh_token:two client_secret=three"
        )

        self.assertNotIn("abc.def", safe)
        self.assertNotIn("one", safe)
        self.assertNotIn("two", safe)
        self.assertNotIn("three", safe)

    def test_page_reads_local_data_and_exposes_bearer_actions(self):
        self.store.data[self.plugin._account_key] = {
            "data": {
                "uuid": "account-uuid",
                "username": "tester",
                "slug": "tester",
            }
        }
        self.store.data[self.plugin._sync_status_key] = {
            "state": "success",
            "message": "done",
            "finished_at": "now",
        }
        normalized_calendar_item = self.plugin._normalize_calendar_item(
            _calendar_show_item(first_aired="2026-08-01T12:00:00Z"),
            "shows",
        )
        normalized_calendar_item.update(
            {
                "moviepilot_state": "subscribed",
                "moviepilot_state_label": "已订阅",
            }
        )
        self.store.data[
            self.plugin._calendar_page_data_key("account-uuid")
        ] = {
            "fetched_at": "2026-08-01T00:00:00Z",
            "start_date": "2026-08-01",
            "days": 14,
            "data": [normalized_calendar_item],
        }
        self.store.data[
            self.plugin._calendar_status_data_key("account-uuid")
        ] = {
            "state": "success",
            "message": "日历刷新完成",
            "finished_at": "now",
        }
        self.store.data[self.plugin._selected_lists_key] = [7]
        self.store.data[
            self.plugin._custom_list_catalog_data_key("account-uuid")
        ] = {
            "data": [
                {
                    "list_id": 7,
                    "name": "Mine",
                    "item_count": 2,
                    "privacy": "private",
                }
            ]
        }
        self.store.data["history"] = {
            "entry": {
                "title": "Movie",
                "action": "subscribe",
                "source": "custom_list:7",
                "time": "2026-01-01 00:00:00",
            }
        }
        self.plugin._trakt_request = Mock(
            side_effect=AssertionError("详情页不得请求 Trakt")
        )

        page = self.plugin.get_page()
        serialized = json.dumps(page, ensure_ascii=False)

        self.plugin._trakt_request.assert_not_called()
        self.assertIn("plugin/TraktSync/sync_now", serialized)
        self.assertIn("plugin/TraktSync/cache/refresh", serialized)
        self.assertIn("plugin/TraktSync/calendar/refresh", serialized)
        self.assertIn("plugin/TraktSync/custom_lists/refresh", serialized)
        self.assertIn("plugin/TraktSync/custom_lists/select", serialized)
        self.assertIn("plugin/TraktSync/history/delete", serialized)
        self.assertIn("个人剧集日历", serialized)
        self.assertIn("S01E01", serialized)
        self.assertIn("已订阅", serialized)
        self.assertNotIn("do-not-expose", serialized)
        self.assertNotIn("access_token", serialized)

    def test_page_api_actions_use_bearer_auth(self):
        apis = self.plugin.get_api()
        expected = {
            "/sync_now",
            "/cache/refresh",
            "/calendar/refresh",
            "/custom_lists/refresh",
            "/custom_lists/select",
        }
        by_path = {item["path"]: item for item in apis}

        self.assertTrue(expected.issubset(by_path))
        for path in expected:
            self.assertEqual("bear", by_path[path]["auth"])
            self.assertEqual(["POST"], by_path[path]["methods"])

    def test_form_has_new_chart_switches_off_by_default(self):
        _, defaults = self.plugin.get_form()
        for key in (
            "enable_watched_movies",
            "enable_watched_shows",
            "enable_collected_movies",
            "enable_collected_shows",
            "enable_boxoffice_movies",
        ):
            self.assertIn(key, defaults)
            self.assertFalse(defaults[key])

    def test_registry_readme_and_plugin_versions_match(self):
        root = Path(__file__).parents[1]
        package = json.loads((root / "package.v2.json").read_text(encoding="utf-8"))
        readme = (root / "README.md").read_text(encoding="utf-8")
        plugin_readme = (
            root / "plugins.v2" / "traktsync" / "README.md"
        ).read_text(encoding="utf-8")

        self.assertEqual("0.6.0", self.module.TraktSync.plugin_version)
        self.assertEqual(
            self.module.TraktSync.plugin_version,
            package["TraktSync"]["version"],
        )
        self.assertIn("Trakt WatchList 同步 `v0.6.0`", readme)
        self.assertIn("get_trakt_lists", plugin_readme)
        self.assertIn("get_trakt_calendar", plugin_readme)


if __name__ == "__main__":
    unittest.main()
