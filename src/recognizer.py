"""手写笔记识别：调用 OpenAI 兼容的多模态模型，输出结构化 JSON。

识别结果结构与项目约定一致：title / sections / arrows / tags。
模型偶尔会包一层 Markdown 代码块或带上解释文字，这里做了提取与容错；
JSON 始终解析失败时会退化成「原文 + 待校对」，保证流水线不中断。

可独立运行：
    python -m src.recognizer tests/sample_images/sample_note.png
    python -m src.recognizer tests/sample_images/sample_note.png --mock
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import logging
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import requests

from config import Settings
from src.markdown_gen import is_weak_heading

logger = logging.getLogger(__name__)

# 连续失败熔断：接口整体不可用时，逐页固定退避（2s+4s）会把队列拖死。
# 连续 3 页耗尽重试后进入 60s 冷却，期间后续页面仍会调用，但不再空等退避。
COOLDOWN_AFTER_FAILURES = 3
COOLDOWN_SECONDS = 60.0
_breaker_lock = threading.Lock()
_failure_streak = 0
_cooldown_until = 0.0


def _breaker_success() -> None:
    global _failure_streak
    with _breaker_lock:
        _failure_streak = 0


def _breaker_failure() -> None:
    global _failure_streak, _cooldown_until
    with _breaker_lock:
        _failure_streak += 1
        if _failure_streak >= COOLDOWN_AFTER_FAILURES:
            _cooldown_until = time.time() + COOLDOWN_SECONDS


def _breaker_in_cooldown() -> bool:
    return time.time() < _cooldown_until


# 限流自适应：429 后并发减半（4→2→1），稳定 RAMP_STABLE_SECONDS 后再逐级升回。
# 升回必须等满稳定窗口，避免「升上去立刻再被限流」的抖动。
RAMP_STABLE_SECONDS = 60.0
MAX_CONCURRENCY = 4


class _ConcurrencyGovernor:
    """识别并发闸门：限制同时在飞的请求数，并按限流反馈动态调整。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._max = 1
        self._limit = 1
        self._min_limit = 1
        self._active = 0
        self._ramp_at = 0.0
        self._throttle_count = 0
        self._downgrades = 0
        self._last_reason = ""
        # 降级后各档位停留时长：任务结束时用来回答「最长停在并发 X」
        self._dwell: dict[int, float] = {}
        self._dwell_since: float | None = None
        # 档位变化只留最近若干条：界面据此写日志/做提示，不需要无限增长
        self._changes: deque[dict[str, Any]] = deque(maxlen=20)

    def _append_change(self, before: int, after: int, reason: str) -> None:
        self._changes.append(
            {"at": time.monotonic(), "from": before, "to": after, "reason": reason}
        )

    def _settle_dwell(self, now: float) -> None:
        """把当前档位的停留时长结掉（调用方需持有锁）。"""
        if self._dwell_since is not None:
            self._dwell[self._limit] = self._dwell.get(self._limit, 0.0) + (now - self._dwell_since)
            self._dwell_since = None

    def reset(self, maximum: int) -> None:
        """新任务开始：档位恢复到配置值，并清零本任务的降级统计。"""
        maximum = max(1, min(int(maximum), MAX_CONCURRENCY))
        with self._cond:
            self._max = maximum
            self._limit = maximum
            self._min_limit = maximum
            self._throttle_count = 0
            self._downgrades = 0
            self._dwell.clear()
            self._dwell_since = None
            self._last_reason = ""
            self._changes.clear()
            self._ramp_at = time.monotonic() + RAMP_STABLE_SECONDS
            self._cond.notify_all()

    def configure(self, maximum: int) -> None:
        """设置并发上限；上限变化时按新上限重置（同批次内保留已降级的状态）。"""
        maximum = max(1, min(int(maximum), MAX_CONCURRENCY))
        with self._cond:
            if maximum == self._max:
                return
            before = self._limit
            self._max = maximum
            self._limit = maximum
            self._min_limit = maximum
            self._downgrades = 0
            self._dwell.clear()
            self._dwell_since = None
            self._ramp_at = time.monotonic() + RAMP_STABLE_SECONDS
            if before != maximum:
                self._append_change(before, maximum, "批次开始，按配置设定并发")
            self._cond.notify_all()

    @contextlib.contextmanager
    def slot(self):
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait(timeout=0.2)
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def on_throttle(self, reason: str = "HTTP 429") -> None:
        now = time.monotonic()
        with self._cond:
            before = self._limit
            self._settle_dwell(now)
            self._limit = max(1, self._limit // 2)
            self._min_limit = min(self._min_limit, self._limit)
            self._ramp_at = now + RAMP_STABLE_SECONDS
            self._throttle_count += 1
            self._last_reason = reason
            after = self._limit
            if before != after:
                self._downgrades += 1
                self._append_change(before, after, f"接口限流（{reason}）")
            # 从第一次降级开始计时，统计各档位的实际停留时长
            self._dwell_since = now
            self._cond.notify_all()
        if before != after:
            logger.info("识别并发 %d → %d（接口限流）", before, after)

    def on_success(self) -> None:
        ramped = 0
        before = 0
        now = time.monotonic()
        with self._cond:
            if self._limit < self._max and now >= self._ramp_at:
                before = self._limit
                self._settle_dwell(now)
                self._limit += 1
                self._ramp_at = now + RAMP_STABLE_SECONDS
                ramped = self._limit
                self._append_change(before, ramped, "接口稳定后回升")
                self._dwell_since = None if ramped >= self._max else now
        if ramped:
            logger.info("接口已稳定 %d 秒，识别并发升回 %d", int(RAMP_STABLE_SECONDS), ramped)

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._cond:
            last = self._changes[-1] if self._changes else None
            dwell = dict(self._dwell)
            if self._dwell_since is not None:
                dwell[self._limit] = dwell.get(self._limit, 0.0) + (now - self._dwell_since)
            longest = max(dwell, key=dwell.get) if dwell else self._limit
            return {
                "limit": self._limit,
                "max": self._max,
                "min_limit": self._min_limit,
                "active": self._active,
                "throttles": self._throttle_count,
                "downgrades": self._downgrades,
                "longest_limit": longest,
                "longest_dwell_s": round(dwell.get(longest, 0.0), 1),
                "last_reason": self._last_reason,
                "ramp_in_s": max(0.0, round(self._ramp_at - now, 1)),
                "last_change": (
                    {
                        "from": last["from"],
                        "to": last["to"],
                        "reason": last["reason"],
                        "age_s": round(now - last["at"], 1),
                    }
                    if last
                    else None
                ),
                "changes": [
                    {
                        "t": round(change["at"] - self._changes[0]["at"], 2),
                        "from": change["from"],
                        "to": change["to"],
                        "reason": change["reason"],
                    }
                    for change in list(self._changes)
                ],
            }


_GOVERNOR = _ConcurrencyGovernor()


def configure_concurrency(maximum: int) -> None:
    """批处理开始前调用：设置本批次的并发上限。"""
    _GOVERNOR.configure(maximum)


def reset_concurrency(maximum: int) -> None:
    """新任务开始时调用：恢复配置档位并清零本次限流统计。"""
    _GOVERNOR.reset(maximum)


def concurrency_status() -> dict[str, Any]:
    """当前并发闸门状态，供日志、测试与配额探测使用。"""
    return _GOVERNOR.status()

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}

