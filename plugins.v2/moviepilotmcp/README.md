# MoviePilot MCP / OpenAPI Server

将 MoviePilot 的内置 Agent 工具通过 **MCP（Model Context Protocol）** 和 **OpenAPI（REST）** 双协议暴露给 ChatGPT、VS Code Copilot、Swagger UI、Postman 等客户端，让 AI 助手和自动化脚本可以直接查询订阅、管理下载、整理媒体库等。

---

## 功能概述

- **OAuth 2.0 授权服务器**：完整实现 RFC 6749 授权码流程 + PKCE（RFC 7636），支持动态客户端注册（RFC 7591）
- **MCP 代理**：完成 OAuth 鉴权后，将 MCP JSON-RPC 请求原样转发至 MoviePilot 内置的 `/api/v1/mcp`
- **OpenAPI REST 接口**：将 MCP 工具映射为标准 REST API，支持 ChatGPT Actions、Swagger UI、Postman 等 OpenAPI 兼容客户端
- **动态 OpenAPI Spec**：根据可用工具自动生成 OpenAPI 3.0 规范文档
- **51 个工具**：覆盖媒体搜索、订阅管理、下载管理、整理历史、站点管理、消息发送等全部内置 Agent 工具
- **写操作开关**：可在插件配置中关闭写操作，只读工具仍可正常使用
- **兼容多客户端**：ChatGPT App、VS Code Copilot、Claude Desktop、Swagger UI、Postman 等

---

## 前置要求

- MoviePilot **v2.10.4+**（需要内置 `/api/v1/mcp` 端点）
- 对外可访问的域名（`APP_DOMAIN` 或反向代理），AI 客户端需能访问插件的 OAuth 和 MCP 端点

---

## 安装

在 MoviePilot 插件市场搜索 **MoviePilot MCP Server** 并安装，或手动将本目录放入 `plugins/` 目录后重启。

---

## 配置项

| 配置项 | 说明 | 默认值 |
|---|---|---|
| **启用插件** | 是否启用 MCP 包装层 | 关闭 |
| **启用写操作工具** | 关闭后，AI 无法执行订阅、下载、整理等写操作，仅能查询 | 开启 |
| **写入显示名称** | 写操作（如添加订阅）时显示的操作者名称 | `ChatGPT MCP` |
| **内部 MCP 转发超时（秒）** | 等待 MoviePilot 内置工具执行完成的最长时间，搜索种子等慢工具可适当调大 | `600` |
| **兼容静态 Token** | 开启后允许直接用 Token 鉴权，无需走 OAuth 流程（适合脚本调用） | 关闭 |
| **静态 Token** | 兼容静态 Token 模式下使用的令牌（首次启动自动生成） | 自动生成 |

---

## 在 ChatGPT 中使用

1. 在 ChatGPT 的「连接器」或「工具」设置中选择「添加 MCP」
2. 填写 **MCP Endpoint URL**（见插件状态页）：
   ```
   https://你的域名/api/v1/plugin/MoviePilotMCP/mcp
   ```
3. 点击连接，ChatGPT 会自动跳转至 MoviePilot 的 OAuth 授权页
4. 输入 MoviePilot **超级管理员**账号密码（支持 OTP），点击「授权」
5. 授权完成后，ChatGPT 即可使用 MoviePilot 的所有工具

---

## 在 VS Code Copilot 中使用

在工作区的 `.vscode/mcp.json` 中添加：

```json
{
  "servers": {
    "MP": {
      "type": "http",
      "url": "https://你的域名/api/v1/plugin/MoviePilotMCP/mcp"
    }
  }
}
```

VS Code 会自动发现 OAuth 配置并引导授权。

---

## 在 Claude Desktop 中使用

`claude_desktop_config.json` 中添加：

```json
{
  "mcpServers": {
    "moviepilot": {
      "type": "http",
      "url": "https://你的域名/api/v1/plugin/MoviePilotMCP/mcp"
    }
  }
}
```

---

## 兼容静态 Token 模式（可选）

适用于脚本或不支持 OAuth 的客户端。在插件配置中开启「兼容静态 Token」，然后用以下方式调用：

```
Authorization: Bearer <静态Token>
```

或 URL 参数：

```
?token=<静态Token>
```

---

## OpenAPI REST 接口

除 MCP JSON-RPC 外，插件同时提供标准 REST API，可通过 OpenAPI spec 接入。

### 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/openapi.json` | GET | OpenAPI 3.0 规范文档（动态生成） |
| `/openapi/tools` | GET | 列出所有可用工具 |
| `/openapi/tools/{tool_name}` | POST | 调用指定工具 |

所有端点均需 OAuth Bearer Token 鉴权（与 MCP 端点一致）。

### 在 ChatGPT Actions 中使用

1. 在 GPT 编辑器中选择「Actions」->「Create new action」
2. 在「Schema」处选择「Import from URL」，填入插件状态页显示的 **OpenAPI Spec URL**：
   ```
   https://你的域名/api/v1/plugin/MoviePilotMCP/openapi.json
   ```
3. ChatGPT 会自动解析所有工具定义，在对话中即可调用

### 在 Swagger UI 中使用

1. 打开 [Swagger UI](https://petstore.swagger.io/)
2. 填入 OpenAPI Spec URL，点击「Explore」
3. 点击「Authorize」输入 Bearer Token
4. 即可交互式调用所有工具

### REST 调用示例

```bash
# 获取工具列表
curl -H "Authorization: Bearer <token>" \
  https://你的域名/api/v1/plugin/MoviePilotMCP/openapi/tools

# 搜索媒体
curl -X POST \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"title": "Inception", "media_type": "movie"}' \
  https://你的域名/api/v1/plugin/MoviePilotMCP/openapi/tools/search_media

# 响应格式
# {"success": true, "result": "..."}
```

---

## 可用工具列表

### 只读工具（32 个）

`search_media`、`recognize_media`、`query_media_detail`、`get_recommendations`、`get_search_results`、`search_torrents`、`search_person`、`search_person_credits`、`query_subscribes`、`query_subscribe_history`、`query_subscribe_shares`、`query_popular_subscribes`、`query_download_tasks`、`query_downloaders`、`query_transfer_history`、`query_library_exists`、`query_library_latest`、`query_episode_schedule`、`query_sites`、`query_site_userdata`、`query_schedulers`、`query_workflows`、`query_installed_plugins`、`query_plugin_capabilities`、`query_directory_settings`、`query_rule_groups`、`query_custom_identifiers`、`list_directory`、`list_slash_commands`、`browse_webpage`、`test_site`、`query_download_tasks`

### 写操作工具（20 个，可通过开关关闭）

`add_subscribe`、`update_subscribe`、`delete_subscribe`、`search_subscribe`、`add_download`、`modify_download`、`delete_download`、`delete_download_history`、`delete_transfer_history`、`transfer_file`、`scrape_metadata`、`run_scheduler`、`run_workflow`、`run_slash_command`、`update_site`、`update_site_cookie`、`update_custom_identifiers`、`send_message`、`send_voice_message`、`send_local_file`

---

## 安全说明

- OAuth 授权页需输入 **超级管理员**账号密码，授权会话仅保留 **2 分钟**，过期后新授权需重新登录
- 已发放的 `access_token` 有效期 **1 小时**，`refresh_token` 有效期 **30 天**
- 建议在不需要写操作时关闭「启用写操作工具」
- 静态 Token 模式绕过 OAuth 流程，请妥善保管 Token，不建议在生产环境启用

---

## 版本历史

见 [package.v2.json](../../package.v2.json) 中的 `history` 字段。
