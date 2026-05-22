import json
import base64
import hashlib
import html
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import jwt
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


@dataclass
class OAuthAuthorizeRequest:
    """
    OAuth 授权请求
    """

    client_id: str
    redirect_uri: str
    state: Optional[str]
    scopes: List[str]
    code_challenge: str
    code_challenge_method: str
    raw_scope: Optional[str] = None


class MoviePilotMCP(_PluginBase):
    """
    MoviePilot ChatGPT MCP 薄包装插件
    """

    plugin_name = "MoviePilot MCP Server"
    plugin_desc = "MoviePilot 内置 Agent 工具的 MCP/OpenAPI 双协议对外暴露层，支持 OAuth 2.0 + PKCE 鉴权"
    plugin_icon = "https://raw.githubusercontent.com/cyt-666/MoviePilot-Plugins/main/icons/moviepilotmcp.svg"
    plugin_version = "0.7.0"
    plugin_author = "cyt-666"
    author_url = "https://github.com/cyt-666/MoviePilot-Plugins"
    plugin_config_prefix = "moviepilotmcp_"
    plugin_order = 2
    auth_level = 1

    _protocol_version = "2024-11-05"
    _server_name = "moviepilot-chatgpt-wrapper"
    _oauth_scopes = ("moviepilot.mcp.read", "moviepilot.mcp.write")
    _oauth_code_ttl = 600
    _oauth_access_token_ttl = 3600
    _oauth_refresh_token_ttl = 30 * 24 * 3600
    # 插件独立管理员会话（仅服务于授权页），避免误用 MoviePilot 的 resource cookie
    # 作为登录态导致退出登录后依然能批准授权。
    _admin_session_cookie_name = "mp_mcp_admin_session"
    # 仅用于「批准授权」页的极短期会话；不影响已经拿到 access_token 的 client。
    _admin_session_ttl = 2 * 60
    _agent_manager_candidates = [
        ("app.agent.tools.manager", "MoviePilotToolsManager"),
    ]

    # 内置 MCP 中属于写操作的工具名，用于 _enable_write_tools 开关
    _write_tool_names: frozenset = frozenset({
        "add_subscribe", "update_subscribe", "delete_subscribe",
        "add_download", "modify_download", "delete_download", "delete_download_history",
        "delete_transfer_history", "run_scheduler", "search_subscribe",
        "run_workflow", "transfer_file", "scrape_metadata",
        "update_site", "update_site_cookie", "update_custom_identifiers",
        "send_message", "send_voice_message", "send_local_file",
        "run_slash_command",
    })

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._allow_legacy_token = False
        self._mcp_token = ""
        self._actor_name = "ChatGPT MCP"
        self._enable_write_tools = True
        self._mcp_proxy_timeout = 600

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._enable_write_tools = bool(config.get("enable_write_tools", True))
        self._allow_legacy_token = bool(config.get("allow_legacy_token", False))
        self._mcp_token = (config.get("mcp_token") or "").strip()
        self._actor_name = (config.get("actor_name") or "ChatGPT MCP").strip() or "ChatGPT MCP"
        self._mcp_proxy_timeout = self._parse_proxy_timeout(config.get("mcp_proxy_timeout", 600))

        if not self._mcp_token:
            self._mcp_token = self._generate_token()

        self.update_config(
            {
                "enabled": self._enabled,
                "mcp_token": self._mcp_token,
                "allow_legacy_token": self._allow_legacy_token,
                "actor_name": self._actor_name,
                "enable_write_tools": self._enable_write_tools,
                "mcp_proxy_timeout": self._mcp_proxy_timeout,
            }
        )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        pass

    @staticmethod
    def _parse_proxy_timeout(value: Any) -> int:
        try:
            timeout = int(float(value))
        except (TypeError, ValueError):
            timeout = 600
        return max(30, min(timeout, 3600))

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/mcp",
                "endpoint": self.handle_mcp,
                "methods": ["POST"],
                "summary": "MoviePilot MCP endpoint",
                "description": "ChatGPT App 可连接的受控 MCP JSON-RPC 接口",
                "allow_anonymous": True,
            },
            {
                "path": "/.well-known/oauth-protected-resource",
                "endpoint": self.handle_protected_resource_metadata,
                "methods": ["GET"],
                "summary": "MoviePilot MCP OAuth protected resource metadata",
                "description": "供 OAuth 客户端发现 MoviePilot MCP 资源元数据",
                "allow_anonymous": True,
            },
            {
                "path": "/oauth/.well-known/oauth-authorization-server",
                "endpoint": self.handle_authorization_server_metadata,
                "methods": ["GET"],
                "summary": "MoviePilot MCP OAuth authorization server metadata",
                "description": "供 OAuth 客户端发现 MoviePilot MCP 授权服务器元数据",
                "allow_anonymous": True,
            },
            {
                "path": "/.well-known/oauth-authorization-server",
                "endpoint": self.handle_authorization_server_metadata,
                "methods": ["GET"],
                "summary": "MoviePilot MCP OAuth authorization server metadata",
                "description": "供 OAuth 客户端发现 MoviePilot MCP 授权服务器元数据（兼容路径）",
                "allow_anonymous": True,
            },
            {
                # VS Code MCP 客户端在 AS 元数据发现的 RFC 8414 path-insertion
                # 和 OIDC path-insertion 两种方式都落在 {origin}/.well-known/... 根路径，
                # 而 MoviePilot 插件只能托管在 /api/v1/plugin/<id>/ 前缀下，
                # 唯一能被 VS Code 命中的是 OIDC path-addition:
                # {issuer}/.well-known/openid-configuration
                # 因此额外暴露一个 OIDC Discovery 端点，返回与 AS 元数据一致的内容。
                "path": "/.well-known/openid-configuration",
                "endpoint": self.handle_authorization_server_metadata,
                "methods": ["GET"],
                "summary": "MoviePilot MCP OpenID Connect discovery",
                "description": "供 VS Code 等 MCP 客户端通过 OIDC path-addition 发现授权服务器元数据",
                "allow_anonymous": True,
            },
            {
                "path": "/oauth/authorize",
                "endpoint": self.handle_authorize,
                "methods": ["GET", "POST"],
                "summary": "MoviePilot MCP OAuth authorize endpoint",
                "description": "Authorization Code + PKCE 授权入口",
                "allow_anonymous": True,
            },
            {
                "path": "/oauth/login",
                "endpoint": self.handle_admin_login,
                "methods": ["POST"],
                "summary": "MoviePilot MCP OAuth admin login",
                "description": "授权页内管理员登录（校验 MoviePilot 超级管理员凭证后颁发插件会话 Cookie）",
                "allow_anonymous": True,
            },
            {
                "path": "/oauth/token",
                "endpoint": self.handle_token,
                "methods": ["POST"],
                "summary": "MoviePilot MCP OAuth token endpoint",
                "description": "Authorization Code / Refresh Token 令牌交换入口",
                "allow_anonymous": True,
            },
            {
                "path": "/oauth/register",
                "endpoint": self.handle_client_registration,
                "methods": ["POST"],
                "summary": "MoviePilot MCP OAuth dynamic client registration endpoint",
                "description": "供 VS Code / ChatGPT 等 OAuth 客户端自动注册 public client",
                "allow_anonymous": True,
            },
            # OpenAPI REST 端点
            {
                "path": "/openapi/tools",
                "endpoint": self.handle_openapi_list_tools,
                "methods": ["GET"],
                "summary": "列出所有可用工具（OpenAPI REST）",
                "description": "以 REST 格式返回所有可用 MCP 工具列表",
                "allow_anonymous": True,
            },
            {
                "path": "/openapi/tools/{tool_name}",
                "endpoint": self.handle_openapi_call_tool,
                "methods": ["POST"],
                "summary": "调用指定工具（OpenAPI REST）",
                "description": "通过 REST 接口调用 MCP 工具，请求体为工具参数 JSON",
                "allow_anonymous": True,
            },
            {
                "path": "/openapi.json",
                "endpoint": self.handle_openapi_spec,
                "methods": ["GET"],
                "summary": "OpenAPI 3.0 Spec（只读工具）",
                "description": "动态生成的 OpenAPI 3.0 规范文档，仅包含只读工具，适合 ChatGPT Actions 等受限场景",
                "allow_anonymous": True,
            },
            {
                "path": "/openapi.write.json",
                "endpoint": self.handle_openapi_write_spec,
                "methods": ["GET"],
                "summary": "OpenAPI 3.0 Spec（写操作工具）",
                "description": "动态生成的 OpenAPI 3.0 规范文档，仅包含写操作工具",
                "allow_anonymous": True,
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        endpoint_url = self._build_endpoint_url()
        authorize_url = self._build_authorization_url()
        token_url = self._build_token_url()
        metadata_url = self._build_resource_metadata_url()
        openapi_spec_url = self._build_openapi_spec_url()
        openapi_write_spec_url = self._build_openapi_write_spec_url()
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "该插件将 MoviePilot 内置 Agent 工具通过 MCP 和 OpenAPI 双协议对外暴露：对内复用内置 Agent tools，对外提供 MCP JSON-RPC 和 REST/OpenAPI 两种接入方式，并通过 OAuth 2.0 Authorization Code + PKCE 完成授权。",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用外部 MCP 包装层",
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
                                            "model": "mcp_proxy_timeout",
                                            "label": "内部 MCP 转发超时（秒）",
                                            "placeholder": "600",
                                            "type": "number",
                                            "min": 30,
                                            "max": 3600,
                                            "hint": "用于等待 MoviePilot 内置工具执行完成；搜索种子等慢工具建议保持 600 秒或更高。",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enable_write_tools",
                                            "label": "启用写操作工具",
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
                                            "model": "actor_name",
                                            "label": "写入显示名称",
                                            "placeholder": "例如：ChatGPT MCP",
                                            "hint": "用于订阅等业务记录中的显示名称，避免在 MoviePilot 页面中显示成纯数字用户标识。",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "allow_legacy_token",
                                            "label": "启用兼容静态 Token",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "show_mcp_token",
                                            "label": "显示兼容 Token 明文",
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
                                            "model": "mcp_token",
                                            "label": "兼容静态 Token",
                                            "placeholder": "留空时自动生成",
                                            "type": "{{ show_mcp_token ? 'text' : 'password' }}",
                                            "hint": "仅供不支持 OAuth 的私有调试客户端备用，不再推荐用于正式 ChatGPT Connector。",
                                            "persistentHint": True,
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
                                            "model": "oauth_scope",
                                            "label": "OAuth Scope",
                                            "readonly": True,
                                            "variant": "outlined",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "endpoint_url",
                                            "label": "MCP Endpoint URL",
                                            "rows": 2,
                                            "readonly": True,
                                            "variant": "outlined",
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
                                            "model": "authorize_url",
                                            "label": "OAuth Authorization URL",
                                            "readonly": True,
                                            "variant": "outlined",
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
                                            "model": "token_url",
                                            "label": "OAuth Token URL",
                                            "readonly": True,
                                            "variant": "outlined",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": "推荐使用 OAuth 2.0 Authorization Code + PKCE；仅支持 tools，不开放 resources/prompts；不复用 MoviePilot 全局 API Key；已移除 URL query token 作为推荐接入方式。",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "resource_metadata_url",
                                            "label": "Protected Resource Metadata URL",
                                            "readonly": True,
                                            "variant": "outlined",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "openapi_spec_url",
                                            "label": "OpenAPI Spec URL（只读工具，推荐用于 ChatGPT Actions）",
                                            "readonly": True,
                                            "variant": "outlined",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "openapi_write_spec_url",
                                            "label": "OpenAPI Write Spec URL（写操作工具）",
                                            "readonly": True,
                                            "variant": "outlined",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {
            "enabled": False,
            "mcp_token": self._generate_token(),
            "allow_legacy_token": False,
            "actor_name": "ChatGPT MCP",
            "enable_write_tools": True,
            "mcp_proxy_timeout": 600,
            "endpoint_url": endpoint_url,
            "authorize_url": authorize_url,
            "token_url": token_url,
            "resource_metadata_url": metadata_url,
            "openapi_spec_url": openapi_spec_url,
            "openapi_write_spec_url": openapi_write_spec_url,
            "oauth_scope": self._format_scope(self._default_scopes()),
            "show_mcp_token": False,
        }

    def get_page(self) -> List[dict]:
        endpoint_url = self._build_endpoint_url()
        authorize_url = self._build_authorization_url()
        token_url = self._build_token_url()
        openapi_spec_url = self._build_openapi_spec_url()
        openapi_write_spec_url = self._build_openapi_write_spec_url()
        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {"component": "VCardTitle", "text": "MoviePilot MCP / OpenAPI 包装层"},
                    {"component": "VCardText", "text": f"状态：{'已启用' if self._enabled else '未启用'}"},
                    {"component": "VCardText", "text": f"MCP Endpoint：{endpoint_url}"},
                    {"component": "VCardText", "text": f"OAuth Authorization：{authorize_url}"},
                    {"component": "VCardText", "text": f"OAuth Token：{token_url}"},
                    {"component": "VCardText", "text": f"OpenAPI Spec（只读）：{openapi_spec_url}"},
                    {"component": "VCardText", "text": f"OpenAPI Spec（写操作）：{openapi_write_spec_url}"},
                    {"component": "VCardText", "text": f"写操作工具：{'已开启' if self._enable_write_tools else '已关闭'}"},
                    {"component": "VCardText", "text": f"写入显示名称：{self._actor_name}"},
                    {"component": "VCardText", "text": f"内部 MCP 转发超时：{self._mcp_proxy_timeout} 秒"},
                    {"component": "VCardText", "text": f"兼容静态 Token：{'已开启' if self._allow_legacy_token else '已关闭'}"},
                ],
            }
        ]

    async def handle_mcp(self, request: Request):
        """
        MCP JSON-RPC 代理端点：完成 OAuth Bearer 认证后，将请求原样转发至
        MoviePilot 内置 /api/v1/mcp，由内置 MCP Server 统一处理工具调度。
        若关闭了写操作开关，则会在转发前拦截写工具调用，并在 tools/list 响应中过滤写工具。
        """
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")

        # OAuth Bearer token 验证
        self._authorize_mcp_request(request)

        # 读取原始请求体（保持字节，避免 re-serialization 丢精度）
        body_bytes = await request.body()
        try:
            payload = json.loads(body_bytes)
        except Exception as err:
            logger.error(f"MoviePilot MCP 请求体解析失败：{err}")
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        # 统一为列表以支持批量请求
        messages = payload if isinstance(payload, list) else [payload]

        # 全为通知（无 id）→ 直接 202，无需转发
        if all(not isinstance(m, dict) or m.get("id") is None for m in messages):
            return Response(status_code=202)

        # 写操作拦截：关闭写工具时提前拒绝
        if not self._enable_write_tools:
            for m in messages:
                if isinstance(m, dict) and m.get("method") == "tools/call":
                    tool_name = (m.get("params") or {}).get("name", "")
                    if tool_name in self._write_tool_names:
                        return JSONResponse({
                            "jsonrpc": "2.0",
                            "id": m.get("id"),
                            "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
                        })

        # 转发至内置 MCP，附带 MoviePilot API Token
        internal_url = f"http://127.0.0.1:{settings.PORT}{settings.API_V1_STR}/mcp"
        fwd_headers = {"Content-Type": "application/json"}
        if settings.API_TOKEN:
            fwd_headers["X-API-KEY"] = settings.API_TOKEN

        try:
            async with httpx.AsyncClient(timeout=float(self._mcp_proxy_timeout)) as client:
                resp = await client.post(internal_url, content=body_bytes, headers=fwd_headers)
        except httpx.TimeoutException as err:
            logger.error(
                f"MoviePilot MCP 内部转发超时：已等待 {self._mcp_proxy_timeout} 秒，"
                f"可在插件配置中继续调大内部 MCP 转发超时。错误：{err}",
                exc_info=True,
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": f"Internal proxy timeout after {self._mcp_proxy_timeout}s",
                    },
                },
                status_code=504,
            )
        except Exception as err:
            logger.error(f"MoviePilot MCP 内部转发失败：{err}", exc_info=True)
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Internal proxy error: {err}"}},
                status_code=502,
            )

        # 内置 MCP 对通知返回 204，统一转换为 MCP Streamable HTTP 规范的 202
        if resp.status_code == 204:
            return Response(status_code=202)

        # 若关闭写工具，从 tools/list 响应里过滤写工具
        if not self._enable_write_tools and resp.status_code == 200:
            try:
                return JSONResponse(self._filter_write_tools(resp.json()))
            except Exception:
                pass  # 解析失败则原样透传

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    def _filter_write_tools(self, resp_data: Any) -> Any:
        """从 tools/list 响应中过滤掉写操作工具（仅在关闭写操作时调用）。"""
        if not isinstance(resp_data, dict):
            return resp_data
        result = resp_data.get("result")
        if not isinstance(result, dict):
            return resp_data
        tools = result.get("tools")
        if not isinstance(tools, list):
            return resp_data
        result["tools"] = [t for t in tools if t.get("name") not in self._write_tool_names]
        return resp_data

    # ==================== OpenAPI REST 端点 ====================

    async def _forward_to_internal_mcp(
        self, body_bytes: bytes, timeout: Optional[float] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        将 JSON-RPC 请求转发至内置 MCP 端点，返回 (响应JSON, 错误信息)。
        成功时 error 为 None；失败时 result 为 None。
        """
        internal_url = f"http://127.0.0.1:{settings.PORT}{settings.API_V1_STR}/mcp"
        fwd_headers = {"Content-Type": "application/json"}
        if settings.API_TOKEN:
            fwd_headers["X-API-KEY"] = settings.API_TOKEN

        try:
            async with httpx.AsyncClient(timeout=timeout or float(self._mcp_proxy_timeout)) as client:
                resp = await client.post(internal_url, content=body_bytes, headers=fwd_headers)
        except httpx.TimeoutException:
            return None, f"内部 MCP 转发超时（{timeout or self._mcp_proxy_timeout} 秒）"
        except Exception as err:
            return None, f"内部 MCP 转发失败：{err}"

        if resp.status_code == 204:
            return None, "内部 MCP 返回空响应"

        try:
            return resp.json(), None
        except Exception:
            return None, f"内部 MCP 响应解析失败：status={resp.status_code}"

    async def _fetch_mcp_tools(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        通过内部 MCP tools/list 获取可用工具列表，已过滤写工具和隐藏工具。
        返回 (工具列表, 错误信息)。
        """
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        resp_data, error = await self._forward_to_internal_mcp(payload.encode("utf-8"))
        if error:
            return [], error
        if not isinstance(resp_data, dict):
            return [], "内部 MCP 响应格式错误"
        result = resp_data.get("result")
        if not isinstance(result, dict):
            return [], "内部 MCP 响应缺少 result"
        tools = result.get("tools")
        if not isinstance(tools, list):
            return [], "内部 MCP 响应缺少 tools"
        # 过滤写工具
        if not self._enable_write_tools:
            tools = [t for t in tools if t.get("name") not in self._write_tool_names]
        return tools, None

    @staticmethod
    def _strip_explanation_from_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        从 inputSchema 中移除 explanation 字段及其 required 引用，
        使 OpenAPI spec 对 REST 客户端更友好。
        """
        if not isinstance(schema, dict):
            return schema
        schema = dict(schema)
        props = dict(schema.get("properties") or {})
        props.pop("explanation", None)
        schema["properties"] = props
        required = [r for r in (schema.get("required") or []) if r != "explanation"]
        if required:
            schema["required"] = required
        else:
            schema.pop("required", None)
        return schema

    async def handle_openapi_list_tools(self, request: Request):
        """以 REST 格式返回所有可用工具列表。"""
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")
        self._authorize_mcp_request(request)

        tools, error = await self._fetch_mcp_tools()
        if error:
            raise HTTPException(status_code=502, detail=error)

        return JSONResponse([
            {
                "name": t.get("name"),
                "description": t.get("description"),
                "inputSchema": self._strip_explanation_from_schema(t.get("inputSchema") or {}),
            }
            for t in tools
        ])

    async def handle_openapi_call_tool(self, request: Request, tool_name: str):
        """通过 REST 接口调用指定 MCP 工具。"""
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")
        self._authorize_mcp_request(request)

        # 写工具拦截
        if not self._enable_write_tools and tool_name in self._write_tool_names:
            return JSONResponse(
                {"success": False, "error": f"写操作工具 '{tool_name}' 已被禁用"},
                status_code=403,
            )

        # 读取请求体并注入 explanation
        try:
            arguments = await request.json()
        except Exception:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        arguments.setdefault("explanation", "OpenAPI call")

        # 构造 JSON-RPC 请求转发至内置 MCP
        rpc_payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        resp_data, error = await self._forward_to_internal_mcp(rpc_payload.encode("utf-8"))
        if error:
            return JSONResponse({"success": False, "error": error}, status_code=502)

        # 解析 MCP 响应
        result = (resp_data or {}).get("result")
        if not isinstance(result, dict):
            err = (resp_data or {}).get("error")
            return JSONResponse(
                {"success": False, "error": err.get("message", "未知错误") if isinstance(err, dict) else str(err)},
                status_code=500,
            )

        content = result.get("content")
        is_error = result.get("isError", False)
        text = ""
        if isinstance(content, list) and content:
            text = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])

        if is_error:
            return JSONResponse({"success": False, "error": text}, status_code=400)
        return JSONResponse({"success": True, "result": text})

    def _build_openapi_paths(self, tools: List[Dict[str, Any]], plugin_prefix: str) -> Dict[str, Any]:
        """将工具列表转换为 OpenAPI paths 字段。"""
        paths = {}
        for tool in tools:
            tool_name = tool.get("name", "")
            input_schema = self._strip_explanation_from_schema(tool.get("inputSchema") or {})
            path_key = f"{plugin_prefix}/openapi/tools/{tool_name}"
            paths[path_key] = {
                "post": {
                    "operationId": tool_name,
                    "summary": tool.get("description", tool_name),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": input_schema,
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "工具调用成功",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "success": {"type": "boolean"},
                                            "result": {"type": "string"},
                                        },
                                        "required": ["success", "result"],
                                    }
                                }
                            },
                        },
                        "400": {
                            "description": "工具调用失败或返回错误",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "success": {"type": "boolean", "enum": [False]},
                                            "error": {"type": "string"},
                                        },
                                        "required": ["success", "error"],
                                    }
                                }
                            },
                        },
                    },
                    "security": [{"BearerAuth": []}],
                }
            }
        return paths

    def _build_openapi_spec(
        self,
        tools: List[Dict[str, Any]],
        base_url: str,
        title_suffix: str = "",
        description_suffix: str = "",
    ) -> Dict[str, Any]:
        """组装完整的 OpenAPI 3.0.3 规范文档。"""
        plugin_prefix = f"{settings.API_V1_STR}/plugin/{self.__class__.__name__}"
        paths = self._build_openapi_paths(tools, plugin_prefix)
        return {
            "openapi": "3.0.3",
            "info": {
                "title": f"MoviePilot MCP OpenAPI{title_suffix}",
                "description": f"MoviePilot 内置 Agent 工具的 REST 接口，由 MoviePilotMCP 插件动态生成。{description_suffix}",
                "version": self.plugin_version,
            },
            "servers": [{"url": base_url}],
            "paths": paths,
            "components": {
                "securitySchemes": {
                    "BearerAuth": {
                        "type": "http",
                        "scheme": "bearer",
                        "description": "OAuth 2.0 Bearer Token，通过 /oauth/token 端点获取",
                    }
                }
            },
        }

    async def _fetch_and_split_tools(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
        """获取所有工具并按只读/写操作拆分。返回 (readonly_tools, write_tools, error)。"""
        tools, error = await self._fetch_mcp_tools()
        if error:
            return [], [], error
        readonly_tools = [t for t in tools if t.get("name") not in self._write_tool_names]
        write_tools = [t for t in tools if t.get("name") in self._write_tool_names]
        return readonly_tools, write_tools, None

    async def handle_openapi_spec(self, request: Request):
        """动态生成 OpenAPI 3.0.3 规范文档 — 仅包含只读工具（无需认证）。"""
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")

        readonly_tools, _, error = await self._fetch_and_split_tools()
        if error:
            raise HTTPException(status_code=502, detail=error)

        base_url = str(request.base_url).rstrip("/")
        spec = self._build_openapi_spec(readonly_tools, base_url)
        return JSONResponse(spec)

    async def handle_openapi_write_spec(self, request: Request):
        """动态生成 OpenAPI 3.0.3 规范文档 — 仅包含写操作工具（无需认证）。"""
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")
        if not self._enable_write_tools:
            raise HTTPException(status_code=403, detail="写操作工具已关闭，请在插件配置中启用")

        _, write_tools, error = await self._fetch_and_split_tools()
        if error:
            raise HTTPException(status_code=502, detail=error)

        base_url = str(request.base_url).rstrip("/")
        spec = self._build_openapi_spec(
            write_tools,
            base_url,
            title_suffix="（写操作）",
            description_suffix="当前 spec 仅包含写操作工具。",
        )
        return JSONResponse(spec)

    async def handle_protected_resource_metadata(self, _: Request):
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")
        return JSONResponse(
            {
                "resource": self._build_endpoint_url(),
                "authorization_servers": [self._build_issuer_url()],
                "authorization_server_metadata": self._build_authorization_server_metadata_url(),
                "bearer_methods_supported": ["header"],
                "scopes_supported": list(self._allowed_scopes()),
            }
        )

    async def handle_authorization_server_metadata(self, _: Request):
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")
        return JSONResponse(
            {
                "issuer": self._build_issuer_url(),
                "authorization_endpoint": self._build_authorization_url(),
                "token_endpoint": self._build_token_url(),
                "registration_endpoint": self._build_registration_url(),
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256", "plain"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": list(self._allowed_scopes()),
            }
        )

    async def handle_authorize(self, request: Request):
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")
        if request.method == "POST":
            return await self._handle_authorize_post(request)
        return self._handle_authorize_get(request)

    async def handle_token(self, request: Request):
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")

        params = await self._parse_form_request(request)
        grant_type = (params.get("grant_type") or "").strip()
        if grant_type == "authorization_code":
            return self._handle_authorization_code_grant(params)
        if grant_type == "refresh_token":
            return self._handle_refresh_token_grant(params)
        return self._oauth_error_response(
            "unsupported_grant_type",
            "仅支持 authorization_code 和 refresh_token",
            status_code=400,
        )

    async def handle_client_registration(self, request: Request):
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")

        payload = await self._parse_json_request(request)
        redirect_uris = payload.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return self._oauth_error_response(
                "invalid_client_metadata",
                "redirect_uris 必须是非空数组",
                status_code=400,
            )
        invalid_redirects = [
            uri for uri in redirect_uris if not self._is_safe_redirect_uri(str(uri))
        ]
        if invalid_redirects:
            return self._oauth_error_response(
                "invalid_redirect_uri",
                f"存在不合法的 redirect_uri: {invalid_redirects[0]}",
                status_code=400,
            )

        client_name = str(payload.get("client_name") or "MoviePilot MCP Client").strip()
        grant_types = payload.get("grant_types") or ["authorization_code", "refresh_token"]
        response_types = payload.get("response_types") or ["code"]
        token_endpoint_auth_method = str(
            payload.get("token_endpoint_auth_method") or "none"
        ).strip() or "none"
        if token_endpoint_auth_method != "none":
            return self._oauth_error_response(
                "invalid_client_metadata",
                "当前仅支持 public client（token_endpoint_auth_method=none）",
                status_code=400,
            )

        client_id = self._register_oauth_client(
            client_name=client_name,
            redirect_uris=[str(uri) for uri in redirect_uris],
            grant_types=[str(item) for item in grant_types],
            response_types=[str(item) for item in response_types],
        )
        client_info = self._get_registered_client(client_id) or {}
        # 按 RFC 7591 返回完整的动态客户端注册元数据，
        # 特别是 client_secret_expires_at=0（永不过期）以便 VS Code 等客户端
        # 在重启后可以复用已持久化的 client_id 进行 refresh_token 刷新，
        # 避免每次启动都走一遍交互式授权。
        return JSONResponse(
            {
                "client_id": client_id,
                "client_id_issued_at": self._now(),
                "client_secret_expires_at": 0,
                "client_name": client_info.get("client_name") or client_name,
                "redirect_uris": client_info.get("redirect_uris") or redirect_uris,
                "grant_types": client_info.get("grant_types") or grant_types,
                "response_types": client_info.get("response_types") or response_types,
                "token_endpoint_auth_method": "none",
                "application_type": "native",
            },
            status_code=201,
        )

    async def handle_admin_login(self, request: Request):
        """
        授权页内嵌的管理员登录：校验 MoviePilot 超级管理员账号密码（可选 OTP），
        通过后颁发插件自己的短 TTL 会话 Cookie，并 302 回到原授权入口继续流程。
        """
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")

        params = await self._parse_form_request(request)
        try:
            auth_request = self._parse_authorize_request(params)
        except Exception as err:
            return HTMLResponse(self._render_authorize_error(str(err)), status_code=400)

        username = (params.get("username") or "").strip()
        password = params.get("password") or ""
        otp_password = (params.get("otp_password") or "").strip() or None
        if not username or not password:
            return HTMLResponse(
                self._render_login_required_page(
                    auth_request, error="请输入用户名和密码"
                ),
                status_code=400,
            )

        try:
            from app.chain.user import UserChain  # 延迟导入，避免插件加载期副作用
            success, user_or_message = UserChain().user_authenticate(
                username=username, password=password, mfa_code=otp_password
            )
        except Exception as err:
            logger.error(f"MoviePilot MCP 登录时调用 UserChain 失败：{err}", exc_info=True)
            return HTMLResponse(
                self._render_login_required_page(
                    auth_request, error="服务器内部错误，请查看 MoviePilot 日志"
                ),
                status_code=500,
            )

        if not success:
            message = str(user_or_message) if user_or_message else "用户名或密码错误"
            return HTMLResponse(
                self._render_login_required_page(auth_request, error=message),
                status_code=401,
            )

        if not getattr(user_or_message, "is_superuser", False):
            return HTMLResponse(
                self._render_login_required_page(
                    auth_request,
                    error="仅允许 MoviePilot 超级管理员批准 MCP 授权，请改用管理员账号登录",
                ),
                status_code=403,
            )

        session_token, expires_at = self._issue_admin_session(
            subject=getattr(user_or_message, "id", 0),
            username=getattr(user_or_message, "name", username) or username,
        )

        # 302 回到 /oauth/authorize，保留原查询参数，让 GET 分支渲染批准页
        redirect_target = self._append_query_params(
            self._build_authorization_url(),
            {
                "response_type": "code",
                "client_id": auth_request.client_id,
                "redirect_uri": auth_request.redirect_uri,
                "state": auth_request.state or "",
                # 回传客户端原始 scope 字符串（含未知 scope）而非过滤后的值，
                # 避免 GET 重新解析时丢掉客户端的原始请求，最终 token 响应里
                # 才能把原始 scope 原样回显，防止客户端提示「并非所有请求的权限都已授予」。
                "scope": auth_request.raw_scope or self._format_scope(auth_request.scopes),
                "code_challenge": auth_request.code_challenge,
                "code_challenge_method": auth_request.code_challenge_method,
            },
        )
        response = RedirectResponse(redirect_target, status_code=302)
        response.set_cookie(
            key=self._admin_session_cookie_name,
            value=session_token,
            max_age=max(1, expires_at - self._now()),
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            # 约束 cookie 到插件前缀，避免污染 MoviePilot 其他路径
            path=self._plugin_api_path(),
        )
        return response

    def _handle_authorize_get(self, request: Request):
        try:
            auth_request = self._parse_authorize_request(dict(request.query_params))
        except HTTPException:
            raise
        except Exception as err:
            return HTMLResponse(self._render_authorize_error(str(err)), status_code=400)

        admin = self._get_logged_in_admin(request)
        if not admin:
            return HTMLResponse(
                self._render_login_required_page(auth_request),
                status_code=401,
            )
        return HTMLResponse(self._render_authorize_page(auth_request, admin))

    async def _handle_authorize_post(self, request: Request):
        params = await self._parse_form_request(request)
        try:
            auth_request = self._parse_authorize_request(params)
        except Exception as err:
            redirect_uri = params.get("redirect_uri")
            if redirect_uri and self._is_safe_redirect_uri(redirect_uri):
                return self._oauth_redirect_error(
                    redirect_uri=redirect_uri,
                    error="invalid_request",
                    description=str(err),
                    state=params.get("state"),
                )
            return HTMLResponse(self._render_authorize_error(str(err)), status_code=400)

        admin = self._get_logged_in_admin(request)
        if not admin:
            return HTMLResponse(self._render_login_required_page(auth_request), status_code=401)

        action = (params.get("action") or "deny").strip().lower()
        if action != "approve":
            return self._oauth_redirect_error(
                redirect_uri=auth_request.redirect_uri,
                error="access_denied",
                description="管理员拒绝了本次授权",
                state=auth_request.state,
            )

        code = self._issue_authorization_code(auth_request, admin)
        redirect_params = {"code": code}
        if auth_request.state:
            redirect_params["state"] = auth_request.state
        return RedirectResponse(
            self._append_query_params(auth_request.redirect_uri, redirect_params),
            status_code=302,
        )

    def _handle_authorization_code_grant(self, params: Dict[str, str]):
        code = (params.get("code") or "").strip()
        redirect_uri = (params.get("redirect_uri") or "").strip()
        client_id = (params.get("client_id") or "").strip()
        code_verifier = (params.get("code_verifier") or "").strip()
        if not code or not redirect_uri or not client_id or not code_verifier:
            return self._oauth_error_response(
                "invalid_request",
                "缺少 code、redirect_uri、client_id 或 code_verifier",
                status_code=400,
            )

        store = self._load_oauth_store()
        self._prune_oauth_store(store)
        code_info = (store.get("codes") or {}).pop(code, None)
        self._save_oauth_store(store)
        if not code_info:
            return self._oauth_error_response("invalid_grant", "授权码不存在或已失效", status_code=400)
        if code_info.get("expires_at", 0) < self._now():
            return self._oauth_error_response("invalid_grant", "授权码已过期", status_code=400)
        if redirect_uri != code_info.get("redirect_uri") or client_id != code_info.get("client_id"):
            return self._oauth_error_response("invalid_grant", "客户端信息不匹配", status_code=400)
        if not self._verify_pkce(
            code_verifier=code_verifier,
            code_challenge=code_info.get("code_challenge", ""),
            method=code_info.get("code_challenge_method", "S256"),
        ):
            return self._oauth_error_response("invalid_grant", "PKCE 校验失败", status_code=400)

        token_payload = self._issue_token_pair(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=code_info.get("scopes") or self._default_scopes(),
            subject=code_info.get("subject") or "admin",
            username=code_info.get("username") or "admin",
            requested_scope=code_info.get("requested_scope"),
        )
        return JSONResponse(token_payload)

    def _handle_refresh_token_grant(self, params: Dict[str, str]):
        refresh_token = (params.get("refresh_token") or "").strip()
        client_id = (params.get("client_id") or "").strip()
        if not refresh_token or not client_id:
            return self._oauth_error_response(
                "invalid_request",
                "缺少 refresh_token 或 client_id",
                status_code=400,
            )

        store = self._load_oauth_store()
        self._prune_oauth_store(store)
        refresh_info = (store.get("refresh_tokens") or {}).pop(refresh_token, None)
        self._save_oauth_store(store)
        if not refresh_info:
            return self._oauth_error_response("invalid_grant", "refresh token 不存在或已失效", status_code=400)
        if refresh_info.get("expires_at", 0) < self._now():
            return self._oauth_error_response("invalid_grant", "refresh token 已过期", status_code=400)
        if client_id != refresh_info.get("client_id"):
            return self._oauth_error_response("invalid_grant", "client_id 不匹配", status_code=400)

        token_payload = self._issue_token_pair(
            client_id=client_id,
            redirect_uri=refresh_info.get("redirect_uri") or "",
            scopes=refresh_info.get("scopes") or self._default_scopes(),
            subject=refresh_info.get("subject") or "admin",
            username=refresh_info.get("username") or "admin",
            requested_scope=refresh_info.get("requested_scope"),
        )
        return JSONResponse(token_payload)

    def _authorize_mcp_request(self, request: Request) -> Dict[str, Any]:
        authorization = request.headers.get("authorization") or ""
        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="缺少 Bearer token",
                headers=self._challenge_headers(),
            )

        token = authorization.split(" ", 1)[1].strip()
        if not token:
            raise HTTPException(
                status_code=401,
                detail="缺少 Bearer token",
                headers=self._challenge_headers(),
            )

        if self._allow_legacy_token and token == self._mcp_token.strip():
            return {
                "subject": "legacy-admin",
                "username": "legacy-admin",
                "scopes": self._default_scopes(),
                "legacy": True,
            }

        store = self._load_oauth_store()
        self._prune_oauth_store(store)
        access_info = (store.get("access_tokens") or {}).get(token)
        if not access_info:
            self._save_oauth_store(store)
            raise HTTPException(
                status_code=401,
                detail="访问令牌无效",
                headers=self._challenge_headers(error="invalid_token", description="access token 无效"),
            )
        if access_info.get("expires_at", 0) < self._now():
            (store.get("access_tokens") or {}).pop(token, None)
            self._save_oauth_store(store)
            raise HTTPException(
                status_code=401,
                detail="访问令牌已过期",
                headers=self._challenge_headers(error="invalid_token", description="access token 已过期"),
            )

        self._save_oauth_store(store)
        return access_info

    async def _parse_form_request(self, request: Request) -> Dict[str, str]:
        body = (await request.body()).decode("utf-8", errors="ignore")
        parsed = dict(parse_qsl(body, keep_blank_values=True))
        if parsed:
            return parsed
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            return {str(k): "" if v is None else str(v) for k, v in payload.items()}
        return {}

    def _parse_authorize_request(self, params: Dict[str, Any]) -> OAuthAuthorizeRequest:
        response_type = (params.get("response_type") or "code").strip()
        if response_type != "code":
            raise ValueError("仅支持 response_type=code")

        client_id = (params.get("client_id") or "").strip()
        redirect_uri = (params.get("redirect_uri") or "").strip()
        if not client_id or not redirect_uri:
            raise ValueError("缺少 client_id 或 redirect_uri")
        if not self._is_safe_redirect_uri(redirect_uri):
            raise ValueError("redirect_uri 不安全或格式不正确")
        if not self._client_allows_redirect_uri(client_id, redirect_uri):
            raise ValueError("redirect_uri 未在该 client_id 的注册信息中")

        code_challenge = (params.get("code_challenge") or "").strip()
        if not code_challenge:
            raise ValueError("缺少 code_challenge，必须启用 PKCE")
        code_challenge_method = (params.get("code_challenge_method") or "S256").strip() or "S256"
        if code_challenge_method not in {"S256", "plain"}:
            raise ValueError("仅支持 S256 或 plain 的 PKCE challenge method")

        raw_scope = (params.get("scope") or "").strip() or None
        scopes = self._normalize_requested_scopes(raw_scope)
        return OAuthAuthorizeRequest(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=(params.get("state") or "").strip() or None,
            scopes=scopes,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            raw_scope=raw_scope,
        )

    def _normalize_requested_scopes(self, raw_scope: Optional[str]) -> List[str]:
        if not raw_scope:
            return self._default_scopes()
        requested = [item.strip() for item in str(raw_scope).split() if item.strip()]
        allowed = set(self._allowed_scopes())
        # 宽松处理：ChatGPT / VS Code 等客户端可能会在 authorize 请求里带上一些
        # 与本服务器无关的 scope（如 "openid" 或客户端自定义值）。这里按 RFC 6749
        # 的推荐做法「忽略未知 scope」而不是直接拒绝，避免因客户端差异导致授权失败。
        filtered = [scope for scope in requested if scope in allowed]
        if self._oauth_scopes[1] in filtered and not self._enable_write_tools:
            filtered = [scope for scope in filtered if scope != self._oauth_scopes[1]]
        if not filtered:
            return self._default_scopes()
        return filtered

    def _default_scopes(self) -> List[str]:
        scopes = [self._oauth_scopes[0]]
        if self._enable_write_tools:
            scopes.append(self._oauth_scopes[1])
        return scopes

    def _allowed_scopes(self) -> Tuple[str, ...]:
        if self._enable_write_tools:
            return self._oauth_scopes
        return (self._oauth_scopes[0],)

    def _issue_authorization_code(
        self, auth_request: OAuthAuthorizeRequest, admin: Dict[str, Any]
    ) -> str:
        store = self._load_oauth_store()
        self._prune_oauth_store(store)
        code = self._generate_token()
        (store.setdefault("codes", {}))[code] = {
            "client_id": auth_request.client_id,
            "redirect_uri": auth_request.redirect_uri,
            "subject": admin.get("subject"),
            "username": admin.get("username"),
            "scopes": auth_request.scopes,
            "requested_scope": auth_request.raw_scope,
            "code_challenge": auth_request.code_challenge,
            "code_challenge_method": auth_request.code_challenge_method,
            "expires_at": self._now() + self._oauth_code_ttl,
        }
        self._save_oauth_store(store)
        return code

    def _issue_token_pair(
        self,
        client_id: str,
        redirect_uri: str,
        scopes: List[str],
        subject: str,
        username: str,
        requested_scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        store = self._load_oauth_store()
        self._prune_oauth_store(store)

        access_token = self._generate_token()
        refresh_token = self._generate_token()
        access_expires_at = self._now() + self._oauth_access_token_ttl
        refresh_expires_at = self._now() + self._oauth_refresh_token_ttl

        store.setdefault("access_tokens", {})[access_token] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "subject": subject,
            "username": username,
            "scopes": scopes,
            "requested_scope": requested_scope,
            "expires_at": access_expires_at,
        }
        store.setdefault("refresh_tokens", {})[refresh_token] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "subject": subject,
            "username": username,
            "scopes": scopes,
            "requested_scope": requested_scope,
            "expires_at": refresh_expires_at,
        }
        self._save_oauth_store(store)
        # 为了避免 ChatGPT / VS Code 等客户端因「授予的 scope 不等于请求的 scope」
        # 而弹出「并非所有请求的权限都已授予」警告，若客户端传了 scope，
        # 就原样回显请求的 scope 字符串；服务端内部权限校验仍基于过滤后的 scopes。
        response_scope = requested_scope.strip() if requested_scope and requested_scope.strip() else self._format_scope(scopes)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._oauth_access_token_ttl,
            "refresh_token": refresh_token,
            "scope": response_scope,
        }

    def _load_oauth_store(self) -> Dict[str, Any]:
        data = self.get_data("oauth_store") or {}
        return {
            "clients": dict(data.get("clients") or {}),
            "codes": dict(data.get("codes") or {}),
            "access_tokens": dict(data.get("access_tokens") or {}),
            "refresh_tokens": dict(data.get("refresh_tokens") or {}),
            "admin_sessions": dict(data.get("admin_sessions") or {}),
        }

    def _save_oauth_store(self, store: Dict[str, Any]) -> None:
        self.save_data("oauth_store", store)

    def _prune_oauth_store(self, store: Dict[str, Any]) -> None:
        now = self._now()
        for bucket in ("codes", "access_tokens", "refresh_tokens", "admin_sessions"):
            values = store.get(bucket) or {}
            expired = [key for key, item in values.items() if (item or {}).get("expires_at", 0) < now]
            for key in expired:
                values.pop(key, None)

    async def _parse_json_request(self, request: Request) -> Dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            return payload
        return {}

    def _register_oauth_client(
        self,
        client_name: str,
        redirect_uris: List[str],
        grant_types: List[str],
        response_types: List[str],
    ) -> str:
        store = self._load_oauth_store()
        client_id = self._generate_token()
        store.setdefault("clients", {})[client_id] = {
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types or ["authorization_code", "refresh_token"],
            "response_types": response_types or ["code"],
            "token_endpoint_auth_method": "none",
            "created_at": self._now(),
        }
        self._save_oauth_store(store)
        return client_id

    def _get_registered_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        if not client_id:
            return None
        store = self._load_oauth_store()
        return (store.get("clients") or {}).get(client_id)

    def _client_allows_redirect_uri(self, client_id: str, redirect_uri: str) -> bool:
        client = self._get_registered_client(client_id)
        if not client:
            # 兼容之前的手工 client_id 模式；若已动态注册，则要求 redirect_uri 匹配注册值。
            return True
        return redirect_uri in (client.get("redirect_uris") or [])

    def _challenge_headers(
        self, error: Optional[str] = None, description: Optional[str] = None
    ) -> Dict[str, str]:
        parts = [
            'Bearer realm="moviepilot-mcp"',
            f'resource_metadata="{self._build_resource_metadata_url()}"',
            f'scope="{self._format_scope(self._allowed_scopes())}"',
        ]
        if error:
            parts.append(f'error="{self._escape_auth_header(error)}"')
        if description:
            parts.append(
                f'error_description="{self._escape_auth_header(description)}"'
            )
        return {"WWW-Authenticate": ", ".join(parts)}

    def _get_logged_in_admin(self, request: Request) -> Optional[Dict[str, Any]]:
        """
        读取插件自管理的管理员会话。

        之前曾尝试读取 MoviePilot 主站的 Bearer token / resource HttpOnly cookie 作为
        登录态，但 MoviePilot 的 resource cookie 在用户从 Web UI 退出登录时不会被后端
        清除（MoviePilot 没有登出接口，前端只清 localStorage），这会导致授权页把已退出
        的用户仍然当成已登录的管理员，出现「明明退出了还能批准授权」的严重安全问题。

        因此这里仅信任插件自己通过 `/oauth/login` 颁发并维护的短 TTL 会话。
        """
        session_token = request.cookies.get(self._admin_session_cookie_name)
        if not session_token:
            return None
        store = self._load_oauth_store()
        self._prune_oauth_store(store)
        session = (store.get("admin_sessions") or {}).get(session_token)
        if not session:
            self._save_oauth_store(store)
            return None
        if session.get("expires_at", 0) < self._now():
            (store.get("admin_sessions") or {}).pop(session_token, None)
            self._save_oauth_store(store)
            return None
        # 命中会话，顺带做一次过期剪枝
        self._save_oauth_store(store)
        return {
            "subject": session.get("subject"),
            "username": session.get("username") or "admin",
            "session_token": session_token,
        }

    def _issue_admin_session(self, subject: str, username: str) -> Tuple[str, int]:
        store = self._load_oauth_store()
        self._prune_oauth_store(store)
        session_token = self._generate_token()
        expires_at = self._now() + self._admin_session_ttl
        store.setdefault("admin_sessions", {})[session_token] = {
            "subject": str(subject),
            "username": username,
            "expires_at": expires_at,
        }
        self._save_oauth_store(store)
        return session_token, expires_at

    def _decode_moviepilot_token(self, token: str, purpose: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        secret = settings.RESOURCE_SECRET_KEY if purpose == "resource" else settings.SECRET_KEY
        try:
            payload = jwt.decode(token, secret, algorithms=["HS256"])
        except Exception:
            return None
        if payload.get("purpose") != purpose:
            return None
        return payload

    def _verify_pkce(self, code_verifier: str, code_challenge: str, method: str) -> bool:
        if method == "plain":
            return code_verifier == code_challenge
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        expected = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
        return expected == code_challenge

    def _oauth_error_response(self, error: str, description: str, status_code: int):
        return JSONResponse(
            {"error": error, "error_description": description},
            status_code=status_code,
        )

    def _oauth_redirect_error(
        self,
        redirect_uri: str,
        error: str,
        description: str,
        state: Optional[str],
    ):
        params = {"error": error, "error_description": description}
        if state:
            params["state"] = state
        return RedirectResponse(self._append_query_params(redirect_uri, params), status_code=302)

    def _render_authorize_page(
        self, auth_request: OAuthAuthorizeRequest, admin: Dict[str, Any]
    ) -> str:
        safe_client = html.escape(auth_request.client_id)
        safe_redirect = html.escape(auth_request.redirect_uri)
        safe_scope = html.escape(self._format_scope(auth_request.scopes))
        safe_user = html.escape(admin.get("username") or "admin")
        action_url = html.escape(self._build_authorization_url())
        hidden_fields = "\n".join(
            [
                self._hidden_field("response_type", "code"),
                self._hidden_field("client_id", auth_request.client_id),
                self._hidden_field("redirect_uri", auth_request.redirect_uri),
                self._hidden_field("state", auth_request.state or ""),
                self._hidden_field("scope", auth_request.raw_scope or self._format_scope(auth_request.scopes)),
                self._hidden_field("code_challenge", auth_request.code_challenge),
                self._hidden_field("code_challenge_method", auth_request.code_challenge_method),
            ]
        )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MoviePilot MCP 授权</title>
  <style>
    body {{ font-family: sans-serif; background: #f5f7fb; color: #1f2937; margin: 0; padding: 24px; }}
    .card {{ max-width: 720px; margin: 0 auto; background: #fff; border-radius: 16px; padding: 28px; box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08); }}
    h1 {{ margin-top: 0; font-size: 24px; }}
    .meta {{ background: #f8fafc; border-radius: 12px; padding: 16px; margin: 16px 0; }}
    .meta p {{ margin: 8px 0; word-break: break-all; }}
    .hint {{ color: #475569; }}
    .actions {{ display: flex; gap: 12px; margin-top: 24px; }}
    button {{ border: 0; border-radius: 10px; padding: 12px 18px; font-size: 15px; cursor: pointer; }}
    .approve {{ background: #0f766e; color: #fff; }}
    .deny {{ background: #e2e8f0; color: #0f172a; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>授权 ChatGPT 访问 MoviePilot MCP</h1>
    <p class="hint">你当前以管理员 <strong>{safe_user}</strong> 身份登录。批准后，客户端会通过 OAuth 2.0 Authorization Code + PKCE 获取访问令牌。</p>
    <div class="meta">
      <p><strong>客户端：</strong>{safe_client}</p>
      <p><strong>回调地址：</strong>{safe_redirect}</p>
      <p><strong>申请范围：</strong>{safe_scope}</p>
    </div>
    <form method="post" action="{action_url}">
      {hidden_fields}
      <div class="actions">
        <button class="approve" type="submit" name="action" value="approve">批准授权</button>
        <button class="deny" type="submit" name="action" value="deny">拒绝</button>
      </div>
    </form>
  </div>
</body>
</html>"""

    def _render_login_required_page(
        self,
        auth_request: OAuthAuthorizeRequest,
        error: Optional[str] = None,
    ) -> str:
        safe_scope = html.escape(self._format_scope(auth_request.scopes))
        safe_client = html.escape(auth_request.client_id)
        safe_redirect = html.escape(auth_request.redirect_uri)
        login_action = html.escape(self._build_login_url())
        hidden_fields = "\n".join(
            [
                self._hidden_field("response_type", "code"),
                self._hidden_field("client_id", auth_request.client_id),
                self._hidden_field("redirect_uri", auth_request.redirect_uri),
                self._hidden_field("state", auth_request.state or ""),
                self._hidden_field("scope", auth_request.raw_scope or self._format_scope(auth_request.scopes)),
                self._hidden_field("code_challenge", auth_request.code_challenge),
                self._hidden_field("code_challenge_method", auth_request.code_challenge_method),
            ]
        )
        error_html = (
            f'<p class="error">{html.escape(error)}</p>' if error else ""
        )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MoviePilot MCP 登录</title>
  <style>
    body {{ font-family: sans-serif; background: #f5f7fb; color: #1f2937; margin: 0; padding: 24px; }}
    .card {{ max-width: 480px; margin: 0 auto; background: #fff; border-radius: 16px; padding: 28px; box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08); }}
    h1 {{ margin-top: 0; font-size: 22px; }}
    .hint {{ color: #475569; font-size: 14px; line-height: 1.6; }}
    .meta {{ background: #f8fafc; border-radius: 12px; padding: 12px 16px; margin: 16px 0; font-size: 13px; color: #475569; }}
    .meta p {{ margin: 4px 0; word-break: break-all; }}
    label {{ display: block; margin-top: 14px; font-size: 14px; color: #334155; }}
    input {{ width: 100%; margin-top: 6px; padding: 10px 12px; border: 1px solid #cbd5e1; border-radius: 10px; font-size: 14px; box-sizing: border-box; }}
    button {{ width: 100%; margin-top: 20px; border: 0; border-radius: 10px; padding: 12px; font-size: 15px; cursor: pointer; background: #0f766e; color: #fff; }}
    .error {{ background: #fee2e2; color: #991b1b; border-radius: 10px; padding: 10px 14px; font-size: 14px; margin-top: 16px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>登录 MoviePilot 以授权 MCP 访问</h1>
    <p class="hint">请使用 <strong>MoviePilot 超级管理员账号</strong> 登录。登录态仅用于本次授权，独立于 MoviePilot 主站会话。</p>
    <div class="meta">
      <p><strong>客户端：</strong>{safe_client}</p>
      <p><strong>回调地址：</strong>{safe_redirect}</p>
      <p><strong>申请范围：</strong>{safe_scope}</p>
    </div>
    {error_html}
    <form method="post" action="{login_action}" autocomplete="off">
      {hidden_fields}
      <label>用户名
        <input type="text" name="username" required autofocus>
      </label>
      <label>密码
        <input type="password" name="password" required>
      </label>
      <label>二次验证码（可选）
        <input type="text" name="otp_password" inputmode="numeric" autocomplete="one-time-code">
      </label>
      <button type="submit">登录并继续授权</button>
    </form>
  </div>
</body>
</html>"""

    def _render_authorize_error(self, message: str) -> str:
        safe_message = html.escape(message)
        return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>MoviePilot MCP 授权错误</title></head>
<body style="font-family:sans-serif;padding:24px;background:#f8fafc;color:#1e293b;">
  <div style="max-width:720px;margin:0 auto;background:#fff;border-radius:16px;padding:28px;box-shadow:0 12px 30px rgba(15,23,42,0.08);">
    <h1>授权请求无效</h1>
    <p>{safe_message}</p>
  </div>
</body>
</html>"""

    def _hidden_field(self, name: str, value: str) -> str:
        return (
            f'<input type="hidden" name="{html.escape(name)}" '
            f'value="{html.escape(value)}">'
        )

    def _append_query_params(self, url: str, params: Dict[str, Any]) -> str:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key, value in params.items():
            if value is not None:
                query[key] = str(value)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _is_safe_redirect_uri(self, redirect_uri: str) -> bool:
        parsed = urlparse(redirect_uri)
        if not parsed.scheme:
            return False
        if parsed.scheme.lower() in {"javascript", "data", "file"}:
            return False
        if parsed.scheme.lower() in {"http", "https"}:
            return bool(parsed.netloc)
        return True

    @staticmethod
    def _escape_auth_header(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _now() -> int:
        return int(time.time())

    @staticmethod
    def _format_scope(scopes: List[str] | Tuple[str, ...]) -> str:
        return " ".join(scopes)

    def _build_endpoint_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/mcp"))

    def _build_authorization_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/oauth/authorize"))

    def _build_token_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/oauth/token"))

    def _build_login_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/oauth/login"))

    def _build_registration_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/oauth/register"))

    def _build_issuer_url(self) -> str:
        return self._absolute_url(self._plugin_api_path())

    def _build_authorization_server_metadata_url(self) -> str:
        return self._absolute_url(
            self._plugin_api_path("/.well-known/oauth-authorization-server")
        )

    def _build_resource_metadata_url(self) -> str:
        return self._absolute_url(
            self._plugin_api_path("/.well-known/oauth-protected-resource")
        )

    def _build_openapi_spec_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/openapi.json"))

    def _build_openapi_write_spec_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/openapi.write.json"))

    def _plugin_api_path(self, suffix: str = "") -> str:
        return f"{settings.API_V1_STR}/plugin/{self.__class__.__name__}{suffix}"

    def _absolute_url(self, path: str) -> str:
        if settings.APP_DOMAIN:
            # APP_DOMAIN 可能带有反向代理子路径，去掉前导斜杠以保留该前缀。
            return settings.MP_DOMAIN(path.lstrip("/"))
        return f"http://127.0.0.1:{settings.PORT}{path}"

    @staticmethod
    def _generate_token() -> str:
        return secrets.token_urlsafe(24)