MAX_SECTION_DEPTH = 3

PROMPT = """你是一名手写笔记识别专家。请仔细阅读这张学生手写笔记图片，完成两件事：
1. 逐字识别其中的文字，保留原始语言，不要翻译、不要总结、不要补写图片里没有的内容。
2. 还原版面结构：标题层级、重点标记、箭头关系。

只输出一个 JSON 对象，不要输出 Markdown 代码块、解释或任何前后缀文字。格式必须严格如下：
{
  "title": "整篇笔记的主标题，图片里没有就概括核心主题，不超过 20 字",
  "sections": [
    {
      "heading": "一级标题",
      "level": 1,
      "content": "该标题下的正文，保留换行",
      "highlights": ["被荧光笔标记、画星号或加框的重点内容"],
      "subsections": [
        {"heading": "二级标题", "level": 2, "content": "正文", "highlights": [], "subsections": []}
      ]
    }
  ],
  "arrows": [{"from": "概念A", "to": "概念B", "relation": "推导/解释/举例/对比"}],
  "tags": ["学科或主题标签"]
}

要求：
- level 从 1 开始，最多 3 层，子标题放在 subsections 中。
- 只有当页面上确实存在标题层级（大标题、编号小标题）时才设 heading；以「解」「例」「注」等开头的续写行是正文，不是标题。
- content 的第一行不能重复 heading 的文字，标题只在标题位置出现一次。
- 每个 section 的 content 必须包含该章节下识别到的全部文字（包括被标记的重点行）；highlights 只是从这些文字里摘出的重点短语，不能替代 content。
- 数学公式统一用 LaTeX：行内公式写成 $...$，独立成行的公式写成 $$...$$；不要用 e^x、a^n 这类纯文本上标写法。
- 没有对应内容时用空字符串或空数组，禁止编造。
- 数字、符号、英文按原样输出（例如 0/0 不要写成 O/O），不要自行修正原文笔误。
- arrows 只在图中箭头或连线明确连接两个文字概念时输出；箭头指向整段内容、只是装饰或无法确定两端时，arrows 留空。
- arrows 端点必须是页面上真实出现的名词性概念；变量（n、x）、数字、括号片段、公式片段都不能作为端点，没有合格端点时 arrows 留空。
- 所有字符串使用双引号，不能出现尾随逗号或注释。
"""

