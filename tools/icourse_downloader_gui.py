#!/usr/bin/env python3
"""GUI launcher for iCourse downloader with persistent profile storage."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from queue import Empty, Queue
from tkinter import BOTH, END, LEFT, RIGHT, X, Y
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk


APP_TITLE = "iCourse 下载/总结 GUI"
CONFIG_FILENAME = ".icourse_gui_config.json"
APP_STATE_FILENAME = ".icourse_gui_state.json"
MODES = ("download", "summarize", "download_and_summarize")

WEEKDAY_LABEL_TO_TOKEN = {
    "每天": "",
    "周一": "monday",
    "周二": "tuesday",
    "周三": "wednesday",
    "周四": "thursday",
    "周五": "friday",
    "周六": "saturday",
    "周日": "sunday",
}
WEEKDAY_TOKEN_TO_LABEL = {
    token: label for label, token in WEEKDAY_LABEL_TO_TOKEN.items() if token
}
PERIOD_LABEL_TO_TOKEN = {
    "上午": "morning",
    "下午": "afternoon",
    "晚上": "evening",
}
PERIOD_TOKEN_TO_LABEL = {
    token: label for label, token in PERIOD_LABEL_TO_TOKEN.items()
}
ASR_ENV_KEYS = {
    "WHISPER_MODEL",
    "WHISPER_DEVICE",
    "WHISPER_COMPUTE_TYPE",
    "WHISPER_LANGUAGE",
    "WHISPER_BEAM_SIZE",
    "WHISPER_VAD_FILTER",
}

WEEKDAY_ALIASES = {
    "monday": ("monday", "mon", "星期一", "周一", "礼拜一"),
    "tuesday": ("tuesday", "tue", "tues", "星期二", "周二", "礼拜二"),
    "wednesday": ("wednesday", "wed", "星期三", "周三", "礼拜三"),
    "thursday": ("thursday", "thu", "thur", "thurs", "星期四", "周四", "礼拜四"),
    "friday": ("friday", "fri", "星期五", "周五", "礼拜五"),
    "saturday": ("saturday", "sat", "星期六", "周六", "礼拜六"),
    "sunday": ("sunday", "sun", "星期日", "星期天", "周日", "周天", "礼拜日", "礼拜天"),
}
PERIOD_ALIASES = {
    "morning": ("morning", "am", "上午", "早上", "早晨", "早课"),
    "afternoon": ("afternoon", "pm", "下午", "中午", "午后"),
    "evening": ("evening", "night", "晚上", "夜间", "晚课"),
}


def _resolve_project_root() -> Path:
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).resolve()
        if exe_path.parent.name.lower() == "dist":
            return exe_path.parent.parent
        if exe_path.parent.parent.name.lower() == "dist":
            return exe_path.parent.parent.parent
        return exe_path.parent
    return Path(__file__).resolve().parent.parent


def _resolve_user_path(base_dir: Path, value: str, default_name: str) -> Path:
    raw = value.strip()
    if not raw:
        return (base_dir / default_name).resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    if not path.suffix:
        path = path.with_suffix(".json")
    return path.resolve()


def _set_default_window_geometry(root: tk.Tk) -> None:
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()

    width = min(1440, max(1120, screen_w - 120))
    height = min(940, max(780, screen_h - 140))
    width = min(width, max(980, screen_w - 40))
    height = min(height, max(700, screen_h - 80))

    x = max((screen_w - width) // 2, 0)
    y = max((screen_h - height) // 2, 0)

    root.geometry(f"{width}x{height}+{x}+{y}")
    root.minsize(min(width, 1180), min(height, 760))


def _enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _safe_float(value: str, default: float = 0.2) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _read_env_file_values(path: Path, keys: set[str]) -> dict[str, str]:
    """Read selected KEY=VALUE pairs without leaking unrelated .env values."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
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
        if key not in keys:
            continue

        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _hidden_subprocess_kwargs() -> dict:
    """Hide spawned console windows on Windows."""
    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _normalize_token(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def _match_alias(value: str, aliases: dict[str, tuple[str, ...]]) -> str | None:
    token = _normalize_token(value)
    if not token:
        return None
    for canonical, words in aliases.items():
        for word in words:
            word_token = _normalize_token(word)
            if token == word_token or word_token in token:
                return canonical
    return None


def _parse_rule_token(value: str) -> tuple[str | None, str | None]:
    raw = value.strip()
    if not raw:
        return None, None

    if "-" in raw:
        left, right = raw.split("-", 1)
        weekday = _match_alias(left, WEEKDAY_ALIASES)
        period = _match_alias(right, PERIOD_ALIASES)
        if weekday and period:
            return weekday, period
        weekday = _match_alias(right, WEEKDAY_ALIASES)
        period = _match_alias(left, PERIOD_ALIASES)
        if weekday and period:
            return weekday, period

    weekday = _match_alias(raw, WEEKDAY_ALIASES)
    period = _match_alias(raw, PERIOD_ALIASES)
    if weekday and period:
        return weekday, period
    if period:
        return None, period
    return None, None


class DownloaderGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        _set_default_window_geometry(self.root)
        self.project_root = _resolve_project_root()
        self.state_path = self.project_root / APP_STATE_FILENAME
        self.config_path = self._load_last_config_path()
        self.proc: subprocess.Popen | None = None
        self.log_queue: Queue[str] = Queue()
        self.stop_event = threading.Event()
        # Frozen history lines (append-only, in display order) + the live
        # "active" task lines that stay pinned at the bottom and refresh in
        # place (tqdm-multibar style). Widget layout is always:
        #   line 1..H   = log_lines (frozen)
        #   line H+1..  = active_prog values (live, one per in-flight task)
        self.log_lines: list[str] = []
        self.active_prog: dict[str, str] = {}
        self.log_window: tk.Toplevel | None = None
        self.log_text_widget: tk.Text | None = None

        self.vars: dict[str, tk.Variable] = {
            "mode": tk.StringVar(value="summarize"),
            "stu_id": tk.StringVar(),
            "uis_psw": tk.StringVar(),
            "course_ids": tk.StringVar(),
            "skip_time_periods": tk.StringVar(),
            "sub_ids": tk.StringVar(),
            "out_dir": tk.StringVar(value=str(self.project_root / "tools" / "course")),
            "summary_dir": tk.StringVar(value=str(self.project_root / "tools" / "summary")),
            "sleep": tk.StringVar(value="0.2"),
            "overwrite": tk.BooleanVar(value=False),
            "list_only": tk.BooleanVar(value=False),
            "llm_name_1": tk.StringVar(value="provider1"),
            "llm_api_key_1": tk.StringVar(),
            "llm_base_url_1": tk.StringVar(),
            "llm_models_1": tk.StringVar(value="gpt-5.4"),
            "whisper_model": tk.StringVar(value="large-v3-turbo"),
            "whisper_device": tk.StringVar(value="cuda"),
            "whisper_compute_type": tk.StringVar(value="float16"),
            "whisper_language": tk.StringVar(value="zh"),
        }
        self.config_path_var = tk.StringVar(value=str(self.config_path))

        self.rule_course_var = tk.StringVar()
        self.rule_weekday_var = tk.StringVar(value="每天")
        self.rule_period_var = tk.StringVar(value="上午")

        # Progress now flows through the regular log stream as timestamped
        # single-line entries (AI-training-style). No GUI-side state tracking
        # is needed; the downloader subprocess prints everything we need.

        self._build_ui()
        self.vars["course_ids"].trace_add("write", self._on_course_ids_change)

        self._load_config(silent=True)
        self._update_rule_course_options()
        self._load_rules_from_skip_text(self.vars["skip_time_periods"].get(), silent=True)

        self.root.update_idletasks()
        self.root.deiconify()
        self.root.after(200, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=BOTH, expand=True)

        top_row = ttk.Frame(main)
        top_row.pack(fill=X, padx=4, pady=4)
        top_row.columnconfigure(0, weight=3)
        top_row.columnconfigure(1, weight=2)

        config_box = ttk.LabelFrame(top_row, text="运行配置")
        config_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        config_box.columnconfigure(1, weight=1)

        self._row_mode(config_box, 0)
        self._row_entry(config_box, 1, "StuId", "stu_id")
        self._row_entry(config_box, 2, "UISPsw", "uis_psw", show="*")
        self._row_entry(config_box, 3, "COURSE_IDS (逗号分隔)", "course_ids")
        self._row_entry(config_box, 4, "SUB_IDS (可空)", "sub_ids")
        self._row_path(config_box, 5, "out-dir", "out_dir", pick_dir=True)
        self._row_path(config_box, 6, "summary-dir", "summary_dir", pick_dir=True)
        self._row_entry(config_box, 7, "--sleep", "sleep")
        self._row_config_path(config_box, 8)
        self._row_flags(config_box, 9)

        side_box = ttk.Frame(top_row)
        side_box.grid(row=0, column=1, sticky="nsew")
        side_box.columnconfigure(0, weight=1)

        llm_box = ttk.LabelFrame(side_box, text="LLM 配置（summarize 用）")
        llm_box.grid(row=0, column=0, sticky="new")
        llm_box.columnconfigure(1, weight=1)
        self._row_entry(llm_box, 0, "LLM_NAME_1", "llm_name_1")
        self._row_entry(llm_box, 1, "LLM_API_KEY_1", "llm_api_key_1", show="*")
        self._row_entry(llm_box, 2, "LLM_BASE_URL_1", "llm_base_url_1")
        self._row_entry(llm_box, 3, "LLM_MODELS_1", "llm_models_1")
        self.btn_test_llm = ttk.Button(
            llm_box, text="测试 LLM", command=self._test_llm,
        )
        self.btn_test_llm.grid(row=4, column=1, sticky="e", padx=4, pady=(4, 4))

        action_box = ttk.LabelFrame(side_box, text="操作")
        action_box.grid(row=1, column=0, sticky="new", pady=(8, 0))
        self._build_action_bar(action_box)

        self._build_skip_rule_box(main)

    def _build_action_bar(self, parent: ttk.Widget) -> None:
        action_bar = ttk.Frame(parent, padding=8)
        action_bar.pack(fill=X)
        self.btn_run = ttk.Button(action_bar, text="运行", command=self._start_run)
        self.btn_run.pack(side=LEFT, padx=3)
        self.btn_stop = ttk.Button(action_bar, text="停止", command=self._stop_run, state="disabled")
        self.btn_stop.pack(side=LEFT, padx=3)
        ttk.Button(action_bar, text="保存配置", command=self._save_config).pack(side=LEFT, padx=3)
        ttk.Button(action_bar, text="加载配置", command=self._load_config).pack(side=LEFT, padx=3)
        ttk.Button(action_bar, text="查看日志", command=self._show_log_window).pack(side=LEFT, padx=3)
        ttk.Button(action_bar, text="清空日志", command=self._clear_log).pack(side=LEFT, padx=3)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(action_bar, textvariable=self.status_var).pack(side=RIGHT, padx=6)

    def _build_skip_rule_box(self, parent: ttk.Widget) -> None:
        rule_box = ttk.LabelFrame(parent, text="排除规则（按周几 + 时间段）")
        rule_box.pack(fill=X, padx=4, pady=4)
        rule_box.columnconfigure(7, weight=1)

        ttk.Label(rule_box, text="课程ID").grid(row=0, column=0, sticky="w", padx=5, pady=4)
        self.rule_course_combo = ttk.Combobox(
            rule_box,
            textvariable=self.rule_course_var,
            width=16,
            state="normal",
        )
        self.rule_course_combo.grid(row=0, column=1, sticky="w", padx=5, pady=4)

        ttk.Label(rule_box, text="周几").grid(row=0, column=2, sticky="w", padx=5, pady=4)
        ttk.Combobox(
            rule_box,
            textvariable=self.rule_weekday_var,
            values=list(WEEKDAY_LABEL_TO_TOKEN.keys()),
            width=10,
            state="readonly",
        ).grid(row=0, column=3, sticky="w", padx=5, pady=4)

        ttk.Label(rule_box, text="时间段").grid(row=0, column=4, sticky="w", padx=5, pady=4)
        ttk.Combobox(
            rule_box,
            textvariable=self.rule_period_var,
            values=list(PERIOD_LABEL_TO_TOKEN.keys()),
            width=10,
            state="readonly",
        ).grid(row=0, column=5, sticky="w", padx=5, pady=4)

        ttk.Button(rule_box, text="添加规则", command=self._add_rule_from_controls).grid(
            row=0, column=6, sticky="w", padx=5, pady=4
        )
        ttk.Button(rule_box, text="删除选中", command=self._remove_selected_rules).grid(
            row=0, column=7, sticky="w", padx=5, pady=4
        )

        columns = ("course", "weekday", "period", "token")
        self.rule_tree = ttk.Treeview(rule_box, columns=columns, show="headings", height=4, selectmode="extended")
        self.rule_tree.heading("course", text="课程ID")
        self.rule_tree.heading("weekday", text="周几")
        self.rule_tree.heading("period", text="时间段")
        self.rule_tree.heading("token", text="规则token")
        self.rule_tree.column("course", width=120, anchor="center")
        self.rule_tree.column("weekday", width=90, anchor="center")
        self.rule_tree.column("period", width=90, anchor="center")
        self.rule_tree.column("token", width=260, anchor="w")
        self.rule_tree.grid(row=1, column=0, columnspan=8, sticky="we", padx=5, pady=4)

        tree_scroll = ttk.Scrollbar(rule_box, orient="vertical", command=self.rule_tree.yview)
        tree_scroll.grid(row=1, column=8, sticky="ns", padx=(0, 5), pady=4)
        self.rule_tree.configure(yscrollcommand=tree_scroll.set)

        bar = ttk.Frame(rule_box)
        bar.grid(row=2, column=0, columnspan=8, sticky="w", padx=5, pady=2)
        ttk.Button(bar, text="清空全部规则", command=self._clear_rules).pack(side=LEFT, padx=3)
        ttk.Button(bar, text="从文本应用", command=self._apply_skip_text).pack(side=LEFT, padx=3)

        ttk.Label(rule_box, text="COURSE_SKIP_TIME_PERIODS").grid(
            row=3, column=0, sticky="w", padx=5, pady=4
        )
        ttk.Entry(
            rule_box,
            textvariable=self.vars["skip_time_periods"],
            width=108,
        ).grid(row=3, column=1, columnspan=8, sticky="we", padx=5, pady=4)

    def _row_mode(self, parent: ttk.Widget, row: int) -> None:
        ttk.Label(parent, text="mode").grid(row=row, column=0, sticky="w", padx=5, pady=4)
        combo = ttk.Combobox(
            parent,
            textvariable=self.vars["mode"],
            values=MODES,
            state="readonly",
            width=32,
        )
        combo.grid(row=row, column=1, columnspan=2, sticky="we", padx=5, pady=4)

    def _row_entry(
        self,
        parent: ttk.Widget,
        row: int,
        label: str,
        key: str,
        show: str | None = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(parent, textvariable=self.vars[key], width=86, show=show or "").grid(
            row=row, column=1, columnspan=2, sticky="we", padx=5, pady=4
        )

    def _row_path(
        self,
        parent: ttk.Widget,
        row: int,
        label: str,
        key: str,
        pick_dir: bool = False,
        pick_file: bool = False,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(parent, textvariable=self.vars[key], width=74).grid(
            row=row, column=1, sticky="we", padx=5, pady=4
        )
        ttk.Button(
            parent,
            text="浏览",
            command=lambda: self._browse_path(key, pick_dir=pick_dir, pick_file=pick_file),
        ).grid(row=row, column=2, sticky="e", padx=5, pady=4)

    def _row_config_path(self, parent: ttk.Widget, row: int) -> None:
        ttk.Label(parent, text="配置文件").grid(row=row, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(parent, textvariable=self.config_path_var, width=74).grid(
            row=row, column=1, sticky="we", padx=5, pady=4
        )
        action_box = ttk.Frame(parent)
        action_box.grid(row=row, column=2, sticky="e", padx=5, pady=4)
        ttk.Button(action_box, text="浏览", command=self._browse_config_path).pack(side=LEFT, padx=2)
        ttk.Button(action_box, text="默认", command=self._reset_config_path).pack(side=LEFT, padx=2)

    def _row_flags(self, parent: ttk.Widget, row: int) -> None:
        wrapper = ttk.Frame(parent)
        wrapper.grid(row=row, column=0, columnspan=3, sticky="w", padx=5, pady=4)
        ttk.Checkbutton(wrapper, text="overwrite", variable=self.vars["overwrite"]).pack(side=LEFT, padx=4)
        ttk.Checkbutton(wrapper, text="list-only", variable=self.vars["list_only"]).pack(side=LEFT, padx=4)

    def _browse_path(self, key: str, pick_dir: bool = False, pick_file: bool = False) -> None:
        current = self.vars[key].get().strip()
        init_dir = current or str(self.project_root)
        if pick_dir:
            selected = filedialog.askdirectory(initialdir=init_dir)
        elif pick_file:
            selected = filedialog.askopenfilename(initialdir=init_dir)
        else:
            selected = filedialog.askopenfilename(initialdir=init_dir)
        if selected:
            self.vars[key].set(selected)

    def _get_config_path(self) -> Path:
        path = _resolve_user_path(self.project_root, self.config_path_var.get(), CONFIG_FILENAME)
        self.config_path = path
        self.config_path_var.set(str(path))
        return path

    def _load_last_config_path(self) -> Path:
        default_path = (self.project_root / CONFIG_FILENAME).resolve()
        if not self.state_path.exists():
            return default_path
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            raw_path = str(data.get("config_path", "")).strip()
            if not raw_path:
                return default_path
            return _resolve_user_path(self.project_root, raw_path, CONFIG_FILENAME)
        except Exception:
            return default_path

    def _save_app_state(self) -> None:
        state = {"config_path": str(self._get_config_path())}
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _browse_config_path(self) -> None:
        current = self._get_config_path()
        selected = filedialog.asksaveasfilename(
            initialdir=str(current.parent),
            initialfile=current.name,
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if selected:
            self.config_path_var.set(selected)
            try:
                self._save_app_state()
                self._append_log(f"[GUI] 配置文件位置已切换: {self._get_config_path()}")
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("保存失败", f"{type(exc).__name__}: {exc}")

    def _reset_config_path(self) -> None:
        self.config_path_var.set(str((self.project_root / CONFIG_FILENAME).resolve()))
        try:
            self._save_app_state()
            self._append_log(f"[GUI] 配置文件位置已重置: {self._get_config_path()}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", f"{type(exc).__name__}: {exc}")

    def _on_course_ids_change(self, *_args) -> None:
        self._update_rule_course_options()

    def _update_rule_course_options(self) -> None:
        options = _parse_csv(self.vars["course_ids"].get())
        self.rule_course_combo["values"] = options
        current = self.rule_course_var.get().strip()
        if not current and options:
            self.rule_course_var.set(options[0])

    def _make_rule_token(self, weekday_label: str, period_label: str) -> str:
        weekday_token = WEEKDAY_LABEL_TO_TOKEN.get(weekday_label, "")
        period_token = PERIOD_LABEL_TO_TOKEN.get(period_label, "morning")
        if weekday_token:
            return f"{weekday_token}-{period_token}"
        return period_token

    def _insert_rule_row(
        self,
        course_id: str,
        weekday_label: str,
        period_label: str,
        token: str,
        dedupe: bool = True,
    ) -> bool:
        course_id = course_id.strip()
        token = token.strip()
        if not course_id or not token:
            return False
        if dedupe:
            for item in self.rule_tree.get_children():
                values = self.rule_tree.item(item, "values")
                if len(values) < 4:
                    continue
                if str(values[0]).strip() == course_id and str(values[3]).strip() == token:
                    return False
        self.rule_tree.insert("", END, values=(course_id, weekday_label, period_label, token))
        return True

    def _add_rule_from_controls(self) -> None:
        course_id = self.rule_course_var.get().strip()
        if not course_id:
            messagebox.showwarning("提示", "请先填写课程ID后再添加规则。")
            return
        weekday_label = self.rule_weekday_var.get().strip() or "每天"
        period_label = self.rule_period_var.get().strip() or "上午"
        token = self._make_rule_token(weekday_label, period_label)
        added = self._insert_rule_row(course_id, weekday_label, period_label, token, dedupe=True)
        if not added:
            messagebox.showinfo("提示", "该规则已存在。")
            return
        self._sync_skip_text_from_tree()

    def _remove_selected_rules(self) -> None:
        selected = self.rule_tree.selection()
        if not selected:
            return
        for item in selected:
            self.rule_tree.delete(item)
        self._sync_skip_text_from_tree()

    def _clear_rules(self) -> None:
        for item in self.rule_tree.get_children():
            self.rule_tree.delete(item)
        self._sync_skip_text_from_tree()

    def _sync_skip_text_from_tree(self) -> None:
        if not hasattr(self, "rule_tree"):
            return
        grouped: dict[str, list[str]] = {}
        for item in self.rule_tree.get_children():
            values = self.rule_tree.item(item, "values")
            if len(values) < 4:
                continue
            course_id = str(values[0]).strip()
            token = str(values[3]).strip()
            if not course_id or not token:
                continue
            grouped.setdefault(course_id, [])
            if token not in grouped[course_id]:
                grouped[course_id].append(token)
        merged = ";".join(f"{cid}:{','.join(tokens)}" for cid, tokens in grouped.items())
        self.vars["skip_time_periods"].set(merged)

    def _load_rules_from_skip_text(self, raw: str, silent: bool = True) -> None:
        if not hasattr(self, "rule_tree"):
            return
        for item in self.rule_tree.get_children():
            self.rule_tree.delete(item)

        invalid_tokens: list[str] = []
        text = (raw or "").strip()
        if text:
            for block in re.split(r"[;\n]+", text):
                part = block.strip()
                if not part:
                    continue
                if ":" not in part:
                    invalid_tokens.append(part)
                    continue
                course_id, tokens_part = part.split(":", 1)
                course_id = course_id.strip()
                if not course_id:
                    continue
                for piece in re.split(r"[,|]+", tokens_part):
                    token_raw = piece.strip()
                    if not token_raw:
                        continue
                    weekday_token, period_token = _parse_rule_token(token_raw)
                    if period_token is None:
                        self._insert_rule_row(course_id, "自定义", "自定义", token_raw, dedupe=True)
                        invalid_tokens.append(f"{course_id}:{token_raw}")
                        continue
                    weekday_label = (
                        WEEKDAY_TOKEN_TO_LABEL.get(weekday_token, "每天")
                        if weekday_token
                        else "每天"
                    )
                    period_label = PERIOD_TOKEN_TO_LABEL.get(period_token, "上午")
                    normalized_token = (
                        f"{weekday_token}-{period_token}" if weekday_token else period_token
                    )
                    self._insert_rule_row(
                        course_id,
                        weekday_label,
                        period_label,
                        normalized_token,
                        dedupe=True,
                    )
        self._sync_skip_text_from_tree()

        if invalid_tokens and not silent:
            preview = ", ".join(invalid_tokens[:3])
            messagebox.showwarning(
                "提示",
                "部分规则无法标准化，已按自定义保留："
                + preview
                + (" ..." if len(invalid_tokens) > 3 else ""),
            )

    def _apply_skip_text(self) -> None:
        self._load_rules_from_skip_text(self.vars["skip_time_periods"].get(), silent=False)

    def _show_log_window(self) -> None:
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.deiconify()
            self.log_window.lift()
            self.log_window.focus_force()
            return

        self.log_window = tk.Toplevel(self.root)
        self.log_window.title("运行日志")
        self.log_window.geometry("1180x760")
        self.log_window.protocol("WM_DELETE_WINDOW", self._close_log_window)

        toolbar = ttk.Frame(self.log_window, padding=(8, 8, 8, 0))
        toolbar.pack(fill=X)
        ttk.Button(toolbar, text="清空日志", command=self._clear_log).pack(side=LEFT, padx=3)
        ttk.Button(toolbar, text="关闭窗口", command=self._close_log_window).pack(side=LEFT, padx=3)

        body = ttk.Frame(self.log_window, padding=8)
        body.pack(fill=BOTH, expand=True)
        # Monospace font so AI-training-style timestamped lines align cleanly.
        self.log_text_widget = tk.Text(body, wrap="none", font=("Consolas", 10))
        self.log_text_widget.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.log_text_widget.yview)
        scrollbar.pack(side=RIGHT, fill=Y)
        self.log_text_widget.configure(yscrollcommand=scrollbar.set)

        self._full_repaint()

    def _close_log_window(self) -> None:
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.destroy()
        self.log_window = None
        self.log_text_widget = None

    def _collect_config(self) -> dict:
        self._sync_skip_text_from_tree()
        return {
            key: var.get() if not isinstance(var, tk.BooleanVar) else bool(var.get())
            for key, var in self.vars.items()
        }

    def _save_config(self) -> None:
        data = self._collect_config()
        try:
            config_path = self._get_config_path()
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._save_app_state()
            self._append_log(f"[GUI] 配置已保存: {config_path}")
            self.status_var.set("配置已保存")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", f"{type(exc).__name__}: {exc}")

    def _load_config(self, silent: bool = False) -> None:
        config_path = self._get_config_path()
        if not config_path.exists():
            if not silent:
                messagebox.showinfo("提示", f"未找到配置文件：{config_path}")
            return
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            for key, var in self.vars.items():
                if key not in data:
                    continue
                value = data[key]
                if isinstance(var, tk.BooleanVar):
                    var.set(bool(value))
                else:
                    var.set("" if value is None else str(value))
            self._update_rule_course_options()
            self._load_rules_from_skip_text(self.vars["skip_time_periods"].get(), silent=True)
            self._save_app_state()
            self._append_log(f"[GUI] 已加载配置: {config_path}")
            self.status_var.set("配置已加载")
        except Exception as exc:  # noqa: BLE001
            if not silent:
                messagebox.showerror("加载失败", f"{type(exc).__name__}: {exc}")

    def _resolve_downloader_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [str(Path(sys.executable).resolve()), "--run-downloader"]

        script = self.project_root / "tools" / "icourse_video_downloader" / "downloader.py"
        if script.exists():
            return [sys.executable, str(script)]

        dist_exe = self.project_root / "dist" / "icourse_downloader.exe"
        if dist_exe.exists():
            return [str(dist_exe)]

        raise FileNotFoundError(
            "Could not find downloader entrypoint. Expected either "
            f"'{script}' or '{dist_exe}'."
        )

    def _build_env(self) -> dict[str, str]:
        cfg = self._collect_config()
        env = dict(os.environ)
        for key, env_key in (
            ("stu_id", "StuId"),
            ("uis_psw", "UISPsw"),
            ("course_ids", "COURSE_IDS"),
            ("skip_time_periods", "COURSE_SKIP_TIME_PERIODS"),
            ("out_dir", "DOWNLOAD_DIR"),
            ("summary_dir", "SUMMARY_DIR"),
            ("llm_name_1", "LLM_NAME_1"),
            ("llm_api_key_1", "LLM_API_KEY_1"),
            ("llm_base_url_1", "LLM_BASE_URL_1"),
            ("llm_models_1", "LLM_MODELS_1"),
        ):
            env[env_key] = str(cfg.get(key, "")).strip()

        for key, env_key in (
            ("whisper_model", "WHISPER_MODEL"),
            ("whisper_device", "WHISPER_DEVICE"),
            ("whisper_compute_type", "WHISPER_COMPUTE_TYPE"),
            ("whisper_language", "WHISPER_LANGUAGE"),
        ):
            value = str(cfg.get(key, "")).strip()
            if value:
                env[env_key] = value

        env_file = self.project_root / ".env"
        env.update(_read_env_file_values(env_file, ASR_ENV_KEYS))
        env["DASHSCOPE_API_KEY"] = ""
        env["GEMINI_API_KEY"] = ""
        # Force the child's stdout to UTF-8 even before its own reconfigure
        # runs, so the GUI pipe (also UTF-8) never sees GBK bytes.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # Tell the downloader/transcriber to emit [PROG:key] refreshable lines
        # (tqdm-style) which we render in place; CLI runs without this var.
        env["ICOURSE_GUI"] = "1"
        return env

    def _build_args(self) -> list[str]:
        cfg = self._collect_config()
        workers_raw = str(cfg.get("workers", "")).strip()
        try:
            workers = max(1, int(workers_raw)) if workers_raw else 1
        except ValueError:
            workers = 1
        args = [
            "--mode",
            str(cfg["mode"]),
            "--course-ids",
            str(cfg["course_ids"]).strip(),
            "--out-dir",
            str(cfg["out_dir"]).strip(),
            "--summary-dir",
            str(cfg["summary_dir"]).strip(),
            "--sleep",
            str(_safe_float(str(cfg["sleep"]), 0.2)),
            "--workers",
            str(workers),
        ]
        skip_time_periods = str(cfg.get("skip_time_periods", "")).strip()
        if skip_time_periods:
            args.extend(["--skip-time-periods", skip_time_periods])
        env_file = self.project_root / ".env"
        if env_file.exists():
            args.extend(["--env-file", str(env_file)])
        sub_ids = str(cfg["sub_ids"]).strip()
        if sub_ids:
            args.extend(["--sub-ids", sub_ids])
        if bool(cfg["overwrite"]):
            args.append("--overwrite")
        if bool(cfg["list_only"]):
            args.append("--list-only")
        return args

    def _test_llm(self) -> None:
        """Sanity-check the configured LLM with a 1-sentence test prompt."""
        cfg = self._collect_config()
        api_key = str(cfg.get("llm_api_key_1", "")).strip()
        base_url = str(cfg.get("llm_base_url_1", "")).strip()
        models = str(cfg.get("llm_models_1", "")).strip()
        if not api_key or not base_url or not models:
            messagebox.showwarning(
                "测试 LLM",
                "请先填写 LLM_API_KEY_1 / LLM_BASE_URL_1 / LLM_MODELS_1。",
            )
            return

        self.btn_test_llm.config(state="disabled", text="测试中…")

        def _worker() -> None:
            import os
            import sys
            import time
            import traceback

            # Make `import src.xxx` work from frozen or source layouts.
            project_root = str(self.project_root)
            if project_root not in sys.path:
                sys.path.insert(0, project_root)

            # Push current GUI fields into env so config.LLM_PROVIDERS rebuilds.
            env_overrides = {
                "LLM_NAME_1": str(cfg.get("llm_name_1", "")).strip() or "provider1",
                "LLM_API_KEY_1": api_key,
                "LLM_BASE_URL_1": base_url,
                "LLM_MODELS_1": models,
            }
            saved = {k: os.environ.get(k) for k in env_overrides}
            for k, v in env_overrides.items():
                os.environ[k] = v

            try:
                # Force-reload modules so they pick up the new env vars.
                for mod_name in ("src.summarizer", "src.config"):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                from src.summarizer import Summarizer  # type: ignore

                summarizer = Summarizer()
                test_text = (
                    "今天讲梯度下降。学习率太大会发散，太小会收敛慢。"
                    "Adam 是结合动量和自适应学习率的代表方法。"
                )
                t0 = time.time()
                summary, model_used = summarizer.summarize(
                    "测试课程", test_text,
                )
                elapsed = time.time() - t0
                preview = summary[:300] + ("…" if len(summary) > 300 else "")
                msg = (
                    f"✅ 调用成功\n\n"
                    f"模型：{model_used}\n"
                    f"耗时：{elapsed:.1f} 秒\n"
                    f"输出字数：{len(summary)}\n\n"
                    f"--- 输出预览 ---\n{preview}"
                )
                self.root.after(0, lambda: messagebox.showinfo("测试 LLM", msg))
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc(limit=3)
                msg = (
                    f"❌ 调用失败\n\n"
                    f"{type(exc).__name__}: {exc}\n\n"
                    f"--- traceback ---\n{tb}"
                )
                self.root.after(0, lambda: messagebox.showerror("测试 LLM", msg))
            finally:
                # Restore env so subsequent runs use whatever was there before.
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                self.root.after(
                    0,
                    lambda: self.btn_test_llm.config(
                        state="normal", text="测试 LLM",
                    ),
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _start_run(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            messagebox.showwarning("提示", "任务正在运行，请先停止。")
            return

        cfg = self._collect_config()
        if not str(cfg["course_ids"]).strip():
            messagebox.showwarning("提示", "请填写 COURSE_IDS。")
            return
        if str(cfg["mode"]) in {"download", "download_and_summarize"}:
            if not str(cfg["stu_id"]).strip() or not str(cfg["uis_psw"]).strip():
                messagebox.showwarning("提示", "download 模式必须填写 StuId 和 UISPsw。")
                return
        if str(cfg["mode"]) in {"summarize", "download_and_summarize"}:
            if not str(cfg["llm_api_key_1"]).strip():
                messagebox.showwarning("提示", "summarize 模式请填写 LLM_API_KEY_1。")
                return
            if not str(cfg["llm_base_url_1"]).strip() or not str(cfg["llm_models_1"]).strip():
                messagebox.showwarning("提示", "summarize 模式请填写 LLM_BASE_URL_1 和 LLM_MODELS_1。")
                return

        self._show_log_window()
        self._save_config()
        self._append_log("")
        self._append_log("[GUI] 开始运行...")
        cmd = self._resolve_downloader_command() + self._build_args()
        env = self._build_env()
        self._append_log("[GUI] command: " + " ".join(cmd))

        self.stop_event.clear()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status_var.set("运行中...")

        def _run() -> None:
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.project_root),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    # Must match the child's stdout encoding. downloader.py
                    # forces UTF-8 so Chinese titles + '·' survive on any host
                    # locale (sharing the exe to a non-GBK Windows otherwise
                    # mangles them). Do NOT use locale.getpreferredencoding().
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    **_hidden_subprocess_kwargs(),
                )
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    if self.stop_event.is_set():
                        break
                    self.log_queue.put(line.rstrip("\n"))
                rc = self.proc.wait()
                self.log_queue.put(f"[GUI] 进程结束，返回码: {rc}")
            except Exception as exc:  # noqa: BLE001
                self.log_queue.put(f"[GUI] 运行失败: {type(exc).__name__}: {exc}")
            finally:
                self.proc = None
                self.log_queue.put("__GUI_DONE__")

        threading.Thread(target=_run, daemon=True).start()

    def _stop_run(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.stop_event.set()
        try:
            self.proc.terminate()
        except Exception:
            pass
        self._append_log("[GUI] 已请求停止进程。")
        self.status_var.set("停止中...")

    def _drain_log_queue(self) -> None:
        while True:
            try:
                item = self.log_queue.get_nowait()
            except Empty:
                break

            if item == "__GUI_DONE__":
                self.btn_run.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self.status_var.set("已结束")
                # Any still-active task lines stop refreshing; leave them as-is.
                continue
            self._handle_log_item(item)
        self.root.after(200, self._drain_log_queue)

    def _handle_log_item(self, item: str) -> None:
        """Route a subprocess output line: live PROG, finalize PFIN, or plain."""
        if item.startswith("[PROG:"):
            sep = item.find("] ")
            if sep != -1:
                self._set_active(item[6:sep], item[sep + 2:])
                return
        if item.startswith("[PFIN:"):
            sep = item.find("] ")
            if sep != -1:
                self._finalize_active(item[6:sep], item[sep + 2:])
                return
        self._append_log(item)

    # ---- rendering helpers (widget invariant: frozen lines then active tail) --

    def _widget_alive(self) -> bool:
        return (
            self.log_text_widget is not None
            and self.log_text_widget.winfo_exists()
        )

    def _maybe_autoscroll(self) -> None:
        """Follow the bottom only if the user is already near it."""
        if not self._widget_alive():
            return
        try:
            if self.log_text_widget.yview()[1] > 0.985:
                self.log_text_widget.see(END)
        except tk.TclError:
            pass

    def _append_log(self, text: str) -> None:
        """Append a frozen history line just above the active (live) tail."""
        boundary = len(self.log_lines) + 1  # 1-based line of first active line
        self.log_lines.append(text)
        if self._widget_alive():
            # Inserting at the boundary pushes the active tail down; when there
            # is no active tail this index equals 'end', i.e. a plain append.
            self.log_text_widget.insert(f"{boundary}.0", text + "\n")
            self._maybe_autoscroll()

    def _set_active(self, key: str, content: str) -> None:
        """Create or refresh a live task line, pinned in the bottom region."""
        if key in self.active_prog:
            self.active_prog[key] = content
            if self._widget_alive():
                ln = len(self.log_lines) + list(self.active_prog).index(key) + 1
                self.log_text_widget.delete(f"{ln}.0", f"{ln}.end")
                self.log_text_widget.insert(f"{ln}.0", content)
        else:
            self.active_prog[key] = content
            if self._widget_alive():
                self.log_text_widget.insert(END, content + "\n")
                self._maybe_autoscroll()

    def _finalize_active(self, key: str, content: str) -> None:
        """Move a task's live line out of the active tail into frozen history."""
        if key in self.active_prog:
            if self._widget_alive():
                ln = len(self.log_lines) + list(self.active_prog).index(key) + 1
                # Delete the whole line including its trailing newline.
                self.log_text_widget.delete(f"{ln}.0", f"{ln + 1}.0")
            del self.active_prog[key]
        self._append_log(content)

    def _full_repaint(self) -> None:
        """Rebuild the whole widget from the model (on window (re)open)."""
        if not self._widget_alive():
            return
        self.log_text_widget.delete("1.0", END)
        lines = self.log_lines + list(self.active_prog.values())
        if lines:
            self.log_text_widget.insert(END, "\n".join(lines) + "\n")
            self.log_text_widget.see(END)

    def _clear_log(self) -> None:
        self.log_lines.clear()
        self.active_prog.clear()
        if self._widget_alive():
            self.log_text_widget.delete("1.0", END)

    def _on_close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno("确认退出", "任务仍在运行，确定退出吗？"):
                return
            self._stop_run()
        self._close_log_window()
        self.root.destroy()


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-downloader":
        args = sys.argv[2:]
        old_argv = sys.argv[:]
        try:
            try:
                from tools.icourse_video_downloader.downloader import main as downloader_main
            except ModuleNotFoundError:
                from icourse_video_downloader.downloader import main as downloader_main
            sys.argv = [old_argv[0]] + args
            return int(downloader_main())
        finally:
            sys.argv = old_argv

    _enable_windows_dpi_awareness()
    if sys.platform == "darwin":
        sys.path.insert(0, str(_resolve_project_root()))
        from src.mac_gui import main as mac_main
        return mac_main()

    root = tk.Tk()
    root.withdraw()
    DownloaderGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
