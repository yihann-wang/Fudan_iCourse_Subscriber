"""Mac-friendly entry points; all heavy imports are command-local."""

import argparse
import importlib.metadata
import json
import platform
import sys
from dataclasses import replace
from pathlib import Path

from . import task_events as events
from .artifacts import (
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    migrate_auxiliary,
)
from .asr import ASRSettings
from .storage_access import check_directory


def doctor():
    from .media import resolve_media_tool
    report = dict(system=platform.system(), machine=platform.machine(),
                  macos=platform.mac_ver()[0], python=sys.version.split()[0])
    errors = []
    for tool in ("ffmpeg", "ffprobe"):
        try:
            report[tool] = resolve_media_tool(tool)
        except FileNotFoundError as exc:
            errors.append(str(exc))
    try:
        settings = ASRSettings.from_env()
        report["asr"] = settings.public_dict()
        report["asr"]["key_configured"] = bool(settings.api_key)
    except ValueError as exc:
        errors.append(str(exc))
    for package in ("fudan-icourse-subscriber", "requests", "PySide6"):
        try:
            report[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report[package] = None
    report["errors"] = errors
    report["ok"] = not errors
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _settings(args):
    settings = ASRSettings.from_env()
    values = {field: getattr(args, field) for field in ("base_url", "model", "response_format")
              if getattr(args, field) is not None}
    return replace(settings, **values).resolved()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        from .pipeline import main as run
        saved = sys.argv
        try:
            sys.argv = ["icourse run", *argv[1:]]
            return run()
        finally:
            sys.argv = saved
    parser = argparse.ArgumentParser(description="iCourse：课程下载与云端转录")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查运行环境（不登录、不调用云模型）")
    commands.add_parser("gui", help="打开 Mac 界面")
    commands.add_parser("run", help="运行课程流水线；run --help 查看参数")
    for name in ("check-asr", "transcribe"):
        command = commands.add_parser(name)
        command.add_argument("--base-url")
        command.add_argument("--model")
        command.add_argument("--env-file", type=Path, help="读取指定配置；默认只使用环境变量")
        command.add_argument("--response-format", choices=["json", "verbose_json", "text", "srt"])
        if name == "transcribe":
            command.add_argument("media", type=Path)
            command.add_argument("--output-dir", type=Path)
            command.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    task = None
    try:
        if args.command == "doctor":
            events.progress("正在检查本机运行环境")
            return doctor()
        if args.command == "gui":
            from .mac_gui import main as gui
            return gui()
        if args.env_file:
            from .pipeline import _load_env_file
            _load_env_file(args.env_file.expanduser().resolve(strict=True))
        settings = _settings(args)
        if args.command == "check-asr":
            events.progress("正在检查语音连接（不上传音频）")
            from .asr.client import CloudWorker
            worker = CloudWorker(settings)
            try:
                result = worker.request(progress=lambda e: print(e.get("message", ""), flush=True))
                print(result["message"])
            finally:
                worker.close()
            return 0
        from .transcriber import Transcriber
        media = args.media.expanduser().resolve(strict=True)
        output = (args.output_dir or media.parent / "转录结果").expanduser().resolve()
        check_directory(output, "转录保存位置", writable=True, create=True)
        identity = file_sha256(media)
        stem = f"{media.stem}_{identity[:12]}"
        metadata_path = migrate_auxiliary(output / (stem + ".json"))
        task = dict(course_id="local", course_title="本地音视频", sub_id=identity[:12], sub_title=media.stem)
        text_path, srt_path = output / (stem + ".txt"), output / (stem + ".srt")
        if metadata_path.exists() and not args.overwrite:
            saved = json.loads(metadata_path.read_text())
            outputs = saved.get("artifacts", {})
            if (saved.get("complete") is True and saved.get("source_sha256") == identity and
                    saved.get("settings_fingerprint") == settings.fingerprint and outputs and
                    all((output / name).is_file() and file_sha256(output / name) == digest
                        for name, digest in outputs.items())):
                print(f"使用已验证的转录：{output / (stem + '.txt')}")
                events.plan_task(task, dict(dl="cached", tr="cached", sm="na"), dl=media, tr=text_path)
                events.emit("planned", total=1)
                return 0
        events.plan_task(task, dict(dl="cached", tr="queued", sm="na"), dl=media, tr=text_path)
        events.emit("planned", total=1)
        events.stage(task, "tr", "running")
        with Transcriber(settings) as transcriber:
            result = transcriber.transcribe_result(str(media), title=media.stem)
            text_path, srt_path = output / (stem + ".txt"), output / (stem + ".srt")
            if not result.complete or not result.text.strip():
                raise RuntimeError("转录不完整或为空，不会标为完成。")
            atomic_write_text(text_path, result.text + "\n")
            subtitle_count = transcriber.write_srt(srt_path)
            outputs = [text_path, srt_path] if subtitle_count else [text_path]
            # Metadata is the commit marker, written after all artifacts.
            atomic_write_json(metadata_path, {
                **result.to_dict(), "source": str(media), "source_sha256": identity,
                "artifacts": {p.name: file_sha256(p) for p in outputs},
            })
        print(f"转录已保存：{text_path}")
        events.stage(task, "tr", "done", path=str(text_path))
        return 0
    except KeyboardInterrupt:
        print("任务已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        if task:
            events.stage(task, "tr", "failed", f"{type(exc).__name__}: {exc}")
        else:
            events.emit("phase", message=f"{type(exc).__name__}: {exc}")
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
