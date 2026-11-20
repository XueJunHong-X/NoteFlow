"""生成 NoteFlow 科技感应用图标（程序化绘制，矢量级清晰）。

输出：
  assets/icon/noteflow-icon.png   1024 主图（预览/备用）
  assets/icon/NoteFlow.ico        多尺寸 ICO（16~256）
并可复制 ICO 到 exe 同目录供快捷方式引用。
"""
from __future__ import annotations

import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "assets" / "icon"
OUT_DIR.mkdir(parents=True, exist_ok=True)

S = 1024
SS = 2  # 超采样倍率，最后缩回，边缘更顺滑
W = S * SS


def rgba(c: tuple[int, ...], a: int = 255) -> tuple[int, ...]:
    return (*c, a)


# ---------- 1. 圆角方形渐变底 + 径向辉光 ----------
top = np.array([13, 32, 74], dtype=np.float32)
bottom = np.array([4, 9, 26], dtype=np.float32)
yy = np.linspace(0, 1, W)[:, None]
grad = top[None, None, :] * (1 - yy[:, :, None]) + bottom[None, None, :] * yy[:, :, None]
base = np.repeat(grad, W, axis=1)

# 中心偏上的青色径向辉光
cx, cy = W * 0.5, W * 0.42
xx, yy2 = np.meshgrid(np.linspace(0, 1, W), np.linspace(0, 1, W))
d = np.sqrt((xx - 0.5) ** 2 + (yy2 - 0.42) ** 2)
glow = np.clip(1 - d / 0.62, 0, 1) ** 2 * 0.55
cyan = np.array([34, 150, 220], dtype=np.float32)
base = base * (1 - glow[..., None]) + cyan[None, None, :] * glow[..., None]

# 四角轻微压暗，增加质感
vig = np.clip(d / 0.95, 0, 1) ** 2 * 0.35
base *= 1 - vig[..., None]

img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

# 圆角遮罩
radius = int(W * 0.225)
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=radius, fill=255)
img.putalpha(mask)

draw = ImageDraw.Draw(img)

# ---------- 2. 电路纹理（弱装饰） ----------
circuit = Image.new("RGBA", (W, W), (0, 0, 0, 0))
cd = ImageDraw.Draw(circuit)
rng = np.random.default_rng(7)
for _ in range(26):
    x0 = int(rng.integers(int(W * 0.06), int(W * 0.94)))
    y0 = int(rng.integers(int(W * 0.05), int(W * 0.95)))
    L = int(W * rng.uniform(0.04, 0.11))
    horizontal = rng.integers(0, 2)
    a = int(rng.integers(18, 46))
    col = (90, 200, 255, a)
    if horizontal:
        cd.line([(x0, y0), (x0 + L, y0)], fill=col, width=SS * 2)
        cd.ellipse([x0 + L - SS * 3, y0 - SS * 3, x0 + L + SS * 3, y0 + SS * 3], fill=col)
    else:
        cd.line([(x0, y0), (x0, y0 + L)], fill=col, width=SS * 2)
        cd.ellipse([x0 - SS * 3, y0 + L - SS * 3, x0 + SS * 3, y0 + L + SS * 3], fill=col)
circuit = circuit.filter(ImageFilter.GaussianBlur(SS))
img = Image.alpha_composite(img, circuit)
draw = ImageDraw.Draw(img)

# ---------- 3. 中央玻璃文档 ----------
doc_w, doc_h = int(W * 0.46), int(W * 0.57)
dx = (W - doc_w) // 2
dy = int(W * 0.24)
fold = int(W * 0.115)
dr = int(W * 0.045)
glass = (120, 190, 255, 34)
edge = (96, 214, 255, 235)

# 文档辉光层
doc_glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
gd = ImageDraw.Draw(doc_glow)
gd.rounded_rectangle([dx, dy, dx + doc_w, dy + doc_h], radius=dr, fill=(40, 170, 255, 150))
# 折角辉光
gd.polygon(
    [(dx + doc_w - fold, dy), (dx + doc_w, dy + fold), (dx + doc_w - fold, dy + fold)],
    fill=(40, 170, 255, 150),
)
doc_glow = doc_glow.filter(ImageFilter.GaussianBlur(SS * 14))
img = Image.alpha_composite(img, doc_glow)

