#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  print "安装需要 Apple Silicon（M 系列）Mac，并在原生终端中运行；不支持 Intel Mac 或 Rosetta 终端。"
  exit 1
fi
mac_version="$(sw_vers -productVersion)"
if (( ${mac_version%%.*} < 14 )); then
  print "安装需要 macOS 14.0 或更新版本。"
  exit 1
fi
if ! command -v uv >/dev/null || ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  print "需要 uv、ffmpeg 和 ffprobe。安装 Homebrew 后运行：brew install uv ffmpeg"
  exit 1
fi
if ! xcrun --find clang >/dev/null 2>&1; then
  print "需要 Apple Command Line Tools 来构建 App。请先执行 xcode-select --install，安装完成后重试。"
  exit 1
fi
print "安装 iCourse。请先关闭正在运行的 iCourse 任务和窗口。"
# A managed interpreter survives Homebrew Python upgrades and moving this checkout.
uv python install 3.13
uv sync --locked --extra mac --no-dev --python 3.13 --managed-python
.venv/bin/python -m src.cli doctor
.venv/bin/python scripts/install_mac_runtime.py
print "安装完成。请在 Finder 中按 Command+Shift+G，输入 ~/Applications，打开 iCourse.app。"
print "也可以双击 启动 iCourse.command。请在设置中填写语音服务地址、模型和 API Key。"
