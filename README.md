# MoviePilot NextFind 插件仓库

这是一个 MoviePilot 第三方插件仓库，目前包含 `NextFind订阅同步` 和 `Emby异步图片刮削` 插件。

仓库结构参考官方插件市场：

- `plugins.v2/nextfindsubsync/`：插件代码目录，目录名为插件类名 `NextFindSubSync` 的小写。
- `plugins.v2/embyasyncimages/`：Emby 异步图片刮削插件代码目录。
- `package.v2.json`：MoviePilot 插件市场读取的插件元数据，版本号需要与代码中的 `plugin_version` 保持一致。
- `package.json`：V1 插件元数据占位，本仓库当前不提供 V1 插件。

## NextFind订阅同步

插件用于拦截 MoviePilot 自带订阅处理，并定时将 MoviePilot 订阅同步到 NextFind。

### 功能

- 配置 NextFind OpenAPI 地址和 API Key。
- 定时或手动同步 MoviePilot 订阅到 NextFind。
- 通过 `X-API-Key` 请求头调用 NextFind OpenAPI。
- 调用 `POST /subscriptions/add` 添加订阅。
- 订阅缺少 TMDB ID 时，先调用 `GET /search` 尝试匹配。
- 可开启“屏蔽系统订阅”，将 MoviePilot 订阅站点改为 NextFind 虚拟站点，避免默认订阅下载。
- 支持远程命令 `/nextfind_sub_sync`。

### 配置

在 MoviePilot 插件配置页填写：

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 开启定时同步服务 |
| 发送通知 | 同步完成后发送插件通知 |
| 屏蔽系统订阅 | 将订阅站点改为 NextFind 虚拟站点 |
| 立即同步 | 保存配置后立即执行一次 |
| 同步周期 | Cron 表达式，默认 `0 */6 * * *` |
| NextFind OpenAPI 地址 | 可填写 `https://host:port` 或 `https://host:port/api/openapi` |
| NextFind API Key | NextFind OpenAPI 密钥 |
| 排除订阅ID | 多个 ID 用英文逗号分隔 |

### NextFind OpenAPI

插件会调用以下接口：

```text
GET /api/openapi/search
POST /api/openapi/subscriptions/add
Header: X-API-Key: <你的密钥>
```

添加订阅请求体：

```json
{
  "tmdb_id": 12345,
  "title": "资源标题",
  "media_type": "movie"
}
```

`media_type` 会根据 MoviePilot 订阅类型传 `movie` 或 `tv`。

## Emby异步图片刮削

插件用于将 Emby 入库和图片下载拆成两个阶段：Emby 先快速完成识别、文本元数据和 NFO，随后通过 Webhook 通知 MoviePilot，由插件异步补齐缺失图片。

### 工作流程

```text
Emby library.new
  -> MoviePilot 内置 Webhook
  -> 插件延迟合并、去重
  -> MoviePilot 图片刮削链（仅缺失）
  -> 写入本地图片旁车文件
  -> 刷新 Emby 对应 Movie / Series
```

### 安全边界

- 不调用 MP 的完整 `scrape_metadata()`。
- NFO 策略固定为“跳过”。
- 所有图片策略固定为“仅缺失”。
- `overwrite=False`，不会覆盖已有图片。
- 不修改 Emby 文本元数据、媒体文件或媒体路径。
- 默认不下载单集缩略图，避免大量剧集产生过多请求；可在插件设置中开启。
- 电影默认等待 `0` 秒，收到通知后立即处理。
- 电视剧默认等待 `10` 秒；建议在 Emby 通知中开启“按剧集和专辑对通知进行分组”。
- 每个任务按等待时间精确唤醒；周期检查仅负责重启恢复和兜底。
- 失败重试和任务持久化仍然生效，短暂的路径不可见不会直接丢任务。

### Emby Webhook

先在 MoviePilot 中配置并启用 Emby 媒体服务器，然后在 Emby 通知设置中添加 Webhook：

```text
http://MoviePilot地址:3001/api/v1/webhook/?token=MP_API_TOKEN&source=MP中的Emby配置名称
```

只需要订阅“新媒体加入”事件。MoviePilot 会将其解析为 `library.new`。电视剧建议同时开启“按剧集和专辑对通知进行分组”，由 Emby 先完成聚合，插件无需再长时间等待。

### 路径映射

如果 Emby 与 MoviePilot 容器内看到的媒体路径不同，在插件设置中每行填写一条映射：

```text
/cloud/symedia => /media
```

插件按最长前缀优先匹配。MoviePilot 对映射后的目录必须有读取和写入权限，否则无法写入图片。

### 推荐的 Emby 媒体库设置

- 文本元数据和 NFO 继续由 Emby 处理。
- 关闭 Emby 在线图片获取器和“预先下载图像”。
- 关闭入库期间章节图生成与片头检测。
- 保留 Emby 本地图片读取器。
- 插件完成后会刷新对应电影或整部电视剧，让 Emby 读取新生成的本地图片。

## 发布

修改插件后请同步更新：

- [plugins.v2/nextfindsubsync/__init__.py](plugins.v2/nextfindsubsync/__init__.py) 中的 `plugin_version`
- [package.v2.json](package.v2.json) 中的 `version` 和 `history`

推送到 GitHub 后，可在 MoviePilot 第三方插件市场中添加本仓库地址使用。
