"""NoteFlow 入口：监听文件夹，串联「拆页 → 预处理/取文字 → 识别 → 归档 → 索引/导图」。

支持输入：图片（png/jpg/bmp/webp）、PDF、GoodNotes 的 .goodnotes 容器。
PDF 有文字层时优先整理文字，扫描件/图表页才走整页视觉识别。

用法：
    # 持续监听（默认监听 ./inbox，输出到 ./output，笔记归档到 ./output/vault）
    python main.py

    # 处理一次监听目录里已有的文件后退出
    python main.py --once

    # 没有 API Key 时，离线验证整条链路（内置示例识别结果）
    python main.py --once --watch-dir tests/sample_images --mock

    # 只验证预处理 / 只跑前几页（控制成本）
    python main.py --once --watch-dir tests/sample_images --dry-run
    python main.py --once --watch-dir tests/sample_images/real --max-pages 3

    # 不重新识别，把已有 Markdown 产物重新归档进 vault（重建索引与导图）
    python main.py --rebuild-vault --rebuild-from output/real_v3/notes

    # 只重算跨笔记双链，不重新归档（不调模型）
    python main.py --refresh-links

    # 本次不生成双链，只归档 + 刷新索引/导图
    python main.py --no-links

    # 归档到 OneDrive 同步目录（自动探测，写入 <OneDrive>/NoteFlow）
    python main.py --use-onedrive
    python main.py --detect-onedrive

环境变量见 config.py 顶部说明。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DOCUMENT_EXTENSIONS, Settings, setup_logging
from src import failure_log, loader, onedrive, organizer, preprocessor, recognizer
from src.watcher import FolderWatcher

logger = logging.getLogger("noteflow")

SOURCE_TYPES = {".pdf": "pdf", ".goodnotes": "goodnotes"}


def _record_failure(settings: Settings, source: Path, page: int | None, stage: str, exc: Exception) -> None:
    """失败留痕：追加一行 JSON，便于事后定位被跳过的文件/页面。"""
    failure_log.record(settings, source, page, stage, exc)


def _discard(path: Path) -> None:
    # 杀毒软件可能短暂占用刚写入的图片，重试几次再放弃，避免临时文件越积越多
    for _ in range(4):
        try:
            Path(path).unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05)


def _discard_page_raster(image_path: Path, settings: Settings) -> None:
    """只删我们自己拆页生成的中间图，绝不碰用户放进监听目录的原图。"""
    try:
        path = Path(image_path).resolve()
        path.relative_to(Path(settings.pages_dir).resolve())
    except (OSError, ValueError):
        return
    _discard(path)


def _process_page(
    page: loader.Page,
    source: Path,
    settings: Settings,
    mock: bool,
    dry_run: bool,
    preprocess_dir: Path,
) -> tuple[dict[str, Any] | None, Path | None, str, Exception | None]:
    """在线程池里跑「预处理 + 识别」；返回结果而不抛异常，交给主线程归档。

    文件流（PDF 拆页）只在主线程迭代，工作线程只碰单页图片，避免并发读同一个生成器。
    """
    stage = "preprocess"
    preprocessed: Path | None = None
    try:
        if page.text:
            stage = "recognize_text"
            if dry_run:
                logger.info(
                    "%s 第 %s 页命中文字层（%d 字），dry-run 不调用模型",
                    source.name,
                    page.index,
                    len(page.text),
                )
                return None, None, stage, None
            note = recognizer.recognize_text(
                page.text, settings, mock=mock, fallback_title=source.stem
            )
            return note, None, stage, None

        if page.image_path is None:
            raise ValueError("页面既没有文字也没有图片")
        stage = "preprocess"
        preprocessed = preprocessor.preprocess_image(
            page.image_path,
            preprocess_dir,
            max_side=settings.max_image_side,
            jpeg_quality=settings.jpeg_quality,
        )
        if not settings.keep_temp:
            _discard_page_raster(page.image_path, settings)
        if dry_run:
            return None, preprocessed, stage, None
        stage = "recognize"
        note = recognizer.recognize_image(preprocessed, settings, mock=mock)
        return note, preprocessed, stage, None
    except Exception as exc:  # 单页失败继续处理后续页面
        return None, preprocessed, stage, exc


def process_source(
    source: Path,
    settings: Settings,
    mock: bool = False,
    dry_run: bool = False,
    max_pages: int = 0,
    progress=None,
    should_cancel=None,
) -> list[Path]:
    """处理一个输入文件：拆页 → 预处理/取文字 → 识别 → 归档进 vault → 更新索引。

    单页失败不会中断其余页面；失败会写入 failures.jsonl 留痕。

    progress / should_cancel 是给桌面端用的可选回调，默认 None 时行为与之前完全一致：
    - progress(source, page_index, total_pages, stage, error=None)
      stage 取值 start / ok / failed；
    - should_cancel() 返回 True 时在页与页之间停止，已完成的页照常保留。
    """
    source = Path(source)
    try:
        pages = loader.PageStream(
            source,
            settings.pages_dir,
            text_min_chars=settings.pdf_text_min_chars,
            image_coverage_max=settings.pdf_image_coverage,
        )
        pages.prepare()
    except Exception as exc:
        # 文件级失败（损坏、格式不支持、.goodnotes 只有矢量笔迹等）也留痕
        failure_log.record(settings, source, None, "load", exc)
        raise
    total = pages.total

    if max_pages and total > max_pages:
        logger.info("--max-pages=%d：本次只处理 %s 的前 %d/%d 页", max_pages, source.name, max_pages, total)
        total = max_pages

    is_document = source.suffix.lower() in DOCUMENT_EXTENSIONS
    source_type = SOURCE_TYPES.get(source.suffix.lower(), "image")
    preprocess_dir = settings.preprocessed_dir / source.stem
    outputs: list[Path] = []
    failed = 0

    # 并发只作用于「预处理 + 识别」；拆页与归档仍在主线程串行，
    # 这样 PDF 生成器不会被多线程同时读取，vault 写入也天然有序。
    concurrency = max(1, min(int(getattr(settings, "recognize_concurrency", 1) or 1), 4))
    recognizer.configure_concurrency(concurrency)

    def finish(page: loader.Page, result: tuple[Any, Path | None, str, Exception | None]) -> None:
        nonlocal failed
        note, preprocessed, stage, error = result
        page_ok = error is None
        try:
            if error is not None:
                raise error
            if note is None:
                # dry-run：图片页保留预处理图供检查，文字页没有产物
                if preprocessed is not None:
                    outputs.append(preprocessed)
                if progress is not None:
                    progress(source, page.index, total, "ok")
                return

            stage = "archive"
            meta: dict[str, Any] = {
                "source": f"{source.name} 第 {page.index} 页" if is_document else source.name,
                "source_type": source_type,
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            if is_document:
                meta["page"] = page.index
                if source.suffix.lower() == ".goodnotes":
                    meta["page_note"] = "页序为提取顺序，不等于 GoodNotes 真实页码"

            output = organizer.archive_note(
                note,
                meta,
                settings,
                document=source.stem if is_document else "",
                filename_hint=f"{source.stem}-p{page.index:03d}" if is_document else None,
            )
            outputs.append(output)
            page_ok = True
            if progress is not None:
                progress(source, page.index, total, "ok")
            logger.info("处理完成: %s 第 %s 页 -> %s", source.name, page.index, output)
        except Exception as exc:  # 单页失败继续处理后续页面
            failed += 1
            _record_failure(settings, source, page.index, stage, exc)
            logger.error("%s 第 %s 页处理失败: %s", source.name, page.index, exc)
            if progress is not None:
                progress(source, page.index, total, "failed", exc)
        finally:
            # 归档成功后预处理图即可删除；失败时保留，便于排查这一页
            if preprocessed is not None and page_ok and not dry_run and not settings.keep_temp:
                _discard(preprocessed)

    pool = (
        ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="noteflow-recog")
        if concurrency > 1
        else None
    )
    pending: deque = deque()
    window = concurrency * 2  # 同时在飞的页数上限，控制内存与临时盘
    try:
        for page in pages:
            if max_pages and page.index > max_pages:
                break
            if should_cancel is not None and should_cancel():
                logger.info("收到停止请求，%s 剩余页面跳过", source.name)
                break
            logger.info("开始处理: %s 第 %s 页", source.name, page.index)
            if progress is not None:
                progress(source, page.index, total, "start")

            if pool is None:
                finish(page, _process_page(page, source, settings, mock, dry_run, preprocess_dir))
                continue

            pending.append(
                (
                    page,
                    pool.submit(_process_page, page, source, settings, mock, dry_run, preprocess_dir),
                )
            )
            if len(pending) >= window:
                pending_page, future = pending.popleft()
                finish(pending_page, future.result())

        # 已经提交的页仍会完成并归档（已经花了识别调用），避免半截丢弃
        while pending:
            pending_page, future = pending.popleft()
            finish(pending_page, future.result())
    finally:
        if pool is not None:
            pool.shutdown(wait=True)

    if is_document:
        logger.info("%s 共 %d 页：成功 %d，失败 %d", source.name, pages.total, len(outputs), failed)
    return outputs


def _collect_sources(directory: Path, extensions: tuple[str, ...]) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in extensions)


def _run_once(settings: Settings, mock: bool, dry_run: bool, max_pages: int) -> int:
    sources = _collect_sources(settings.watch_dir, settings.supported_extensions)
    if not sources:
        logger.warning("监听目录中没有可处理的文件: %s", settings.watch_dir)
        return 0

    failed = 0
    for source in sources:
        try:
            process_source(source, settings, mock=mock, dry_run=dry_run, max_pages=max_pages)
        except Exception as exc:  # 单个文件失败继续处理其余文件（失败已由 process_source 留痕）
            failed += 1
            logger.error("处理失败 %s: %s", source.name, exc)

    logger.info("本次共处理 %d 个文件，失败 %d 个", len(sources) - failed, failed)
    return 1 if failed else 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NoteFlow —— 手写笔记智能整理流水线（P2 跨笔记关联）")
    parser.add_argument("--watch-dir", type=Path, help="监听目录，覆盖 NOTEFLOW_WATCH_DIR")
    parser.add_argument("--output-dir", type=Path, help="输出目录，覆盖 NOTEFLOW_OUTPUT_DIR")
    parser.add_argument("--vault-dir", type=Path, help="Obsidian vault 目录，覆盖 NOTEFLOW_VAULT_DIR")
    parser.add_argument("--once", action="store_true", help="处理一次现有文件后退出，不常驻监听")
    parser.add_argument("--mock", action="store_true", help="使用内置示例识别结果，不调用模型")
    parser.add_argument("--dry-run", action="store_true", help="只做拆页与预处理，不调用模型")
    parser.add_argument("--max-pages", type=int, default=0, help="每个文档最多处理前 N 页（0 表示不限）")
    parser.add_argument("--process-existing", action="store_true", help="常驻监听启动时先处理已有文件")
    parser.add_argument("--no-links", action="store_true", help="本次不生成跨笔记双链（仍刷新索引与导图）")
    parser.add_argument("--refresh-links", action="store_true", help="按当前 vault 全量重算双链（不调模型）")
    parser.add_argument("--rebuild-vault", action="store_true", help="把已有 Markdown 产物重新归档进 vault（不调模型）")
    parser.add_argument("--rebuild-from", type=Path, help="重建来源目录，默认 <output>/notes")
    parser.add_argument("--use-onedrive", action="store_true", help="自动探测 OneDrive，归档到 <OneDrive>/NoteFlow")
    parser.add_argument("--detect-onedrive", action="store_true", help="只探测并打印本机 OneDrive 路径")
    parser.add_argument("--log-level", default=None, help="日志级别：DEBUG / INFO / WARNING")
    return parser.parse_args()


def _print_onedrive_result() -> int:
    paths = onedrive.detect_onedrive_paths()
    if paths:
        print("探测到 OneDrive 目录：")
        for path in paths:
            print(f"  - {path}")
        print(f"建议 vault 目录：{paths[0] / onedrive.ONEDRIVE_SUBDIR}")
    else:
        print(
            "未探测到 OneDrive 目录。请确认 OneDrive 客户端已登录并完成同步，\n"
            "然后用 --vault-dir 手动指定同步目录，例如：\n"
            "  python main.py --vault-dir \"D:\\OneDrive\\NoteFlow\""
        )
    return 0


def main() -> int:
    args = _parse_args()

    settings = Settings.from_env()
    if args.watch_dir:
        settings.watch_dir = args.watch_dir.expanduser()
    if args.output_dir:
        settings.output_dir = args.output_dir.expanduser()
    if args.vault_dir:
        settings.vault_root = str(args.vault_dir.expanduser())
    if args.use_onedrive:
        settings.vault_onedrive = True
    if args.no_links:
        settings.link_enabled = False
    if args.log_level:
        settings.log_level = args.log_level.upper()

    setup_logging(settings.log_level)

    if args.detect_onedrive:
        return _print_onedrive_result()

    if settings.vault_onedrive and not settings.vault_root:
        try:
            settings.vault_root = str(onedrive.prepare_onedrive_vault())
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2
        logger.info("OneDrive vault 已就绪: %s", settings.vault_root)

    try:
        onedrive.ensure_writable(settings.vault_dir)
        settings.ensure_dirs()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2
    except OSError as exc:
        logger.error(
            "无法创建输出目录: %s\n请检查路径是否存在、是否有写权限。原始错误: %s",
            settings.output_dir,
            exc,
        )
        return 2

    if args.rebuild_vault:
        source_notes = (args.rebuild_from or settings.notes_dir).expanduser()
        try:
            outputs = organizer.rebuild_vault(source_notes, settings)
        except FileNotFoundError as exc:
            logger.error("%s", exc)
            return 2
        logger.info("重建完成：%d 篇笔记已归档到 %s", len(outputs), settings.vault_dir)
        return 0

    if args.refresh_links:
        count = organizer.refresh_links(settings)
        logger.info("双链刷新完成：%d 篇笔记有更新", count)
        return 0

    if not args.dry_run and not args.mock:
        try:
            settings.require_api()
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2

    if args.once:
        return _run_once(settings, mock=args.mock, dry_run=args.dry_run, max_pages=args.max_pages)

    watcher = FolderWatcher(
        settings,
        processor=lambda path: process_source(
            path, settings, mock=args.mock, dry_run=args.dry_run, max_pages=args.max_pages
        ),
    )
    if args.process_existing:
        count = watcher.process_existing_files()
        logger.info("已把 %d 个已有文件加入队列", count)

    watcher.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
