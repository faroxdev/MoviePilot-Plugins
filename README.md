# 下载器115秒传

MoviePilot 插件：监控下载器任务，使用 fake115uploader 的 `-f` 模式尝试 115 秒传。
只有整个任务全部秒传成功，才通过 MoviePilot 删除种子和数据；失败时保留任务和文件。

## 安装

在 MoviePilot「插件 → 插件市场设置」中添加仓库：

`https://github.com/faroxdev/MoviePilot-Plugins`

从插件市场安装“下载器115秒传”。MP V2 使用 `1.3.1`；MP V3（`>=3.0.10`）
使用 SDK 专用实现 `2.0.0`，不会回退安装 V2 版本。Release ZIP 内置 Linux amd64/arm64 秒传程序，通常
无需填写程序路径，也无需在容器中安装 Go。

需要本地 ZIP 安装时，下载 Release 中的 `downloader115fastupload_v版本.zip`，
直接上传到兼容当前 MP 版本的“本地插件安装器”。旧安装器在 MP V3 上可能缺少
加载实例登记并调用已移除的备份接口，表现为安装成功但加载失败；请更新安装器
或使用插件市场安装。

## 配置

1. 选择 MoviePilot 中已配置的下载器。
2. 填写 115 Cookie，或填写已挂载到容器的 fake115uploader 配置文件路径。
3. 填写 115 目标 CID，并确认 MP 可以访问下载路径；容器路径差异在 MP 下载器设置中映射。
4. 按需设置分类、包含标签和排除标签，然后启用。

首次启用默认只观察新完成任务。开启“处理已有完成任务”才会处理既有做种任务。
秒传失败默认每小时自动重试一次，间隔可以配置；多文件任务重试时只提交失败文件。
日志、通知和最近 300 条结果可在 MP 中查看。

## 发布与许可

V2 源码位于 `plugins.v2/downloader115fastupload/`，V3 源码位于
`plugins.v3/downloader115fastupload/`。修改版本时，同步更新插件的
`plugin_version` 和对应 `package.v2.json` / `package.v3.json` 的版本及更新日志，
推送后自动创建 Release。
已发布版本不会被自动覆盖。

构建使用固定上游提交，包含 Linux amd64/arm64 二进制、上游 GPLv3 许可证和对应源码。
详细信息见 [构建说明](plugins.v3/downloader115fastupload/FAKE115UPLOADER.md)。
仓库采用 [GPLv3](LICENSE)。
