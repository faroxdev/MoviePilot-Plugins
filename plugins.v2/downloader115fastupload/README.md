# 下载器115秒传：安装说明

## 插件市场安装

在 MoviePilot 插件市场设置中添加本仓库地址：

`https://github.com/faroxdev/MoviePilot-Plugins`

然后从插件市场安装或升级“下载器115秒传”。插件 ZIP 内置 Linux amd64/arm64
的 fake115uploader，通常无需填写程序路径。

## 本地仓库安装（无需公开仓库）

使用 MoviePilot 官方的 `PLUGIN_LOCAL_REPO_PATHS` 功能，由主程序完成安装、
加载登记和 Docker 持久化备份。此方式不需要额外安装脚本。

1. 下载 Release 中的 `downloader115fastupload_v1.3.1_localrepo.zip`。
2. 解压到 MoviePilot 能访问的持久化目录。解压后应包含：

   ```text
   downloader115fastupload-localrepo/
   ├── package.v2.json
   └── plugins.v2/
       └── downloader115fastupload/
           ├── __init__.py
           ├── bin/
           └── third_party/
   ```

3. 在 MP「设定 → 系统 → 进阶设置」的本地插件仓库路径中填写上述仓库根目录。
   Docker 环境填写容器内路径，而非宿主机路径。例如解压到已挂载的 `/config`
   中，可填写 `/config/downloader115fastupload-localrepo`。多个仓库用英文逗号分隔，
   保留已有路径。如果此配置由 Docker 环境变量注入，应在部署配置中修改。
4. 刷新插件市场，从本地来源安装“下载器115秒传”。已安装插件可在来源选项中
   选择该本地仓库，再更新到这一来源，无需卸载或重置配置。
5. 打开插件设置，确认下载器、115 Cookie、目标 CID，再按需启用。

以后升级时，用新版本的本地仓库包更新同一目录，再通过 MP 更新插件。
本地仓库包已经包含二进制，无需自行编译。

## MP V3：本地 ZIP 安装成功但无法加载

旧版第三方“本地插件安装器”可能只更新已安装列表，没有登记新版 MP 要求的
可加载实例，并调用已移除的备份接口。这种情况会出现安装成功、配置页面却返回
404，或容器更新后插件丢失。

请使用上面的原生插件市场或本地仓库方式安装。普通
`downloader115fastupload_v1.3.1.zip` 仍提供给支持该 MP 版本的 ZIP 安装器；
更新秒传插件本身不能修复旧安装器的登记行为。

本插件继续维护既有 V2 实现；MP V3 可以按官方兼容规则回退读取 `package.v2.json`。
插件只有整个任务秒传成功后才删除种子和数据，任何失败都会保留任务和本地文件。

官方参考：[配置参考](https://wiki.movie-pilot.org/zh/configuration)、
[仓库指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Repository_Guide.md)。
