"""清理本地缓存向导：先扫描、逐项展示五项校验，再整批二次确认。"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from src.appconfig import AppConfig
from src.cleanup import ActionResult, Candidate, CleanupService, candidate_display_status, format_bytes

from . import theme as T
from .widgets import Card, RoundButton


class CleanupDialog(tk.Toplevel):
    def __init__(self, master, config: AppConfig) -> None:
        super().__init__(master)
        self._config = config
        self._service = CleanupService(config.to_settings(offline=True), config)
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._candidates: list[Candidate] = []
        self._results: dict[str, ActionResult] = {}
        self._scanning = False
        self._executing = False
        self._stop = threading.Event()
        self._closed = False
        self._pending_close = False
        self._mode = config.cleanup_mode if config.cleanup_mode in ("release", "delete") else "release"
        if self._mode == "delete" and not config.cleanup_delete_allowed:
            self._mode = "release"

        self.title("清理本地缓存 · NoteFlow")
        self.configure(bg=T.BG)
        self.resizable(True, True)
        self.minsize(T.px(760), T.px(520))
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close())
        try:
            self._service.acquire_lock()
            self._build()
            self._center(master)
            self._start_scan()
        except Exception:
            self._service.release_lock()
            self.destroy()
            raise
        self.after(0, self._activate_modal)

    def _activate_modal(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            if not self._closed:
                self.after(80, self._activate_modal)

    # ---------------- 界面 ----------------

    def _build(self) -> None:
        wrap = tk.Frame(self, bg=T.BG)
        wrap.pack(fill="both", expand=True, padx=T.px(20), pady=T.px(16))

        tk.Label(wrap, text="清理本地缓存", bg=T.BG, fg=T.INK, font=T.f(15, "bold"), anchor="w").pack(fill="x")
        tk.Label(
            wrap,
            text="只处理收件箱内、已完成处理且 vault / ima / 云端三端都有记录的文件。默认只释放本地空间。",
            bg=T.BG,
            fg=T.MUTED,
            font=T.f(9),
            anchor="w",
            justify="left",
            wraplength=T.px(860),
        ).pack(fill="x", pady=(T.px(2), T.px(8)))

        self.summary_label = tk.Label(wrap, text="正在扫描收件箱…", bg=T.BG, fg=T.INK_SOFT, font=T.f(9, "bold"), anchor="w")
        self.summary_label.pack(fill="x")

        self.interrupted_label = tk.Label(
            wrap, text="", bg=T.BG, fg=T.WARN, font=T.f(8), anchor="w", justify="left", wraplength=T.px(860)
        )
        self.interrupted_label.pack(fill="x")
        interrupted = self._service.interrupted_actions()
        if interrupted:
            self.interrupted_label.configure(
                text=f"检测到 {len(interrupted)} 个上次未完成的操作，请检查日志或回收站；不会自动重试。"
            )

        policy = Card(wrap, padding=12)
        policy.pack(fill="x", pady=(T.px(8), 0))
        pbody = policy.body
        tk.Label(pbody, text="清理方式", bg=T.CARD, fg=T.INK, font=T.f(9, "bold")).pack(side="left")
        self.mode_var = tk.StringVar(value=self._mode)
        self.release_radio = tk.Radiobutton(
            pbody,
            text="释放本地空间（推荐）",
            variable=self.mode_var,
            value="release",
            command=self._on_mode_change,
            bg=T.CARD,
            fg=T.INK_SOFT,
            activebackground=T.CARD,
            selectcolor=T.CARD,
            font=T.f(9),
            bd=0,
            highlightthickness=0,
        )
        self.release_radio.pack(side="left", padx=(T.px(12), 0))
        self.delete_radio = tk.Radiobutton(
            pbody,
            text="彻底删除到回收站",
            variable=self.mode_var,
            value="delete",
            command=self._on_mode_change,
            bg=T.CARD,
            fg=T.INK_SOFT,
            activebackground=T.CARD,
            selectcolor=T.CARD,
            font=T.f(9),
            bd=0,
            highlightthickness=0,
            state="normal" if self._config.cleanup_delete_allowed else "disabled",
        )
        self.delete_radio.pack(side="left", padx=(T.px(12), 0))
        tk.Label(
            pbody,
            text="OneDrive 同步目录内只允许释放；彻底删除仅对已登记云端副本的非同步文件开放。",
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(8),
        ).pack(side="left", padx=(T.px(12), 0))

        table_card = Card(wrap, padding=10)
        table_card.pack(fill="both", expand=True, pady=(T.px(8), 0))
        tbody = table_card.body
        columns = ("status", "file", "size", "cloud", "vault", "ima", "reason")
        # ttk 默认行高是固定像素，高分屏下不会跟着字体缩放，会裁掉半行字
        style = ttk.Style(self)
        style.configure("Cleanup.Treeview", rowheight=T.px(26), font=T.f(9))
        style.configure("Cleanup.Treeview.Heading", font=T.f(9, "bold"))
        self.tree = ttk.Treeview(
            tbody, columns=columns, show="headings", height=12, selectmode="browse", style="Cleanup.Treeview"
        )
        headings = {
            "status": ("状态", 64),
            "file": ("文件", 230),
            "size": ("本地占用", 90),
            "cloud": ("云端", 64),
            "vault": ("vault", 64),
            "ima": ("ima", 64),
            "reason": ("说明", 300),
        }
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=T.px(width), minwidth=T.px(54), anchor="w", stretch=(key in ("file", "reason")))
        scroll = ttk.Scrollbar(tbody, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda _e: self._hide_tooltip())
        self.tree.bind("<Configure>", self._on_tree_resize)
        self._reason_full: dict[str, str] = {}
        self._reason_font = None  # 省略号测量用字体，复用避免反复创建
        self._tooltip_after = None
        self._tooltip_row = None
        self._tooltip_win = None
        self._resize_after = None

        footer = tk.Frame(wrap, bg=T.BG)
        footer.pack(fill="x", pady=(T.px(10), 0))
        self.btn_ima = RoundButton(
            footer, "登记 ima 导入", command=self._register_ima, kind="soft", height=32, font_size=8
        )
        self.btn_ima.pack(side="left")
        self.btn_cloud = RoundButton(
            footer, "登记云端副本", command=self._register_cloud, kind="soft", height=32, font_size=8
        )
        self.btn_cloud.pack(side="left", padx=(T.px(6), 0))
        RoundButton(footer, "打开日志", command=self._open_log, kind="ghost", height=32, font_size=8).pack(
            side="left", padx=(T.px(6), 0)
        )
        RoundButton(footer, "打开回收站", command=self._open_recycle, kind="ghost", height=32, font_size=8).pack(
            side="left", padx=(T.px(6), 0)
        )

        self.btn_cancel = RoundButton(footer, "关闭", command=self._close, kind="secondary", height=32, font_size=8)
        self.btn_cancel.pack(side="right")
        self.btn_start = RoundButton(
            footer, "开始清理", command=self._start, kind="primary", height=32, font_size=8
        )
        self.btn_start.pack(side="right", padx=(0, T.px(8)))
        self.btn_rescan = RoundButton(
            footer, "重新扫描", command=self._start_scan, kind="ghost", height=32, font_size=8
        )
        self.btn_rescan.pack(side="right", padx=(0, T.px(6)))

        self.state_label = tk.Label(
            wrap, text="", bg=T.BG, fg=T.MUTED, font=T.f(8), anchor="w", justify="left", wraplength=T.px(860)
        )
        self.state_label.pack(fill="x", pady=(T.px(6), 0))

        self.hint_label = tk.Label(
            wrap, text="", bg=T.BG, fg=T.MUTED, font=T.f(8), anchor="w", justify="left", wraplength=T.px(860)
        )
        self.hint_label.pack(fill="x", pady=(T.px(6), 0))
        self._update_buttons()

    def _center(self, master) -> None:
        self.update_idletasks()
        screen_w = master.winfo_screenwidth()
        screen_h = master.winfo_screenheight()
        width = min(max(self.winfo_reqwidth(), T.px(860)), T.px(1080), screen_w - 40)
        height = min(max(self.winfo_reqheight(), T.px(560)), T.px(820), screen_h - 80)
        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        x = master.winfo_rootx() + max((master.winfo_width() - width) // 2, 0)
        y = master.winfo_rooty() + max((master.winfo_height() - height) // 2, 0)
        x = min(max(x, 0), max(screen_w - width, 0))
        y = min(max(y, 0), max(screen_h - height, 0))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _center_child(self, win: tk.Toplevel) -> None:
        win.update_idletasks()
        width = win.winfo_reqwidth()
        height = win.winfo_reqheight()
        x = self.winfo_rootx() + max((self.winfo_width() - width) // 2, 0)
        y = self.winfo_rooty() + max((self.winfo_height() - height) // 2, 0)
        win.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    # ---------------- 扫描 ----------------

    def _start_scan(self) -> None:
        if self._scanning or self._executing:
            return
        self._scanning = True
        self._results.clear()
        self.summary_label.configure(text="正在扫描收件箱…", fg=T.INK_SOFT)
        self.hint_label.configure(text="")
        self._clear_tree()
        self._update_buttons()
        threading.Thread(target=self._scan_worker, name="noteflow-cleanup-scan", daemon=True).start()
        self.after(100, self._poll)

    def _scan_worker(self) -> None:
        try:
            self._queue.put(("scan", (self._service.scan(), "")))
        except Exception as exc:
            self._queue.put(("scan", ([], str(exc))))

    def _clear_tree(self) -> None:
        self._hide_tooltip()
        for item in self.tree.get_children():
            self.tree.delete(item)

    def _refresh_rows(self) -> None:
        keep = self.tree.selection()
        ypos = self.tree.yview()
        self._clear_tree()
        self._reason_full = {}
        for index, candidate in enumerate(self._candidates):
            status, reason = candidate_display_status(candidate, self.mode_var.get())
            result = self._results.get(candidate.source_id)
            if result is not None:
                status = {
                    "success": "已完成",
                    "pending": "待确认",
                    "skipped": "跳过",
                    "failed": "失败",
                }.get(result.status, status)
                reason = result.reason or reason
            self._reason_full[str(index)] = reason
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    status,
                    candidate.path.name,
                    format_bytes(candidate.local_bytes),
                    self._check_text(candidate.status("cloud")),
                    self._check_text(candidate.status("vault")),
                    self._check_text(candidate.status("ima")),
                    reason,
                ),
            )
        if keep and self.tree.exists(keep[0]):
            self.tree.selection_set(keep[0])
        if ypos:
            self.tree.yview_moveto(ypos[0])
        self._apply_reason_ellipsis()

    @staticmethod
    def _check_text(status: str) -> str:
        return {"pass": "通过", "fail": "未通过", "unknown": "未知"}.get(status, status)

    # ---------------- 说明列省略号与悬浮提示 ----------------

    def _apply_reason_ellipsis(self) -> None:
        """说明列按实际列宽截断加省略号，完整文本由悬浮提示提供。"""
        children = self.tree.get_children()
        if not children:
            return
        width = max(self._reason_width(children) - T.px(8), T.px(40))
        if self._reason_font is None:
            self._reason_font = tkfont.Font(font=T.f(9))
        font = self._reason_font
        for iid in children:
            full = self._reason_full.get(iid, self.tree.set(iid, "reason"))
            self.tree.set(iid, "reason", self._ellipsize(full, width, font))

    def _reason_width(self, children) -> int:
        """取说明列的实际显示宽度：优先量可见行单元格，都不可见时用当前列宽设置。"""
        for iid in children:
            bbox = self.tree.bbox(iid, "reason")
            if bbox:
                return bbox[2]
        return int(self.tree.column("reason", "width"))

    @staticmethod
    def _ellipsize(text: str, width: int, font) -> str:
        if font.measure(text) <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"

    def _on_tree_resize(self, _event: tk.Event) -> None:
        self._hide_tooltip()
        if self._resize_after is not None:
            try:
                self.after_cancel(self._resize_after)
            except tk.TclError:
                pass
        self._resize_after = self.after(250, self._apply_reason_ellipsis)

    def _on_tree_motion(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if not row or self.tree.identify_column(event.x) != "#7":
            self._hide_tooltip()
            return
        full = self._reason_full.get(row, "")
        if not full or self.tree.set(row, "reason") == full:
            self._hide_tooltip()
            return
        if self._tooltip_row == row:
            return
        self._hide_tooltip()
        self._tooltip_row = row
        self._tooltip_after = self.after(400, lambda: self._show_tooltip(full, event.x_root, event.y_root))

    def _show_tooltip(self, text: str, x: int, y: int) -> None:
        if self._closed or not self.winfo_exists():
            return
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except tk.TclError:
                pass
        win = tk.Toplevel(self)
        win.wm_overrideredirect(True)
        tk.Label(
            win,
            text=text,
            bg=T.INK,
            fg="#FFFFFF",
            font=T.f(8),
            justify="left",
            wraplength=T.px(520),
            padx=T.px(8),
            pady=T.px(6),
        ).pack()
        win.update_idletasks()
        tip_w = win.winfo_reqwidth()
        tip_h = win.winfo_reqheight()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        margin = T.px(8)
        tx = min(x + T.px(12), max(screen_w - tip_w - margin, 0))
        ty = y + T.px(16)
        # 下方放不下就翻到指针上方，避免底部行的完整文本被屏幕下缘裁掉
        if ty + tip_h > screen_h - margin:
            ty = max(y - tip_h - T.px(12), margin)
        win.wm_geometry(f"+{tx}+{ty}")
        self._tooltip_win = win

    def _hide_tooltip(self) -> None:
        if self._tooltip_after is not None:
            try:
                self.after_cancel(self._tooltip_after)
            except tk.TclError:
                pass
            self._tooltip_after = None
        self._tooltip_row = None
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except tk.TclError:
                pass
            self._tooltip_win = None

    def _on_scan_done(self, candidates: list[Candidate], error: str) -> None:
        self._scanning = False
        if error:
            self._candidates = []
            self._results.clear()
            self._refresh_rows()
            self.summary_label.configure(text=f"扫描失败：{error}", fg=T.DANGER)
            self._update_buttons()
            return
        self._candidates = candidates
        self._refresh_rows()
        eligible = [item for item in candidates if item.eligible(self.mode_var.get())]
        total = sum(item.local_bytes for item in eligible)
        self.summary_label.configure(
            text=f"收件箱：{self._config.watch_dir}    可清理：{len(eligible)} 个文件 / {format_bytes(total)}",
            fg=T.INK_SOFT,
        )
        self._update_buttons()

    def _on_mode_change(self) -> None:
        self._mode = self.mode_var.get()
        if self._mode == "delete" and not self._config.cleanup_delete_allowed:
            self.mode_var.set("release")
            self._mode = "release"
        self._refresh_rows()
        self._on_scan_done(self._candidates, "")

    # ---------------- 按钮状态 ----------------

    def _selected(self) -> Candidate | None:
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            return self._candidates[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def _eligible_now(self) -> list[Candidate]:
        mode = self.mode_var.get()
        if self._scanning:
            return []
        return [
            item
            for item in self._candidates
            if item.eligible(mode) and item.source_id not in self._results
        ]

    def _update_buttons(self) -> None:
        mode = self.mode_var.get()
        eligible = self._eligible_now()
        total = sum(item.local_bytes for item in eligible)
        busy = self._executing or self._scanning
        self.btn_ima.set_enabled(not busy)
        self.btn_cloud.set_enabled(not busy)
        # 主按钮固定动作文案 + 禁用态；状态用 state_label 另起一行说明
        self.btn_start.set_text("开始清理")
        self.btn_start.set_enabled(not busy and bool(eligible))
        self.btn_rescan.set_enabled(not busy)
        verb = "释放" if mode == "release" else "彻底删除"
        if self._executing:
            self.state_label.configure(text="", fg=T.MUTED)
        elif self._scanning:
            self.state_label.configure(text="正在扫描收件箱…", fg=T.MUTED)
        elif eligible:
            self.state_label.configure(
                text=f"可清理 {len(eligible)} 个文件 / {format_bytes(total)}，点击「开始清理」{verb}。",
                fg=T.INK_SOFT,
            )
        elif self._results:
            self.state_label.configure(text="本批次已处理完成，可点「重新扫描」查看最新状态。", fg=T.MUTED)
        else:
            self.state_label.configure(text="没有可清理文件。", fg=T.MUTED)

    # ---------------- 确认与执行 ----------------

    def _start(self) -> None:
        if self._executing or self._scanning:
            return
        mode = self.mode_var.get()
        eligible = self._eligible_now()
        if not eligible:
            return
        if mode == "delete" and not self._config.cleanup_delete_allowed:
            messagebox.showwarning("需要许可", "请先在设置中勾选“允许彻底删除”。", parent=self)
            return
        if not self._confirm(eligible, mode):
            return
        self._service.mark_first_run()
        self._service.new_batch_id()
        self._executing = True
        self._stop.clear()
        self._results.clear()
        self._update_buttons()
        self.hint_label.configure(
            text="正在执行，请勿关闭窗口；每个文件执行前都会重新校验。",
            fg=T.MUTED,
        )
        threading.Thread(
            target=self._exec_worker,
            args=(eligible, mode),
            name="noteflow-cleanup-exec",
            daemon=True,
        ).start()
        self.after(100, self._poll)

    def _exec_worker(self, eligible: list[Candidate], mode: str) -> None:
        for candidate in eligible:
            if self._stop.is_set():
                self._queue.put(("stopped", None))
                return
            try:
                result = self._service.execute(candidate, mode)
            except Exception as exc:
                result = ActionResult(candidate.path, mode, "failed", f"执行异常：{exc}", candidate.size_bytes)
            self._queue.put(("result", result))
        self._queue.put(("done", None))

    def _confirm(self, eligible: list[Candidate], mode: str) -> bool:
        win = tk.Toplevel(self)
        win.title("确认清理")
        win.configure(bg=T.BG)
        win.transient(self)
        win.resizable(False, True)
        confirmed = {"ok": False}

        wrap = tk.Frame(win, bg=T.BG)
        wrap.pack(fill="both", expand=True, padx=T.px(18), pady=T.px(16))
        tk.Label(wrap, text="确认要处理的文件", bg=T.BG, fg=T.INK, font=T.f(13, "bold"), anchor="w").pack(fill="x")
        tk.Label(
            wrap,
            text=(
                "这一步不会删除 vault 中的笔记，也不会删除 ima 中的内容。"
                if mode == "release"
                else "彻底删除会移入 Windows 回收站；不会永久删除，也不会删除 vault / ima 内容。"
            ),
            bg=T.BG,
            fg=T.MUTED,
            font=T.f(9),
            anchor="w",
            justify="left",
            wraplength=T.px(700),
        ).pack(fill="x", pady=(T.px(2), T.px(8)))

        text = tk.Text(wrap, height=min(14, max(5, len(eligible))), width=88, bg=T.CARD, fg=T.INK_SOFT, font=T.f(8), bd=0)
        text.pack(fill="both", expand=True)
        for item in eligible:
            text.insert(
                "end",
                f"{item.path.name}    {format_bytes(item.local_bytes)}    {item.path}\n",
            )
        text.configure(state="disabled")
        total = sum(item.local_bytes for item in eligible)
        tk.Label(
            wrap,
            text=f"共 {len(eligible)} 个文件，本地占用 {format_bytes(total)}",
            bg=T.BG,
            fg=T.INK,
            font=T.f(9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(T.px(8), T.px(4)))

        if not self._service.first_run_done():
            tk.Label(
                wrap,
                text=(
                    "首次执行说明：释放后文件仍在 OneDrive 云端，只把本地副本退为仅在线；"
                    "vault 与 ima 的内容不受影响。操作记录会写入 output/cleanup/operations.jsonl。"
                ),
                bg=T.AMBER_SOFT if hasattr(T, "AMBER_SOFT") else T.CARD,
                fg=T.WARN,
                font=T.f(8),
                anchor="w",
                justify="left",
                wraplength=T.px(700),
            ).pack(fill="x", pady=(0, T.px(6)))

        confirm_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            wrap,
            text="我已确认以上文件均有云端副本、vault 笔记和 ima 导入记录",
            variable=confirm_var,
            command=lambda: self._sync_confirm(win, confirm_var, delete_var, btn, mode),
            bg=T.BG,
            fg=T.INK,
            activebackground=T.BG,
            selectcolor=T.BG,
            font=T.f(9),
            bd=0,
            highlightthickness=0,
        ).pack(anchor="w")
        delete_var = tk.BooleanVar(value=False)
        if mode == "delete":
            tk.Checkbutton(
                wrap,
                text="我确认要彻底删除以上文件，删除后只能从回收站恢复",
                variable=delete_var,
                command=lambda: self._sync_confirm(win, confirm_var, delete_var, btn, mode),
                bg=T.BG,
                fg=T.DANGER,
                activebackground=T.BG,
                selectcolor=T.BG,
                font=T.f(9),
                bd=0,
                highlightthickness=0,
            ).pack(anchor="w", pady=(T.px(2), 0))

        footer = tk.Frame(wrap, bg=T.BG)
        footer.pack(fill="x", pady=(T.px(10), 0))

        def confirm_execute() -> None:
            if not confirm_var.get() or (mode == "delete" and not delete_var.get()):
                return
            confirmed["ok"] = True
            win.destroy()

        btn = RoundButton(footer, "确认执行", command=confirm_execute, kind="primary", height=34, font_size=9)
        btn.pack(side="right")
        RoundButton(footer, "取消", command=win.destroy, kind="secondary", height=34, font_size=9).pack(
            side="right", padx=(0, T.px(8))
        )
        btn.set_enabled(False)
        self._sync_confirm(win, confirm_var, delete_var, btn, mode)
        self._center_child(win)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        self.wait_window(win)
        return confirmed["ok"]

    @staticmethod
    def _sync_confirm(win, confirm_var, delete_var, button, mode: str) -> None:
        ok = confirm_var.get() and (mode != "delete" or delete_var.get())
        button.set_enabled(ok)

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "scan":
                    candidates, error = payload if isinstance(payload, tuple) else (payload, "")
                    self._on_scan_done(candidates, error)
                elif kind == "result":
                    result: ActionResult = payload
                    for candidate in self._candidates:
                        if candidate.path == result.path:
                            self._results[candidate.source_id] = result
                            break
                    self._refresh_rows()
                    self._update_buttons()
                elif kind == "done":
                    self._executing = False
                    self._on_exec_done()
                elif kind == "stopped":
                    self._executing = False
                    self.hint_label.configure(text="已停止；已完成的动作不会回滚。", fg=T.WARN)
                    self._update_buttons()
                    if self._pending_close:
                        self._do_close()
        except queue.Empty:
            pass
        if self._scanning or self._executing:
            self.after(120, self._poll)

    def _on_exec_done(self) -> None:
        if self._pending_close:
            self._do_close()
            return
        success = sum(1 for item in self._results.values() if item.status == "success")
        pending = sum(1 for item in self._results.values() if item.status == "pending")
        skipped = sum(1 for item in self._results.values() if item.status == "skipped")
        failed = sum(1 for item in self._results.values() if item.status == "failed")
        freed = sum(item.freed_bytes for item in self._results.values())
        self._service.save_last_summary(
            {"success": success, "pending": pending, "skipped": skipped, "failed": failed, "freed_bytes": freed}
        )
        summary_text = f"完成：成功 {success}，待确认 {pending}，跳过 {skipped}，失败 {failed}"
        if freed > 0:
            # 只在有可信读数时报释放字节；读数不可用的条目逐条显示"已脱水，未统计释放字节"
            summary_text += f"；释放 {format_bytes(freed)}"
        summary_text += "。"
        self.hint_label.configure(
            text=summary_text,
            fg=T.SUCCESS if failed == 0 else T.WARN,
        )
        self._update_buttons()

    # ---------------- 登记 ----------------

    def _register_ima(self) -> None:
        candidate = self._selected()
        if candidate is None:
            messagebox.showinfo("选择文件", "请先在列表中选择一个文件。", parent=self)
            return
        if not candidate.notes:
            messagebox.showwarning("无法登记", "该文件没有可对应的 vault 笔记。", parent=self)
            return
        names = "\n".join(path.name for path in candidate.notes[:12])
        if not messagebox.askyesno(
            "登记 ima 导入",
            f"确认已在 ima 中看到以下笔记？\n\n{names}\n\n登记后清理时才会把该文件视为已导入。",
            parent=self,
        ):
            return
        try:
            self._service.register_ima(candidate)
        except Exception as exc:
            messagebox.showerror("登记失败", str(exc), parent=self)
            return
        self._start_scan()

    def _register_cloud(self) -> None:
        candidate = self._selected()
        if candidate is None:
            messagebox.showinfo("选择文件", "请先在列表中选择一个文件。", parent=self)
            return
        if candidate.zone == "onedrive":
            messagebox.showwarning("无需登记", "OneDrive 同步目录内只允许释放，禁止彻底删除。", parent=self)
            return
        win = tk.Toplevel(self)
        win.title("登记云端副本")
        win.configure(bg=T.BG)
        win.transient(self)
        win.resizable(False, False)
        wrap = tk.Frame(win, bg=T.BG)
        wrap.pack(fill="both", expand=True, padx=T.px(18), pady=T.px(16))
        tk.Label(
            wrap,
            text="非 OneDrive 文件需要在云盘仍有副本，才允许彻底删除。",
            bg=T.BG,
            fg=T.INK,
            font=T.f(10, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(wrap, text="云盘名称", bg=T.BG, fg=T.MUTED, font=T.f(8)).pack(anchor="w", pady=(T.px(8), 2))
        provider_var = tk.StringVar()
        tk.Entry(wrap, textvariable=provider_var, width=46, font=T.f(9)).pack(fill="x", ipady=T.px(4))
        tk.Label(wrap, text="分享链接 / 文件 ID（可选）", bg=T.BG, fg=T.MUTED, font=T.f(8)).pack(
            anchor="w", pady=(T.px(6), 2)
        )
        reference_var = tk.StringVar()
        tk.Entry(wrap, textvariable=reference_var, width=46, font=T.f(9)).pack(fill="x", ipady=T.px(4))
        confirmed = tk.BooleanVar(value=False)
        tk.Checkbutton(
            wrap,
            text="我确认云端仍保留该文件",
            variable=confirmed,
            bg=T.BG,
            fg=T.INK,
            activebackground=T.BG,
            selectcolor=T.BG,
            font=T.f(9),
            bd=0,
            highlightthickness=0,
        ).pack(anchor="w", pady=(T.px(8), 0))

        def save() -> None:
            try:
                self._service.register_cloud(candidate, provider_var.get(), reference_var.get(), confirmed.get())
            except Exception as exc:
                messagebox.showerror("登记失败", str(exc), parent=win)
                return
            win.destroy()
            self._start_scan()

        footer = tk.Frame(wrap, bg=T.BG)
        footer.pack(fill="x", pady=(T.px(10), 0))
        RoundButton(footer, "保存登记", command=save, kind="primary", height=32, font_size=9).pack(side="right")
        RoundButton(footer, "取消", command=win.destroy, kind="secondary", height=32, font_size=9).pack(
            side="right", padx=(0, T.px(8))
        )
        self._center_child(win)
        try:
            win.grab_set()
        except tk.TclError:
            pass

    # ---------------- 关闭 ----------------

    def _open_log(self) -> None:
        try:
            self._service.open_log_dir()
        except Exception as exc:
            messagebox.showerror("打开日志失败", str(exc), parent=self)

    def _open_recycle(self) -> None:
        try:
            self._service.open_recycle_bin()
        except Exception as exc:
            messagebox.showerror("打开回收站失败", str(exc), parent=self)

    def _close(self) -> None:
        if self._executing:
            if not messagebox.askyesno("正在执行", "当前批次仍在执行，确定要停止并关闭吗？", parent=self):
                return
            self._pending_close = True
            self._stop.set()
            self.hint_label.configure(text="正在停止：当前文件完成后关闭…", fg=T.WARN)
            self.btn_start.set_enabled(False)
            self.btn_rescan.set_enabled(False)
            return
        self._do_close()

    def _do_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._hide_tooltip()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self._service.release_lock()
        self.destroy()
