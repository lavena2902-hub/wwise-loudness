# Wwise 响度与增益管理器

用于 Windows 的 Wwise 批量响度管理工具。

**[下载最新版](https://github.com/lavena2902-hub/wwise-loudness/releases/latest)** · [使用说明](使用说明.md)

- 支持 Sound、音乐 Work Unit、Event、Music Track / Segment / Container。
- 支持 Max LUFS-M 和综合 LUFS-I，以文件测量和完整静态路由增益计算 Output。
- 默认只降低超标项，响度调整只写对象 Make-Up Gain。
- 写入后回读验证，自动生成受影响 Event 的 Excel 名单，支持撤回最近一次写入。
- 支持检查更新、下载校验、自动安装和重启。

## 安装与更新

从 Releases 下载 Windows x64 完整 ZIP，解压到可写目录，运行其中的 EXE，保留 `_internal` 文件夹完整。首次需要安装一次包含更新入口的版本；此后点击顶部「检查更新」即可下载并安装新版。

程序已预设本仓库作为更新源。更新保留撤回快照、最近名单与历史记录。详细操作见使用说明。

Output 沿用完整静态增益与路由链估算；实时多轨混音、裁切、淡入淡出、RTPC / State 和效果器处理结果需在 Wwise 中核对。

## 发布新版本

修改 `source/` 中的程序源码，更新 `CHANGELOG.md`，推送新版本标签（例如 `v1.0.1`），或在 Actions 中手动运行 **Build Windows update** 并填写三段数字版本号。

工作流会构建 Windows 程序并生成 Release 草稿。检查附件后发布为正式版，使用者即可检查到更新。每个版本需同时提供 `X3-Wwise-版本号-windows-x64.zip` 与 `update.json`，使用构建生成的同一套附件。

请使用新的、更高的版本号；发布后不要重新替换同版本附件。
