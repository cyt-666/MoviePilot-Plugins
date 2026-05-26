# MoviePilot-Plugins
MoviePilot插件市场：https://github.com/cyt-666/MoviePilot-Plugins

## 插件列表

### MoviePilot MCP Server `v0.7.1`

将 MoviePilot 内置 Agent 工具通过 **MCP（Model Context Protocol）** 暴露给 ChatGPT、VS Code Copilot、Claude 等 AI 客户端。

- OAuth 2.0 Authorization Code + PKCE 鉴权，支持动态客户端注册
- 转发至 MoviePilot 内置 MCP，覆盖全部内置工具
- 可一键关闭写操作工具，保护媒体库安全

详见 [plugins.v2/moviepilotmcp/README.md](plugins.v2/moviepilotmcp/README.md)

---

### Trakt WatchList 同步 `v0.4.4`

Trakt 观看列表同步与榜单浏览插件。

- **WatchList 同步**：将 Trakt watchlist 自动同步为 MoviePilot 订阅，支持电影和剧集
- **榜单探索**：在探索页面浏览 Trakt 热门、趋势、推荐、待映榜单，支持无限滚动分页
- **MCP 工具**：提供 `get_trakt_recommendations` 工具供智能体调用
- 榜单数据通过 Trakt 公开 API 获取，缓存 6 小时

#### 使用方法

1. 在 [Trakt 网站](https://trakt.tv/settings/applications) 注册应用，redirect_url 填写 `urn:ietf:wg:oauth:2.0:oob`
2. 获取 Client ID 和 Client Secret
3. 在插件配置中填写 Client ID 和 Client Secret，保存
4. 查看插件日志中的认证链接和认证码，完成 Trakt 账户绑定
5. 在插件配置中启用所需的榜单类型，即可在探索页面的 Trakt Tab 中浏览

---

### 媒体库封面生成 `v0.10.2`

为 Emby / Jellyfin 媒体库生成动态或静态封面。

- 支持多种封面样式，自定义标题、字体、颜色
- 自动从媒体库获取内容海报作为封面背景
- 智能字体选择与渲染校验，确保文字可见
- 支持定时自动更新封面

---

### 媒体库服务器通知 AI 版 `v1.8.5`

在媒体库服务器通知的基础上增加媒体删除通知，支持 AI 智能分类。

- 支持 Emby / Jellyfin 媒体服务器事件监听
- 媒体入库、删除、播放等消息推送
- AI 智能分类与 TMDB 信息缓存
- 支持微信、Telegram、Slack 等通知渠道
