"""设置弹窗：路径、接口、Key 都在这里改，首屏不再摆一大块配置卡。"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from src.appconfig import AppConfig
from src.controller import test_connection

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
        wrap = tk.Frame(self, bg=T.BG)
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
        entry = tk.Entry(
            row,
            textvariable=self.watch_var,
            bg="#FAFBFE",
            fg=T.INK,
            relief="flat",
            highlightthickness=T.px(1),
            highlightbackground=T.BORDER_STRONG,
            highlightcolor=T.ACCENT,
            font=T.f(9),
            insertbackground=T.INK,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=T.px(6))
        RoundButton(row, "浏览", command=lambda: self._pick(self.watch_var), height=32, font_size=9, padx=12).pack(
            side="left", padx=(T.px(8), 0)
        )

        tk.Label(body, text="笔记库（Obsidian vault）", bg=T.CARD, fg=T.INK, font=T.f(10, "bold"), anchor="w").pack(fill="x")
        row2 = tk.Frame(body, bg=T.CARD)
        row2.pack(fill="x", pady=(T.px(6), 0))
        self.vault_var = tk.StringVar(value=self._config.vault_dir)
        entry2 = tk.Entry(
            row2,
            textvariable=self.vault_var,
            bg="#FAFBFE",
            fg=T.INK,
            relief="flat",
            highlightthickness=T.px(1),
            highlightbackground=T.BORDER_STRONG,
            highlightcolor=T.ACCENT,
            font=T.f(9),
            insertbackground=T.INK,
        )
        entry2.pack(side="left", fill="x", expand=True, ipady=T.px(6))
        RoundButton(row2, "浏览", command=lambda: self._pick(self.vault_var), height=32, font_size=9, padx=12).pack(
            side="left", padx=(T.px(8), 0)
        )

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

        tk.Label(abody, text="API Key", bg=T.CARD, fg=T.MUTED, font=T.f(9), anchor="w").pack(
            fill="x", pady=(T.px(8), T.px(4))
        )
        self.key_var = tk.StringVar(value=self._config.api_key)
        self.key_entry = tk.Entry(
            abody,
            textvariable=self.key_var,
            show="•",
            bg="#FAFBFE",
            fg=T.INK,
            relief="flat",
            highlightthickness=T.px(1),
            highlightbackground=T.BORDER_STRONG,
            highlightcolor=T.ACCENT,
            font=T.f(9),
            insertbackground=T.INK,
        )
        self.key_entry.pack(fill="x", ipady=T.px(6))

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
            tk.Entry(
                grid,
                textvariable=variable,
                bg="#FAFBFE",
                fg=T.INK,
                relief="flat",
                highlightthickness=T.px(1),
                highlightbackground=T.BORDER_STRONG,
                highlightcolor=T.ACCENT,
                font=T.f(9),
                insertbackground=T.INK,
            ).grid(
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
        self.btn_test = RoundButton(action_row, "测试连接", command=self._start_test, kind="soft", height=32, font_size=9)
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

        self.error_label = tk.Label(
            wrap, text="", bg=T.BG, fg=T.DANGER, font=T.f(9), anchor="w", justify="left", wraplength=T.px(600)
        )
        self.error_label.pack(fill="x", pady=(T.px(8), 0))

        footer = tk.Frame(wrap, bg=T.BG)
        footer.pack(fill="x", pady=(T.px(10), 0))
        RoundButton(footer, "保存", command=self._save, kind="primary", height=36, width=96).pack(side="right")
        RoundButton(footer, "取消", command=self._close, kind="secondary", height=36).pack(
            side="right", padx=(0, T.px(8))
        )

    def _center(self, master) -> None:
        self.update_idletasks()
        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()
        screen_w = master.winfo_screenwidth()
        screen_h = master.winfo_screenheight()
        x = master.winfo_rootx() + max((master.winfo_width() - width) // 2, 0)
        y = master.winfo_rooty() + max((master.winfo_height() - height) // 2, 0)
        # 防止多显示器 / 缩放差异把弹窗放到屏幕外
        x = min(max(x, 0), max(screen_w - width, 0))
        y = min(max(y, 0), max(screen_h - height, 0))
        self.geometry(f"{width}x{height}+{x}+{y}")

    # ---------------- 交互 ----------------

    def focus_key(self) -> None:
        self.key_entry.focus_set()

    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key.get() else "•")

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

        for label, path in (("收件箱", watch), ("笔记库", vault)):
            try:
                Path(path).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._error(f"{label}目录不可用：{exc}")
                return

        new_config = AppConfig(
            watch_dir=str(Path(watch).expanduser()),
            output_dir=self._config.output_dir,
            vault_dir=str(Path(vault).expanduser()),
            api_base=base.rstrip("/"),
            model=model,
            api_key=self.key_var.get().strip(),
            offline_mode=self._config.offline_mode,
            recognize_concurrency=int(self.concurrency_var.get() or 2),
        )
        try:
            new_config.save()
        except OSError as exc:
            self._error(f"配置写入失败：{exc}")
            return
        if self._on_saved is not None:
            self._on_saved(new_config)
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
        if ok:
            self.test_label.configure(text=f"通过：{message}", fg=T.SUCCESS)
        else:
            self.test_label.configure(text=f"失败：{message}", fg=T.DANGER)
