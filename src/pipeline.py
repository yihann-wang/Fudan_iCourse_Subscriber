#!/usr/bin/env python3
"""Shared course pipeline for CLI and desktop applications.

This tool reuses the repository's WebVPN + iCourse auth/client code to
authenticate, list available playback lectures, and download selected videos
to local files.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path

# Force UTF-8 on our own stdout/stderr regardless of the host console code
# page (Chinese Windows defaults to GBK, which can't encode '·'/emoji and
# crashes print()). errors="replace" guarantees a stray glyph never aborts a
# log line. The GUI reads this pipe as UTF-8 too (see icourse_downloader_gui).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    pass


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_ROOT = PROJECT_ROOT / "tools"
TOOL_DIR = TOOLS_ROOT / "icourse_video_downloader"
VIDEO_SUBDIR = "录屏"
NOTES_SUBDIR = "笔记"
RAW_TXT_SUBDIR = "原始txt"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import task_events as events
from src.artifacts import (
    atomic_write_json,
    atomic_write_text,
    internal_path,
    local_media_id,
    migrate_auxiliary,
    migrate_course_metadata,
)
from src.pipeline_state import (
    PipelineState,
    artifact_metadata,
    mark_stage,
    valid_artifact,
)
from src.storage_access import StorageAccessError, check_pipeline_storage, storage_error
from src.summary_storage import archive_legacy_review_exports, archive_previous_note

_STATE = None
_TICK_KEY = "overall"


def _stage_queue(stage):
    return _STATE.queue(stage) if _STATE else queue.Queue()


def _begin_artifact(path):
    path = Path(path)
    atomic_write_json(migrate_auxiliary(path.with_suffix(path.suffix + ".icourse.json")), {"status": "writing"})


# Avoid interleaving multi-line per-lecture output across worker threads.
_PRINT_LOCK = threading.Lock()


def _log(message: str) -> None:
    """Print one logical message atomically."""
    with _PRINT_LOCK:
        print(message, flush=True)


def _ts() -> str:
    """HH:MM:SS timestamp prefix shared by all training-style log lines."""
    return time.strftime("[%H:%M:%S]")


def _tlog(message: str) -> None:
    """Timestamped log line, atomic across worker threads (a milestone)."""
    _log(f"{_ts()} {message}")


# True when launched by the GUI — it sets ICOURSE_GUI=1 and knows how to
# interpret the [PROG:key] refresh protocol. In a plain terminal we stay in
# append mode (each progress tick is just another timestamped line).
_GUI_MODE = os.environ.get("ICOURSE_GUI") == "1"


def _prog(key: str, message: str) -> None:
    """Emit a *refreshable* progress line for an in-flight task.

    In GUI mode the line is prefixed with ``[PROG:<key>] `` so the GUI can
    replace the previous line carrying the same key in place (tqdm-style),
    keeping each running task to a single self-updating line. In CLI mode it
    degrades to an ordinary timestamped append line.

    Live ticks carry NO timestamp in GUI mode — they're transient.
    """
    if events.enabled():
        return  # Structured progress is rendered in place; never duplicate it.
    if _GUI_MODE:
        _log(f"[PROG:{key}] {message}")
    else:
        _tlog(message)


def _prog_final(key: str, message: str) -> None:
    """Finalize a refreshable line into frozen history (done / fail).

    GUI: emits [PFIN:<key>] so the GUI moves that task's live line out of the
    pinned-bottom active region into frozen history, timestamped. CLI: a plain
    timestamped append (no double timestamp).
    """
    if events.enabled():
        return
    if _GUI_MODE:
        _log(f"[PFIN:{key}] {_ts()} {message}")
    else:
        _tlog(message)


# Placeholder written for a lecture whose audio has no recognizable speech
# (holiday / mic off / recording glitch). Detected on re-runs so we never
# re-transcribe or mis-classify it as a failure.
_SILENT_MARK = "[本节无可识别语音]"


class _Counters:
    """Thread-safe stage counters.

    Terminal states (each lecture lands in exactly one, so they sum to total):
      summarized / summary_skipped / silent / pending / failed
    `silent`  = no speech in audio (not a failure — placeholder written).
    `pending` = playback not published yet (not a failure — retried next run).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.summarized = 0
        self.transcript_only = 0
        self.summary_skipped = 0
        self.silent = 0
        self.pending = 0
        self.downloaded = 0
        self.download_skipped = 0
        self.failed = 0

    def inc(self, field: str, by: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + by)


def _load_env_file(path: Path, override: bool = False) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export "):].strip()

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]

        if override or key not in os.environ:
            os.environ[key] = value


