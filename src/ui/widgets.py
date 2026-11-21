"""自绘控件：圆角卡片、按钮、模式开关、进度条、拖入区、日志与失败面板。

只依赖 tkinter/ttk，不引入第三方 UI 库；圆角、虚线框都用 Canvas 画。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from . import theme as T


def separator(master, color: str = T.BORDER) -> tk.Frame:
    line = tk.Frame(master, bg=color, height=T.px(1))
    line.pack(fill="x")
    return line


def elide(text: str, limit: int = 46) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    keep = max(limit - 1, 8)
    return text[: keep // 2] + "…" + text[-(keep - keep // 2) :]


class Card(tk.Frame):
    """圆角白卡片：Canvas 画底，内容放在 body 里。

    卡片高度由 body 的内容自然撑开；需要拉伸时 pack/grid 加 expand 即可。
    """

    def __init__(
        self,
        master,
        padding: int = 16,
        radius: int = 12,
        fill: str = T.CARD,
        border: str = T.BORDER,
        **kwargs,
    ) -> None:
        self._fill = fill
        self._border = border
        self._radius = T.px(radius)
        super().__init__(master, bg=T.parent_bg(master), bd=0, highlightthickness=0, **kwargs)
        self._canvas = tk.Canvas(self, bg=T.parent_bg(master), highlightthickness=0, bd=0, width=1, height=1)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self.body = tk.Frame(self, bg=fill)
        self.body.grid(row=0, column=0, sticky="nsew", padx=T.px(padding), pady=T.px(padding))
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._canvas.bind("<Configure>", self._redraw)

    def _redraw(self, event=None) -> None:
        width = event.width if event else self._canvas.winfo_width()
        height = event.height if event else self._canvas.winfo_height()
        inset = T.px(1)
        if width <= inset * 2 or height <= inset * 2:
            return
        self._canvas.delete("shape")
        T.draw_round_rect(
            self._canvas,
            inset,
            inset,
            width - inset,
            height - inset,
            self._radius,
            fill=self._fill,
            outline=self._border,
            width=inset,
            tags="shape",
        )
        self._canvas.tag_lower("shape")


class RoundButton(tk.Canvas):
    """圆角按钮：primary / secondary / soft / danger / ghost / violet / teal / amber。

    彩色实底样式（violet / teal / amber）与 primary 一样是白字粗体；icon 传名称即可在
    文字前画与文字同色的小图标，可选 obsidian / ima / file / package / mindmap / clipboard /
    folder / broom / plug / log / save。
    """

    KINDS = {
        "primary": {"fill": T.ACCENT, "hover": T.ACCENT_HOVER, "text": "#FFFFFF", "outline": "", "bold": True},
        "secondary": {"fill": T.CARD, "hover": "#F2F5FD", "text": T.INK, "outline": T.BORDER_STRONG, "bold": False},
        "soft": {"fill": T.ACCENT_SOFT, "hover": "#DCE8FF", "text": T.ACCENT, "outline": "", "bold": True},
        "danger": {"fill": T.DANGER_SOFT, "hover": "#FDE4E1", "text": T.DANGER, "outline": "#F5C6C1", "bold": False},
        "ghost": {"fill": None, "hover": "#EEF2FA", "text": T.MUTED, "outline": "", "bold": False},
        "violet": {
            "fill": T.OBSIDIAN,
            "hover": T.OBSIDIAN_HOVER,
            "text": "#FFFFFF",
            "outline": "",
            "bold": True,
            "ring": "#C4B5FD",
        },
        "teal": {
            "fill": T.IMA,
            "hover": T.IMA_HOVER,
            "text": "#FFFFFF",
            "outline": "",
            "bold": True,
            "ring": "#99F6E4",
        },
        "amber": {
            "fill": T.AMBER,
            "hover": T.AMBER_HOVER,
            "text": "#FFFFFF",
            "outline": "",
            "bold": True,
            "ring": "#FDE68A",
        },
    }

    ICON_SIZE = 15  # 逻辑像素：图标方框边长
    ICON_GAP = 7  # 图标与文字的间距
    # 合法图标名，与 _draw_icon 支持的分支保持一致
    ICONS = frozenset(
        {
            "obsidian",
            "ima",
            "file",
            "package",
            "mindmap",
            "clipboard",
            "folder",
            "broom",
            "plug",
            "log",
            "save",
            "book",
        }
    )

    def __init__(
        self,
        master,
        text: str,
        command=None,
        kind: str = "secondary",
        height: int = 38,
        radius: int = 10,
        font_size: int = 10,
        padx: int = 18,
        width: int | None = None,
        icon: str | None = None,
    ) -> None:
        self._parent_bg = T.parent_bg(master)
        self._height = T.px(height)
        self._min_width = T.px(64)
        super().__init__(
            master,
            bg=self._parent_bg,
            highlightthickness=0,
            bd=0,
            height=self._height,
            takefocus=1,
            cursor="hand2",
        )
        self._text = text
        self._command = command
        self._kind = kind
        self._icon = icon
        if icon is not None and icon not in self.ICONS:
            raise ValueError(f"未知图标名 {icon!r}，可选：{'、'.join(sorted(self.ICONS))}")
        self._radius = T.px(radius)
        self._font = T.f(font_size, "bold" if self.KINDS.get(kind, {}).get("bold") else "normal")
        self._padx = T.px(padx)
        self._fixed_width = T.px(width) if width else None
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._focused = False
        self._apply_width()

        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._on_focus)
        self.bind("<FocusOut>", self._on_focus)
        self.bind("<Return>", lambda _e: self._activate())
        self.bind("<space>", lambda _e: self._activate())

    # ---- 外部接口 ----

    def set_text(self, text: str) -> None:
        self._text = text
        self._apply_width()
        self._redraw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._redraw()

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self._redraw()

    # ---- 内部 ----

    def _apply_width(self) -> None:
        if self._fixed_width:
            self.configure(width=self._fixed_width)
            return
        width = tkfont.Font(font=self._font).measure(self._text) + self._padx * 2
        if self._icon:
            width += T.px(self.ICON_SIZE) + T.px(self.ICON_GAP)
        self.configure(width=max(width, self._min_width))

    def _size(self) -> tuple[int, int]:
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            width = int(self.cget("width"))
        if height <= 1:
            height = int(self.cget("height"))
        return width, height

    def _redraw(self) -> None:
        self.delete("all")
        width, height = self._size()
        inset = T.px(1)
        spec = self.KINDS.get(self._kind, self.KINDS["secondary"])

        if not self._enabled:
            fill, text_color = T.DISABLED_FILL, T.DISABLED_INK
            outline = T.DISABLED_FILL
        elif self._hover or self._pressed:
            fill, text_color = spec["hover"], spec["text"]
            outline = spec["outline"] or spec["hover"]
        else:
            fill = spec["fill"] if spec["fill"] is not None else self._parent_bg
            text_color, outline = spec["text"], spec["outline"]

        T.draw_round_rect(
            self,
            inset,
            inset,
            width - inset,
            height - inset,
            self._radius,
            fill=fill,
            outline=outline or fill,
            width=inset,
        )
        if self._focused and self._enabled:
            ring = T.px(2)
            T.draw_round_rect(
                self,
                ring,
                ring,
                width - ring,
                height - ring,
                max(self._radius - T.px(1), T.px(2)),
                fill="",
                outline=spec.get("ring", T.ACCENT),
                width=inset,
            )

        if self._icon:
            icon_size = T.px(self.ICON_SIZE)
            gap = T.px(self.ICON_GAP)
            text_width = tkfont.Font(font=self._font).measure(self._text)
            left = (width - (icon_size + gap + text_width)) / 2
            self._draw_icon(left, (height - icon_size) / 2, icon_size, text_color, fill)
            self.create_text(
                left + icon_size + gap,
                height / 2,
                text=self._text,
                anchor="w",
                fill=text_color,
                font=self._font,
            )
        else:
            self.create_text(width / 2, height / 2, text=self._text, fill=text_color, font=self._font)

    # ---- 图标：按 icon 方框内的相对坐标画，跟字号缩放无关 ----

    def _draw_icon(self, x: float, y: float, size: float, color: str, bg: str) -> None:
        if self._icon == "obsidian":
            self._draw_obsidian_icon(x, y, size, color)
        elif self._icon == "ima":
            self._draw_ima_icon(x, y, size, color)
        elif self._icon == "file":
            self._draw_file_icon(x, y, size, color, bg)
        elif self._icon == "package":
            self._draw_package_icon(x, y, size, color, bg)
        elif self._icon == "mindmap":
            self._draw_mindmap_icon(x, y, size, color)
        elif self._icon == "clipboard":
            self._draw_clipboard_icon(x, y, size, color, bg)
        elif self._icon == "folder":
            self._draw_folder_icon(x, y, size, color, bg)
        elif self._icon == "broom":
            self._draw_broom_icon(x, y, size, color, bg)
        elif self._icon == "plug":
            self._draw_plug_icon(x, y, size, color)
        elif self._icon == "log":
            self._draw_log_icon(x, y, size, color)
        elif self._icon == "save":
            self._draw_save_icon(x, y, size, color, bg)
        elif self._icon == "book":
            self._draw_book_icon(x, y, size, color, bg)

    def _draw_obsidian_icon(self, x: float, y: float, size: float, color: str) -> None:
        """Obsidian 水晶标：上方三角棱面 + 下方三根向下内收的棱柱。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        self.create_polygon(*pt(0.5, 0.0), *pt(0.94, 0.3), *pt(0.06, 0.3), fill=color, outline="")
        self.create_polygon(*pt(0.02, 0.42), *pt(0.26, 0.42), *pt(0.4, 1.0), *pt(0.18, 1.0), fill=color, outline="")
        self.create_polygon(*pt(0.35, 0.42), *pt(0.65, 0.42), *pt(0.5, 0.94), fill=color, outline="")
        self.create_polygon(*pt(0.98, 0.42), *pt(0.74, 0.42), *pt(0.6, 1.0), *pt(0.82, 1.0), fill=color, outline="")

    def _draw_ima_icon(self, x: float, y: float, size: float, color: str) -> None:
        """ima 星芒：四角闪光，内凹边。"""
        center_x = x + size / 2
        center_y = y + size / 2
        radius = size / 2
        inner = radius * 0.27
        points = (
            (center_x, center_y - radius),
            (center_x + inner, center_y - inner),
            (center_x + radius, center_y),
            (center_x + inner, center_y + inner),
            (center_x, center_y + radius),
            (center_x - inner, center_y + inner),
            (center_x - radius, center_y),
            (center_x - inner, center_y - inner),
        )
        self.create_polygon(*[coord for point in points for coord in point], fill=color, outline="")

    def _draw_file_icon(self, x: float, y: float, size: float, color: str, bg: str) -> None:
        """单篇 .md：折角文档纸。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        self.create_polygon(
            *pt(0.14, 0.0), *pt(0.60, 0.0), *pt(0.88, 0.28), *pt(0.88, 1.0), *pt(0.14, 1.0), fill=color, outline=""
        )
        self.create_polygon(*pt(0.60, 0.0), *pt(0.60, 0.28), *pt(0.88, 0.28), fill=bg, outline="")
        self.create_line(*pt(0.28, 0.50), *pt(0.74, 0.50), fill=bg, width=T.px(1.5))
        self.create_line(*pt(0.28, 0.74), *pt(0.74, 0.74), fill=bg, width=T.px(1.5))

    def _draw_package_icon(self, x: float, y: float, size: float, color: str, bg: str) -> None:
        """整库 .zip：盒盖 + 盒身 + 中间胶带。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        self.create_polygon(*pt(0.04, 0.16), *pt(0.96, 0.16), *pt(0.96, 0.40), *pt(0.04, 0.40), fill=color, outline="")
        self.create_polygon(*pt(0.10, 0.48), *pt(0.90, 0.48), *pt(0.90, 1.0), *pt(0.10, 1.0), fill=color, outline="")
        self.create_rectangle(*pt(0.42, 0.48), *pt(0.58, 1.0), fill=bg, outline="")

    def _draw_mindmap_icon(self, x: float, y: float, size: float, color: str) -> None:
        """HTML 脑图：上面一个节点、下面两个节点的最小树。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        self.create_line(*pt(0.5, 0.30), *pt(0.22, 0.74), fill=color, width=T.px(1.5))
        self.create_line(*pt(0.5, 0.30), *pt(0.78, 0.74), fill=color, width=T.px(1.5))
        for cu, cv in ((0.5, 0.22), (0.22, 0.82), (0.78, 0.82)):
            self.create_oval(*pt(cu - 0.16, cv - 0.16), *pt(cu + 0.16, cv + 0.16), fill=color, outline="")

    def _draw_clipboard_icon(self, x: float, y: float, size: float, color: str, bg: str) -> None:
        """复制到剪贴板：写字板 + 两行文字。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        self.create_polygon(*pt(0.16, 0.14), *pt(0.84, 0.14), *pt(0.84, 1.0), *pt(0.16, 1.0), fill=color, outline="")
        self.create_polygon(*pt(0.34, 0.02), *pt(0.66, 0.02), *pt(0.66, 0.22), *pt(0.34, 0.22), fill=color, outline="")
        self.create_line(*pt(0.30, 0.46), *pt(0.70, 0.46), fill=bg, width=T.px(1.5))
        self.create_line(*pt(0.30, 0.68), *pt(0.70, 0.68), fill=bg, width=T.px(1.5))

    def _draw_folder_icon(self, x: float, y: float, size: float, color: str, bg: str) -> None:
        """打开笔记库 / 浏览：文件夹——后页带标签凸起，前页盖住下半部。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        self.create_polygon(
            *pt(0.05, 0.10),
            *pt(0.37, 0.10),
            *pt(0.47, 0.28),
            *pt(0.95, 0.28),
            *pt(0.95, 0.42),
            *pt(0.05, 0.42),
            fill=color,
            outline="",
        )
        self.create_polygon(
            *pt(0.05, 0.50), *pt(0.95, 0.50), *pt(0.95, 0.92), *pt(0.05, 0.92), fill=color, outline=""
        )

    def _draw_broom_icon(self, x: float, y: float, size: float, color: str, bg: str) -> None:
        """清理本地缓存：扫帚——竖柄 + 外扩刷头 + 束腰与刷毛纹。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        self.create_line(*pt(0.50, 0.02), *pt(0.50, 0.42), fill=color, width=T.px(2.4))
        self.create_polygon(
            *pt(0.30, 0.46), *pt(0.70, 0.46), *pt(0.86, 0.96), *pt(0.14, 0.96), fill=color, outline=""
        )
        self.create_line(*pt(0.31, 0.56), *pt(0.69, 0.56), fill=bg, width=T.px(1.5))
        self.create_line(*pt(0.36, 0.66), *pt(0.30, 0.92), fill=bg, width=T.px(1.5))
        self.create_line(*pt(0.50, 0.66), *pt(0.50, 0.92), fill=bg, width=T.px(1.5))
        self.create_line(*pt(0.64, 0.66), *pt(0.70, 0.92), fill=bg, width=T.px(1.5))

    def _draw_plug_icon(self, x: float, y: float, size: float, color: str) -> None:
        """测试连接：电源插头——两只插脚 + 插身 + 下垂电线。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        self.create_rectangle(*pt(0.28, 0.02), *pt(0.40, 0.22), fill=color, outline="")
        self.create_rectangle(*pt(0.60, 0.02), *pt(0.72, 0.22), fill=color, outline="")
        self.create_rectangle(*pt(0.16, 0.22), *pt(0.84, 0.56), fill=color, outline="")
        self.create_line(*pt(0.50, 0.56), *pt(0.50, 0.98), fill=color, width=T.px(2.4))

    def _draw_log_icon(self, x: float, y: float, size: float, color: str) -> None:
        """打开清理日志：三行带项目符号的日志清单。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        for v in (0.22, 0.50, 0.78):
            self.create_oval(*pt(0.08, v - 0.08), *pt(0.22, v + 0.08), fill=color, outline="")
            self.create_line(*pt(0.32, v), *pt(0.94, v), fill=color, width=T.px(1.8))

    def _draw_save_icon(self, x: float, y: float, size: float, color: str, bg: str) -> None:
        """保存：软盘——方身 + 顶部滑片缺口 + 下方标签。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        T.draw_round_rect(self, *pt(0.10, 0.06), *pt(0.90, 0.94), T.px(2), fill=color, outline="")
        self.create_rectangle(*pt(0.34, 0.06), *pt(0.66, 0.28), fill=bg, outline="")
        self.create_rectangle(*pt(0.24, 0.52), *pt(0.76, 0.90), fill=bg, outline="")
        self.create_line(*pt(0.30, 0.64), *pt(0.70, 0.64), fill=color, width=T.px(1.5))

    def _draw_book_icon(self, x: float, y: float, size: float, color: str, bg: str) -> None:
        """说明书：摊开的书——左右两页 + 中缝 + 页内文字线。"""

        def pt(u: float, v: float) -> tuple[float, float]:
            return x + u * size, y + v * size

        self.create_polygon(
            *pt(0.04, 0.16), *pt(0.47, 0.23), *pt(0.47, 0.92), *pt(0.04, 0.84), fill=color, outline=""
        )
        self.create_polygon(
            *pt(0.53, 0.23), *pt(0.96, 0.16), *pt(0.96, 0.84), *pt(0.53, 0.92), fill=color, outline=""
        )
        for v in (0.44, 0.60, 0.74):
            end = 0.38 if v < 0.7 else 0.30
            self.create_line(*pt(0.14, v), *pt(end, v + 0.03), fill=bg, width=T.px(1.4))
            end = 0.62 if v < 0.7 else 0.70
            self.create_line(*pt(end, v + 0.03), *pt(0.86, v - 0.03), fill=bg, width=T.px(1.4))

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event=None) -> None:
        if not self._enabled:
            return
        self.focus_set()
        self._pressed = True
        self._redraw()

    def _on_release(self, event=None) -> None:
        if not self._enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if not was_pressed or event is None:
            return
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self._activate()

    def _on_focus(self, _event=None) -> None:
        self._focused = self.focus_get() is self
        self._redraw()

    def _activate(self) -> None:
        if self._enabled and self._command is not None:
            self._command()


class ModeSwitch(tk.Canvas):
    """真实识别 / 离线示例 分段开关。当前模式一眼可辨。"""

    def __init__(self, master, on_change, mode: str = "offline", width: int = 236, height: int = 36) -> None:
        self._parent_bg = T.parent_bg(master)
        super().__init__(
            master,
            bg=self._parent_bg,
            highlightthickness=0,
            bd=0,
            width=T.px(width),
            height=T.px(height),
            cursor="hand2",
        )
        self._on_change = on_change
        self._mode = mode
        self._enabled = True
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", self._on_click)

    def set_mode(self, mode: str) -> None:
        self._mode = "real" if mode == "real" else "offline"
        self._redraw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")

    def _on_click(self, event) -> None:
        if not self._enabled:
            return
        mode = "real" if event.x < self.winfo_width() / 2 else "offline"
        if mode != self._mode:
            self._on_change(mode)

    def _redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 2)
        height = max(self.winfo_height(), 2)
        if width <= 2:
            return
        border = T.px(1)
        gap = T.px(3)
        T.draw_round_rect(
            self, border, border, width - border, height - border, height / 2,
            fill="#EFF2F9", outline=T.BORDER, width=border,
        )
        half = width / 2
        selected_real = self._mode == "real"
        x1 = gap if selected_real else half + gap / 3
        x2 = half - gap / 3 if selected_real else width - gap
        T.draw_round_rect(
            self, x1, gap, x2, height - gap, max((height - gap * 2) / 2, T.px(4)),
            fill=T.CARD, outline=T.BORDER_STRONG if not selected_real else "#C9D9FF", width=border,
        )

        font_selected = T.f(9, "bold")
        font_normal = T.f(9)
        dot = T.px(6)
        dot_gap = T.px(6)
        for index, (label, mode, color) in enumerate(
            (("真实识别", "real", T.ACCENT), ("离线示例", "offline", T.WARN))
        ):
            center = half / 2 if index == 0 else half + half / 2
            active = (mode == "real") == selected_real
            text_color = color if active else T.MUTED
            font_spec = font_selected if active else font_normal
            text_width = tkfont.Font(font=font_spec).measure(label)
            start = center - (text_width + dot + dot_gap) / 2
            self.create_oval(
                start, height / 2 - dot / 2, start + dot, height / 2 + dot / 2,
                fill=color if active else T.MUTED_LIGHT, outline="",
            )
            self.create_text(
                start + dot + dot_gap, height / 2, text=label, anchor="w", fill=text_color, font=font_spec
            )


class ProgressBar(tk.Canvas):
    """细圆角进度条；value=None 时显示不确定态的来回块。"""

    def __init__(self, master, height: int = 8) -> None:
        self._height = T.px(height)
        super().__init__(master, bg=T.parent_bg(master), highlightthickness=0, bd=0, height=self._height)
        self._value: float | None = 0.0
        self._offset = 0.0
        self._job = None
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_value(self, value: float | None) -> None:
        self._value = None if value is None else max(0.0, min(1.0, float(value)))
        if self._value is None:
            self._start_animation()
        else:
            self._stop_animation()
        self._redraw()

    def _start_animation(self) -> None:
        if self._job is not None:
            return
        self._tick()

    def _tick(self) -> None:
        if self._value is not None or not self.winfo_exists():
            self._job = None
            return
        self._offset = (self._offset + 0.035) % 1.0
        self._redraw()
        self._job = self.after(40, self._tick)

    def _stop_animation(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self._height
        if width <= 2:
            return
        radius = height / 2
        T.draw_round_rect(self, 0, 0, width, height, radius, fill="#EDF1F8", outline="")
        if self._value is None:
            block = max(width * 0.28, T.px(40))
            x = -block + (width + block) * self._offset
            T.draw_round_rect(self, x, 0, x + block, height, radius, fill=T.ACCENT, outline="")
        elif self._value > 0:
            filled = max(width * self._value, height)
            T.draw_round_rect(self, 0, 0, filled, height, radius, fill=T.ACCENT, outline="")


class DropZone(tk.Frame):
    """虚线拖入区：整块可点击选文件，也接受系统拖拽（由外部挂 windnd）。"""

    def __init__(self, master, on_pick_files, on_pick_folder, radius: int = 14) -> None:
        self._bg = T.parent_bg(master)
        super().__init__(master, bg=self._bg, bd=0, highlightthickness=0)
        self._radius = T.px(radius)
        self._enabled = True
        self._hover = False

        self._canvas = tk.Canvas(self, bg=self._bg, highlightthickness=0, bd=0, width=1, height=1)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self.body = tk.Frame(self, bg=T.CARD)
        self.body.grid(row=0, column=0, sticky="nsew", padx=T.px(22), pady=T.px(20))
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        icon_size = T.px(48)
        self.icon = tk.Canvas(self.body, bg=T.CARD, highlightthickness=0, bd=0, width=icon_size, height=icon_size)
        self.icon.pack(pady=(0, T.px(8)))
        self.title = tk.Label(
            self.body, text="把笔记拖到这里", bg=T.CARD, fg=T.INK, font=T.f(12, "bold")
        )
        self.title.pack()
        self.hint = tk.Label(
            self.body,
            text="支持 图片 / PDF / GoodNotes 导出包 · 拖文件夹会设为收件箱",
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(9),
            wraplength=T.px(460),
        )
        self.hint.pack(pady=(T.px(4), T.px(10)))

        buttons = tk.Frame(self.body, bg=T.CARD)
        buttons.pack()
        self.btn_files = RoundButton(buttons, "选择文件", command=on_pick_files, kind="secondary", height=34, font_size=9)
        self.btn_files.pack(side="left", padx=(0, T.px(8)))
        self.btn_folder = RoundButton(
            buttons, "选择文件夹", command=on_pick_folder, kind="secondary", height=34, font_size=9
        )
        self.btn_folder.pack(side="left")

        for widget in (self._canvas, self.title, self.hint):
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", lambda _e: self._pick(on_pick_files))
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)
        self.body.bind("<Enter>", self._on_enter)
        self.body.bind("<Leave>", self._on_leave)

        self._canvas.bind("<Configure>", lambda _e: self._redraw())
        self._draw_icon()
        self.after_idle(self._redraw)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.btn_files.set_enabled(enabled)
        self.btn_folder.set_enabled(enabled)
        for widget in (self._canvas, self.title, self.hint):
            widget.configure(cursor="hand2" if enabled else "arrow")
        self._redraw()

    def set_hint(self, text: str) -> None:
        self.hint.configure(text=text)

    def _pick(self, callback) -> None:
        if self._enabled and callback is not None:
            callback()

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, event=None) -> None:
        # 子控件之间移动也会触发 Leave，指针还在区域内就不取消高亮
        try:
            x, y = self.winfo_pointerxy()
            widget = self.winfo_containing(x, y)
            if widget is not None and (widget is self or str(widget).startswith(str(self))):
                return
        except tk.TclError:
            pass
        self._hover = False
        self._redraw()

    def _redraw(self) -> None:
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        if width <= 2 or height <= 2:
            return
        self._canvas.delete("all")
        fill = "#FBFCFF" if (self._hover and self._enabled) else T.CARD
        outline = T.ACCENT if (self._hover and self._enabled) else "#C9D3E6"
        inset = T.px(1)
        T.draw_round_rect(self._canvas, inset, inset, width - inset, height - inset, self._radius, fill=fill, outline="")
        dash_inset = T.px(3)
        points = T.round_points(
            dash_inset, dash_inset, width - dash_inset, height - dash_inset, self._radius - T.px(2), steps=10
        )
        points.extend(points[:2])
        self._canvas.create_line(
            *points,
            fill=outline,
            width=T.px(1),
            dash=() if (self._hover and self._enabled) else (T.px(6), T.px(5)),
            smooth=True,
        )
        self._canvas.tag_lower("all")

    def _draw_icon(self) -> None:
        canvas = self.icon
        k = T.scale()

        def s(value: float) -> float:
            return value * k

        pad = T.ACCENT_SOFT
        T.draw_round_rect(canvas, s(8), s(3), s(30), s(37), s(6), fill="#FFFFFF", outline="#C7D2E8", width=T.px(2))
        canvas.create_line(s(23), s(3), s(30), s(10), fill="#C7D2E8", width=T.px(2))
        for y in (17, 23, 29):
            canvas.create_line(s(14), s(y), s(24 if y < 29 else 20), s(y), fill="#C7D2E8", width=T.px(2))
        canvas.create_oval(s(27), s(27), s(45), s(45), fill=pad, outline="")
        canvas.create_line(s(36), s(31), s(36), s(40), fill=T.ACCENT, width=T.px(2))
        canvas.create_line(s(32), s(36), s(36), s(40), s(40), s(36), fill=T.ACCENT, width=T.px(2))


class LogPanel(tk.Frame):
    """深色日志控制台，按级别着色，行数有上限。"""

    def __init__(self, master, height: int = 12) -> None:
        super().__init__(master, bg=T.CARD)
        header = tk.Frame(self, bg=T.CARD)
        header.pack(fill="x")
        tk.Label(header, text="运行日志", bg=T.CARD, fg=T.INK, font=T.f(11, "bold")).pack(side="left")
        self._autoscroll = True
        self.btn_clear = RoundButton(header, "清空", command=self.clear, kind="ghost", height=26, font_size=8, padx=10)
        self.btn_clear.pack(side="right")
        self.btn_copy = RoundButton(
            header, "复制", command=self.copy, kind="ghost", height=26, font_size=8, padx=10
        )
        self.btn_copy.pack(side="right", padx=(0, 4))

        wrapper = tk.Frame(self, bg=T.LOG_BG)
        wrapper.pack(fill="both", expand=True, pady=(T.px(8), 0))
        wrapper.grid_rowconfigure(0, weight=1)
        wrapper.grid_columnconfigure(0, weight=1)

        self.text = tk.Text(
            wrapper,
            height=height,
            wrap="word",
            font=T.mono(9),
            bg=T.LOG_BG,
            fg=T.LOG_INK,
            insertbackground=T.LOG_INK,
            relief="flat",
            bd=0,
            padx=T.px(12),
            pady=T.px(10),
            state="disabled",
            highlightthickness=0,
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = tk.Scrollbar(
            wrapper,
            command=self.text.yview,
            bg=T.LOG_BG,
            troughcolor="#111A2E",
            activebackground="#243049",
            highlightthickness=0,
            bd=0,
            width=T.px(10),
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)

        self.text.tag_configure("INFO", foreground=T.LOG_INK)
        self.text.tag_configure("DEBUG", foreground="#7D8AA3")
        self.text.tag_configure("WARNING", foreground="#FBBF24")
        self.text.tag_configure("ERROR", foreground="#FB7185")
        self.text.tag_configure("SUCCESS", foreground="#4ADE80")
        self.text.tag_configure("MUTED", foreground="#7D8AA3")

    def append(self, level: str, line: str) -> None:
        tag = level if level in ("INFO", "DEBUG", "WARNING", "ERROR", "SUCCESS") else "INFO"
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n", tag)
        self.text.configure(state="disabled")
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 1500:
            self.text.configure(state="normal")
            self.text.delete("1.0", "400.0")
            self.text.configure(state="disabled")
        if self._autoscroll:
            self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def copy(self) -> None:
        content = self.text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(content)


class FailurePanel(tk.Frame):
    """失败项列表：名称 + 中文原因，最多展示 3 条，可一键全部重试。"""

    MAX_ROWS = 3

    def __init__(self, master, on_retry_all) -> None:
        super().__init__(master, bg=T.CARD)
        self._on_retry_all = on_retry_all
        header = tk.Frame(self, bg=T.CARD)
        header.pack(fill="x")
        tk.Label(header, text="失败项", bg=T.CARD, fg=T.INK, font=T.f(11, "bold")).pack(side="left")
        self.count_label = tk.Label(header, text="0", bg=T.CARD, fg=T.MUTED, font=T.f(9))
        self.count_label.pack(side="left", padx=(T.px(6), 0))
        self.btn_retry = RoundButton(
            header, "全部重试", command=on_retry_all, kind="ghost", height=26, font_size=8, padx=10
        )
        self.btn_retry.pack(side="right")
        self.btn_retry.set_enabled(False)

        self.rows = tk.Frame(self, bg=T.CARD)
        self.rows.pack(fill="x", pady=(T.px(6), 0))
        self.footer = tk.Label(self, text="", bg=T.CARD, fg=T.MUTED_LIGHT, font=T.f(8), anchor="w")
        self._empty = tk.Label(
            self.rows, text="暂无失败项，处理结果都写进了笔记库。", bg=T.CARD, fg=T.MUTED_LIGHT, font=T.f(9), anchor="w"
        )
        self._empty.pack(fill="x", pady=T.px(2))
        self._items: list[dict] = []

    def set_items(self, items: list[dict]) -> None:
        self._items = list(items)
        for child in self.rows.winfo_children():
            child.destroy()
        if not items:
            self._empty = tk.Label(
                self.rows,
                text="暂无失败项，处理结果都写进了笔记库。",
                bg=T.CARD,
                fg=T.MUTED_LIGHT,
                font=T.f(9),
                anchor="w",
            )
            self._empty.pack(fill="x", pady=T.px(2))
            self.count_label.configure(text="0", fg=T.MUTED)
            self.btn_retry.set_enabled(False)
            self.footer.configure(text="")
            return

        self.count_label.configure(text=f"{len(items)} 项", fg=T.DANGER)
        self.btn_retry.set_enabled(True)
        for item in items[: self.MAX_ROWS]:
            row = tk.Frame(self.rows, bg=T.CARD)
            row.pack(fill="x", pady=(0, T.px(6)))
            name = item.get("name", "未知文件")
            page = item.get("page")
            title = f"{name} · 第 {page} 页" if page else name
            tk.Label(row, text=elide(title, 56), bg=T.CARD, fg=T.INK_SOFT, font=T.f(9, "bold"), anchor="w").pack(
                fill="x"
            )
            tk.Label(
                row,
                text=item.get("reason", "处理失败"),
                bg=T.CARD,
                fg=T.MUTED,
                font=T.f(9),
                anchor="w",
                justify="left",
                wraplength=T.px(420),
            ).pack(fill="x")
        extra = len(items) - self.MAX_ROWS
        self.footer.configure(text=f"另有 {extra} 项未显示，完整记录见 failures.jsonl" if extra > 0 else "")

    def set_retry_enabled(self, enabled: bool) -> None:
        self.btn_retry.set_enabled(enabled and bool(self._items))


class Banner(tk.Frame):
    """顶部提示条：配置修正、目录不可写、缺少 Key 之类都放这里。"""

    COLORS = {
        "info": (T.ACCENT_SOFT, T.ACCENT),
        "warn": (T.WARN_SOFT, T.WARN),
        "error": (T.DANGER_SOFT, T.DANGER),
        "success": (T.SUCCESS_SOFT, T.SUCCESS),
    }

    def __init__(self, master) -> None:
        super().__init__(master, bg=T.BG)
        self._label = tk.Label(
            self, text="", bg=T.ACCENT_SOFT, fg=T.ACCENT, font=T.f(9), anchor="w", justify="left",
            padx=T.px(12), pady=T.px(8),
        )
        self._label.pack(fill="x")
        self._job = None
        self._visible = False
        self._before = None

    def set_anchor(self, widget) -> None:
        """提示条出现时要插在哪个控件之前（保持视觉顺序）。"""
        self._before = widget

    def show(self, text: str, level: str = "info", auto_hide_ms: int = 0) -> None:
        bg, fg = self.COLORS.get(level, self.COLORS["info"])
        self._label.configure(text=text, bg=bg, fg=fg)
        self.pack(fill="x", padx=T.px(20), pady=(0, T.px(8)), before=self._before)
        self._visible = True
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if auto_hide_ms:
            self._job = self.after(auto_hide_ms, self.hide)

    def hide(self) -> None:
        self._job = None
        self._visible = False
        self.pack_forget()
