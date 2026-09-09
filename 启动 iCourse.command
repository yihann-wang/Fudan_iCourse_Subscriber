#!/bin/zsh
set -euo pipefail
app_path="$HOME/Applications/iCourse.app"
if [[ ! -d "$app_path" ]]; then
  print "尚未安装 iCourse。请先运行同目录中的 安装 Mac.command。"
  exit 1
fi
exec /usr/bin/open "$app_path"
