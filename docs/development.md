# 开发与发布

## 环境与测试

标准开发环境：Apple Silicon、macOS 14+、uv、ffmpeg，Python 3.13。

```sh
uv sync --locked --extra mac --extra cpu --python 3.13 --managed-python
.venv/bin/pytest -q
.venv/bin/ruff check --select F src tools scripts main.py tests
```

测试使用模拟登录、模型回答和临时文件，覆盖整课一次调用、失败重试、截断响应、转录产物校验、身份冲突、并发锁、外置目录布局、钥匙串失败和安装器。不要在自动化测试里填真实凭据、运行完整课程任务或发送邮件。

GitHub Actions 在 macOS 14、macOS 26 和 Linux 上检查锁定依赖、代码、测试和 wheel。Mac 任务还会安装到临时目录、验证隔离运行环境和无界面的 Qt 窗口创建；不会下载语音模型或访问课程。这里的安装与导入检查不等于所有硬件上的真实 GPU 识别性能测试。

本地实机已完成真实回放的下载、转录和整课笔记生成验证。运行速度和识别效果随硬件、音质、网络和模型服务变化。Windows/NVIDIA 仅保留适配代码，未做本版硬件回归。

## 目录

| 路径 | 职责 |
|---|---|
| `src/mac_gui.py` | Qt 界面，启动同一 CLI 引擎 |
| `src/preferences.py` | 普通设置、钥匙串与任务环境 |
| `src/cli.py`、`src/engine.py` | 命令入口、取消和保持唤醒 |
| `src/pipeline.py`、`src/pipeline_state.py` | 下载 → 转录 → 笔记队列、SQLite 状态与锁 |
| `src/icourse.py`、`src/webvpn.py` | 学校登录、课程目录与回放 |
| `src/asr/`、`src/transcriber.py`、`src/media.py` | MLX / CPU / CUDA 后端、持久进程、音频处理与字幕 |
| `src/summarizer.py` | 整课单次请求、重试与完整响应验证 |
| `src/artifacts.py`、`src/summary_storage.py` | 校验、隐藏状态、原子写入与历史 |
| `scripts/install_mac_runtime.py`、`scripts/create_mac_app.py` | 独立运行环境与本机启动器 |
| `scripts/macos_launcher.m`、`src/mac_app_check.py` | 原生 App 入口与不读取真实设置的启动自检 |
| `tests/` | 不使用真实账号的回归测试 |
| `main.py`、数据库/邮件相关模块与 `tools/` | 保留的旧接口；新入口优先使用 `src.cli` |

每个阶段默认一个 worker，阶段之间可重叠工作。ASR 子进程复用模型，避免每课重新加载。取消会终止任务进程组；课程流水线互斥，安装器也检查同一把锁。

## 不应回归的行为

- 正常笔记只发一次请求，携带完整转录；不要悄悄引入提纲、分章或审核调用。
- 空响应、输出截断或非正常结束不能写成成功产物。
- 单独重做笔记必须复用转录；缺少转录时明确失败。
- 更新失败不能覆盖已完成的正式笔记；内部信息放入隐藏 `.icourse`。
- GUI 保存的密码、API Key 不能进入 JSON，也不能在钥匙串失败时回退明文。
- App 必须使用已安装的运行环境，不依赖源码目录或启动时的 Python 搜索路径。

原生入口通过 `dlopen` 加载安装环境的 Python 动态库，在同一进程调用 `Py_BytesMain`，使用 `-I` 隔离 Python 搜索路径，并保留运行环境的 `sys.executable` 供后台引擎使用。不要将入口改回 shell 脚本或 `exec` 替换为解释器，否则会丢失原生主程序身份。GUI 和后台分别检查目录；没有启用 App Sandbox，也不修改 TCC 数据库。这里的本地 ad-hoc 签名只用于运行，不等于跨版本稳定的 Developer ID 身份。[Apple DTS 背景说明](https://developer.apple.com/forums/thread/678819)。

## 构建与安装检查

```sh
uv build --wheel --out-dir build/wheels
.venv/bin/python -c 'from pathlib import Path; from scripts.install_mac_runtime import validate_wheel; [validate_wheel(p) for p in Path("build/wheels").glob("*.whl")]'
```

wheel 仅允许 `src/`、`tools/` 中的 Python 文件和标准包元数据；安装器拒绝夹带配置、视频等文件。公开源码只包含代码、示例配置、文档和锁文件。

测试安装器时使用临时目录，避免改动正在使用的 App：

```sh
validation_dir="$(mktemp -d)"
ICOURSE_STATE_DIR="$validation_dir/state" .venv/bin/python scripts/install_mac_runtime.py \
  --support-dir "$validation_dir/support" --applications-dir "$validation_dir/Applications"
"$validation_dir/support/runtime/bin/python" -I -m src.cli doctor
QT_QPA_PLATFORM=offscreen "$validation_dir/Applications/iCourse.app/Contents/MacOS/iCourse" --self-test
QT_QPA_PLATFORM=offscreen "$validation_dir/Applications/iCourse.app/Contents/MacOS/iCourse" --self-test
codesign --verify --strict "$validation_dir/Applications/iCourse.app"
```

`--self-test` 只创建使用默认值的 Qt 窗口对象、检查后台 Python 和 App 身份，不读取个人设置、钥匙串或课程，不发起网络请求。它不能模拟用户在 Finder 启动后给予的真实 TCC 授权。验证授权保留时，应在干净的 macOS 用户环境中打开已安装 App，授权 Documents/外置卷，正常退出后再次打开，确认无需重新选目录；不要用 Terminal 的既有授权冒充 App 授权。

普通用户更新仍运行根目录的安装器。`.app` 中含绝对运行环境路径，只供安装它的用户和电脑使用；发布时使用 Git 跟踪的源码，不发布本机 `.app`、虚拟环境或模型缓存。

## 发布检查

1. 检查 `git status` 与暂存差异；不得加入 `.env`、个人 JSON、数据库、日志、课程、旧本机测试产物或字体二进制。
2. 在干净目录运行上述测试与构建；确认 CI 通过。
3. 更新 `pyproject.toml` 版本、`uv.lock`、`CHANGELOG.md` 和使用文档。
4. 创建提交和版本标签。GitHub 的 **Source code (zip)** 即可作为分发源码包；源码包必须重新检查内容。
5. 新用户从空配置启动，输入自己的凭据，不附带开发者配置。

本分支不是上游 V2 的替换升级安装器。需要 V2 云端功能时请使用对应版本，不能混用其数据库和工作流。详情见 [NOTICE](../NOTICE.md)。
