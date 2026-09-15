"""Install the locked desktop runtime without copying local configuration."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from filelock import FileLock, Timeout
from platformdirs import user_data_path

# Works both as a script and when imported by offline installer tests.
if __package__:
    from .create_mac_app import compile_launcher, create_app, validate_destination
else:
    from create_mac_app import compile_launcher, create_app, validate_destination


def validate_wheel(wheel: Path) -> None:
    """Runtime packages contain Python source only, plus standard wheel metadata."""
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("构建结果包含异常路径，停止安装。")
            if name.endswith("/"):
                continue
            if path.parts[0] in {"src", "tools"} and path.suffix == ".py":
                continue
            if path.parts[0].startswith("fudan_icourse_subscriber-") and path.parts[0].endswith(".dist-info"):
                if len(path.parts) == 2 and path.name in {"METADATA", "WHEEL", "RECORD", "entry_points.txt"}:
                    continue
                if path.parts[1:] == ("licenses", "NOTICE.md"):
                    continue
            raise RuntimeError(f"构建结果包含非程序文件，停止安装：{name}")


def install(root: Path, support: Path, applications: Path) -> Path:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("找不到 uv。请使用 安装 Mac.command 安装。")
    runtime = support / "runtime"
    validate_destination(applications / "iCourse.app")

    def run(args):
        subprocess.run(args, cwd=root, check=True)

    # Build and audit before touching an existing working installation.
    with tempfile.TemporaryDirectory(prefix="icourse-build-") as build:
        run([uv, "build", "--wheel", "--out-dir", build])
        wheels = list(Path(build).glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("没有找到唯一的安装包。")
        wheel = wheels[0]
        validate_wheel(wheel)
        launcher = Path(build) / "iCourse"
        compile_launcher(root, launcher)
        requirements = Path(build) / "requirements.txt"
        run([uv, "export", "--locked", "--extra", "mac", "--no-dev",
             "--no-emit-project", "--format", "requirements-txt", "--output-file", str(requirements), "--quiet"])
        support.mkdir(parents=True, exist_ok=True)
        python = runtime / "bin" / "python"
        if not python.exists():
            run([uv, "venv", "--python", sys.executable, str(runtime)])
        run([uv, "pip", "sync", "--python", str(python), "--require-hashes", str(requirements)])
        run([uv, "pip", "install", "--python", str(python), "--no-deps", "--reinstall", str(wheel)])
        run([str(python), "-I", "-c", "import src.mac_gui, src.pipeline, src.summarizer"])
        shutil.copy2(requirements, support / "runtime-requirements.txt")
        return create_app(root, python, applications, launcher=launcher)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--support-dir", type=Path, default=Path.home() / "Library" / "Application Support" / "Fudan iCourse Subscriber")
    parser.add_argument("--applications-dir", type=Path, default=Path.home() / "Applications")
    args = parser.parse_args()
    state_dir = Path(os.environ.get("ICOURSE_STATE_DIR") or user_data_path("Fudan iCourse", appauthor=False))
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(state_dir / "pipeline.lock", timeout=0):
            app = install(Path(__file__).resolve().parent.parent,
                          args.support_dir.expanduser().resolve(), args.applications_dir.expanduser().resolve())
        print(f"安装完成：{app}")
    except Timeout:
        raise SystemExit("iCourse 正在处理课程。请等待任务结束并关闭 App，再重新安装。")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"安装未完成：{exc}。解决问题后可重新运行 安装 Mac.command。")


if __name__ == "__main__":
    main()
