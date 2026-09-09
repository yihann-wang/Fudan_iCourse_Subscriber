"""Mac-friendly entry points; all heavy imports are command-local."""

import argparse
import importlib.metadata
import importlib.util
import json
import platform
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .artifacts import atomic_write_json, atomic_write_text, file_sha256, migrate_auxiliary
from .asr import ASRSettings


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
        report["asr"] = asdict(settings)
        package = "mlx_whisper" if settings.backend == "mlx" else "faster_whisper"
        if importlib.util.find_spec(package) is None:
            errors.append(f"缺少 {package}；请运行安装脚本。")
    except ValueError as exc:
        errors.append(str(exc))
    for package in ("mlx", "mlx-whisper", "faster-whisper", "PySide6"):
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
    values = {}
    if args.backend:
        values["backend"] = args.backend
        if args.backend == "cpu" and settings.model.startswith("mlx-community/"):
            values.update(model="large-v3-turbo", revision="", compute_type="auto")
    if args.model:
        values.update(model=args.model, revision="")
    if args.revision:
        values["revision"] = args.revision
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
    parser = argparse.ArgumentParser(description="iCourse：Mac 本地转录与课程下载")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查运行环境（不登录、不调用云模型）")
    commands.add_parser("gui", help="打开 Mac 界面")
    commands.add_parser("run", help="运行课程流水线；run --help 查看参数")
    for name in ("prepare-model", "transcribe"):
        command = commands.add_parser(name)
        command.add_argument("--backend", choices=["auto", "mlx", "cpu", "cuda"])
        command.add_argument("--model")
        command.add_argument("--revision")
        if name == "transcribe":
            command.add_argument("media", type=Path)
            command.add_argument("--output-dir", type=Path)
            command.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor()
        if args.command == "gui":
            from .mac_gui import main as gui
            return gui()
        settings = _settings(args)
        if args.command == "prepare-model":
            from .asr.client import ASRWorker
            worker = ASRWorker(settings)
            try:
                result = worker.request(progress=lambda e: print(json.dumps(e, ensure_ascii=False), flush=True))
                print(json.dumps(result, ensure_ascii=False))
            finally:
                worker.close()
            return 0
        from .transcriber import Transcriber
        media = args.media.expanduser().resolve(strict=True)
        output = (args.output_dir or media.parent / "转录结果").expanduser().resolve()
        identity = file_sha256(media)
        stem = f"{media.stem}_{identity[:12]}"
        metadata_path = migrate_auxiliary(output / (stem + ".json"))
        if metadata_path.exists() and not args.overwrite:
            saved = json.loads(metadata_path.read_text())
            outputs = saved.get("artifacts", {})
            if (saved.get("complete") is True and saved.get("source_sha256") == identity and
                    saved.get("settings_fingerprint") == settings.fingerprint and outputs and
                    all((output / name).is_file() and file_sha256(output / name) == digest
                        for name, digest in outputs.items())):
                print(f"使用已验证的转录：{output / (stem + '.txt')}")
                return 0
        with Transcriber(settings) as transcriber:
            result = transcriber.transcribe_result(str(media), title=media.stem)
            text_path, srt_path = output / (stem + ".txt"), output / (stem + ".srt")
            atomic_write_text(text_path, result.text + "\n")
            transcriber.write_srt(srt_path)
            # Metadata is the commit marker, written after all artifacts.
            atomic_write_json(metadata_path, {
                **result.to_dict(), "source": str(media), "source_sha256": identity,
                "artifacts": {p.name: file_sha256(p) for p in (text_path, srt_path)},
            })
        print(f"转录已保存：{text_path}")
        return 0
    except KeyboardInterrupt:
        print("任务已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
