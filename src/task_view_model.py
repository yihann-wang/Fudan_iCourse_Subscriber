"""Pure task state reducer, independent of Qt and human-readable log formatting."""

import math
import time
from dataclasses import dataclass, field

STAGES = ("dl", "tr", "sm")
STAGE_NAMES = dict(dl="下载", tr="云端转录", sm="笔记")
TERMINAL = {"done", "cached", "na", "pending", "failed", "cancelled", "interrupted"}
LABELS = dict(queued="排队中", waiting="等待前序阶段", running="处理中", done="已完成",
              cached="使用已有", na="不适用", pending="等待回放", failed="失败",
              cancelled="已停止", interrupted="已中断")


def duration(seconds):
    seconds = max(0, int(seconds or 0))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def size(value):
    value = max(0, float(value or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024


@dataclass
class Stage:
    status: str = "waiting"
    message: str = ""
    metrics: dict = field(default_factory=dict)
    started: float | None = None
    changed: float = 0


@dataclass
class Lecture:
    course_id: str
    sub_id: str
    course_title: str
    title: str
    stages: dict = field(default_factory=lambda: {s: Stage() for s in STAGES})
    paths: dict = field(default_factory=dict)

    @property
    def outcome(self):
        values = [s.status for s in self.stages.values()]
        for status in ("failed", "pending", "interrupted", "cancelled"):
            if status in values:
                return status
        if all(s in {"done", "cached", "na"} for s in values):
            return "success"
        return "running" if "running" in values else "queued"


class TaskViewModel:
    def __init__(self, run_id):
        self.run_id = run_id
        self.seq = 0
        self.tasks = {}
        self.courses = {}
        self.planned = False
        self.total = 0
        self.phase = "正在启动…"
        self.final = None
        self.exited = False
        self.stopping = False
        self.started = time.monotonic()
        self.ended = None
        self.heartbeat = None
        self.engine_pid = None
        self.records = []
        self.record_id = 0

    def record(self, event, message):
        self.record_id += 1
        self.records.append(dict(id=self.record_id, time=event.get("time", ""), message=message,
                                 course_id=event.get("course_id"), sub_id=event.get("sub_id")))
        del self.records[:-1000]

    def apply(self, event):
        if (event.get("event") != "icourse" or event.get("version") != 1
                or event.get("run_id") != self.run_id or self.exited):
            return False
        seq = event.get("seq")
        if type(seq) is not int or seq <= self.seq:
            return False
        self.seq = seq
        kind = event.get("kind")
        now = time.monotonic()
        if kind == "heartbeat":
            self.heartbeat = now
            self.engine_pid = event.get("pid")
            return True
        if kind == "run_started":
            self.record(event, "任务已启动")
        elif kind == "phase":
            self.phase = event.get("message", "正在准备")
            self.record(event, self.phase)
        elif kind == "course":
            self.courses[str(event["course_id"])] = event.get("course_title", event["course_id"])
            if event.get("empty"):
                self.record(event, f"{event['course_title']}：没有符合条件的课次")
        elif kind == "planned":
            self.total = max(len(self.tasks), int(event.get("total", 0)))
            self.planned = True
            self.phase = "正在处理课程" if self.total else "没有符合条件的课次"
            self.record(event, f"课次检查完成：共 {self.total} 课")
        elif kind == "run_finished":
            self.final = event.get("status", "interrupted")
            if event.get("message"):
                self.record(event, event["message"])
        elif kind in {"task", "stage", "progress"}:
            if not event.get("course_id") or not event.get("sub_id"):
                return False
            key = (str(event["course_id"]), str(event["sub_id"]))
            if kind == "task":
                if key in self.tasks:
                    return False
                task = Lecture(*key, str(event.get("course_title", key[0])), str(event.get("title", key[1])))
                for name, status in event.get("stages", {}).items():
                    if name in STAGES and status in LABELS:
                        task.stages[name].status = status
                task.paths.update(event.get("paths", {}))
                self.tasks[key] = task
                self.courses[key[0]] = task.course_title
                self.total = max(self.total, len(self.tasks))
                if task.outcome == "success":
                    self.record(event, f"{task.course_title} · {task.title}：复用已有结果")
                return True
            task = self.tasks.get(key)
            name = event.get("stage")
            if task is None or name not in STAGES:
                return False
            stage = task.stages[name]
            if task.outcome in {"failed", "pending", "cancelled", "interrupted", "success"} or stage.status in TERMINAL:
                return False
            if kind == "stage":
                status = event.get("status")
                if status not in LABELS:
                    return False
                stage.status = status
                if status == "running" and stage.started is None:
                    stage.started = now
                stage.message = event.get("message", "")
                stage.changed = now
                if event.get("path"):
                    task.paths[name] = event["path"]
                self.record(event, f"{task.course_title} · {task.title} · {STAGE_NAMES[name]}：{stage.message or LABELS[status]}")
                if status == "done":
                    index = STAGES.index(name)
                    for following in STAGES[index + 1:]:
                        if task.stages[following].status == "waiting":
                            task.stages[following].status = "queued"
                            break
            else:
                if stage.status != "running":
                    return False
                message = str(event.get("message", ""))
                metrics = event.get("metrics", {})
                if not isinstance(metrics, dict):
                    return False
                old_phase = stage.metrics.get("phase")
                phase = metrics.get("phase", old_phase)
                if phase != old_phase or metrics.get("attempt", stage.metrics.get("attempt")) != stage.metrics.get("attempt") or metrics.get("model", stage.metrics.get("model")) != stage.metrics.get("model"):
                    stage.metrics = {}
                    stage.changed = now
                    if message:
                        self.record(event, f"{task.course_title} · {task.title}：{message}")
                stage.metrics.update(metrics)
                if message:
                    stage.message = message
        return True

    def counts(self, course_id=None):
        counts = {s: 0 for s in ("success", "running", "queued", "failed", "pending", "cancelled", "interrupted")}
        for task in self.tasks.values():
            if course_id is None or task.course_id == course_id:
                counts[task.outcome] += 1
        return counts

    def finish(self, code, *, cancelled=False, crashed=False):
        if self.exited:
            return
        self.exited = True
        self.stopping = False
        self.ended = time.monotonic()
        unfinished = [t for t in self.tasks.values() if t.outcome in {"running", "queued"}]
        for task in unfinished:
            for stage in task.stages.values():
                if stage.status not in TERMINAL:
                    stage.status = "cancelled" if cancelled else "interrupted"
        if cancelled:
            self.final = "cancelled"
        elif crashed or unfinished or self.final is None:
            self.final = "interrupted"
        elif code or self.counts()["failed"]:
            self.final = "failed"

    @property
    def elapsed(self):
        return (self.ended or time.monotonic()) - self.started

    @property
    def heading(self):
        if self.stopping:
            return "正在停止任务…"
        if not self.exited:
            return "正在收尾…" if self.final else self.phase
        c = self.counts()
        if self.final == "cancelled":
            return "任务已停止，已完成的结果已保留"
        if self.final == "interrupted":
            return "任务意外中断，请查看诊断详情"
        if self.final == "failed":
            return "本次任务已结束，部分课次需要处理" if c["success"] else "任务未完成，请查看原因"
        if c["pending"]:
            return "本次检查已结束，部分课次尚无回放"
        return "所有课次均已完成" if self.total else "没有符合条件的课次" if self.planned else "操作完成"


def percent(stage):
    if stage.status != "running" or stage.metrics.get("unit") not in {"bytes", "seconds"}:
        return None
    completed, total = stage.metrics.get("completed"), stage.metrics.get("total")
    if not isinstance(completed, (int, float)) or not isinstance(total, (int, float)):
        return None
    if not math.isfinite(completed) or not math.isfinite(total) or total <= 0:
        return None
    return max(0, min(100, completed / total * 100))


def stage_text(stage):
    if stage.status != "running":
        return LABELS.get(stage.status, stage.status)
    pct = percent(stage)
    if pct is not None:
        return f"{stage.message or '处理中'} {pct:.0f}%"
    elapsed = duration(time.monotonic() - (stage.started or time.monotonic()))
    return f"{stage.message or '处理中'} · {elapsed}"
