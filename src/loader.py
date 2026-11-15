"""输入加载：把图片 / PDF / .goodnotes 统一转成「页面」，供后续处理复用。

- 图片：本身即一页
- PDF：有文字层且不是整页图片时直接抽取文字（保留段落/换行），
  扫描件、手写翻拍、矢量图页用 PyMuPDF 栅格化后走视觉识别
- .goodnotes：zip 容器，页内容存放在 attachments/ 下；notes/*.pb 是矢量笔迹，
  目前不做笔迹渲染，若文档只有笔迹没有可用的页面图片会明确报错
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from PIL import Image

from config import IMAGE_EXTENSIONS

logger = logging.getLogger(__name__)

GOODNOTES_EXT = ".goodnotes"
DOCUMENT_EXTENSIONS = IMAGE_EXTENSIONS + (".pdf", GOODNOTES_EXT)
PDF_DPI = 200

# 课件截图通常是 16:9 左右（1.6-1.8），手机翻拍接近 4:3（1.33）；
# 两者并存时优先取截图，避免同一页识别两遍
SCREENSHOT_RATIO_RANGE = (1.45, 2.1)


@dataclass
class Page:
    """一页输入。

    - 扫描件/图片页：image_path 指向可预处理的图片
    - 有文字层的 PDF 页：text 为按版面顺序抽取的文字，无需栅格化
    """

    index: int
    image_path: Path | None = None
    text: str | None = None


def load_pages(
    source: Path,
    work_dir: Path,
    dpi: int = PDF_DPI,
    text_min_chars: int = 40,
    image_coverage_max: float = 0.2,
) -> list[Page]:
    """把任意受支持的输入拆成页面列表（保持原有一次性返回的调用方式）。"""
    stream = PageStream(source, work_dir, dpi, text_min_chars, image_coverage_max)
    stream.prepare()
    return list(stream)


class PageStream:
    """页面流：PDF 逐页栅格化，处理完一页再拆下一页，避免整本先落盘。

    - prepare() 只做准备（打开 PDF 取页数 / 解包 .goodnotes），total 立即可用
    - 迭代 PDF 时每页才做 200dpi 栅格化，首屏延迟与临时盘峰值从「整本」降到「一页」
    - .goodnotes 仍是两阶段（先落盘候选图才能按画幅筛选），但不再把全部附件读进内存
    """

    def __init__(
        self,
        source: Path,
        work_dir: Path,
        dpi: int = PDF_DPI,
        text_min_chars: int = 40,
        image_coverage_max: float = 0.2,
    ) -> None:
        self.source = Path(source)
        self.work_dir = Path(work_dir)
        self.dpi = dpi
        self.text_min_chars = text_min_chars
        self.image_coverage_max = image_coverage_max
        self.total = 0
        self._kind = ""
        self._pages: list[Page] = []

    def prepare(self) -> int:
        suffix = self.source.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            self._kind = "image"
            self._pages = [Page(1, self.source)]
        elif suffix == ".pdf":
            self._kind = "pdf"
            with pymupdf.open(self.source) as doc:
                if doc.page_count == 0:
                    raise ValueError(f"PDF 没有可渲染页面: {self.source.name}")
                self.total = int(doc.page_count)
            return self.total
        elif suffix == GOODNOTES_EXT:
            self._kind = "goodnotes"
            self._pages = _goodnotes_pages(
                self.source, self.work_dir, self.dpi, self.text_min_chars, self.image_coverage_max
            )
        else:
            raise ValueError(f"不支持的文件类型: {self.source.suffix}")
        self.total = len(self._pages)
        return self.total

    def __iter__(self):
        if self._kind == "pdf":
            yield from self._iter_pdf()
            return
        yield from self._pages

    def _iter_pdf(self):
        output_dir = _page_dir(self.source, self.work_dir)
        zoom = self.dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        text_pages = 0
        rendered = 0
        with pymupdf.open(self.source) as doc:
            for page in doc:
                index = page.number + 1
                text = _extract_page_text(page)
                if _is_text_page(page, text, self.text_min_chars, self.image_coverage_max):
                    text_pages += 1
                    yield Page(index, None, text)
                    continue
                pixmap = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)
                # 中间页图只需喂给预处理：JPEG q90 编码比 PNG 快约 3 倍、体积小 4 倍
                target = output_dir / f"p{index:03d}.jpg"
                pixmap.save(str(target), jpg_quality=90)
                rendered += 1
                yield Page(index, target)
        logger.info(
            "PDF 已解析 %d 页（文字层 %d 页，视觉 %d 页）: %s",
            text_pages + rendered,
            text_pages,
            rendered,
            self.source.name,
        )


def estimate_pages(source: Path) -> tuple[int, int]:
    """快速估算页数与模型调用次数，不做栅格化、不读图。

    图片按 1 页；PDF 按页数；.goodnotes 按 attachments 里的图片条目数粗估。
    有文字层的 PDF 页仍会过一次文本模型，所以调用次数按页数计。
    估算失败时返回 (1, 1)，让界面至少能给出量级。
    """
    source = Path(source)
    suffix = source.suffix.lower()
    try:
        if suffix in IMAGE_EXTENSIONS:
            return 1, 1
        if suffix == ".pdf":
            with pymupdf.open(source) as doc:
                pages = max(int(doc.page_count), 1)
            return pages, pages
        if suffix == GOODNOTES_EXT:
            with zipfile.ZipFile(source) as archive:
                attachments = [
                    name
                    for name in archive.namelist()
                    if name.startswith("attachments/")
                    and Path(name).suffix.lower() in (".png", ".jpg", ".jpeg")
                ]
            pages = max(len(attachments), 1)
            return pages, pages
    except Exception as exc:  # 估算只是提示，读不了就按 1 页算
        logger.debug("估算页数失败 %s: %s", source.name, exc)
    return 1, 1


def _page_dir(source: Path, work_dir: Path) -> Path:
    target = Path(work_dir) / source.stem
    target.mkdir(parents=True, exist_ok=True)
    return target


def _extract_page_text(page: "pymupdf.Page") -> str:
    """按版面顺序抽取文字，保留段落与换行结构。"""
    parts: list[str] = []
    for block in page.get_text("blocks", sort=True):
        if len(block) >= 7 and block[6] != 0:
            continue  # 图片块，文字路径不处理图内信息
        text = str(block[4]).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _is_text_page(page: "pymupdf.Page", text: str, min_chars: int, coverage_max: float) -> bool:
    """文字够多且页面不是整页图片（扫描件/手写翻拍）时才走文本路径。

    像「手写页上叠了几行电子标注」的 PDF，文字层只有几十个字、
    图片却覆盖大半页，这时必须走视觉识别，否则会丢掉手写和图表。
    """
    chars = len(re.sub(r"\s+", "", text))
    if chars < min_chars:
        return False

    area = page.rect.width * page.rect.height
    if area <= 0:
        return True

    covered = 0.0
    for info in page.get_image_info():
        rect = pymupdf.Rect(info.get("bbox"))
        covered += max(rect.width, 0.0) * max(rect.height, 0.0)
    if covered / area > coverage_max:
        return False

    # 矢量图页（工程图/流程图）：文字只是标注，整页视觉识别才能保留结构
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    if len(drawings) > 40 and chars < 400:
        return False
    return True


def _pdf_pages(
    source: Path,
    work_dir: Path,
    dpi: int,
    text_min_chars: int = 40,
    image_coverage_max: float = 0.2,
) -> list[Page]:
    """逐页解析 PDF：有文字层且非整页图片的走文本，其余栅格化。"""
    output_dir = _page_dir(source, work_dir)
    pages: list[Page] = []
    text_pages = 0

    with pymupdf.open(source) as doc:
        if doc.page_count == 0:
            raise ValueError(f"PDF 没有可渲染页面: {source.name}")

        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        for page in doc:
            index = page.number + 1
            text = _extract_page_text(page)
            if _is_text_page(page, text, text_min_chars, image_coverage_max):
                pages.append(Page(index, None, text))
                text_pages += 1
                continue

            pixmap = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)
            target = output_dir / f"p{index:03d}.jpg"
            pixmap.save(str(target), jpg_quality=90)
            pages.append(Page(index, target))

    logger.info(
        "PDF 已解析 %d 页（文字层 %d 页，视觉 %d 页）: %s",
        len(pages),
        text_pages,
        len(pages) - text_pages,
        source.name,
    )
    return pages


def _detect_kind(data: bytes) -> str:
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return "unknown"


def _ordered_image_entries(archive: zipfile.ZipFile) -> list[str]:
    """按 index.attachments.pb 里的顺序取附件名；解析失败则退回 zip 顺序。"""
    ordered: list[str] = []
    try:
        index_bytes = archive.read("index.attachments.pb")
        for match in re.findall(rb"attachments/[0-9A-Fa-f-]{10,}", index_bytes):
            name = match.decode("ascii", errors="ignore")
            if name in archive.namelist() and name not in ordered:
                ordered.append(name)
    except KeyError:
        pass

    for name in archive.namelist():
        if name.startswith("attachments/") and name not in ordered:
            ordered.append(name)
    return ordered


def _move_into_place(source: Path, target: Path) -> None:
    """把候选页图改成最终文件名。

    Windows 上杀毒软件可能短暂占用刚写入的文件，rename 会报 WinError 32；
    重试几次仍失败时退化为复制 + 删除，保证不因瞬时占用丢掉整份文档。
    """
    for _ in range(5):
        try:
            source.replace(target)
            return
        except PermissionError:
            time.sleep(0.05)
    shutil.copyfile(source, target)
    try:
        source.unlink()
    except OSError:
        pass


def _goodnotes_pages(
    source: Path,
    work_dir: Path,
    dpi: int = PDF_DPI,
    text_min_chars: int = 40,
    image_coverage_max: float = 0.2,
) -> list[Page]:
    """从 .goodnotes 的 zip 容器中提取页面图片。

    GoodNotes 把页面内容放在 attachments/ 下（截图、翻拍照或内嵌 PDF），
    notes/*.pb 里是矢量笔迹，暂不渲染。优先使用清晰截图，丢弃同一内容的翻拍照。
    """
    output_dir = _page_dir(source, work_dir)
    # 清掉上次运行遗留的候选图，避免重跑时越堆越多
    for stale in output_dir.glob("cand*"):
        try:
            stale.unlink()
        except OSError:
            pass

    image_candidates: list[tuple[str, Path, tuple[int, int]]] = []
    embedded_pdfs: list[tuple[str, Path]] = []
    seen_digest: set[str] = set()

    with zipfile.ZipFile(source) as archive:
        for name in _ordered_image_entries(archive):
            # 一次只读一个附件：大 .goodnotes 不再把全部图片字节同时驻留内存
            data = archive.read(name)
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_digest:
                continue

            kind = _detect_kind(data)
            if kind == "pdf":
                pdf_path = output_dir / f"{Path(name).name}.pdf"
                pdf_path.write_bytes(data)
                embedded_pdfs.append((name, pdf_path))
                seen_digest.add(digest)
                continue
            if kind not in ("png", "jpeg"):
                continue

            try:
                with Image.open(io.BytesIO(data)) as img:
                    width, height = img.size
            except OSError:
                continue
            if width * height < 50_000:
                continue

            seen_digest.add(digest)
            suffix = ".png" if kind == "png" else ".jpg"
            candidate = output_dir / f"cand{len(image_candidates):03d}{suffix}"
            # 原样落盘：PNG/JPEG 再编码一次既慢又把文件放大，预处理阶段会统一转灰度 JPG
            candidate.write_bytes(data)
            image_candidates.append((name, candidate, (width, height)))

    if not image_candidates and not embedded_pdfs:
        raise ValueError(
            f"{source.name} 未找到可用的页面图片：该文件可能只包含矢量笔迹（notes/*.pb），暂不支持渲染"
        )

    # 截图与翻拍照并存时，丢掉手机翻拍（非 16:9 画幅），避免同一页识别两遍
    screenshots = [
        item for item in image_candidates if SCREENSHOT_RATIO_RANGE[0] <= item[2][0] / item[2][1] <= SCREENSHOT_RATIO_RANGE[1]
    ]
    kept = screenshots if len(screenshots) >= 2 else image_candidates
    if len(kept) < len(image_candidates):
        logger.info("忽略 %d 张翻拍照，仅使用清晰截图: %s", len(image_candidates) - len(kept), source.name)
        kept_ids = {id(item) for item in kept}
        for item in image_candidates:
            if id(item) not in kept_ids:
                try:
                    item[1].unlink()
                except OSError:
                    pass

    pages: list[Page] = []
    for index, (name, candidate, _size) in enumerate(kept, start=1):
        target = output_dir / f"gn{index:03d}{candidate.suffix}"
        _move_into_place(candidate, target)
        pages.append(Page(index, target))

    # 内嵌 PDF（如整本讲义）追加为后续页；单页 PDF 多为页面模板/背景，不作为内容页
    for name, pdf_path in embedded_pdfs:
        try:
            with pymupdf.open(pdf_path) as probe:
                page_count = probe.page_count
        except Exception as exc:
            logger.warning("内嵌 PDF 无法打开 %s: %s", name, exc)
            pdf_path.unlink(missing_ok=True)
            continue
        if page_count <= 1:
            logger.info("跳过单页内嵌 PDF（视为页面模板）: %s", name)
            pdf_path.unlink(missing_ok=True)
            continue
        try:
            sub_pages = _pdf_pages(
                pdf_path, output_dir / "embedded", dpi, text_min_chars, image_coverage_max
            )
        except Exception as exc:  # 内嵌 PDF 损坏不影响已提取的图片页
            logger.warning("内嵌 PDF 解析失败 %s: %s", name, exc)
            continue
        finally:
            # 内嵌 PDF 只是中间容器，拆完页就删，避免 1GB 级输入残留几十 MB 临时文件
            pdf_path.unlink(missing_ok=True)
        for page in sub_pages:
            pages.append(Page(len(pages) + 1, page.image_path))

    logger.info(".goodnotes 提取到 %d 页: %s", len(pages), source.name)
    return pages
