# MoviePilot-Plugins
MoviePilot插件市场：https://github.com/cyt-666/MoviePilot-Plugins

## 插件列表

### MoviePilot MCP Server

将 MoviePilot v2.10.4 的内置 Agent 工具通过 **OAuth 2.0 + MCP（Model Context Protocol）** 暴露给 ChatGPT、VS Code Copilot、Claude 等 AI 客户端。

- 完整 OAuth 2.0 授权码流程 + PKCE，支持动态客户端注册
- 转发至 MoviePilot 内置MCP，覆盖全部 51 个工具
- 可一键关闭写操作工具，保护媒体库安全

详见 [plugins.v2/moviepilotmcp/README.md](plugins.v2/moviepilotmcp/README.md)

---

### Trakt 同步

因为个人追剧app用的是trakt，并且emby和plex可以同步库和播放信息到trakt。
因此参考豆瓣同步插件写了这个trakt同步插件，当前只有一个功能，就是将trakt的watch list添加到MP的订阅里。




## 使用方法

调用Trakt的API需要在trakt的网站上注册一个app，注册位置在个人设置里



注册的时候redirt_url填写：`urn:ietf:wg:oauth:2.0:oob`




会给生成clinet id和client secret


装上插件后在插件的配置里填写clinet id和client secret

点击保存


然后去插件日志里找认证链接和认证code完成与自己的trakt账户的绑定即可。

