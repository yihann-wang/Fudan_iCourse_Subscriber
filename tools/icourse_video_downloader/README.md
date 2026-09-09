# 兼容下载入口

`downloader.py` 转发到 `src.pipeline`，与 Mac App 共用下载、转录和整课一次总结流程。

新用户请从仓库根目录按 [Mac 安装指南](../../docs/mac-install.md)安装，再阅读[使用说明](../../docs/usage.md)。命令行建议使用 `python -m src.cli run`。当前目录的旧 `.env` 仍可读取；根目录 `.env` 作为回退，显式 `--env-file` 可指定配置文件。
