import base64
import copy
import hashlib
import importlib.util
import itertools
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path


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


class _HTTPException(Exception):
    def __init__(self, status_code, detail=None, headers=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}


class _Response:
    def __init__(self, content=None, status_code=200, headers=None, **kwargs):
        self.status_code = status_code
        self.headers = headers or {}
        if isinstance(content, bytes):
            self.body = content
        elif content is None:
            self.body = b""
        else:
            self.body = str(content).encode("utf-8")


class _JSONResponse(_Response):
    def __init__(self, content, status_code=200, headers=None, **kwargs):
        super().__init__(
            json.dumps(content, ensure_ascii=False).encode("utf-8"),
            status_code=status_code,
            headers=headers,
        )


class _RedirectResponse(_Response):
    def __init__(self, url, status_code=307, headers=None, **kwargs):
        super().__init__(b"", status_code=status_code, headers=headers)
        self.url = url


class _PluginBase:
    def __init__(self, *args, **kwargs):
        pass


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _Settings:
    API_V1_STR = "/api/v1"
    APP_DOMAIN = "https://mp.example.com"
    PORT = 3001
    API_TOKEN = "internal-api-token"
    RESOURCE_SECRET_KEY = "resource-secret"
    SECRET_KEY = "secret"

    @staticmethod
    def MP_DOMAIN(path):
        return f"https://mp.example.com/{path.lstrip('/')}"


