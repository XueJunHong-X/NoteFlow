"""NoteFlow 桌面版入口：只负责参数分发，界面在 src/ui，编排在 src/controller。

    NoteFlow.exe                 启动图形界面
    NoteFlow.exe --selftest      无界面自检：mock 跑通全链路后退出
    NoteFlow.exe --cli ...       转交给 main.py 的原参数
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from src.resources import FROZEN, icon_path, sample_dir


def run_gui() -> int:
    import tkinter as tk

    from src.appconfig import AppConfig
    from src.ui import theme
    from src.ui.app import NoteFlowApp

    theme.enable_dpi_awareness()
    config = AppConfig.load()
    root = tk.Tk()
    icon = icon_path()
    if icon.exists():
        try:
            root.iconbitmap(default=str(icon))
        except tk.TclError:  # 个别主题/平台不认 ico，窗口仍可正常显示
            pass
    theme.set_scale(root)
    theme.apply_ttk(root)
    NoteFlowApp(root, config)
    root.mainloop()
    return 0


def run_selftest() -> int:
    """无界面自检：用内置样例走一遍离线全链路，把结果写成 JSON 报告。"""
    from main import process_source
    from src.appconfig import AppConfig

    config = AppConfig.load()
    settings = config.to_settings(offline=True)
    settings.output_dir = Path(settings.output_dir) / "selftest"
    settings.vault_root = str(settings.output_dir / "vault")

    if settings.output_dir.exists():
        shutil.rmtree(settings.output_dir, ignore_errors=True)
    settings.ensure_dirs()

    started = time.time()
    samples = sample_dir()
    files = [p for p in sorted(samples.iterdir()) if p.is_file()] if samples.is_dir() else []
    notes: list[str] = []
    for path in files:
        if path.suffix.lower() not in settings.supported_extensions:
            continue
        try:
            notes.extend(str(p) for p in process_source(path, settings, mock=True))
        except Exception as exc:  # 自检要报告失败，不抛出
            print(f"[selftest] 失败 {path.name}: {exc}")

    report = {
        "mode": "offline-mock",
        "frozen": FROZEN,
        "samples": [p.name for p in files],
        "notes": notes,
        "elapsed_sec": round(time.time() - started, 2),
        "vault": str(settings.vault_dir),
        "watch_dir": str(settings.watch_dir),
        "ok": len(notes) >= len([p for p in files if p.suffix.lower() in settings.supported_extensions]),
    }
    report_path = Path(settings.output_dir) / "selftest.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


def _apply_config_defaults() -> None:
    """把 GUI 的 config.json 作为 CLI 的默认值，保证两边不各写一套路径。

    优先级仍是：命令行参数 > 环境变量 > config.json > 内置默认。
    """
    from src.appconfig import AppConfig

    config = AppConfig.load()
    for name, value in (
        ("NOTEFLOW_WATCH_DIR", config.watch_dir),
        ("NOTEFLOW_OUTPUT_DIR", config.output_dir),
        ("NOTEFLOW_VAULT_DIR", config.vault_dir),
        ("NOTEFLOW_API_BASE", config.api_base),
        ("NOTEFLOW_MODEL", config.model),
        ("NOTEFLOW_API_KEY", config.api_key),
    ):
        if value and not os.environ.get(name):
            os.environ[name] = value


def run_cli(argv: list[str]) -> int:
    """把参数原样交给 main.py，保持原有命令行能力。"""
    import main as cli

    _apply_config_defaults()
    sys.argv = [sys.argv[0]] + argv
    return cli.main()


def _attach_console() -> None:
    """exe 是 --noconsole 构建的；跑 --cli / --selftest 时把输出接回父控制台。"""
    if not FROZEN:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.AttachConsole(-1)
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except Exception:  # 没有父控制台时静默降级（结果仍会写进 JSON 报告）
        pass


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--cli", action="store_true")
    known, rest = parser.parse_known_args()

    if known.selftest or known.cli:
        _attach_console()
    if known.selftest:
        return run_selftest()
    if known.cli:
        return run_cli(rest)
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
