"""LLM-based course lecture summarization via OpenAI-compatible APIs."""

import hashlib
import json
import os
import time
from pathlib import Path

import requests
from openai import OpenAI

from . import config
from .artifacts import atomic_write_json
from .summary_errors import EmptySummaryResponseError, TruncatedSummaryError
from .summary_result import SummaryResult
from .summary_settings import (
    DEFAULT_OUTPUT_TOKENS,
    DEFAULT_TIMEOUT_MINUTES,
    WORKFLOW_VERSION,
)
from .summary_storage import archive_copy

SYSTEM_PROMPT = r"""你是一个专业的课程助教。你的任务是根据用户提供的课程录音文本，生成用于学生自学和期末复习的详细笔记。
1. **直接输出**：不要包含任何"好的"、"没问题"、"以下是总结"等客套话，不要输出全局课程名称大标题（由系统自动生成），直接开始总结即可。
2. **文本清洗**：语言必须通顺、逻辑清晰，严格去除口语化表达、重复句和无意义的录音识别错误等。内容可能被识别成同音字，通过学术语境修复。
3. **格式严格**：
   - 必须使用 Markdown 格式排版。
   - 标题级别限制：只允许使用三级及后续级别的标题（即只能使用 `###`、`####`或`#####`），禁止使用 `#` 和 `##`。禁止出现`##### ###`这种错误的重复标题符号。
   - 合理使用加粗、列表、表格来组织信息，确保结构清晰。
   - 不得使用超过两级的缩进。可以适当使用bullet point列表但不得过多。不要把一段话拆成很多个用bullet point组成的短句子列表，而要尽可能用完整的段落来组织老师的讲解。
   - 出于节省空间考虑，不要使用连续的回车换行，不要出现空行。
4. **公式规范**：所有数学公式或科学变量必须使用规范的 LaTeX 语法（行内公式用 $...$，行间公式用 $$...$$）。由于图床限制，latex公式中不要出现中文。
5. **事无巨细、忠于原文**：总结必须尽可能详尽全面，力求覆盖录音中的**每一个**知识点、推导过程、举例/案例/类比、解释说明，以及老师对概念的补充和延伸。不要遗漏任何实质性内容，包括但不限于：核心概念的定义与内涵、推导步骤与逻辑链条、具体案例与应用场景、老师穿插的经验分享与行业见解、对比分析与优缺点讨论、历史背景与发展脉络等。
   - 以整节课为整体组织笔记，将前后出现的相关概念、补充说明和例子贯通整理。每次输入包含完整原文；根据本次写作任务展开，详略由实际内容决定，不为凑字数扩写。
   - 用完整连贯的段落忠实呈现老师的讲解内容和论述逻辑，保留老师强调的重点和反复提及的要点。
   - 禁止捏造录音中未提及的内容。禁止用"等"、"等等"、"诸如此类"来模糊化具体内容——如果老师列举了具体的例子，就必须把每个例子都写出来。
6. 你需要格外注意课程中是否提及了作业、考试、签到、组队等关键事项，如果有的话，用三级标题【课程事项提醒】标注在开头。
7. **文风示例**：以下是一个关于"梯度下降"的片段，展示了笔记总结过程中【错误的】和【正确的】的两种总结风格，请严格模仿后者。

【❌ 错误的风格】
### 梯度下降

**定义：**
- 梯度下降是一种优化算法
- 用于最小化损失函数
- 广泛应用于机器学习

**核心步骤：**
- 计算梯度
- 更新参数
- 重复迭代

**学习率：**
- 学习率决定步长
- 太大会发散
- 太小会收敛慢
- 需要调参

**类型：**
- 批量梯度下降（BGD）
- 随机梯度下降（SGD）
- 小批量梯度下降（Mini-batch GD）

---

【✅ 正确的风格】

### 梯度下降

梯度下降是最小化损失函数 $L(\theta)$ 的核心优化算法。其基本思想是沿着损失函数对参数 $\theta$ 的梯度的反方向迭代更新，每一步的更新公式为 $\theta \leftarrow \theta - \eta \nabla_\theta L(\theta)$，其中 $\eta$ 称为学习率，控制每次更新的步长大小。
学习率的选取至关重要：若 $\eta$ 过大，参数更新幅度过猛，损失函数可能在最优点附近震荡甚至发散；若 $\eta$ 过小，收敛速度极慢，训练成本大幅上升。实践中通常通过学习率调度（learning rate schedule）或自适应方法（如 Adam）来缓解这一问题。
根据每次更新时使用的样本量，梯度下降可分为三类：**批量梯度下降（BGD）** 每次使用全部训练数据，梯度估计准确但计算开销大；**随机梯度下降（SGD）** 每次仅用单个样本，更新频繁但噪声大；**小批量梯度下降（Mini-batch GD）** 则折中两者，是深度学习中最常用的形式。
---
核心区别在于：前一种的风格将一段完整的知识拆解成大量零碎的短句 bullet point，读起来像提纲而非能让人看懂的笔记，缺乏逻辑连贯性和上下文衔接；喜欢的风格用完整段落讲清楚一件事的来龙去脉，bullet point 仅在真正需要并列枚举时少量使用。"""

