"""Create a local Finder launcher; this .app is not a portable distribution."""

import os
import plistlib
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from uuid import uuid4

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


def compile_launcher(root: Path, executable: Path) -> None:
    """Build before updating the installed runtime; requires Xcode Command Line Tools."""
    executable.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "/usr/bin/xcrun", "clang", "-fobjc-arc", "-O2", "-Wall", "-Wextra", "-Werror",
        "-mmacosx-version-min=14.0", "-framework", "Cocoa",
        str(root / "scripts" / "macos_launcher.m"), "-o", str(executable),
    ], check=True)


def python_library(python: Path) -> Path:
    # Homebrew uses a framework, while uv-managed Python uses libpython*.dylib.
    code = """
import sys, sysconfig
from pathlib import Path
base = Path(sys.base_prefix)
libdir = Path(sysconfig.get_config_var('LIBDIR') or base / 'lib')
candidates = [libdir / (sysconfig.get_config_var('LDLIBRARY') or ''), base / 'Python']
library = next((p for p in candidates if p.is_file()), None)
if library is None:
    raise SystemExit('找不到 Python 动态库，请重新安装运行环境。')
print(library)
"""
    result = subprocess.run([str(python), "-I", "-c", code], check=True, capture_output=True, text=True)
    library = Path(result.stdout.strip())
    if not library.is_absolute() or not library.is_file():
        raise RuntimeError("找不到 Python 动态库。")
    return library


def replace_bundle(source: Path, destination: Path) -> None:
    """Stage the complete signed bundle; restore the old app if replacement fails."""
    validate_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep the backup outside TemporaryDirectory: a failed rollback must not
    # cause automatic cleanup to erase the previous working bundle.
    previous = destination.with_name(f".iCourse-previous-{uuid4().hex}.app")
    with tempfile.TemporaryDirectory(prefix=".icourse-install-", dir=destination.parent) as folder:
        staged = Path(folder) / "iCourse.app"
        shutil.copytree(source, staged)
        if destination.exists():
            destination.rename(previous)
        try:
            staged.rename(destination)
        except OSError:
            if previous.exists():
                try:
                    previous.rename(destination)
                except OSError as exc:
                    raise RuntimeError(f"App 替换失败，旧版已保留在 {previous}") from exc
            raise
    if previous.exists():
        shutil.rmtree(previous)


def create_app(root: Path, python: Path, applications: Path, *, launcher: Path | None = None) -> Path:
    if not python.is_file():
        raise RuntimeError("请先运行安装 Mac.command。")
    installed = applications / "iCourse.app"
    validate_destination(installed)
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    library = python_library(python)
    with tempfile.TemporaryDirectory(prefix="icourse-app-") as build:
        app = Path(build) / "iCourse.app"
        executable = app / "Contents" / "MacOS" / "iCourse"
        executable.parent.mkdir(parents=True)
        resources = app / "Contents" / "Resources"
        resources.mkdir()
        if launcher is None:
            compile_launcher(root, executable)
        else:
            shutil.copy2(launcher, executable)
        executable.chmod(0o755)
        with (resources / "runtime.plist").open("wb") as stream:
            plistlib.dump({"PythonExecutable": str(python.absolute()), "PythonLibrary": str(library)}, stream)
        with (app / "Contents" / "Info.plist").open("wb") as stream:
            plistlib.dump({
                "CFBundleName": "iCourse", "CFBundleDisplayName": "iCourse",
                "CFBundleIdentifier": BUNDLE_ID, "CFBundleVersion": version,
                "CFBundleShortVersionString": version, "CFBundleExecutable": "iCourse",
                "CFBundlePackageType": "APPL", "NSHighResolutionCapable": True,
                "LSMinimumSystemVersion": "14.0",
                "NSDocumentsFolderUsageDescription": "读取已保存的课程转录，并将学习笔记保存到你选择的文件夹。",
                "NSDownloadsFolderUsageDescription": "读取你选择的本地课程文件，并保存课程与学习笔记。",
                "NSDesktopFolderUsageDescription": "访问你选择的桌面文件夹中的课程与学习笔记。",
                "NSRemovableVolumesUsageDescription": "读取和保存你选择的外置磁盘上的课程录像。",
                "NSNetworkVolumesUsageDescription": "读取和保存你选择的网络磁盘上的课程与学习笔记。",
            }, stream)
        # Local ad-hoc signing, not Developer ID signing or notarization. Ordinary
        # relaunch keeps this code identity; rebuilding may require consent again.
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True)
        replace_bundle(app, root / "dist" / "iCourse.app")
        replace_bundle(app, installed)
    return installed


def main():
    root = Path(__file__).resolve().parent.parent
    python = Path(os.environ.get("ICOURSE_RUNTIME_PYTHON") or (
        Path.home() / "Library" / "Application Support" / "Fudan iCourse Subscriber"
        / "runtime" / "bin" / "python"))
    print(create_app(root, python, Path.home() / "Applications"))


if __name__ == "__main__":
    main()
