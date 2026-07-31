# MoviePilot MCP Server

将 MoviePilot 的内置 Agent 工具通过 **MCP（Model Context Protocol）** 暴露给 ChatGPT、Codex、VS Code Copilot 和 Claude Desktop 等客户端，让 AI 助手可以直接查询订阅、管理下载、整理媒体库等。

---

## 功能概述

- **OAuth 2.0 授权服务器**：完整实现 RFC 6749 授权码流程 + PKCE（RFC 7636），支持动态客户端注册（RFC 7591）
- **MCP 代理**：完成 OAuth 鉴权后，将 MCP JSON-RPC 请求原样转发至 MoviePilot 内置的 `/api/v1/mcp`
- **OpenAPI 代码保留**：相关实现暂未注册为对外路由，当前接入方式为 MCP
- **动态工具集**：与 MoviePilot 内置 MCP 同步，工具数量随 MoviePilot 版本、音频配置和已安装插件变化
- **写操作开关**：可在插件配置中关闭写操作，只读工具仍可正常使用
- **兼容多客户端**：ChatGPT App、Codex、VS Code Copilot 和 Claude Desktop

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

## 在 Codex 中使用

Codex 添加远程 MCP 后不会自动打开 OAuth 授权页，需要再执行一次显式登录：

```bash
codex mcp add moviepilot \
  --url https://你的域名/api/v1/plugin/MoviePilotMCP/mcp
codex mcp login moviepilot
codex mcp list
```

如果已经通过 `config.toml` 或 Codex 设置页面添加了服务器，只需执行：

```bash
codex mcp login moviepilot
```

桌面版也可以在 MCP 服务器详情中点击 **Authenticate**。授权完成后，如果当前任务仍看不到
MoviePilot 工具，请新建任务或重新启用该 MCP，让 Codex 刷新工具列表。搜索种子等慢工具建议在
Codex 配置中设置 `tool_timeout_sec = 300` 或更高。

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

插件保留了 OpenAPI 代码，但当前版本暂未注册 OpenAPI 路由。请使用上面的 MCP
JSON-RPC 端点接入 ChatGPT、Codex、VS Code Copilot 或其他 MCP 客户端。

---

## 可用工具列表

插件不再维护固定的工具数量和静态清单。MCP 客户端通过 `tools/list` 获取当前列表，
列表来源于 MoviePilot 内置 `/api/v1/mcp`，并会受到以下因素影响：

- MoviePilot 版本中的内置工具变化；
- `send_voice_message` 等能力开关；
- 已安装插件提供的 Agent 工具；
- 内置 MCP 对高风险工具的隐藏规则。

当前源码包含 81 个基础工具类。内置 MCP 还会追加 `send_local_file`，音频输出开启时
追加 `send_voice_message`，并隐藏 `execute_command`、`search_web`、`edit_file`、
`write_file` 和 `read_file`。因此 81 不是最终对外工具数。

关闭「启用写操作工具」后，插件会根据运行时工具的 `write` 标签过滤工具列表，并在
调用阶段再次拦截写工具。这样可以覆盖 MoviePilot 内置工具和其他插件新增的写工具。

---

## 安全说明

- OAuth 授权页需输入 **超级管理员**账号密码，授权会话仅保留 **2 分钟**，过期后新授权需重新登录
- 已发放的 `access_token` 有效期 **1 小时**，`refresh_token` 有效期 **30 天**
- 建议在不需要写操作时关闭「启用写操作工具」
- 静态 Token 模式绕过 OAuth 流程，请妥善保管 Token，不建议在生产环境启用

---

## 版本历史

见 [package.v2.json](../../package.v2.json) 中的 `history` 字段。