def _load_plugin_module_with_stubs():
    _install_module("httpx")
    _install_module("jwt", decode=lambda *args, **kwargs: {})
    _install_module("fastapi", HTTPException=_HTTPException, Request=object)
    _install_module(
        "fastapi.responses",
        HTMLResponse=_Response,
        JSONResponse=_JSONResponse,
        RedirectResponse=_RedirectResponse,
        Response=_Response,
    )
    _install_module("app")
    _install_module("app.core")
    _install_module("app.core.config", settings=_Settings())
    _install_module("app.log", logger=_Logger())
    _install_module("app.plugins", _PluginBase=_PluginBase)

    plugin_path = (
        Path(__file__).parents[1]
        / "plugins.v2"
        / "moviepilotmcp"
        / "__init__.py"
    )
    module_name = "moviepilotmcp_plugin_under_test"
    spec = importlib.util.spec_from_file_location(module_name, plugin_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
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


class _InMemoryPluginData:
    def __init__(self, initial=None, delay=0):
        self._data = {"oauth_store": copy.deepcopy(initial or {})}
        self._lock = threading.Lock()
        self._delay = delay

    def attach(self, plugin):
        def get_data(key=None):
            with self._lock:
                value = copy.deepcopy(self._data.get(key))
            if self._delay:
                time.sleep(self._delay)
            return value

        def save_data(key, value):
            if self._delay:
                time.sleep(self._delay)
            with self._lock:
                self._data[key] = copy.deepcopy(value)

        plugin.get_data = get_data
        plugin.save_data = save_data

    def oauth_store(self):
        with self._lock:
            return copy.deepcopy(self._data["oauth_store"])


class MoviePilotMCPOAuthTest(unittest.TestCase):
    NOW = 2_000_000_000

    @classmethod
    def setUpClass(cls):
        cls.module = _load_plugin_module()

    def _plugin(self, store=None, delay=0):
        plugin = self.module.MoviePilotMCP()
        plugin._enabled = True
        plugin._enable_write_tools = True
        plugin._now = lambda: self.NOW
        counter = itertools.count(1)
        token_lock = threading.Lock()

        def generate_token():
            with token_lock:
                return f"token-{next(counter)}"

        plugin._generate_token = generate_token
        storage = _InMemoryPluginData(store, delay=delay)
        storage.attach(plugin)
        return plugin, storage

    @staticmethod
    def _empty_store():
        return {
            "clients": {},
            "codes": {},
            "access_tokens": {},
            "refresh_tokens": {},
            "admin_sessions": {},
        }

    def _refresh_info(self, plugin, expires_at=None):
        return {
            "client_id": "chatgpt-client",
            "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
            "subject": "1",
            "username": "admin",
            "scopes": ["moviepilot.mcp.read"],
            "requested_scope": "moviepilot.mcp.read",
            "resource": plugin._build_endpoint_url(),
            "expires_at": expires_at or self.NOW + 3600,
        }

    def test_authorize_resource_is_bound_and_mismatch_is_rejected(self):
        plugin, _ = self._plugin(self._empty_store())
        plugin._client_allows_redirect_uri = lambda *args: True
        params = {
            "response_type": "code",
            "client_id": "chatgpt-client",
            "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
            "scope": "moviepilot.mcp.read",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
            "resource": plugin._build_endpoint_url(),
        }

        request = plugin._parse_authorize_request(params)
        self.assertEqual(request.resource, plugin._build_endpoint_url())

        with self.assertRaisesRegex(ValueError, "resource"):
            plugin._parse_authorize_request(
                {**params, "resource": "https://other.example.com/mcp"}
            )

    def test_refresh_rotates_token_and_restarts_180_day_idle_ttl(self):
        plugin, _ = self._plugin(self._empty_store())
        store = self._empty_store()
        store["refresh_tokens"]["old-refresh"] = self._refresh_info(plugin)
        storage = _InMemoryPluginData(store)
        storage.attach(plugin)

        response = plugin._handle_refresh_token_grant(
            {
                "grant_type": "refresh_token",
                "client_id": "chatgpt-client",
                "refresh_token": "old-refresh",
                "resource": plugin._build_endpoint_url(),
            }
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        persisted = storage.oauth_store()
        self.assertNotIn("old-refresh", persisted["refresh_tokens"])
        self.assertIn(payload["access_token"], persisted["access_tokens"])
        self.assertIn(payload["refresh_token"], persisted["refresh_tokens"])
        refresh_info = persisted["refresh_tokens"][payload["refresh_token"]]
        self.assertEqual(
            refresh_info["expires_at"],
            self.NOW + 180 * 24 * 3600,
        )
        self.assertEqual(refresh_info["resource"], plugin._build_endpoint_url())

    def test_authorization_code_exchange_persists_resource_binding(self):
        plugin, _ = self._plugin(self._empty_store())
        verifier = "chatgpt-pkce-verifier"
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("utf-8")).digest()
        ).decode("utf-8").rstrip("=")
        store = self._empty_store()
        store["codes"]["authorization-code"] = {
            "client_id": "chatgpt-client",
            "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
            "subject": "1",
            "username": "admin",
            "scopes": ["moviepilot.mcp.read"],
            "requested_scope": "moviepilot.mcp.read",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": plugin._build_endpoint_url(),
            "expires_at": self.NOW + 600,
        }
        storage = _InMemoryPluginData(store)
        storage.attach(plugin)

        response = plugin._handle_authorization_code_grant(
            {
                "code": "authorization-code",
                "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
                "client_id": "chatgpt-client",
                "code_verifier": verifier,
                "resource": plugin._build_endpoint_url(),
            }
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        persisted = storage.oauth_store()
        self.assertNotIn("authorization-code", persisted["codes"])
        access_info = persisted["access_tokens"][payload["access_token"]]
        self.assertEqual(access_info["resource"], plugin._build_endpoint_url())

    def test_refresh_rejects_mismatched_resource(self):
        plugin, _ = self._plugin(self._empty_store())
        store = self._empty_store()
        store["refresh_tokens"]["old-refresh"] = self._refresh_info(plugin)
        storage = _InMemoryPluginData(store)
        storage.attach(plugin)

        response = plugin._handle_refresh_token_grant(
            {
                "client_id": "chatgpt-client",
                "refresh_token": "old-refresh",
                "resource": "https://other.example.com/mcp",
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"], "invalid_target")
        self.assertFalse(storage.oauth_store()["access_tokens"])

    def test_concurrent_refresh_keeps_the_successful_rotated_pair(self):
        plugin, _ = self._plugin(self._empty_store())
        store = self._empty_store()
        store["refresh_tokens"]["old-refresh"] = self._refresh_info(plugin)
        storage = _InMemoryPluginData(store, delay=0.01)
        storage.attach(plugin)
        start = threading.Barrier(3)
        responses = []

        def refresh():
            start.wait()
            responses.append(
                plugin._handle_refresh_token_grant(
                    {
                        "client_id": "chatgpt-client",
                        "refresh_token": "old-refresh",
                        "resource": plugin._build_endpoint_url(),
                    }
                )
            )

        threads = [threading.Thread(target=refresh) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 400])
        success = next(response for response in responses if response.status_code == 200)
        payload = json.loads(success.body)
        persisted = storage.oauth_store()
        self.assertIn(payload["access_token"], persisted["access_tokens"])
        self.assertIn(payload["refresh_token"], persisted["refresh_tokens"])

    def test_oauth_audit_result_reports_only_error_code(self):
        plugin, _ = self._plugin(self._empty_store())
        response = self.module.JSONResponse(
            {
                "error": "invalid_grant",
                "error_description": "refresh token secret must not be logged",
            },
            status_code=400,
        )

        self.assertEqual(
            plugin._oauth_response_result(response),
            "rejected_invalid_grant",
        )


if __name__ == "__main__":
    unittest.main()
