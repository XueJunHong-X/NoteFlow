"""OneDrive 路径探测与可写性检查（不调用任何云 API）。

同步由 OneDrive 客户端完成，NoteFlow 只负责把笔记写进同步目录。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ONEDRIVE_SUBDIR = "NoteFlow"
ENV_KEYS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "OneDriveBusiness", "OneDrivePersonal")


def _registry_candidates() -> list[Path]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    results: list[Path] = []
    base = r"Software\Microsoft\OneDrive\Accounts"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as accounts:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(accounts, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(accounts, name) as account:
                        value, _ = winreg.QueryValueEx(account, "UserFolder")
                        if value:
                            results.append(Path(str(value)))
                except OSError:
                    continue
    except OSError:
        return results
    return results


def detect_onedrive_paths() -> list[Path]:
    """按「环境变量 → 注册表 → 默认目录」顺序收集候选 OneDrive 根目录。"""
    candidates: list[Path] = []
    for key in ENV_KEYS:
        value = os.getenv(key)
        if value and value.strip():
            candidates.append(Path(value.strip()))
    candidates.extend(_registry_candidates())

    home = Path.home() / "OneDrive"
    if home.is_dir():
        candidates.append(home)

    seen: set[str] = set()
    existing: list[Path] = []
    for path in candidates:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        marker = str(resolved).lower()
        if marker in seen or not resolved.is_dir():
            continue
        seen.add(marker)
        existing.append(resolved)
    return existing


def detect_onedrive() -> Path | None:
    paths = detect_onedrive_paths()
    return paths[0] if paths else None


def prepare_onedrive_vault() -> Path:
    """返回 <OneDrive>/NoteFlow，并确保目录存在且可写。

    探测不到或不可写时抛 RuntimeError，消息面向直接展示给用户。
    """
    root = detect_onedrive()
    if root is None:
        raise RuntimeError(
            "未探测到 OneDrive 目录。请先确认 OneDrive 客户端已登录并同步，"
            "或用 --vault-dir 手动指定同步目录（例如 D:\\OneDrive\\NoteFlow）。"
        )

    target = root / ONEDRIVE_SUBDIR
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / f".noteflow_write_test_{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"OneDrive 目录不可写: {target}\n"
            f"请检查目录权限或磁盘空间，或用 --vault-dir 指定其他同步目录。\n原始错误: {exc}"
        ) from exc
    return target


def ensure_writable(path: Path) -> Path:
    """校验任意 vault 路径可创建、可写；失败时抛带中文说明的 RuntimeError。"""
    path = Path(path).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".noteflow_write_test_{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"vault 目录不可写: {path}\n"
            f"请检查路径是否存在、是否有写权限，或换一个目录后重试。\n原始错误: {exc}"
        ) from exc
    return path
