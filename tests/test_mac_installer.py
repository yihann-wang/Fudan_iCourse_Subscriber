import plistlib
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.create_mac_app import BUNDLE_ID, create_app, replace_bundle
from scripts.install_mac_runtime import install, validate_wheel


@pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS app")
def test_native_launcher_reopens_with_same_identity_and_isolated_runtime(tmp_path):
    root = tmp_path / "source checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nversion="0.3.0"\n')
    (root / "scripts").mkdir()
    shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / "macos_launcher.m", root / "scripts")
    python = Path(sys.executable)
    app = create_app(root, python, tmp_path / "Applications")
    executable = app / "Contents" / "MacOS" / "iCourse"
    assert executable.read_bytes()[:4] in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
    before = hashlib.sha256(executable.read_bytes()).hexdigest()
    assert executable.stat().st_mode & 0o111
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == BUNDLE_ID
    assert info["CFBundleShortVersionString"] == "0.3.0"
    assert info["LSMinimumSystemVersion"] == "14.0"
    assert info["NSDocumentsFolderUsageDescription"]
    assert info["NSRemovableVolumesUsageDescription"]
    # A shell PYTHONPATH must not replace the app package, even on a later launch.
    injection = tmp_path / "injected" / "src"
    injection.mkdir(parents=True)
    (injection / "__init__.py").write_text('raise RuntimeError("Wrong package loaded")')
    for _ in range(2):
        result = subprocess.run([str(executable), "--self-test"], capture_output=True, text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(injection.parent)},
            timeout=45, check=True)
        report = json.loads(result.stdout)
        assert report["bundle_id"] == BUNDLE_ID
        assert report["executable"] == str(python)
        assert report["prefix"] == sys.prefix and report["isolated"] == 1
        assert report["child"]["prefix"] == sys.prefix
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == before
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True)


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


def test_failed_app_replacement_restores_existing_app(tmp_path, monkeypatch):
    app = tmp_path / "Applications" / "iCourse.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": BUNDLE_ID}))
    (app / "old-version").touch()
    source = tmp_path / "new.app"
    source.mkdir()
    (source / "new-version").touch()
    rename = Path.rename

    def fail_replace(path, target):
        if path.parent.name.startswith(".icourse-install-"):
            raise OSError("simulated replacement failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        replace_bundle(source, app)
    assert (app / "old-version").exists()
    assert not (app / "new-version").exists()


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
