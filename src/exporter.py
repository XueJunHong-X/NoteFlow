"""导出：只读 vault 产物，生成单篇 Markdown、整库 zip、单文件 HTML 脑图。

这里不参与识别与归档：默认处理结果仍由 organizer 写进笔记库。
导出只是把已有产物复制或重新组织成用户指定的位置。

可独立运行（生成 HTML 脑图，便于快速检查）：
    python -m src.exporter --html output/mindmap.html
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from config import UNCLASSIFIED
from src import organizer

logger = logging.getLogger(__name__)

HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
WIKILINK = re.compile(
    r"\[\[(?:(?P<stem>[^\]|#]+))?(?:#(?P<anchor>[^\]|]+))?(?:\|(?P<alias>[^\]]+))?\]\]"
)
MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
ARROW_LINE = re.compile(r"^- (.+?)\s*→\s*(.+?)\s*(?:[（(]([^（）()]*)[）)])?\s*$")
SPECIAL_SECTIONS = {"思维导图", "关联概念"}

HTML_NAME = "NoteFlow-脑图.html"


@dataclass
class NoteSummary:
    """笔记列表项：只含选择与展示需要的信息。"""

    path: Path
    relative: str
    stem: str
    title: str
    course: str
    subject: str
    tags: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    page: int | None = None
    source: str = ""
    mtime: float = 0.0


# ---------------------------------------------------------------------------
# 读取笔记清单
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """统一用 organizer 的长路径处理，避免深目录在 Windows 上读不到。"""
    return organizer._io_path(path).read_text(encoding="utf-8")


def list_notes(vault_dir: Path) -> list[NoteSummary]:
    """列出 vault 中的可导出笔记，按最近修改时间倒序。"""
    vault = Path(vault_dir)
    if not vault.is_dir():
        return []

    summaries: list[NoteSummary] = []
    for record in organizer.collect_note_records(vault):
        try:
            mtime = record.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        try:
            relative = record.path.relative_to(vault).as_posix()
        except ValueError:
            relative = record.path.name
        summaries.append(
            NoteSummary(
                path=record.path,
                relative=relative,
                stem=record.stem,
                title=record.title or record.stem,
                course=record.course or UNCLASSIFIED,
                subject=record.subject or UNCLASSIFIED,
                tags=list(record.tags),
                headings=list(record.headings),
                page=record.page,
                source=record.source,
                mtime=mtime,
            )
        )
    summaries.sort(key=lambda item: (-item.mtime, item.course, item.title))
    return summaries


def latest_note(vault_dir: Path) -> Path | None:
    notes = list_notes(vault_dir)
    return notes[0].path if notes else None


# ---------------------------------------------------------------------------
# 单篇 Markdown
# ---------------------------------------------------------------------------


def export_markdown(note_path: Path, target_file: Path, overwrite: bool = True) -> Path:
    """把一篇已归档笔记原样写成独立 .md 文件。"""
    source = Path(note_path)
    if not source.is_file():
        raise FileNotFoundError(f"笔记不存在：{source}")

    target = Path(target_file)
    if target.suffix.lower() != ".md":
        target = target.with_suffix(".md")
    if target.exists() and not overwrite:
        target = _unique_path(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_read_text(source), encoding="utf-8")
    except OSError as exc:
        raise OSError(f"写入失败：{target}（{exc}）") from exc
    logger.info("已导出 Markdown: %s -> %s", source, target)
    return target


def export_markdowns(note_paths: list[Path], target_dir: Path) -> list[Path]:
    """把多篇笔记导出到同一个文件夹，同名文件自动加序号。"""
    folder = Path(target_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"无法创建导出目录：{folder}（{exc}）") from exc

    outputs: list[Path] = []
    for path in note_paths:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"笔记不存在：{source}")
        outputs.append(export_markdown(source, folder / f"{source.stem}.md", overwrite=False))
    return outputs


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


# ---------------------------------------------------------------------------
# 整库 zip
# ---------------------------------------------------------------------------


def export_vault_zip(vault_dir: Path, target_zip: Path) -> tuple[Path, int, int]:
    """把整个笔记库打包成 zip，返回 (文件路径, 文件数, 原始字节数)。"""
    vault = Path(vault_dir)
    if not vault.is_dir():
        raise FileNotFoundError(f"笔记库不存在：{vault}")

    target = Path(target_zip)
    if target.suffix.lower() != ".zip":
        target = target.with_suffix(".zip")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"无法创建导出目录：{target.parent}（{exc}）") from exc

    resolved_target = target.resolve()
    count = 0
    total_bytes = 0
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as archive:
            for path in sorted(vault.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    if path.resolve() == resolved_target:
                        continue
                except OSError:
                    pass
                arcname = path.relative_to(vault).as_posix()
                archive.write(path, arcname)
                count += 1
                try:
                    total_bytes += path.stat().st_size
                except OSError:
                    pass
    except OSError as exc:
        raise OSError(f"打包失败：{exc}") from exc

    if count == 0:
        try:
            target.unlink()
        except OSError:
            pass
        raise ValueError("笔记库中还没有可打包的文件。")

    logger.info("已导出整库 zip: %s（%d 个文件，%.1f MB）", target, count, total_bytes / 1024 / 1024)
    return target, count, total_bytes


# ---------------------------------------------------------------------------
# 剪贴板
# ---------------------------------------------------------------------------


def set_clipboard(widget, text: str) -> None:
    """把文本放进剪贴板；widget 需要是已存在的 Tk 组件。"""
    widget.clipboard_clear()
    widget.clipboard_append(text)
    try:
        widget.update()
    except Exception:
        pass


def latest_text(vault_dir: Path, preferred: Path | None = None) -> tuple[Path, str]:
    """取「最新一篇」的完整 Markdown：优先本轮刚处理的那篇。"""
    candidate = Path(preferred) if preferred else None
    if candidate is not None and candidate.is_file():
        return candidate, _read_text(candidate)
    newest = latest_note(vault_dir)
    if newest is None:
        raise FileNotFoundError("笔记库中还没有可复制的笔记。")
    return newest, _read_text(newest)


# ---------------------------------------------------------------------------
# HTML 脑图
# ---------------------------------------------------------------------------


def _clean_endpoint(text: str) -> str:
    """箭头端点去掉双链标记，保留展示文字。"""

    def repl(match: re.Match[str]) -> str:
        return (match.group("alias") or match.group("stem") or match.group("anchor") or "").strip()

    return re.sub(r"\s+", " ", WIKILINK.sub(repl, str(text or ""))).strip()


def _extract_headings(body: str) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for match in HEADING_LINE.finditer(body):
        text = match.group(2).strip()
        if text in SPECIAL_SECTIONS:
            continue
        headings.append({"level": len(match.group(1)), "text": text})
    return headings


def _extract_mindmap(body: str) -> list[dict[str, Any]]:
    """从 Mermaid mindmap 代码块取层级，作为脑图数据的一部分。"""
    match = MERMAID_BLOCK.search(body)
    if not match:
        return []

    rows: list[dict[str, Any]] = []
    for raw in match.group(1).splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        label = raw.strip()
        if label == "mindmap":
            continue
        if label.startswith("root(") or label.startswith("root(("):
            label = re.sub(r"^root\(\((.*)\)\)$", r"\1", label)
            label = re.sub(r"^root\((.*)\)$", r"\1", label)
        rows.append({"depth": max(indent // 2, 0), "text": _clean_endpoint(label)})
    return rows


def _extract_arrows(body: str) -> list[dict[str, str]]:
    """读取「关联概念」小节里的 A → B（关系）。"""
    arrows: list[dict[str, str]] = []
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped in ("## 关联概念", "# 关联概念"):
            in_section = True
            continue
        if in_section and stripped.startswith("#"):
            in_section = False
        if not in_section:
            continue
        match = ARROW_LINE.match(stripped)
        if not match:
            continue
        source = _clean_endpoint(match.group(1))
        target = _clean_endpoint(match.group(2))
        relation = (match.group(3) or "").strip()
        if source and target:
            arrows.append({"from": source, "to": target, "relation": relation})
    return arrows


def _extract_links(body: str) -> list[str]:
    stems: list[str] = []
    for match in WIKILINK.finditer(body):
        stem = (match.group("stem") or "").strip()
        if stem and stem not in stems:
            stems.append(stem)
    return stems


def _preview(body: str, limit: int = 480) -> str:
    """正文预览：去掉代码块与 Markdown 标记，避免网页里出现原始符号堆。"""
    text = MERMAID_BLOCK.sub("", body)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = WIKILINK.sub(lambda m: (m.group("alias") or m.group("stem") or ""), text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _resolve_note(text: str, by_title: dict[str, str], by_stem: dict[str, str]) -> str | None:
    cleaned = _clean_endpoint(text)
    if not cleaned:
        return None
    if cleaned in by_title:
        return by_title[cleaned]
    if cleaned in by_stem:
        return by_stem[cleaned]
    # 箭头端点常带「（课程-p004）」这类后缀，按前缀再匹配一次
    for title, note_id in by_title.items():
        if title and (cleaned.startswith(title) or title.startswith(cleaned)):
            return note_id
    return None


def collect_mindmap_data(vault_dir: Path) -> dict[str, Any]:
    """收集 HTML 脑图需要的数据：笔记、层级、Mermaid、关系。"""
    vault = Path(vault_dir)
    summaries = list_notes(vault)
    by_title: dict[str, str] = {}
    by_stem: dict[str, str] = {}
    for item in summaries:
        note_id = item.relative
        by_title.setdefault(item.title, note_id)
        by_stem.setdefault(item.stem, note_id)

    notes: list[dict[str, Any]] = []
    for item in summaries:
        try:
            text = _read_text(item.path)
        except OSError as exc:
            logger.warning("读取笔记失败，已跳过 %s: %s", item.path, exc)
            continue
        _meta, body = organizer.split_frontmatter(text)
        notes.append(
            {
                "id": item.relative,
                "stem": item.stem,
                "title": item.title,
                "course": item.course,
                "subject": item.subject,
                "tags": item.tags,
                "source": item.source,
                "page": item.page,
                "headings": _extract_headings(body),
                "mindmap": _extract_mindmap(body),
                "arrows": _extract_arrows(body),
                "links": _extract_links(body),
                "preview": _preview(body),
            }
        )

    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_edge(source_id: str, target_id: str | None, kind: str, label: str) -> None:
        if not target_id or source_id == target_id:
            return
        key = (source_id, target_id, kind)
        if key in seen:
            return
        seen.add(key)
        edges.append({"source": source_id, "target": target_id, "kind": kind, "label": label})

    for note in notes:
        for stem in note["links"]:
            add_edge(note["id"], by_stem.get(stem), "link", "提及")
        for arrow in note["arrows"]:
            target_id = _resolve_note(arrow["to"], by_title, by_stem)
            add_edge(note["id"], target_id, "arrow", arrow["relation"] or "关联")

    courses = sorted({note["course"] for note in notes})
    tags = sorted({tag for note in notes for tag in note["tags"]})
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vault_name": vault.name or "笔记库",
        "notes": notes,
        "edges": edges,
        "stats": {"notes": len(notes), "courses": len(courses), "tags": len(tags)},
    }


def build_mindmap_html(vault_dir: Path) -> str:
    """生成单文件 HTML：无外部资源，双击即可在任意浏览器打开。"""
    data = collect_mindmap_data(vault_dir)
    payload = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__TITLE__", html.escape(f"NoteFlow 脑图 · {data['vault_name']}")).replace(
        "__DATA__", payload
    )


def export_mindmap_html(vault_dir: Path, target_file: Path) -> Path:
    target = Path(target_file)
    if target.suffix.lower() not in (".html", ".htm"):
        target = target.with_suffix(".html")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(build_mindmap_html(vault_dir), encoding="utf-8")
    except OSError as exc:
        raise OSError(f"写入失败：{target}（{exc}）") from exc
    logger.info("已导出 HTML 脑图: %s", target)
    return target


# ---------------------------------------------------------------------------
# 页面模板
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#F5F7FB; --card:#FFFFFF; --border:#E6E9F2; --ink:#101828; --ink-soft:#344054;
    --muted:#667085; --muted-light:#98A2B3; --accent:#2F6BFF; --accent-soft:#EAF0FF;
    --success:#12B76A; --warn:#D97706;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei UI","PingFang SC","Helvetica Neue",Arial,sans-serif;font-size:14px}
  header.top{display:flex;align-items:center;gap:18px;padding:16px 24px;background:var(--card);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10}
  .brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;white-space:nowrap}
  .logo{width:32px;height:32px;border-radius:9px;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700}
  .brand small{display:block;font-weight:400;font-size:11px;color:var(--muted)}
  .stats{display:flex;gap:8px;flex-wrap:wrap}
  .stat{background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:4px 11px;font-size:12px;white-space:nowrap}
  .search{margin-left:auto;width:280px;max-width:42vw;padding:9px 12px;border:1px solid var(--border);border-radius:9px;font-size:13px;outline:none;background:#FAFBFE;color:var(--ink)}
  .search:focus{border-color:var(--accent)}
  main{display:flex;gap:16px;padding:16px 20px 24px;min-height:calc(100vh - 76px);align-items:flex-start}
  aside.side{width:320px;flex:0 0 320px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px;max-height:calc(100vh - 108px);overflow:auto;position:sticky;top:92px}
  .side-head{display:flex;align-items:center;justify-content:space-between;font-weight:700;font-size:14px;margin-bottom:10px}
  .side-head span{font-weight:400;font-size:12px;color:var(--muted)}
  .group{margin-bottom:12px}
  .group-head{display:flex;align-items:center;gap:7px;padding:5px 4px;color:var(--ink-soft);font-weight:700;font-size:12px}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--accent);flex:0 0 auto}
  .group-count{margin-left:auto;color:var(--muted-light);font-weight:400;font-size:11px}
  .note-item{display:block;width:100%;text-align:left;border:0;background:transparent;border-radius:8px;padding:7px 9px;cursor:pointer;color:var(--ink)}
  .note-item:hover{background:#F2F5FD}
  .note-item.active{background:var(--accent-soft)}
  .note-title{display:block;font-size:13px;line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .note-meta{display:block;color:var(--muted-light);font-size:11px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  section.panel{flex:1;min-width:0;background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden}
  .tabs{display:flex;gap:4px;padding:10px 14px 0;border-bottom:1px solid var(--border)}
  .tabs button{border:0;background:transparent;padding:9px 14px;border-radius:8px 8px 0 0;color:var(--muted);cursor:pointer;font-size:13px;font-family:inherit}
  .tabs button:hover{background:#F2F5FD;color:var(--ink)}
  .tabs button.active{background:var(--accent-soft);color:var(--accent);font-weight:700}
  .tab-body{padding:20px 22px 28px}
  .hidden{display:none!important}
  .detail-title{margin:0 0 10px;font-size:23px;line-height:1.35}
  .badges{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
  .badge{background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:3px 10px;font-size:12px}
  .tag{background:#F2F4F7;color:var(--muted);border-radius:999px;padding:3px 9px;font-size:12px}
  .detail-source{color:var(--muted-light);font-size:12px;margin-bottom:18px;word-break:break-all}
  h2.sec{font-size:15px;margin:20px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--border)}
  .outline{list-style:none;margin:0;padding:0}
  .outline li{position:relative;padding:4px 0 4px 0;font-size:13px;color:var(--ink-soft);line-height:1.5}
  .outline .mark{display:inline-block;width:6px;height:6px;border-radius:2px;background:var(--accent);margin-right:8px;vertical-align:2px;opacity:.65}
  .mm-row{padding:3px 0;font-size:13px;color:var(--ink-soft);line-height:1.5;white-space:pre-wrap;word-break:break-word}
  .mm-row .bullet{display:inline-block;width:14px;color:var(--muted-light)}
  .rel{display:flex;gap:8px;align-items:baseline;padding:5px 0;font-size:13px;color:var(--ink-soft);flex-wrap:wrap}
  .rel .arrow{color:var(--accent);font-weight:700}
  .rel .label{color:var(--muted-light);font-size:11px}
  .link-chip{display:inline-block;margin:3px 6px 3px 0;padding:3px 9px;background:#F2F5FD;border-radius:6px;color:var(--accent);font-size:12px;cursor:pointer}
  .link-chip:hover{background:#E4ECFF}
  pre.preview{margin:0;padding:14px;background:#FAFBFE;border:1px solid var(--border);border-radius:9px;white-space:pre-wrap;word-break:break-word;color:var(--ink-soft);font-family:inherit;font-size:13px;line-height:1.7;max-height:360px;overflow:auto}
  .empty{color:var(--muted-light);padding:30px 6px;text-align:center}
  .muted{color:var(--muted-light);font-size:12px}
  #graph{overflow:auto}
  #graph-svg{display:block}
  .edge{fill:none;stroke:#C7D2E8;stroke-width:1.4}
  .edge.arrow{stroke:#8FB0FF;stroke-dasharray:5 4}
  .node{cursor:pointer}
  .node rect{fill:#F7F9FE;stroke:#D5DBEA;rx:8}
  .node:hover rect{stroke:var(--accent);fill:#EFF4FF}
  .node .node-title{font-size:12px;fill:var(--ink);font-family:inherit}
  .node .node-sub{font-size:10px;fill:var(--muted-light);font-family:inherit}
  .graph-course{font-size:12px;fill:var(--muted);font-weight:700;font-family:inherit}
  .legend{display:flex;gap:16px;color:var(--muted);font-size:12px;margin-bottom:10px}
  .legend i{display:inline-block;width:18px;height:0;border-top:2px solid #C7D2E8;vertical-align:3px;margin-right:5px}
  .legend i.dash{border-top-style:dashed;border-color:#8FB0FF}
  .tree-all{margin:0;padding-left:18px}
  .tree-all li{margin:6px 0;line-height:1.5}
  .tree-all .course-name{font-weight:700}
  .tree-all .note-link{color:var(--accent);cursor:pointer}
  .tree-all .note-link:hover{text-decoration:underline}
  .tree-all .head{color:var(--muted);font-size:12px}
  @media (max-width:900px){
    main{flex-direction:column;padding:12px}
    aside.side{width:100%;flex:none;position:static;max-height:300px}
    .search{width:180px}
    header.top{flex-wrap:wrap}
  }
</style>
</head>
<body>
<header class="top">
  <div class="brand"><div class="logo">N</div><div>NoteFlow 笔记脑图<small id="vault-name"></small></div></div>
  <div class="stats" id="stats"></div>
  <input id="search" class="search" type="search" placeholder="搜索标题、课程、标签…">
</header>
<main>
  <aside class="side">
    <div class="side-head">笔记库 <span id="count"></span></div>
    <div id="tree"></div>
  </aside>
  <section class="panel">
    <nav class="tabs">
      <button type="button" data-tab="detail" class="active">笔记详情</button>
      <button type="button" data-tab="graph">关系图</button>
      <button type="button" data-tab="outline">全库大纲</button>
    </nav>
    <div id="detail" class="tab-body"></div>
    <div id="graph" class="tab-body hidden">
      <div class="legend"><span><i></i>笔记引用</span><span><i class="dash"></i>关联概念</span></div>
      <svg id="graph-svg" xmlns="http://www.w3.org/2000/svg"></svg>
      <div id="graph-empty" class="empty hidden">暂无可视化的笔记间关系。</div>
    </div>
    <div id="outline" class="tab-body hidden"></div>
  </section>
</main>
<script>
const DATA = __DATA__;
const notes = DATA.notes || [];
const byId = {};
notes.forEach(function(n){ byId[n.id] = n; });
let currentId = notes.length ? notes[0].id : null;
let keyword = "";
let activeTab = "detail";

function el(tag, cls, text){
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function svgEl(tag, attrs){
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.keys(attrs || {}).forEach(function(k){ node.setAttribute(k, attrs[k]); });
  return node;
}
function matches(n){
  if (!keyword) return true;
  const hay = [n.title, n.course, n.subject, n.source]
    .concat(n.tags || [])
    .concat((n.headings || []).map(function(h){ return h.text; }))
    .join(" ").toLowerCase();
  return hay.indexOf(keyword) >= 0;
}
function sortedCourses(items){
  return Array.from(new Set(items.map(function(n){ return n.course; }))).sort(function(a, b){
    if (a === "未分类") return 1;
    if (b === "未分类") return -1;
    return a.localeCompare(b, "zh");
  });
}
function renderStats(){
  const box = document.getElementById("stats");
  box.innerHTML = "";
  const stats = DATA.stats || {};
  [["笔记", stats.notes], ["课程", stats.courses], ["标签", stats.tags]].forEach(function(pair){
    box.appendChild(el("span", "stat", pair[0] + " " + (pair[1] || 0)));
  });
  document.getElementById("vault-name").textContent = "生成于 " + (DATA.generated_at || "").replace("T", " ");
}
function renderTree(){
  const box = document.getElementById("tree");
  box.innerHTML = "";
  const visible = notes.filter(matches);
  const groups = {};
  visible.forEach(function(n){ (groups[n.course] = groups[n.course] || []).push(n); });
  sortedCourses(visible).forEach(function(course){
    const group = el("div", "group");
    const head = el("div", "group-head");
    head.appendChild(el("span", "dot"));
    head.appendChild(el("span", "group-name", course));
    head.appendChild(el("span", "group-count", String(groups[course].length)));
    group.appendChild(head);
    groups[course].forEach(function(n){
      const item = el("button", "note-item" + (n.id === currentId ? " active" : ""));
      item.type = "button";
      item.appendChild(el("span", "note-title", n.title));
      const sub = [n.page ? ("第 " + n.page + " 页") : "", n.tags && n.tags.length ? n.tags.slice(0, 2).join(" / ") : ""]
        .filter(Boolean).join(" · ");
      item.appendChild(el("span", "note-meta", sub || n.source || ""));
      item.onclick = function(){ currentId = n.id; renderTree(); renderDetail(); showTab("detail"); };
      group.appendChild(item);
    });
    box.appendChild(group);
  });
  document.getElementById("count").textContent = visible.length + " / " + notes.length + " 篇";
}
function sectionTitle(text){
  return el("h2", "sec", text);
}
function renderDetail(){
  const box = document.getElementById("detail");
  box.innerHTML = "";
  const n = byId[currentId];
  if (!n){
    box.appendChild(el("div", "empty", "笔记库中还没有可导出的笔记。"));
    return;
  }
  const head = el("div", "detail-head");
  head.appendChild(el("h1", "detail-title", n.title));
  const badges = el("div", "badges");
  [n.course, n.subject].filter(Boolean).forEach(function(t){ badges.appendChild(el("span", "badge", t)); });
  (n.tags || []).forEach(function(t){ badges.appendChild(el("span", "tag", "#" + t)); });
  head.appendChild(badges);
  head.appendChild(el("div", "detail-source", [n.source, n.page ? ("第 " + n.page + " 页") : "", DATA.vault_name + "/" + n.id].filter(Boolean).join(" · ")));
  box.appendChild(head);

  box.appendChild(sectionTitle("层级大纲"));
  if (!n.headings || !n.headings.length){
    box.appendChild(el("div", "muted", "这篇笔记没有分节标题。"));
  } else {
    const list = el("ul", "outline");
    n.headings.forEach(function(h){
      const li = el("li");
      li.style.paddingLeft = Math.max(0, (h.level - 1) * 18) + "px";
      li.appendChild(el("span", "mark"));
      li.appendChild(document.createTextNode(h.text));
      list.appendChild(li);
    });
    box.appendChild(list);
  }

  box.appendChild(sectionTitle("思维导图"));
  if (!n.mindmap || !n.mindmap.length){
    box.appendChild(el("div", "muted", "这篇笔记没有内嵌 Mermaid 思维导图。"));
  } else {
    const wrap = el("div");
    n.mindmap.forEach(function(row){
      const line = el("div", "mm-row");
      line.style.paddingLeft = (row.depth * 18) + "px";
      line.appendChild(el("span", "bullet", "•"));
      line.appendChild(document.createTextNode(row.text));
      wrap.appendChild(line);
    });
    box.appendChild(wrap);
  }

  box.appendChild(sectionTitle("关联概念"));
  if (!n.arrows || !n.arrows.length){
    box.appendChild(el("div", "muted", "没有记录箭头关系。"));
  } else {
    n.arrows.forEach(function(a){
      const row = el("div", "rel");
      row.appendChild(el("span", "", a.from));
      row.appendChild(el("span", "arrow", "→"));
      row.appendChild(el("span", "", a.to));
      if (a.relation) row.appendChild(el("span", "label", "（" + a.relation + "）"));
      box.appendChild(row);
    });
  }

  box.appendChild(sectionTitle("关联笔记"));
  const linked = [];
  (n.links || []).forEach(function(stem){
    notes.forEach(function(other){
      if (other.stem === stem && other.id !== n.id && linked.indexOf(other) < 0) linked.push(other);
    });
  });
  if (!linked.length){
    box.appendChild(el("div", "muted", "没有跨笔记双链。"));
  } else {
    linked.forEach(function(other){
      const chip = el("span", "link-chip", other.title);
      chip.onclick = function(){ currentId = other.id; renderTree(); renderDetail(); showTab("detail"); };
      box.appendChild(chip);
    });
  }

  box.appendChild(sectionTitle("正文预览"));
  box.appendChild(el("pre", "preview", n.preview || "（正文为空）"));
}
function renderGraph(){
  const svg = document.getElementById("graph-svg");
  const empty = document.getElementById("graph-empty");
  svg.innerHTML = "";
  const edges = (DATA.edges || []).filter(function(e){ return byId[e.source] && byId[e.target]; });
  if (!notes.length || !edges.length){
    empty.classList.remove("hidden");
    svg.setAttribute("width", "0");
    svg.setAttribute("height", "0");
    return;
  }
  empty.classList.add("hidden");

  const courses = sortedCourses(notes);
  const columns = courses.map(function(course){
    return { course: course, notes: notes.filter(function(n){ return n.course === course; }) };
  });
  const padX = 40, padTop = 56, nodeW = 200, nodeH = 38, gapY = 12, colW = 268;
  const pos = {};
  let maxRows = 1;
  columns.forEach(function(col, ci){
    maxRows = Math.max(maxRows, col.notes.length);
    col.notes.forEach(function(n, ri){
      pos[n.id] = { x: padX + ci * colW, y: padTop + ri * (nodeH + gapY) };
    });
  });
  const width = padX * 2 + columns.length * colW;
  const height = padTop + maxRows * (nodeH + gapY) + 24;
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);

  const defs = svgEl("defs");
  defs.innerHTML = '<marker id="arrow-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8FB0FF"></path></marker>';
  svg.appendChild(defs);

  columns.forEach(function(col, ci){
    const label = svgEl("text", { x: padX + ci * colW, y: 28, "class": "graph-course" });
    label.textContent = col.course + " · " + col.notes.length + " 篇";
    svg.appendChild(label);
  });

  edges.forEach(function(e){
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b) return;
    let d;
    if (Math.abs(a.x - b.x) < 1){
      // 同课程内部的连线沿右侧绕行，避免穿过节点形成大回环
      const sx = a.x + nodeW, sy = a.y + nodeH / 2;
      const tx = b.x + nodeW + 2, ty = b.y + nodeH / 2;
      const bend = sx + 30;
      d = "M " + sx + " " + sy + " C " + bend + " " + sy + ", " + bend + " " + ty + ", " + tx + " " + ty;
    } else {
      const x1 = a.x + nodeW, y1 = a.y + nodeH / 2;
      const x2 = b.x - 2, y2 = b.y + nodeH / 2;
      const curve = Math.max(36, Math.abs(x2 - x1) / 2);
      d = "M " + x1 + " " + y1 + " C " + (x1 + curve) + " " + y1 + ", " + (x2 - curve) + " " + y2 + ", " + x2 + " " + y2;
    }
    const path = svgEl("path", {
      d: d,
      "class": "edge" + (e.kind === "arrow" ? " arrow" : ""),
      "marker-end": "url(#arrow-head)"
    });
    const title = svgEl("title");
    title.textContent = (byId[e.source].title + " → " + byId[e.target].title + (e.label ? "（" + e.label + "）" : ""));
    path.appendChild(title);
    svg.appendChild(path);
  });

  notes.forEach(function(n){
    const p = pos[n.id];
    if (!p) return;
    const group = svgEl("g", { "class": "node", transform: "translate(" + p.x + "," + p.y + ")" });
    group.appendChild(svgEl("rect", { width: nodeW, height: nodeH, rx: 9 }));
    const title = svgEl("text", { x: 10, y: 16, "class": "node-title" });
    const text = n.title.length > 15 ? n.title.slice(0, 15) + "…" : n.title;
    title.textContent = text;
    group.appendChild(title);
    const sub = svgEl("text", { x: 10, y: 30, "class": "node-sub" });
    sub.textContent = (n.tags && n.tags.length ? n.tags[0] : n.course) || "";
    group.appendChild(sub);
    group.style.cursor = "pointer";
    group.addEventListener("click", function(){
      currentId = n.id; renderTree(); renderDetail(); showTab("detail");
    });
    const titleNode = svgEl("title");
    titleNode.textContent = n.title + " · " + n.course;
    group.appendChild(titleNode);
    svg.appendChild(group);
  });
}
function renderOutline(){
  const box = document.getElementById("outline");
  box.innerHTML = "";
  if (!notes.length){
    box.appendChild(el("div", "empty", "笔记库中还没有笔记。"));
    return;
  }
  const list = el("ul", "tree-all");
  sortedCourses(notes).forEach(function(course){
    const courseLi = el("li");
    courseLi.appendChild(el("span", "course-name", course));
    const noteList = el("ul", "tree-all");
    notes.filter(function(n){ return n.course === course; }).forEach(function(n){
      const noteLi = el("li");
      const link = el("span", "note-link", n.title);
      link.onclick = function(){ currentId = n.id; renderTree(); renderDetail(); showTab("detail"); };
      noteLi.appendChild(link);
      if (n.headings && n.headings.length){
        const heads = el("ul", "tree-all");
        n.headings.forEach(function(h){
          const hLi = el("li", "head", h.text);
          hLi.style.marginLeft = Math.max(0, (h.level - 1) * 14) + "px";
          heads.appendChild(hLi);
        });
        noteLi.appendChild(heads);
      }
      noteList.appendChild(noteLi);
    });
    courseLi.appendChild(noteList);
    list.appendChild(courseLi);
  });
  box.appendChild(list);
}
function showTab(name){
  activeTab = name;
  document.querySelectorAll(".tabs button").forEach(function(btn){
    btn.classList.toggle("active", btn.getAttribute("data-tab") === name);
  });
  document.getElementById("detail").classList.toggle("hidden", name !== "detail");
  document.getElementById("graph").classList.toggle("hidden", name !== "graph");
  document.getElementById("outline").classList.toggle("hidden", name !== "outline");
  if (name === "graph") renderGraph();
  if (name === "outline") renderOutline();
}
document.querySelectorAll(".tabs button").forEach(function(btn){
  btn.addEventListener("click", function(){ showTab(btn.getAttribute("data-tab")); });
});
document.getElementById("search").addEventListener("input", function(event){
  keyword = (event.target.value || "").trim().toLowerCase();
  renderTree();
});
renderStats();
renderTree();
renderDetail();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="NoteFlow 导出工具（可独立运行）")
    parser.add_argument("--vault", type=Path, default=Path("vault"), help="笔记库目录")
    parser.add_argument("--html", type=Path, help="生成单文件 HTML 脑图到指定路径")
    parser.add_argument("--zip", type=Path, help="把整个笔记库打包到指定 zip")
    parser.add_argument("--md", type=Path, help="导出最新一篇 Markdown 到指定文件")
    parser.add_argument("--list", action="store_true", help="只列出笔记清单")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    if args.list:
        for item in list_notes(args.vault):
            page = f" 第 {item.page} 页" if item.page else ""
            print(f"{item.title} · {item.course}{page} -> {item.path}")
        return 0
    if args.html:
        print(export_mindmap_html(args.vault, args.html))
        return 0
    if args.zip:
        path, count, size = export_vault_zip(args.vault, args.zip)
        print(f"{path}（{count} 个文件，{size / 1024 / 1024:.1f} MB）")
        return 0
    if args.md:
        source = latest_note(args.vault)
        if source is None:
            print("笔记库中还没有笔记。")
            return 1
        print(export_markdown(source, args.md))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
