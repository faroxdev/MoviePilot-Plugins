# MoviePilot 插件仓库

这是一个 MoviePilot 第三方插件仓库，目前包含 `Emby异步图片刮削` 和
`下载器115秒传` 插件。

仓库结构参考官方插件市场：

- `plugins.v2/embyasyncimages/`：Emby 异步图片刮削插件代码目录。
- `plugins.v2/downloader115fastupload/`：下载器任务完成后尝试 115 秒传插件目录。
- `package.v2.json`：MoviePilot 插件市场读取的插件元数据，版本号需要与代码中的 `plugin_version` 保持一致。
- `package.json`：V1 插件元数据占位，本仓库当前不提供 V1 插件。

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

## 下载器115秒传

插件通过 MoviePilot 的统一下载器接口定时监控下载任务，支持 qBittorrent、Transmission
等 MP 已适配的下载器。任务下载完成并稳定后，调用
[`fake115uploader`](https://github.com/orzogc/fake115uploader) 的 `-f` 模式尝试 115
秒传。只有整个任务全部秒传成功，插件才会通过 MoviePilot 删除对应种子和数据；
任何文件失败或进程异常都会保留种子与本地文件。

### 部署前准备

MP V3 本地安装请优先使用原生本地仓库功能，Release 提供包含二进制的
`*_localrepo.zip`。无需公开仓库或执行 Docker 修复脚本，具体步骤见
[安装说明](plugins.v2/downloader115fastupload/README.md)。

插件 Release 已内置从固定上游提交构建的 Linux amd64 和 arm64 版本
`fake115uploader`。插件会根据 MoviePilot 容器架构自动选择并补充执行权限，不需要在
容器中安装 Go，也通常不需要填写“fake115uploader 路径”。

1. 在插件中直接填写 115 Cookie；或者准备 `fake115uploader.json`，填入 Cookie 后挂载进
   MoviePilot 容器并填写配置文件路径。两种方式任选一种。
2. 确保 MoviePilot 容器可以访问下载路径；不同容器路径应在 MP 下载器设置中配置路径映射。
3. 在插件配置中选择下载器、填写配置文件路径和 115 目标 CID。

直接从源码目录以本地插件方式运行时不会生成内置二进制。此时可在相同环境执行
`go install github.com/orzogc/fake115uploader@master`，再将生成的程序路径填入插件；
也可等待 GitHub Actions 生成插件 Release 后从 MoviePilot 插件市场安装。非 Linux 或
非 amd64/arm64 环境同样需要填写外部二进制路径。

首次启用默认只处理插件观察到的新完成任务，不会删除下载器中既有的做种任务。需要处理
历史完成任务时，开启一次“处理已有完成任务”。可使用分类、包含标签和排除标签缩小范围。
秒传输出、失败原因和删除结果会进入 MoviePilot 日志，最近 300 条结果也会显示在插件页。
秒传失败任务会使用独立计时器自动重试，默认间隔为 3600 秒（1 小时），可在插件配置中调整；
下载扫描周期不会影响这个重试间隔。默认不限制失败次数，会持续保留任务并按该间隔重试。
多文件种子会保存每个失败文件及其目标 115 CID；后续自动重试只提交失败文件，不会再次
提交本轮已经秒传成功的文件。

## 发布

修改插件后，请同步更新插件代码中的 `plugin_version`，以及
[package.v2.json](package.v2.json) 中对应插件的 `version` 和 `history`。

推送到 GitHub 后，可在 MoviePilot 第三方插件市场中添加本仓库地址使用。
