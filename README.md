# Fudan iCourse Subscriber · Mac 版

在 Mac 上下载自己有权访问的复旦 iCourse 课程回放，通过可配置的云端语音接口转录，生成整课学习笔记。

**整节课一次总结，正常成功时只调用一次笔记 API。** 保留正常标题和段落，每个课次输出一份 Markdown 笔记。视频可放外置硬盘，文字笔记可留在电脑中。

支持按课程、课次同时查看下载、转录和笔记进度。关键日志不重复刷屏，失败课次可单独重试。[进度与日志说明](docs/usage.md#查看多门课程的进度)

录像下载在服务器支持时保留断点并续传，通过完整性检查后才交给转录；已成功的课次继续复用。

[安装指南](docs/mac-install.md) · [使用与命令行](docs/usage.md) · [常见问题](docs/troubleshooting.md) · [更新记录](CHANGELOG.md)

## 支持范围

- **Apple Silicon（M 系列）Mac，macOS 14.0 或更新版本**；安装时自动准备 Python 3.13。
- 需要 `uv`、`ffmpeg`，通过 Homebrew 安装即可。安装及云端转录需要网络。
- 下载课程需要自己的复旦 UIS 账号；转录和生成笔记分别使用自己的语音服务、笔记服务 API Key。
- Intel Mac 不在当前安装器支持范围内。本版已移除本地转录；其他平台的命令行没有完成实机验证。
- 这是**在本机安装的桌面版**，不是签名、公证后的独立 DMG。请分享源码或本仓库地址，让每位用户各自安装。

0.5.0 起不再安装或加载 MLX、Whisper、PyTorch 等本地转录组件。Mac 只提取和压缩音频，语音识别由服务端完成。

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

安装器只安装运行依赖，不下载语音模型。安装本身不会登录学校、生成笔记或发送邮件。

## 第一次使用

在 **设置** 页填写：

| 界面字段 | 如何填写 |
|---|---|
| 学号、统一身份认证密码 | 自己的复旦 UIS 凭据；只转录本地文件时可留空 |
| 笔记服务地址 | 服务商的 API 基础地址 |
| 笔记模型 | 服务商提供的准确模型 ID |
| 笔记服务 API Key | 自己的 API Key；只下载或单独转录音视频时可留空 |
| 语音服务 | 按服务商文档选择“OpenAI 兼容音频接口”或“百炼录音文件接口（异步识别）” |
| 百炼服务地址 | 可编辑，默认 `https://dashscope.aliyuncs.com/api/v1`；与 `/compatible-mode/v1` 不是同一种接口 |
| 百炼语音模型、API Key | 模型可自由输入完整 ID，下拉里的 `fun-asr`、`paraformer-v2` 只是示例；填写所选服务的密钥 |
| 时间戳校准 | 默认跟随服务，仅在模型支持时开启；字幕本身仍取自服务返回的真实时间戳 |
| 语音服务地址 | 默认 `https://api.siliconflow.cn/v1`，也支持其他兼容音频接口 |
| 语音模型 | 默认 `XingChenAGI/XingChenASR-V3.2-Ultra`；可填写其他完整模型 ID |
| 语音服务 API Key | 自己的语音服务凭据，与笔记服务分别保存 |
| 单次输出上限、单次请求最长等待 | 默认 `65536 tokens`、`20 分钟`；所用模型需支持该输出预算 |

例如 DeepSeek 官方 API：地址填 `https://api.deepseek.com`，模型填 `deepseek-v4-flash`，API Key 从自己的 DeepSeek 账户取得。模型名称以[官方文档](https://api-docs.deepseek.com/)为准。

然后在 **任务** 页：

1. 选择“下载并生成笔记”。
2. 填写**课程 ID**；多个用英文逗号分隔。课程 ID 与某一节回放的**课次 ID** 不同，后者填入“指定课次”。
3. 选择“课程保存位置”和“笔记保存位置”。用外置硬盘时，先连接磁盘，再点“选择…”。
4. 第一次建议只填一个课次。普通运行保持两个“重新生成”选项未勾选。
5. 切回“设置”，点击“保存设置”；返回“任务”，点击“开始任务”。

密码和 API Key 保存在 **macOS Keychain**；普通设置保存到当前用户的 Application Support。音频上传到配置的语音服务，完整转录文字发送到笔记服务。费用和限额以各服务商为准。[语音配置与切换模型](docs/cloud-asr.md)。

## 输出是什么

| 位置 | 内容 |
|---|---|
| 课程保存位置 | 按课程整理的 MP4 回放；语音服务提供完整真实时间戳时才有同名 SRT 字幕 |
| 笔记保存位置 | 按课程整理的 Markdown 笔记、TXT 转录原文 |
| 各目录的隐藏 `.icourse` | 校验记录、恢复缓存、旧笔记历史；不包含 API Key |

默认星辰接口实测返回纯文字，没有时间戳，所以生成 TXT 和笔记，不伪造字幕。长音频分块转录后合并为整课，成功块保存在 Application Support 的 `asr-cache` 中，重试时复用。

需要同步字幕时，可选择已实测的百炼 **Fun-ASR** 或 **Paraformer**，生成与 MP4 同名的 SRT。0.6.2 起也可直接输入其他支持相同录音接口和结果格式的模型 ID，无需修改程序。旧课需勾选“重新生成已有转录和笔记”才能用新模型补字幕，录像继续复用。[百炼设置及验证范围](docs/cloud-asr.md#阿里云百炼fun-asr--paraformer)

每个课次只有一份当前正式笔记，不再生成可见的分章草稿或核对意见。再次运行会复用已完成产物。只想重写笔记时，选择“为本地课程生成笔记”，勾选“只重新生成笔记（复用已有转录）”。

成功请求一次生成全文；空响应、超时或临时服务错误可能触发有限重试，增加 API 用量。输出截断或上下文不足会明确报错，不把半篇笔记标记为完成，也不自动拆分整课。

## 开发与项目来源

[开发说明](docs/development.md)介绍目录、测试、安装包检查和维护方式。GitHub Actions 只运行不使用真实账号的测试，不会自动订阅课程或调用付费笔记 API。当前桌面任务由用户手动启动。

本项目派生自 [LeafCreeper/Fudan_iCourse_Subscriber](https://github.com/LeafCreeper/Fudan_iCourse_Subscriber)，保留 Git 历史与来源说明。本分支基于早期代码完成 Mac 改造，与上游现在的 V2 云端订阅、PPT OCR、网页数据库实现不同；这些 V2 功能未集成到此 Mac 版。[来源与版本关系](NOTICE.md)。