_MODEL_ALIASES = {
    "GLM4.7": "GLM-4.7",
}
_OPENAI_COMPAT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

def summary_fingerprint():
    providers = [(p.name, p.base_url, p.models, p.api_style) for p in config.LLM_PROVIDERS]
    value = {"prompt": SYSTEM_PROMPT, "providers": providers,
             "workflow": WORKFLOW_VERSION,
             "output_tokens": os.environ.get("LLM_MAX_OUTPUT_TOKENS", str(DEFAULT_OUTPUT_TOKENS))}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Summarizer:
    """Course lecture summarizer using configurable OpenAI-compatible APIs."""

    def __init__(self, providers=None, timeout_seconds=None):
        providers = config.LLM_PROVIDERS if providers is None else providers
        if not providers:
            raise ValueError(
                "No LLM provider configured. Set either legacy DASHSCOPE_API_KEY/"
                "GEMINI_API_KEY or indexed LLM_API_KEY_n + LLM_BASE_URL_n + "
                "LLM_MODELS_n."
            )

        self.providers = []
        for provider in providers:
            provider_info = {
                "name": provider.name,
                "api_style": provider.api_style,
                "api_key": provider.api_key,
                "base_url": provider.base_url.rstrip("/"),
                "models": list(provider.models),
            }
            if provider.api_style == "openai":
                provider_info["client"] = OpenAI(
                    api_key=provider.api_key,
                    base_url=provider.base_url,
                    default_headers=_OPENAI_COMPAT_HEADERS,
                    max_retries=0,
                )
            self.providers.append(provider_info)
        self.timeout_seconds = timeout_seconds or max(1, int(os.environ.get(
            "API_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MINUTES * 60000))) / 1000)
        self.max_output_tokens = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", str(DEFAULT_OUTPUT_TOKENS)))
        if self.max_output_tokens <= 0:
            raise ValueError("笔记输出上限必须大于零。")

    @staticmethod
    def _openai_messages(title: str, content: str, *, system_prompt="") -> list[dict[str, str]]:
        return [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + system_prompt},
            {
                "role": "user",
                "content": f"以下是课程《{title}》的录音文本，请根据完整原文一次生成整篇笔记：\n\n{content}",
            },
        ]

    def _call_openai_sdk(self, provider: dict, model: str,
                         title: str, content: str, **options) -> str:
        """Send a summarization request to an OpenAI-compatible model."""
        t0 = time.time()
        response = provider["client"].chat.completions.create(
            model=model,
            messages=self._openai_messages(title, content, **options),
            temperature=0.3,
            timeout=self.timeout_seconds,
            max_tokens=self.max_output_tokens,
        )
        if not response.choices:
            raise EmptySummaryResponseError("模型没有返回结果（空响应）。")
        if response.choices[0].finish_reason == "length":
            raise TruncatedSummaryError(self._truncation_message())
        if response.choices[0].finish_reason != "stop":
            raise RuntimeError(f"模型未正常完成输出：{response.choices[0].finish_reason}")
        result = (getattr(response.choices[0].message, "content", None) or "").strip()
        if not result:
            raise EmptySummaryResponseError("模型未返回正文（空响应）。")
        elapsed = time.time() - t0
        print(
            f"[Summarizer] Done ({model}/sdk): {len(content)} chars input"
            f" → {len(result)} chars output in {elapsed:.0f}s"
        )
        return result

    def _call_openai_http(self, provider: dict, model: str,
                          title: str, content: str, **options) -> str:
        """Send a raw HTTP OpenAI-compatible request.

        Selected explicitly with LLM_TRANSPORT=http for compatible gateways.
        """
        t0 = time.time()
        url = provider["base_url"] + "/chat/completions"
        payload = {
            "model": model,
            "messages": self._openai_messages(title, content, **options),
            "temperature": 0.3,
            "max_tokens": self.max_output_tokens,
        }
        headers = {
            "authorization": f"Bearer {provider['api_key']}",
            "content-type": "application/json",
            **_OPENAI_COMPAT_HEADERS,
        }
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise EmptySummaryResponseError("模型没有返回结果（空响应）。")
        if choices[0].get("finish_reason") == "length":
            raise TruncatedSummaryError(self._truncation_message())
        if choices[0].get("finish_reason") != "stop":
            raise RuntimeError(f"模型未正常完成输出：{choices[0].get('finish_reason')}")
        message = choices[0].get("message") or {}
        result = (message.get("content") or choices[0].get("text") or "").strip()
        if not result:
            raise EmptySummaryResponseError("模型未返回正文（空响应）。")

        elapsed = time.time() - t0
        print(
            f"[Summarizer] Done ({model}/http): {len(content)} chars input"
            f" → {len(result)} chars output in {elapsed:.0f}s"
        )
        return result

    def _call_openai_llm(self, provider: dict, model: str,
                         title: str, content: str, **options) -> str:
        if os.environ.get("LLM_TRANSPORT", "sdk") == "http":
            return self._call_openai_http(provider, model, title, content, **options)
        return self._call_openai_sdk(provider, model, title, content, **options)

    def _call_anthropic_llm(self, provider: dict, model: str,
                            title: str, content: str, **options) -> str:
        """Send a summarization request to an Anthropic-compatible model."""
        t0 = time.time()
        url = provider["base_url"] + "/v1/messages"
        resolved_model = _MODEL_ALIASES.get(model, model)
        payload = {
            "model": resolved_model,
            "max_tokens": self.max_output_tokens,
            "temperature": 0.3,
            "system": SYSTEM_PROMPT + "\n\n" + options.get("system_prompt", ""),
            "messages": [
                {
                    "role": "user",
                    "content": self._openai_messages(title, content)[1]["content"],
                }
            ],
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": provider["api_key"],
            "authorization": f"Bearer {provider['api_key']}",
            "anthropic-version": "2023-06-01",
        }
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("stop_reason") == "max_tokens":
            raise TruncatedSummaryError(self._truncation_message())
        if data.get("stop_reason") not in ("end_turn", "stop_sequence"):
            raise RuntimeError(f"模型未正常完成输出：{data.get('stop_reason')}")
        parts = data.get("content") or []
        texts = [
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        result = "\n".join(t for t in texts if t).strip()
        if not result:
            raise EmptySummaryResponseError("模型未返回正文（空响应）。")

        elapsed = time.time() - t0
        print(
            f"[Summarizer] Done ({resolved_model}): {len(content)} chars input"
            f" → {len(result)} chars output in {elapsed:.0f}s"
        )
        return result

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        """Classify whether to retry the same model or fall through to next.

        Transient: empty answers, timeouts, connection drops, 5xx, rate limits.
        Permanent: 4xx auth errors, model-not-found.
        """
        if isinstance(exc, EmptySummaryResponseError):
            return True
        msg = f"{type(exc).__name__}: {exc}".lower()
        for needle in (
            "timeout", "timed out", "readtimeout", "connectiontimeout",
            "remotedisconnected", "connection aborted", "connection reset",
            "rate limit", "ratelimit", "429",
            "internal server", "502", "503", "504",
            "temporarily", "try again",
        ):
            if needle in msg:
                return True
        return False

    def _truncation_message(self):
        return (f"笔记输出达到上限（本次 {self.max_output_tokens} tokens），未保存为完成结果。"
                "请在设置中恢复笔记推荐参数或提高输出上限后重试。")

    # Bound transient retries; output recovery keeps the full transcript.
    _RETRY_WAITS = (5, 15)

    def _call_with_retry(
        self, provider: dict, model: str, title: str, content: str, **options,
    ) -> str:
        """Call an LLM with up to N retries on transient errors."""
        max_attempts = len(self._RETRY_WAITS) + 1
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            from . import task_events as events
            events.progress("等待笔记服务返回", phase="request", model=model, provider=provider["name"],
                            attempt=attempt, max_attempts=max_attempts, input_chars=len(content))
            try:
                if provider["api_style"] == "anthropic":
                    return self._call_anthropic_llm(provider, model, title, content, **options)
                return self._call_openai_llm(provider, model, title, content, **options)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < max_attempts and self._is_transient_error(exc):
                    wait = self._RETRY_WAITS[attempt - 1]
                    reason = "模型返回空响应" if isinstance(exc, EmptySummaryResponseError) else type(exc).__name__
                    print(
                        f"[Summarizer] {provider['name']}/{model} 第 {attempt}/{max_attempts} 次请求未成功"
                        f"（{reason}），{wait} 秒后重试当前步骤。",
                        flush=True,
                    )
                    events.progress("请求未成功，等待重试", phase="retry", model=model, provider=provider["name"],
                                    attempt=attempt, max_attempts=max_attempts, retry_seconds=wait, reason=reason)
                    time.sleep(wait)
                    continue
                raise
        # Unreachable, but mypy-friendly.
        assert last_exc is not None
        raise last_exc

    def _checkpoint_identity(self, title, content):
        settings = dict(prompt=SYSTEM_PROMPT, workflow=WORKFLOW_VERSION,
                        output_tokens=self.max_output_tokens,
                        providers=[{k: p[k] for k in ("name", "base_url", "models", "api_style")}
                                   for p in self.providers])
        return hashlib.sha256(json.dumps([title, content, settings], sort_keys=True,
                                         ensure_ascii=False).encode()).hexdigest()

    def summarize(self, title: str, content: str, *, checkpoint_path=None,
                  progress=None, overwrite=False) -> SummaryResult:
        """Send the full transcript once; cache success until the file is committed."""
        if not content.strip():
            raise ValueError("整课转录为空，无法生成笔记。")
        if len(content) >= 8000 and self.max_output_tokens < 4096:
            raise ValueError(
                f"当前笔记输出上限仅 {self.max_output_tokens} tokens，无法容纳长课笔记；"
                f"建议恢复为 {DEFAULT_OUTPUT_TOKENS}。本次未调用笔记 API。")
        path = Path(checkpoint_path) if checkpoint_path else None
        identity = self._checkpoint_identity(title, content)

        def report(message):
            from . import task_events as events
            events.progress(message, phase="preparing", input_chars=len(content))
            print(f"[Summarizer] {message}", flush=True)
            if progress:
                progress(message)

        saved = None
        if path and path.is_file():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if not overwrite and isinstance(saved, dict):
            value = saved.get("result")
            if (saved.get("schema") == 3 and saved.get("workflow") == WORKFLOW_VERSION
                    and saved.get("identity") == identity and isinstance(value, dict)
                    and all(isinstance(value.get(k), str) and value[k].strip() for k in ("text", "model"))
                    and saved.get("sha256") == self._result_digest(value)):
                report("已恢复生成完成的笔记，正在保存")
                return SummaryResult(value["text"], value["model"])

        def save(result=None):
            if path:
                value = dict(text=result.text, model=result.model) if result else None
                atomic_write_json(path, dict(schema=3, workflow=WORKFLOW_VERSION, identity=identity,
                    result=value, sha256=self._result_digest(value)), private=True)

        if path and path.exists() and (overwrite or not isinstance(saved, dict)
                or saved.get("schema") != 3 or saved.get("identity") != identity or saved.get("result") is not None):
            archive_copy(path, path.parent / "history")
        save()  # Verify disk access before any paid request.
        report(f"正在生成整篇笔记 · 完整原文 {len(content)} 字符 · 输出上限 {self.max_output_tokens} tokens")
        text, model = self._generate_summary(title, content)
        result = SummaryResult(text, model)
        save(result)
        return result

    @staticmethod
    def _result_digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _generate_summary(self, title: str, content: str, **options) -> tuple[str, str]:
        """Summarize lecture content with configured provider/model fallback.

        Returns:
            (summary, model_used) tuple.

        Raises:
            RuntimeError: If all models fail (after retries each).
        """
        if not content or not content.strip():
            return ("（内容为空）", "")

        errors = []
        truncated = False

        for provider in self.providers:
            provider_name = provider["name"]
            for model in provider["models"]:
                try:
                    result = self._call_with_retry(provider, model, title, content, **options)
                    return (result, f"{provider_name}/{model}")
                except Exception as e:  # noqa: BLE001
                    truncated = truncated or isinstance(e, TruncatedSummaryError)
                    model_id = f"{provider_name}/{model}"
                    print(f"[Summarizer] {model_id} failed: {type(e).__name__}: {e}")
                    errors.append(f"{model_id}: {e}")

        error_type = TruncatedSummaryError if truncated else RuntimeError
        raise error_type(
            "All LLM models failed:\n" + "\n".join(errors)
        )
