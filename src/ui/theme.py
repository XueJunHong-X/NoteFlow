"""视觉令牌与 ttk 样式：所有颜色、字号、圆角都从这里取，控件里不写死。"""

from __future__ import annotations

import ctypes
import math
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ---- 色板 ----
BG = "#F5F7FB"
CARD = "#FFFFFF"
BORDER = "#E6E9F2"
BORDER_STRONG = "#D5DBEA"
INK = "#101828"
INK_SOFT = "#344054"
MUTED = "#667085"
MUTED_LIGHT = "#98A2B3"

ACCENT = "#2F6BFF"
ACCENT_HOVER = "#1F55E0"
ACCENT_SOFT = "#EAF0FF"
SUCCESS = "#12B76A"
SUCCESS_SOFT = "#E7F8F0"
WARN = "#D97706"
WARN_SOFT = "#FFF4E5"
DANGER = "#D92D20"
DANGER_SOFT = "#FEF3F2"
DISABLED_FILL = "#EEF1F7"
DISABLED_INK = "#A4ADBF"

LOG_BG = "#0B1220"
LOG_TRACK = "#16203A"
LOG_INK = "#CBD5E1"

FAMILY = "Microsoft YaHei UI"
MONO = "Consolas"


def f(size: int = 10, weight: str = "normal") -> tuple:
    return (FAMILY, size, weight)


def mono(size: int = 9) -> tuple:
    return (MONO, size, "normal")


def parent_bg(widget) -> str:
    """取父容器底色，用于圆角外的留白；ttk 控件没有 bg 选项时退回全局底色。"""
    try:
        return widget.cget("bg")
    except tk.TclError:
        return BG


def enable_dpi_awareness() -> None:
    """让高分屏下的文字保持清晰；必须在创建 Tk 之前调用。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def ui_scale(root: tk.Misc) -> float:
    try:
        return max(1.0, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


_SCALE = 1.0


def set_scale(root: tk.Misc) -> float:
    """记录像素缩放比：高分屏下字号由 Tk 自动放大，间距/尺寸要按这个比例补。"""
    global _SCALE
    _SCALE = ui_scale(root)
    return _SCALE


def scale() -> float:
    return _SCALE


def px(value: float) -> int:
    """逻辑像素 -> 当前屏幕像素。"""
    return max(1, int(round(value * _SCALE)))


def round_points(x1: float, y1: float, x2: float, y2: float, r: float, steps: int = 8) -> list[float]:
    """生成圆角矩形的顶点序列，用 create_polygon 直接画。"""
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    points: list[float] = []
    corners = (
        (x2 - r, y1 + r, -90, 0),
        (x2 - r, y2 - r, 0, 90),
        (x1 + r, y2 - r, 90, 180),
        (x1 + r, y1 + r, 180, 270),
    )
    for cx, cy, start, end in corners:
        for i in range(steps + 1):
            angle = math.radians(start + (end - start) * i / steps)
            points.extend((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return points


def draw_round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
    """在 Canvas 上画圆角矩形，返回 item id。"""
    return canvas.create_polygon(round_points(x1, y1, x2, y2, r), **kwargs)


def apply_ttk(root: tk.Misc) -> ttk.Style:
    """统一 ttk 外观，尽量贴近现代扁平风格。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=BG, foreground=INK, font=f(10))
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)

    style.configure("TLabel", background=BG, foreground=INK, font=f(10))
    style.configure("Card.TLabel", background=CARD, foreground=INK, font=f(10))
    style.configure("Title.TLabel", background=CARD, foreground=INK, font=f(15, "bold"))
    style.configure("H2.TLabel", background=CARD, foreground=INK, font=f(11, "bold"))
    style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=f(9))
    style.configure("MutedBg.TLabel", background=BG, foreground=MUTED, font=f(9))
    style.configure("Caption.TLabel", background=CARD, foreground=MUTED_LIGHT, font=f(8))

    style.configure(
        "TEntry",
        fieldbackground=CARD,
        foreground=INK,
        bordercolor=BORDER_STRONG,
        lightcolor=BORDER_STRONG,
        darkcolor=BORDER_STRONG,
        insertcolor=INK,
        padding=(8, 7),
        font=f(10),
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", ACCENT)],
        lightcolor=[("focus", ACCENT)],
        darkcolor=[("focus", ACCENT)],
    )

    style.configure(
        "TButton",
        background=CARD,
        foreground=INK,
        bordercolor=BORDER_STRONG,
        lightcolor=BORDER_STRONG,
        darkcolor=BORDER_STRONG,
        padding=(14, 7),
        relief="flat",
        font=f(10),
    )
    style.map(
        "TButton",
        background=[("active", "#F1F5FE"), ("disabled", DISABLED_FILL)],
        foreground=[("disabled", DISABLED_INK)],
    )

    style.configure("TCheckbutton", background=CARD, foreground=INK, font=f(9))
    style.map("TCheckbutton", background=[("active", CARD)])

    style.configure("TProgressbar", background=ACCENT, troughcolor="#EDF1F8", bordercolor="#EDF1F8")

    return style


def font_measure(text: str, font_spec: tuple) -> int:
    return tkfont.Font(font=font_spec).measure(text)
