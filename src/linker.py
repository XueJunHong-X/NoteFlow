"""跨笔记双链：概念提取、别名归一、链接解析与正文注入。

设计原则（P2）：
- 概念只来自已有识别结果（标题、各级 heading），不额外调用识别接口
- 链接目标必须是 vault 里**真实存在**的笔记；找不到、有歧义、或本篇自己就在讲这个概念时不连
- 正文只在首次出现处注入一次，跳过标题行、引用块、代码块、LaTeX 公式
- 冷启动保守：低于权重阈值或笔记总量过少时宁可不连

可独立运行（查看某篇笔记会往哪里连）：
    python -m src.linker output/p2_course/vault
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from config import UNCLASSIFIED, Settings
from src.markdown_gen import is_meaningful_endpoint, is_weak_heading

logger = logging.getLogger(__name__)

# 单篇笔记的概念提取结果缓存：签名是 (标题, 标题层级)，内容没变就不重复跑正则。
# 归档上千页时，每页都要为全部已有笔记重建概念表，这一步是主要 CPU 开销。
_CONCEPT_CACHE: dict[Path, tuple[tuple[str, tuple[str, ...]], list["Concept"]]] = {}
_CONCEPT_CACHE_LOCK = threading.Lock()

# 列表编号 / 项目符号：「1. 」「二、」「(3)」「• 」
ENUM_PREFIX = re.compile(
    r"^\s*(?:第?[一二三四五六七八九十百零〇]+[、.．)）]|\d+(?:[.．]\d+)*[、.．)）]?|[（(]\s*\d+\s*[）)]|[•·・*\-]\s*)\s*"
)
TRAILING_PUNCT = re.compile(r"[\s:：,，。.;；、!！?？]+$")
SENTENCE_PUNCT = re.compile(r"[。；;！？!?，,、]")
FORMULA_CHARS = set("$\\^_{}=")
# 题型 / 章节 / 图表标记：出现在 heading 位置上但不是概念
CHAPTER_HEAD = re.compile(
    r"^(?:例|解|证|注|习题|练习|作业|思考|小结|总结|引入|复习|图|表|式|步骤|方式|"
    r"第[一二三四五六七八九十百]+[章节讲部分]|[\d.]+[章节])"
)
# 「中文名 (ABC)」形式，用于自动生成缩写别名
ACRONYM_PAIR = re.compile(
    r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9\-]{1,24})\s*[（(]\s*([A-Za-z][A-Za-z0-9\-]{1,12})\s*[）)]"
)
ACRONYM_SUFFIX = re.compile(r"\s*[（(]\s*[A-Za-z][A-Za-z0-9\-]{1,12}\s*[）)]\s*$")

# NoteFlow 自己生成的固定小节，不算概念
SECTION_TITLES = frozenset({"关联概念", "思维导图", "相关笔记", "重点", "笔记"})

# 正文注入时整行跳过的前缀
SKIP_LINE_PREFIX = ("#", ">", "|", "```")
# 行内不允许改写的片段：公式、已有双链、行内代码
PROTECTED = re.compile(r"\$\$.+?\$\$|\$[^$\n]+?\$|\[\[[^\]]*?\]\]|`[^`]*`")


@dataclass(frozen=True)
class Concept:
    """一条概念：展示用 surface + 归一后的匹配键。"""

    surface: str
    key: str
    weight: int
    kind: str


@dataclass
class NoteRecord:
    """vault 里一篇笔记的可链接信息（只看 frontmatter 和标题层级，不读正文）。"""

    path: Path
    stem: str
    title: str
    course: str
    subject: str
    document: str
    page: int | None
    headings: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = ""


@dataclass(frozen=True)
class PlanEntry:
    """一条待注入的链接。"""

    surface: str
    key: str
    stem: str
    title: str
    score: int
    surfaces: tuple[str, ...] = ()  # 同一概念的所有写法，注入时按长度优先尝试


@dataclass
class AliasMap:
    """别名归一结果：匹配键映射 + 规范名下的其它写法（用于正文匹配）。"""

    keys: dict[str, str] = field(default_factory=dict)
    variants: dict[str, list[str]] = field(default_factory=dict)

    def canon(self, key: str) -> str:
        return self.keys.get(key, key)

    def surfaces(self, key: str) -> list[str]:
        return self.variants.get(key, [])


@dataclass
class LinkPlan:
    """一篇笔记的链接方案。"""

    entries: list[PlanEntry] = field(default_factory=list)
    heading_anchors: dict[str, str] = field(default_factory=dict)
    arrow_targets: dict[str, tuple[str, str]] = field(default_factory=dict)
    applied: list[PlanEntry] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def related_titles(self) -> list[str]:
        seen: list[str] = []
        for entry in self.applied:
            if entry.title not in seen:
                seen.append(entry.title)
        return seen

    def related_labels(self, current_title: str) -> list[str]:
        """思维导图里显示的关联笔记名；同名页补上文件名，避免看不出指向哪一篇。"""
        labels: list[str] = []
        for entry in self.applied:
            label = entry.title if entry.title != current_title else f"{entry.title}（{entry.stem}）"
            if label not in labels:
                labels.append(label)
        return labels


# ---------------------------------------------------------------------------
# 归一与过滤
# ---------------------------------------------------------------------------


def normalize_key(text: str) -> str:
    """把一个概念写法归成匹配键：全半角统一、去编号、去尾标点、ASCII 小写。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = ENUM_PREFIX.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = TRAILING_PUNCT.sub("", value)
    # 只对 ASCII 词做小写，避免动到中文
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0).lower(), value).strip()


