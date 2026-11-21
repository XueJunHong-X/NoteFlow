"""处理清单：记录一次来源文件处理是否完整、产出了哪些 vault 笔记。

清理本地缓存不能只看 vault 里“有没有同名 md”，必须知道：
- 这份来源文件是否真的处理完了；
- 是真实识别还是 mock / dry-run；
- 每一页对应的笔记路径。

清单只写 output 目录，不写 vault，避免污染 Obsidian 同步。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = 1
APP_VERSION = "0.1.2"


def _norm_path(path: Path) -> str:
    """Windows 大小写不敏感；统一用绝对路径参与指纹。"""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path).absolute()
    return os.path.normcase(str(resolved))


def source_id_for(path: Path) -> str:
    """来源指纹：路径 + 大小 + mtime_ns。

    只用于本地匹配，不写进 vault，避免笔记外发时泄露本机绝对路径。
    """
    stat = Path(path).stat()
    raw = f"{_norm_path(Path(path))}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def manifest_dir(output_dir: Path) -> Path:
    return Path(output_dir) / "manifests"


def manifest_path(output_dir: Path, source_id: str) -> Path:
    return manifest_dir(output_dir) / f"{source_id}.json"


def write_manifest(
    output_dir: Path,
    source: Path,
    *,
    source_id: str,
    source_type: str,
    recognition_mode: str,
    pages_total: int,
    pages_ok: int,
    pages_failed: int,
    status: str,
    started_at: str,
    vault_dir: str,
    notes: list[str],
) -> Path | None:
    """原子写清单；失败只记日志，不影响主处理流程。"""
    try:
        stat = Path(source).stat()
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "source_id": source_id,
            "source_file": Path(source).name,
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_type": source_type,
            "recognition_mode": recognition_mode,
            "pages_total": int(pages_total),
            "pages_ok": int(pages_ok),
            "pages_failed": int(pages_failed),
            "status": status,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "vault_dir": str(vault_dir),
            "notes": [str(Path(note)) for note in notes],
            "app_version": APP_VERSION,
        }
        target = manifest_path(Path(output_dir), source_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
        return target
    except OSError as exc:
        logger.warning("写入处理清单失败：%s", exc)
        return None


def read_manifest(output_dir: Path, source_id: str) -> dict[str, Any] | None:
    path = manifest_path(Path(output_dir), source_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != SCHEMA or data.get("source_id") != source_id:
        return None
    return data
