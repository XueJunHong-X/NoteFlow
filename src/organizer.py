"""Obsidian vault 归档：按课程/笔记本名分目录、写 frontmatter、维护课程索引与总览。

目录默认是「课程 → 文档 → 页」，学科只写进 frontmatter 的 subject/tags，
不再参与目录主分组；跨学科课程（如工程力学）单独成目录。

每写一篇笔记都会重建所属课程索引和课程总览（按学科标签分组），
并按 P2 规则自动注入跨笔记双链（链接失败只告警，不影响笔记落盘）。

可独立运行（把已有 notes 目录重新归档进 vault，不调用识别接口）：
    python -m src.organizer output/real_v3/notes -o output/real_v3_vault
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from config import UNCLASSIFIED, Settings
from src import failure_log, linker
from src.markdown_gen import is_weak_heading, render_markdown, sanitize_filename, truncate_filename
from src.mindmap_gen import build_mermaid, build_mindmap_file

logger = logging.getLogger(__name__)

# vault 笔记记录缓存：key 为 vault 绝对路径，value 为 (文件戳, 记录列表)。
# 文件戳用 (mtime_ns, size)，外部编辑（例如在 Obsidian 里改稿）也能被下一次扫描发现。
_RECORD_CACHE: dict[str, tuple[dict[Path, tuple[int, int]], list[linker.NoteRecord]]] = {}
_RECORD_CACHE_LOCK = threading.Lock()

# vault 写入串行化：并发识别时多页同时归档，索引/总览/双链必须排队写，
# 否则两篇同时改同一个索引文件会互相覆盖。RLock 允许归档内部嵌套调用。
_ARCHIVE_LOCK = threading.RLock()


def _vault_write_locked(func):
    """保证归档与各类 vault 重建不会并发改写同一批 Markdown。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _ARCHIVE_LOCK:
            return func(*args, **kwargs)

    return wrapper


def _io_path(path: Path) -> Path:
    """Windows 长路径：超过 MAX_PATH 时加 \\\\?\\ 前缀，读写都不受 260 限制。"""
    if os.name != "nt":
        return Path(path)
    text = str(path)
    if text.startswith("\\\\?\\"):
        return Path(text)
    try:
        absolute = str(Path(path).resolve())
    except OSError:
        absolute = str(Path(path).absolute())
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def _scan_root(path: Path) -> Path:
    """目录遍历统一走扩展路径，避免深目录/长课程名在 scandir 阶段就失败。"""
    return _io_path(path)


def _logical_path(path: Path) -> Path:
    """去掉 \\\\?\\ 前缀，缓存键与界面展示仍用普通路径。"""
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[8:])
    if text.startswith("\\\\?\\"):
        return Path(text[4:])
    return Path(path)

# 完整路径超过 240 字符就截断文档名，给 .md 与系统余量留空间
PATH_TRUNCATE_THRESHOLD = 240
DOC_NAME_MIN_HEAD = 24

PAGE_SUFFIX = re.compile(r"\s*第\s*\d+\s*页\s*$")
HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
# 「关联概念」里一行关系：「- A → B（关系）」，两端可能是双链、页内锚点或纯文本
ARROW_RELATION = re.compile(r"[（(]([^（）()]*)[）)]\s*$")
# 正文里的双链（含别名 [[stem|展示文字]] 和页内锚点 [[#标题]]），重建时还原成纯文字再重新注入
WIKILINK = re.compile(
    r"\[\[(?:(?P<stem>[^\]|#]+))?(?:#(?P<anchor>[^\]|]+))?(?:\|(?P<alias>[^\]]+))?\]\]"
)
# 相机/截图/测试文件这类文件名不是课程名
GENERIC_SOURCE = re.compile(
    r"^(?:real_handwriting|sample_note|sample|img|dsc|scan|image|photo|p\d{3,}|page\d+|unnamed|未命名|noteflow|tmp)",
    re.IGNORECASE,
)
# 没有课程关键词可查时，用这些后缀判断标签像不像课程/笔记本名
COURSE_LIKE_TAG = re.compile(r"(?:学|工程|技术|原理|设计|制图|实验|教程|概论|导论)$")
GENERIC_TAGS = {"未分类", "待校对", "笔记", "重点", "noteflow", "离线示例"}
SOURCE_TYPE_BY_SUFFIX = {
    ".pdf": "pdf",
    ".goodnotes": "goodnotes",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".bmp": "image",
    ".webp": "image",
}
MOC_TYPE = "moc"
MINDMAP_TYPE = "mindmap"
CONCEPT_TYPE = "concept"