TEXT_PROMPT = """你是一名笔记整理专家。下面是从 PDF 文字层按版面顺序抽取出来的文字，
请把它整理成结构化的笔记 JSON。要求：

1. 只使用文字里实际存在的内容，不要补写、不要总结成提纲、不要翻译。
2. 依据编号、短行、字号无法判断时，优先用「一、二、三」「1. 2. 3.」这类编号推断标题层级。
3. 以「解」「例」「注」「证」开头的单字/短标记是正文标记，不是标题；不要输出成「# 解」。
4. content 的第一行不能重复 heading；标题只在标题位置出现一次。
5. 数学公式统一用 LaTeX：行内 $...$，独立成行 $$...$$；原文里的纯文本上标（如 e^x）请转成 LaTeX。
6. 没有对应内容时用空字符串或空数组，禁止编造。
7. arrows 只在文字里明确出现「A 推出 B」「A 决定 B」这类关系时才输出，否则留空。

只输出一个 JSON 对象，不要输出 Markdown 代码块、解释或任何前后缀文字。格式必须严格如下：
{
  "title": "整篇笔记的主标题，文字里没有就概括核心主题，不超过 20 字",
  "sections": [
    {
      "heading": "一级标题",
      "level": 1,
      "content": "该标题下的正文，保留换行",
      "highlights": ["重点内容"],
      "subsections": [
        {"heading": "二级标题", "level": 2, "content": "正文", "highlights": [], "subsections": []}
      ]
    }
  ],
  "arrows": [{"from": "概念A", "to": "概念B", "relation": "推导/解释/举例/对比"}],
  "tags": ["学科或主题标签"]
}

要求：level 从 1 开始，最多 3 层；所有字符串使用双引号，不能出现尾随逗号或注释。
"""

MAX_TEXT_INPUT = 20000


class RecognitionError(RuntimeError):
    """识别失败且不可恢复时抛出。"""


def _encode_image(image_path: Path) -> str:
    mime = MIME_TYPES.get(image_path.suffix.lower(), "image/jpeg")
    data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{data}"


class _RetryableError(RuntimeError):
    """限流、5xx 等可重试错误。"""


