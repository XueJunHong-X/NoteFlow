"""由笔记标题层级生成本地 Mermaid mindmap（不调用识别接口）。

- 根节点 = 笔记标题，子节点 = sections / subsections 的 heading
- Obsidian 原生渲染 ```mermaid 代码块
- 清洗 Mermaid 语法敏感字符，避免思维导图渲染失败

可独立运行：
    python -m src.mindmap_gen output/vault/数学/高等数学/xxx.md
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_LABEL_LENGTH = 48
_WHITESPACE = re.compile(r"\s+")
# Mermaid mindmap 用 ()[]{} 表示节点形状，正文里的这些字符要替换成全角
_SHAPE_CHARS = str.maketrans(
    {
        "(": "（",
        ")": "）",
        "[": "［",
        "]": "］",
        "{": "｛",
        "}": "｝",
        '"': "",
        "'": "",
        "`": "",
        "\\": "",
        "$": "",
        "#": "",
        ";": "；",
    }
)


def _clean_label(text: str) -> str:
    """压缩空白、去掉公式/形状语法字符，并限制节点长度。"""
    cleaned = _WHITESPACE.sub(" ", str(text or "")).strip()
    cleaned = cleaned.translate(_SHAPE_CHARS)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" .:：-")
    if len(cleaned) > MAX_LABEL_LENGTH:
        cleaned = cleaned[:MAX_LABEL_LENGTH].rstrip() + "…"
    return cleaned


def _collect_nodes(sections: list[dict[str, Any]], depth: int, lines: list[str]) -> None:
    for section in sections:
        if not isinstance(section, dict):
            continue

        heading = _clean_label(section.get("heading") or "")
        child_depth = depth
        if heading:
            indent = "  " * depth
            lines.append(f"{indent}{heading}")
            child_depth = depth + 1

        _collect_nodes(section.get("subsections") or [], child_depth, lines)


def build_mermaid(note: dict[str, Any], related: list[str] | None = None) -> str:
    """生成可直接放进 Obsidian 的 mermaid 代码块。

    related 传入已建立双链的笔记标题时，额外挂一个「关联笔记」分支，
    让跨笔记关联在导图里也能一眼看到。
    """
    title = _clean_label(note.get("title") or "") or "未命名笔记"
    lines = ["```mermaid", "mindmap", f"  root(({title}))"]
    _collect_nodes(note.get("sections") or [], 2, lines)
    labels = [_clean_label(item) for item in (related or []) if _clean_label(item)]
    if labels:
        lines.append("    关联笔记")
        lines.extend(f"      {label}" for label in dict.fromkeys(labels))
    lines.append("```")
    return "\n".join(lines)


def build_mindmap_file(
    note: dict[str, Any],
    source_note: str,
    meta: dict[str, Any] | None = None,
    related: list[str] | None = None,
) -> str:
    """独立思维导图文件内容（frontmatter + mermaid）。"""
    from datetime import datetime

    from src.markdown_gen import _yaml_str

    meta = meta or {}
    title = str(note.get("title") or "未命名笔记")
    lines = [
        "---",
        f"title: {_yaml_str(f'{title} 思维导图')}",
        "type: mindmap",
        f"source: {_yaml_str(source_note)}",
        f"created: {meta.get('created') or datetime.now().isoformat(timespec='seconds')}",
        "---",
        "",
        f"# {title} · 思维导图",
        "",
        build_mermaid(note, related=related),
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="NoteFlow Mermaid 思维导图生成（可独立运行）")
    parser.add_argument("note", type=Path, help="已生成的 Markdown 笔记")
    args = parser.parse_args()

    from src.organizer import parse_markdown_note  # 延迟导入，避免循环依赖

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    note, _meta = parse_markdown_note(args.note)
    print(build_mermaid(note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
