"""Create a local Finder launcher; this .app is not a portable distribution."""

import os
import plistlib
import shlex
import shutil
import tomllib
from pathlib import Path

BUNDLE_ID = "local.fudan.icourse"


def validate_destination(installed: Path) -> None:
    if installed.exists():
        try:
            with (installed / "Contents" / "Info.plist").open("rb") as stream:
                owned = plistlib.load(stream).get("CFBundleIdentifier") == BUNDLE_ID
        except (OSError, ValueError, plistlib.InvalidFileException):
            owned = False
        if not owned:
            raise RuntimeError("Applications 中的 iCourse.app 不属于此项目，未覆盖。")


def create_app(root: Path, python: Path, applications: Path) -> Path:
    if not python.is_file():
        raise RuntimeError("请先运行安装 Mac.command。")
    installed = applications / "iCourse.app"
    validate_destination(installed)
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    app = root / "dist" / "iCourse.app"
    executable = app / "Contents" / "MacOS" / "iCourse"
    executable.parent.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Resources").mkdir(exist_ok=True)
    executable.write_text(
        "#!/bin/zsh\nset -eu\n"
        'cd "$HOME"\n'
        'umask 077\n'
        'log_dir="$HOME/Library/Logs/Fudan iCourse Subscriber"\n'
        'mkdir -p "$log_dir"\n'
        "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin\n"
        f'exec {shlex.quote(str(python))} -I -m src.mac_gui >> "$log_dir/application.log" 2>&1\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({
            "CFBundleName": "iCourse", "CFBundleDisplayName": "iCourse",
            "CFBundleIdentifier": BUNDLE_ID, "CFBundleVersion": version,
            "CFBundleShortVersionString": version, "CFBundleExecutable": "iCourse",
            "CFBundlePackageType": "APPL", "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "14.0",
        }, stream)
    shutil.copytree(app, installed, dirs_exist_ok=True)
    return installed


def main():
    root = Path(__file__).resolve().parent.parent
    python = Path(os.environ.get("ICOURSE_RUNTIME_PYTHON") or (
        Path.home() / "Library" / "Application Support" / "Fudan iCourse Subscriber"
        / "runtime" / "bin" / "python"))
    print(create_app(root, python, Path.home() / "Applications"))


if __name__ == "__main__":
    main()