class _ThrottleError(_RetryableError):
    """429：除重试外还要下调并发，避免继续撞限流。"""

    def __init__(self, wait: float = 0.0) -> None:
        super().__init__("服务限流（HTTP 429）")
        self.wait = wait


def _retry_after_seconds(response: "requests.Response") -> float:
    """优先按服务端 Retry-After 等待，读不到就交给指数退避。"""
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after") or ""
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0.0


def _log_rate_limit_headers(response: "requests.Response") -> None:
    """记录服务端配额响应头，便于排查限流与估算 RPM/TPM。"""
    headers = {
        key.lower(): value
        for key, value in response.headers.items()
        if "ratelimit" in key.lower() or "rate-limit" in key.lower()
    }
    if headers:
        logger.debug("限流响应头: %s", headers)


def _describe_request_error(exc: Exception) -> str:
    """把底层网络异常翻译成用户能看懂的中文提示。"""
    if isinstance(exc, requests.Timeout):
        return "请求超时：网络太慢或模型服务无响应"
    if isinstance(exc, requests.ConnectionError):
        return "网络连接失败：可能已断网，或接口地址不可达"
    return str(exc)


def _request_completion(settings: Settings, messages: list[dict[str, Any]], temperature: float) -> str:
    """调用聊天补全接口，返回纯文本内容。"""
    payload: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 3000,
    }
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.api_base}/chat/completions"

    last_error: Exception | None = None
    for attempt in range(1, settings.max_retries + 2):
        try:
            # 闸门只包住真正的网络请求：等待并发名额的时间不计入超时
            with _GOVERNOR.slot():
                response = requests.post(url, headers=headers, json=payload, timeout=settings.request_timeout)

            if response.status_code == 429:
                _log_rate_limit_headers(response)
                _GOVERNOR.on_throttle()
                raise _ThrottleError(_retry_after_seconds(response))
            if response.status_code >= 500:
                raise _RetryableError(f"模型服务端错误（HTTP {response.status_code}）")
            if response.status_code in (401, 403):
                raise RecognitionError(
                    "API Key 无效或没有该模型的权限"
                    f"（HTTP {response.status_code}），请检查密钥后重试。"
                    f"原始返回：{response.text[:200]}"
                )
            if response.status_code >= 400:
                # 其它 4xx 多为参数或模型名问题，重试无意义
                raise RecognitionError(
                    f"模型接口返回 HTTP {response.status_code}：{response.text[:500]}"
                )

            text = _extract_message_text(response.json())
            _breaker_success()
            _GOVERNOR.on_success()
            return text
        except _ThrottleError as exc:
            # 429 交给并发闸门处理：不计入熔断，只做退避 + 降并发
            last_error = exc
            if attempt <= settings.max_retries:
                wait = min(exc.wait or float(2 ** attempt), 30.0)
                logger.debug(
                    "识别请求被限流（第 %d 次），%.1fs 后重试；当前并发 %d",
                    attempt,
                    wait,
                    _GOVERNOR.status()["limit"],
                )
                time.sleep(wait)
        except (_RetryableError, requests.RequestException) as exc:
            last_error = exc
            _breaker_failure()
            if attempt <= settings.max_retries:
                wait = 0 if _breaker_in_cooldown() else 2 ** attempt
                if wait:
                    logger.warning("识别请求失败（第 %d 次），%ds 后重试: %s", attempt, wait, _describe_request_error(exc))
                    time.sleep(wait)
                else:
                    logger.warning("识别请求失败（第 %d 次），接口冷却中，跳过退避直接重试", attempt)

    detail = _describe_request_error(last_error) if last_error else "未知错误"
    raise RecognitionError(
        f"模型接口调用失败：{detail}（已重试 {settings.max_retries} 次）"
    )


