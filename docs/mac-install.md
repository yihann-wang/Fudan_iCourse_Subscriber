# Mac 安装与更新

## 准备

支持 Apple Silicon M 系列、macOS 14.0+。在 **Apple menu → About This Mac** 查看芯片与系统版本。Terminal 不要以 Rosetta 模式运行。

需要网络安装依赖和转录模型。请给内置磁盘留出数 GB 空间存放 Python、依赖和模型；课程视频的空间另算，可使用外置磁盘。实际占用随模型和依赖版本变化。

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

等待出现“安装完成”。失败时按提示修复后重新执行同一命令即可。安装器使用 `uv.lock` 锁定依赖，并在更新运行环境之前构建和检查程序包。请先结束课程任务并关闭旧 App；已有流水线运行时安装器会拒绝更新。

安装结束后，在 **Finder → Go → Go to Folder…**（**Command+Shift+G**）输入：

```text
~/Applications
```

打开 **iCourse.app**。后续双击源码目录中的 **启动 iCourse.command**，也会打开这个已安装版本。

## 首次配置

1. 在 App 的“设置”页输入自己的 UIS 账号和笔记 API 配置，点击“保存设置”。
2. 点击“检查环境”；这一步不登录学校，也不调用笔记 API。
3. 点击“准备转录模型”。默认模型从 Hugging Face 下载；也可等第一次转录时自动下载。
4. 在“任务”页选择保存目录，然后到“设置”页点击“保存设置”；填写课程 ID，开始任务。首次访问受保护目录时，按系统提示点击 **Allow**。详细示例见[使用说明](usage.md)。

已保存的目录会在下次打开时恢复。0.3.2 起 App 使用原生入口，普通退出、重开会保持同一个 App 身份；无需每次重新选择文件夹。系统拒绝过访问、重装或重建 App 后，可能需要重新授权，详见[磁盘访问问题](troubleshooting.md#外置磁盘或-documents-没有访问权限)。

“本地转录”保持自动即可。安装的 CPU 后端可作为备用，但转录速度取决于硬件。模型缓存完成后，本地转录可以离线；下载课程和调用在线笔记服务仍需要网络。

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
| 普通设置 | `~/Library/Application Support/Fudan iCourse Subscriber/settings.json` |
| 密码、API Key | macOS Keychain |
| 任务状态 | `~/Library/Application Support/Fudan iCourse/` |
| App 启动与运行日志 | `~/Library/Logs/Fudan iCourse Subscriber/application.log` |
| uv 管理的 Python | 通常为 `~/.local/share/uv/python/` |
| 模型缓存 | 通常为 `~/.cache/huggingface/hub/` |

`~` 代表自己的用户文件夹。以上隐藏路径可通过 Finder 的 **Go to Folder…** 打开。

安装完成后可以移动源码文件夹；已安装 App 使用独立的非 editable 程序包。不要删除它依赖的 uv Python 或运行环境。源码目录里的 `dist/iCourse.app` 只是本机启动器，包含本机运行环境路径，**不能直接拷给另一台 Mac 使用**。

## 更新

结束任务并退出 App。Git 安装用户在仓库文件夹运行：

```sh
git pull --ff-only
zsh "安装 Mac.command"
```

ZIP 用户下载最新版并解压，运行新目录中的安装器。升级会替换程序，保留设置、钥匙串凭据、视频、转录和笔记。不要只更新源码就继续使用旧 App；安装器运行成功后才完成桌面版更新。

从旧脚本启动器升级到 0.3.2 后，macOS 可能重新询问文件访问或钥匙串访问。确认是自己刚安装的 iCourse 后授权一次。安装器使用本地 ad-hoc 签名，尚无 Developer ID 签名和公证，不能保证跨重装或跨版本永久保留系统授权。

原来已安装的 0.2 版本可以按同样方式更新。旧运行环境若依赖已删除的 Homebrew Python，可重新运行安装器修复；重装前不要删除课程或笔记目录。

## 卸载

退出 App，在 Finder 删除 `~/Applications/iCourse.app` 和上述 `runtime` 文件夹即可移除程序主体。保留设置和课程文件，方便以后重新安装。若不再需要，可自行单独删除该项目的设置、任务状态和日志。

模型缓存和 uv Python 可能被其他软件共用，不要整目录盲目删除。钥匙串凭据可在 **Keychain Access** 中搜索 `Fudan iCourse Subscriber`，确认条目属于本项目后自行移除。
