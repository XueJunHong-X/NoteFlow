"""watchdog 文件夹监听：发现新图片后交给处理函数，串行执行避免并发调用模型。

特性：
- 只关心配置里的图片扩展名
- 文件写入完成（大小稳定）后才处理，避免读到半张图
- 已处理记录持久化到 output/.noteflow_state.json，重启后不重复处理
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from config import Settings

logger = logging.getLogger(__name__)

STABLE_INTERVALS = (0.1, 0.2, 0.4, 0.8)  # 秒，检查文件大小是否稳定的轮询间隔（自适应退避）
STABLE_MIN_WINDOW = 0.3  # 秒，至少观察这么久才认定写完，避免抄写中途的短暂停顿误判
STABLE_TIMEOUT = 30.0  # 秒，等待新文件写完的最长时间
STATE_FLUSH_INTERVAL = 2.0  # 秒，处理记录合并写盘的最小间隔


class ProcessedRegistry:
    """记录已处理文件（绝对路径 + 修改时间），进程重启后不重复处理。"""

    def __init__(self, state_file: Path) -> None:
        self._state_file = Path(state_file)
        self._items: dict[str, int] = self._load()
        self._last_flush = 0.0
        self._dirty = False

    def _load(self) -> dict[str, int]:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return {str(key): int(value) for key, value in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _key(path: Path) -> str:
        return str(path.resolve())

    def is_processed(self, path: Path) -> bool:
        try:
            return self._items.get(self._key(path)) == path.stat().st_mtime_ns
        except OSError:
            return False

    def mark(self, path: Path) -> None:
        try:
            self._items[self._key(path)] = path.stat().st_mtime_ns
        except OSError:
            return
        self._dirty = True
        # 文件洪峰时每处理一个就整表重写会放大成 O(n²) 写盘，合并成每 2 秒一次
        if time.time() - self._last_flush >= STATE_FLUSH_INTERVAL:
            self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
            self._dirty = False
            self._last_flush = time.time()
        except OSError as exc:
            logger.warning("写入处理记录失败: %s", exc)


class _ImageEventHandler(FileSystemEventHandler):
    """把新建/移入的图片转交给监听器。"""

    def __init__(self, submit: Callable[[Path], None], extensions: tuple[str, ...]) -> None:
        self._submit = submit
        self._extensions = extensions

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event.src_path, event.is_directory)

    def on_modified(self, event: FileSystemEvent) -> None:
        # 覆盖「已存在文件被改写」的情况；未改动的文件会被 mtime 记录挡掉，不会重复识别
        self._handle(event.src_path, event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._handle(getattr(event, "dest_path", event.src_path), event.is_directory)

    def _handle(self, raw_path: str, is_directory: bool) -> None:
        if is_directory:
            return
        path = Path(raw_path)
        if path.suffix.lower() not in self._extensions:
            return
        self._submit(path)


def _wait_until_stable(path: Path) -> bool:
    """等待文件写入完成：连续两次大小一致且非空即认为稳定。

    采用 0.1→0.8s 的退避轮询：已经写完的文件约 0.3s 即可开始处理，
    比固定 0.8s 快 2-3 倍；仍在增长的大文件会自动退到 0.8s，避免频繁 stat。
    """
    deadline = time.time() + STABLE_TIMEOUT
    started = time.time()
    last_size = -1
    attempt = 0
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return False  # 文件被删除或移动
        if size > 0 and size == last_size and time.time() - started >= STABLE_MIN_WINDOW:
            return True
        last_size = size
        time.sleep(STABLE_INTERVALS[min(attempt, len(STABLE_INTERVALS) - 1)])
        attempt += 1

    logger.warning("等待文件稳定超时，跳过: %s", path.name)
    return False


class FolderWatcher:
    """监听目录并串行处理新图片。"""

    def __init__(self, settings: Settings, processor: Callable[[Path], None]) -> None:
        self._settings = settings
        self._processor = processor
        self._queue: queue.Queue[Path] = queue.Queue()
        self._pending: set[Path] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._registry = ProcessedRegistry(settings.state_file)
        self._handler = _ImageEventHandler(self.submit, settings.supported_extensions)
        self._observer = Observer()
        self._worker = threading.Thread(target=self._work, name="noteflow-worker", daemon=True)

    def submit(self, path: Path) -> None:
        """入口：去重后放入处理队列。"""
        try:
            path = path.resolve()
        except OSError:
            return

        with self._lock:
            if path in self._pending:
                return
            if self._registry.is_processed(path):
                logger.debug("已处理过，跳过: %s", path.name)
                return
            self._pending.add(path)
        self._queue.put(path)

    def process_existing_files(self) -> int:
        """把监听目录里已有的受支持文件加入队列，返回加入数量。"""
        count = 0
        for path in sorted(self._settings.watch_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in self._settings.supported_extensions:
                self.submit(path)
                count += 1
        return count

    def _work(self) -> None:
        while not self._stop_event.is_set():
            try:
                path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                if _wait_until_stable(path):
                    self._processor(path)
                    self._registry.mark(path)
            except Exception as exc:  # 单张图片失败不影响后续处理
                logger.error("处理失败 %s: %s", path.name, exc)
            finally:
                with self._lock:
                    self._pending.discard(path)
                self._queue.task_done()

    def start(self) -> None:
        self._settings.ensure_dirs()
        self._worker.start()
        self._observer.schedule(self._handler, str(self._settings.watch_dir), recursive=False)
        self._observer.start()
        logger.info("开始监听 %s", self._settings.watch_dir)

    def run_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到中断，正在停止监听")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._worker.is_alive():
            self._worker.join(timeout=5)
        self._registry.flush()
