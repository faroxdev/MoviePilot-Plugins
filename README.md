# MoviePilot NextFind 插件仓库

这是一个 MoviePilot 第三方插件仓库，目前包含 `NextFind订阅同步` 插件。

仓库结构参考官方插件市场：

- `plugins.v2/nextfindsubsync/`：插件代码目录，目录名为插件类名 `NextFindSubSync` 的小写。
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

## 发布

修改插件后请同步更新：

- [plugins.v2/nextfindsubsync/__init__.py](plugins.v2/nextfindsubsync/__init__.py) 中的 `plugin_version`
- [package.v2.json](package.v2.json) 中的 `version` 和 `history`

推送到 GitHub 后，可在 MoviePilot 第三方插件市场中添加本仓库地址使用。
