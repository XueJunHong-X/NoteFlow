"""图片预处理：去阴影、倾斜矫正、对比度增强。

处理流程：EXIF 校正 → 灰度 → 去阴影 → 倾斜矫正 → CLAHE 对比度增强 → 缩放 → 落盘。

可独立运行：
    python -m src.preprocessor tests/sample_images/sample_note.png
    python -m src.preprocessor tests/sample_images -o output/preprocessed
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# 超过该角度多半不是纸张倾斜（而是整页旋转），交给人工处理，避免误矫正
MAX_SKEW_DEG = 15.0
MIN_SKEW_DEG = 0.3


def read_image(path: Path) -> np.ndarray:
    """读取图片，按 EXIF 方向自动转正。用 Pillow 打开以兼容中文路径。"""
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image


def remove_shadow(gray: np.ndarray) -> np.ndarray:
    """用背景估计做除法，消除拍照/扫描时的光照不均与阴影。"""
    height, width = gray.shape[:2]
    kernel = max(15, (min(height, width) // 40) | 1)  # 保证是奇数
    background = cv2.medianBlur(gray, kernel)
    background = cv2.dilate(background, np.ones((5, 5), np.uint8))
    background = np.maximum(background, 1)  # 防止除零
    return cv2.divide(gray, background, scale=255)


def _row_projection_score(binary: np.ndarray, angle: float) -> float:
    """按给定角度旋转后，统计水平投影的方差：文字行越水平，方差越大。"""
    height, width = binary.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    rotated = cv2.warpAffine(
        binary,
        matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    projection = rotated.sum(axis=1, dtype=np.float64)
    return float(np.var(projection))


def estimate_skew_angle(gray: np.ndarray) -> float:
    """用投影法估计倾斜角（正值为逆时针旋转量）。

    比最小外接矩形更稳：纸面边框、箭头等干扰不会带偏结果，
    因为评估的是「文字行投影是否足够尖锐」。
    """
    # 缩到较小尺寸即可，兼顾速度与精度
    sample = resize_max_side(gray, 800)
    binary = cv2.threshold(sample, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    if cv2.countNonZero(binary) < 200:
        return 0.0

    coarse = np.arange(-MAX_SKEW_DEG, MAX_SKEW_DEG + 0.01, 1.0)
    best = max(coarse, key=lambda angle: _row_projection_score(binary, float(angle)))

    fine = np.arange(best - 1.0, best + 1.01, 0.1)
    best = max(fine, key=lambda angle: _row_projection_score(binary, float(angle)))

    best_angle = float(best)
    return 0.0 if abs(best_angle) < MIN_SKEW_DEG else best_angle


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    height, width = gray.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    return cv2.warpAffine(
        gray,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def enhance_contrast(gray: np.ndarray) -> np.ndarray:
    """CLAHE 局部对比度增强，让铅笔/淡墨字迹更清晰。"""
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def resize_max_side(gray: np.ndarray, max_side: int) -> np.ndarray:
    height, width = gray.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return gray
    scale = max_side / float(longest)
    return cv2.resize(gray, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)


def preprocess_image(
    source: Path,
    output_dir: Path,
    max_side: int = 1800,
    jpeg_quality: int = 90,
) -> Path:
    """预处理单张图片并保存为 JPG，返回输出文件路径。"""
    source = Path(source)
    image = read_image(source)
    gray = to_gray(image)

    # 去阴影/CLAHE 在原始像素上计算是浪费：最终只保留 max_side，先降到目标尺寸再算，
    # 12MP 拍照页可少算约 5 倍像素，INTER_AREA 降采样本身还能压掉噪点。
    gray = resize_max_side(gray, max_side)

    gray = remove_shadow(gray)

    angle = estimate_skew_angle(gray)
    if angle:
        gray = rotate(gray, angle)
        logger.info("倾斜矫正 %.2f° (%s)", angle, source.name)

    gray = enhance_contrast(gray)
    gray = resize_max_side(gray, max_side)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # 预处理产物是中间文件，同名直接覆盖，重复运行不会堆出一串副本
    target = output_dir / f"{source.stem}.jpg"

    # 用 imencode + tofile，避免 cv2.imwrite 不支持非 ASCII 路径的问题
    ok, buffer = cv2.imencode(".jpg", gray, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
    if not ok:
        raise RuntimeError(f"图片编码失败: {source}")
    buffer.tofile(str(target))

    logger.info("预处理完成: %s -> %s", source.name, target)
    return target


def _collect_inputs(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp"})


def main() -> int:
    parser = argparse.ArgumentParser(description="NoteFlow 图片预处理（可独立运行）")
    parser.add_argument("input", type=Path, help="图片文件或目录")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output/preprocessed"), help="输出目录")
    parser.add_argument("--max-side", type=int, default=1800, help="图片最长边像素")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    if not args.input.exists():
        logger.error("输入路径不存在: %s", args.input)
        return 1

    failed = 0
    for image_path in _collect_inputs(args.input):
        try:
            preprocess_image(image_path, args.output_dir, max_side=args.max_side)
        except Exception as exc:  # 单张失败不影响批量处理
            failed += 1
            logger.error("处理失败 %s: %s", image_path.name, exc)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
