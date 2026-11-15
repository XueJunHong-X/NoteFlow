"""NoteFlow 一键演示入口：一条命令跑完整条流水线，并自动选择在线 / 离线模式。

评审拿到仓库后只需要：
    python demo.py

行为：
1. 检查 Python 版本与依赖，缺什么就提示装什么；
2. 检查 NOTEFLOW_API_KEY —— 有 Key 走真实识别，没有 Key 自动切到离线示例，
   不会一上来就报错退出；
3. 处理 tests/sample_images/ 里的合成样例图（也可以 --input 换成自己的目录）；
4. 输出结构化 Markdown、课程索引、课程总览、Mermaid 思维导图、跨笔记双链。

模式说明：
- 在线模式：调用多模态模型真实识别（需要 NOTEFLOW_API_KEY）。
- 离线模式：不调用任何接口，使用仓库内置的示例识别结果，链路与在线模式完全一致
  （同样经过预处理 → 结构规整 → 渲染 → 归档 → 索引/导图/双链）。

用法：
    python demo.py                     # 一键演示（自动选模式）
    python demo.py --mock              # 强制离线示例
    python demo.py --real              # 强制真实识别（必须已配 Key）
    python demo.py --input 我的笔记/    # 换成自己的图片 / PDF / .goodnotes
    python demo.py --out output/demo   # 换输出目录（默认 output/demo）
    python demo.py --clean             # 先清空演示输出目录再跑
    python demo.py --no-links          # 本次不生成跨笔记双链

常驻监听、重建 vault、刷新双链等完整参数见 main.py。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent

REQUIRED_IMPORTS = (
    ("cv2", "opencv-python-headless"),
    ("numpy", "numpy"),
    ("PIL", "Pillow"),
    ("requests", "requests"),
    ("fitz", "PyMuPDF"),
)

BAR = "=" * 68


def _fail(message: str) -> int:
    print(f"\n[停止] {message}\n")
    return 2


def _check_python() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit(
            f"需要 Python 3.10 或更高版本，当前是 {sys.version.split()[0]}。\n"
            "请安装新版 Python 后重新运行：python demo.py"
        )


def _check_dependencies() -> None:
    """缺依赖时一次性列出，并给出可直接复制的安装命令。"""
    import warnings

    missing: list[str] = []
    for module, package in REQUIRED_IMPORTS:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # 第三方库的弃用提示不该出现在演示输出里
                __import__(module)
        except ImportError:
            missing.append(package)

    if not missing:
        return

    venv_hint = ""
    if not Path(sys.prefix, "pyvenv.cfg").is_file():
        venv_hint = (
            "\n建议先建虚拟环境（Windows PowerShell）：\n"
            "  python -m venv .venv\n"
            "  .venv\\Scripts\\Activate.ps1\n"
        )
    print(f"\n缺少依赖：{', '.join(missing)}")
    print(venv_hint)
    print("安装命令：")
    print("  python -m pip install -r requirements.txt")
    print("\n装好后重新运行：python demo.py\n")
    raise SystemExit(2)


def _print_banner(mode: str, input_dir: Path, settings: Any, sources: list[Path]) -> None:
    print(BAR)
    print("  NoteFlow 一键演示 —— 手写笔记智能整理流水线")
    print(BAR)
    print(f"  运行模式 : {'离线示例（不调用模型，0 消耗）' if mode == 'mock' else '在线识别（调用多模态模型）'}")
    print(f"  输入目录 : {input_dir}")
    print(f"  待处理   : {len(sources)} 个文件 -> {', '.join(p.name for p in sources)}")
    print(f"  输出目录 : {settings.output_dir}")
    print(f"  Vault    : {settings.vault_dir}")
    if mode == "real":
        print(f"  识别模型 : {settings.model}")
    else:
        print("  离线说明 : 内置示例结果来自本项目对这两张样例图的真实识别记录")
        print("             配好 NOTEFLOW_API_KEY 再跑一次即可得到实时识别结果")
    print(BAR)


def _collect_sources(input_path: Path, extensions: tuple[str, ...]) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if not input_path.exists():
        return []
    return sorted(p for p in input_path.iterdir() if p.is_file() and p.suffix.lower() in extensions)


def _process_file(source: Path, settings: Any, mode: str, max_pages: int) -> tuple[int, int]:
    """处理单个输入文件，返回（成功页数, 失败页数）。

    与 main.process_source 同构：拆页 → 预处理 → 识别 → 归档。
    这里额外打印舞台提示，并让离线模式复用内置结果。
    """
    from config import DOCUMENT_EXTENSIONS
    from src import failure_log, loader, organizer, preprocessor, recognizer

    started = time.time()
    pages = loader.load_pages(
        source,
        settings.pages_dir,
        text_min_chars=settings.pdf_text_min_chars,
        image_coverage_max=settings.pdf_image_coverage,
    )
    total = len(pages)
    if max_pages and total > max_pages:
        pages = pages[:max_pages]

    is_document = source.suffix.lower() in DOCUMENT_EXTENSIONS
    source_type = {".pdf": "pdf", ".goodnotes": "goodnotes"}.get(source.suffix.lower(), "image")
    preprocess_dir = settings.preprocessed_dir / source.stem

    print(f"\n[{source.name}] 共 {len(pages)} 页" + (f"（原文档 {total} 页，--max-pages 截断）" if len(pages) < total else ""))
    ok = failed = 0

    for page in pages:
        stage = "preprocess"
        page_started = time.time()
        try:
            print(f"  ├─ 第 {page.index} 页：预处理 ...", end="", flush=True)
            if page.text:
                stage = "recognize_text"
                print(" 有文字层，跳过图片预处理")
                note = recognizer.recognize_text(
                    page.text, settings, mock=(mode == "mock"), fallback_title=source.stem
                )
            else:
                if page.image_path is None:
                    raise ValueError("页面既没有文字也没有图片")
                preprocessed = preprocessor.preprocess_image(
                    page.image_path,
                    preprocess_dir,
                    max_side=settings.max_image_side,
                    jpeg_quality=settings.jpeg_quality,
                )
                print(" 完成")

                stage = "recognize"
                print(f"  ├─ 第 {page.index} 页：识别 ...", end="", flush=True)
                if mode == "mock":
                    note = recognizer.build_mock_note(preprocessed)
                else:
                    note = recognizer.recognize_image(preprocessed, settings, mock=False)
                print(f" 完成（{len(note.get('sections') or [])} 个章节，{len(note.get('arrows') or [])} 条关联）")

            stage = "archive"
            print(f"  ├─ 第 {page.index} 页：渲染归档 ...", end="", flush=True)
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
            print(f" 完成（{time.time() - page_started:.1f}s）")
            print(f"  │    -> {output.relative_to(settings.output_dir)}")
            ok += 1
        except Exception as exc:  # 单页失败跳过，不影响后续页面与文件
            failed += 1
            failure_log.record(settings, source, page.index, stage, exc)
            print(f" 失败（已记录到 failures.jsonl）：{exc}")

    print(f"  └─ {source.name} 完成，用时 {time.time() - started:.1f}s")
    return ok, failed


def _print_vault_tree(vault_dir: Path) -> None:
    if not vault_dir.exists():
        return
    print("\nVault 结构：")
    for path in sorted(vault_dir.rglob("*")):
        if path.is_dir():
            continue
        depth = len(path.relative_to(vault_dir).parts) - 1
        print(f"  {'  ' * depth}{path.name}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NoteFlow 一键演示（自动选择在线 / 离线模式）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python demo.py\n"
            "  python demo.py --mock\n"
            "  python demo.py --input tests/sample_images\n"
            "  python demo.py --input 我的笔记 --out output/demo-custom --clean"
        ),
    )
    parser.add_argument("--input", type=Path, default=BASE_DIR / "tests" / "sample_images", help="输入文件或目录")
    parser.add_argument("--out", type=Path, default=BASE_DIR / "output" / "demo", help="输出目录（默认 output/demo）")
    parser.add_argument("--mock", action="store_true", help="强制离线示例模式，不调用模型")
    parser.add_argument("--real", action="store_true", help="强制真实识别（必须已配 NOTEFLOW_API_KEY）")
    parser.add_argument("--clean", action="store_true", help="先清空演示输出目录再跑，保证从零开始")
    parser.add_argument("--max-pages", type=int, default=0, help="每个文档最多处理前 N 页（0 表示不限）")
    parser.add_argument("--no-links", action="store_true", help="本次不生成跨笔记双链")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    _check_python()
    _check_dependencies()

    from config import Settings, setup_logging
    from src import onedrive

    settings = Settings.from_env()
    settings.output_dir = args.out.expanduser().resolve()
    settings.log_level = "WARNING"  # 演示只用中文舞台提示，底层日志降噪
    if args.no_links:
        settings.link_enabled = False
    setup_logging(settings.log_level)

    input_path = args.input.expanduser()
    sources = _collect_sources(input_path, settings.supported_extensions)
    if not sources:
        return _fail(
            f"输入目录里没有可处理的文件：{input_path}\n"
            "支持的格式：png / jpg / jpeg / bmp / webp / pdf / goodnotes"
        )

    # ---- 模式判定：宁可少花钱，也不让评审一上来就卡在报错上 ----
    if args.mock and args.real:
        return _fail("--mock 和 --real 不能同时使用。")
    if args.real and not settings.api_key:
        return _fail(
            "你指定了 --real，但没有找到 NOTEFLOW_API_KEY。\n"
            "  配法一：项目根目录新建 .env，写入一行 NOTEFLOW_API_KEY=你的密钥\n"
            "  配法二：PowerShell 里执行 $env:NOTEFLOW_API_KEY=\"你的密钥\"\n"
            "  不配密钥也能演示：去掉 --real，或改用 --mock 走离线示例"
        )

    if args.mock:
        mode = "mock"
    elif args.real:
        mode = "real"
    else:
        mode = "real" if settings.api_key else "mock"
        if mode == "mock":
            print("\n提示：没有检测到 NOTEFLOW_API_KEY，已自动切换到【离线示例】模式。")
            print("      整条链路照常跑完，只是识别结果用仓库内置示例，不调用模型、不产生费用。")
            print("      想跑真实识别：在项目根目录 .env 里配置 NOTEFLOW_API_KEY 后重新执行 python demo.py")

    if args.clean and settings.output_dir.exists():
        shutil.rmtree(settings.output_dir)
        print(f"已清空演示输出目录：{settings.output_dir}")

    try:
        onedrive.ensure_writable(settings.vault_dir)
        settings.ensure_dirs()
    except (RuntimeError, OSError) as exc:
        return _fail(f"无法准备输出目录：{exc}")

    _print_banner(mode, input_path, settings, sources)

    started = time.time()
    ok = failed = 0
    for source in sources:
        file_ok, file_failed = _process_file(source, settings, mode, args.max_pages)
        ok += file_ok
        failed += file_failed

    print(BAR)
    print(f"  演示结束：成功 {ok} 页，失败 {failed} 页，用时 {time.time() - started:.1f}s")
    print(BAR)

    _print_vault_tree(settings.vault_dir)

    steps = ["用 Obsidian 打开 vault 目录，看双向链接、索引页和思维导图：\n     " + str(settings.vault_dir)]
    compare_page = BASE_DIR / "docs" / "before-after.html"
    if compare_page.is_file():
        steps.append(f"浏览器打开前后对比页：\n     {compare_page}")
    steps.append('换成自己的笔记（需配 Key）：python demo.py --input "D:\\我的笔记"')
    steps.append("常驻监听整个文件夹：python main.py --watch-dir inbox")

    print("\n接下来可以：")
    for index, step in enumerate(steps, start=1):
        print(f"  {index}. {step}")
    if mode == "mock":
        print("\n说明：样例是合成英文笔记，课程名推断不出时归档到「未分类」属预期；")
        print("      换成按课程命名的中文笔记（如「高等数学.pdf」）会自动分课程目录。")
    if failed:
        print(f"\n失败明细见：{settings.failure_log}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
