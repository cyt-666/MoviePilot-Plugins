import importlib
import inspect
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder

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


class MoviePilotMCP(_PluginBase):
    """
    MoviePilot ChatGPT MCP 薄包装插件
    """

    plugin_name = "MoviePilot MCP Server"
    plugin_desc = "MoviePilot v2.10.4 的 ChatGPT 外部 MCP 薄包装层"
    plugin_icon = "https://raw.githubusercontent.com/cyt-666/MoviePilot-Plugins/main/icons/moviepilotmcp.svg"
    plugin_version = "0.2.3"
    plugin_author = "Codex"
    author_url = "https://wiki.movie-pilot.org/"
    plugin_config_prefix = "moviepilotmcp_"
    plugin_order = 2
    auth_level = 1

    _protocol_version = "2024-11-05"
    _server_name = "moviepilot-chatgpt-wrapper"
    _agent_manager_candidates = [
        ("app.agent.tools.manager", "MoviePilotToolsManager"),
    ]

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._mcp_token = ""
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
        self._mcp_token = (config.get("mcp_token") or "").strip()

        if not self._mcp_token:
            self._mcp_token = self._generate_token()

        self.update_config(
            {
                "enabled": self._enabled,
                "mcp_token": self._mcp_token,
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
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        endpoint_url = self._build_endpoint_url()
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
                                            "text": "该插件是 MoviePilot 内置 Agent 工具的 ChatGPT 外部 MCP 包装层：对外提供独立入口与独立密钥，对内优先复用内置 Agent tools。",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "show_mcp_token",
                                            "label": "显示 Token 明文",
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
                                            "label": "MCP Token",
                                            "placeholder": "留空时自动生成",
                                            "type": "{{ show_mcp_token ? 'text' : 'password' }}",
                                            "hint": "首次安装后可直接复制该 Token 给 VS Code 或 ChatGPT Connector 使用。",
                                            "persistentHint": True,
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": "仅支持 tools，不开放 resources/prompts；不复用 MoviePilot 全局 API Key；默认裁剪高风险工具。",
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
            "enable_write_tools": True,
            "endpoint_url": endpoint_url,
            "show_mcp_token": False,
        }

    def get_page(self) -> List[dict]:
        self._refresh_agent_catalog(force=True)
        enabled_tools = self._available_tools()
        endpoint_url = self._build_endpoint_url()
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

        self._verify_mcp_token(request)

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
                result = await self._handle_tools_call(params)
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

    async def _handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        tool_name = params.get("name")
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

    def _verify_mcp_token(self, request: Request) -> None:
        expected = self._mcp_token.strip()
        if not expected:
            raise HTTPException(status_code=503, detail="MCP token 未配置")

        authorization = request.headers.get("authorization") or ""
        if authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1].strip()
        else:
            token = (request.query_params.get("mcp_token") or "").strip()

        if not token:
            raise HTTPException(status_code=401, detail="缺少 MCP token")
        if token != expected:
            raise HTTPException(status_code=403, detail="MCP token 无效")

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
            "dashboard.summary",
            "获取系统统计、存储、下载器、调度、CPU 和内存摘要。",
            self._schema({}),
            internal_names=["query_dashboard_summary", "query_dashboard", "query_system_summary"],
            fallback_handler=self._fallback_dashboard_summary,
        )
        self._register_agent_tool(
            "dashboard.processes",
            "获取 MoviePilot 所在主机的进程概览。",
            self._schema({"limit": limit_schema, "offset": offset_schema}),
            internal_names=["query_processes", "query_dashboard_processes"],
            fallback_handler=self._fallback_dashboard_processes,
        )
        self._register_agent_tool(
            "media.search",
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
            "media.detail",
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
            "media.recognize",
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
            "media.seasons",
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
            "media.discover",
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
            "media.recommend",
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
            "subscribe.list",
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
            "subscribe.detail",
            "按订阅 ID 获取订阅详情。",
            self._schema({"subscribe_id": {"type": "integer"}}, required=["subscribe_id"]),
            internal_names=["query_subscribe_detail", "get_subscribe_detail"],
            arg_mapper=self._map_subscribe_detail,
        )
        self._register_agent_tool(
            "subscribe.create",
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
            "subscribe.update",
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
            "subscribe.set_state",
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
            "subscribe.refresh",
            "刷新订阅或 TMDB 信息。",
            self._schema({"mode": {"type": "string", "enum": ["all", "tmdb"], "default": "all"}}),
            internal_names=["run_scheduler", "refresh_subscribes"],
            arg_mapper=self._map_subscribe_refresh,
            is_write=True,
        )
        self._register_agent_tool(
            "subscribe.search",
            "触发搜索全部订阅或指定订阅。",
            self._schema({"subscribe_id": {"type": "integer"}}),
            internal_names=["search_subscribe", "run_scheduler"],
            arg_mapper=self._map_subscribe_search,
            is_write=True,
        )
        self._register_agent_tool(
            "download.list",
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
            "download.clients",
            "获取可用下载器列表。",
            self._schema({}),
            internal_names=["query_downloaders"],
        )
        self._register_agent_tool(
            "download.add",
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
            "download.start",
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
            "download.stop",
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
            "history.download",
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
            "history.transfer",
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
            "mediaserver.clients",
            "获取可用媒体服务器列表。",
            self._schema({}),
            internal_names=["query_mediaservers", "query_media_servers"],
        )
        self._register_agent_tool(
            "mediaserver.library",
            "获取媒体库列表。",
            self._schema(
                {"server": {"type": "string"}, "page": {"type": "integer", "minimum": 1, "default": 1}}
            ),
            internal_names=["query_library", "query_media_library"],
        )
        self._register_agent_tool(
            "mediaserver.latest",
            "获取最近入库内容。",
            self._schema(
                {"server": {"type": "string"}, "page": {"type": "integer", "minimum": 1, "default": 1}}
            ),
            internal_names=["query_library_latest"],
        )
        self._register_agent_tool(
            "mediaserver.playing",
            "获取正在播放内容。",
            self._schema(
                {"server": {"type": "string"}, "page": {"type": "integer", "minimum": 1, "default": 1}}
            ),
            internal_names=["query_library_playing", "query_now_playing"],
        )
        self._register_agent_tool(
            "mediaserver.exists_local",
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
            "mediaserver.not_exists",
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
            "site.list",
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
            "site.detail",
            "查询站点详情。",
            self._schema({"site_id": {"type": "integer"}}, required=["site_id"]),
            internal_names=["query_site_detail", "get_site_detail"],
        )
        self._register_agent_tool(
            "site.test",
            "测试站点连通性。",
            self._schema({"site_id": {"type": "integer"}}, required=["site_id"]),
            internal_names=["test_site"],
            arg_mapper=lambda args: {"site_identifier": self._require(args, "site_id")},
        )
        self._register_agent_tool(
            "site.resource",
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
            "site.userdata",
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
            "workflow.list",
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
            "workflow.detail",
            "查询工作流详情。",
            self._schema({"workflow_id": {"type": "integer"}}, required=["workflow_id"]),
            internal_names=["query_workflow_detail", "get_workflow_detail"],
        )
        self._register_agent_tool(
            "workflow.create",
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
            "workflow.update",
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
            "workflow.run",
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
            "workflow.start",
            "启用工作流。",
            self._schema({"workflow_id": {"type": "integer"}}, required=["workflow_id"]),
            internal_names=["start_workflow"],
            is_write=True,
        )
        self._register_agent_tool(
            "workflow.pause",
            "停用工作流。",
            self._schema({"workflow_id": {"type": "integer"}}, required=["workflow_id"]),
            internal_names=["pause_workflow"],
            is_write=True,
        )
        self._register_agent_tool(
            "plugin.list",
            "查询已安装插件列表。",
            self._schema({"limit": limit_schema, "offset": offset_schema}),
            internal_names=["query_installed_plugins"],
            result_mapper=self._postprocess_paginated_result,
            fallback_handler=self._fallback_plugin_list,
        )
        self._register_agent_tool(
            "plugin.config",
            "读取指定插件配置。",
            self._schema({"plugin_id": {"type": "string"}}, required=["plugin_id"]),
            internal_names=[],
            fallback_handler=self._fallback_plugin_config,
        )
        self._register_agent_tool(
            "plugin.page",
            "读取指定插件详情页数据。",
            self._schema({"plugin_id": {"type": "string"}}, required=["plugin_id"]),
            internal_names=[],
            fallback_handler=self._fallback_plugin_page,
        )
        self._register_agent_tool(
            "plugin.reload",
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
        path = f"{settings.API_V1_STR}/plugin/{self.__class__.__name__}/mcp"
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
