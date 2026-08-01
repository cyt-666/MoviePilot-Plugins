# TraktSync

TraktSync 将单一管理员 Trakt 账户的 Watchlist 和已选择的个人列表作为 MoviePilot 订阅来源，并提供只读榜单、个人数据与播出日历 MCP 工具。

接口语义以 Trakt 当前官方契约为准：[Sync](https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/sync/index.ts)、[电影](https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/movies/index.ts)、[剧集](https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/shows/index.ts)、[日历](https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/calendars/index.ts) 和 [个人列表](https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/users/subroutes/userLists.ts)。

## 同步行为

- 定时任务先读取 `/sync/last_activities`，未变化的来源不会重复拉取；活动接口失败时执行完整来源刷新，并继续跳过已完成条目。
- 手动“立即同步”忽略活动时间并强制刷新远程来源，但只处理新增、失败或尚未完成的电影和整剧。
- 从旧版升级时会将既有同步历史迁移到新的来源状态，避免首次运行重新处理全部 Watchlist；旧订阅会按兼容媒体 ID 回退检查。
- 每个来源按“来源类型、列表 ID、媒体类型、Trakt ID”独立记录处理状态。跨来源重复条目会分别记为已处理，但 MoviePilot 中只会存在一份订阅。
- 从 Trakt 移除条目只清理该来源的处理状态，不删除 MoviePilot 订阅；重新加入后可以再次检查。
- 季和单集可由 MCP 浏览，但不会创建 MoviePilot 订阅。
- 识别或订阅失败的条目保留为待重试状态。同步历史只用于展示，不参与运行时去重；“已存在”不会重复追加相同来源的历史。
- 日志会记录手动任务入队、同步模式、各来源统计、新增订阅和最终汇总，不输出账户 UUID、Token 或请求头。

## MCP 工具

- `get_trakt_lists`：公开查询 popular、trending、anticipated、watched、collected 和电影 boxoffice；recommended 需要管理员身份与 OAuth。
- `get_trakt_personal_data`：管理员查询 watchlist、collection、history、up_next 和 stats。
- `get_trakt_custom_lists`：管理员查询个人列表目录及 movie、show、season、episode 条目，并显示 `selected_for_sync`。
- `get_trakt_calendar`：查询 `my` 或 `all` 的 shows、movies、new_shows、season_premieres、finales 和 dvd 日历。公开日历允许普通 Agent 查询；个人日历要求管理员和 OAuth。个人剧集结果附带 MoviePilot 逐集状态。

四个工具统一返回：

```json
{
  "success": true,
  "meta": {
    "pagination": {},
    "cached": false,
    "stale": false
  },
  "data": []
}
```

响应不会包含 OAuth Token、邮箱、Client Secret 或 Authorization 请求头。

## 缓存

- 公开榜单：6 小时。
- recommended、个人数据、自定义列表条目和日历：15 分钟。
- 自定义列表目录：1 小时。

缓存按规范化端点和参数保存，个人缓存键包含账户 UUID。实时请求失败时，如存在旧数据，会返回旧缓存并在 `meta.stale` 标记为 `true`。切换 Trakt 账户会清理旧账户缓存、列表选择和同步状态。

## 个人剧集日历

- 详情页默认展示 MoviePilot 时区当天起未来 14 天的个人剧集，按本地日期分组。
- 状态优先级依次为已入库、下载中、待入库、未播出、已订阅和缺失；单个状态源失败时保留旧状态或显示“状态未知”，不会误标为缺失。
- 订阅和下载任务批量读取，媒体库按唯一 TMDB 剧集查询，最大并发数为 4。
- 日历保持只读，不会自动创建或删除 MoviePilot 订阅。
- 启用插件后每小时独立刷新一次；首次启用且没有快照时自动预取，也可以在详情页手动刷新。

## 详情页操作

详情页只读取本地状态。远程操作通过 MoviePilot Bearer 鉴权 API 执行：

- `POST /sync_now`
- `POST /cache/refresh`
- `POST /calendar/refresh`
- `POST /custom_lists/refresh`
- `POST /custom_lists/select`

自定义列表默认不选择，新增探索榜单开关默认关闭。
