# Embedded fake115uploader

MoviePilot 插件 Release 构建时会从以下固定源码提交交叉编译静态 Linux 二进制：

- 项目：<https://github.com/orzogc/fake115uploader>
- 提交：`e65268c005dc997ab4365c47cb6968dca2ea0f44`
- 目标：`linux/amd64`、`linux/arm64`
- 构建参数：`CGO_ENABLED=0`、`-trimpath`、`-ldflags="-s -w"`

二进制仅存在于 GitHub Release 插件包中，不提交到本仓库。为保留上游 GPLv3 许可及
对应源码，Release 包同时包含：

- `third_party/fake115uploader.LICENSE`
- `third_party/fake115uploader-source.tar.gz`

插件启动时根据 `platform.machine()` 选择 `bin/` 中的对应文件，并补充可执行权限。
用户明确配置的外部程序路径始终拥有更高优先级。
