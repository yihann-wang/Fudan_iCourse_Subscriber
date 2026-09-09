import plistlib
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.create_mac_app import BUNDLE_ID, create_app
from scripts.install_mac_runtime import install, validate_wheel


def test_launcher_uses_isolated_installed_runtime_with_spaces(tmp_path):
    root = tmp_path / "source checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nversion="0.3.0"\n')
    python = tmp_path / "Application Support" / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    app = create_app(root, python, tmp_path / "Applications")
    executable = app / "Contents" / "MacOS" / "iCourse"
    command = next(line for line in executable.read_text().splitlines() if line.startswith("exec "))
    assert shlex.split(command)[:5] == ["exec", str(python), "-I", "-m", "src.mac_gui"]
    assert str(root) not in executable.read_text()
    assert executable.stat().st_mode & 0o111
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == BUNDLE_ID
    assert info["CFBundleShortVersionString"] == "0.3.0"
    assert info["LSMinimumSystemVersion"] == "14.0"


def test_unrelated_app_is_preserved_before_install(tmp_path, monkeypatch):
    app = tmp_path / "Applications" / "iCourse.app"
    app.mkdir(parents=True)
    marker = app / "important.txt"
    marker.write_text("keep")
    monkeypatch.setattr("scripts.install_mac_runtime.shutil.which", lambda _: "/fake/uv")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match="不属于此项目"):
        install(tmp_path, tmp_path / "support", app.parent)
    assert marker.read_text() == "keep" and not calls


@pytest.mark.parametrize("name", ["src/.env", "tools/.env.generated", "tools/lecture.mp4", "src/settings.json", "../src/injected.py"])
def test_wheel_rejects_private_or_noncode_files(tmp_path, name):
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(name, "private")
    with pytest.raises(RuntimeError):
        validate_wheel(wheel)


def test_wheel_accepts_source_and_standard_metadata(tmp_path):
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("src/__init__.py", "")
        archive.writestr("tools/tool.py", "")
        archive.writestr("fudan_icourse_subscriber-0.3.0.dist-info/METADATA", "")
        archive.writestr("fudan_icourse_subscriber-0.3.0.dist-info/licenses/NOTICE.md", "Attribution")
    validate_wheel(wheel)


def test_failed_build_leaves_existing_runtime_untouched(tmp_path, monkeypatch):
    runtime = tmp_path / "support" / "runtime"
    runtime.mkdir(parents=True)
    marker = runtime / "existing-package"
    marker.write_text("keep")
    monkeypatch.setattr("scripts.install_mac_runtime.shutil.which", lambda _: "/fake/uv")
    calls = []

    def fail(args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        install(tmp_path, runtime.parent, tmp_path / "Applications")
    assert marker.read_text() == "keep"
    assert len(calls) == 1 and "build" in calls[0]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS zsh installer entry point")
def test_installer_rejects_intel_before_dependency_changes(tmp_path):
    # Feed a fake uname through the script's PATH without installing anything.
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    uname = fakebin / "uname"
    uname.write_text('#!/bin/sh\nif [ "$1" = "-s" ]; then echo Darwin; else echo x86_64; fi\n')
    uname.chmod(0o755)
    script = (Path(__file__).resolve().parents[1] / "安装 Mac.command").read_text()
    script = script.replace('export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"',
                            f'export PATH={shlex.quote(str(fakebin))}:/usr/bin:/bin')
    result = subprocess.run(["/bin/zsh", "-c", script], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 1
    assert "不支持 Intel Mac" in result.stdout