def _extract_message_text(body: dict[str, Any]) -> str:
    choices = body.get("choices") or []
    if not choices:
        raise RecognitionError(f"模型返回缺少 choices: {str(body)[:300]}")

    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        # 部分服务商把内容拆成 [{type: text, text: ...}]
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        content = "".join(parts)

    if not isinstance(content, str) or not content.strip():
        raise RecognitionError("模型返回内容为空")
    return content


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出中提取 JSON 对象，容忍代码块包裹与前后说明文字。"""
    cleaned = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("输出中找不到 JSON 对象")
    candidate = cleaned[start : end + 1]

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # 常见问题：尾随逗号。做一次温和修正再试
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        return json.loads(repaired)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _as_text_list(value: Any) -> list[str]:
    # 兼容模型把数组写成 "标签1, 标签2" 的情况
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，、;；]", value) if item.strip()]
    if not isinstance(value, list):
        return []
    return [text for text in (_as_text(item) for item in value) if text]


def _normalize_sections(raw_sections: Any, depth: int = 1) -> list[dict[str, Any]]:
    if not isinstance(raw_sections, list) or depth > MAX_SECTION_DEPTH:
        return []

    sections: list[dict[str, Any]] = []
    for item in raw_sections:
        if not isinstance(item, dict):
            continue

        heading = _as_text(item.get("heading"))
        content = _as_text(item.get("content"))
        subsections = _normalize_sections(item.get("subsections"), depth + 1)
        if is_weak_heading(heading):
            # 「解 / 证 / 注 / 例」是答题标记，降级为正文首行
            content = f"{heading}\n{content}".strip()
            heading = ""
        if not heading and not content and not subsections:
            continue

        try:
            level = int(item.get("level", depth))
        except (TypeError, ValueError):
            level = depth
        level = min(max(level, 1), MAX_SECTION_DEPTH)

        sections.append(
            {
                "heading": heading,
                "level": level,
                "content": content,
                "highlights": _as_text_list(item.get("highlights")),
                "subsections": subsections,
            }
        )
    return sections


def _normalize_arrows(raw_arrows: Any) -> list[dict[str, str]]:
    if not isinstance(raw_arrows, list):
        return []

    arrows: list[dict[str, str]] = []
    for item in raw_arrows:
        if not isinstance(item, dict):
            continue
        source = _as_text(item.get("from"))
        target = _as_text(item.get("to"))
        if not source or not target:
            continue
        arrows.append({"from": source, "to": target, "relation": _as_text(item.get("relation"))})
    return arrows


def normalize_note(data: Any, fallback_title: str) -> dict[str, Any]:
    """把模型输出规整成约定结构，缺失或类型异常的字段做兜底。"""
    if not isinstance(data, dict):
        data = {}

    title = _as_text(data.get("title")) or fallback_title
    sections = _normalize_sections(data.get("sections"))
    arrows = _normalize_arrows(data.get("arrows"))
    tags = _as_text_list(data.get("tags"))

    if not sections:
        # 没有可用结构时至少保留一个占位章节，避免生成空笔记
        sections = [
            {
                "heading": "待整理内容",
                "level": 1,
                "content": _as_text(data.get("content")) or "模型未返回可用的结构化内容，请人工校对。",
                "highlights": [],
                "subsections": [],
            }
        ]

    return {"title": title, "sections": sections, "arrows": arrows, "tags": tags}


def build_fallback_note(image_path: Path, raw_text: str) -> dict[str, Any]:
    """JSON 解析彻底失败时的兜底结构，保留原始输出供人工校对。"""
    logger.warning("识别结果解析失败，使用兜底结构: %s", image_path.name)
    content = raw_text.strip() or "（模型未返回内容）"
    return {
        "title": image_path.stem,
        "sections": [
            {"heading": "原始识别输出（待校对）", "level": 1, "content": content, "highlights": [], "subsections": []}
        ],
        "arrows": [],
        "tags": ["待校对"],
    }


def build_mock_note(image_path: Path) -> dict[str, Any]:
    """离线自检用的固定结构，不调用外部接口，用于验证整条链路。

    样例图有内置的真实识别结果（src/offline_samples.py）时优先用内置结果，
    这样没配密钥的演示也能看到「有内容」的产物，而不是占位文字。
    """
    from src import offline_samples

    preset = offline_samples.for_stem(image_path.stem)
    if preset:
        logger.info("使用内置示例识别结果: %s", image_path.name)
        return preset

    logger.info("使用 mock 识别结果: %s", image_path.name)
    return {
        "title": f"{image_path.stem}（离线示例）",
        "sections": [
            {
                "heading": "示例章节",
                "level": 1,
                "content": "这是 mock 模式生成的内容，用于验证「预处理 → 识别 → Markdown」链路。",
                "highlights": ["mock 模式不调用模型"],
                "subsections": [
                    {
                        "heading": "子标题",
                        "level": 2,
                        "content": "把 NOTEFLOW_API_KEY 配好即可得到真实识别结果。",
                        "highlights": [],
                        "subsections": [],
                    }
                ],
            }
        ],
        "arrows": [{"from": "手写笔记", "to": "Markdown", "relation": "转换"}],
        "tags": ["NoteFlow", "离线示例"],
    }


def _recognize_messages(
    messages: list[dict[str, Any]],
    settings: Settings,
    fallback_title: str,
    label: str,
) -> dict[str, Any]:
    """调用模型并解析 JSON；JSON 不合法时追加一次纠错请求。"""
    last_raw = ""
    for attempt in range(2):
        try:
            raw = _request_completion(settings, messages, temperature=0.1 if attempt == 0 else 0.0)
            last_raw = raw
            data = extract_json(raw)
            note = normalize_note(data, fallback_title)
            logger.info("识别完成: %s（%d 个章节，%d 条关联）", label, len(note["sections"]), len(note["arrows"]))
            return note
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("第 %d 次识别结果不是合法 JSON: %s", attempt + 1, exc)
            messages = messages + [
                {"role": "assistant", "content": last_raw},
                {"role": "user", "content": "上面的输出不是合法 JSON。请只输出一个符合约定格式的 JSON 对象，不要任何解释或代码块。"},
            ]

    return build_fallback_note(Path(f"{fallback_title}.txt"), last_raw)


def recognize_image(image_path: Path, settings: Settings, mock: bool = False) -> dict[str, Any]:
    """识别单张图片，返回规整后的笔记结构。"""
    image_path = Path(image_path)

    if mock:
        return build_mock_note(image_path)

    settings.require_api()

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": _encode_image(image_path)}},
            ],
        }
    ]
    return _recognize_messages(messages, settings, image_path.stem, image_path.name)


def recognize_text(
    text: str,
    settings: Settings,
    mock: bool = False,
    fallback_title: str = "PDF 文本页",
) -> dict[str, Any]:
    """整理 PDF 文字层（不经过视觉识别）。"""
    text = (text or "").strip()
    if not text:
        raise RecognitionError("PDF 页面没有可用文字")

    if mock:
        return build_mock_note(Path(f"{fallback_title}.txt"))

    settings.require_api()

    if len(text) > MAX_TEXT_INPUT:
        logger.info("文字层较长（%d 字），截断到 %d 字后送模型", len(text), MAX_TEXT_INPUT)
        text = text[:MAX_TEXT_INPUT] + "\n…（后续内容已截断）"

    messages = [{"role": "user", "content": f"{TEXT_PROMPT}\n\n--- PDF 文字开始 ---\n{text}\n--- PDF 文字结束 ---"}]
    return _recognize_messages(messages, settings, fallback_title, f"{fallback_title}（文字层）")


def main() -> int:
    parser = argparse.ArgumentParser(description="NoteFlow 手写识别（可独立运行）")
    parser.add_argument("image", type=Path, help="待识别的图片")
    parser.add_argument("--mock", action="store_true", help="不调用模型，输出固定示例结构")
    args = parser.parse_args()

    from config import setup_logging

    settings = Settings.from_env()
    setup_logging(settings.log_level)

    if not args.image.exists():
        logger.error("图片不存在: %s", args.image)
        return 1

    try:
        note = recognize_image(args.image, settings, mock=args.mock)
    except RecognitionError as exc:
        logger.error("%s", exc)
        return 1

    print(json.dumps(note, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
