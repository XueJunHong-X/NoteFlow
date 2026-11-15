"""任务编排：界面的请求都从这里进入流水线，工作线程与 Tk 主线程完全隔离。

界面只依赖本类的公开方法，事件以 (kind, payload) 形式通过 emit 回调送出：
    state     运行状态：idle / running / watching / stopping
    file      开始处理某个文件
    page      某一页开始/成功/失败
    stats     当前累计统计
    failure   一条失败记录（已翻译成中文原因）
    done      一轮任务结束
    log       日志（由 LogBridge 推送）
    notice    需要用户注意的提示
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

Emit = Callable[[str, dict[str, Any]], None]


@dataclass
class FailureItem:
    source: Path
    page: int | None
    reason: str
    detail: str


@dataclass
class Estimate:
    files: int
    pages: int
    calls: int


@dataclass
class RunStats:
    files_ok: int = 0
    files_failed: int = 0
    pages_ok: int = 0
    pages_failed: int = 0

    @property
    def failed(self) -> int:
        return self.files_failed + self.pages_failed

    def as_dict(self) -> dict[str, int]:
        return {
            "files_ok": self.files_ok,
            "files_failed": self.files_failed,
            "pages_ok": self.pages_ok,
            "pages_failed": self.pages_failed,
            "failed": self.failed,
        }


def friendly_error(exc: BaseException | None) -> str:
    """把底层异常翻译成用户能看懂的一句话。"""
    if exc is None:
        return "未知错误"
    text = str(exc).strip()
    lowered = text.lower()

    if isinstance(exc, PermissionError) or "permission" in lowered or "access is denied" in lowered:
        return "没有写入权限，请换一个可写的目录"
    if isinstance(exc, FileNotFoundError):
        return "文件不存在或已被移动"
    if isinstance(exc, MemoryError):
        return "内存不足，建议减少同时处理的文件"
    if "矢量笔迹" in text or "notes/*.pb" in text:
        return "这个 .goodnotes 只有矢量笔迹，请从 GoodNotes 导出 PDF 后再处理"
    if "api_key" in lowered or "未设置 noteflow_api_key" in lowered or "api key" in lowered:
        return "未配置 API Key，无法调用模型"
    if any(word in lowered for word in ("timeout", "timed out", "connection", "http", "ssl")):
        return "接口调用失败：网络异常、超时或额度不足"
    if "不支持的文件类型" in text:
        return "不支持的文件格式"
    if any(word in lowered for word in ("cannot open", "damaged", "broken")) or "损坏" in text:
        return "文件损坏或格式不受支持"
    if isinstance(exc, OSError):
        return f"磁盘或文件错误：{text[:80]}"
    first_line = (text.splitlines() or ["处理失败"])[0].strip()
    return (first_line or "处理失败")[:120]


class LogBridge(logging.Handler):
    """把日志推进界面队列；格式化放在这里，界面只负责显示。"""

    def __init__(self, emit: Emit) -> None:
        super().__init__()
        self._emit = emit

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("src.ui"):
                level = "DEBUG"
            else:
                level = record.levelname
            self._emit(
                "log",
                {"level": level, "text": f"{time.strftime('%H:%M:%S', time.localtime(record.created))}  {record.getMessage()}"},
            )
        except Exception:  # 日志失败不能影响主流程
            self.handleError(record)


def test_connection(api_base: str, api_key: str, model: str) -> tuple[bool, str]:
    """测试接口连通性，返回 (是否可用, 中文说明)。不产生识别费用。"""
    import requests

    base = (api_base or "").strip().rstrip("/")
    key = (api_key or "").strip()
    if not key:
        return False, "请先填写 API Key"
    if not base:
        return False, "请先填写接口地址"

    headers = {"Authorization": f"Bearer {key}"}
    try:
        response = requests.get(f"{base}/models", headers=headers, timeout=15)
        if response.status_code == 200:
            return True, f"连接正常（{base}）"
        model_note = f"接口返回 HTTP {response.status_code}，正在用一次最小对话请求复核…"
    except requests.RequestException as exc:
        model_note = f"接口地址不可达（{exc.__class__.__name__}），正在用最小对话请求复核…"

    try:
        response = requests.post(
            f"{base}/chat/completions",
            headers={**headers, "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            timeout=20,
        )
    except requests.RequestException as exc:
        return False, f"连接失败：{model_note} 复核也失败（{exc.__class__.__name__}）"

    if response.status_code < 400:
        return True, "连接正常（已用最小对话请求验证）"
    body = response.text[:160].replace("\n", " ")
    return False, f"连接失败：HTTP {response.status_code} · {body}"


class Controller:
    """单工作线程的任务编排器。"""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._config = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._watcher = None
        self._watch_settings = None
        self._watch_offline = False
        self._busy = False
        self._watching = False
        self._failures: list[FailureItem] = []
        self._last_vault: Path | None = None

    # ---------------- 对外状态 ----------------

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def watching(self) -> bool:
        return self._watching

    @property
    def failures(self) -> list[FailureItem]:
        return list(self._failures)

    def apply_config(self, config) -> None:
        self._config = config

    def clear_failures(self) -> None:
        self._failures.clear()

    def concurrency_status(self) -> dict[str, Any]:
        """识别并发的只读快照；界面定时轮询它，不直接接触流水线内部状态。"""
        from src import recognizer

        return recognizer.concurrency_status()

    # ---------------- 估算 ----------------

    def estimate(self, paths: list[Path], offline: bool | None = None) -> Estimate:
        from src import loader

        files = 0
        pages = 0
        for path in paths:
            path = Path(path)
            if not path.is_file():
                continue
            files += 1
            try:
                page_count, _ = loader.estimate_pages(path)
            except Exception:
                page_count = 1
            pages += max(page_count, 1)
        is_offline = self._config.is_offline if offline is None else offline
        return Estimate(files=files, pages=pages, calls=0 if is_offline else pages)

    # ---------------- 单次处理 ----------------

    def start_batch(self, paths: list[Path]) -> bool:
        paths = [Path(p) for p in paths if Path(p).is_file()]
        if not paths:
            self._emit("notice", {"level": "warn", "text": "没有可处理的文件。"})
            return False
        if not self._begin():
            return False

        settings = self._config.to_settings()
        if not self._prepare(settings):
            self._finish("就绪")
            return False

        self._failures.clear()
        self._emit("failures_clear", {})
        self._last_vault = settings.vault_dir
        offline = not settings.api_key
        self._emit("state", {"state": "running", "message": "正在处理"})
        # 新任务从配置档位起步，并清零上一轮留下的限流统计
        from src import recognizer

        recognizer.reset_concurrency(settings.recognize_concurrency)
        self._worker = threading.Thread(
            target=self._batch_worker,
            args=(paths, settings, offline),
            name="noteflow-batch",
            daemon=True,
        )
        self._worker.start()
        return True

    def _batch_worker(self, paths: list[Path], settings, offline: bool) -> None:
        from main import process_source

        started = time.time()
        stats = RunStats()
        cancelled = False
        last_note = ""

        for index, source in enumerate(paths, start=1):
            if self._cancel.is_set():
                cancelled = True
                break
            self._emit("file", {"name": source.name, "index": index, "total": len(paths)})

            def progress(src, page, total, stage, error=None, _index=index):
                self._on_progress(src, page, total, stage, error, stats, _index, len(paths))

            try:
                outputs = process_source(
                    source,
                    settings,
                    mock=offline,
                    progress=progress,
                    should_cancel=self._cancel.is_set,
                )
                # 整份文件所有页都失败时 outputs 为空；不能算作成功文件
                if outputs or self._cancel.is_set():
                    stats.files_ok += 1
                else:
                    stats.files_failed += 1
                if outputs:
                    last_note = str(outputs[-1])
            except Exception as exc:
                stats.files_failed += 1
                self._record_failure(source, None, exc)

        cancelled = cancelled or self._cancel.is_set()
        from src import recognizer

        snapshot = recognizer.concurrency_status()
        self._emit(
            "done",
            {
                "mode": "batch",
                "pages": stats.pages_ok,
                "files": stats.files_ok,
                "failed": stats.failed,
                "elapsed": round(time.time() - started, 1),
                "cancelled": cancelled,
                "vault": str(settings.vault_dir),
                "last_note": last_note,
                "offline": offline,
                "throttles": snapshot.get("throttles", 0),
                "downgrades": snapshot.get("downgrades", 0),
                "min_concurrency": snapshot.get("min_limit", 0),
                "longest_concurrency": snapshot.get("longest_limit", 0),
                "concurrency": snapshot.get("max", 0),
                **stats.as_dict(),
            },
        )
        self._finish("已停止" if cancelled else "完成")

    # ---------------- 内置样例演示 ----------------

    def start_sample(self) -> bool:
        from src.resources import sample_dir

        samples = sample_dir()
        if not samples.is_dir():
            self._emit("notice", {"level": "error", "text": f"找不到内置样例目录：{samples}"})
            return False
        if not self._begin():
            return False

        settings = self._config.to_settings(offline=True)
        demo_root = Path(self._config.output_dir) / "sample-demo"
        settings.output_dir = demo_root
        settings.vault_root = str(demo_root / "vault")
        if not self._prepare(settings, clean_dir=demo_root):
            self._finish("就绪")
            return False

        self._failures.clear()
        self._emit("failures_clear", {})
        self._last_vault = settings.vault_dir
        self._emit("state", {"state": "running", "message": "正在演示内置样例"})
        # 演示也按配置档位起步，保证界面的并发提示与设置一致
        from src import recognizer

        recognizer.reset_concurrency(settings.recognize_concurrency)
        self._worker = threading.Thread(
            target=self._sample_worker, args=(samples, settings), name="noteflow-sample", daemon=True
        )
        self._worker.start()
        return True

    def _sample_worker(self, samples: Path, settings) -> None:
        from main import process_source

        files = [
            p
            for p in sorted(samples.iterdir())
            if p.is_file() and p.suffix.lower() in settings.supported_extensions
        ]
        started = time.time()
        stats = RunStats()
        last_note = ""

        for index, source in enumerate(files, start=1):
            if self._cancel.is_set():
                break
            self._emit("file", {"name": source.name, "index": index, "total": len(files)})

            def progress(src, page, total, stage, error=None, _index=index):
                self._on_progress(src, page, total, stage, error, stats, _index, len(files))

            try:
                outputs = process_source(source, settings, mock=True, progress=progress)
                # 整份文件所有页都失败时 outputs 为空；不能算作成功文件
                if outputs or self._cancel.is_set():
                    stats.files_ok += 1
                else:
                    stats.files_failed += 1
                if outputs:
                    last_note = str(outputs[-1])
            except Exception as exc:
                stats.files_failed += 1
                self._record_failure(source, None, exc)

        self._emit(
            "done",
            {
                "mode": "sample",
                "pages": stats.pages_ok,
                "files": stats.files_ok,
                "failed": stats.failed,
                "elapsed": round(time.time() - started, 1),
                "cancelled": self._cancel.is_set(),
                "vault": str(settings.vault_dir),
                "last_note": last_note,
                "offline": True,
                **stats.as_dict(),
            },
        )
        self._finish("演示完成")

    # ---------------- 监听 ----------------

    def start_watch(self) -> bool:
        if self._watching:
            return True
        settings = self._config.to_settings()
        if not self._prepare(settings):
            return False
        if not self._begin():
            return False

        try:
            from src.watcher import FolderWatcher
        except Exception as exc:
            self._busy = False
            self._emit("notice", {"level": "error", "text": f"无法启动文件夹监听：{friendly_error(exc)}"})
            self._emit("state", {"state": "idle", "message": "就绪"})
            return False

        self._watch_settings = settings
        self._watch_offline = not settings.api_key
        self._watcher = FolderWatcher(settings, processor=self._watch_processor)
        try:
            self._watcher.start()
        except Exception as exc:
            self._watcher = None
            self._busy = False
            self._emit("notice", {"level": "error", "text": f"无法监听该文件夹：{friendly_error(exc)}"})
            self._emit("state", {"state": "idle", "message": "就绪"})
            return False

        self._watching = True
        self._emit(
            "state",
            {
                "state": "watching",
                "message": f"监听中：{settings.watch_dir}",
                "watch_dir": str(settings.watch_dir),
                "offline": self._watch_offline,
            },
        )
        return True

    def _watch_processor(self, path: Path) -> None:
        from main import process_source

        settings = self._watch_settings
        stats = RunStats()
        self._emit("file", {"name": Path(path).name, "index": 1, "total": 1, "watch": True})

        def progress(src, page, total, stage, error=None):
            self._on_progress(src, page, total, stage, error, stats, 1, 1)

        try:
            outputs = process_source(
                path,
                settings,
                mock=self._watch_offline,
                progress=progress,
                should_cancel=lambda: False,
            )
            stats.files_ok = 1 if outputs else 0
        except Exception as exc:
            stats.files_failed = 1
            self._record_failure(Path(path), None, exc)
            raise
        finally:
            self._emit("stats", {"watch": True, **stats.as_dict()})

    def stop_watch(self) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        self._watcher = None
        self._watching = False
        self._busy = True
        self._emit("state", {"state": "stopping", "message": "正在停止监听…"})

        def close() -> None:
            try:
                watcher.stop()
            except Exception as exc:
                logger.warning("停止监听时出错: %s", exc)
            self._finish("已停止监听")

        threading.Thread(target=close, name="noteflow-stop-watch", daemon=True).start()

    # ---------------- 失败重试 ----------------

    def retry_failures(self) -> bool:
        """重跑本次所有失败过的文件（页级失败会整份文件重跑）。"""
        with self._lock:
            if self._busy or self._watching:
                self._emit("notice", {"level": "warn", "text": "当前还有任务在运行，请先停止。"})
                return False
        sources: list[Path] = []
        for item in self._failures:
            if item.source not in sources:
                sources.append(item.source)
        if not sources:
            self._emit("notice", {"level": "info", "text": "本次还没有失败项。"})
            return False
        return self.start_batch(sources)

    # ---------------- 停止 / 退出 ----------------

    def cancel(self) -> None:
        if not self._busy and not self._watching:
            return
        self._cancel.set()
        self._emit("state", {"state": "stopping", "message": "正在停止，当前页处理完就停…"})

    def shutdown(self) -> None:
        self._cancel.set()
        watcher, self._watcher = self._watcher, None
        self._watching = False
        if watcher is not None:
            threading.Thread(target=watcher.stop, name="noteflow-shutdown", daemon=True).start()

    # ---------------- 内部 ----------------

    def _on_progress(self, source, page, total, stage, error, stats: RunStats, index, file_total) -> None:
        self._emit("page", {"page": page, "total": total, "stage": stage})
        if stage == "ok":
            stats.pages_ok += 1
        elif stage == "failed":
            stats.pages_failed += 1
            self._record_failure(Path(source), page, error)
        self._emit("stats", {"index": index, "total": file_total, **stats.as_dict()})

    def _record_failure(self, source: Path, page: int | None, exc: BaseException | None) -> None:
        item = FailureItem(
            source=Path(source),
            page=page,
            reason=friendly_error(exc),
            detail=str(exc or "").strip(),
        )
        self._failures.append(item)
        if len(self._failures) > 300:
            del self._failures[:-300]
        self._emit(
            "failure",
            {
                "source": str(item.source),
                "name": item.source.name,
                "page": item.page,
                "reason": item.reason,
                "detail": item.detail,
            },
        )

    def _prepare(self, settings, clean_dir: Path | None = None) -> bool:
        try:
            if clean_dir is not None and clean_dir.exists():
                shutil.rmtree(clean_dir, ignore_errors=True)
            settings.ensure_dirs()
            probe = settings.output_dir / ".noteflow_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            self._emit("notice", {"level": "error", "text": f"目录不可写：{friendly_error(exc)}"})
            return False
        return True

    def _begin(self) -> bool:
        with self._lock:
            if self._busy or self._watching:
                self._emit("notice", {"level": "warn", "text": "当前还有任务在运行，请先停止。"})
                return False
            self._busy = True
            self._cancel.clear()
            return True

    def _finish(self, message: str) -> None:
        with self._lock:
            self._busy = False
        self._emit("state", {"state": "idle", "message": message})
