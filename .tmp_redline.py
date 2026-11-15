"""NoteFlow 上传前后红线核查：密钥形态、禁止文件、真实笔记残留。

- 阻断项（exit 1）：API Key 形态、.env/config.json 入库、tests/sample_images/real 入库
- 提示项（仅打印）：绝对路径、日期目录名等非敏感信息
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent

KEY_PATTERNS = [
    r"sk-[A-Za-z0-9]{16,}",
    r"ghu_[A-Za-z0-9]{16,}",
    r"ghp_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"api[_-]?key\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}",
    r"secret\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}",
]
# 阻断：真实个人路径与密钥文件
BLOCK_PATH_PATTERNS = [
    r"C:\\Users\\Junhong",
    r"E:\\NoteFlow",
    r"\.trae-cn",
    r"attachments\\",
]
# 提示：机器相关但非敏感的字符串
INFO_PATH_PATTERNS = [
    r"Trae Work",
    r"2026-11-1[0-9]",
]
FORBIDDEN_NAMES = {".env", "config.json", ".env.local"}
FORBIDDEN_DIRS = {"__pycache__", "real", "output", "vault", ".venv"}
IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".exe", ".zip", ".pdf", ".7z", ".onnx"}


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [p for p in out.stdout.split("\0") if p]


def scan(files: list[str]) -> int:
    blocking = 0
    for rel in files:
        path = REPO / rel
        parts = set(Path(rel).parts)
        if path.name in FORBIDDEN_NAMES and rel != ".env.example":
            print(f"[阻断] 禁止文件入库：{rel}")
            blocking += 1
        hit_dirs = parts & FORBIDDEN_DIRS
        if hit_dirs:
            print(f"[阻断] 禁止目录入库：{rel} 命中 {sorted(hit_dirs)}")
            blocking += 1
        if path.suffix.lower() in IMAGE_SUFFIX:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in KEY_PATTERNS:
            for m in re.finditer(pat, text):
                line = text[: m.start()].count("\n") + 1
                print(f"[阻断] 密钥形态：{rel}:{line} {m.group()[:20]}...")
                blocking += 1
        for pat in BLOCK_PATH_PATTERNS:
            for m in re.finditer(pat, text):
                line = text[: m.start()].count("\n") + 1
                print(f"[阻断] 个人路径：{rel}:{line} {m.group()}")
                blocking += 1
        for pat in INFO_PATH_PATTERNS:
            for m in re.finditer(pat, text):
                line = text[: m.start()].count("\n") + 1
                print(f"[提示] 机器相关字符串：{rel}:{line} {m.group()}")
    print(f"\n已扫描 {len(files)} 个已跟踪文件，阻断项 {blocking} 处")
    return blocking


if __name__ == "__main__":
    raise SystemExit(1 if scan(tracked()) else 0)
