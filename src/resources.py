"""运行时资源定位：开发态用仓库根目录，打包后用 PyInstaller 的解包目录。"""

from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """资源根目录：打包后指向解包目录，开发时指向仓库根目录。"""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return BASE_DIR


def sample_dir() -> Path:
    """内置合成样例图：打包后在 exe 同级的 sample_images，开发时在 tests/sample_images。"""
    if FROZEN:
        return resource_dir() / "sample_images"
    return BASE_DIR / "tests" / "sample_images"