# ---------------------------------------------------------------------------
# frontmatter 读写（只覆盖 NoteFlow 自己写出的简单 YAML 子集）
# ---------------------------------------------------------------------------


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆出 frontmatter 与正文，解析失败时按无 frontmatter 处理。"""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text

    end = text.find("\n---", 4)
    if end == -1:
        return {}, text

    raw = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    data: dict[str, Any] = {}
    current_key: str | None = None

    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and current_key:
            data.setdefault(current_key, [])
            if isinstance(data[current_key], list):
                data[current_key].append(_unquote(line.lstrip()[2:]))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        current_key = key
        if value:
            data[key] = _unquote(value)
        else:
            data[key] = []
    return data, body


def read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = _io_path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    return split_frontmatter(text)[0]


# ---------------------------------------------------------------------------
# 学科 / 课程推断
# ---------------------------------------------------------------------------


def _source_stem(meta: dict[str, Any]) -> str:
    """从 source（可能是「xx.goodnotes 第 3 页」）取出文件 stem。"""
    source = str(meta.get("source_file") or meta.get("source") or "").strip()
    source = PAGE_SUFFIX.sub("", source)
    return Path(source).stem if source else ""


def infer_course(note: dict[str, Any], meta: dict[str, Any], settings: Settings) -> str:
    """课程 / 笔记本名作为目录主分组；推不出就落「未分类」。"""
    course = str(meta.get("course") or "").strip()
    if course:
        return course

    tags = [tag.strip() for tag in note.get("tags") or [] if isinstance(tag, str) and tag.strip()]
    mapping = {keyword for keywords in settings.subject_keywords.values() for keyword in keywords}

    # 1) 标签命中已知课程名（如「高等数学」「工程制图」）
    for tag in tags:
        if tag in mapping:
            return tag

    # 2) 文件名本身就是课程/笔记本名（如 通信工程.pdf、工程力学.goodnotes）
    stem = _source_stem(meta)
    if stem and not GENERIC_SOURCE.match(stem):
        return stem

    # 3) 标签看起来像课程名（兼容「工程力学」这类跨学科课程）
    subject_names = set(settings.subject_keywords)
    for tag in tags:
        if tag in subject_names or tag.lower() in GENERIC_TAGS:
            continue
        if COURSE_LIKE_TAG.search(tag):
            return tag

    return UNCLASSIFIED


def infer_subject(note: dict[str, Any], meta: dict[str, Any], settings: Settings, course: str) -> str:
    """学科只作为 frontmatter 标签与总览分组，不参与目录主分组。"""
    subject = str(meta.get("subject") or "").strip()
    if subject:
        return subject

    mapping: dict[str, str] = {}
    for subject_name, keywords in settings.subject_keywords.items():
        for keyword in keywords:
            mapping[keyword] = subject_name

    if course in mapping:
        return mapping[course]

    tags = [tag.strip() for tag in note.get("tags") or [] if isinstance(tag, str) and tag.strip()]
    ordered_keywords = sorted(mapping, key=len, reverse=True)
    for tag in tags:
        for keyword in ordered_keywords:
            if keyword in tag:
                return mapping[keyword]

    for tag in tags:
        if tag in settings.subject_keywords:
            return tag

    return UNCLASSIFIED


# ---------------------------------------------------------------------------
# 路径与写入
# ---------------------------------------------------------------------------


def _layout_parts(course: str, subject: str, document: str, layout: str) -> list[str]:
    values = {"course": course, "subject": subject, "document": document}
    try:
        rendered = layout.format(**values)
    except (KeyError, ValueError, IndexError):
        logger.warning("vault 目录模板无效，回退为按课程分组: %s", layout)
        rendered = course

    parts: list[str] = []
    for raw_part in re.split(r"[\\/]+", rendered):
        if not raw_part.strip():
            continue
        part = sanitize_filename(raw_part)
        if part and (not parts or parts[-1] != part):
            parts.append(part)

    # 模板里没写 {document} 时，多页文档自动追加一层文档目录；与课程重名则跳过
    if document and "{document}" not in layout:
        doc = sanitize_filename(document)
        if doc and (not parts or parts[-1] != doc):
            parts.append(doc)
    return parts or [UNCLASSIFIED]


def build_note_path(
    note: dict[str, Any],
    meta: dict[str, Any],
    settings: Settings,
    document: str = "",
    filename_hint: str | None = None,
) -> tuple[Path, str, str]:
    """计算笔记在 vault 中的目标路径，同时把 subject/course 写回 meta。

    只有完整路径超过阈值时才截断「文档名」（目录与文件名前缀），课程/学科不截；
    截断保留前段并追加 6 位哈希，正文标题与 frontmatter.title 始终完整。
    """
    course = infer_course(note, meta, settings)
    subject = infer_subject(note, meta, settings, course)
    meta["course"] = course
    meta["subject"] = subject

    parts = _layout_parts(course, subject, document, settings.vault_layout)
    base = sanitize_filename(filename_hint or str(note.get("title") or "未命名笔记"))
    target = settings.vault_dir.joinpath(*parts, f"{base}.md")

    if not document or len(str(target)) <= PATH_TRUNCATE_THRESHOLD:
        return target, subject, course

    # 先压缩文件名（它由文档名派生），再视情况压缩文档目录，课程目录始终不动
    prefix_len = len(str(settings.vault_dir.joinpath(*parts))) + 1
    stem_budget = max(DOC_NAME_MIN_HEAD + 7, PATH_TRUNCATE_THRESHOLD - prefix_len - len(".md"))
    if len(base) > stem_budget:
        shortened = truncate_filename(base, stem_budget)
        logger.warning("文档名过长，文件名已截断: %s -> %s", base, shortened)
        base = shortened
        target = settings.vault_dir.joinpath(*parts, f"{base}.md")

    doc_part = sanitize_filename(document)
    if doc_part in parts and len(str(target)) > PATH_TRUNCATE_THRESHOLD:
        index = max(i for i, part in enumerate(parts) if part == doc_part)
        rest = [part for i, part in enumerate(parts) if i != index]
        budget = (
            PATH_TRUNCATE_THRESHOLD
            - len(str(settings.vault_dir.joinpath(*rest)))
            - 2  # 课程→文档、文档→文件两个分隔符
            - len(base)
            - len(".md")
        )
        shortened = truncate_filename(doc_part, max(DOC_NAME_MIN_HEAD + 7, budget))
        logger.warning("文档名过长，目录已截断: %s -> %s", doc_part, shortened)
        parts[index] = shortened
        target = settings.vault_dir.joinpath(*parts, f"{base}.md")

    return target, subject, course


def _pick_target(target: Path, source: str) -> Path:
    """同一篇重新处理时覆盖旧文件；不同来源同名时加序号，避免互相覆盖。"""
    if not _io_path(target).exists():
        return target

    existing = read_frontmatter(target)
    if str(existing.get("source") or "") == source:
        return target

    index = 2
    while True:
        candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
        if not _io_path(candidate).exists():
            return candidate
        if str(read_frontmatter(candidate).get("source") or "") == source:
            return candidate
        index += 1


@_vault_write_locked
def archive_note(
    note: dict[str, Any],
    meta: dict[str, Any],
    settings: Settings,
    document: str = "",
    filename_hint: str | None = None,
) -> Path:
    """渲染 Markdown（含双链与思维导图）并写入 vault，随后更新课程索引与总览。

    双链、索引、导图任一环失败都只告警并留痕，绝不阻断笔记落盘。
    """
    meta = dict(meta)
    target, subject, course = build_note_path(note, meta, settings, document, filename_hint)
    _io_path(target).parent.mkdir(parents=True, exist_ok=True)
    target = _pick_target(target, str(meta.get("source") or ""))

    # 学科降级为标签：写进 frontmatter 的 subject，同时补进 tags 方便检索
    render_note = dict(note)
    tags = [tag for tag in note.get("tags") or [] if isinstance(tag, str) and tag.strip()]
    if subject and subject != UNCLASSIFIED and subject not in tags:
        tags.insert(0, subject)
    render_note["tags"] = tags

    # 覆盖同一来源的旧文件时，接管旧文件「思维导图」之后的用户内容，
    # 否则重新识别同一份笔记会把用户在 Obsidian 里补写的段落静默清空。
    if not note.get("trailing_lines") and _io_path(target).exists():
        try:
            previous, _ = parse_markdown_note(target)
            preserved = previous.get("trailing_lines") or []
        except Exception as exc:
            preserved = []
            logger.warning("读取旧笔记尾部失败（按无尾部处理）: %s", exc)
        if preserved:
            render_note["trailing_lines"] = preserved

    # 链接规划：读取当前 vault（不含本篇），失败则退化为不带链接
    plan = linker.LinkPlan()
    prior_count = 0
    try:
        records = collect_note_records(settings.vault_dir)
        prior_count = len(records)
        plan = linker.build_plan(render_note, meta, records, settings, exclude_stem=target.stem)
        if plan.skipped:
            logger.info(
                "跳过无合适目标的链接 %d 条: %s",
                len(plan.skipped),
                "，".join(f"{key}({reason})" for key, reason in plan.skipped[:5]),
            )
    except Exception as exc:
        failure_log.record(settings, target, meta.get("page"), "link_plan", exc)
        logger.warning("计算双链失败，本篇不注入链接: %s", exc)

    body = render_markdown(
        render_note,
        meta,
        arrow_targets=plan.arrow_targets,
        heading_anchors=plan.heading_anchors,
    ).rstrip() + "\n"
    try:
        body = linker.apply_inline_links(body, plan)
    except Exception as exc:
        failure_log.record(settings, target, meta.get("page"), "link_inject", exc)
        logger.warning("正文注入双链失败，按无链接输出: %s", exc)

    if settings.mindmap_mode == "inline":
        body += "\n## 思维导图\n\n" + build_mermaid(
            render_note, related=plan.related_labels(str(render_note.get("title") or ""))
        ) + "\n"
    body = _append_trailing(body, render_note)

    _io_path(target).write_text(body, encoding="utf-8")
    if plan.applied:
        logger.info("已注入 %d 条跨笔记双链: %s", len(plan.applied), "，".join(e.stem for e in plan.applied))
    logger.info("笔记已归档: %s（课程: %s，学科标签: %s）", target, course, subject)

    if settings.mindmap_mode == "file":
        try:
            mindmap_path = target.with_name(f"{target.stem}-思维导图.md")
            _io_path(mindmap_path).write_text(
                build_mindmap_file(
                    render_note,
                    target.name,
                    {"created": meta.get("created")},
                    related=plan.related_labels(str(render_note.get("title") or "")),
                ),
                encoding="utf-8",
            )
            logger.info("思维导图已生成: %s", mindmap_path)
        except Exception as exc:
            failure_log.record(settings, target, meta.get("page"), "mindmap", exc)
            logger.warning("生成思维导图失败（不影响笔记）: %s", exc)

    if settings.moc_enabled:
        try:
            # 刚写完的这篇只在缓存里增量更新，不再为索引/总览做第二次全量扫描
            records = collect_note_records(settings.vault_dir, dirty=(target,))
        except Exception as exc:
            records = None
            failure_log.record(settings, target, meta.get("page"), "moc_scan", exc)
            logger.warning("索引扫描失败，回退为逐次读取: %s", exc)
        try:
            refresh_course_index(course, settings, records=records)
        except Exception as exc:  # 索引失败不能影响笔记本身
            failure_log.record(settings, target, meta.get("page"), "course_index", exc)
            logger.warning("更新课程索引失败（%s）: %s", course, exc)
        try:
            refresh_overview(settings, records=records)
        except Exception as exc:
            failure_log.record(settings, target, meta.get("page"), "overview", exc)
            logger.warning("更新课程总览失败: %s", exc)

    # 刚够冷启动样本数时补一次全量对齐：此前归档的笔记也能拿到双链
    if settings.link_enabled and prior_count + 1 == settings.link_min_notes:
        try:
            refresh_links(settings)
        except Exception as exc:
            failure_log.record(settings, target, meta.get("page"), "refresh_links", exc)
            logger.warning("冷启动补齐双链失败（不影响笔记）: %s", exc)

    return target


# ---------------------------------------------------------------------------
# 课程索引与课程总览（MOC）
# ---------------------------------------------------------------------------


def _course_index_path(course: str, settings: Settings) -> Path:
    name = sanitize_filename(course or UNCLASSIFIED)
    return settings.vault_dir / name / f"{name} 索引.md"


def _walk_note_frontmatters(settings: Settings) -> list[tuple[Path, dict[str, Any]]]:
    items: list[tuple[Path, dict[str, Any]]] = []
    for entry in sorted(_scan_root(settings.vault_dir).rglob("*.md")):
        path = _logical_path(entry)
        meta = read_frontmatter(path)
        if meta.get("type") in (MOC_TYPE, MINDMAP_TYPE, CONCEPT_TYPE):
            continue
        items.append((path, meta))
    return items


def collect_note_records(vault_dir: Path, dirty: Iterable[Path] | None = None) -> list[linker.NoteRecord]:
    """一次扫描拿到 vault 里全部笔记的「可链接信息」：frontmatter + 标题层级。

    只读一次文件，供双链规划和索引刷新共用，避免每篇反复全量扫描。

    结果按文件 mtime/size 缓存在进程内：归档一页时只重新解析真正变化的文件，
    其余记录直接复用。传 dirty 时跳过目录遍历，只更新指定文件（用于刚写完的笔记），
    因此每页两次取记录的成本从「两次全量读盘」降到「一次目录 stat + 一个新文件」。
    """
    vault_dir = Path(vault_dir)
    if not _io_path(vault_dir).is_dir():
        return []

    key = str(vault_dir.resolve())
    # 扫描结果里的路径统一是「去掉 \\?\ 前缀的绝对路径」，dirty 也要归一，
    # 否则同一个文件会以相对/绝对两种键各存一份，产生重复记录。
    dirty_paths = {_logical_path(_io_path(path)) for path in dirty or ()}
    with _RECORD_CACHE_LOCK:
        cached = _RECORD_CACHE.get(key)

    if dirty_paths and cached is not None:
        stamps, records = cached
        by_path = {record.path: record for record in records}
        for path in dirty_paths:
            record = _read_note_record(path)
            if record is None:
                by_path.pop(path, None)
                stamps.pop(path, None)
                continue
            try:
                stat = _io_path(path).stat()
                stamps[path] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamps.pop(path, None)
            by_path[path] = record
        merged = sorted(by_path.values(), key=lambda item: item.path)
        with _RECORD_CACHE_LOCK:
            _RECORD_CACHE[key] = (stamps, merged)
        return list(merged)

    old_stamps, old_records = cached if cached is not None else ({}, [])
    old_by_path = {record.path: record for record in old_records}

    records: list[linker.NoteRecord] = []
    stamps: dict[Path, tuple[int, int]] = {}
    for entry in sorted(_scan_root(vault_dir).rglob("*.md")):
        path = _logical_path(entry)
        try:
            stat = _io_path(path).stat()
        except OSError:
            continue
        stamp = (stat.st_mtime_ns, stat.st_size)
        stamps[path] = stamp
        if old_stamps.get(path) == stamp:
            record = old_by_path.get(path)
            if record is not None:
                records.append(record)
                continue
        record = _read_note_record(path)
        if record is not None:
            records.append(record)

    with _RECORD_CACHE_LOCK:
        _RECORD_CACHE[key] = (stamps, records)
    return records


def _read_note_record(path: Path) -> linker.NoteRecord | None:
    """读取单篇笔记的 frontmatter 与标题层级；索引页/导图/概念页不参与链接。"""
    try:
        text = _io_path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = split_frontmatter(text)
    if meta.get("type") in (MOC_TYPE, MINDMAP_TYPE, CONCEPT_TYPE):
        return None
    headings = [match.group(2).strip() for match in HEADING_LINE.finditer(body)]
    return linker.NoteRecord(
        path=path,
        stem=path.stem,
        title=str(meta.get("title") or path.stem),
        course=str(meta.get("course") or UNCLASSIFIED),
        subject=str(meta.get("subject") or UNCLASSIFIED),
        document=_source_stem(meta),
        page=_as_int(meta.get("page")) if meta.get("page") is not None else None,
        headings=headings,
        tags=[str(tag) for tag in meta.get("tags") or [] if str(tag).strip()],
        source=str(meta.get("source") or path.name),
    )


def clear_record_cache(vault_dir: Path | None = None) -> None:
    """清空记录缓存（vault 被整体重建或外部批量改写后调用）。"""
    with _RECORD_CACHE_LOCK:
        if vault_dir is None:
            _RECORD_CACHE.clear()
        else:
            _RECORD_CACHE.pop(str(Path(vault_dir).resolve()), None)


def _iter_note_meta(
    settings: Settings, records: list[linker.NoteRecord] | None
) -> list[tuple[Path, dict[str, Any]]]:
    """索引/总览取数：优先复用已扫描的记录，避免重复读盘。"""
    if records is None:
        return _walk_note_frontmatters(settings)
    return [
        (
            record.path,
            {
                "course": record.course,
                "subject": record.subject,
                "title": record.title,
                "source": record.source,
                "page": record.page,
            },
        )
        for record in records
    ]


@_vault_write_locked
def refresh_course_index(
    course: str, settings: Settings, records: list[linker.NoteRecord] | None = None
) -> Path:
    """一门课一份索引：列出该课程目录下的全部笔记。"""
    course = course or UNCLASSIFIED
    entries: list[tuple[str, str, Any, str]] = []  # source, title, page, stem
    subject_votes: dict[str, int] = {}

    for path, meta in _iter_note_meta(settings, records):
        if str(meta.get("course") or UNCLASSIFIED) != course:
            continue
        subject = str(meta.get("subject") or UNCLASSIFIED)
        subject_votes[subject] = subject_votes.get(subject, 0) + 1
        entries.append(
            (str(meta.get("source") or path.name), str(meta.get("title") or path.stem), meta.get("page"), path.stem)
        )

    entries.sort(key=lambda item: (item[0], _as_int(item[2]), item[3]))
    subject = max(subject_votes, key=subject_votes.get) if subject_votes else UNCLASSIFIED

    lines = [
        "---",
        f"title: {json.dumps(course + ' 索引', ensure_ascii=False)}",
        f"type: {MOC_TYPE}",
        f"course: {json.dumps(course, ensure_ascii=False)}",
        f"subject: {json.dumps(subject, ensure_ascii=False)}",
        f"updated: {datetime.now().isoformat(timespec='seconds')}",
        "---",
        "",
        f"# {course}",
        "",
        "> 本页由 NoteFlow 自动维护：新增笔记后会自动重建，请勿手工编辑。",
        "> 返回 [[课程总览]]。",
    ]

    if not entries:
        lines.extend(["", "（该课程下暂无笔记）"])
    else:
        lines.extend(["", f"共 {len(entries)} 篇笔记"])
        for source, title, page, stem in entries:
            link = f"[[{stem}]]" if title == stem else f"[[{stem}|{title.replace('|', '／')}]]"
            lines.append(f"- {link} · {source}")

    target = _course_index_path(course, settings)
    _io_path(target).parent.mkdir(parents=True, exist_ok=True)
    _io_path(target).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    logger.info("课程索引已更新: %s（%d 篇）", target, len(entries))
    return target


@_vault_write_locked
def refresh_overview(settings: Settings, records: list[linker.NoteRecord] | None = None) -> Path:
    """课程总览：按学科标签分组列出全部课程，跨学科课程照常出现。"""
    groups: dict[str, dict[str, int]] = {}  # subject -> course -> note count
    for _path, meta in _iter_note_meta(settings, records):
        subject = str(meta.get("subject") or UNCLASSIFIED)
        course = str(meta.get("course") or UNCLASSIFIED)
        groups.setdefault(subject, {})
        groups[subject][course] = groups[subject].get(course, 0) + 1

    subjects = sorted(groups, key=lambda name: (name == UNCLASSIFIED, name))
    total_notes = sum(sum(courses.values()) for courses in groups.values())
    total_courses = len({course for courses in groups.values() for course in courses})

    lines = [
        "---",
        f"title: {json.dumps('课程总览', ensure_ascii=False)}",
        f"type: {MOC_TYPE}",
        f"updated: {datetime.now().isoformat(timespec='seconds')}",
        "---",
        "",
        "# 课程总览",
        "",
        "> 本页由 NoteFlow 自动维护：新增笔记后会自动重建，请勿手工编辑。",
        f"> 共 {total_courses} 门课程 / {total_notes} 篇笔记。",
    ]

    if not groups:
        lines.extend(["", "（vault 中暂无笔记）"])
    else:
        for subject in subjects:
            lines.extend(["", f"## {subject}", ""])
            for course, count in sorted(groups[subject].items()):
                index_stem = f"{sanitize_filename(course)} 索引"
                lines.append(f"- [[{index_stem}|{course.replace('|', '／')}]] · {count} 篇笔记")

    target = settings.vault_dir / "课程总览.md"
    _io_path(target).parent.mkdir(parents=True, exist_ok=True)
    _io_path(target).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    logger.info("课程总览已更新: %s（%d 门课程）", target, total_courses)
    return target


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 离线重建：从已有 Markdown 产物重新归档，不调用识别接口
# ---------------------------------------------------------------------------


def _append_content(section: dict[str, Any], line: str) -> None:
    section["content"] = (section["content"] + "\n" + line).strip("\n") if section["content"] else line


def _unwrap_links(line: str) -> str:
    """把正文里的双链还原成纯文字，重建时才能按新关系重新注入（避免每次叠加）。"""

    def repl(match: re.Match[str]) -> str:
        return (match.group("alias") or match.group("stem") or match.group("anchor") or "").strip()

    return WIKILINK.sub(repl, line)


def _parse_arrow_line(line: str) -> dict[str, str] | None:
    """解析「- A → B（关系）」，A/B 可以是双链、页内锚点或纯文本。"""
    if not line.startswith("- ") or "→" not in line:
        return None

    body = line[2:].strip()
    relation = ""
    match = ARROW_RELATION.search(body)
    if match:
        relation = match.group(1).strip()
        body = body[: match.start()].strip()

    source, _, target = body.partition("→")
    source_text = _unwrap_links(source.strip()).strip()
    target_text = _unwrap_links(target.strip()).strip()
    if not source_text or not target_text:
        return None
    return {"from": source_text, "to": target_text, "relation": relation}


def parse_markdown_note(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """把 NoteFlow 生成的 Markdown 反向解析成 note 结构 + meta。"""
    text = _io_path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
    meta, body = split_frontmatter(text)
    meta = dict(meta)

    lines = body.splitlines()
    arrows: list[dict[str, str]] = []
    content_lines: list[str] = []
    trailing_lines: list[str] = []
    in_mindmap = False  # 仍在丢弃「思维导图」区（标题行 → 代码块闭合）
    fence_open = False
    after_mindmap = False  # 已越过思维导图区，后面全是用户内容
    skip_section: str | None = None  # 仅用于「关联概念」
    for line in lines:
        stripped = line.strip()

        if in_mindmap:
            if stripped.startswith("```"):
                fence_open = not fence_open
                if not fence_open:  # 代码块闭合，思维导图区结束
                    in_mindmap = False
                    after_mindmap = True
                continue
            if fence_open:
                continue  # mermaid 源码
            if stripped:  # 代码块之前（或没有代码块）出现正文，从这行起都是用户内容
                in_mindmap = False
                after_mindmap = True
                trailing_lines.append(line)
            continue

        if after_mindmap:
            # 思维导图之后的内容（任意 Markdown：标题、段落、列表、表格、代码块）
            # 必须原样保留，否则重写全文时会把用户补写的内容静默删掉。
            trailing_lines.append(line)
            continue

        if stripped in ("## 关联概念", "# 关联概念"):
            skip_section = "arrows"
            continue
        if stripped in ("## 思维导图", "# 思维导图"):
            # 思维导图由归档时重新生成，丢弃该区，其后内容进 trailing
            in_mindmap = True
            fence_open = False
            skip_section = None
            continue
        if skip_section and HEADING_LINE.match(line):
            skip_section = None
        if skip_section == "arrows":
            arrow = _parse_arrow_line(stripped)
            if arrow:
                arrows.append(arrow)
            elif stripped:
                # 不是生成器写的箭头行，说明是用户补写的内容，原样保留
                trailing_lines.append(line)
            continue
        content_lines.append(_unwrap_links(line))

    root: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    preamble: dict[str, Any] | None = None

    for line in content_lines:
        heading_match = HEADING_LINE.match(line)
        if heading_match:
            level = min(len(heading_match.group(1)), 3)
            heading = heading_match.group(2).strip()
            if is_weak_heading(heading):
                if stack:
                    _append_content(stack[-1], heading)
                    preamble = None
                else:
                    if preamble is None:
                        preamble = {"heading": "", "level": 1, "content": "", "highlights": [], "subsections": []}
                        root.append(preamble)
                    _append_content(preamble, heading)
                continue

            section = {"heading": heading, "level": level, "content": "", "highlights": [], "subsections": []}
            while stack and int(stack[-1]["level"]) >= level:
                stack.pop()
            if stack:
                stack[-1]["subsections"].append(section)
            else:
                root.append(section)
            stack.append(section)
            preamble = None
            continue

        if not line.strip():
            continue
        if stack:
            _append_content(stack[-1], line)
        else:
            if preamble is None:
                preamble = {"heading": "", "level": 1, "content": "", "highlights": [], "subsections": []}
                root.append(preamble)
            _append_content(preamble, line)

    note = {
        "title": str(meta.get("title") or (root[0]["heading"] if root else path.stem)),
        "sections": root,
        "arrows": arrows,
        "tags": [tag for tag in meta.get("tags") or [] if isinstance(tag, str)],
        "trailing_lines": trailing_lines,
    }
    return note, meta


def infer_source_type(source: str) -> str:
    cleaned = PAGE_SUFFIX.sub("", str(source or "")).strip()
    return SOURCE_TYPE_BY_SUFFIX.get(Path(cleaned).suffix.lower(), "unknown")


def _append_trailing(body: str, note: dict[str, Any]) -> str:
    """把「思维导图」之后保留下来的尾部内容接回文末（往返保真）。"""
    trailing = note.get("trailing_lines") or []
    if not trailing:
        return body
    return body.rstrip("\n") + "\n\n" + "\n".join(trailing).strip("\n") + "\n"


@_vault_write_locked
def refresh_links(settings: Settings, vault_dir: Path | None = None) -> int:
    """按当前 vault 全量重算双链（不调模型）。

    逐篇归档时只能看到已存在的笔记，重建目录时更会先写入少量笔记。
    这里统一做一次全量重算，保证结果与处理顺序无关、也不留人工补刀。
    """
    vault = Path(vault_dir or settings.vault_dir)
    records = collect_note_records(vault)
    if not records:
        return 0

    updated = 0
    for record in records:
        try:
            note, meta = parse_markdown_note(record.path)
            plan = linker.build_plan(note, meta, records, settings, exclude_stem=record.stem)
            body = render_markdown(
                note,
                meta,
                arrow_targets=plan.arrow_targets,
                heading_anchors=plan.heading_anchors,
            ).rstrip() + "\n"
            body = linker.apply_inline_links(body, plan)
            if settings.mindmap_mode == "inline":
                body += "\n## 思维导图\n\n" + build_mermaid(
                    note, related=plan.related_labels(str(note.get("title") or ""))
                ) + "\n"
            body = _append_trailing(body, note)
            if body != _io_path(record.path).read_text(encoding="utf-8"):
                _io_path(record.path).write_text(body, encoding="utf-8")
                updated += 1
        except Exception as exc:  # 单篇失败继续刷新其余笔记
            failure_log.record(settings, record.path, None, "refresh_links", exc)
            logger.warning("刷新双链失败 %s: %s", record.path.name, exc)

    logger.info("双链全量刷新完成：%d/%d 篇有变化", updated, len(records))
    return updated


@_vault_write_locked
def rebuild_vault(notes_dir: Path, settings: Settings) -> list[Path]:
    """把已有 Markdown 产物重新归档进 vault（用于离线验证，不调用模型）。"""
    notes_dir = Path(notes_dir)
    if not notes_dir.is_dir():
        raise FileNotFoundError(f"待重建目录不存在: {notes_dir}")

    outputs: list[Path] = []
    for entry in sorted(_scan_root(notes_dir).rglob("*.md")):
        md = _logical_path(entry)
        try:
            note, fm = parse_markdown_note(md)
            source = str(fm.get("source") or md.name)
            page = fm.get("page")
            document = ""
            if page is not None:
                document = _source_stem({"source": source}) or md.parent.name
            meta: dict[str, Any] = {
                "source": source,
                "source_type": fm.get("source_type") or infer_source_type(source),
                "created": fm.get("created"),
                "page": _as_int(page) if page is not None else None,
                "page_note": fm.get("page_note"),
            }
            if meta["page"] is None:
                meta.pop("page")
            if not meta["page_note"]:
                meta.pop("page_note")
            if fm.get("subject"):
                meta["subject"] = fm["subject"]
            if fm.get("course"):
                meta["course"] = fm["course"]

            outputs.append(archive_note(note, meta, settings, document=document, filename_hint=md.stem))
        except Exception as exc:
            logger.error("重建失败 %s: %s", md.name, exc)
    logger.info("离线重建完成：%d/%d 篇", len(outputs), len(list(notes_dir.rglob("*.md"))))

    if settings.link_enabled and outputs:
        try:
            refresh_links(settings)
        except Exception as exc:
            failure_log.record(settings, notes_dir, None, "refresh_links", exc)
            logger.warning("双链全量刷新失败（不影响已归档笔记）: %s", exc)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description="NoteFlow vault 归档（可独立运行）")
    parser.add_argument("notes_dir", type=Path, help="已有 Markdown 产物目录")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"), help="输出目录（vault 位于其下）")
    args = parser.parse_args()

    from config import setup_logging

    settings = Settings.from_env()
    settings.output_dir = args.output_dir
    setup_logging(settings.log_level)
    settings.ensure_dirs()

    rebuild_vault(args.notes_dir, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
