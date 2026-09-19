"""Lightweight network subprocess so Stop can cancel an in-flight upload."""

import json
import sys

from .cloud import CloudAPI, SpeechAPIError
from .types import ASRSettings
from .dashscope import DashScopeAPI


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    for line in sys.stdin:
        api = None
        try:
            request = json.loads(line)
            if request.get("op") == "close":
                return
            settings = ASRSettings(**request["settings"])
            api = (DashScopeAPI if settings.provider == "dashscope" else CloudAPI)(settings)
            options = {"task_path": request.get("task_path")} if settings.provider == "dashscope" else {}
            result = (api.check() if request["op"] == "check" else
                      api.transcribe(request["path"], request["duration"], progress=emit, **options))
            emit(dict(event="result", **result))
        except (SpeechAPIError, ValueError) as exc:
            # SpeechAPIError and settings validation contain controlled messages only.
            emit(dict(event="error", message=str(exc) if isinstance(exc, SpeechAPIError)
                      else "语音设置或服务返回的数据无效。"))
        except Exception:
            emit(dict(event="error", message="云端转录进程失败；请检查音频文件和服务配置。"))
        finally:
            if api:
                api.close()


if __name__ == "__main__":
    main()
