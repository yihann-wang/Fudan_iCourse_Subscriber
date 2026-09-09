#!/usr/bin/env python3
"""Batch-generate soft subtitles (.srt) for already-downloaded lectures.

Walks every ``*/录屏/*.mp4`` under the given root(s), transcribes each video
with the local faster-whisper model, and writes a same-named ``.srt`` next to
it so players (VLC / PotPlayer / mpv) auto-load it.

Resumable: a lecture that already has a ``.srt`` is skipped, so the job can be
stopped and re-run freely. No LLM / summary work — subtitles only.

Usage:
    python tools/make_subtitles.py [ROOT ...] [--overwrite]

ROOT defaults to ~/iCourse/课程 on Mac. Set WHISPER_MODEL to a local snapshot path
to avoid any network download.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

VIDEO_SUBDIR = "录屏"
DEFAULT_ROOT = Path.home() / "iCourse" / "课程"


def _ts() -> str:
    return time.strftime("[%H:%M:%S]")


def _find_videos(roots: list[Path]) -> list[Path]:
    """All mp4s living in a ``录屏`` directory under any root, sorted."""
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            print(f"{_ts()} skip missing root: {root}", flush=True)
            continue
        # course_dir/录屏/*.mp4  (and tolerate deeper nesting just in case)
        for mp4 in root.rglob("*.mp4"):
            if mp4.parent.name == VIDEO_SUBDIR:
                found.append(mp4)
    # Stable order: by course dir then filename.
    return sorted(set(found), key=lambda p: (str(p.parent).lower(), p.name))


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch .srt subtitle generator")
    parser.add_argument(
        "roots", nargs="*", default=[str(DEFAULT_ROOT)],
        help=f"Root dir(s) to scan (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Regenerate .srt even if one already exists",
    )
    args = parser.parse_args()

    roots = [Path(r).expanduser() for r in args.roots]
    videos = _find_videos(roots)
    if not videos:
        print(f"{_ts()} no mp4 found under: {', '.join(map(str, roots))}")
        return 0

    # Decide work list up front so we can show N / total.
    todo: list[Path] = []
    skipped = 0
    for mp4 in videos:
        srt = mp4.with_suffix(".srt")
        if srt.exists() and not args.overwrite:
            skipped += 1
            continue
        todo.append(mp4)

    print(
        f"{_ts()} found {len(videos)} videos · "
        f"{skipped} already have .srt · {len(todo)} to do",
        flush=True,
    )
    if not todo:
        print(f"{_ts()} nothing to do — all videos already subtitled.")
        return 0

    # Import here so the scan/usage message shows before the model loads.
    from src.transcriber import Transcriber  # noqa: E402

    transcriber = Transcriber()
    started = time.time()
    ok = silent = failed = 0

    for i, mp4 in enumerate(todo, 1):
        rel = f"{mp4.parent.parent.name}/{mp4.stem}"
        print(f"\n{_ts()} [{i}/{len(todo)}] {rel}", flush=True)
        t0 = time.time()
        try:
            transcript = transcriber.transcribe_video(str(mp4))
            srt = mp4.with_suffix(".srt")
            n = transcriber.write_srt(srt)
            dt = time.time() - t0
            if n == 0:
                # No speech (holiday / mic off) — drop the empty .srt.
                if srt.exists():
                    srt.unlink()
                silent += 1
                print(f"{_ts()}   silent (no speech) · {dt:.0f}s", flush=True)
            else:
                ok += 1
                print(
                    f"{_ts()}   .srt {n} cues · {len(transcript)} chars · "
                    f"{dt:.0f}s",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(
                f"{_ts()}   FAIL {type(exc).__name__}: {exc}",
                flush=True,
            )

        done = i
        elapsed = time.time() - started
        per = elapsed / done
        remaining = (len(todo) - done) * per
        eta_m = int(remaining // 60)
        print(
            f"{_ts()}   progress {done}/{len(todo)} · "
            f"ok {ok} · silent {silent} · failed {failed} · "
            f"ETA ~{eta_m}min",
            flush=True,
        )

    total_m = (time.time() - started) / 60
    print(
        f"\n{_ts()} DONE · {ok} subtitled · {silent} silent · "
        f"{failed} failed · {total_m:.0f}min total",
        flush=True,
    )
    transcriber.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
