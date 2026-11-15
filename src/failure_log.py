"""失败留痕：统一往 failures.jsonl 追加一行，识别 / 归档 / 链接各阶段共用。

任何一环失败都只记录、不抛出，保证流水线连续（单点失败跳过并留痕）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from config import Settings

logger = logging.getLogger(__name__)


def record(settings: Settings, source: Path | str, page: int | None, stage: str, exc: Exception) -> None:
    """追加一条失败记录；写日志本身失败也只告警。"""
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "source": str(source),
        "page": page,
        "stage": stage,
        "error": f"{type(exc).__name__}: {exc}",
    }
    try:
        settings.failure_log.parent.mkdir(parents=True, exist_ok=True)
        with settings.failure_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as log_exc:
        logger.warning("写入失败记录失败: %s", log_exc)
