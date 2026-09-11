# 常见问题

## 安装器提示系统不支持

本版需要 Apple Silicon M 系列、macOS 14.0+。在 Terminal 执行 `uname -m` 应显示 `arm64`。若 M 系列电脑显示 `x86_64`，检查 **Finder → Applications → Utilities → Terminal → Get Info**，关闭 **Open using Rosetta** 后重开 Terminal。Intel Mac 不支持此安装入口。

## uv / ffmpeg / ffprobe 找不到

在 Terminal 中执行 `brew install uv ffmpeg`，按 Homebrew 的提示完成 PATH 配置，再重开 Terminal。安装器和 App 都检查 Homebrew 常见安装位置。只安装 Python 不够；音视频解码还需要 ffmpeg 与 ffprobe。

## 下载依赖或转录模型失败

检查是否能访问依赖下载地址和 Hugging Face，再重试“准备转录模型”。已完整缓存的模型可以离线复用。不要在日志中粘贴代理密码或访问令牌。暂时无法使用 MLX 时，可在设置中选择“CPU 备用”，它使用另一套模型缓存，首次也可能需要下载。

## 外置磁盘或 Documents 没有访问权限

典型错误：`PermissionError: Operation not permitted: '/Volumes/…'`。这通常与 macOS 隐私权限有关，也可能是磁盘挂载、文件夹权限或连接问题。

1. 先在 Finder 确认磁盘或 Documents 中的目录存在，并能在目标目录新建一个普通文件夹。
2. 在 App 中点保存位置右侧“选择…”，重新选择目标文件夹。若系统询问访问权限，选择 **Allow**。
3. 打开 **System Settings → Privacy & Security → Files & Folders**。找到实际运行的 iCourse 条目并展开；若有 **Removable Volumes** 或 **Documents Folder**，开启对应权限。
4. 退出并重开 App，重试一个课次。命令行运行时，系统可能把访问权限归到 **Terminal**，应检查实际启动方式对应的条目。

没有出现 Allow 或 iCourse 条目时，不代表权限已经授予。先确认用的是 `~/Applications/iCourse.app`，完成安装后重新选择目录；可暂时选择用户文件夹下新建的 `~/iCourse` 目录来区分程序问题和受保护目录问题。

如果始终没有可用的授权项，可在 Finder 将原笔记根目录完整复制到用户文件夹下，例如 `~/iCourse/courses`，然后在 App 中把“笔记保存位置”改为这个新目录并保存设置。保留原目录直到确认新位置正常；完整复制可以带上既有转录和隐藏状态，避免重复调用 API。仅切换为空目录不会自动迁移旧笔记。

0.3.1 起，程序会在学校登录和模型任务前检查所需目录。“只下载课程”不使用笔记目录。权限检查和友好提示不能替代系统授权，也不能保证此前被拒绝的 App 会自动弹出新的 Allow 对话框。[Apple 的文件访问权限说明](https://support.apple.com/guide/mac-help/control-access-to-files-and-folders-on-mac-mchld5a35146/mac)。

如果 Finder 自己也无法写入，先解决磁盘问题。扩展坞连接不稳、只读挂载、文件系统损坏不会被隐私权限开关修复。不要用 `chmod 777` 或给 Python 加 `sudo` 代替定位原因。

## 登录失败或找不到课程

先用浏览器验证自己的 UIS 登录和 iCourse 访问是否正常。课程 ID 与单节回放的课次 ID 不同；不要把回放 ID 填进课程 ID。新学期需要更新课程 ID。登录成功但“暂无回放”可能只是平台还没有发布，稍后手动重试。

## 一直显示“生成笔记中”

这个计时表示还在等待模型回答，不表示字数进度。整节课一次生成可能需要几分钟；输入长度、服务负载和模型选择会影响等待时间。默认单次最长等待 20 分钟。

“超时”表示在指定时间内没有收到完整响应，客户端停止等待。服务端仍可能已处理请求，重试可能再次产生费用。查看之后是否出现明确错误或重试提示，不要同时启动多个相同任务。

## 摘要输出被截断

模型因输出预算不足或其他停止原因没有完整写完。程序不会把它标记完成，也不会自动分章继续请求。

在服务商支持范围内提高“单次输出上限”，或换成能够处理整课输入并输出足够内容的模型，然后选择“只重新生成笔记（复用已有转录）”。如果服务提示上下文过长，需要支持更长上下文的模型；程序不会悄悄删除原文。不能保证所有服务、所有长度的课程永不失败。

## 生成了笔记，但再次运行还想重新生成

普通运行复用已有正式笔记。需要强制重写时使用“只重新生成笔记（复用已有转录）”；完成后取消勾选。旧笔记隐藏归档，新笔记成功提交才替换当前文件。

## 为什么还有 JSON / .icourse / 旧稿

新版本把校验与进度文件放到隐藏 `.icourse`。它们不是要手动配置的参数，也不含 API Key。旧版本可见的同名 JSON 会在处理对应文件时迁移；已确认属于程序的同课次待核对稿会在新笔记成功后归档。无法确认来源的文件不会自动删除。

## 命令行已更新，App 还是旧逻辑

App 运行独立安装的程序包。更新源码后，退出 App 并重新运行 **安装 Mac.command**，再打开 `~/Applications/iCourse.app`。单纯修改源码或运行 `git pull` 不会更新已安装的 App。

## App 打不开或没有错误窗口

用 Finder 的 **Go to Folder…** 打开 `~/Library/Logs/Fudan iCourse Subscriber/`，查看 `application.log` 的末尾。运行安装器可检查 Python、依赖与 App 是否完整。

提交 Issue 时提供 macOS 版本、芯片、程序版本、失败阶段和去除敏感信息的错误片段。日志可能含课程标题、路径或带参数的播放链接；不要上传完整日志、`.env`、设置文件、钥匙串凭据或课程内容。