# 文档本体（圆角矩形 + 折角镂空效果）
doc_layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
dd = ImageDraw.Draw(doc_layer)
dd.rounded_rectangle([dx, dy, dx + doc_w, dy + doc_h], radius=dr, fill=glass, outline=edge, width=SS * 5)
# 折角：盖背景色三角 + 描边折线
dd.polygon(
    [(dx + doc_w - fold, dy), (dx + doc_w, dy + fold), (dx + doc_w - fold, dy + fold)],
    fill=(26, 58, 108, 255),
)
dd.line(
    [(dx + doc_w - fold, dy), (dx + doc_w - fold, dy + fold), (dx + doc_w, dy + fold)],
    fill=(140, 226, 255, 200),
    width=SS * 4,
)
# 文档内的文字行（抽象横线，暗示笔记内容）
line_col = (150, 220, 255, 150)
for i, w_ratio in enumerate((0.62, 0.72, 0.55, 0.68, 0.45)):
    ly = dy + int(doc_h * (0.22 + i * 0.115))
    lx0 = dx + int(doc_w * 0.16)
    lx1 = dx + int(doc_w * (0.16 + w_ratio * 0.68))
    dd.rounded_rectangle([lx0, ly, lx1, ly + SS * 9], radius=SS * 4, fill=line_col)
img = Image.alpha_composite(img, doc_layer)

# ---------- 4. 流动光线（Flow） ----------
def bezier(p0, p1, p2, p3, n=200):
    t = np.linspace(0, 1, n)[:, None]
    pts = (
        (1 - t) ** 3 * np.array(p0)
        + 3 * (1 - t) ** 2 * t * np.array(p1)
        + 3 * (1 - t) * t**2 * np.array(p2)
        + t**3 * np.array(p3)
    )
    return [tuple(map(int, p)) for p in pts]


flow_layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
fd = ImageDraw.Draw(flow_layer)
streams = [
    ((0.16, 0.40), (0.38, 0.30), (0.62, 0.52), (0.86, 0.42), (72, 226, 255), SS * 11, 235),
    ((0.14, 0.56), (0.36, 0.66), (0.60, 0.44), (0.88, 0.58), (88, 150, 255), SS * 9, 205),
    ((0.20, 0.72), (0.42, 0.80), (0.62, 0.66), (0.82, 0.74), (140, 110, 255), SS * 7, 175),
]
for p0, p1, p2, p3, col, width, alpha in streams:
    pts = bezier(
        (p0[0] * W, p0[1] * W),
        (p1[0] * W, p1[1] * W),
        (p2[0] * W, p2[1] * W),
        (p3[0] * W, p3[1] * W),
    )
    fd.line(pts, fill=(*col, alpha), width=width, joint="curve")
    # 末端光点
    ex, ey = pts[-1]
    r = width * 1.6
    fd.ellipse([ex - r, ey - r, ex + r, ey + r], fill=(*col, min(alpha + 20, 255)))
flow_glow = flow_layer.filter(ImageFilter.GaussianBlur(SS * 10))
img = Image.alpha_composite(img, flow_glow)
img = Image.alpha_composite(img, flow_layer)

# ---------- 5. 顶部玻璃高光 ----------
gloss = Image.new("RGBA", (W, W), (0, 0, 0, 0))
gn = np.linspace(1, 0, W).reshape(W, 1) * np.ones((1, W))
gmask = (np.clip(gn - 0.55, 0, 1) / 0.45 * 46).astype(np.uint8)
gmask_img = Image.fromarray(gmask, "L")
gloss_full = Image.new("RGBA", (W, W), (255, 255, 255, 255))
gloss = Image.composite(gloss_full, Image.new("RGBA", (W, W), (0, 0, 0, 0)), gmask_img)
gloss.putalpha(Image.composite(gmask_img, Image.new("L", (W, W), 0), mask))
img = Image.alpha_composite(img, gloss)

# ---------- 6. 缩回 1024 并导出 ----------
final = img.resize((S, S), Image.LANCZOS)
png_path = OUT_DIR / "noteflow-icon.png"
final.save(png_path)

ico_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
ico_path = OUT_DIR / "NoteFlow.ico"
final.save(ico_path, format="ICO", sizes=ico_sizes)

exe_dir = Path(r"D:\NoteFlow-Demo\app\NoteFlow")
deployed = None
if exe_dir.is_dir():
    deployed = exe_dir / "NoteFlow.ico"
    shutil.copyfile(ico_path, deployed)

print("PNG :", png_path, png_path.stat().st_size, "B")
print("ICO :", ico_path, ico_path.stat().st_size, "B")
print("copy:", deployed)
