#!/usr/bin/env python3
"""Interactive launcher for icourse_downloader.exe."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    text = f"{label}{suffix}: "
    raw = getpass.getpass(text) if secret else input(text)
    raw = raw.strip()
    return raw if raw else default


def _prompt_yes_no(label: str, default_yes: bool = True) -> bool:
    hint = "Y/n" if default_yes else "y/N"
    while True:
        raw = input(f"{label} [{hint}]: ").strip().lower()
        if not raw:
            return default_yes
        if raw in {"y", "yes", "1"}:
            return True
        if raw in {"n", "no", "0"}:
            return False
        print("请输入 y 或 n。")


def _prompt_required(label: str, default: str = "", secret: bool = False) -> str:
    while True:
        value = _prompt(label, default, secret=secret)
        if value:
            return value
        print("该项不能为空。")


def _pick_mode(default_mode: str) -> str:
    mode_map = {
        "1": "download",
        "2": "summarize",
        "3": "download_and_summarize",
    }
    reverse = {v: k for k, v in mode_map.items()}
    default_choice = reverse.get(default_mode, "2")
    print("运行模式：1) download  2) summarize  3) download_and_summarize")
    while True:
        choice = input(f"请选择模式 [{default_choice}]: ").strip()
        if not choice:
            return mode_map[default_choice]
        if choice in mode_map:
            return mode_map[choice]
        if choice in mode_map.values():
            return choice
        print("输入无效，请输入 1/2/3 或模式名称。")


def _resolve_project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parent.parent


def _resolve_downloader_command(project_root: Path) -> list[str]:
    exe_candidate = project_root / "dist" / "icourse_downloader.exe"
    if exe_candidate.exists():
        return [str(exe_candidate)]
    script_candidate = project_root / "tools" / "icourse_video_downloader" / "downloader.py"
    return [sys.executable, str(script_candidate)]


def _build_runtime_env(base_env: dict[str, str], values: dict[str, str]) -> dict[str, str]:
    env = dict(base_env)
    for key, val in values.items():
        if val is not None:
            env[key] = val
    return env


def _write_env(path: Path, values: dict[str, str]) -> None:
    lines = [
        f"StuId={values.get('StuId', '')}",
        f"UISPsw={values.get('UISPsw', '')}",
        f"COURSE_IDS={values.get('COURSE_IDS', '')}",
        f"DOWNLOAD_DIR={values.get('DOWNLOAD_DIR', '')}",
        f"SUMMARY_DIR={values.get('SUMMARY_DIR', '')}",
        f"ASR_BACKEND={values.get('ASR_BACKEND', 'auto')}",
        f"WHISPER_MODEL={values.get('WHISPER_MODEL', 'large-v3-turbo')}",
        f"LLM_NAME_1={values.get('LLM_NAME_1', '')}",
        f"LLM_API_KEY_1={values.get('LLM_API_KEY_1', '')}",
        f"LLM_BASE_URL_1={values.get('LLM_BASE_URL_1', '')}",
        f"LLM_MODELS_1={values.get('LLM_MODELS_1', '')}",
        "DASHSCOPE_API_KEY=",
        "GEMINI_API_KEY=",
    ]
    lines += [f"WHISPER_DEVICE={values.get('WHISPER_DEVICE', 'auto')}", "WHISPER_COMPUTE_TYPE=auto"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def main() -> int:
    print("=== iCourse 交互启动器 ===")
    print("全部配置手动输入，不读取已有 .env。")
    print("将根据输入自动调用 downloader（下载/总结）。")
    project_root = _resolve_project_root()

    mode = _pick_mode("summarize")
    course_ids = _prompt_required("课程编号 COURSE_IDS（逗号分隔）")
    sub_ids = _prompt("课次编号 sub_ids（可空，逗号分隔）", "")
    out_dir = _prompt_required("视频目录 out-dir", str(project_root / "tools" / "course"))
    summary_dir = _prompt_required("笔记目录 summary-dir", str(project_root / "tools" / "summary"))
    overwrite = _prompt_yes_no("是否覆盖已有笔记/视频", default_yes=False)
    list_only = _prompt_yes_no("是否仅列出不执行", default_yes=False)
    sleep_value = _prompt("每节课间隔秒数 --sleep", "0.2")

    runtime_values: dict[str, str] = {
        "COURSE_IDS": course_ids,
        "DOWNLOAD_DIR": out_dir,
        "SUMMARY_DIR": summary_dir,
    }

    if mode in {"download", "download_and_summarize"}:
        runtime_values["StuId"] = _prompt_required("学号 StuId")
        runtime_values["UISPsw"] = _prompt_required(
            "UIS 密码 UISPsw",
            secret=True,
        )

    if mode in {"summarize", "download_and_summarize"}:
        runtime_values["LLM_NAME_1"] = _prompt_required("LLM_NAME_1", "provider1")
        runtime_values["LLM_API_KEY_1"] = _prompt_required("LLM_API_KEY_1", secret=True)
        runtime_values["LLM_BASE_URL_1"] = _prompt_required("LLM_BASE_URL_1")
        runtime_values["LLM_MODELS_1"] = _prompt_required("LLM_MODELS_1")
        runtime_values["WHISPER_MODEL"] = _prompt_required(
            "WHISPER_MODEL", "large-v3-turbo"
        )
        runtime_values["WHISPER_DEVICE"] = _prompt_required(
            "WHISPER_DEVICE", "auto"
        )

    keep_generated = _prompt_yes_no("是否保留配置文件 .env.interactive.generated", default_yes=True)
    env_file = project_root / (
        ".env.interactive.generated"
        if keep_generated
        else ".env.interactive.runtime"
    )
    _write_env(env_file, runtime_values)
    print(f"配置已写入: {env_file}")

    cmd = _resolve_downloader_command(project_root)
    cmd.extend(
        [
            "--mode",
            mode,
            "--course-ids",
            course_ids,
            "--out-dir",
            out_dir,
            "--summary-dir",
            summary_dir,
            "--sleep",
            sleep_value,
            "--env-file",
            str(env_file),
        ]
    )
    if sub_ids:
        cmd.extend(["--sub-ids", sub_ids])
    if overwrite:
        cmd.append("--overwrite")
    if list_only:
        cmd.append("--list-only")

    print("\n即将执行命令：")
    print(" ".join(cmd))
    print()

    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        env=_build_runtime_env(os.environ, runtime_values),
        check=False,
    )
    if not keep_generated and env_file.exists():
        env_file.unlink()
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
