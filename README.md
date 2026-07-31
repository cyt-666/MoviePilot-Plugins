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

### Trakt WatchList 同步 `v0.5.0`

Trakt 观看列表、自定义列表订阅同步与完整榜单浏览插件。

- **增量订阅同步**：使用 Trakt `last_activities` 仅同步发生变化的 Watchlist 和已选自定义列表；移除来源条目不会删除 MoviePilot 订阅
- **完整榜单探索**：热门、趋势、待映、观看、收藏、推荐，以及电影周末票房
- **MCP 查询**：`get_trakt_lists`、管理员工具 `get_trakt_personal_data` 和 `get_trakt_custom_lists`
- **账户隔离缓存**：公开榜单缓存 6 小时，推荐、个人数据和列表条目缓存 15 分钟，自定义列表目录缓存 1 小时；实时失败可回退陈旧缓存
- **详情页管理**：查看账户和同步状态，立即同步、刷新缓存、刷新并选择自定义列表订阅源

#### 使用方法

1. 在 [Trakt 网站](https://trakt.tv/settings/applications) 注册应用，redirect_url 填写 `urn:ietf:wg:oauth:2.0:oob`
2. 获取 Client ID 和 Client Secret
3. 在插件配置中填写 Client ID 和 Client Secret，保存
4. 在插件详情页查看设备授权地址和代码，完成单一管理员 Trakt 账户绑定
5. 在插件配置中启用所需探索榜单；在详情页刷新并选择需要同步的自定义列表

详细能力、缓存和同步语义见 [plugins.v2/traktsync/README.md](plugins.v2/traktsync/README.md)。

---

### 媒体库封面生成 `v0.10.3`

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
