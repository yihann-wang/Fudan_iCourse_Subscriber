# Fudan iCourse Subscriber · Mac 版

在 Mac 上下载自己有权访问的复旦 iCourse 课程回放，使用本机 Apple GPU 转录，生成字幕与学习笔记。

**整节课一次总结，正常成功时只调用一次笔记 API。** 保留正常标题和段落，每个课次输出一份 Markdown 笔记。视频可放外置硬盘，文字笔记可留在电脑中。

[安装指南](docs/mac-install.md) · [使用与命令行](docs/usage.md) · [常见问题](docs/troubleshooting.md) · [更新记录](CHANGELOG.md)

## 支持范围

- **Apple Silicon（M 系列）Mac，macOS 14.0 或更新版本**；安装时自动准备 Python 3.13。
- 需要 `uv`、`ffmpeg`，通过 Homebrew 安装即可。首次安装和下载转录模型需要网络。
- 下载课程需要自己的复旦 UIS 账号；生成笔记需要自己的模型服务 API Key。
- Intel Mac 不在当前安装器支持范围内。保留 CPU/CUDA 后端，但 Windows/NVIDIA 未完成本版硬件验证。
- 这是**在本机安装的桌面版**，不是签名、公证后的独立 DMG。请分享源码或本仓库地址，让每位用户各自安装。

MLX 的系统要求见[官方安装说明](https://ml-explore.github.io/mlx/build/html/install.html)。本地实机验证环境为 Apple M5 Pro、macOS 26.4；其他支持版本由依赖约束和持续集成进一步检查。

## 安装

1. 从 [Homebrew 官网](https://brew.sh/)安装 Homebrew。打开 **Terminal**，执行：

   ```sh
   brew install uv ffmpeg
   ```

2. 下载本仓库：点击 **Code → Download ZIP**，解压。打开解压后的文件夹，双击 **安装 Mac.command**。

   如果 macOS 不允许双击脚本，按[安装指南](docs/mac-install.md#运行安装器)在 Terminal 中运行。也可以使用 Git：

   ```sh
   git clone https://github.com/yihann-wang/Fudan_iCourse_Subscriber.git
   cd Fudan_iCourse_Subscriber
   zsh "安装 Mac.command"
   ```

3. 安装完成后，在 **Finder** 中按 **Command+Shift+G**，输入 `~/Applications`，打开 **iCourse.app**。

安装器会下载依赖。首次使用“准备转录模型”或开始转录时，还会下载默认模型；下载完成后可重复使用。安装本身不会登录学校、生成笔记或发送邮件。

## 第一次使用

在 **设置** 页填写：

| 界面字段 | 如何填写 |
|---|---|
| 学号、统一身份认证密码 | 自己的复旦 UIS 凭据；只转录本地文件时可留空 |
| 笔记服务地址 | 服务商的 API 基础地址 |
| 笔记模型 | 服务商提供的准确模型 ID |
| 笔记服务 API Key | 自己的 API Key；只下载或本地转录时可留空 |
| 本地转录 | 保持“自动选择”，M 系列使用 Apple GPU / MLX |
| 单次输出上限、单次请求最长等待 | 默认 `65536 tokens`、`20 分钟`；所用模型需支持该输出预算 |

例如 DeepSeek 官方 API：地址填 `https://api.deepseek.com`，模型填 `deepseek-v4-flash`，API Key 从自己的 DeepSeek 账户取得。模型名称以[官方文档](https://api-docs.deepseek.com/)为准。

然后在 **任务** 页：

1. 选择“下载并生成笔记”。
2. 填写**课程 ID**；多个用英文逗号分隔。课程 ID 与某一节回放的**课次 ID** 不同，后者填入“指定课次”。
3. 选择“课程保存位置”和“笔记保存位置”。用外置硬盘时，先连接磁盘，再点“选择…”。
4. 第一次建议只填一个课次。普通运行保持两个“重新生成”选项未勾选。
5. 切回“设置”，点击“保存设置”；返回“任务”，点击“开始任务”。

密码和 API Key 保存在 **macOS Keychain**；普通设置保存到当前用户的 Application Support。转录在本机完成；生成笔记会将完整转录文本发送给你配置的模型服务，API 费用由该服务收取。

## 输出是什么

| 位置 | 内容 |
|---|---|
| 课程保存位置 | 按课程整理的 MP4 回放、同名 SRT 字幕 |
| 笔记保存位置 | 按课程整理的 Markdown 笔记、TXT 转录原文 |
| 各目录的隐藏 `.icourse` | 校验记录、恢复缓存、旧笔记历史；不包含 API Key |

每个课次只有一份当前正式笔记，不再生成可见的分章草稿或核对意见。再次运行会复用已完成产物。只想重写笔记时，选择“为本地课程生成笔记”，勾选“只重新生成笔记（复用已有转录）”。

成功请求一次生成全文；空响应、超时或临时服务错误可能触发有限重试，增加 API 用量。输出截断或上下文不足会明确报错，不把半篇笔记标记为完成，也不自动拆分整课。

## 开发与项目来源

[开发说明](docs/development.md)介绍目录、测试、安装包检查和维护方式。GitHub Actions 只运行不使用真实账号的测试，不会自动订阅课程或调用付费笔记 API。当前桌面任务由用户手动启动。

本项目派生自 [LeafCreeper/Fudan_iCourse_Subscriber](https://github.com/LeafCreeper/Fudan_iCourse_Subscriber)，保留 Git 历史与来源说明。本分支基于早期代码完成 Mac 改造，与上游现在的 V2 云端订阅、PPT OCR、网页数据库实现不同；这些 V2 功能未集成到此 Mac 版。[来源与版本关系](NOTICE.md)。
