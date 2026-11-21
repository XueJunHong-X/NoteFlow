"""把识别出的 JSON 转成 Obsidian 风格的 Markdown。

规则：
- title 写入 frontmatter，同时作为文件名
- section.level = n → n 个 # 的标题
- highlights → 正文内首次出现处加粗；正文中找不到时追加为「重点」列表
- arrows → 文末「关联概念」，用 [[双向链接]] 连接
- tags → frontmatter 的 tags

可独立运行：
    python -m src.markdown_gen note.json -o output/notes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows 单个目录项上限 255 个 UTF-16 码元；留出「 索引.md」「-思维导图.md」等后缀余量。
# 普通长度（<=240）的名字原样保留，完整路径超阈值时才由 organizer 做带哈希的截断。
MAX_FILENAME_LENGTH = 240

# Windows 保留名（不区分大小写，带扩展名同样非法），截断/清洗后仍需再挡一层
RESERVED_FILENAME_STEMS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 「解 / 证 / 注 / 例」这类单字是答题标记，不是标题
WEAK_HEADING = re.compile(r"\s*[解证注例]\s*[:：。.、]?\s*")


def is_weak_heading(text: str) -> bool:
    """判断是否为需要降级为正文的单字标记标题。"""
    return bool(WEAK_HEADING.fullmatch(text or ""))


def _yaml_str(value: str) -> str:
    """用 JSON 字符串语法写 YAML，天然处理引号与特殊字符。"""
    return json.dumps(value, ensure_ascii=False)


def _render_frontmatter(note: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    """标准字段：title / subject / course / source / source_type / page / created / tags。"""
    lines = ["---", f"title: {_yaml_str(str(note.get('title', '未命名笔记')))}"]

    for key in ("subject", "course"):
        value = str(meta.get(key) or "").strip()
        if value:
            lines.append(f"{key}: {_yaml_str(value)}")

    if meta.get("source"):
        lines.append(f"source: {_yaml_str(str(meta['source']))}")
    if meta.get("source_type"):
        lines.append(f"source_type: {_yaml_str(str(meta['source_type']))}")
    # 来源指纹：只写本地可重算的摘要，不写绝对路径，避免笔记外发时泄露本机目录
    if meta.get("source_id"):
        lines.append(f"source_id: {_yaml_str(str(meta['source_id']))}")
    if meta.get("source_file"):
        lines.append(f"source_file: {_yaml_str(str(meta['source_file']))}")
    if meta.get("source_size"):
        lines.append(f"source_size: {int(meta['source_size'])}")
    if meta.get("page"):
        lines.append(f"page: {meta['page']}")
    if meta.get("page_note"):
        lines.append(f"page_note: {_yaml_str(str(meta['page_note']))}")

    lines.append(f"created: {meta.get('created') or datetime.now().isoformat(timespec='seconds')}")

    tags = [tag for tag in note.get("tags", []) if isinstance(tag, str) and tag.strip()]
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {_yaml_str(tag)}" for tag in tags[:12])

    lines.append("---")
    return lines


def _render_content(content: str, highlights: list[str]) -> list[str]:
    """输出正文，并把重点内容加粗；正文里找不到的重点单独列出。"""
    text = content.strip()
    unmatched: list[str] = []

    for highlight in highlights:
        keyword = highlight.strip()
        if not keyword:
            continue
        if keyword in text:
            # 只包一次，避免同一重点重复加粗
            text = text.replace(keyword, f"**{keyword}**", 1)
        else:
            unmatched.append(keyword)

    lines: list[str] = []
    if text:
        lines.extend(text.splitlines())

    if unmatched:
        lines.append("")
        lines.append("> **重点**：" + "；".join(f"**{item}**" for item in unmatched))
    return lines


def _render_sections(sections: list[dict[str, Any]], depth: int = 1) -> list[str]:
    lines: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue

        level = section.get("level", depth)
        if not isinstance(level, int) or level < 1:
            level = depth

        heading = str(section.get("heading") or "").strip()
        content = str(section.get("content") or "")
        highlights = [item for item in section.get("highlights", []) if isinstance(item, str)]

        if not heading and not content and not section.get("subsections"):
            continue

        if heading and is_weak_heading(heading):
            # 单字标记降级为正文，避免出现 `# 解`
            content = f"{heading}\n{content}".strip()
            heading = ""

        lines.append("")
        if heading:
            lines.append(f"{'#' * level} {heading}")
            lines.append("")

        lines.extend(_render_content(content, highlights))
        lines.extend(_render_sections(section.get("subsections") or [], level + 1))

    return lines


def is_meaningful_endpoint(value: str) -> bool:
    """过滤变量、数字、公式片段这类不适合做双链端点的文本。

    P0 起沿用：单字、纯数字、LaTeX 片段、罗马数字序号一律拒绝。
    """
    text = value.strip()
    if len(text) < 2:
        return False
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", text):
        return False
    if "$" in text or "\\" in text:
        # 公式片段不是概念，做双链没有意义
        return False
    if re.fullmatch(r"[\(\（]?[A-Za-z]\d*[\)\）]?", text):
        return False
    if re.fullmatch(r"[IVXLC]+|[Ⅰ-Ⅻ]", text):
        # 图上的 I / II / III 是标注序号，不是概念
        return False
    if len(text) <= 12 and re.search(r"[=+×·/^−]", text):
        return False
    if re.search(r"[A-Za-z]\s*-\s*\d", text):
        return False
    return True


# 兼容旧调用名
_is_meaningful_endpoint = is_meaningful_endpoint


def _render_endpoint(
    text: str,
    arrow_targets: dict[str, tuple[str, str]] | None,
    heading_anchors: dict[str, str] | None,
) -> str:
    """把箭头端点渲染成链接：有对应笔记才给双链，本篇小节用页内锚点，否则纯文本。

    P2 起不再无条件写 `[[概念]]`：指向不存在笔记的空链在 Obsidian 里只是灰点噪音。
    """
    from src.linker import normalize_key  # 延迟导入，避免 markdown_gen ↔ linker 循环依赖

    key = normalize_key(text)
    if arrow_targets and key in arrow_targets:
        stem, title = arrow_targets[key]
        return f"[[{stem}|{text}]]"
    if heading_anchors and key in heading_anchors:
        heading = heading_anchors[key]
        return f"[[#{heading}|{text}]]" if heading != text else f"[[#{heading}]]"
    return text


def _render_arrows(
    arrows: list[dict[str, str]],
    arrow_targets: dict[str, tuple[str, str]] | None = None,
    heading_anchors: dict[str, str] | None = None,
) -> list[str]:
    if not arrows:
        return []

    lines = ["", "## 关联概念", ""]
    for arrow in arrows:
        source = str(arrow.get("from") or "").strip()
        target = str(arrow.get("to") or "").strip()
        if not source or not target:
            continue
        if not is_meaningful_endpoint(source) or not is_meaningful_endpoint(target):
            logger.info("跳过无意义双链: %s -> %s", source, target)
            continue
        relation = str(arrow.get("relation") or "").strip()
        suffix = f"（{relation}）" if relation else ""
        source_text = _render_endpoint(source, arrow_targets, heading_anchors)
        target_text = _render_endpoint(target, arrow_targets, heading_anchors)
        lines.append(f"- {source_text} → {target_text}{suffix}")
    return lines if len(lines) > 3 else []


def render_markdown(
    note: dict[str, Any],
    meta: dict[str, Any] | None = None,
    arrow_targets: dict[str, tuple[str, str]] | None = None,
    heading_anchors: dict[str, str] | None = None,
) -> str:
    """把笔记结构渲染为 Markdown 文本。"""
    meta = meta or {}
    lines: list[str] = []
    lines.extend(_render_frontmatter(note, meta))
    lines.extend(_render_sections(note.get("sections") or []))
    lines.extend(_render_arrows(note.get("arrows") or [], arrow_targets, heading_anchors))
    lines.append("")

    # 压缩连续空行，保证排版稳定
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).lstrip("\n")


def _fit_component(name: str) -> str:
    """按 UTF-16 码元把单个目录项压到 Windows 上限内（部分字符占 2 个码元）。"""
    def units(text: str) -> int:
        return len(text.encode("utf-16-le", "surrogatepass")) // 2

    if units(name) <= MAX_FILENAME_LENGTH:
        return name
    shortened = name
    while shortened and units(shortened) > MAX_FILENAME_LENGTH:
        shortened = shortened[:-1]
    logger.warning("文件名超过 Windows 单目录项上限，已截断: %s -> %s", name, shortened)
    return shortened


def sanitize_filename(title: str) -> str:
    """清理 Windows 非法字符，生成安全的文件名。"""
    name = ILLEGAL_FILENAME_CHARS.sub(" ", title).strip()
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = f"NoteFlow-{datetime.now():%Y%m%d-%H%M%S}"
    stem, dot, suffix = name.partition(".")
    if stem.upper() in RESERVED_FILENAME_STEMS:
        name = f"Note-{name}"
    return _fit_component(name).strip(" .")


def truncate_filename(name: str, budget: int) -> str:
    """把超长名字截成「前段-6 位哈希」，同名不同文不会互相覆盖。"""
    if budget <= 0 or len(name) <= budget:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    keep = max(12, budget - len(digest) - 1)
    head = name[:keep].rstrip(" .-_")
    return f"{head}-{digest}" if head else f"Note-{digest}"


def write_markdown(
    note: dict[str, Any],
    notes_dir: Path,
    meta: dict[str, Any] | None = None,
    filename: str | None = None,
) -> Path:
    """写出 Markdown 文件（同名自动加序号），返回文件路径。

    filename 用于多页文档按「文档名-页码」命名；不传则用笔记标题。
    """
    notes_dir = Path(notes_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)

    base = sanitize_filename(filename if filename else str(note.get("title", "")))
    target = notes_dir / f"{base}.md"
    index = 2
    while target.exists():
        target = notes_dir / f"{base}-{index}.md"
        index += 1

    target.write_text(render_markdown(note, meta), encoding="utf-8")
    logger.info("Markdown 已生成: %s", target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="NoteFlow Markdown 生成（可独立运行）")
    parser.add_argument("json_file", type=Path, help="识别结果 JSON 文件")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output/notes"), help="输出目录")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    if not args.json_file.exists():
        logger.error("JSON 文件不存在: %s", args.json_file)
        return 1

    note = json.loads(args.json_file.read_text(encoding="utf-8"))
    output = write_markdown(note, args.output_dir, {"source": args.json_file.name})
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
