"""设置弹窗：路径、接口、Key 都在这里改，首屏不再摆一大块配置卡。"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

from src.appconfig import AppConfig
from src.controller import test_connection
from src.openers import open_manual

from . import theme as T
from .widgets import Card, RoundButton


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, config: AppConfig, on_saved) -> None:
        super().__init__(master)
        self._config = config
        self._on_saved = on_saved
        self._test_queue: queue.Queue[tuple[bool, str]] = queue.Queue()
        self._testing = False
        self._closed = False
        # 本次打开时的 Key 与刚刚测试通过的 Key：用于区分「已保存并验证」和「刚改过未验证」
        self._initial_key = (config.api_key or "").strip()
        self._tested_key: str | None = None
        self._async_test_key = ""

        self.title("设置 · NoteFlow")
        self.configure(bg=T.BG)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close())

        try:
            self._build()
            self._center(master)
        except Exception:
            self.destroy()
            raise
        self._modal_tries = 0
        self.after(0, self._activate_modal)

    def _activate_modal(self) -> None:
        """窗口真正可见后再抓取焦点，失败时重试；绝不能因为抓取失败变成隐形窗口。"""
        if self._closed:
            return
        try:
            self.grab_set()
        except tk.TclError:
            self._modal_tries += 1
            if self._modal_tries < 10:
                self.after(60, self._activate_modal)

    # ---------------- 构建 ----------------

    def _build(self) -> None:
        from tkinter import ttk

        # 200% 缩放下内容高度会超出工作区：外层放可滚动画布，保证「保存/取消」随时能滚到并点到
        self._canvas = tk.Canvas(self, bg=T.BG, highlightthickness=0, bd=0)
        self._scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._content = tk.Frame(self._canvas, bg=T.BG)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._content, anchor="nw")
        # 画布尺寸变化时同步内部窗口宽度；内容变化时只更新滚动范围
        self._canvas.bind("<Configure>", self._sync_canvas)
        self._content.bind("<Configure>", self._update_scrollregion)
        self.bind("<MouseWheel>", self._on_mousewheel)
        # 键盘 Tab 聚焦到视野外的控件时，把画布滚到能看见它的位置
        self.bind("<FocusIn>", self._on_focus_in)

        wrap = tk.Frame(self._content, bg=T.BG)
        wrap.pack(fill="both", expand=True, padx=T.px(20), pady=T.px(18))

        tk.Label(wrap, text="设置", bg=T.BG, fg=T.INK, font=T.f(15, "bold"), anchor="w").pack(fill="x")
        tk.Label(
            wrap,
            text="所有配置只保存在本机 %APPDATA%\\NoteFlow\\config.json，不会写进代码或安装包。",
            bg=T.BG,
            fg=T.MUTED,
            font=T.f(9),
            anchor="w",
        ).pack(fill="x", pady=(T.px(2), T.px(12)))

        paths = Card(wrap, padding=16)
        paths.pack(fill="x")
        body = paths.body
        tk.Label(body, text="收件箱（监听文件夹）", bg=T.CARD, fg=T.INK, font=T.f(10, "bold"), anchor="w").pack(fill="x")
        row = tk.Frame(body, bg=T.CARD)
        row.pack(fill="x", pady=(T.px(6), T.px(12)))
        self.watch_var = tk.StringVar(value=self._config.watch_dir)
        entry = self._make_entry(row, self.watch_var)
        entry.pack(side="left", fill="x", expand=True, ipady=T.px(6))
        RoundButton(
            row,
            "浏览",
            command=lambda: self._pick(self.watch_var),
            kind="soft",
            icon="folder",
            height=32,
            font_size=9,
            padx=12,
        ).pack(side="left", padx=(T.px(8), 0))

        tk.Label(body, text="笔记库（Obsidian vault）", bg=T.CARD, fg=T.INK, font=T.f(10, "bold"), anchor="w").pack(fill="x")
        row2 = tk.Frame(body, bg=T.CARD)
        row2.pack(fill="x", pady=(T.px(6), 0))
        self.vault_var = tk.StringVar(value=self._config.vault_dir)
        entry2 = self._make_entry(row2, self.vault_var)
        entry2.pack(side="left", fill="x", expand=True, ipady=T.px(6))
        RoundButton(
            row2,
            "浏览",
            command=lambda: self._pick(self.vault_var),
            kind="soft",
            icon="folder",
            height=32,
            font_size=9,
            padx=12,
        ).pack(side="left", padx=(T.px(8), 0))

        api = Card(wrap, padding=16)
        api.pack(fill="x", pady=(T.px(12), 0))
        abody = api.body
        head = tk.Frame(abody, bg=T.CARD)
        head.pack(fill="x")
        tk.Label(head, text="模型接口", bg=T.CARD, fg=T.INK, font=T.f(10, "bold")).pack(side="left")
        self.show_key = tk.BooleanVar(value=False)
        tk.Checkbutton(
            head,
            text="显示 Key",
            variable=self.show_key,
            command=self._toggle_key,
            bg=T.CARD,
            fg=T.MUTED,
            activebackground=T.CARD,
            font=T.f(8),
            bd=0,
            highlightthickness=0,
        ).pack(side="right")
        self.btn_manual = RoundButton(
            head,
            "说明书",
            command=self._open_manual,
            kind="soft",
            icon="book",
            height=28,
            font_size=8,
            padx=10,
        )
        self.btn_manual.pack(side="right", padx=(0, T.px(8)))

        # 常驻状态：重启后仍显示当前 Key 是否已验证
        status_row = tk.Frame(abody, bg=T.CARD)
        status_row.pack(fill="x", pady=(T.px(6), 0))
        self.key_status_label = tk.Label(
            status_row, text="", bg=T.CARD, fg=T.MUTED, font=T.f(9), anchor="w"
        )
        self.key_status_label.pack(side="left")

        tk.Label(abody, text="API Key", bg=T.CARD, fg=T.MUTED, font=T.f(9), anchor="w").pack(
            fill="x", pady=(T.px(8), T.px(4))
        )
        self.key_var = tk.StringVar(value=self._config.api_key)
        self.key_entry = self._make_entry(abody, self.key_var, show="•")
        self.key_entry.pack(fill="x", ipady=T.px(6))
        tk.Label(
            abody,
            text="获取 Key：阿里云百炼 bailian.console.aliyun.com ｜ 智谱 open.bigmodel.cn（详细步骤见「说明书」）",
            bg=T.CARD,
            fg=T.MUTED_LIGHT,
            font=T.f(8),
            anchor="w",
            justify="left",
            wraplength=T.px(560),
        ).pack(fill="x", pady=(T.px(4), 0))
        self.key_var.trace_add("write", self._on_key_edit)
        self._refresh_key_status()

        grid = tk.Frame(abody, bg=T.CARD)
        grid.pack(fill="x", pady=(T.px(10), 0))
        grid.grid_columnconfigure(0, weight=3)
        grid.grid_columnconfigure(1, weight=2)
        tk.Label(grid, text="接口地址", bg=T.CARD, fg=T.MUTED, font=T.f(9), anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(grid, text="模型名", bg=T.CARD, fg=T.MUTED, font=T.f(9), anchor="w").grid(
            row=0, column=1, sticky="w", padx=(T.px(10), 0)
        )
        self.base_var = tk.StringVar(value=self._config.api_base)
        self.model_var = tk.StringVar(value=self._config.model)
        for column, variable in ((0, self.base_var), (1, self.model_var)):
            self._make_entry(grid, variable).grid(
                row=1,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else T.px(10), 0),
                pady=(T.px(4), 0),
                ipady=T.px(6),
            )

        concurrency_row = tk.Frame(abody, bg=T.CARD)
        concurrency_row.pack(fill="x", pady=(T.px(10), 0))
        tk.Label(concurrency_row, text="识别并发", bg=T.CARD, fg=T.MUTED, font=T.f(9)).pack(side="left")
        self.concurrency_var = tk.StringVar(value=str(self._config.recognize_concurrency))
        from tkinter import ttk

        ttk.Combobox(
            concurrency_row,
            textvariable=self.concurrency_var,
            values=("1", "2", "4"),
            state="readonly",
            width=5,
            font=T.f(9),
        ).pack(side="left", padx=(T.px(8), T.px(8)))
        tk.Label(
            concurrency_row,
            text="默认 2；接口限流（429）时自动降到 1，稳定后再逐步升回",
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(8),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        action_row = tk.Frame(abody, bg=T.CARD)
        action_row.pack(fill="x", pady=(T.px(12), 0))
        self.btn_test = RoundButton(
            action_row, "测试连接", command=self._start_test, kind="soft", icon="plug", height=32, font_size=9
        )
        self.btn_test.pack(side="left")
        self.test_label = tk.Label(
            action_row,
            text="",
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(9),
            anchor="w",
            justify="left",
            wraplength=T.px(430),
        )
        self.test_label.pack(side="left", padx=(T.px(10), 0), fill="x", expand=True)

        self._build_cleanup_card(wrap)

        self.error_label = tk.Label(
            wrap, text="", bg=T.BG, fg=T.DANGER, font=T.f(9), anchor="w", justify="left", wraplength=T.px(600)
        )
        self.error_label.pack(fill="x", pady=(T.px(8), 0))

        footer = tk.Frame(wrap, bg=T.BG)
        footer.pack(fill="x", pady=(T.px(10), 0))
        RoundButton(footer, "保存", command=self._save, kind="primary", icon="save", height=36, width=96).pack(side="right")
        RoundButton(footer, "取消", command=self._close, kind="secondary", height=36).pack(
            side="right", padx=(0, T.px(8))
        )

    def _build_cleanup_card(self, wrap) -> None:
        card = Card(wrap, padding=14)
        card.pack(fill="x", pady=(T.px(12), 0))
        body = card.body
        head = tk.Frame(body, bg=T.CARD)
        head.pack(fill="x")
        tk.Label(head, text="本地缓存清理", bg=T.CARD, fg=T.INK, font=T.f(10, "bold")).pack(side="left")
        tk.Label(
            head,
            text="默认关闭；只处理收件箱，不碰 vault",
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(8),
        ).pack(side="right")

        self.cleanup_enabled_var = tk.BooleanVar(value=bool(self._config.cleanup_enabled))
        tk.Checkbutton(
            body,
            text="启用本地缓存清理（默认关闭）",
            variable=self.cleanup_enabled_var,
            bg=T.CARD,
            fg=T.INK,
            activebackground=T.CARD,
            selectcolor=T.CARD,
            font=T.f(9),
            bd=0,
            highlightthickness=0,
        ).pack(anchor="w", pady=(T.px(6), T.px(4)))

        self.cleanup_mode_var = tk.StringVar(value=self._config.cleanup_mode)
        self.cleanup_delete_allowed_var = tk.BooleanVar(value=bool(self._config.cleanup_delete_allowed))

        self.cleanup_release_radio = tk.Radiobutton(
            body,
            text="释放本地空间（推荐）：OneDrive 文件退为仅在线，本地占用归零",
            variable=self.cleanup_mode_var,
            value="release",
            bg=T.CARD,
            fg=T.INK_SOFT,
            activebackground=T.CARD,
            selectcolor=T.CARD,
            font=T.f(9),
            bd=0,
            highlightthickness=0,
        )
        self.cleanup_release_radio.pack(anchor="w")

        delete_row = tk.Frame(body, bg=T.CARD)
        delete_row.pack(fill="x", pady=(T.px(2), 0))
        self.cleanup_delete_radio = tk.Radiobutton(
            delete_row,
            text="彻底删除到回收站",
            variable=self.cleanup_mode_var,
            value="delete",
            bg=T.CARD,
            fg=T.INK_SOFT,
            activebackground=T.CARD,
            selectcolor=T.CARD,
            font=T.f(9),
            bd=0,
            highlightthickness=0,
            state="normal" if self.cleanup_delete_allowed_var.get() else "disabled",
        )
        self.cleanup_delete_radio.pack(side="left")

        tk.Checkbutton(
            delete_row,
            text="允许彻底删除（需单独勾选；每次执行还会再次确认）",
            variable=self.cleanup_delete_allowed_var,
            command=self._sync_cleanup_controls,
            bg=T.CARD,
            fg=T.DANGER,
            activebackground=T.CARD,
            selectcolor=T.CARD,
            font=T.f(8),
            bd=0,
            highlightthickness=0,
        ).pack(side="left", padx=(T.px(8), 0))

        footer = tk.Frame(body, bg=T.CARD)
        footer.pack(fill="x", pady=(T.px(6), 0))
        tk.Label(footer, text="操作记录保留", bg=T.CARD, fg=T.MUTED, font=T.f(8)).pack(side="left")
        self.cleanup_retention_var = tk.StringVar(value=str(self._config.cleanup_retention_days))
        from tkinter import ttk

        ttk.Combobox(
            footer,
            textvariable=self.cleanup_retention_var,
            values=("1", "7", "30", "90", "365"),
            state="readonly",
            width=4,
            font=T.f(8),
        ).pack(side="left", padx=(T.px(6), T.px(2)))
        tk.Label(footer, text="天", bg=T.CARD, fg=T.MUTED, font=T.f(8)).pack(side="left")
        RoundButton(
            footer,
            "打开清理日志",
            command=self._open_cleanup_log,
            kind="soft",
            icon="log",
            height=28,
            font_size=8,
            padx=10,
        ).pack(side="right")
        tk.Label(
            body,
            text="OneDrive 同步目录内只允许释放；彻底删除仅对非同步目录、且已登记云端副本的文件开放。",
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(8),
            anchor="w",
            justify="left",
            wraplength=T.px(600),
        ).pack(fill="x", pady=(T.px(4), 0))
        self._sync_cleanup_controls()

    def _sync_cleanup_controls(self) -> None:
        allowed = self.cleanup_delete_allowed_var.get()
        self.cleanup_delete_radio.configure(state="normal" if allowed else "disabled")
        if not allowed and self.cleanup_mode_var.get() == "delete":
            self.cleanup_mode_var.set("release")

    def _open_cleanup_log(self) -> None:
        try:
            from src.cleanup import CleanupService

            CleanupService(self._config.to_settings(offline=True), self._config).open_log_dir()
        except Exception as exc:
            self._error(f"打开清理日志失败：{exc}")

    def _make_entry(self, parent, variable: tk.StringVar, show: str = "") -> tk.Entry:
        """统一输入框样式：浅底、细边框、聚焦高亮，与下拉框观感一致。"""
        entry = tk.Entry(
            parent,
            textvariable=variable,
            show=show,
            bg="#FAFBFE",
            fg=T.INK,
            relief="flat",
            highlightthickness=T.px(1),
            highlightbackground=T.BORDER_STRONG,
            highlightcolor=T.ACCENT,
            font=T.f(9),
            insertbackground=T.INK,
        )
        entry.bind("<FocusIn>", lambda _e: entry.configure(bg="#FFFFFF"))
        entry.bind("<FocusOut>", lambda _e: entry.configure(bg="#FAFBFE"))
        return entry

    def _sync_canvas(self, event: tk.Event = None) -> None:
        width = event.width if event is not None else self._canvas.winfo_width()
        self._canvas.itemconfigure(self._canvas_window, width=max(width, 1))
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _update_scrollregion(self, _event: tk.Event = None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_mousewheel(self, event: tk.Event) -> None:
        units = max(1, abs(event.delta) // 120)
        self._canvas.yview_scroll(-units if event.delta > 0 else units, "units")

    def _on_focus_in(self, event: tk.Event) -> None:
        """键盘 Tab 切换焦点时，把获得焦点的控件滚进可视区。"""
        widget = event.widget
        if not str(widget).startswith(str(self._content)):
            return
        self._scroll_into_view(widget)

    def _scroll_into_view(self, widget) -> None:
        self.update_idletasks()
        total = self._content.winfo_height()
        if total <= 0:
            return
        view_top, view_bottom = self._canvas.yview()
        if view_bottom - view_top >= 1.0:
            return  # 内容没有超出画布，无需滚动
        margin = T.px(8)
        top = widget.winfo_rooty() - self._content.winfo_rooty() - margin
        bottom = top + widget.winfo_height() + margin * 2
        view_px_top = view_top * total
        view_px_bottom = view_bottom * total
        if top < view_px_top:
            self._canvas.yview_moveto(max(top / total, 0.0))
        elif bottom > view_px_bottom:
            self._canvas.yview_moveto(min((bottom - self._canvas.winfo_height()) / total, 1.0))

    @staticmethod
    def _workarea() -> tuple[int, int]:
        """取工作区（不含任务栏）尺寸；异常时按常见 1080p 工作区回退。"""
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return max(rect.right - rect.left, 800), max(rect.bottom - rect.top, 600)
        except Exception:
            pass
        return 1280, 960

    def _center(self, master) -> None:
        self.update_idletasks()
        content_w = self._content.winfo_reqwidth()
        content_h = self._content.winfo_reqheight()
        work_w, work_h = self._workarea()
        # 高度以工作区为上限：内容更高时靠滚动条滚到底部按钮
        width = min(max(content_w, T.px(860)), T.px(1080), max(work_w - T.px(40), T.px(400)))
        # 上下预留标题栏/边框高度，避免压到任务栏或把标题栏顶出屏幕
        height = min(max(content_h, T.px(560)), max(work_h - T.px(130), T.px(320)))
        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        self.after_idle(self._sync_canvas)
        self._update_scrollregion()
        if height >= content_h:
            self._scrollbar.pack_forget()
        x = master.winfo_rootx() + max((master.winfo_width() - width) // 2, 0)
        y = master.winfo_rooty() + max((master.winfo_height() - height) // 2, 0)
        # 防止多显示器 / 缩放差异把弹窗放到屏幕外或任务栏下
        x = min(max(x, 0), max(work_w - width, 0))
        y = min(max(y, T.px(40)), max(work_h - height - T.px(40), T.px(40)))
        self.geometry(f"{width}x{height}+{x}+{y}")

    # ---------------- 交互 ----------------

    def focus_key(self) -> None:
        self.key_entry.focus_set()

    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key.get() else "•")

    def _open_manual(self) -> None:
        ok, message = open_manual()
        if ok:
            self._error("")
        else:
            self._error(message)

    @staticmethod
    def _mask_key(key: str) -> str:
        key = (key or "").strip()
        if not key:
            return "（未配置）"
        if len(key) <= 6:
            return "****"
        return f"****{key[-4:]}"

    def _on_key_edit(self, *_args) -> None:
        """Key 一改动，就作废上一次「测试通过」，避免把旧结果当成新 Key 的结论。"""
        key = self.key_var.get().strip()
        if self._tested_key is not None and key != self._tested_key:
            self._tested_key = None
            if hasattr(self, "test_label"):
                self.test_label.configure(text="", fg=T.MUTED)
        self._refresh_key_status()

    def _refresh_key_status(self) -> None:
        """常驻状态行：未配置 / 未验证 / 已验证，重启后按持久化的结果恢复。"""
        key = self.key_var.get().strip()
        label = self.key_status_label
        if not key:
            label.configure(text="当前 Key：未配置 · 真实识别不可用，仅可离线演示", fg=T.MUTED)
            return
        masked = self._mask_key(key)
        if self._tested_key == key:
            label.configure(text=f"当前 Key：{masked} · 已验证（测试通过，保存后写入本机）", fg=T.SUCCESS)
        elif key == self._initial_key and self._config.api_key_verified:
            when = self._config.api_key_verified_at
            suffix = f"（{when}）" if when else ""
            label.configure(text=f"当前 Key：{masked} · 已验证{suffix}", fg=T.SUCCESS)
        elif key == self._initial_key and self._config.api_key:
            label.configure(text=f"当前 Key：{masked} · 未验证（还没通过「测试连接」）", fg=T.WARN)
        else:
            label.configure(text=f"当前 Key：{masked} · 未验证（已修改，请重新测试）", fg=T.WARN)

    def _pick(self, variable: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(parent=self, initialdir=variable.get() or str(Path.home()))
        if chosen:
            variable.set(chosen)

    def _error(self, text: str) -> None:
        self.error_label.configure(text=text)

    def _save(self) -> None:
        watch = self.watch_var.get().strip()
        vault = self.vault_var.get().strip()
        base = self.base_var.get().strip()
        model = self.model_var.get().strip()
        if not watch or not vault:
            self._error("收件箱和笔记库路径不能为空。")
            return
        if not base or not model:
            self._error("接口地址和模型名不能为空。")
            return
        cleanup_mode = self.cleanup_mode_var.get()
        if cleanup_mode == "delete" and not self.cleanup_delete_allowed_var.get():
            self._error("彻底删除需要先勾选“允许彻底删除”，或改回“释放本地空间”。")
            return

        for label, path in (("收件箱", watch), ("笔记库", vault)):
            try:
                Path(path).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._error(f"{label}目录不可用：{exc}")
                return

        key = self.key_var.get().strip()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        if key and key == self._tested_key:
            verified, verified_at = True, now
        elif key and key == self._initial_key and self._config.api_key_verified:
            verified, verified_at = True, self._config.api_key_verified_at or now
        else:
            verified, verified_at = False, ""

        new_config = AppConfig(
            watch_dir=str(Path(watch).expanduser()),
            output_dir=self._config.output_dir,
            vault_dir=str(Path(vault).expanduser()),
            api_base=base.rstrip("/"),
            model=model,
            api_key=key,
            api_key_verified=verified,
            api_key_verified_at=verified_at,
            offline_mode=self._config.offline_mode,
            recognize_concurrency=int(self.concurrency_var.get() or 2),
            cleanup_enabled=bool(self.cleanup_enabled_var.get()),
            cleanup_mode=cleanup_mode,
            cleanup_delete_allowed=bool(self.cleanup_delete_allowed_var.get()),
            cleanup_retention_days=int(self.cleanup_retention_var.get() or 7),
        )
        try:
            new_config.save()
        except OSError as exc:
            self._error(f"配置写入失败：{exc}")
            return
        if self._on_saved is not None:
            self._on_saved(new_config)

        if key:
            if sys.platform == "win32":
                stored = "API Key 已加密保存到本机（Windows DPAPI，只有当前 Windows 用户能解密）。"
            else:
                stored = "API Key 已保存到本机配置文件。"
            status = "已验证" if verified else "未验证（建议先点「测试连接」再保存）"
            detail = f"{stored}\n位置：{new_config.path}\n当前 Key 状态：{status}"
        else:
            detail = "设置已保存。当前没有 API Key，只能使用离线演示。"
        messagebox.showinfo("设置已保存 · NoteFlow", detail, parent=self)
        self._close()

    def _close(self) -> None:
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    # ---------------- 连接测试 ----------------

    def _start_test(self) -> None:
        if self._testing:
            return
        key = self.key_var.get().strip()
        if not key:
            self.test_label.configure(text="请先填写 API Key，再测试连接。", fg=T.WARN)
            return
        self._testing = True
        self._async_test_key = key
        self.btn_test.set_enabled(False)
        self.test_label.configure(text="正在测试连接…", fg=T.MUTED)
        threading.Thread(
            target=self._test_worker,
            args=(self.base_var.get(), key, self.model_var.get()),
            name="noteflow-test-conn",
            daemon=True,
        ).start()
        self.after(120, self._poll_test)

    def _test_worker(self, base: str, key: str, model: str) -> None:
        try:
            self._test_queue.put(test_connection(base, key, model))
        except Exception as exc:  # 测试本身失败也要给用户一个结果
            self._test_queue.put((False, f"测试过程出错：{exc}"))

    def _poll_test(self) -> None:
        if self._closed or not self.winfo_exists():
            return
        try:
            ok, message = self._test_queue.get_nowait()
        except queue.Empty:
            if self._testing:
                self.after(120, self._poll_test)
            return
        self._testing = False
        self.btn_test.set_enabled(True)
        if ok and self.key_var.get().strip() == self._async_test_key:
            self._tested_key = self._async_test_key
            self.test_label.configure(text=f"通过：{message}", fg=T.SUCCESS)
        elif ok:
            # 测试期间 Key 又被改过：旧结果不能算到新 Key 头上
            self._tested_key = None
            self.test_label.configure(text="测试期间 Key 已修改，请重新测试连接。", fg=T.WARN)
        else:
            self._tested_key = None
            self.test_label.configure(text=f"失败：{message}", fg=T.DANGER)
        self._refresh_key_status()