def concept_keys(text: str) -> list[str]:
    """返回一个概念的等价匹配键：原样 + 去掉括注缩写后的主干。"""
    base = normalize_key(text)
    keys = [base] if base else []
    stripped = ACRONYM_SUFFIX.sub("", base).strip()
    if stripped and stripped != base and len(stripped) >= 2:
        keys.append(stripped)
    return keys


def is_concept(text: str, settings: Settings) -> bool:
    """判断一段文字能不能当概念：长度、题型标记、公式、泛词、P0 端点规则全部要过。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    key = normalize_key(raw)
    if len(key) < settings.concept_min_len or len(key) > settings.concept_max_len:
        return False
    if is_weak_heading(key):
        return False
    if not is_meaningful_endpoint(key):
        return False
    if SENTENCE_PUNCT.search(key):
        return False
    if any(char in key for char in FORMULA_CHARS):
        return False
    if key.isdigit():
        return False
    if key in SECTION_TITLES:
        return False
    if key in settings.concept_stopwords:
        return False
    if CHAPTER_HEAD.match(key):
        return False
    return True


# ---------------------------------------------------------------------------
# 概念归属：一篇笔记「拥有」哪些概念
# ---------------------------------------------------------------------------


def extract_owned_concepts(note: dict[str, Any], settings: Settings) -> list[Concept]:
    """从标题和各级 heading 提取这篇笔记所讲的概念。

    标题权重最高（整篇就是讲它），一级/二级标题次之，三级以下最低。
    """
    concepts: list[Concept] = []
    title = str(note.get("title") or "").strip()
    title_key = normalize_key(title)
    if is_concept(title, settings):
        concepts.append(Concept(title, title_key, 3, "title"))

    def walk(sections: Iterable[dict[str, Any]], depth: int) -> None:
        for section in sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            if heading and is_concept(heading, settings):
                key = normalize_key(heading)
                if key and key != title_key:
                    concepts.append(Concept(heading, key, 2 if depth <= 2 else 1, f"h{depth}"))
            walk(section.get("subsections") or [], depth + 1)

    walk(note.get("sections") or [], 1)

    # 同一个 key 只留权重最高、字数最短的写法
    best: dict[str, Concept] = {}
    for concept in concepts:
        current = best.get(concept.key)
        if current is None or concept.weight > current.weight or (
            concept.weight == current.weight and len(concept.surface) < len(current.surface)
        ):
            best[concept.key] = concept
    return list(best.values())


def record_concepts(record: NoteRecord, settings: Settings) -> list[Concept]:
    """从已归档笔记的 frontmatter + 标题层级提取概念（不读正文）。"""
    concepts: list[Concept] = []
    title_key = normalize_key(record.title)
    if is_concept(record.title, settings):
        concepts.append(Concept(record.title, title_key, 3, "title"))
    for heading in record.headings:
        if not is_concept(heading, settings):
            continue
        key = normalize_key(heading)
        if not key or key == title_key:
            continue
        concepts.append(Concept(heading, key, 2, "heading"))

    best: dict[str, Concept] = {}
    for concept in concepts:
        current = best.get(concept.key)
        if current is None or concept.weight > current.weight:
            best[concept.key] = concept
    return list(best.values())


def cached_record_concepts(record: NoteRecord, settings: Settings) -> list[Concept]:
    """record_concepts 的进程内缓存版本：按 (路径, 标题, 标题层级) 命中。"""
    signature = (record.title, tuple(record.headings))
    with _CONCEPT_CACHE_LOCK:
        hit = _CONCEPT_CACHE.get(record.path)
    if hit is not None and hit[0] == signature:
        return hit[1]

    concepts = record_concepts(record, settings)
    with _CONCEPT_CACHE_LOCK:
        _CONCEPT_CACHE[record.path] = (signature, concepts)
    return concepts


def clear_concept_cache() -> None:
    with _CONCEPT_CACHE_LOCK:
        _CONCEPT_CACHE.clear()


# ---------------------------------------------------------------------------
# 别名归一
# ---------------------------------------------------------------------------


def build_alias_map(records: list[NoteRecord], settings: Settings) -> AliasMap:
    """变体 key -> 规范 key。单跳解析，冲突的别名直接丢弃。"""
    proposals: dict[str, set[str]] = {}
    variants: dict[str, list[str]] = {}

    def propose(variant_key: str, canonical: str, variant_surface: str = "") -> None:
        if variant_key and variant_key != canonical:
            proposals.setdefault(variant_key, set()).add(canonical)
            if variant_surface:
                bucket = variants.setdefault(canonical, [])
                if variant_surface not in bucket:
                    bucket.append(variant_surface)

    for canonical_name, aliases in (settings.concept_aliases or {}).items():
        canonical_key = normalize_key(canonical_name)
        if not canonical_key:
            continue
        proposals.setdefault(canonical_key, set()).add(canonical_key)
        for variant in aliases:
            propose(normalize_key(variant), canonical_key, str(variant).strip())

    # 自动别名：「嵌入式微控制器 (MCU)」→ MCU 归到 嵌入式微控制器
    for record in records:
        for text in [record.title, *record.headings]:
            for chinese, acronym in ACRONYM_PAIR.findall(text):
                if len(acronym) < 2 or not is_concept(chinese, settings):
                    continue
                propose(normalize_key(acronym), normalize_key(chinese), acronym)

    alias: dict[str, str] = {}
    for variant_key, candidates in proposals.items():
        if len(candidates) > 1:
            logger.info("概念别名冲突，放弃归一: %s -> %s", variant_key, sorted(candidates))
            continue
        alias[variant_key] = next(iter(candidates))
    return AliasMap(alias, variants)


# ---------------------------------------------------------------------------
# 链接方案
# ---------------------------------------------------------------------------


def build_plan(
    note: dict[str, Any],
    meta: dict[str, Any],
    records: list[NoteRecord],
    settings: Settings,
    exclude_stem: str = "",
) -> LinkPlan:
    """为当前笔记计算链接方案：正文注入目标 + 箭头端点 + 页内锚点。"""
    plan = LinkPlan()

    # 本篇自己的 heading 永远走页内锚点，不做跨笔记链接
    def collect_anchors(sections: Iterable[dict[str, Any]]) -> None:
        for section in sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            if heading and is_concept(heading, settings):
                plan.heading_anchors.setdefault(normalize_key(heading), heading)
            collect_anchors(section.get("subsections") or [])

    collect_anchors(note.get("sections") or [])

    if not settings.link_enabled:
        return plan

    others = [record for record in records if record.stem != exclude_stem]
    total_notes = len(others) + 1  # 本篇 + vault 里其他笔记
    if total_notes < settings.link_min_notes:
        logger.info(
            "vault 笔记数 %d 少于冷启动阈值 %d，本次不生成跨笔记链接",
            total_notes,
            settings.link_min_notes,
        )
        return plan

    alias = build_alias_map(records, settings)
    course = str(meta.get("course") or UNCLASSIFIED)
    subject = str(meta.get("subject") or UNCLASSIFIED)
    own_keys = {alias.canon(concept.key) for concept in extract_owned_concepts(note, settings)}
    body_text = _note_body_text(note)

    # 目标概念 -> 拥有它的笔记
    owned: dict[str, list[tuple[NoteRecord, Concept]]] = {}
    for record in others:
        for concept in cached_record_concepts(record, settings):
            for key in concept_keys(concept.surface):
                owned.setdefault(alias.canon(key), []).append((record, concept))

    tags = {str(tag).strip() for tag in (note.get("tags") or []) if str(tag).strip()}
    candidates: list[PlanEntry] = []

    for key, owners in owned.items():
        if key in own_keys:
            continue  # 本篇自己就在讲这个概念，不自我链接
        entry, reason = _resolve_owner(key, owners, alias, settings, course, subject, tags)
        if entry is None:
            if reason.startswith("ambiguous"):
                # 同名概念出现在多个学科：无法判断指向，留空并记日志
                logger.info("链接歧义，留空: %s（%s）", key, reason)
                plan.skipped.append((key, reason))
            elif reason != "missing":
                plan.skipped.append((key, reason))
            continue
        # 链接必须落在正文里：找不到写法就不连，绝不凭空造链
        if not _present_in_body(entry, body_text):
            continue
        candidates.append(entry)

    candidates.sort(key=lambda item: (-item.score, -len(item.surface), item.stem))

    # 同一目标笔记只保留分最高的一条，避免一篇里连出三条指向同一页
    picked: list[PlanEntry] = []
    used_stems: set[str] = set()
    for entry in candidates:
        if entry.stem in used_stems:
            continue
        picked.append(entry)
        used_stems.add(entry.stem)
        if len(picked) >= settings.link_max_per_note:
            break

    plan.entries = picked
    # 箭头端点可能用别名写法（如 MCU），把别名也指向同一目标
    for entry in plan.entries:
        plan.arrow_targets[entry.key] = (entry.stem, entry.title)
        for variant_key, canonical in alias.keys.items():
            if canonical == entry.key:
                plan.arrow_targets.setdefault(variant_key, (entry.stem, entry.title))
    return plan


def _note_body_text(note: dict[str, Any]) -> str:
    """把正文内容拼成一段文本，用于判断概念是否真的在正文里出现。"""
    chunks: list[str] = []

    def walk(sections: Iterable[dict[str, Any]]) -> None:
        for section in sections:
            if not isinstance(section, dict):
                continue
            content = section.get("content")
            if content:
                chunks.append(str(content))
            walk(section.get("subsections") or [])

    walk(note.get("sections") or [])
    return "\n".join(chunks)


def _present_in_body(entry: PlanEntry, body_text: str) -> bool:
    return any(surface and surface in body_text for surface in entry.surfaces or (entry.surface,))


def _resolve_owner(
    key: str,
    owners: list[tuple[NoteRecord, Concept]],
    alias: AliasMap,
    settings: Settings,
    course: str,
    subject: str,
    tags: set[str],
) -> tuple[PlanEntry | None, str]:
    """在一组拥有者里选出唯一目标；跨学科同名视为歧义，留空。"""
    seen: dict[str, tuple[NoteRecord, Concept]] = {}
    for record, concept in owners:
        seen.setdefault(record.stem, (record, concept))
    unique = list(seen.values())
    if not unique:
        return None, "missing"

    subjects = {record.subject for record, _ in unique if record.subject and record.subject != UNCLASSIFIED}
    if len(subjects) > 1:
        return None, f"ambiguous:{'/'.join(sorted(subjects))}"

    unique.sort(key=lambda item: (item[0].page or 0, item[0].stem))
    record, concept = unique[0]

    same_course = bool(record.course) and record.course == course
    same_subject = bool(record.subject) and record.subject == subject
    shared_tags = tags & set(record.tags)

    # 跨域不连：既不同课、又不同学科、也没有共同标签，说明上下文毫无关系
    if not (same_course or same_subject or shared_tags):
        return None, "cross_domain"

    score = concept.weight
    if same_course:
        score += 2
    elif same_subject:
        score += 1
    if shared_tags:
        score += 1

    if score < settings.link_min_score:
        return None, f"low_score:{score}"

    surfaces = [concept.surface, *alias.surfaces(key)]
    surfaces = list(dict.fromkeys(surface for surface in surfaces if surface))
    return PlanEntry(concept.surface, key, record.stem, record.title, score, tuple(surfaces)), "ok"


# ---------------------------------------------------------------------------
# 正文注入
# ---------------------------------------------------------------------------


def _protected_spans(line: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in PROTECTED.finditer(line)]


def _is_protected(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and span_start < end for span_start, span_end in spans)


def _replace_first(line: str, entry: PlanEntry) -> str:
    """在非公式、非已有链接的位置替换首次出现的概念（含别名写法）。"""
    surfaces = sorted(entry.surfaces or (entry.surface,), key=len, reverse=True)
    spans = _protected_spans(line)

    for surface in surfaces:
        if not surface:
            continue
        if surface.isascii():
            pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(surface) + r"(?![A-Za-z0-9])", re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(surface))

        for match in pattern.finditer(line):
            if _is_protected(match.start(), match.end(), spans):
                continue
            token = f"[[{entry.stem}]]" if normalize_key(surface) == normalize_key(entry.stem) else f"[[{entry.stem}|{surface}]]"
            return line[: match.start()] + token + line[match.end() :]
    return line


def apply_inline_links(markdown_text: str, plan: LinkPlan) -> str:
    """把链接注入正文首次出现处；返回注入后的全文。

    只动正文段落：标题、引用、表格、代码块、公式一律不碰，
    每个概念整篇只注入一次。
    """
    if not plan.entries:
        return markdown_text

    lines = markdown_text.split("\n")
    start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                start = index + 1
                break

    used: set[str] = set()
    applied: list[PlanEntry] = []
    in_code = False

    for index in range(start, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped:
            continue
        if stripped.startswith(SKIP_LINE_PREFIX):
            continue

        for entry in plan.entries:
            if entry.key in used:
                continue
            updated = _replace_first(line, entry)
            if updated != line:
                line = updated
                used.add(entry.key)
                applied.append(entry)
        lines[index] = line

    plan.applied = applied
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 独立运行：打印某目录下每篇笔记的链接方案
# ---------------------------------------------------------------------------


def main() -> int:
    from config import setup_logging
    from src.organizer import collect_note_records, parse_markdown_note

    settings = Settings.from_env()
    setup_logging(settings.log_level)

    vault = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.vault_dir
    records = collect_note_records(vault)
    for record in records:
        note, meta = parse_markdown_note(record.path)
        plan = build_plan(note, meta, records, settings, exclude_stem=record.stem)
        print(f"{record.stem}: {len(plan.entries)} 个候选")
        for entry in plan.entries:
            print(f"  - {entry.surface} -> {entry.stem}（score {entry.score}）")
        for key, reason in plan.skipped:
            print(f"  ! {key}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
