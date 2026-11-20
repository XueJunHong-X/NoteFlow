"""主窗口：只负责布局与事件，所有处理都交给 Controller。"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from config import DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS
from src.appconfig import AppConfig
from src.controller import Controller, LogBridge
from src.openers import open_ima, open_in_obsidian

from . import theme as T
from .export_dialog import ExportDialog
from .settings_dialog import SettingsDialog
from .widgets import Banner, Card, DropZone, FailurePanel, LogPanel, ModeSwitch, ProgressBar, RoundButton, elide

logger = logging.getLogger(__name__)


def _blend(start: str, end: str, ratio: float) -> str:
    """在两条 #RRGGBB 之间插值，用来做提示文字的淡出。"""
    ratio = max(0.0, min(1.0, ratio))
    left = tuple(int(start[index : index + 2], 16) for index in (1, 3, 5))
    right = tuple(int(end[index : index + 2], 16) for index in (1, 3, 5))
    return "#" + "".join(f"{int(a + (b - a) * ratio):02x}" for a, b in zip(left, right))


class NoteFlowApp:
    def __init__(self, root: tk.Tk, config: AppConfig) -> None:
        self.root = root
        self.config = config
        self._exts = tuple(IMAGE_EXTENSIONS) + tuple(DOCUMENT_EXTENSIONS)
        self._events: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.controller = Controller(self._push_event)
        self.controller.apply_config(config)

        self._state = "idle"
        self._expected_pages = 0
        self._done_pages = 0
        self._started_at = 0.0
        self._current_name = ""
        self._file_prefix = ""
        self._failure_items: list[dict] = []
        self._last_note = ""
        self._pending_real = False
        self._estimate_token = 0
        self._dialog: SettingsDialog | None = None
        self._export_dialog: ExportDialog | None = None
        self._scale = 1.0
        # 并发反馈：档位、限流提示的淡出进度，以及 ETA 的速率估计
        self._concurrency_prev: int | None = None
        self._concurrency_render: tuple[str, str] = ("", "")
        self._concurrency_hold_until = 0.0
        self._eta_rate = 0.0
        self._eta_last_at = 0.0
        self._last_stats: dict = {}

        self._install_logging()
        self._build_window()
        self._build_header()
        self._build_path_strip()
        self.banner = Banner(self.root)
        self._build_body()
        self.banner.set_anchor(self._body)
        self._build_footer()
        self._hook_drop()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._apply_config(initial=True)
        self.root.after(80, self._poll)
        self.root.after(260, self._refresh_estimate_async)

    # ================= 构建界面 =================

    def _build_window(self) -> None:
        root = self.root
        root.title("NoteFlow · 手写笔记智能整理")
        root.configure(bg=T.BG)
        self._scale = T.set_scale(root)
        width = T.px(1180)
        height = T.px(820)
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = max((screen_w - width) // 2, 0)
        y = max((screen_h - height) // 2 - T.px(12), 0)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.minsize(T.px(1040), T.px(740))

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=T.CARD, height=T.px(64))
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Frame(self.root, bg=T.BORDER, height=T.px(1)).pack(fill="x")

        inner = tk.Frame(header, bg=T.CARD)
        inner.pack(fill="both", expand=True, padx=T.px(22))

        logo_size = T.px(34)
        logo = tk.Canvas(inner, width=logo_size, height=logo_size, bg=T.CARD, highlightthickness=0, bd=0)
        T.draw_round_rect(logo, 1, 1, logo_size - 1, logo_size - 1, T.px(9), fill=T.ACCENT, outline="")
        logo.create_text(logo_size / 2, logo_size / 2, text="N", fill="#FFFFFF", font=T.f(14, "bold"))
        logo.pack(side="left")

        titles = tk.Frame(inner, bg=T.CARD)
        titles.pack(side="left", padx=(T.px(10), 0))
        tk.Label(titles, text="NoteFlow", bg=T.CARD, fg=T.INK, font=T.f(13, "bold"), anchor="w").pack(anchor="w")
        tk.Label(
            titles,
            text="手写笔记丢进来，自动变成可检索的电子笔记",
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(9),
            anchor="w",
        ).pack(anchor="w")

        settings = RoundButton(inner, "设置", command=self._open_settings, kind="secondary", height=34, font_size=9)
        settings.pack(side="right")
        export = RoundButton(inner, "导出", command=self._open_export, kind="secondary", height=34, font_size=9)
        export.pack(side="right", padx=(0, T.px(8)))
        self.mode_switch = ModeSwitch(inner, on_change=self._on_mode_change, mode="offline")
        self.mode_switch.pack(side="right", padx=(0, T.px(10)))

    def _build_path_strip(self) -> None:
        strip = tk.Frame(self.root, bg=T.BG)
        strip.pack(fill="x", padx=T.px(22), pady=(T.px(8), T.px(4)))

        left = tk.Frame(strip, bg=T.BG)
        left.pack(side="left")
        tk.Label(left, text="收件箱", bg=T.BG, fg=T.MUTED, font=T.f(8), anchor="w").pack(side="left")
        self.watch_label = tk.Label(left, text="", bg=T.BG, fg=T.INK_SOFT, font=T.f(9), anchor="w", width=46)
        self.watch_label.pack(side="left", padx=(T.px(6), T.px(4)))
        RoundButton(left, "更改", command=self._change_watch_dir, kind="ghost", height=24, font_size=8, padx=8).pack(
            side="left"
        )

        right = tk.Frame(strip, bg=T.BG)
        right.pack(side="right")
        RoundButton(right, "打开", command=self._open_vault, kind="ghost", height=24, font_size=8, padx=8).pack(
            side="right", padx=(T.px(4), 0)
        )
        self.vault_label = tk.Label(right, text="", bg=T.BG, fg=T.INK_SOFT, font=T.f(9), anchor="e", width=40)
        self.vault_label.pack(side="right")
        tk.Label(right, text="笔记库", bg=T.BG, fg=T.MUTED, font=T.f(8)).pack(side="right", padx=(0, T.px(6)))

    def _build_body(self) -> None:
        body = tk.Frame(self.root, bg=T.BG)
        body.pack(fill="both", expand=True, padx=T.px(22), pady=(T.px(2), 0))
        self._body = body
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=3, minsize=T.px(560))
        body.grid_columnconfigure(1, weight=2, minsize=T.px(360))

        left = tk.Frame(body, bg=T.BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, T.px(12)))
        right = tk.Frame(body, bg=T.BG)
        right.grid(row=0, column=1, sticky="nsew")

        self.drop = DropZone(left, self._choose_files, self._choose_folder)
        self.drop.pack(fill="x")

        actions = tk.Frame(left, bg=T.BG)
        actions.pack(fill="x", pady=(T.px(12), 0))
        self.btn_start = RoundButton(actions, "开始处理", command=self._start_inbox, kind="primary", height=40, width=112)
        self.btn_start.pack(side="left")
        self.btn_watch = RoundButton(
            actions, "开始监听", command=self._toggle_watch, kind="secondary", height=40, width=112
        )
        self.btn_watch.pack(side="left", padx=(T.px(8), 0))
        RoundButton(actions, "打开笔记库", command=self._open_vault, kind="ghost", height=40).pack(
            side="left", padx=(T.px(8), 0)
        )
        RoundButton(actions, "离线演示", command=self._run_sample, kind="ghost", height=40).pack(side="right")

        openers = tk.Frame(left, bg=T.BG)
        openers.pack(fill="x", pady=(T.px(8), 0))
        RoundButton(
            openers, "在 Obsidian 中打开", command=self._open_in_obsidian, kind="ghost", height=34, font_size=9
        ).pack(side="left")
        RoundButton(
            openers, "打开 ima", command=self._open_ima, kind="ghost", height=34, font_size=9
        ).pack(side="left", padx=(T.px(8), 0))

        task_card = Card(left)
        task_card.pack(fill="x", pady=(T.px(12), 0))
        tb = task_card.body
        top = tk.Frame(tb, bg=T.CARD)
        top.pack(fill="x")
        tk.Label(top, text="本次任务", bg=T.CARD, fg=T.INK, font=T.f(11, "bold")).pack(side="left")
        self.task_state = tk.Label(top, text="空闲", bg=T.CARD, fg=T.MUTED, font=T.f(9))
        self.task_state.pack(side="right")

        self.progress = ProgressBar(tb)
        self.progress.pack(fill="x", pady=(T.px(10), T.px(8)))

        line = tk.Frame(tb, bg=T.CARD)
        line.pack(fill="x")
        self.current_label = tk.Label(
            line,
            text="还没有开始：拖入文件，或点“开始处理”",
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(9),
            anchor="w",
        )
        self.current_label.pack(side="left", fill="x", expand=True)
        self.pct_label = tk.Label(line, text="0%", bg=T.CARD, fg=T.ACCENT, font=T.f(10, "bold"))
        self.pct_label.pack(side="right")
        self.concurrency_label = tk.Label(line, text="", bg=T.CARD, fg=T.MUTED, font=T.f(9))
        self.concurrency_label.pack(side="right", padx=(0, T.px(10)))

        self.meta_label = tk.Label(
            tb,
            text="文件 0/0 · 页 0 · 失败 0 · 用时 00:00",
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(9),
            anchor="w",
        )
        self.meta_label.pack(fill="x", pady=(T.px(6), 0))

        bottom = tk.Frame(tb, bg=T.CARD)
        bottom.pack(fill="x", pady=(T.px(4), 0))
        self.estimate_label = tk.Label(
            bottom, text="正在估算收件箱…", bg=T.CARD, fg=T.MUTED_LIGHT, font=T.f(8), anchor="w"
        )
        self.estimate_label.pack(side="left")
        self.btn_recent = RoundButton(
            bottom, "打开最近一篇", command=self._open_recent, kind="ghost", height=26, font_size=8, padx=10
        )

        failure_card = Card(left)
        failure_card.pack(fill="x", pady=(T.px(12), 0))
        self.failure_panel = FailurePanel(failure_card.body, self._retry_failed)
        self.failure_panel.pack(fill="x")

        log_card = Card(right)
        log_card.pack(fill="both", expand=True)
        self.log_panel = LogPanel(log_card.body, height=14)
        self.log_panel.pack(fill="both", expand=True)

        tips_card = Card(right)
        tips_card.pack(fill="x", pady=(T.px(12), 0))
        tk.Label(tips_card.body, text="小贴士", bg=T.CARD, fg=T.INK, font=T.f(11, "bold"), anchor="w").pack(fill="x")
        tips = (
            "• 真实识别每页调用一次模型，通常 6–35 秒\n"
            "• 拖入文件夹会把它设为收件箱\n"
            "• .goodnotes 只有手写笔迹时会失败，建议导出 PDF\n"
            "• 离线示例不联网，结果仅用于演示"
        )
        tk.Label(
            tips_card.body,
            text=tips,
            bg=T.CARD,
            fg=T.MUTED,
            font=T.f(8),
            justify="left",
            anchor="w",
            wraplength=T.px(320),
        ).pack(fill="x", pady=(T.px(6), 0))

    def _build_footer(self) -> None:
        footer = tk.Frame(self.root, bg=T.BG)
        footer.pack(fill="x", padx=T.px(22), pady=(T.px(8), T.px(10)))
        self.status_label = tk.Label(footer, text="就绪", bg=T.BG, fg=T.MUTED, font=T.f(8), anchor="w")
        self.status_label.pack(side="left")
        tk.Label(
            footer,
            text="隐私：真实识别会把页面发送给你自己配置的模型，密钥只保存在本机",
            bg=T.BG,
            fg=T.MUTED_LIGHT,
            font=T.f(8),
        ).pack(side="right")

    # ================= 配置 =================

    def _apply_config(self, initial: bool = False) -> None:
        self.controller.apply_config(self.config)
        for key in ("watch_dir", "output_dir", "vault_dir"):
            try:
                Path(getattr(self.config, key)).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                if initial:
                    self.banner.show(f"目录创建失败：{getattr(self.config, key)}（{exc}）", "error")
        self.watch_label.configure(text=elide(self.config.watch_dir, 46))
        self.vault_label.configure(text=elide(self.config.vault_dir, 40))
        self.mode_switch.set_mode("real" if not self.config.is_offline else "offline")
        self._update_mode_ui()

        if initial:
            repaired = getattr(self.config, "repaired", []) or []
            load_error = getattr(self.config, "load_error", "") or ""
            if load_error:
                self.banner.show(load_error, "error")
            elif repaired:
                self.banner.show("；".join(repaired), "warn")
            elif not self.config.api_key.strip():
                self.banner.show(
                    "还没有配置 API Key，当前只能离线演示。点右上角“设置”填入 Key 后即可真实识别。",
                    "warn",
                )

    def _update_mode_ui(self) -> None:
        if self.config.is_offline:
            self.drop.set_hint("当前离线示例：不联网、不调用模型 · 支持 图片 / PDF / .goodnotes")
        else:
            self.drop.set_hint("当前真实识别：每页一次模型调用（6–35 秒）· 支持 图片 / PDF / .goodnotes")

    def _save_config(self) -> bool:
        try:
            self.config.save()
            return True
        except OSError as exc:
            self.banner.show(f"配置保存失败：{exc}", "error")
            return False

    def _on_mode_change(self, mode: str) -> None:
        if mode == "real" and not self.config.api_key.strip():
            self._pending_real = True
            self.mode_switch.set_mode("offline")
            self.banner.show("真实识别需要 API Key：在设置里填写并测试连接后保存即可。", "warn")
            self._open_settings()
            return
        self.config.offline_mode = mode == "offline"
        self._save_config()
        self.mode_switch.set_mode("real" if not self.config.is_offline else "offline")
        self._update_mode_ui()
        if self.config.is_offline:
            self.log_panel.append("INFO", "已切换到离线示例模式，接下来不会调用模型。")
        else:
            self.log_panel.append("SUCCESS", "已切换到真实识别模式。")
            self.banner.hide()

    def _on_settings_saved(self, new_config: AppConfig) -> None:
        self.config = new_config
        self.controller.apply_config(new_config)
        if self._pending_real and new_config.api_key.strip():
            self.config.offline_mode = False
            self._pending_real = False
            self._save_config()
        self._apply_config()
        self.log_panel.append("SUCCESS", "设置已保存。")
        self.banner.show("设置已保存。", "success", auto_hide_ms=4000)
        self._refresh_estimate_async()

    def _open_settings(self) -> None:
        try:
            dialog = self._dialog
            if dialog is not None and dialog.winfo_exists():
                self._bring_to_front(dialog)
                return
            dialog = SettingsDialog(self.root, self.config, self._on_settings_saved)
            self._dialog = dialog
            if self._pending_real:
                dialog.focus_key()
            self._bring_to_front(dialog)
        except Exception as exc:  # 打包后没有控制台，异常不能静默成「点了没反应」
            logger.exception("打开设置窗口失败")
            self.banner.show(f"设置窗口打开失败：{exc}", "error")

    def _open_export(self) -> None:
        try:
            dialog = self._export_dialog
            if dialog is not None and dialog.winfo_exists():
                self._bring_to_front(dialog)
                return
            dialog = ExportDialog(
                self.root,
                Path(self.config.vault_dir).expanduser(),
                latest_note=Path(self._last_note) if self._last_note else None,
                on_event=self._on_export_event,
            )
            self._export_dialog = dialog
            self._bring_to_front(dialog)
        except Exception as exc:  # 打包后没有控制台，异常不能静默成「点了没反应」
            logger.exception("打开导出窗口失败")
            self.banner.show(f"导出窗口打开失败：{exc}", "error")

    def _on_export_event(self, level: str, text: str) -> None:
        tag = {"error": "ERROR", "warn": "WARNING", "success": "SUCCESS"}.get(level, "INFO")
        self.log_panel.append(tag, text)
        if level == "error":
            self.banner.show(text, "error")

    def _bring_to_front(self, dialog: tk.Toplevel) -> None:
        """确保弹窗一定出现在主窗口之上（Windows 前台锁可能把它压在后面）。"""
        try:
            dialog.deiconify()
            dialog.lift()
            dialog.attributes("-topmost", True)
            dialog.update_idletasks()
            dialog.focus_force()
            dialog.after(200, lambda: dialog.winfo_exists() and dialog.attributes("-topmost", False))
        except tk.TclError:
            pass

    # ================= 任务控制 =================

    def _start_inbox(self) -> None:
        if self._state != "idle":
            self.banner.show("当前还有任务在运行，请先停止。", "warn")
            return
        watch = Path(self.config.watch_dir).expanduser()
        if not watch.is_dir():
            self.banner.show(f"收件箱不存在：{watch}", "error")
            return
        files = self._files_in(watch)
        if not files:
            self.banner.show("收件箱里还没有可处理的文件，拖一个进来试试。", "info", auto_hide_ms=6000)
            return
        self._start_paths(files, "收件箱")

    def _files_in(self, folder: Path) -> list[Path]:
        try:
            return sorted(
                p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in self._exts and not p.name.startswith(".")
            )
        except OSError:
            return []

    def _start_paths(self, paths: list[Path], source_label: str) -> None:
        if self._state != "idle":
            self.banner.show("当前还有任务在运行，请先停止。", "warn")
            return
        estimate = self.controller.estimate(paths)
        self._expected_pages = max(estimate.pages, 0)
        self._done_pages = 0
        self._started_at = time.time()
        self._reset_task_ui()
        self.current_label.configure(text=f"准备处理 {len(paths)} 个文件…", fg=T.INK)
        self.estimate_label.configure(text=self._estimate_text(estimate, source_label))
        self.btn_recent.pack_forget()

        self.log_panel.append(
            "INFO",
            f"开始处理 {len(paths)} 个文件 · 预计 {estimate.pages} 页"
            + ("（离线示例，不调用模型）" if estimate.calls == 0 else f" / {estimate.calls} 次调用"),
        )
        if not self.controller.start_batch(paths):
            self.task_state.configure(text="空闲", fg=T.MUTED)

    def _estimate_text(self, estimate, label: str) -> str:
        if estimate.files == 0:
            return "没有可处理的文件"
        if estimate.calls == 0:
            return f"{label} {estimate.files} 个文件 · 预计 {estimate.pages} 页 · 离线示例不调用模型"
        low = max(int(estimate.calls * 6 / 60), 1)
        high = max(int(estimate.calls * 35 / 60), low + 1)
        return f"{label} {estimate.files} 个文件 · 预计 {estimate.pages} 页 / {estimate.calls} 次调用 · 约 {low}–{high} 分钟"

    def _toggle_watch(self) -> None:
        if self.controller.watching:
            self.controller.stop_watch()
            return
        if self._state != "idle":
            self.banner.show("当前还有任务在运行，请先停止。", "warn")
            return
        watch = Path(self.config.watch_dir).expanduser()
        try:
            watch.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.banner.show(f"收件箱目录不可用：{exc}", "error")
            return
        self._expected_pages = 0
        self._done_pages = 0
        self._started_at = time.time()
        self._reset_task_ui()
        self.current_label.configure(text="等待新文件…", fg=T.MUTED)
        if self.controller.start_watch():
            self.banner.show(
                f"已开始监听：{watch}。之后放进来的文件会自动处理；已有文件请点“开始处理”。",
                "success",
                auto_hide_ms=8000,
            )

    def _run_sample(self) -> None:
        if self._state != "idle":
            self.banner.show("当前还有任务在运行，请先停止。", "warn")
            return
        self._expected_pages = 0
        self._done_pages = 0
        self._started_at = time.time()
        self._reset_task_ui()
        self.current_label.configure(text="正在运行离线示例…", fg=T.INK)
        self.controller.start_sample()

    def _retry_failed(self) -> None:
        sources: list[Path] = []
        for item in self._failure_items:
            path = Path(item.get("source", ""))
            if path.is_file() and path not in sources:
                sources.append(path)
        if not sources:
            self.banner.show("失败文件已经不在原位置，无法重试。", "warn")
            return
        estimate = self.controller.estimate(sources)
        self._expected_pages = estimate.pages
        self._done_pages = 0
        self._started_at = time.time()
        self._reset_task_ui()
        self.current_label.configure(text=f"重试 {len(sources)} 个失败文件…", fg=T.INK)
        self.controller.retry_failures()

    def _stop(self) -> None:
        if self.controller.watching:
            self.controller.stop_watch()
        else:
            self.controller.cancel()

    # ================= 拖拽与选择 =================

    def _hook_drop(self) -> None:
        try:
            import windnd
        except Exception:
            self.log_panel.append("WARNING", "拖拽组件不可用，请使用“选择文件/选择文件夹”。")
            return

        def handler(files) -> None:
            decoded = []
            for item in files:
                if isinstance(item, bytes):
                    try:
                        item = item.decode("gbk")
                    except UnicodeDecodeError:
                        item = item.decode("utf-8", "replace")
                decoded.append(str(item))
            try:
                self.root.after(0, lambda: self._on_drop(decoded))
            except tk.TclError:
                pass

        try:
            windnd.hook_dropfiles(self.root.winfo_id(), func=handler, force_unicode=True)
            self.log_panel.append("INFO", "拖拽已就绪：把文件或文件夹直接拖进窗口即可开始。")
        except Exception as exc:
            self.log_panel.append("WARNING", f"拖拽初始化失败：{exc}")

    def _on_drop(self, paths: list[str]) -> None:
        if self._state != "idle":
            self.banner.show("当前还有任务在运行，请先停止。", "warn")
            return
        files: list[Path] = []
        folders: list[Path] = []
        ignored = 0
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                folders.append(path)
            elif path.is_file() and path.suffix.lower() in self._exts:
                files.append(path)
            else:
                ignored += 1

        if folders:
            target = folders[0]
            if len(folders) > 1:
                self.banner.show(f"一次只接管一个文件夹，已使用：{target}", "info", auto_hide_ms=6000)
            self.config.watch_dir = str(target)
            self._save_config()
            self.watch_label.configure(text=elide(self.config.watch_dir, 46))
            self.log_panel.append("INFO", f"收件箱已设为：{target}")
            existing = self._files_in(target)
            for path in existing:
                if path not in files:
                    files.append(path)

        if ignored:
            self.log_panel.append("WARNING", f"已忽略 {ignored} 个不支持的文件。")
        if not files:
            if folders:
                self.banner.show("已把这个文件夹设为收件箱，目前里面没有可处理的文件。", "info", auto_hide_ms=6000)
            else:
                self.banner.show("拖入的内容不是支持的格式（图片 / PDF / .goodnotes）。", "warn")
            return
        self._start_paths(files, "拖入")

    def _choose_files(self) -> None:
        initial = self.config.watch_dir if Path(self.config.watch_dir).is_dir() else str(Path.home())
        chosen = filedialog.askopenfilenames(
            parent=self.root,
            title="选择要整理的笔记",
            initialdir=initial,
            filetypes=[
                ("支持的笔记", "*.png *.jpg *.jpeg *.bmp *.webp *.pdf *.goodnotes"),
                ("图片", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("PDF", "*.pdf"),
                ("所有文件", "*.*"),
            ],
        )
        if not chosen:
            return
        files = [Path(p) for p in chosen if Path(p).suffix.lower() in self._exts]
        if not files:
            self.banner.show("选中的文件格式不支持。", "warn")
            return
        self._start_paths(files, "选择")

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="选择收件箱文件夹",
            initialdir=self.config.watch_dir if Path(self.config.watch_dir).is_dir() else str(Path.home()),
        )
        if not chosen:
            return
        self.config.watch_dir = chosen
        self._save_config()
        self.watch_label.configure(text=elide(chosen, 46))
        files = self._files_in(Path(chosen))
        if not files:
            self.banner.show("已把该文件夹设为收件箱，目前里面没有可处理的文件。", "info", auto_hide_ms=6000)
            return
        self._start_paths(files, "文件夹")

    def _change_watch_dir(self) -> None:
        self._choose_folder()

    # ================= 打开目录 =================

    def _open_vault(self) -> None:
        path = Path(self.config.vault_dir).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.banner.show(f"打开笔记库失败：{exc}", "error")
            return
        self._open_path(path)

    def _open_in_obsidian(self) -> None:
        path = Path(self.config.vault_dir).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.banner.show(f"打开笔记库失败：{exc}", "error")
            return
        ok, message = open_in_obsidian(path)
        if ok:
            self.banner.show(message, "success", auto_hide_ms=6000)
            return
        # 没装 Obsidian / 协议未注册 / 唤起失败：明确提示，并回退到打开文件夹
        self.banner.show(message, "warn")
        self._open_path(path)

    def _open_ima(self) -> None:
        ok, message = open_ima()
        self.banner.show(message, "success" if ok else "warn", auto_hide_ms=12000 if ok else 0)

    def _open_recent(self) -> None:
        if not self._last_note:
            return
        path = Path(self._last_note)
        target = path if path.exists() else path.parent
        if not target.exists():
            self.banner.show("最近一篇笔记已被移动或删除。", "warn")
            return
        self._open_path(target)

    def _open_path(self, path: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                import webbrowser

                webbrowser.open(path.as_uri())
        except OSError as exc:
            self.banner.show(f"无法打开目录：{exc}", "error")

    # ================= 事件 =================

    def _push_event(self, kind: str, payload: dict) -> None:
        self._events.put((kind, payload))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                handler = getattr(self, f"_on_{kind}", None)
                if handler is None:
                    continue
                try:
                    handler(payload)
                except Exception as exc:  # 界面处理异常不能拖垮主循环
                    logger.exception("界面处理 %s 事件失败: %s", kind, exc)
        except queue.Empty:
            pass
        self._refresh_concurrency()
        if self.root.winfo_exists():
            self.root.after(80, self._poll)

    def _on_state(self, payload: dict) -> None:
        state = payload.get("state", "idle")
        message = payload.get("message", "")
        self._state = state
        self.status_label.configure(text=message or "就绪")

        if state == "running":
            self._set_start_button("停止处理", "danger", True)
            self._set_watch_button("开始监听", "secondary", False)
            self.drop.set_enabled(False)
            self.mode_switch.set_enabled(False)
            self.task_state.configure(text="进行中", fg=T.ACCENT)
        elif state == "watching":
            self._set_start_button("开始处理", "primary", False)
            self._set_watch_button("停止监听", "danger", True)
            self.drop.set_enabled(False)
            self.mode_switch.set_enabled(False)
            self.task_state.configure(text="监听中", fg=T.SUCCESS)
        elif state == "stopping":
            self.btn_start.set_enabled(False)
            self.btn_watch.set_enabled(False)
            self.drop.set_enabled(False)
            self.task_state.configure(text="正在停止", fg=T.WARN)
        else:
            self._set_start_button("开始处理", "primary", True)
            self._set_watch_button("开始监听", "secondary", True)
            self.drop.set_enabled(True)
            self.mode_switch.set_enabled(True)
            self.task_state.configure(text=message or "空闲", fg=T.MUTED)
            self.failure_panel.set_retry_enabled(bool(self._failure_items))
            self._refresh_estimate_async()

    def _set_start_button(self, text: str, kind: str, enabled: bool) -> None:
        self.btn_start.set_kind(kind)
        self.btn_start.set_text(text)
        self.btn_start.set_enabled(enabled)

    def _set_watch_button(self, text: str, kind: str, enabled: bool) -> None:
        self.btn_watch.set_kind(kind)
        self.btn_watch.set_text(text)
        self.btn_watch.set_enabled(enabled)

    def _on_log(self, payload: dict) -> None:
        self.log_panel.append(payload.get("level", "INFO"), payload.get("text", ""))

    def _on_notice(self, payload: dict) -> None:
        level = payload.get("level", "info")
        text = payload.get("text", "")
        self.banner.show(text, level, auto_hide_ms=6000 if level in ("info", "success") else 0)
        tag = {"info": "INFO", "warn": "WARNING", "error": "ERROR"}.get(level, "INFO")
        self.log_panel.append(tag, text)

    def _on_failures_clear(self, _payload: dict) -> None:
        self._failure_items = []
        self.failure_panel.set_items([])

    def _on_failure(self, payload: dict) -> None:
        self._failure_items.append(payload)
        self.failure_panel.set_items(self._failure_items)
        self.failure_panel.set_retry_enabled(self._state == "idle")

    def _on_file(self, payload: dict) -> None:
        self._current_name = payload.get("name", "")
        index = payload.get("index", 1)
        total = payload.get("total", 1)
        self._file_prefix = f"文件 {index}/{total} · " if total > 1 else ""
        self.current_label.configure(text=f"{self._file_prefix}{self._current_name}", fg=T.INK)
        if payload.get("watch"):
            self.progress.set_value(None)

    def _on_page(self, payload: dict) -> None:
        stage = payload.get("stage")
        page = payload.get("page", 0)
        total = payload.get("total", 0)
        if stage == "start":
            self.current_label.configure(
                text=f"{getattr(self, '_file_prefix', '')}{self._current_name} · 第 {page}/{total} 页", fg=T.INK
            )
        elif stage in ("ok", "failed"):
            self._done_pages += 1
            # 用页完成间隔估计速率；档位变化时再按比例即时修正
            now = time.monotonic()
            if self._eta_last_at:
                gap = now - self._eta_last_at
                if 0.05 <= gap <= 60:
                    instant = 1.0 / gap
                    self._eta_rate = instant if self._eta_rate <= 0 else self._eta_rate * 0.85 + instant * 0.15
            self._eta_last_at = now

        if self._expected_pages > 0:
            value = min(self._done_pages / self._expected_pages, 1.0)
            self.progress.set_value(value)
            self.pct_label.configure(text=f"{int(value * 100)}%")
        else:
            self.progress.set_value(None)
            self.pct_label.configure(text="处理中")

    def _on_stats(self, payload: dict) -> None:
        self._last_stats = payload
        self._render_meta()

    def _render_meta(self) -> None:
        payload = self._last_stats
        if not payload:
            return
        elapsed = int(time.time() - self._started_at) if self._started_at else 0
        if payload.get("watch"):
            text = f"本次文件 · 页 {payload.get('pages_ok', 0)} · 失败 {payload.get('failed', 0)} · 用时 {self._fmt(elapsed)}"
        else:
            text = (
                f"文件 {payload.get('index', 0)}/{payload.get('total', 0)}"
                f" · 页 {payload.get('pages_ok', 0)}"
                f" · 失败 {payload.get('failed', 0)}"
                f" · 用时 {self._fmt(elapsed)}"
            )
        eta = self._eta_text()
        self.meta_label.configure(text=f"{text} · {eta}" if eta else text)

    def _eta_text(self) -> str:
        """剩余时间：页速率按当前有效并发实时折算，降档后不会继续按旧速度估。"""
        if self._state != "running" or self._expected_pages <= 0 or self._done_pages <= 0:
            return ""
        remaining = self._expected_pages - self._done_pages
        if remaining <= 0 or self._eta_rate <= 0:
            return ""
        seconds = int(remaining / self._eta_rate)
        if seconds <= 0 or seconds > 3 * 3600:
            return ""
        return f"剩余约 {self._fmt(seconds)}"

    def _refresh_concurrency(self) -> None:
        """按 80ms 节奏读一次并发快照：稳态显示「并发 N」，降级时短暂提示后淡回。"""
        if self._state != "running":
            if self._concurrency_render[0]:
                self.concurrency_label.configure(text="")
                self._concurrency_render = ("", "")
                self._concurrency_prev = None
            return
        try:
            status = self.controller.concurrency_status()
        except Exception:
            return
        limit = int(status.get("limit") or 0)
        if limit <= 0:
            return

        now = time.monotonic()
        previous = self._concurrency_prev
        if previous is not None and limit != previous:
            if self._eta_rate > 0:
                self._eta_rate *= limit / previous
                self._render_meta()
            if limit < previous:
                self._concurrency_hold_until = now + 3.0
        self._concurrency_prev = limit

        hold = self._concurrency_hold_until
        if hold and now < hold:
            text, fg = f"已降到 {limit}（限流）", T.WARN
        elif hold and now < hold + 1.6:
            text = f"已降到 {limit}（限流）"
            fg = _blend(T.WARN, T.MUTED, (now - hold) / 1.6)
        else:
            if hold:
                self._concurrency_hold_until = 0.0
            text, fg = f"并发 {limit}", T.MUTED
        if (text, fg) != self._concurrency_render:
            self.concurrency_label.configure(text=text, fg=fg)
            self._concurrency_render = (text, fg)

    def _on_done(self, payload: dict) -> None:
        cancelled = payload.get("cancelled", False)
        pages = payload.get("pages", 0)
        failed = payload.get("failed", 0)
        elapsed = payload.get("elapsed", 0)
        self._last_note = payload.get("last_note", "") or ""
        throttles = int(payload.get("downgrades") or 0)
        low_limit = payload.get("longest_concurrency") or payload.get("min_concurrency")
        throttle_note = f" · 限流降级 {throttles} 次，最长停在并发 {low_limit}" if throttles else ""

        if cancelled:
            self.current_label.configure(text=f"已停止 · 本次完成 {pages} 页{throttle_note}", fg=T.WARN)
        else:
            self.current_label.configure(text=f"完成 · 本次 {pages} 页{throttle_note}", fg=T.INK)
            self.progress.set_value(1.0)
            self.pct_label.configure(text="100%")
        self._last_stats = {}
        self._eta_rate = 0.0
        self._eta_last_at = 0.0

        self.meta_label.configure(
            text=f"用时 {self._fmt(int(elapsed))} · 失败 {failed} · 结果已写入笔记库"
            if failed
            else f"用时 {self._fmt(int(elapsed))} · 结果已写入笔记库"
        )
        if self._last_note and Path(self._last_note).exists():
            self.btn_recent.pack(side="right")

        if failed:
            self.banner.show(f"完成，但有 {failed} 项失败。可在下方点“全部重试”。", "warn")
        elif cancelled:
            self.banner.show("已停止，已完成的页都已保留。", "info", auto_hide_ms=6000)
        else:
            self.banner.show(f"完成：{pages} 页，用时 {self._fmt(int(elapsed))}。", "success", auto_hide_ms=6000)

        if payload.get("mode") == "sample" and payload.get("vault"):
            self.banner.show(
                f"离线示例完成，结果在：{payload['vault']}（这不是真实识别）", "info", auto_hide_ms=10000
            )
            self._open_path(Path(payload["vault"]))
        self._refresh_estimate_async()

    def _on_estimate(self, payload: dict) -> None:
        if payload.get("token") != self._estimate_token:
            return
        if self._state != "idle":
            return
        files = payload.get("files", 0)
        if files == 0:
            self.estimate_label.configure(text="收件箱还是空的 · 拖入文件即可开始")
            return
        if payload.get("offline"):
            self.estimate_label.configure(
                text=f"收件箱 {files} 个文件 · 预计 {payload.get('pages', 0)} 页 · 离线示例不调用模型"
            )
        else:
            calls = payload.get("calls", 0)
            low = max(int(calls * 6 / 60), 1)
            high = max(int(calls * 35 / 60), low + 1)
            self.estimate_label.configure(
                text=f"收件箱 {files} 个文件 · 预计 {payload.get('pages', 0)} 页 / {calls} 次调用 · 约 {low}–{high} 分钟"
            )

    # ================= 辅助 =================

    def _refresh_estimate_async(self) -> None:
        self._estimate_token += 1
        token = self._estimate_token
        config = self.config

        def work() -> None:
            try:
                watch = Path(config.watch_dir).expanduser()
                files = self._files_in(watch) if watch.is_dir() else []
                estimate = self.controller.estimate(files, offline=config.is_offline)
                payload = {
                    "token": token,
                    "files": estimate.files,
                    "pages": estimate.pages,
                    "calls": estimate.calls,
                    "offline": config.is_offline,
                }
            except Exception as exc:
                payload = {"token": token, "files": 0, "error": str(exc)}
            self._push_event("estimate", payload)

        threading.Thread(target=work, name="noteflow-estimate", daemon=True).start()

    def _reset_task_ui(self) -> None:
        self.progress.set_value(0)
        self.pct_label.configure(text="0%")
        self.meta_label.configure(text="文件 0/0 · 页 0 · 失败 0 · 用时 00:00")
        self.failure_panel.set_retry_enabled(False)
        self.btn_recent.pack_forget()
        self._last_stats = {}
        self._eta_rate = 0.0
        self._eta_last_at = 0.0
        self._concurrency_prev = None
        self._concurrency_hold_until = 0.0
        self.concurrency_label.configure(text="")
        self._concurrency_render = ("", "")

    def _fmt(self, seconds: int) -> str:
        seconds = max(int(seconds), 0)
        if seconds < 60:
            return f"{seconds} 秒"
        minutes, remain = divmod(seconds, 60)
        return f"{minutes} 分 {remain:02d} 秒"

    def _install_logging(self) -> None:
        handler = LogBridge(self._push_event)
        handler.setLevel(logging.INFO)
        root_logger = logging.getLogger()
        for existing in list(root_logger.handlers):
            if not isinstance(existing, LogBridge):
                root_logger.removeHandler(existing)
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
        logging.getLogger("PIL").setLevel(logging.WARNING)
        logging.getLogger("watchdog").setLevel(logging.WARNING)

    def _on_close(self) -> None:
        if self._state in ("running", "watching", "stopping"):
            if not messagebox.askyesno("NoteFlow", "任务还在进行，停止并退出吗？", parent=self.root):
                return
        self.controller.shutdown()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
