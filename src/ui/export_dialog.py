"""导出弹窗：五种目标都从这里触发，失败会给出明确中文提示。

默认归档行为不在这里：不打开弹窗、不做任何操作时，流水线仍然照旧写入笔记库。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from src import exporter

from . import theme as T
from .widgets import Card, RoundButton

logger = logging.getLogger(__name__)


class ExportDialog(tk.Toplevel):
    def __init__(self, master, vault_dir: Path, latest_note: Path | None = None, on_event=None) -> None:
        super().__init__(master)
        self._vault = Path(vault_dir).expanduser()
        self._latest = Path(latest_note) if latest_note else None
        self._on_event = on_event
        self._notes: list[exporter.NoteSummary] = []
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._busy = False
        self._closed = False
        self._modal_tries = 0
        self._action_buttons: list[RoundButton] = []

        self.title("导出 · NoteFlow")
        self.configure(bg=T.BG)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())

        try:
            self._build()
            self._center(master)
        except Exception:
            self.destroy()
            raise
        self.after(0, self._activate_modal)
        self.after(60, self._load_notes)

    # ---------------- 窗口 ----------------

    def _activate_modal(self) -> None:
        """窗口真正可见后再抓焦点，失败重试；避免出现隐形但仍拦截点击的窗口。"""
        if self._closed:
            return
        try:
            self.grab_set()
        except tk.TclError:
            self._modal_tries += 1
            if self._modal_tries < 10:
                self.after(60, self._activate_modal)

    def _center(self, master) -> None:
        self.update_idletasks()
        width = max(self.winfo_reqwidth(), T.px(720))
        height = self.winfo_reqheight()
        screen_w = master.winfo_screenwidth()
        screen_h = master.winfo_screenheight()
        x = master.winfo_rootx() + max((master.winfo_width() - width) // 2, 0)
        y = master.winfo_rooty() + max((master.winfo_height() - height) // 2, 0)
        x = min(max(x, 0), max(screen_w - width, 0))
        y = min(max(y, 0), max(screen_h - height, 0))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _close(self) -> None:
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    # ---------------- 构建 ----------------

    def _build(self) -> None:
        wrap = tk.Frame(self, bg=T.BG)
        wrap.pack(fill="both", expand=True, padx=T.px(20), pady=T.px(18))

        tk.Label(wrap, text="导出", bg=T.BG, fg=T.INK, font=T.f(15, "bold"), anchor="w").pack(fill="x")
        tk.Label(
            wrap,
            text="处理结果仍会照常归档进笔记库；下面只是把已有笔记额外导出成你需要的格式。",
            bg=T.BG,
            fg=T.MUTED,
            font=T.f(9),
            anchor="w",
            justify="left",
            wraplength=T.px(680),
        ).pack(fill="x", pady=(T.px(2), T.px(12)))

        notes_card = Card(wrap, padding=14)
        notes_card.pack(fill="x")
        body = notes_card.body
        head = tk.Frame(body, bg=T.CARD)
        head.pack(fill="x")
        tk.Label(head, text="选择笔记", bg=T.CARD, fg=T.INK, font=T.f(10, "bold")).pack(side="left")
        self.count_label = tk.Label(head, text="读取中…", bg=T.CARD, fg=T.MUTED, font=T.f(8))
        self.count_label.pack(side="right")

        list_wrap = tk.Frame(body, bg=T.CARD)
        list_wrap.pack(fill="x", pady=(T.px(8), 0))
        list_wrap.grid_columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(
            list_wrap,
            height=7,
            selectmode="extended",
            exportselection=False,
            activestyle="none",
            bg="#FAFBFE",
            fg=T.INK,
            relief="flat",
            highlightthickness=T.px(1),
            highlightbackground=T.BORDER_STRONG,
            highlightcolor=T.ACCENT,
            selectbackground=T.ACCENT_SOFT,
            selectforeground=T.ACCENT,
            font=T.f(9),
        )
        self.listbox.grid(row=0, column=0, sticky="ew")
        scroll = tk.Scrollbar(list_wrap, command=self.listbox.yview, width=T.px(10))
        scroll.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scroll.set)

        tk.Label(
            body,
            text="默认选中最近处理的一篇。导出单篇 .md 选一篇；选多篇时会导出到同一个文件夹。",
            bg=T.CARD,
            fg=T.MUTED_LIGHT,
            font=T.f(8),
            anchor="w",
            justify="left",
            wraplength=T.px(650),
        ).pack(fill="x", pady=(T.px(6), 0))

        actions = Card(wrap, padding=14)
        actions.pack(fill="x", pady=(T.px(12), 0))
        abody = actions.body

        self._action_row(
            abody,
            "本地文件夹（默认）",
            f"保持现状：处理完成的结果自动归档进笔记库。{self._vault}",
            "打开笔记库",
            self._open_vault,
            kind="ghost",
        )
        self._action_row(
            abody,
            "导出单篇 Markdown（.md）",
            "把选中的笔记保存为独立的 Markdown 文件，标题、层级、重点、标签与思维导图都会保留。",
            "导出选中 .md",
            self._export_selected_md,
        )
        self._action_row(
            abody,
            "导出整库压缩包（.zip）",
            "把整个笔记库打包，方便换机器或发给别人；可用系统压缩工具直接解压。",
            "导出整库 .zip",
            self._export_zip,
        )
        self._action_row(
            abody,
            "导出 HTML 脑图（.html）",
            "生成单文件网页，双击即可在浏览器查看笔记层级、思维导图与笔记间关系，不需要安装任何软件。",
            "导出 HTML 脑图",
            self._export_html,
        )
        self._action_row(
            abody,
            "复制到剪贴板",
            "把最新一篇的 Markdown 全文复制到剪贴板，可直接粘贴到别的笔记软件。",
            "复制最新一篇",
            self._copy_latest,
            last=True,
        )

        self.status_label = tk.Label(
            wrap,
            text="",
            bg=T.BG,
            fg=T.MUTED,
            font=T.f(9),
            anchor="w",
            justify="left",
            wraplength=T.px(680),
        )
        self.status_label.pack(fill="x", pady=(T.px(10), 0))

        footer = tk.Frame(wrap, bg=T.BG)
        footer.pack(fill="x", pady=(T.px(10), 0))
        RoundButton(footer, "关闭", command=self._close, kind="secondary", height=36).pack(side="right")

    def _action_row(
        self,
        parent,
        title: str,
        description: str,
        button_text: str,
        command,
        kind: str = "secondary",
        last: bool = False,
    ) -> RoundButton:
        row = tk.Frame(parent, bg=T.CARD)
        row.pack(fill="x", pady=(0, 0 if last else T.px(12)))
        left = tk.Frame(row, bg=T.CARD)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=title, bg=T.CARD, fg=T.INK, font=T.f(10, "bold"), anchor="w").pack(fill="x")
        tk.Label(
            left,
            text=description,
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(8),
            anchor="w",
            justify="left",
            wraplength=T.px(470),
        ).pack(fill="x", pady=(T.px(2), 0))
        button = RoundButton(row, button_text, command=command, kind=kind, height=32, font_size=9)
        button.pack(side="right", padx=(T.px(12), 0))
        self._action_buttons.append(button)
        return button

    # ---------------- 数据 ----------------

    def _load_notes(self) -> None:
        if self._closed:
            return
        try:
            notes = exporter.list_notes(self._vault)
        except Exception as exc:
            logger.exception("读取笔记库失败")
            self._set_status(f"读取笔记库失败：{exc}", "error")
            self.count_label.configure(text="读取失败")
            return

        self._notes = notes
        self.listbox.delete(0, "end")
        default_index = 0
        latest_resolved = None
        if self._latest is not None:
            try:
                latest_resolved = self._latest.resolve()
            except OSError:
                latest_resolved = None

        for index, note in enumerate(notes):
            suffix = f" · 第 {note.page} 页" if note.page else ""
            self.listbox.insert("end", f"{note.title} · {note.course}{suffix}")
            if latest_resolved is not None:
                try:
                    if note.path.resolve() == latest_resolved:
                        default_index = index
                except OSError:
                    pass

        self.count_label.configure(text=f"{len(notes)} 篇")
        if not notes:
            self._set_status("笔记库中还没有笔记，先处理一份文件再来导出。", "warn")
            return
        self.listbox.selection_set(default_index)
        self.listbox.see(default_index)
        self._set_status(f"已选中《{notes[default_index].title}》，也可以按住 Ctrl 多选。", "muted")

    def _selected(self) -> list[exporter.NoteSummary]:
        return [self._notes[index] for index in self.listbox.curselection() if index < len(self._notes)]

    def _latest_summary(self) -> exporter.NoteSummary | None:
        if self._latest is not None:
            try:
                latest = self._latest.resolve()
                for note in self._notes:
                    if note.path.resolve() == latest:
                        return note
            except OSError:
                pass
        return self._notes[0] if self._notes else None

    # ---------------- 五种目标 ----------------

    def _open_vault(self) -> None:
        try:
            self._vault.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(self._vault))
            else:
                import webbrowser

                webbrowser.open(self._vault.as_uri())
            self._set_status(f"已打开笔记库：{self._vault}", "muted")
        except Exception as exc:
            self._fail(f"打开笔记库失败：{exc}")

    def _export_selected_md(self) -> None:
        selected = self._selected()
        if not selected:
            self._set_status("请先在上方列表中选中至少一篇笔记。", "warn")
            return

        if len(selected) == 1:
            note = selected[0]
            target = filedialog.asksaveasfilename(
                parent=self,
                title="导出 Markdown",
                initialdir=str(self._vault),
                initialfile=f"{note.stem}.md",
                defaultextension=".md",
                filetypes=[("Markdown", "*.md"), ("所有文件", "*.*")],
            )
            if not target:
                return
            self._start_task(
                "正在导出 Markdown",
                lambda: exporter.export_markdown(note.path, Path(target)),
                lambda path: f"已导出 1 篇笔记：{path}",
            )
            return

        target_dir = filedialog.askdirectory(parent=self, title="选择导出文件夹", initialdir=str(self._vault.parent))
        if not target_dir:
            return
        paths = [note.path for note in selected]
        self._start_task(
            "正在导出 Markdown",
            lambda: exporter.export_markdowns(paths, Path(target_dir)),
            lambda outputs: f"已导出 {len(outputs)} 篇笔记到：{target_dir}",
        )

    def _export_zip(self) -> None:
        if not self._notes:
            self._set_status("笔记库中还没有可以打包的笔记。", "warn")
            return
        target = filedialog.asksaveasfilename(
            parent=self,
            title="导出整库压缩包",
            initialdir=str(Path.home()),
            initialfile=f"NoteFlow-笔记库-{self._stamp()}.zip",
            defaultextension=".zip",
            filetypes=[("ZIP 压缩包", "*.zip"), ("所有文件", "*.*")],
        )
        if not target:
            return
        self._start_task(
            "正在打包笔记库",
            lambda: exporter.export_vault_zip(self._vault, Path(target)),
            lambda result: (
                f"已导出整库：{result[0]}（{result[1]} 个文件，{result[2] / 1024 / 1024:.1f} MB）"
            ),
        )

    def _export_html(self) -> None:
        if not self._notes:
            self._set_status("笔记库中还没有可以生成的笔记。", "warn")
            return
        target = filedialog.asksaveasfilename(
            parent=self,
            title="导出 HTML 脑图",
            initialdir=str(Path.home()),
            initialfile=f"NoteFlow-脑图-{self._stamp()}.html",
            defaultextension=".html",
            filetypes=[("HTML 网页", "*.html"), ("所有文件", "*.*")],
        )
        if not target:
            return
        self._start_task(
            "正在生成 HTML 脑图",
            lambda: exporter.export_mindmap_html(self._vault, Path(target)),
            lambda path: f"已导出 HTML 脑图：{path}（双击即可在浏览器打开）",
        )

    def _copy_latest(self) -> None:
        note = self._latest_summary()
        if note is None:
            self._set_status("笔记库中还没有可复制的笔记。", "warn")
            return
        try:
            path, text = exporter.latest_text(self._vault, preferred=note.path)
            exporter.set_clipboard(self, text)
        except Exception as exc:
            logger.exception("复制最新笔记失败")
            self._fail(f"复制失败：{exc}")
            return
        self._set_status(f"已复制《{note.title}》的 Markdown 全文（{len(text)} 字），可直接粘贴。", "success")
        self._notify("success", f"已复制最新一篇 Markdown 到剪贴板：{path}")

    # ---------------- 后台任务 ----------------

    def _start_task(self, label: str, func, describe) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_actions_enabled(False)
        self._set_status(f"{label}…", "muted")
        threading.Thread(
            target=self._task_worker,
            args=(func, describe),
            name="noteflow-export",
            daemon=True,
        ).start()
        self.after(100, self._poll_task)

    def _task_worker(self, func, describe) -> None:
        try:
            result = func()
        except Exception as exc:
            logger.exception("导出失败")
            self._queue.put(("error", self._describe_error(exc)))
            return
        try:
            message = describe(result)
        except Exception:
            message = "导出完成。"
        self._queue.put(("ok", message))

    def _poll_task(self) -> None:
        if self._closed:
            return
        try:
            kind, message = self._queue.get_nowait()
        except queue.Empty:
            if self._busy:
                self.after(100, self._poll_task)
            return

        self._busy = False
        self._set_actions_enabled(True)
        if kind == "ok":
            self._set_status(message, "success")
            self._notify("success", message)
        else:
            self._set_status(message, "error")
            self._notify("error", message)

    def _describe_error(self, exc: BaseException) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        if isinstance(exc, PermissionError) or "permission" in text.lower() or "access is denied" in text.lower():
            return "导出失败：没有写入权限，请换一个可写的位置。"
        if isinstance(exc, FileNotFoundError):
            return f"导出失败：{text}"
        if text.startswith("导出失败") or text.startswith("写入失败") or text.startswith("打包失败"):
            return text
        return f"导出失败：{text[:160]}"

    # ---------------- 状态 ----------------

    def _set_status(self, text: str, level: str) -> None:
        color = {
            "error": T.DANGER,
            "warn": T.WARN,
            "success": T.SUCCESS,
            "muted": T.MUTED,
        }.get(level, T.MUTED)
        try:
            self.status_label.configure(text=text, fg=color)
        except tk.TclError:
            pass

    def _fail(self, message: str) -> None:
        self._set_status(message, "error")
        self._notify("error", message)

    def _notify(self, level: str, message: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(level, message)
        except Exception:
            logger.exception("导出结果通知主界面失败")

    def _set_actions_enabled(self, enabled: bool) -> None:
        for button in self._action_buttons:
            button.set_enabled(enabled)

    @staticmethod
    def _stamp() -> str:
        from datetime import datetime

        return datetime.now().strftime("%Y%m%d-%H%M")
