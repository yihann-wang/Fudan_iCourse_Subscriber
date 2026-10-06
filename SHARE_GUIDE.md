# 分享与安装

将本仓库地址发给其他用户即可：

https://github.com/yihann-wang/Fudan_iCourse_Subscriber

对方按 [Mac 安装指南](docs/mac-install.md)自行安装，并使用自己的学校账号、语音 API Key 和笔记 API Key。安装需要 Apple Silicon M 系列、macOS 14.0+。

请分享源码或 GitHub 的源码 ZIP，不要打包自己的 `.env`、设置、日志、课程、模型缓存、`.venv` 或 `~/Library/Application Support`。本机生成的 `.app` 包含本机运行环境路径，不是便携安装包。

GitHub 发布副本中的 `src/elearning_helper/config.json` 是通用示例，会随 wheel 安装。接收者应先在 eLearning 的“保存位置与课程设置…”中替换示例课程 ID、名称和保存位置。修改过内置配置的本地工作副本在分享前也须恢复为通用示例。

0.6.10 将密码与 API Key 直接保存在本机 `settings.json`，eLearning 个人课程配置保存在 `elearning.json`；两者都不属于分享内容。源码、测试、通用示例及文档可以发布，课程和诊断记录留在本机。

当前源码版本为 0.6.10。GitHub 提交用于追踪源码，源码推送不会自动创建版本标签或便携 App。正式发布流程见[开发与发布说明](docs/development.md)。安装占用、重复入口和旧验证环境的清理见[空间管理](docs/mac-install.md#安装结构与空间管理)。