def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _normalize_token(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("_", "")


_PERIOD_ALIASES = {
    "morning": [
        "morning", "am",
        "\u4e0a\u5348", "\u65e9\u4e0a", "\u65e9\u6668", "\u65e9\u8bfe",
    ],
    "afternoon": [
        "afternoon", "pm",
        "\u4e0b\u5348", "\u4e2d\u5348", "\u5348\u540e",
    ],
    "evening": [
        "evening", "night",
        "\u665a\u4e0a", "\u591c\u95f4", "\u665a\u8bfe",
    ],
}


_WEEKDAY_ALIASES = {
    0: ["mon", "monday", "\u661f\u671f\u4e00", "\u5468\u4e00", "\u793c\u62dc\u4e00"],
    1: ["tue", "tues", "tuesday", "\u661f\u671f\u4e8c", "\u5468\u4e8c", "\u793c\u62dc\u4e8c"],
    2: ["wed", "wednesday", "\u661f\u671f\u4e09", "\u5468\u4e09", "\u793c\u62dc\u4e09"],
    3: ["thu", "thur", "thurs", "thursday", "\u661f\u671f\u56db", "\u5468\u56db", "\u793c\u62dc\u56db"],
    4: ["fri", "friday", "\u661f\u671f\u4e94", "\u5468\u4e94", "\u793c\u62dc\u4e94"],
    5: ["sat", "saturday", "\u661f\u671f\u516d", "\u5468\u516d", "\u793c\u62dc\u516d"],
    6: ["sun", "sunday", "\u661f\u671f\u65e5", "\u661f\u671f\u5929", "\u5468\u65e5", "\u5468\u5929", "\u793c\u62dc\u65e5", "\u793c\u62dc\u5929"],
}


_WEEKDAY_LABELS = {
    0: "mon",
    1: "tue",
    2: "wed",
    3: "thu",
    4: "fri",
    5: "sat",
    6: "sun",
}


def _normalize_time_period(value: str) -> str | None:
    """Normalize user-facing time period labels to canonical values."""
    token = _normalize_token(value)
    for period, aliases in _PERIOD_ALIASES.items():
        for alias in aliases:
            if token == _normalize_token(alias):
                return period
    return None


def _extract_time_period_keyword(value: str) -> str | None:
    token = _normalize_token(value)
    for period, aliases in _PERIOD_ALIASES.items():
        for alias in aliases:
            if _normalize_token(alias) in token:
                return period
    return None


def _normalize_weekday(value: str) -> int | None:
    token = _normalize_token(value)
    for weekday, aliases in _WEEKDAY_ALIASES.items():
        for alias in aliases:
            if token == _normalize_token(alias):
                return weekday
    return None


def _extract_weekday_keyword(value: str) -> int | None:
    token = _normalize_token(value)
    for weekday, aliases in _WEEKDAY_ALIASES.items():
        for alias in aliases:
            if _normalize_token(alias) in token:
                return weekday
    return None


def _parse_course_skip_time_periods(raw: str) -> dict[str, dict[str, set]]:
    """Parse per-course time-period skip rules.

    Format:
      course_id:morning,evening,monday-morning,?????;course_id:afternoon
    """
    rules: dict[str, dict[str, set]] = {}
    if not raw.strip():
        return rules

    for course_block in re.split(r"[;\n]+", raw.strip()):
        block = course_block.strip()
        if not block:
            continue
        if ":" not in block:
            print(f"[Filter] ignore invalid rule (missing ':'): {block}")
            continue

        course_id, periods_part = block.split(":", 1)
        course_id = course_id.strip()
        if not course_id:
            print(f"[Filter] ignore invalid rule (empty course id): {block}")
            continue

        period_set: set[str] = set()
        weekday_period_set: set[tuple[int, str]] = set()

        for token in re.split(r"[,|;]+", periods_part):
            piece = token.strip()
            if not piece:
                continue

            period = _normalize_time_period(piece)
            if period is not None:
                period_set.add(period)
                continue

            weekday = _extract_weekday_keyword(piece)
            period = _extract_time_period_keyword(piece)
            if weekday is not None and period is not None:
                weekday_period_set.add((weekday, period))
                continue

            print(f"[Filter] ignore invalid time period for {course_id}: {piece}")

        if period_set or weekday_period_set:
            rules[course_id] = {
                "periods": period_set,
                "weekday_periods": weekday_period_set,
            }

    return rules


def _period_from_sections(text: str) -> str | None:
    """Parse period from expressions like '?3-4?'."""
    # Supports forms like: ?3-4?, 3-4?, 11?, ?11?12?
    match = re.search(
        r"(?:\u7b2c)?\s*(\d{1,2})(?:\s*[-~\u5230\u81f3]\s*(\d{1,2}))?\s*\u8282",
        text,
    )
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    mid = (start + end) / 2.0
    if mid <= 4.5:
        return "morning"
    if mid <= 8.5:
        return "afternoon"
    return "evening"


def _parse_loose_date(value: str) -> date | None:
    """Parse a date like YYYY-M-D from mixed strings."""
    match = re.search(r"(20\d{2})\D+(\d{1,2})\D+(\d{1,2})", value)
    if not match:
        return None
    y, m, d = map(int, match.groups())
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _extract_lecture_weekday(lecture: dict) -> int | None:
    """Best-effort weekday extraction (0=Mon ... 6=Sun)."""
    # Prefer the visible lecture title/local filename over the API "date" field.
    # Some iCourse payloads expose the teaching-week date in `date`, while the
    # actual playback date is encoded in `sub_title` like `2026-03-09第11-12节`.
    candidates = [
        str(lecture.get("sub_title", "")),
        str(lecture.get("title", "")),
    ]
    local_path = lecture.get("local_path")
    if local_path:
        try:
            candidates.append(Path(local_path).stem)
        except Exception:
            candidates.append(str(local_path))
    candidates.append(str(lecture.get("date", "")))

    for text in candidates:
        parsed = _parse_loose_date(text)
        if parsed is not None:
            return parsed.weekday()
        weekday = _extract_weekday_keyword(text)
        if weekday is not None:
            return weekday
    return None


def _extract_lecture_time_period(lecture: dict) -> str | None:
    """Best-effort time-period extraction from API/local fields."""
    candidates = [
        str(lecture.get("sub_title", "")),
        str(lecture.get("date", "")),
        str(lecture.get("title", "")),
    ]
    local_path = lecture.get("local_path")
    if local_path:
        try:
            candidates.append(Path(local_path).stem)
        except Exception:
            candidates.append(str(local_path))

    for text in candidates:
        lower = text.lower()
        if any(x in lower for x in ("morning", "am")):
            return "morning"
        if any(x in lower for x in ("afternoon", "pm")):
            return "afternoon"
        if any(x in lower for x in ("evening", "night")):
            return "evening"

        phrase_period = _extract_time_period_keyword(text)
        if phrase_period is not None:
            return phrase_period

        section_period = _period_from_sections(text)
        if section_period is not None:
            return section_period

    return None


def _apply_course_time_period_skip_rules(
    course_id: str,
    lectures: list[dict],
    skip_rules: dict[str, dict[str, set]],
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Apply per-course period skip rules and return (kept, skipped)."""
    config = skip_rules.get(str(course_id))
    if not config:
        return lectures, []

    periods = set(config.get("periods", set()))
    weekday_periods = set(config.get("weekday_periods", set()))

    kept: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    for lecture in lectures:
        period = _extract_lecture_time_period(lecture)
        if period is None:
            kept.append(lecture)
            continue

        if period in periods:
            skipped.append((lecture, period))
            continue

        weekday = _extract_lecture_weekday(lecture)
        if weekday is not None and (weekday, period) in weekday_periods:
            reason = f"{_WEEKDAY_LABELS.get(weekday, str(weekday))}-{period}"
            skipped.append((lecture, reason))
            continue

        kept.append(lecture)
    return kept, skipped


def _safe_filename(text: str, fallback: str = "lecture") -> str:
    text = text.strip()
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return fallback
    return text[:100].rstrip()


def _sub_id_sort_key(item: dict) -> tuple[int, str]:
    sub_id = str(item.get("sub_id", ""))
    if sub_id.isdigit():
        return (0, f"{int(sub_id):020d}")
    return (1, sub_id)


def _extract_sub_id_from_name(path: Path) -> str | None:
    """Extract sub_id from local filename stem.

    Supports both layouts:
    - New (preferred): `<title>_<sub_id>` — sorts naturally by date prefix.
    - Old: `<sub_id>_<title>` — kept for backward compat.

    sub_id is matched as 5+ consecutive digits so we don't pick up the
    4-digit year inside dates like "2026-04-17".
    """
    stem = path.stem.strip()
    local = re.search(r"_(local-[0-9a-f]{24})$", stem)
    if local:
        return local.group(1)
    # New format: title_<sub_id> at end (preferred)
    match = re.search(r"_(\d{5,})$", stem)
    if match:
        return match.group(1)
    # Old format: <sub_id>_title at start
    match = re.match(r"^(\d{5,})(?:[_-].*)?$", stem)
    if match:
        return match.group(1)
    # Bare sub_id (no title)
    if stem.isdigit() and len(stem) >= 5:
        return stem
    return None


def _extract_local_sub_title(path: Path, sub_id: str) -> str:
    """Extract lecture title from local filename stem (handles both layouts)."""
    stem = path.stem.strip()
    if sub_id.startswith("local-"):
        return stem.removesuffix("_" + sub_id)
    # New format: <title>_<sub_id>
    match = re.match(r"^(.+)_\d{5,}$", stem)
    if match and match.group(1).strip():
        return match.group(1).strip()
    # Old format: <sub_id>_<title>
    match = re.match(r"^\d{5,}[_-](.+)$", stem)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return sub_id


def _resolve_course_home(path: Path) -> Path:
    """Map paths under layout subdirs back to the course root directory."""
    if path.name in {VIDEO_SUBDIR, NOTES_SUBDIR, RAW_TXT_SUBDIR}:
        return path.parent
    return path


def _layout_paths(course_dir: Path) -> tuple[Path, Path, Path]:
    """Return (video_dir, notes_dir, raw_txt_dir) for a course directory."""
    course_home = _resolve_course_home(course_dir)
    return (
        course_home / VIDEO_SUBDIR,
        course_home / NOTES_SUBDIR,
        course_home / RAW_TXT_SUBDIR,
    )


def _move_legacy_artifacts_to_layout(course_dir: Path) -> None:
    """Move legacy flat files into 录屏/笔记/原始txt layout."""
    if not course_dir.exists() or not course_dir.is_dir():
        return

    course_home = _resolve_course_home(course_dir)
    video_dir, notes_dir, raw_txt_dir = _layout_paths(course_home)
    for suffix, target_dir in (
        (".mp4", video_dir),
        (".md", notes_dir),
        (".txt", raw_txt_dir),
    ):
        for file_path in sorted(course_home.glob(f"*{suffix}")):
            if not file_path.is_file():
                continue
            if _extract_sub_id_from_name(file_path) is None:
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / file_path.name
            if target_path.exists():
                continue
            file_path.replace(target_path)
            # Keep existing commit markers attached when arranging flat files.
            companions = [file_path.with_suffix(file_path.suffix + ".icourse.json")]
            if suffix == ".txt":
                companions.append(file_path.with_suffix(".segments.json"))
            for old in companions:
                companion = migrate_auxiliary(old)
                if companion.exists():
                    target = internal_path(target_dir / old.name)
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        companion.rename(target)
    migrate_course_metadata(course_home)


def _guess_course_title(course_dirs: list[Path], course_id: str) -> str:
    """Best-effort course title guess from local directory names."""
    pattern = re.compile(rf"^{re.escape(course_id)}[-_](.+)$")
    for course_dir in course_dirs:
        if not course_dir.exists() or not course_dir.is_dir():
            continue
        match = pattern.match(course_dir.name)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return f"course_{course_id}"


def _collect_local_lectures(course_dirs: list[Path], sub_ids_filter: set[str]) -> list[dict]:
    """Collect local lectures from mp4 files in course dirs."""
    lectures = []
    seen = set()
    for course_dir in course_dirs:
        if not course_dir.exists() or not course_dir.is_dir():
            continue
        for video_path in sorted(course_dir.rglob("*.mp4")):
            if not video_path.is_file():
                continue
            sub_id = _extract_sub_id_from_name(video_path)
            if not sub_id:
                sub_id = local_media_id(video_path)
            if sub_ids_filter and sub_id not in sub_ids_filter:
                continue
            if sub_id in seen:
                continue
            seen.add(sub_id)
            lectures.append(
                {
                    "sub_id": sub_id,
                    "sub_title": _extract_local_sub_title(video_path, sub_id),
                    "date": "",
                    "local_path": video_path,
                }
            )
    return sorted(lectures, key=_sub_id_sort_key)


def _scan_existing_sub_ids(course_dirs: list[Path], pattern: str) -> set[str]:
    """Collect existing sub_ids by scanning files matching glob pattern."""
    sub_ids = set()
    for course_dir in course_dirs:
        if not course_dir.exists() or not course_dir.is_dir():
            continue
        for file_path in course_dir.rglob(pattern):
            if not file_path.is_file():
                continue
            parts = file_path.relative_to(course_dir).parts
            if ".icourse" in parts or (file_path.suffix == ".md" and "待核对" in parts):
                continue
            sub_id = _extract_sub_id_from_name(file_path)
            if sub_id:
                sub_ids.add(sub_id)
    return sub_ids


def _scan_downloaded_sub_ids(course_dirs: list[Path]) -> set[str]:
    """Collect downloaded sub_ids by scanning existing mp4 filenames."""
    return _scan_existing_sub_ids(course_dirs, "*.mp4")


def _scan_summarized_sub_ids(course_dirs: list[Path]) -> set[str]:
    """Collect summarized sub_ids by scanning existing markdown filenames."""
    return _scan_existing_sub_ids(course_dirs, "*.md")


def _resolve_course_dirs(out_dir: Path, course_id: str, course_title: str) -> tuple[Path, list[Path]]:
    """Return target course dir and all same-course dirs for local scan."""
    dir_name = f"{course_id}-{_safe_filename(course_title, fallback='course')}"
    target_dir = out_dir / dir_name
    same_course_dirs = []
    if out_dir.exists():
        for d in out_dir.iterdir():
            if not d.is_dir():
                continue
            if d.name.startswith(f"{course_id}-") or d.name.startswith(f"{course_id}_"):
                same_course_dirs.append(d)
    if target_dir not in same_course_dirs:
        same_course_dirs.append(target_dir)
    return target_dir, same_course_dirs


def _find_file_by_sub_id(course_dirs: list[Path], sub_id: str, suffix: str) -> Path | None:
    """Find first file whose name encodes the exact sub_id."""
    suffix = suffix.lower()
    for course_dir in course_dirs:
        if not course_dir.exists() or not course_dir.is_dir():
            continue
        for file_path in sorted(course_dir.rglob(f"*{suffix}")):
            if not file_path.is_file():
                continue
            parts = file_path.relative_to(course_dir).parts
            if ".icourse" in parts or (suffix == ".md" and "待核对" in parts):
                continue
            if file_path.suffix.lower() != suffix:
                continue
            parsed = _extract_sub_id_from_name(file_path)
            if parsed == sub_id and valid_artifact(file_path):
                return file_path
    return None


def _find_file_for_lecture(course_dirs: list[Path], sub_title: str,
                            sub_id: str, suffix: str) -> Path | None:
    """Only reuse artifacts with an exact identity; a title is not an ID."""
    return _find_file_by_sub_id(course_dirs, sub_id, suffix)



def _format_size(num_bytes: float) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(num_bytes)
    unit_idx = 0
    while value >= 1024 and unit_idx < len(units) - 1:
        value /= 1024
        unit_idx += 1
    return f"{value:.1f}{units[unit_idx]}"


def _format_eta(seconds: float) -> str:
    if seconds < 0:
        return "--:--"
    total = int(seconds)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _bar(pct: float, width: int = 18) -> str:
    """pip/wget-style ASCII progress bar: '=======>          ' for 0..100 pct.

    Pure ASCII — renders identically on any font/locale (we share the exe).
    The '>' head stays visible until exactly 100% (floor, not round).
    """
    pct = max(0.0, min(100.0, float(pct)))
    if pct >= 100.0:
        return "=" * width
    if pct <= 0.0:
        return " " * width
    filled = int(pct / 100.0 * width)  # floor → never fully solid before 100%
    return "=" * filled + ">" + " " * (width - filled - 1)


def _pulse(elapsed: float, width: int = 18, block: int = 4) -> str:
    """Indeterminate bar: a '====' block bouncing left↔right (ASCII).

    Used when total size is unknown (CDN sent no content-length) so the user
    still sees motion. Position is a function of elapsed seconds.
    """
    span = max(width - block, 1)
    # Triangle wave 0..span..0 over a ~4s period per sweep.
    phase = int(elapsed * 4) % (2 * span)
    pos = phase if phase <= span else 2 * span - phase
    return " " * pos + "=" * block + " " * (width - block - pos)


def _download_video_with_progress(client, video_url: str, output_path: Path,
                                   chunk_size: int = 1024 * 256,
                                   sub_id: str | None = None,
                                   tag: str = "", prog_key: str | None = None,
                                   title: str = "") -> Path:
    """Download one video with a refreshable progress line.

    In GUI mode the progress updates a single line in place (tqdm-style) via
    _prog(prog_key, ...); in CLI mode it degrades to one append line per tick.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    start = time.time()
    last_print = start
    downloaded = 0
    # Refresh ~1/s in GUI (cheap, in-place) but stay sparse in CLI append mode.
    interval = 1.0 if _GUI_MODE else 5.0

    resp = client.get_video_response(video_url)
    try:
        resp.raise_for_status()
        if "text/html" in resp.headers.get("content-type", "").lower():
            raise RuntimeError("下载返回登录页面，请重新登录。")
        total = int(resp.headers.get("content-length", 0))
    except Exception:
        resp.close()
        raise

    try:
        with tmp_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                now = time.time()
                if now - last_print < interval:
                    continue
                elapsed = max(now - start, 1e-6)
                avg_speed = downloaded / elapsed
                events.progress("正在下载", phase="download", unit="bytes", completed=downloaded,
                                total=total, speed=avg_speed)

                if total > 0:
                    pct = downloaded * 100 / total
                    remaining = max(total - downloaded, 0)
                    eta = remaining / max(avg_speed, 1e-6)
                    msg = (
                        f"dl  {title} {tag} "
                        f"[{_bar(pct)}] {pct:5.1f}% · "
                        f"{_format_size(downloaded)}/{_format_size(total)} · "
                        f"{_format_size(avg_speed)}/s · "
                        f"ETA {_format_eta(eta)}"
                    )
                else:
                    # CDN gave no content-length → no %, show a spinner-ish bar
                    # driven by elapsed so the user still sees motion.
                    msg = (
                        f"dl  {title} {tag} "
                        f"[{_pulse(elapsed)}] {_format_size(downloaded)} · "
                        f"{_format_size(avg_speed)}/s · {_format_eta(elapsed)}"
                    )
                if prog_key:
                    _prog(prog_key, msg)
                else:
                    _tlog(msg)
                last_print = now
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    finally:
        resp.close()

    elapsed = max(time.time() - start, 1e-6)
    avg_speed = downloaded / elapsed
    if total > 0 and downloaded < total:
        if tmp_path.exists():
            tmp_path.unlink()
        raise RuntimeError(
            f"Incomplete download: {downloaded}/{total} bytes "
            f"({downloaded / total:.1%})"
        )

    from src.media import probe
    try:
        probe(tmp_path)
        _begin_artifact(output_path)
        os.replace(tmp_path, output_path)
        artifact_metadata(output_path, kind="video")
    finally:
        tmp_path.unlink(missing_ok=True)
    return output_path


def _ticker_thread(counters: "_Counters", total: int,
                    started_at: float, stop_event: threading.Event,
                    needs_summary: bool, interval: float = 60.0) -> None:
    """Refresh a single overall-progress line every `interval` seconds.

    Reads from the shared _Counters; doesn't track per-task in-flight state
    (each stage worker already logs its own start/done lines for that).
    """
    def emit() -> None:
        elapsed = time.time() - started_at
        # Count every terminal state so done reaches total at the end.
        terminal = counters.silent + counters.pending + counters.failed
        done = terminal + (
            counters.summarized + counters.summary_skipped
            if needs_summary
            else counters.downloaded + counters.download_skipped
        )
        pct = (done / total * 100) if total else 0
        eta_str = ""
        if 0 < done < total:
            per = elapsed / done
            remaining = (total - done) * per
            eta_str = f" · ETA {_format_eta(remaining)}"
        extra = ""
        if counters.silent:
            extra += f" · 无声 {counters.silent}"
        if counters.pending:
            extra += f" · 暂无 {counters.pending}"
        if counters.failed:
            extra += f" · 失败 {counters.failed}"
        _prog(
            _TICK_KEY,
            f"总进度 [{_bar(pct)}] {done}/{total} ({pct:.0f}%)"
            f" · dl {counters.downloaded}+{counters.download_skipped}"
            f" · sm {counters.summarized}+{counters.summary_skipped}"
            f"{extra}"
            f" · elapsed {_format_eta(elapsed)}{eta_str}"
        )

    emit()  # show the overall line immediately, not after the first interval
    # wait() returns True the moment stop_event is set → prompt shutdown.
    while not stop_event.wait(timeout=interval):
        emit()


def _stage_event(task, stage, status, message=""):
    path_key = {"dl": "target_video_path", "tr": "target_transcript_path", "sm": "target_summary_path"}[stage]
    events.stage(task, stage, status, message, path=str(task.get(path_key) or ""))


def _task_tag(task: dict) -> str:
    return f"[{task['course_id']}/{task['sub_id']}]"


def _task_title(task: dict, max_len: int = 36) -> str:
    title = task.get("sub_title") or task["sub_id"]
    return title if len(title) <= max_len else title[:max_len - 1] + "…"


def _stage_failed(in_q, counters, task, stage, exc):
    message = events.redact(f"{type(exc).__name__}: {exc}")
    # Signed media URLs should never be written to logs or the task database.
    message = re.sub(r"https?://[^\s]+", "[URL]", message)
    mark_stage(in_q, "failed", message)
    counters.inc("failed")
    _prog_final(f"{stage}:{task['sub_id']}", f"{stage} FAIL {_task_tag(task)} · {message}")
    _stage_event(task, stage, "failed", message)


def _download_stage(in_q, out_q, client, sleep_sec, counters):
    while True:
        task = in_q.get()
        try:
            if task is None:
                if out_q is not None:
                    out_q.put(None)
                return
            _stage_event(task, "dl", "running")
            try:
                video_path = None
                for attempt in range(3):
                    try:
                        video_url = client.get_video_url(task["course_id"], task["sub_id"])
                        if not video_url:
                            break
                        video_path = _download_video_with_progress(
                            client, video_url, task["target_video_path"],
                            sub_id=task["sub_id"], tag=_task_tag(task),
                            prog_key=f"dl:{task['sub_id']}", title=_task_title(task),
                        )
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        if not client.check_alive():
                            from src.icourse import ICourseClient
                            from src.webvpn import WebVPNSession
                            client = ICourseClient(_login_with_retry(WebVPNSession, max_attempts=2))
                        events.progress("下载失败，等待重试", phase="retry", attempt=attempt + 1,
                                        max_attempts=3, retry_seconds=2 ** attempt)
                        time.sleep(2 ** attempt)
                if video_path is None:
                    mark_stage(in_q, "pending", "回放尚未发布")
                    counters.inc("pending")
                    _stage_event(task, "dl", "pending", "回放尚未发布")
                    continue
                task["video_path"] = video_path
                counters.inc("downloaded")
                _prog_final(f"dl:{task['sub_id']}", f"dl done {_task_tag(task)} · {video_path.name}")
                _stage_event(task, "dl", "done")
                if out_q is not None:
                    out_q.put(task)
            except Exception as exc:
                _stage_failed(in_q, counters, task, "dl", exc)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
        finally:
            in_q.task_done()


def _transcribe_stage(in_q, out_q, transcriber_factory, counters):
    while True:
        task = in_q.get()
        try:
            if task is None:
                if out_q is not None:
                    out_q.put(None)
                return
            _stage_event(task, "tr", "running")
            try:
                video = Path(task["video_path"])
                transcriber = transcriber_factory()
                result = transcriber.transcribe_result(
                    str(video), prog_key=f"tr:{task['sub_id']}",
                    title=_task_title(task), tag=_task_tag(task),
                )
                transcript_path = task["target_transcript_path"]
                atomic_write_json(migrate_auxiliary(transcript_path.with_suffix(".segments.json")), result.to_dict())
                if not result.text.strip():
                    raise RuntimeError("未识别到语音，请检查录音；不会自动生成已完成的空笔记。")
                if not result.complete:
                    raise RuntimeError("无法核对音频完整性，请检查录音元数据。")
                _begin_artifact(transcript_path)
                atomic_write_text(transcript_path, result.text.strip())
                transcriber.write_srt(video.with_suffix(".srt"))
                artifact_metadata(transcript_path, source=video,
                                  settings_fingerprint=result.settings_fingerprint,
                                  course_id=task["course_id"], sub_id=task["sub_id"],
                                  backend=result.backend, model=result.model, revision=result.model_revision)
                task["transcript"] = ""  # downstream reads committed text; no large queue payload
                _prog_final(f"tr:{task['sub_id']}", f"tr done {_task_tag(task)} · {len(result.text)} 字")
                _stage_event(task, "tr", "done", " ".join(result.warnings))
                if out_q is not None:
                    out_q.put(task)
            except Exception as exc:
                _stage_failed(in_q, counters, task, "tr", exc)
        finally:
            in_q.task_done()


def _summarize_stage(in_q, summarizer, sleep_sec, counters,
                     summarized_sub_ids, summarized_sub_ids_lock):
    from src.summarizer import summary_fingerprint
    while True:
        task = in_q.get()
        try:
            if task is None:
                return
            _stage_event(task, "sm", "running")
            watch_stop = threading.Event()
            watcher = None
            try:
                if task.get("transcript_missing"):
                    raise RuntimeError("没有找到可复用的完整转录；请先完成转录，再选择只重新生成笔记。")
                transcript = task.get("transcript") or task["target_transcript_path"].read_text(encoding="utf-8").strip()
                if not transcript or transcript == _SILENT_MARK:
                    raise RuntimeError("转录文本为空，需要检查录音。")
                started = time.monotonic()
                segment_status = "正在准备整课原文"

                def progress(message):
                    nonlocal segment_status
                    segment_status = message
                    _prog(f"sm:{task['sub_id']}", f"sm {_task_title(task)} · {message}")

                def watch():
                    while not watch_stop.wait(2):
                        _prog(f"sm:{task['sub_id']}", f"sm {_task_title(task)} · {segment_status} · {_format_eta(time.monotonic() - started)}")

                if not events.enabled():
                    watcher = threading.Thread(target=watch, daemon=True)
                    watcher.start()
                checkpoint = internal_path(task["target_summary_path"].with_suffix(".summary-progress.json"))
                result = summarizer.summarize(
                    task["course_title"], transcript, checkpoint_path=checkpoint,
                    progress=progress, overwrite=task.get("overwrite", False),
                )
                summary, model = result.text, result.model
                if not summary.strip():
                    raise RuntimeError("摘要为空。")
                path = task["target_summary_path"]
                events.progress("正在保存笔记", phase="saving", model=model)
                document = f"### {task['sub_title']}\n\n{summary.strip()}\n"
                archive_previous_note(path, document)
                _begin_artifact(path)
                atomic_write_text(path, document)
                artifact_metadata(path, source=task["target_transcript_path"],
                                  settings_fingerprint=summary_fingerprint(), model=model,
                                  status="complete", generation="single_pass",
                                  course_id=task["course_id"], sub_id=task["sub_id"])
                try:
                    archive_legacy_review_exports(path, task["course_id"], task["sub_id"])
                except OSError:
                    _log("[Summarizer] 笔记已保存；旧版辅助文件暂时无法归档。")
                # The final artifact is committed; its duplicate segment notes
                # are no longer needed. Keep checkpoints on every failure.
                try:
                    checkpoint.unlink(missing_ok=True)
                except OSError:
                    _log("[Summarizer] 笔记已保存；暂时无法清理写作缓存。")
                with summarized_sub_ids_lock:
                    summarized_sub_ids.add(task["sub_id"])
                counters.inc("summarized")
                _prog_final(f"sm:{task['sub_id']}", f"sm done {_task_tag(task)} · {len(summary)} 字 · {model}")
                _stage_event(task, "sm", "done")
            except Exception as exc:
                _stage_failed(in_q, counters, task, "sm", exc)
            finally:
                watch_stop.set()
                if watcher:
                    watcher.join(timeout=3)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
        finally:
            in_q.task_done()


def _resolve_target_path(target_dir: Path, safe_title: str, sub_id: str,
                          ext: str) -> Path:
    """Always include identity, including before either file exists."""
    return target_dir / f"{safe_title}_{sub_id}{ext}"



def _make_task(*, lec: dict, course_id: str, course_title: str,
               video_dir: Path | None, notes_dir: Path, raw_txt_dir: Path,
               existing_video_path: Path | None = None, overwrite=False) -> dict:
    """Build a pipeline LectureTask dict.

    Filenames include a stable server ID or a content-based local ID.
    """
    sub_id = str(lec["sub_id"])
    sub_title = lec.get("sub_title", sub_id)
    safe_title = _safe_filename(sub_title, fallback=sub_id)
    video_path = existing_video_path or lec.get("local_path")
    target_video_path = (
        _resolve_target_path(video_dir, safe_title, sub_id, ".mp4")
        if video_dir is not None
        else (Path(video_path) if video_path else None)
    )
    return {
        "sub_id": sub_id,
        "sub_title": sub_title,
        "course_id": course_id,
        "course_title": course_title,
        "overwrite": overwrite,
        "video_path": Path(video_path) if video_path else None,
        "target_video_path": target_video_path,
        "target_transcript_path": _resolve_target_path(raw_txt_dir, safe_title, sub_id, ".txt"),
        "target_summary_path": _resolve_target_path(notes_dir, safe_title, sub_id, ".md"),
        "transcript": "",
    }


def _announce_task(task, entry, mode, *, video=None, transcript=None, summary=None):
    stages = dict(dl="cached" if video else "na", tr="cached" if transcript else "na", sm="na")
    if mode != "download":
        stages["sm"] = "cached" if summary else "waiting"
    if entry:
        names = ("dl", "tr", "sm") if mode != "download" else ("dl",)
        for name in names[names.index(entry):]:
            stages[name] = "queued" if name == entry else "waiting"
    events.plan_task(task, stages, dl=video or task.get("target_video_path"),
                     tr=transcript or task.get("target_transcript_path"),
                     sm=summary or task.get("target_summary_path"))


def _filter_targets(lectures, course_id, targets):
    return list({str(lec["sub_id"]): lec for lec in lectures
                 if not targets or (str(course_id), str(lec["sub_id"])) in targets}.values())


def _build_parser(default_env_file: Path, default_out_dir: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download iCourse playback videos to local files.",
    )
    parser.add_argument(
        "--env-file",
        default=str(default_env_file),
        help=(
            "Path to env file "
            f"(default: {default_env_file}; if missing, fallback to project .env)"
        ),
    )
    parser.add_argument(
        "--course-ids",
        default="",
        help="Comma-separated course IDs. Falls back to COURSE_IDS in env.",
    )
    parser.add_argument(
        "--skip-time-periods",
        default="",
        help=(
            "Per-course time periods to skip. "
            "Format: 32890:morning,evening,monday-morning,星期一早上;35652:afternoon. "
            "Supported period labels: morning/afternoon/evening and 上午/下午/晚上. "
            "Falls back to COURSE_SKIP_TIME_PERIODS in env."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("download", "summarize", "download_and_summarize"),
        default="download",
        help=(
            "Run mode: download videos only, summarize local videos only, "
            "or download then summarize each lecture."
        ),
    )
    parser.add_argument(
        "--sub-ids",
        default="",
        help="Comma-separated lecture sub_id values. Empty means all playback lectures.",
    )
    parser.add_argument("--target", action="append", default=[], metavar="COURSE:LECTURE",
                        help="Restrict work to exact course/lecture pairs; may be repeated.")
    parser.add_argument(
        "--out-dir",
        default="",
        help=(
            "Base video output directory. "
            f"Default: DOWNLOAD_DIR in env, else {default_out_dir}"
        ),
    )
    parser.add_argument(
        "--summary-dir",
        default="",
        help=(
            "Base summary output directory (used by summarize modes). "
            "Default: SUMMARY_DIR in env, else tools/summary"
        ),
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List matching lectures only, do not download.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Overwrite transcript/summary outputs. "
            "Existing MP4 files are still reused and not re-downloaded."
        ),
    )
    parser.add_argument("--resume-stage", action="append", default=[], metavar="COURSE:LECTURE:STAGE",
                        help="Retry selected failures from dl, tr or sm, reusing earlier artifacts.")
    parser.add_argument("--redo-notes", action="store_true",
                        help="Regenerate notes using existing verified transcripts; never download or transcribe missing input.")
    parser.add_argument(
        "--login-retries",
        type=int,
        default=3,
        help="Max login attempts (default: 3).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.2,
        help="Sleep seconds between downloads (default: 0.2).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "[deprecated] kept for backward compat. The current pipeline runs"
            " 1 download + 1 transcribe + 1 LLM call concurrently, regardless"
            " of this value."
        ),
    )
    return parser


def _login_with_retry(webvpn_cls, max_attempts: int):
    """Create authenticated WebVPN+iCourse session with retry."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"[Login] attempt {attempt}/{max_attempts}")
            vpn = webvpn_cls()
            vpn.login()
            vpn.authenticate_icourse()
            return vpn
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[Login] failed: {type(exc).__name__}: {exc}")
            if attempt < max_attempts:
                time.sleep(2)
    raise RuntimeError(f"Login failed after {max_attempts} attempts") from last_error


def _run_main() -> int:
    default_env_file = TOOL_DIR / ".env"
    fallback_env_file = PROJECT_ROOT / ".env"
    default_out_dir = Path.home() / "iCourse" / "课程" if sys.platform == "darwin" else TOOLS_ROOT / "course"
    default_summary_dir = Path.home() / "iCourse" / "笔记" if sys.platform == "darwin" else TOOLS_ROOT / "summary"
    parser = _build_parser(default_env_file, default_out_dir)
    args = parser.parse_args()
    if args.redo_notes and (args.overwrite or args.mode == "download"):
        raise ValueError("只重新生成笔记不能与重新转录或只下载模式同时使用。")

    requested_env_file = Path(args.env_file).expanduser()
    using_default_env_arg = requested_env_file == default_env_file
    if using_default_env_arg and not requested_env_file.exists() and fallback_env_file.exists():
        env_file = fallback_env_file.resolve()
    else:
        env_file = requested_env_file.resolve()
    print(f"[Env] Loading env file: {env_file}")
    _load_env_file(env_file)

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    mode = args.mode
    needs_download = mode in ("download", "download_and_summarize")
    needs_summary = mode in ("summarize", "download_and_summarize")

    course_ids = list(dict.fromkeys(_parse_csv(args.course_ids or os.environ.get("COURSE_IDS", ""))))
    targets = set()
    for value in args.target:
        parts = value.split(":")
        if len(parts) != 2 or not all(parts) or parts[0] not in course_ids:
            raise ValueError("重试课次必须使用已选择的课程 ID 和课次 ID。")
        targets.add(tuple(parts))
    resume_stages = {}
    for value in args.resume_stage:
        parts = value.split(":")
        if len(parts) != 3 or tuple(parts[:2]) not in targets or parts[2] not in {"dl", "tr", "sm"}:
            raise ValueError("重试阶段必须属于本次选择的课次，且为 dl、tr 或 sm。")
        if args.overwrite or args.redo_notes or (mode == "download" and parts[2] != "dl"):
            raise ValueError("重试阶段与本次任务模式不兼容。")
        resume_stages[tuple(parts[:2])] = parts[2]
    seen_targets = set()
    sub_ids_filter = set(_parse_csv(args.sub_ids))
    skip_period_rules_raw = (
        args.skip_time_periods
        or os.environ.get("COURSE_SKIP_TIME_PERIODS", "")
    )
    skip_period_rules = _parse_course_skip_time_periods(skip_period_rules_raw)
    if skip_period_rules:
        print(f"[Filter] Loaded period skip rules for {len(skip_period_rules)} course(s).")
    configured_out_dir = (
        args.out_dir
        or os.environ.get("DOWNLOAD_DIR", "")
        or str(default_out_dir)
    )
    if sys.platform != "win32" and re.match(r"^[A-Za-z]:[\\/]", configured_out_dir):
        raise ValueError("下载目录是 Windows 路径，请在 Mac 界面中选择本地文件夹。")
    out_dir_path = Path(configured_out_dir).expanduser()
    if not out_dir_path.is_absolute():
        out_dir_path = PROJECT_ROOT / out_dir_path
    out_dir = out_dir_path.resolve()
    configured_summary_dir = (
        args.summary_dir
        or os.environ.get("SUMMARY_DIR", "")
        or str(default_summary_dir)
    )
    if sys.platform != "win32" and re.match(r"^[A-Za-z]:[\\/]", configured_summary_dir):
        raise ValueError("笔记目录是 Windows 路径，请在 Mac 界面中选择本地文件夹。")
    summary_dir_path = Path(configured_summary_dir).expanduser()
    if not summary_dir_path.is_absolute():
        summary_dir_path = PROJECT_ROOT / summary_dir_path
    summary_dir = summary_dir_path.resolve()

    # Fail before school login, downloads, model loading or paid note requests.
    events.emit("phase", message="正在检查保存目录")
    check_pipeline_storage(out_dir, summary_dir, mode=mode, list_only=args.list_only)

    # One lightweight network process per pipeline, created only if needed.
    _transcriber_lock = threading.Lock()
    _transcriber_holder: dict = {}

    def _transcriber_factory():
        with _transcriber_lock:
            if "instance" not in _transcriber_holder:
                _log("    [init] preparing cloud transcription...")
                from src.transcriber import Transcriber  # pylint: disable=import-error
                _transcriber_holder["instance"] = Transcriber()
            return _transcriber_holder["instance"]

    # Local-only summarize mode: no WebVPN login required.
    if mode == "summarize":
        if not course_ids:
            print("No course IDs provided. Use --course-ids or set COURSE_IDS in .env.")
            return 1

        summarizer = None
        if not args.list_only:
            from src.summarizer import Summarizer  # pylint: disable=import-error

            summarizer = Summarizer()

        total_targets = 0
        counters = _Counters()
        summarized_sub_ids_global: set = set()
        summarized_sub_ids_lock = threading.Lock()

        # Build all tasks across all courses, pre-classifying each into the
        # right stage queue based on what's already on disk.
        transcribe_q = _stage_queue("transcribe")
        summarize_q = _stage_queue("summarize")

        for course_id in course_ids:
            events.emit("phase", message=f"正在检查课程 {course_id} 的本地文件")
            print(f"\n[Course] {course_id}")
            _, video_scan_dirs = _resolve_course_dirs(out_dir, course_id, f"course_{course_id}")
            if not args.list_only:
                for course_dir in video_scan_dirs:
                    _move_legacy_artifacts_to_layout(course_dir)
            course_title = _guess_course_title(video_scan_dirs, course_id)
            local_lectures = _collect_local_lectures(video_scan_dirs, sub_ids_filter)
            selected, skipped_by_period = _apply_course_time_period_skip_rules(
                course_id, local_lectures, skip_period_rules
            )
            selected = _filter_targets(selected, course_id, targets)
            seen_targets.update((course_id, str(lec["sub_id"])) for lec in selected)
            events.emit("course", course_id=course_id, course_title=course_title, empty=not selected)

            print(f"  Title: {course_title}")
            print(f"  Local videos: {len(local_lectures)}")
            print(f"  Selected: {len(selected)}")
            if skipped_by_period:
                print(f"  Skipped by period rule: {len(skipped_by_period)}")
                for lec, period in sorted(skipped_by_period, key=lambda x: _sub_id_sort_key(x[0])):
                    sub_id = str(lec.get("sub_id", ""))
                    sub_title = lec.get("sub_title", sub_id)
                    print(f"  - [skip-period] [{sub_id}] {sub_title} ({period})")

            for lec in selected:
                sub_id = str(lec["sub_id"])
                sub_title = lec.get("sub_title", sub_id)
                print(f"  - [{sub_id}] {sub_title}")

            total_targets += len(selected)
            if not selected:
                continue
            if args.list_only:
                continue

            summary_course_dir, summary_scan_dirs = _resolve_course_dirs(
                summary_dir, course_id, course_title
            )
            for course_dir in summary_scan_dirs:
                _move_legacy_artifacts_to_layout(course_dir)
            summarized_sub_ids = _scan_summarized_sub_ids(summary_scan_dirs)
            if summarized_sub_ids:
                print(f"  Local summaries (by sub_id scan): {len(summarized_sub_ids)}")
            _, notes_dir, raw_txt_dir = _layout_paths(summary_course_dir)
            notes_dir.mkdir(parents=True, exist_ok=True)
            raw_txt_dir.mkdir(parents=True, exist_ok=True)

            for lec in selected:
                sub_id = str(lec["sub_id"])
                sub_title = lec.get("sub_title", sub_id)
                existing_summary_path = _find_file_for_lecture(summary_scan_dirs, sub_title, sub_id, ".md")
                existing_transcript_path = _find_file_for_lecture(summary_scan_dirs, sub_title, sub_id, ".txt")

                resume = resume_stages.get((course_id, sub_id))
                force_transcribe = args.overwrite or resume == "tr"
                redo_notes = args.redo_notes or resume == "sm"
                task = _make_task(
                    lec=lec, course_id=course_id, course_title=course_title,
                    video_dir=None, notes_dir=notes_dir, raw_txt_dir=raw_txt_dir, overwrite=force_transcribe,
                )

                if not force_transcribe and not redo_notes and existing_summary_path is not None:
                    _log(f"    [skip-summary] summary exists sub_id={sub_id}")
                    counters.inc("summary_skipped")
                    _announce_task(task, None, mode, video=lec.get("local_path"),
                                   transcript=existing_transcript_path, summary=existing_summary_path)
                    continue

                if redo_notes:
                    task["transcript_missing"] = existing_transcript_path is None
                    if existing_transcript_path is not None:
                        task["target_transcript_path"] = existing_transcript_path
                    _announce_task(task, "sm", mode, video=lec.get("local_path"), transcript=existing_transcript_path)
                    summarize_q.put(task)
                elif not force_transcribe and existing_transcript_path is not None:
                    task["target_transcript_path"] = existing_transcript_path
                    _announce_task(task, "sm", mode, video=lec.get("local_path"), transcript=existing_transcript_path)
                    summarize_q.put(task)
                else:
                    _announce_task(task, "tr", mode, video=lec.get("local_path"))
                    transcribe_q.put(task)

        if targets - seen_targets:
            raise ValueError("未找到指定课次：" + ", ".join(":".join(t) for t in sorted(targets - seen_targets)))
        if args.list_only:
            print(f"\n[Done] targets: {total_targets} (list-only)")
            return 0

        # 2-stage pipeline (no download): transcribe → summarize.
        events.emit("planned", total=total_targets)
        transcribe_q.put(None)  # sentinel cascades to summarize_q

        pipeline_started_at = time.time()
        ticker_stop = threading.Event()
        ticker = threading.Thread(
            target=_ticker_thread,
            args=(counters, total_targets, pipeline_started_at,
                  ticker_stop, True),  # needs_summary=True
            daemon=True,
        )

        _tlog(
            f"pipeline start · {total_targets} targets · "
            "mode=summarize · pipe=transcribe(1)→summarize(1)"
        )
        ticker.start()
        t_thread = threading.Thread(
            target=_transcribe_stage,
            args=(transcribe_q, summarize_q, _transcriber_factory, counters),
            daemon=True,
        )
        s_thread = threading.Thread(
            target=_summarize_stage,
            args=(summarize_q, summarizer, args.sleep, counters,
                  summarized_sub_ids_global, summarized_sub_ids_lock),
            daemon=True,
        )
        t_thread.start()
        s_thread.start()
        t_thread.join()
        s_thread.join()
        ticker_stop.set()
        ticker.join(timeout=2)

        total_elapsed = _format_eta(time.time() - pipeline_started_at)
        # Freeze the overall line into the final summary (no dangling tick).
        _prog_final(
            _TICK_KEY,
            f"==== pipeline done · {total_targets} targets in {total_elapsed} · "
            f"summarized {counters.summarized} · "
            f"skip-sm {counters.summary_skipped} · 无声 {counters.silent} · "
            f"暂无 {counters.pending} · failed {counters.failed}"
        )

        mode_desc = args.mode + (" (list-only)" if args.list_only else "")
        _tlog(
            f"summary: mode={mode_desc} · video={out_dir} · summary={summary_dir}"
        )
        if "instance" in _transcriber_holder:
            _transcriber_holder["instance"].close()
        return 1 if counters.failed else 0

    from src.icourse import ICourseClient  # pylint: disable=import-error
    from src.webvpn import WebVPNSession  # pylint: disable=import-error

    if not os.environ.get("StuId") or not os.environ.get("UISPsw"):
        print("Missing StuId/UISPsw. Set them in env file or shell environment.")
        return 1

    if not course_ids:
        print("No course IDs provided. Use --course-ids or set COURSE_IDS in .env.")
        return 1

    events.emit("phase", message="正在登录学校并获取课程列表")
    vpn = _login_with_retry(WebVPNSession, max_attempts=max(1, args.login_retries))
    client = ICourseClient(vpn)

    summarizer = None
    if not args.list_only and needs_summary:
        from src.summarizer import Summarizer  # pylint: disable=import-error
        summarizer = Summarizer()

    total_targets = 0
    counters = _Counters()
    summarized_sub_ids_global: set = set()
    summarized_sub_ids_lock = threading.Lock()

    # Three stage queues. Tasks are pre-classified into the right entry queue
    # based on what's already on disk — so e.g. an existing transcript skips
    # straight to the summarize stage without burning download/transcribe time.
    download_q = _stage_queue("download")
    transcribe_q = _stage_queue("transcribe")
    summarize_q = _stage_queue("summarize")

    for course_id in course_ids:
        events.emit("phase", message=f"正在读取课程 {course_id}")
        print(f"\n[Course] {course_id}")
        detail = client.get_course_detail(course_id)
        course_title = detail.get("title", f"course_{course_id}")
        lectures = detail.get("lectures", [])
        playback_lectures = [lec for lec in lectures if lec.get("has_playback")]
        playback_map = {str(lec["sub_id"]): lec for lec in playback_lectures}

        if sub_ids_filter:
            selected = []
            for sub_id in sub_ids_filter:
                lec = playback_map.get(sub_id)
                if lec:
                    selected.append(lec)
                else:
                    print(f"  - skip sub_id={sub_id}: not found or no playback")
        else:
            selected = playback_lectures

        selected, skipped_by_period = _apply_course_time_period_skip_rules(
            course_id, selected, skip_period_rules
        )
        selected = sorted(_filter_targets(selected, course_id, targets), key=_sub_id_sort_key)
        seen_targets.update((course_id, str(lec["sub_id"])) for lec in selected)
        events.emit("course", course_id=course_id, course_title=course_title, empty=not selected)
        print(f"  Title: {course_title}")
        print(f"  Playback lectures: {len(playback_lectures)}")
        print(f"  Selected: {len(selected)}")
        if skipped_by_period:
            print(f"  Skipped by period rule: {len(skipped_by_period)}")
            for lec, period in sorted(skipped_by_period, key=lambda x: _sub_id_sort_key(x[0])):
                sub_id = str(lec.get("sub_id", ""))
                sub_title = lec.get("sub_title", sub_id)
                print(f"  - [skip-period] [{sub_id}] {sub_title} ({period})")

        if not selected:
            continue

        for lec in selected:
            sub_id = str(lec["sub_id"])
            sub_title = lec.get("sub_title", sub_id)
            lec_date = lec.get("date", "")
            total_targets += 1
            print(f"  - [{sub_id}] {sub_title} ({lec_date})")

        if args.list_only:
            continue

        course_dir, scan_dirs = _resolve_course_dirs(out_dir, course_id, course_title)
        for scan_dir in scan_dirs:
            _move_legacy_artifacts_to_layout(scan_dir)
        downloaded_sub_ids = _scan_downloaded_sub_ids(scan_dirs)
        if needs_download and downloaded_sub_ids:
            print(f"  Local downloaded videos (by sub_id scan): {len(downloaded_sub_ids)}")
        video_dir, _, _ = _layout_paths(course_dir)
        if needs_download:
            video_dir.mkdir(parents=True, exist_ok=True)

        summary_scan_dirs = []
        # Download-only must not read, migrate or create anything in the notes root.
        summary_course_dir = summary_dir / course_dir.name
        _, notes_dir, raw_txt_dir = _layout_paths(summary_course_dir)
        if needs_summary:
            summary_course_dir, summary_scan_dirs = _resolve_course_dirs(
                summary_dir, course_id, course_title
            )
            for scan_dir in summary_scan_dirs:
                _move_legacy_artifacts_to_layout(scan_dir)
            summarized_sub_ids = _scan_summarized_sub_ids(summary_scan_dirs)
            if summarized_sub_ids:
                print(f"  Local summaries (by sub_id scan): {len(summarized_sub_ids)}")
            _, notes_dir, raw_txt_dir = _layout_paths(summary_course_dir)
            notes_dir.mkdir(parents=True, exist_ok=True)
            raw_txt_dir.mkdir(parents=True, exist_ok=True)

        # Classify each lecture into its starting stage.
        for lec in selected:
            sub_id = str(lec["sub_id"])
            sub_title = lec.get("sub_title", sub_id)
            existing_video_path = _find_file_for_lecture(scan_dirs, sub_title, sub_id, ".mp4")
            existing_summary_path = _find_file_for_lecture(summary_scan_dirs, sub_title, sub_id, ".md") if needs_summary else None
            existing_transcript_path = _find_file_for_lecture(summary_scan_dirs, sub_title, sub_id, ".txt") if needs_summary else None

            resume = resume_stages.get((course_id, sub_id))
            force_transcribe = args.overwrite or resume == "tr"
            redo_notes = args.redo_notes or resume == "sm"
            task = _make_task(
                lec=lec, course_id=course_id, course_title=course_title,
                video_dir=video_dir, notes_dir=notes_dir, raw_txt_dir=raw_txt_dir,
                existing_video_path=existing_video_path, overwrite=force_transcribe,
            )

            if needs_summary and not force_transcribe and not redo_notes and existing_summary_path is not None:
                _log(f"    [skip-summary] summary exists sub_id={sub_id}")
                counters.inc("summary_skipped")
                if needs_download and existing_video_path:
                    counters.inc("download_skipped")
                _announce_task(task, None, mode, video=existing_video_path,
                               transcript=existing_transcript_path, summary=existing_summary_path)
                continue

            if redo_notes:
                task["transcript_missing"] = existing_transcript_path is None
                if existing_transcript_path is not None:
                    task["target_transcript_path"] = existing_transcript_path
                _announce_task(task, "sm", mode, video=existing_video_path, transcript=existing_transcript_path)
                summarize_q.put(task)
                if needs_download and existing_video_path:
                    counters.inc("download_skipped")
                continue

            # Existing transcript → skip download + transcribe, straight to LLM
            if (
                needs_summary
                and not force_transcribe
                and existing_transcript_path is not None
            ):
                task["target_transcript_path"] = existing_transcript_path
                _announce_task(task, "sm", mode, video=existing_video_path, transcript=existing_transcript_path)
                summarize_q.put(task)
                if needs_download and existing_video_path:
                    counters.inc("download_skipped")
                continue

            # Existing video → skip download, still transcribe + (optionally) summarize
            if existing_video_path:
                _log(f"    [skip-download] already downloaded sub_id={sub_id}")
                counters.inc("download_skipped")
                _announce_task(task, "tr" if needs_summary else None, mode, video=existing_video_path)
                if needs_summary:
                    transcribe_q.put(task)
                continue

            # No existing artifacts → start from download stage (if mode allows)
            if needs_download:
                _announce_task(task, "dl", mode)
                download_q.put(task)
            elif needs_summary:
                # mode=summarize with no local video — fail at transcribe stage
                _announce_task(task, "tr", mode)
                transcribe_q.put(task)

    if targets - seen_targets:
        raise ValueError("未找到指定课次：" + ", ".join(":".join(t) for t in sorted(targets - seen_targets)))
    if args.list_only:
        print(f"\n[Done] targets: {total_targets} (list-only)")
        return 0

    # Spawn 3 stage workers (each capped at 1 in-flight task). Sentinel flows:
    events.emit("planned", total=total_targets)
    # download_q → transcribe_q → summarize_q.
    download_q.put(None)

    pipeline_started_at = time.time()
    ticker_stop = threading.Event()
    pipe_parts = []
    if needs_download:
        pipe_parts.append("download(1)")
    if needs_summary:
        pipe_parts.extend(["transcribe(1)", "summarize(1)"])
    _tlog(
        f"pipeline start · {total_targets} targets · "
        f"mode={args.mode} · pipe=" + "→".join(pipe_parts)
    )
    ticker = threading.Thread(
        target=_ticker_thread,
        args=(counters, total_targets, pipeline_started_at,
              ticker_stop, needs_summary),
        daemon=True,
    )
    ticker.start()

    threads: list[threading.Thread] = []
    if needs_download:
        d_thread = threading.Thread(
            target=_download_stage,
            args=(download_q, transcribe_q if needs_summary else None,
                  client, args.sleep, counters),
            daemon=True,
        )
        threads.append(d_thread)
        d_thread.start()
    else:
        # Forward download sentinel manually so transcribe stage terminates.
        transcribe_q.put(None)

    if needs_summary:
        t_thread = threading.Thread(
            target=_transcribe_stage,
            args=(transcribe_q, summarize_q, _transcriber_factory, counters),
            daemon=True,
        )
        threads.append(t_thread)
        t_thread.start()

        s_thread = threading.Thread(
            target=_summarize_stage,
            args=(summarize_q, summarizer, args.sleep, counters,
                  summarized_sub_ids_global, summarized_sub_ids_lock),
            daemon=True,
        )
        threads.append(s_thread)
        s_thread.start()

    for th in threads:
        th.join()
    ticker_stop.set()
    ticker.join(timeout=2)

    total_elapsed = _format_eta(time.time() - pipeline_started_at)
    mode_desc = args.mode + (" (list-only)" if args.list_only else "")
    summary_bits = [f"{total_targets} targets in {total_elapsed}"]
    if needs_download:
        summary_bits.append(f"downloaded {counters.downloaded}")
        summary_bits.append(f"skip-dl {counters.download_skipped}")
        summary_bits.append(f"暂无 {counters.pending}")
    if needs_summary:
        summary_bits.append(f"summarized {counters.summarized}")
        summary_bits.append(f"skip-sm {counters.summary_skipped}")
        summary_bits.append(f"无声 {counters.silent}")
    summary_bits.append(f"failed {counters.failed}")
    # Freeze the overall line into the final summary (no dangling tick).
    _prog_final(_TICK_KEY, "==== pipeline done · " + " · ".join(summary_bits))
    _tlog(f"summary: mode={mode_desc}")
    if needs_download:
        _tlog(f"summary: video dir = {out_dir}")
    if needs_summary:
        _tlog(f"summary: summary dir = {summary_dir}")
    if "instance" in _transcriber_holder:
        _transcriber_holder["instance"].close()
    return 1 if counters.failed else 0


def main() -> int:
    from filelock import FileLock, Timeout
    from platformdirs import user_data_path
    global _STATE
    state_dir = Path(os.environ.get("ICOURSE_STATE_DIR") or user_data_path("Fudan iCourse", appauthor=False))
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(state_dir / "pipeline.lock", timeout=0):
            _STATE = PipelineState(state_dir / "pipeline.sqlite3", run_id=os.environ.get("ICOURSE_RUN_ID"))
            events.begin_run(_STATE.run_id)
            try:
                code = _run_main()
                events.emit("run_finished", status="failed" if code else "done")
                return code
            finally:
                _STATE.close()
                _STATE = None
    except Timeout:
        print("另一个课程任务正在运行，请等待完成后重试。")
        return 2
    except StorageAccessError as exc:
        print(f"[存储错误] {exc}")
        return 1
    except PermissionError as exc:
        # Also explain denials that occur after preflight, e.g. a protected child.
        print(f"[存储错误] {storage_error(Path(exc.filename or state_dir), '文件或目录', exc)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
