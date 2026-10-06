# Mac 安装与更新

## 准备

支持 Apple Silicon M 系列、macOS 14.0+。在 **Apple menu → About This Mac** 查看芯片与系统版本。Terminal 不要以 Rosetta 模式运行。

需要网络安装依赖并调用语音服务。请为 Python、界面依赖和临时音频预留空间；不再下载本地语音模型。临时音频约每小时 115 MB，任务结束后清理。视频可使用外置磁盘。

从 [Homebrew 官网](https://brew.sh/)按说明安装 Homebrew，并完成它提示的 shell 设置，然后执行：

```sh
brew install uv ffmpeg
```

安装器会自动下载 uv 管理的 Python 3.13，不要求自行配置系统 Python。

0.3.2 起需要 Apple **Command Line Tools** 来编译原生 App 入口（安装 Homebrew 时通常已装好）。如果安装器提示缺少编译工具，执行 `xcode-select --install`，完成系统安装窗口里的步骤后重试。不需要下载完整 Xcode。

## 运行安装器

打开[仓库首页](https://github.com/yihann-wang/Fudan_iCourse_Subscriber)，选择 **Code → Download ZIP**，解压到自己可写的文件夹。不要在 ZIP 预览中直接运行。

双击 **安装 Mac.command**。如果双击被系统阻止，打开 **Terminal**，输入 `cd `（末尾有一个空格），把解压后的文件夹拖入 Terminal，按 Return。随后执行：

```sh
zsh "安装 Mac.command"
```

等待出现“安装完成”。失败时按提示修复后重新执行同一命令即可。安装器使用 `uv.lock` 锁定依赖，并在更新运行环境之前构建和检查程序包。请先结束录像与 eLearning 两类任务并完全退出旧 App。安装器会检查录像流水线锁，但尚未内建 eLearning 锁或空闲 App 进程检查；不要将没有报锁冲突当作可以边运行边更新。

安装结束后，在 **Finder → Go → Go to Folder…**（**Command+Shift+G**）输入：

```text
~/Applications
```

打开 **iCourse.app**。后续双击源码目录中的 **启动 iCourse.command**，也会打开这个已安装版本。

## 首次配置

1. 在 App 的“设置”页填写所需配置：只使用 eLearning 或下载只需学校账号；新转录需要语音 API，生成笔记需要笔记 API。点击“保存设置”后供新任务使用；两组功能的课程、路径也统一在此配置。
2. 在「设置 → 高级与检查」点击“检查环境”；这一步不登录学校，也不调用笔记 API。
3. 需要转录时点击“检查语音连接”：兼容模式读取模型列表，百炼模式检查上传凭证，不上传音频或提交识别。检查通过不保证所有参数和音频都可用，先用短文件确认。
4. 在「设置 → 录像与笔记」选任务方式、课程 ID 和保存目录并保存，再去「录像与笔记」开始；eLearning 课程和目录在「设置 → eLearning」，在「eLearning 与作业」点击刷新或同步，课程来自独立映射，详见 [eLearning](elearning.md)。首次访问受保护目录时，按系统提示点击 **Allow**。详细示例见[使用说明](usage.md)。

已保存的目录会在下次打开时恢复。0.3.2 起 App 使用原生入口，普通退出、重开会保持同一个 App 身份；无需每次重新选择文件夹。系统拒绝过访问、重装或重建 App 后，可能需要重新授权，详见[磁盘访问问题](troubleshooting.md#外置磁盘或-documents-没有访问权限)。

旧版本地转录功能与依赖已移除。已有 TXT 和笔记仍可复用；新转录需要配置云端语音服务。详情见[云端转录](cloud-asr.md)。

## 外置硬盘保存视频

在 Finder 确认磁盘正常挂载，然后通过 App 的“选择…”指定目录。例如：

- 课程保存位置：`/Volumes/CourseDisk/iCourse`
- 笔记保存位置：`~/Documents/Study/courses`（通过选择器选择该文件夹最直观）

实际路径中的 `CourseDisk` 必须换成自己的磁盘名称。任务期间保持连接；结束后在 Finder 中 **Eject** 再拔出。磁盘重命名后需重新选择路径。无需为安装程序重新格式化磁盘。

若出现 `Operation not permitted`，参见[磁盘访问问题](troubleshooting.md#外置磁盘或-documents-没有访问权限)。

## 文件安装在哪里

| 内容 | 默认位置 |
|---|---|
| App 启动器 | `~/Applications/iCourse.app` |
| 独立 Python 运行环境 | `~/Library/Application Support/Fudan iCourse Subscriber/runtime` |
| 设置、密码和 API Key（明文） | `~/Library/Application Support/Fudan iCourse Subscriber/settings.json` |
| 录像状态与锁 | `~/Library/Application Support/Fudan iCourse/pipeline.sqlite3`、`pipeline.lock` |
| eLearning 索引与锁 | `~/Library/Application Support/Fudan iCourse/eLearning/index.sqlite3`、`run.lock` |
| eLearning 个人课程与保存配置 | 同一个 `settings.json` 的 `elearning` 字段；旧 `elearning.json` 仅作导入来源 |
| eLearning 初始示例 | 安装 runtime 的 `site-packages/src/elearning_helper/config.json`；首次使用须在界面替换示例课程 |
| App 启动日志 | `~/Library/Logs/Fudan iCourse Subscriber/application.log` |
| 录像诊断与最近结果 | 同目录的 `tasks.log`、`last-run.txt`；eLearning 详情在页签内显示 |
| uv 管理的 Python | 通常为 `~/.local/share/uv/python/` |
| 转录块缓存 | `~/Library/Application Support/Fudan iCourse Subscriber/asr-cache/` |
| 大文件校验缓存 | `~/Library/Caches/Fudan iCourse Subscriber/file-hashes/` |

`~` 代表自己的用户文件夹。以上隐藏路径可通过 Finder 的 **Go to Folder…** 打开。

安装完成后可以移动源码文件夹；已安装 App 使用独立的非 editable 程序包。不要删除它依赖的 uv Python 或运行环境。源码目录里的 `dist/iCourse.app` 只是本机启动器，包含本机运行环境路径，**不能直接拷给另一台 Mac 使用**。

## 安装结构与空间管理

安装脚本先用锁定依赖准备源码目录的 `.venv`，供构建和环境检查使用；再把程序及依赖安装到 Application Support 的 `runtime`；最后编译一个原生启动器，分别放进源码的 `dist` 和 `~/Applications`。更新复用同一个正式 `runtime`，不会每次新建一个带版本号的运行环境。

| 目录或文件 | 日常用途与清理边界 |
|---|---|
| `~/Applications/iCourse.app` | 正式入口，应保留；本机测得约 86 KB |
| Application Support 中的 `runtime` | 正式程序及依赖，应保留；本机测得约 1.32 GB |
| 源码 `.venv` | 安装、开发和测试环境；本机约 1.35 GB，删除后下次安装或开发需重建 |
| 源码 `dist/iCourse.app` | 与正式 App 指向同一运行环境的构建副本，可移除；再次构建会生成 |
| 旧发布或临时验证目录的环境与 App | 确认不再使用、没有运行中的任务后可清理；不要连同唯一的源码仓库一起删除 |
| 课程、笔记、个人配置、索引与 ASR 缓存 | 用户资料或恢复状态，应与程序清理分开处理 |

Spotlight 会列出磁盘上的多个 `.app`，旧测试启动器还可能指向旧环境。日常使用 Applications 中的正式入口。删除旧 App 后搜索结果可能稍后才刷新。

临时验证应使用独立目录，完成自检后清理该次生成的 App、运行环境和演示状态。磁盘故障处理中人工建立的中转目录可能含唯一备份及未完成录像，只有在核对保留内容并明确不再需要后才能删除。安装器不会自动删除这些资料。上表为一台 Mac 的测量值，依赖版本、共享数据块和文件系统快照会影响实际占用及可释放空间。

## 更新

结束任务并退出 App。Git 安装用户在仓库文件夹运行：

```sh
git pull --ff-only
zsh "安装 Mac.command"
```

ZIP 用户下载最新版并解压，运行新目录中的安装器。升级会替换程序，保留本地设置和凭据文件、视频、转录、笔记及 eLearning 索引。安装版内的默认课程 JSON 属于程序文件，会随安装替换；个人映射在「设置 → eLearning」维护并统一保存到 `settings.json`，更新后仍保留。缺少统一配置时先读取旧 `elearning.json`，否则使用程序随附配置；旧文件和已有课件不删除、不搬移。不要只更新源码就继续使用旧 App；安装器运行成功后才完成桌面版更新。

0.6.10 起密码和 API Key 直接保存在本地 `settings.json`。首次从旧版迁移时需要读取原钥匙串，系统可能要求授权；全部读取并保存成功后，后续启动与保存只使用文件。迁移失败不会覆盖旧配置，原钥匙串条目保留。Documents、外置磁盘等文件访问授权仍由 macOS 管理。

原来已安装的 0.2 版本可以按同样方式更新。旧运行环境若依赖已删除的 Homebrew Python，可重新运行安装器修复；重装前不要删除课程或笔记目录。

## 更新后检查

重新打开主 App，确认 eLearning 页签显示“同名直接保留”，且没有同名比对复选框；点击前不会登录。开发者可按[开发文档](development.md)运行原生 `--self-test`，该自检使用默认设置与离线数据，不读取个人凭据。完整真实登录、目录授权及学校访问由用户手动验证。

当前版本为 0.7.1，主窗口为两个工作页和统一设置页。录像任务仍仅有“下载录像并生成笔记”和“只下载录像”，可与 eLearning 并行；课程、目录、账号及 API 参数统一保存到本地配置文件。更新不创建后台服务、定时任务或邮件通知。

## 卸载

退出 App，在 Finder 删除 `~/Applications/iCourse.app` 和上述 `runtime` 文件夹即可移除程序主体。保留设置和课程文件，方便以后重新安装。若不再需要，可自行单独删除该项目的设置、任务状态和日志。

旧版 Hugging Face 模型缓存和 uv Python 可能被其他软件共用，升级不会删除这些共享目录。钥匙串凭据可在 **Keychain Access** 中搜索 `Fudan iCourse Subscriber`，确认条目属于本项目后自行移除。
