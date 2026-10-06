import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

HARD_LIMIT = 50_000_000



@dataclass(frozen=True)
class Course:
    id: str
    name: str
    directory: Path
    folders: tuple
    dedup_directories: tuple = ()


def excluded_local_directory(name):
    """Exclude hidden/runtime internals, not teaching subjects or file kinds."""
    return name.startswith(".") or name.casefold() in {"venv", "node_modules", "__pycache__", "__macosx"}


@dataclass(frozen=True)
class Config:
    base_url: str
    root: Path
    state_dir: Path
    max_bytes: int
    auth_env: str
    timezone: str
    download_hosts: tuple
    extensions: tuple
    include_patterns: tuple
    exclude_patterns: tuple
    courses: tuple


def load_config(filename):
    filename = Path(filename).expanduser().resolve()
    data = json.loads(filename.read_text(encoding="utf-8"))
    return parse_config(data, filename)


def parse_config(data, filename):
    """Validate in-memory edits before the desktop commits a config file."""
    filename = Path(filename).expanduser().resolve()
    if (not isinstance(data, dict) or not isinstance(data.get("base_url"), str)
            or not isinstance(data.get("root"), str) or not isinstance(data.get("courses"), list)):
        raise ValueError("配置需要包含学校地址、保存根目录和课程列表")
    base = data["base_url"].rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or parsed.hostname != "elearning.fudan.edu.cn"
            or parsed.port not in (None, 443) or parsed.path or parsed.query
            or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("base_url 必须为 https://elearning.fudan.edu.cn")
    root = Path(data["root"]).expanduser()
    if not root.is_absolute():
        raise ValueError("root 必须为绝对路径")
    limit = data.get("max_bytes", HARD_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= HARD_LIMIT:
        raise ValueError("max_bytes 必须为 1–50,000,000 的整数字节数")
    state = Path(data.get("state_dir", ".state")).expanduser()
    if not state.is_absolute():
        state = filename.parent / state
    environment = data.get("auth_env", "FUDAN_ELEARNING_TOKEN")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", environment):
        raise ValueError("auth_env 必须为环境变量名，不能填写凭据")
    timezone = data.get("timezone", "Asia/Shanghai")
    ZoneInfo(timezone)
    hosts = tuple(data.get("download_hosts", ["canvas-production.s3.fudan.edu.cn"]))
    if any(not re.fullmatch(r"[a-z0-9.-]+\.fudan\.edu\.cn", h) for h in hosts):
        raise ValueError("下载主机必须为明确的复旦域名，不支持通配符")
    # Older configurations may carry type/name filters; this policy ignores
    # them deliberately. Every accessible file below the exclusive size limit
    # is eligible, regardless of extension, MIME type or subject.
    extensions = includes = excludes = ()
    courses = []
    seen = set()
    for item in data["courses"]:
        if not isinstance(item, dict) or not isinstance(item.get("directory"), str) or not isinstance(item.get("name"), str):
            raise ValueError("每门课程需要包含 ID、名称和保存子目录")
        cid = str(item["id"])
        relative = Path(item["directory"])
        if not cid.isdecimal() or cid in seen:
            raise ValueError("课程 ID 必须唯一且为数字")
        if relative.is_absolute() or ".." in relative.parts or str(relative) == ".":
            raise ValueError("课程目录必须为 root 下的相对路径")
        if any(excluded_local_directory(part) for part in relative.parts[1:]):
            raise ValueError("保存位置不能指向隐藏目录或运行环境目录")
        target = root / relative
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("课程目录不能越出 root")
        seen.add(cid)
        if item.get("dedup_directories"):
            raise ValueError("去重范围仅限保存课件目录及其允许的子目录，不能配置额外查重路径")
        courses.append(Course(cid, str(item["name"]), target, ()))
    if not courses:
        raise ValueError("至少需要一门课程")
    for index, course in enumerate(courses):
        for other in courses[index + 1:]:
            for a in (course.directory, *course.dedup_directories):
                for b in (other.directory, *other.dedup_directories):
                    if a.resolve().is_relative_to(b.resolve()) or b.resolve().is_relative_to(a.resolve()):
                        raise ValueError("不同课程的保存/去重范围不能重叠")
    return Config(base, root, state, limit, environment, timezone, hosts, extensions,
                  includes, excludes, tuple(courses))
