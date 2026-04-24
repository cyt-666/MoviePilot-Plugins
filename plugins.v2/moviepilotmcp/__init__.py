import importlib
import inspect
import json
import base64
import hashlib
import html
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import jwt
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import schemas
from app.core.config import settings
from app.core.plugin import PluginManager
from app.log import logger
from app.plugins import _PluginBase


class MCPError(Exception):
    """
    MCP 协议错误
    """

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class MCPTool:
    """
    MCP 工具定义
    """

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Any]
    is_write: bool = False


@dataclass
class AgentToolBinding:
    """
    外部 MCP 工具到内置 Agent 工具的绑定关系
    """

    external_name: str
    internal_names: List[str]
    arg_mapper: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    result_mapper: Optional[
        Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
    ] = None
    internal_selector: Optional[Callable[[Dict[str, Any], List[str]], Optional[str]]] = None
    fallback_handler: Optional[Callable[[Dict[str, Any]], Any]] = None


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


class MoviePilotMCP(_PluginBase):
    """
    MoviePilot ChatGPT MCP 薄包装插件
    """

    plugin_name = "MoviePilot MCP Server"
    plugin_desc = "MoviePilot v2.10.4 的 ChatGPT 外部 MCP OAuth 包装层"
    plugin_icon = "https://raw.githubusercontent.com/cyt-666/MoviePilot-Plugins/main/icons/moviepilotmcp.svg"
    plugin_version = "0.3.5"
    plugin_author = "Codex"
    author_url = "https://wiki.movie-pilot.org/"
    plugin_config_prefix = "moviepilotmcp_"
    plugin_order = 2
    auth_level = 1

    _protocol_version = "2024-11-05"
    _server_name = "moviepilot-chatgpt-wrapper"
    _oauth_scopes = ("moviepilot.mcp.read", "moviepilot.mcp.write")
    _oauth_code_ttl = 600
    _oauth_access_token_ttl = 3600
    _oauth_refresh_token_ttl = 30 * 24 * 3600
    _agent_manager_candidates = [
        ("app.agent.tools.manager", "MoviePilotToolsManager"),
    ]

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._allow_legacy_token = False
        self._mcp_token = ""
        self._actor_name = "ChatGPT MCP"
        self._enable_write_tools = True
        self._tools: Dict[str, MCPTool] = {}
        self._bindings: Dict[str, AgentToolBinding] = {}
        self._agent_catalog: Dict[str, Dict[str, Any]] = {}
        self._agent_available = False
        self._agent_error: Optional[str] = None
        self._build_tools()

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._enable_write_tools = bool(config.get("enable_write_tools", True))
        self._allow_legacy_token = bool(config.get("allow_legacy_token", False))
        self._mcp_token = (config.get("mcp_token") or "").strip()
        self._actor_name = (config.get("actor_name") or "ChatGPT MCP").strip() or "ChatGPT MCP"

        if not self._mcp_token:
            self._mcp_token = self._generate_token()

        self.update_config(
            {
                "enabled": self._enabled,
                "mcp_token": self._mcp_token,
                "allow_legacy_token": self._allow_legacy_token,
                "actor_name": self._actor_name,
                "enable_write_tools": self._enable_write_tools,
            }
        )
        self._refresh_agent_catalog(force=True)

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        pass

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
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        endpoint_url = self._build_endpoint_url()
        authorize_url = self._build_authorization_url()
        token_url = self._build_token_url()
        metadata_url = self._build_resource_metadata_url()
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
                                            "text": "该插件是 MoviePilot 内置 Agent 工具的 ChatGPT 外部 MCP OAuth 包装层：对外暴露独立 MCP 入口，对内优先复用内置 Agent tools，并通过 OAuth 2.0 Authorization Code + PKCE 完成授权。",
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
            "endpoint_url": endpoint_url,
            "authorize_url": authorize_url,
            "token_url": token_url,
            "resource_metadata_url": metadata_url,
            "oauth_scope": self._format_scope(self._default_scopes()),
            "show_mcp_token": False,
        }

    def get_page(self) -> List[dict]:
        self._refresh_agent_catalog(force=True)
        enabled_tools = self._available_tools()
        endpoint_url = self._build_endpoint_url()
        authorize_url = self._build_authorization_url()
        token_url = self._build_token_url()
        status_text = "已就绪" if self._agent_available else "不可用"
        agent_text = (
            f"内置 Agent 工具状态：{status_text}"
            if self._agent_available
            else f"内置 Agent 工具状态：不可用，原因：{self._agent_error or '未知'}"
        )
        missing = self._missing_external_tools()

        page = [
            {
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "MoviePilot ChatGPT MCP 包装层",
                    },
                    {
                        "component": "VCardText",
                        "text": f"状态：{'已启用' if self._enabled else '未启用'}",
                    },
                    {
                        "component": "VCardText",
                        "text": f"Endpoint：{endpoint_url}",
                    },
                    {
                        "component": "VCardText",
                        "text": f"OAuth Authorization：{authorize_url}",
                    },
                    {
                        "component": "VCardText",
                        "text": f"OAuth Token：{token_url}",
                    },
                    {
                        "component": "VCardText",
                        "text": agent_text,
                    },
                    {
                        "component": "VCardText",
                        "text": f"当前可对外暴露工具数：{len(enabled_tools)}",
                    },
                    {
                        "component": "VCardText",
                        "text": f"写操作工具：{'已开启' if self._enable_write_tools else '已关闭'}",
                    },
                    {
                        "component": "VCardText",
                        "text": f"写入显示名称：{self._actor_name}",
                    },
                    {
                        "component": "VCardText",
                        "text": f"兼容静态 Token：{'已开启' if self._allow_legacy_token else '已关闭'}",
                    },
                ],
            }
        ]

        if missing:
            page.append(
                {
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {
                            "component": "VCardTitle",
                            "text": "缺失说明",
                        },
                        {
                            "component": "VCardText",
                            "text": "以下外部工具因目标版本未暴露对应内置 Agent 工具而不会出现在 tools/list 中："
                        },
                        {
                            "component": "VCardText",
                            "text": "、".join(missing[:20]),
                        },
                    ],
                }
            )

        return page

    async def handle_mcp(self, request: Request):
        if not self._enabled:
            raise HTTPException(status_code=403, detail="MoviePilot MCP 包装层未启用")

        auth_context = self._authorize_mcp_request(request)

        try:
            payload = await request.json()
        except Exception as err:
            logger.error(f"MoviePilot MCP 请求体解析失败：{err}")
            return self._jsonrpc_error(None, -32700, "Invalid JSON")

        if not isinstance(payload, dict):
            return self._jsonrpc_error(None, -32600, "Invalid Request")

        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        if not method:
            return self._jsonrpc_error(request_id, -32600, "Missing method")

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = await self._handle_tools_call(params, auth_context)
            else:
                raise MCPError(-32601, f"Method not found: {method}")
            return self._jsonrpc_result(request_id, result)
        except MCPError as err:
            return self._jsonrpc_error(request_id, err.code, err.message)
        except Exception as err:
            logger.error(f"MoviePilot MCP 调用失败：{err}", exc_info=True)
            return self._jsonrpc_error(request_id, -32603, str(err))

    def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._refresh_agent_catalog(force=True)
        client_info = params.get("clientInfo") or {}
        logger.info(
            "MoviePilot MCP client initialized: %s",
            client_info.get("name") or "unknown",
        )
        instructions = (
            "Use tools to read and operate MoviePilot safely through the external ChatGPT MCP wrapper. "
            "This wrapper only exposes a curated subset of internal Agent tools."
        )
        if not self._agent_available:
            instructions += f" Internal Agent tools are currently unavailable: {self._agent_error or 'unknown'}"
        return {
            "protocolVersion": self._protocol_version,
            "capabilities": {
                "tools": {
                    "listChanged": False,
                }
            },
            "serverInfo": {
                "name": self._server_name,
                "version": self.plugin_version,
            },
            "instructions": instructions,
        }

    def _handle_tools_list(self) -> Dict[str, Any]:
        if not self._agent_available:
            logger.warning("内置 Agent 工具不可用，tools/list 返回空列表")
            return {"tools": []}

        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "annotations": {
                        "readOnlyHint": not tool.is_write,
                    },
                }
                for tool in self._available_tools().values()
            ]
        }

    async def _handle_tools_call(
        self, params: Dict[str, Any], auth_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        tool_name = self._canonical_tool_name(params.get("name"))
        if not tool_name:
            raise MCPError(-32602, "Missing tool name")

        tool = self._tools.get(tool_name)
        if not tool:
            raise MCPError(-32601, f"Tool not found: {tool_name}")

        if not self._agent_available:
            raise MCPError(
                -32001,
                f"内置 Agent 工具不可用，当前包装层无法处理 {tool_name}：{self._agent_error or '未知原因'}",
            )

        if tool.is_write and not self._enable_write_tools:
            raise MCPError(-32601, f"Tool not found: {tool_name}")
        if tool.is_write and self._oauth_scopes[1] not in auth_context.get("scopes", []):
            raise MCPError(-32010, "当前访问令牌未授予写操作 scope")

        if tool_name not in self._available_tools():
            raise MCPError(
                -32002,
                f"工具 {tool_name} 在当前 MoviePilot v2.10.4 运行环境中不可用，已按 fail-closed 策略隐藏",
            )

        arguments = params.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception as err:
                raise MCPError(-32602, f"Tool arguments must be valid JSON: {err}") from err
        if not isinstance(arguments, dict):
            raise MCPError(-32602, "Tool arguments must be an object")

        result = tool.handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        normalized = self._normalize_tool_result(result)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(normalized, ensure_ascii=False, indent=2),
                }
            ],
            "structuredContent": normalized,
            "isError": not normalized.get("success", False),
        }

    def _normalize_tool_result(self, result: Any) -> Dict[str, Any]:
        if isinstance(result, schemas.Response):
            return {
                "success": result.success,
                "message": result.message,
                "data": self._jsonable(result.data),
            }
        if isinstance(result, dict) and {"success", "message", "data"} <= set(result.keys()):
            return {
                "success": bool(result.get("success")),
                "message": result.get("message"),
                "data": self._jsonable(result.get("data")),
            }
        if isinstance(result, str):
            success, message, data = self._parse_agent_text_result(result)
            return {
                "success": success,
                "message": message,
                "data": self._jsonable(data),
            }
        return {
            "success": True,
            "message": None,
            "data": self._jsonable(result),
        }

    def _jsonrpc_result(self, request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }

    def _jsonrpc_error(self, request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
            },
        }

    @staticmethod
    def _canonical_tool_name(name: Any) -> Optional[str]:
        if not name:
            return None
        return str(name).strip().replace(".", "_")

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
        return JSONResponse(
            {
                "client_id": client_id,
                "client_id_issued_at": self._now(),
                "client_name": client_info.get("client_name") or client_name,
                "redirect_uris": client_info.get("redirect_uris") or redirect_uris,
                "grant_types": client_info.get("grant_types") or grant_types,
                "response_types": client_info.get("response_types") or response_types,
                "token_endpoint_auth_method": "none",
            },
            status_code=201,
        )

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

        scopes = self._normalize_requested_scopes(params.get("scope"))
        return OAuthAuthorizeRequest(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=(params.get("state") or "").strip() or None,
            scopes=scopes,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )

    def _normalize_requested_scopes(self, raw_scope: Optional[str]) -> List[str]:
        if not raw_scope:
            return self._default_scopes()
        requested = [item.strip() for item in str(raw_scope).split() if item.strip()]
        allowed = set(self._allowed_scopes())
        if any(scope not in allowed for scope in requested):
            raise ValueError("请求了不被允许的 scope")
        if self._oauth_scopes[1] in requested and not self._enable_write_tools:
            raise ValueError("当前插件未开启写操作工具，不能授予写操作 scope")
        if not requested:
            return self._default_scopes()
        return requested

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
            "expires_at": access_expires_at,
        }
        store.setdefault("refresh_tokens", {})[refresh_token] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "subject": subject,
            "username": username,
            "scopes": scopes,
            "expires_at": refresh_expires_at,
        }
        self._save_oauth_store(store)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._oauth_access_token_ttl,
            "refresh_token": refresh_token,
            "scope": self._format_scope(scopes),
        }

    def _load_oauth_store(self) -> Dict[str, Any]:
        data = self.get_data("oauth_store") or {}
        return {
            "clients": dict(data.get("clients") or {}),
            "codes": dict(data.get("codes") or {}),
            "access_tokens": dict(data.get("access_tokens") or {}),
            "refresh_tokens": dict(data.get("refresh_tokens") or {}),
        }

    def _save_oauth_store(self, store: Dict[str, Any]) -> None:
        self.save_data("oauth_store", store)

    def _prune_oauth_store(self, store: Dict[str, Any]) -> None:
        now = self._now()
        for bucket in ("codes", "access_tokens", "refresh_tokens"):
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
        authorization = request.headers.get("authorization") or ""
        if authorization.startswith("Bearer "):
            payload = self._decode_moviepilot_token(
                authorization.split(" ", 1)[1].strip(),
                purpose="authentication",
            )
            if payload and payload.get("super_user"):
                return {
                    "subject": str(payload.get("sub")),
                    "username": payload.get("username") or "admin",
                }

        resource_token = request.cookies.get(settings.PROJECT_NAME)
        if resource_token:
            payload = self._decode_moviepilot_token(resource_token, purpose="resource")
            if payload and payload.get("super_user"):
                return {
                    "subject": str(payload.get("sub")),
                    "username": payload.get("username") or "admin",
                }
        return None

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
                self._hidden_field("scope", self._format_scope(auth_request.scopes)),
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

    def _render_login_required_page(self, auth_request: OAuthAuthorizeRequest) -> str:
        safe_scope = html.escape(self._format_scope(auth_request.scopes))
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MoviePilot MCP 授权</title>
  <style>
    body {{ font-family: sans-serif; background: #f8fafc; color: #1e293b; margin: 0; padding: 24px; }}
    .card {{ max-width: 720px; margin: 0 auto; background: #fff; border-radius: 16px; padding: 28px; box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08); }}
    p {{ line-height: 1.6; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>需要先登录 MoviePilot</h1>
    <p>当前浏览器没有检测到 MoviePilot 管理员登录态，因此暂时不能批准 OAuth 授权。</p>
    <p>请先在同一浏览器中登录 MoviePilot 管理界面，再刷新当前页面继续授权。</p>
    <p>本次申请范围：<strong>{safe_scope}</strong></p>
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

    def _available_tools(self) -> Dict[str, MCPTool]:
        tools = {}
        for name, tool in self._tools.items():
            if tool.is_write and not self._enable_write_tools:
                continue
            if self._binding_available(self._bindings[name]):
                tools[name] = tool
        return tools

    def _missing_external_tools(self) -> List[str]:
        missing = []
        for name, binding in self._bindings.items():
            if not self._binding_available(binding):
                missing.append(name)
        return missing

    def _binding_available(self, binding: AgentToolBinding) -> bool:
        if not self._agent_available:
            return False
        if self._resolve_internal_tool_name(binding, {}) is not None:
            return True
        return binding.fallback_handler is not None

    def _refresh_agent_catalog(self, force: bool = False) -> None:
        if self._agent_catalog and not force:
            return

        try:
            manager_cls = self._get_agent_manager_class()
            manager = manager_cls(
                user_id=str(self._resolve_user().id),
                session_id=str(uuid.uuid4()),
            )
            catalog: Dict[str, Dict[str, Any]] = {}
            for tool_def in manager.list_tools():
                name = getattr(tool_def, "name", None)
                if not name:
                    continue
                catalog[name] = {
                    "description": getattr(tool_def, "description", ""),
                    "input_schema": getattr(tool_def, "input_schema", {}) or {},
                }
            self._agent_catalog = catalog
            self._agent_available = True
            self._agent_error = None
        except Exception as err:
            self._agent_catalog = {}
            self._agent_available = False
            self._agent_error = str(err)
            logger.warning("MoviePilot 内置 Agent 工具不可用：%s", err)

    def _get_agent_manager_class(self):
        last_error = None
        for module_name, class_name in self._agent_manager_candidates:
            try:
                module = importlib.import_module(module_name)
                return getattr(module, class_name)
            except Exception as err:
                last_error = err
        raise RuntimeError(
            "未找到 MoviePilot 内置 Agent Tools Manager，请确认目标运行版本为 v2.10.4 且已包含 Agent MCP 能力"
        ) from last_error

    async def _dispatch_tool(self, external_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        binding = self._bindings[external_name]
        internal_name = self._resolve_internal_tool_name(binding, args)

        if internal_name:
            agent_args = binding.arg_mapper(args) if binding.arg_mapper else dict(args)
            if not isinstance(agent_args, dict):
                raise MCPError(-32603, f"{external_name} 参数适配失败")
            agent_args.setdefault(
                "explanation",
                f"External ChatGPT MCP wrapper call for {external_name}",
            )
            result = await self._call_agent_tool(internal_name, agent_args)
            normalized = self._normalize_tool_result(result)
            if binding.result_mapper:
                normalized = binding.result_mapper(normalized, args)
            return normalized

        if binding.fallback_handler:
            logger.warning("工具 %s 回退到插件内部安全实现", external_name)
            result = binding.fallback_handler(args)
            if inspect.isawaitable(result):
                result = await result
            normalized = self._normalize_tool_result(result)
            if binding.result_mapper:
                normalized = binding.result_mapper(normalized, args)
            return normalized

        raise MCPError(
            -32002,
            f"工具 {external_name} 在目标 MoviePilot v2.10.4 环境中缺少对应内置 Agent 工具，已按 fail-closed 拒绝调用",
        )

    def _resolve_internal_tool_name(
        self, binding: AgentToolBinding, args: Dict[str, Any]
    ) -> Optional[str]:
        catalog_names = list(self._agent_catalog.keys())
        if binding.internal_selector:
            selected = binding.internal_selector(args, catalog_names)
            if selected:
                return selected
        for internal_name in binding.internal_names:
            if internal_name in self._agent_catalog:
                return internal_name
        return None

    async def _call_agent_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        manager_cls = self._get_agent_manager_class()
        manager = manager_cls(
            user_id=str(self._resolve_user().id),
            session_id=str(uuid.uuid4()),
        )
        result = manager.call_tool(tool_name, arguments)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _build_tools(self) -> None:
        limit_schema = {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}
        offset_schema = {"type": "integer", "minimum": 0, "default": 0}

        self._register_agent_tool(
            "dashboard_summary",
            "获取系统统计、存储、下载器、调度、CPU 和内存摘要。",
            self._schema({}),
            internal_names=["query_dashboard_summary", "query_dashboard", "query_system_summary"],
            fallback_handler=self._fallback_dashboard_summary,
        )
        self._register_agent_tool(
            "dashboard_processes",
            "获取 MoviePilot 所在主机的进程概览。",
            self._schema({"limit": limit_schema, "offset": offset_schema}),
            internal_names=["query_processes", "query_dashboard_processes"],
            fallback_handler=self._fallback_dashboard_processes,
        )
        self._register_agent_tool(
            "media_search",
            "搜索媒体、合集或人物信息。",
            self._schema(
                {
                    "title": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["media", "collection", "person"],
                        "default": "media",
                    },
                    "year": {"type": "string"},
                    "season": {"type": "integer"},
                    "limit": limit_schema,
                    "offset": offset_schema,
                },
                required=["title"],
            ),
            internal_names=["search_media", "search_person"],
            arg_mapper=self._map_media_search,
            internal_selector=self._select_media_search_tool,
            result_mapper=self._postprocess_paginated_result,
        )
        self._register_agent_tool(
            "media_detail",
            "按媒体 ID 获取媒体详情。",
            self._schema(
                {
                    "tmdb_id": {"type": "integer"},
                    "douban_id": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["movie", "tv"]},
                },
                required=["media_type"],
            ),
            internal_names=["query_media_detail"],
            arg_mapper=self._map_media_detail,
        )
        self._register_agent_tool(
            "media_recognize",
            "根据标题副标题或文件路径识别媒体信息。",
            self._schema(
                {
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "path": {"type": "string"},
                }
            ),
            internal_names=["recognize_media"],
        )
        self._register_agent_tool(
            "media_seasons",
            "查询电视剧季信息。",
            self._schema(
                {
                    "tmdb_id": {"type": "integer"},
                    "douban_id": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["tv"]},
                },
                required=["media_type"],
            ),
            internal_names=["query_media_detail"],
            arg_mapper=self._map_media_detail,
            result_mapper=self._map_media_seasons_result,
        )
        self._register_agent_tool(
            "media_discover",
            "浏览探索内容源。",
            self._schema(
                {
                    "source": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["movie", "tv", "all"], "default": "all"},
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                },
                required=["source"],
            ),
            internal_names=["get_discoveries", "discover_media"],
            arg_mapper=self._map_recommendation_like,
        )
        self._register_agent_tool(
            "media_recommend",
            "浏览推荐内容源。",
            self._schema(
                {
                    "source": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["movie", "tv", "all"], "default": "all"},
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                },
                required=["source"],
            ),
            internal_names=["get_recommendations"],
            arg_mapper=self._map_recommendation_like,
        )
        self._register_agent_tool(
            "subscribe_list",
            "查询订阅列表。",
            self._schema(
                {
                    "state": {"type": "string"},
                    "type": {"type": "string", "enum": ["movie", "tv", "all"]},
                    "tmdb_id": {"type": "integer"},
                    "douban_id": {"type": "string"},
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": limit_schema,
                    "offset": offset_schema,
                }
            ),
            internal_names=["query_subscribes"],
            arg_mapper=self._map_subscribe_list,
            result_mapper=self._postprocess_paginated_result,
        )
        self._register_agent_tool(
            "subscribe_detail",
            "按订阅 ID 获取订阅详情。",
            self._schema({"subscribe_id": {"type": "integer"}}, required=["subscribe_id"]),
            internal_names=["query_subscribe_detail", "get_subscribe_detail"],
            arg_mapper=self._map_subscribe_detail,
        )
        self._register_agent_tool(
            "subscribe_create",
            "新增订阅。",
            self._schema(
                {
                    "name": {"type": "string"},
                    "year": {"type": "string"},
                    "type": {"type": "string", "enum": ["movie", "tv"]},
                    "season": {"type": "integer"},
                    "tmdbid": {"type": "integer"},
                    "doubanid": {"type": "string"},
                    "start_episode": {"type": "integer"},
                    "total_episode": {"type": "integer"},
                    "quality": {"type": "string"},
                    "resolution": {"type": "string"},
                    "effect": {"type": "string"},
                    "filter_groups": {"type": "array", "items": {"type": "string"}},
                    "sites": {"type": "array", "items": {"type": "integer"}},
                },
                required=["name", "year", "type"],
            ),
            internal_names=["add_subscribe"],
            arg_mapper=self._map_subscribe_create,
            is_write=True,
        )
        self._register_agent_tool(
            "subscribe_update",
            "更新订阅。",
            self._schema(
                {
                    "subscribe_id": {"type": "integer"},
                    "patch": {"type": "object"},
                },
                required=["subscribe_id", "patch"],
            ),
            internal_names=["update_subscribe"],
            arg_mapper=self._map_subscribe_update,
            is_write=True,
        )
        self._register_agent_tool(
            "subscribe_set_state",
            "更新订阅状态。",
            self._schema(
                {
                    "subscribe_id": {"type": "integer"},
                    "state": {"type": "string", "enum": ["R", "P", "S", "N"]},
                },
                required=["subscribe_id", "state"],
            ),
            internal_names=["update_subscribe"],
            arg_mapper=self._map_subscribe_state,
            is_write=True,
        )
        self._register_agent_tool(
            "subscribe_refresh",
            "刷新订阅或 TMDB 信息。",
            self._schema({"mode": {"type": "string", "enum": ["all", "tmdb"], "default": "all"}}),
            internal_names=["run_scheduler", "refresh_subscribes"],
            arg_mapper=self._map_subscribe_refresh,
            is_write=True,
        )
        self._register_agent_tool(
            "subscribe_search",
            "触发搜索全部订阅或指定订阅。",
            self._schema({"subscribe_id": {"type": "integer"}}),
            internal_names=["search_subscribe", "run_scheduler"],
            arg_mapper=self._map_subscribe_search,
            is_write=True,
        )
        self._register_agent_tool(
            "download_list",
            "查看当前下载任务。",
            self._schema(
                {
                    "downloader": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["all", "downloading", "completed", "paused"],
                        "default": "all",
                    },
                    "hash": {"type": "string"},
                    "title": {"type": "string"},
                    "tag": {"type": "string"},
                    "limit": limit_schema,
                    "offset": offset_schema,
                }
            ),
            internal_names=["query_download_tasks"],
            result_mapper=self._postprocess_paginated_result,
        )
        self._register_agent_tool(
            "download_clients",
            "获取可用下载器列表。",
            self._schema({}),
            internal_names=["query_downloaders"],
        )
        self._register_agent_tool(
            "download_add",
            "新增下载任务。",
            self._schema(
                {
                    "torrent": {"type": "object"},
                    "media": {"type": "object"},
                    "downloader": {"type": "string"},
                    "save_path": {"type": "string"},
                },
                required=["torrent"],
            ),
            internal_names=["add_download"],
            arg_mapper=self._map_download_add,
            is_write=True,
        )
        self._register_agent_tool(
            "download_start",
            "开始下载任务。",
            self._schema(
                {
                    "hash": {"type": "string"},
                    "downloader": {"type": "string"},
                },
                required=["hash"],
            ),
            internal_names=["modify_download"],
            arg_mapper=lambda args: self._map_download_modify(args, "start"),
            is_write=True,
        )
        self._register_agent_tool(
            "download_stop",
            "暂停下载任务。",
            self._schema(
                {
                    "hash": {"type": "string"},
                    "downloader": {"type": "string"},
                },
                required=["hash"],
            ),
            internal_names=["modify_download"],
            arg_mapper=lambda args: self._map_download_modify(args, "stop"),
            is_write=True,
        )
        self._register_agent_tool(
            "history_download",
            "查询下载历史。",
            self._schema(
                {
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": limit_schema,
                    "offset": offset_schema,
                }
            ),
            internal_names=["query_download_history"],
            result_mapper=self._postprocess_paginated_result,
        )
        self._register_agent_tool(
            "history_transfer",
            "查询整理历史。",
            self._schema(
                {
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": ["all", "success", "failed"], "default": "all"},
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": limit_schema,
                    "offset": offset_schema,
                }
            ),
            internal_names=["query_transfer_history"],
            result_mapper=self._postprocess_paginated_result,
        )
        self._register_agent_tool(
            "mediaserver_clients",
            "获取可用媒体服务器列表。",
            self._schema({}),
            internal_names=["query_mediaservers", "query_media_servers"],
        )
        self._register_agent_tool(
            "mediaserver_library",
            "获取媒体库列表。",
            self._schema(
                {"server": {"type": "string"}, "page": {"type": "integer", "minimum": 1, "default": 1}}
            ),
            internal_names=["query_library", "query_media_library"],
        )
        self._register_agent_tool(
            "mediaserver_latest",
            "获取最近入库内容。",
            self._schema(
                {"server": {"type": "string"}, "page": {"type": "integer", "minimum": 1, "default": 1}}
            ),
            internal_names=["query_library_latest"],
        )
        self._register_agent_tool(
            "mediaserver_playing",
            "获取正在播放内容。",
            self._schema(
                {"server": {"type": "string"}, "page": {"type": "integer", "minimum": 1, "default": 1}}
            ),
            internal_names=["query_library_playing", "query_now_playing"],
        )
        self._register_agent_tool(
            "mediaserver_exists_local",
            "查询媒体是否已存在于媒体服务器。",
            self._schema(
                {
                    "tmdb_id": {"type": "integer"},
                    "douban_id": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["movie", "tv"]},
                },
                required=["media_type"],
            ),
            internal_names=["query_library_exists"],
            arg_mapper=self._map_media_detail,
        )
        self._register_agent_tool(
            "mediaserver_not_exists",
            "查询媒体服务器缺失内容。",
            self._schema(
                {
                    "tmdb_id": {"type": "integer"},
                    "douban_id": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["movie", "tv"]},
                },
                required=["media_type"],
            ),
            internal_names=["query_library_not_exists"],
            arg_mapper=self._map_media_detail,
        )
        self._register_agent_tool(
            "site_list",
            "查询站点列表。",
            self._schema(
                {
                    "status": {"type": "string", "enum": ["all", "active", "inactive"], "default": "all"},
                    "name": {"type": "string"},
                    "limit": limit_schema,
                    "offset": offset_schema,
                }
            ),
            internal_names=["query_sites"],
            result_mapper=self._postprocess_paginated_result,
        )
        self._register_agent_tool(
            "site_detail",
            "查询站点详情。",
            self._schema({"site_id": {"type": "integer"}}, required=["site_id"]),
            internal_names=["query_site_detail", "get_site_detail"],
        )
        self._register_agent_tool(
            "site_test",
            "测试站点连通性。",
            self._schema({"site_id": {"type": "integer"}}, required=["site_id"]),
            internal_names=["test_site"],
            arg_mapper=lambda args: {"site_identifier": self._require(args, "site_id")},
        )
        self._register_agent_tool(
            "site_resource",
            "浏览站点资源。",
            self._schema(
                {
                    "site_id": {"type": "integer"},
                    "keyword": {"type": "string"},
                    "cat": {"type": "string"},
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                },
                required=["site_id"],
            ),
            internal_names=["query_site_resource", "search_torrents_for_site"],
        )
        self._register_agent_tool(
            "site_userdata",
            "查询站点用户数据。",
            self._schema(
                {
                    "site_id": {"type": "integer"},
                    "workdate": {"type": "string"},
                    "limit": limit_schema,
                    "offset": offset_schema,
                },
                required=["site_id"],
            ),
            internal_names=["query_site_userdata"],
            result_mapper=self._postprocess_paginated_result,
        )
        self._register_agent_tool(
            "workflow_list",
            "查询工作流列表。",
            self._schema(
                {
                    "state": {"type": "string", "default": "all"},
                    "name": {"type": "string"},
                    "trigger_type": {"type": "string", "default": "all"},
                    "limit": limit_schema,
                    "offset": offset_schema,
                }
            ),
            internal_names=["query_workflows"],
            result_mapper=self._postprocess_paginated_result,
        )
        self._register_agent_tool(
            "workflow_detail",
            "查询工作流详情。",
            self._schema({"workflow_id": {"type": "integer"}}, required=["workflow_id"]),
            internal_names=["query_workflow_detail", "get_workflow_detail"],
        )
        self._register_agent_tool(
            "workflow_create",
            "创建工作流。",
            self._schema(
                {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "timer": {"type": "string"},
                    "state": {"type": "string"},
                    "actions": {"type": "array", "items": {"type": "object"}},
                    "flows": {"type": "array", "items": {"type": "object"}},
                },
                required=["name"],
            ),
            internal_names=["create_workflow"],
            is_write=True,
        )
        self._register_agent_tool(
            "workflow_update",
            "更新工作流。",
            self._schema(
                {
                    "workflow_id": {"type": "integer"},
                    "patch": {"type": "object"},
                },
                required=["workflow_id", "patch"],
            ),
            internal_names=["update_workflow"],
            is_write=True,
        )
        self._register_agent_tool(
            "workflow_run",
            "执行工作流。",
            self._schema(
                {
                    "workflow_id": {"type": "integer"},
                    "from_begin": {"type": "boolean", "default": True},
                },
                required=["workflow_id"],
            ),
            internal_names=["run_workflow"],
            is_write=True,
        )
        self._register_agent_tool(
            "workflow_start",
            "启用工作流。",
            self._schema({"workflow_id": {"type": "integer"}}, required=["workflow_id"]),
            internal_names=["start_workflow"],
            is_write=True,
        )
        self._register_agent_tool(
            "workflow_pause",
            "停用工作流。",
            self._schema({"workflow_id": {"type": "integer"}}, required=["workflow_id"]),
            internal_names=["pause_workflow"],
            is_write=True,
        )
        self._register_agent_tool(
            "plugin_list",
            "查询已安装插件列表。",
            self._schema({"limit": limit_schema, "offset": offset_schema}),
            internal_names=["query_installed_plugins"],
            result_mapper=self._postprocess_paginated_result,
            fallback_handler=self._fallback_plugin_list,
        )
        self._register_agent_tool(
            "plugin_config",
            "读取指定插件配置。",
            self._schema({"plugin_id": {"type": "string"}}, required=["plugin_id"]),
            internal_names=[],
            fallback_handler=self._fallback_plugin_config,
        )
        self._register_agent_tool(
            "plugin_page",
            "读取指定插件详情页数据。",
            self._schema({"plugin_id": {"type": "string"}}, required=["plugin_id"]),
            internal_names=[],
            fallback_handler=self._fallback_plugin_page,
        )
        self._register_agent_tool(
            "plugin_reload",
            "重新加载指定插件。",
            self._schema({"plugin_id": {"type": "string"}}, required=["plugin_id"]),
            internal_names=[],
            fallback_handler=self._fallback_plugin_reload,
            is_write=True,
        )

    def _register_agent_tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        internal_names: List[str],
        arg_mapper: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        result_mapper: Optional[
            Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
        ] = None,
        internal_selector: Optional[Callable[[Dict[str, Any], List[str]], Optional[str]]] = None,
        fallback_handler: Optional[Callable[[Dict[str, Any]], Any]] = None,
        is_write: bool = False,
    ) -> None:
        self._bindings[name] = AgentToolBinding(
            external_name=name,
            internal_names=internal_names,
            arg_mapper=arg_mapper,
            result_mapper=result_mapper,
            internal_selector=internal_selector,
            fallback_handler=fallback_handler,
        )
        self._tools[name] = MCPTool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=lambda args, tool_name=name: self._dispatch_tool(tool_name, args),
            is_write=is_write,
        )

    @staticmethod
    def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
            "required": required or [],
        }

    def _select_media_search_tool(
        self, args: Dict[str, Any], catalog_names: List[str]
    ) -> Optional[str]:
        search_type = args.get("type", "media")
        if search_type == "person" and "search_person" in catalog_names:
            return "search_person"
        if "search_media" in catalog_names:
            return "search_media"
        if "search_person" in catalog_names:
            return "search_person"
        return None

    def _map_media_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        search_type = args.get("type", "media")
        title = self._require(args, "title")
        if search_type == "person":
            return {"name": title}
        return {
            "title": title,
            "year": args.get("year"),
            "media_type": self._normalize_media_type(args.get("type")),
            "season": args.get("season"),
        }

    def _map_media_detail(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "tmdb_id": args.get("tmdb_id"),
            "douban_id": args.get("douban_id"),
            "media_type": self._normalize_media_type(self._require(args, "media_type")),
        }

    def _map_recommendation_like(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "source": self._require(args, "source"),
            "media_type": args.get("media_type", "all"),
            "page": int(args.get("page", 1)),
        }

    def _map_subscribe_list(self, args: Dict[str, Any]) -> Dict[str, Any]:
        state = args.get("state")
        if state and "," in str(state):
            state = str(state).split(",", 1)[0].strip()
        return {
            "status": state or "all",
            "media_type": self._normalize_media_type(args.get("type", "all")),
            "tmdb_id": args.get("tmdb_id"),
            "douban_id": args.get("douban_id"),
            "page": self._page_from_limit_offset(args),
        }

    def _map_subscribe_detail(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"subscribe_id": int(self._require(args, "subscribe_id"))}

    def _map_subscribe_create(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": self._require(args, "name"),
            "year": self._require(args, "year"),
            "media_type": self._normalize_media_type(self._require(args, "type")),
            "season": args.get("season"),
            "tmdb_id": args.get("tmdbid"),
            "douban_id": args.get("doubanid"),
            "start_episode": args.get("start_episode"),
            "total_episode": args.get("total_episode"),
            "quality": args.get("quality"),
            "resolution": args.get("resolution"),
            "effect": args.get("effect"),
            "filter_groups": args.get("filter_groups"),
            "sites": args.get("sites"),
            "username": self._actor_name,
        }

    def _map_subscribe_update(self, args: Dict[str, Any]) -> Dict[str, Any]:
        subscribe_id = int(self._require(args, "subscribe_id"))
        patch = args.get("patch") or {}
        if not isinstance(patch, dict):
            raise MCPError(-32602, "patch 必须是对象")
        mapped = {"subscribe_id": subscribe_id}
        mapped.update(patch)
        return mapped

    def _map_subscribe_state(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "subscribe_id": int(self._require(args, "subscribe_id")),
            "state": self._require(args, "state"),
        }

    def _map_subscribe_refresh(self, args: Dict[str, Any]) -> Dict[str, Any]:
        mode = args.get("mode", "all")
        if mode == "tmdb":
            return {"scheduler_id": "subscribe_tmdb"}
        return {"scheduler_id": "subscribe_refresh"}

    def _map_subscribe_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if args.get("subscribe_id") is not None:
            return {"subscribe_id": int(args["subscribe_id"])}
        return {"scheduler_id": "subscribe_search"}

    def _map_download_add(self, args: Dict[str, Any]) -> Dict[str, Any]:
        mapped = {
            "torrent": self._require(args, "torrent"),
            "downloader": args.get("downloader"),
            "save_path": args.get("save_path"),
        }
        if args.get("media") is not None:
            mapped["media"] = args["media"]
        return mapped

    def _map_download_modify(self, args: Dict[str, Any], action: str) -> Dict[str, Any]:
        return {
            "hash": self._require(args, "hash"),
            "action": action,
            "downloader": args.get("downloader"),
        }

    def _map_media_seasons_result(
        self, normalized: Dict[str, Any], _: Dict[str, Any]
    ) -> Dict[str, Any]:
        data = normalized.get("data")
        if isinstance(data, dict):
            season_info = data.get("season_info")
            if season_info is not None:
                return {
                    "success": normalized.get("success", True),
                    "message": normalized.get("message"),
                    "data": season_info,
                }
        return normalized

    def _postprocess_paginated_result(
        self, normalized: Dict[str, Any], args: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not normalized.get("success", False):
            return normalized

        data = normalized.get("data")
        if isinstance(data, list):
            return {
                "success": True,
                "message": normalized.get("message"),
                "data": self._slice(data, self._limit(args), self._offset(args)),
            }
        if isinstance(data, dict) and "results" in data and isinstance(data.get("results"), list):
            items = data.get("results") or []
            data["results"] = items[self._offset(args): self._offset(args) + self._limit(args)]
            data["limit"] = self._limit(args)
            data["offset"] = self._offset(args)
            data["returned"] = len(data["results"])
            return {
                "success": True,
                "message": normalized.get("message"),
                "data": data,
            }
        return normalized

    def _fallback_dashboard_summary(self, _: Dict[str, Any]) -> Dict[str, Any]:
        from app.api.endpoints.dashboard import (
            cpu,
            downloader,
            memory,
            schedule,
            statistic,
            storage,
        )

        return self._success(
            {
                "statistic": statistic(),
                "storage": storage(),
                "downloader": downloader(),
                "schedule": schedule(),
                "cpu": cpu(),
                "memory": memory(),
            }
        )

    def _fallback_dashboard_processes(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from app.api.endpoints.dashboard import processes

        items = self._jsonable(processes())
        return self._success(self._paginate(items, args))

    def _fallback_plugin_list(self, args: Dict[str, Any]) -> Dict[str, Any]:
        items = [
            plugin
            for plugin in self._jsonable(PluginManager().get_local_plugins())
            if plugin.get("installed")
        ]
        return self._success(self._paginate(items, args))

    def _fallback_plugin_config(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._success(
            PluginManager().get_plugin_config(self._require(args, "plugin_id"))
        )

    def _fallback_plugin_page(self, args: Dict[str, Any]) -> Dict[str, Any]:
        plugin_id = self._require(args, "plugin_id")
        plugin_instance = PluginManager().running_plugins.get(plugin_id)
        if not plugin_instance:
            return self._success_message(False, f"插件 {plugin_id} 不存在或未加载")
        render_mode, _ = plugin_instance.get_render_mode()
        return self._success(
            {
                "render_mode": render_mode,
                "page": plugin_instance.get_page() or [],
            }
        )

    def _fallback_plugin_reload(self, args: Dict[str, Any]) -> Dict[str, Any]:
        plugin_id = self._require(args, "plugin_id")
        PluginManager().reload_plugin(plugin_id)
        return self._success_message(True, "插件已重载", {"plugin_id": plugin_id})

    def _resolve_user(self, username: Optional[str] = None):
        from app.db import SessionFactory
        from app.db.models.user import User

        db = SessionFactory()
        try:
            if username:
                user = User.get_by_name(db, username)
                if user:
                    return user
            user = db.query(User).filter(User.is_active == True, User.is_superuser == True).first()  # noqa: E712
            if user:
                return user
            user = db.query(User).filter(User.is_active == True).first()  # noqa: E712
            if not user:
                raise RuntimeError("未找到可用的 MoviePilot 用户")
            return user
        finally:
            db.close()

    def _parse_agent_text_result(self, text: str) -> Tuple[bool, Optional[str], Any]:
        payload, prefix = self._extract_json_payload(text)
        if payload is not None:
            if isinstance(payload, dict) and "success" in payload:
                if "data" in payload or "message" in payload:
                    return (
                        bool(payload.get("success")),
                        payload.get("message") or prefix,
                        payload.get("data"),
                    )
                message = payload.get("message") or prefix
                data = {k: v for k, v in payload.items() if k not in {"success", "message"}}
                return bool(payload.get("success")), message, data or None
            if isinstance(payload, dict) and payload.get("error"):
                return False, str(payload.get("error")), None
            return True, prefix or None, payload

        stripped = text.strip()
        return (not self._looks_like_error(stripped)), stripped or None, None

    @staticmethod
    def _extract_json_payload(text: str) -> Tuple[Optional[Any], Optional[str]]:
        stripped = text.strip()
        if not stripped:
            return None, None

        try:
            return json.loads(stripped), None
        except Exception:
            pass

        parts = [part.strip() for part in stripped.split("\n\n") if part.strip()]
        if len(parts) > 1:
            tail = parts[-1]
            try:
                return json.loads(tail), "\n\n".join(parts[:-1])
            except Exception:
                return None, None
        return None, None

    @staticmethod
    def _looks_like_error(text: str) -> bool:
        lowered = text.lower()
        keywords = [
            "错误",
            "失败",
            "无效",
            "异常",
            "not found",
            "error",
            "invalid",
            "denied",
            "forbidden",
        ]
        return any(keyword in lowered for keyword in keywords)

    def _build_endpoint_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/mcp"))

    def _build_authorization_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/oauth/authorize"))

    def _build_token_url(self) -> str:
        return self._absolute_url(self._plugin_api_path("/oauth/token"))

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

    @staticmethod
    def _jsonable(data: Any) -> Any:
        return jsonable_encoder(data)

    @staticmethod
    def _require(args: Dict[str, Any], key: str) -> Any:
        value = args.get(key)
        if value is None or value == "":
            raise MCPError(-32602, f"缺少参数: {key}")
        return value

    @staticmethod
    def _normalize_media_type(value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        mapping = {
            "电影": "movie",
            "电视剧": "tv",
            "media": None,
            "collection": None,
            "movie": "movie",
            "tv": "tv",
            "all": "all",
        }
        return mapping.get(str(value).lower(), value)

    def _page_from_limit_offset(self, args: Dict[str, Any], page_size: int = 100) -> int:
        if args.get("page") is not None:
            return max(1, int(args.get("page", 1)))
        offset = self._offset(args)
        return max(1, (offset // page_size) + 1)

    @staticmethod
    def _limit(args: Dict[str, Any]) -> int:
        return max(1, min(int(args.get("limit", 20)), 200))

    @staticmethod
    def _offset(args: Dict[str, Any]) -> int:
        return max(0, int(args.get("offset", 0)))

    def _slice(self, items: List[Any], limit: int, offset: int) -> Dict[str, Any]:
        paged = items[offset:offset + limit]
        return {
            "items": paged,
            "limit": limit,
            "offset": offset,
            "returned": len(paged),
        }

    def _paginate(self, items: List[Any], args: Dict[str, Any]) -> Dict[str, Any]:
        return self._slice(items, self._limit(args), self._offset(args))

    def _success(self, data: Any) -> Dict[str, Any]:
        if isinstance(data, schemas.Response):
            return {
                "success": data.success,
                "message": data.message,
                "data": self._jsonable(data.data),
            }
        if isinstance(data, dict) and {"success", "message", "data"} <= set(data.keys()):
            return {
                "success": bool(data.get("success")),
                "message": data.get("message"),
                "data": self._jsonable(data.get("data")),
            }
        return {
            "success": True,
            "message": None,
            "data": self._jsonable(data),
        }

    def _success_message(self, success: bool, message: str, data: Any = None) -> Dict[str, Any]:
        return {
            "success": success,
            "message": message,
            "data": self._jsonable(data),
        }
