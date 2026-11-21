"""本地缓存清理：只处理受管收件箱、通过五项校验后释放或回收站删除。

安全原则（与 docs/清理本地缓存-设计方案.md 对齐）：
- 默认关闭，由 UI 显式开启；
- 只碰 watch_dir 直接子文件，白名单外一律拒绝；
- 云端副本 / vault 笔记 / ima 导入 / 处理完成 / 受管路径 五项全部通过才执行；
- OneDrive 同步目录内只允许释放，禁止彻底删除；
- 删除只走回收站，失败绝不退化为永久删除；
- 每个动作先写 intent 日志，再执行，便于中断恢复。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # 仅用于清理窗口互斥锁；非 Windows 平台（如纯逻辑测试）退化为不加锁
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

from config import DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, Settings
from src import manifest, onedrive, organizer

logger = logging.getLogger(__name__)

# Windows 文件属性
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

CLEANUP_EXTENSIONS = tuple(IMAGE_EXTENSIONS) + tuple(DOCUMENT_EXTENSIONS)

# 硬拒绝：演示环境、参赛材料。用户真实 vault 通过 settings.vault_dir 动态加入。
STATIC_DENY_ROOTS = (
    Path(r"D:\NoteFlow-Demo"),
    Path(r"E:\NoteFlow-参赛材料"),
)

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_KEEP = 5


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _under(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))]) == os.path.normcase(
            str(root)
        )
    except ValueError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def format_bytes(size: int) -> str:
    size = max(0, int(size))
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _file_attributes(path: Path) -> int:
    if os.name != "nt":
        return 0
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetFileAttributesW.restype = ctypes.c_uint32
        return int(kernel32.GetFileAttributesW(str(path)))
    except Exception:
        return INVALID_FILE_ATTRIBUTES


def _measure_local_alloc(path: Path) -> tuple[int, bool]:
    """返回 (本地实际占用字节数, 读数是否可信)。

    判定顺序：
    1. 属性带 OFFLINE / RECALL_ON_DATA_ACCESS 占位标志 → OneDrive 已脱水，本地占用记 0（可信）；
    2. GetCompressedFileSizeW 读取成功 → 实际占用（可信）；
       已知局限：水合的 OneDrive 文件该值等于逻辑大小，只能与脱水后的读数同源对比；
    3. 全部失败 → 退回逻辑大小（不可信），调用方不得据此统计释放字节。
    """
    try:
        fallback = Path(path).stat().st_size
    except OSError:
        fallback = 0
    if os.name != "nt":
        return fallback, False
    attrs = _file_attributes(path)
    if attrs != INVALID_FILE_ATTRIBUTES and attrs & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
        return 0, True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCompressedFileSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetCompressedFileSizeW.restype = ctypes.c_uint32
        high = ctypes.c_uint32(0)
        low = kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
        if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
            return fallback, False
        return (int(high.value) << 32) + int(low), True
    except Exception:
        return fallback, False


def _local_alloc_bytes(path: Path) -> int:
    """本地实际占用估值（扫描列表用）；释放统计请用 _measure_local_alloc，以区分读数是否可信。"""
    return _measure_local_alloc(path)[0]


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _has_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if Path(path).is_symlink():
        return True
    return bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass
class Check:
    status: str  # pass / fail / unknown
    reason: str = ""


@dataclass
class Candidate:
    path: Path
    source_id: str
    size_bytes: int
    local_bytes: int
    attrs: int
    zone: str  # onedrive / external
    manifest: dict[str, Any] | None
    notes: list[Path]
    checks: dict[str, Check] = field(default_factory=dict)

    def status(self, key: str) -> str:
        return self.checks.get(key, Check("fail", "未检查")).status

    @property
    def reasons(self) -> list[str]:
        return [f"{key}: {check.reason}" for key, check in self.checks.items() if check.status != "pass" and check.reason]

    def eligible(self, mode: str) -> bool:
        if self.status("managed") != "pass":
            return False
        if self.status("processed") != "pass" or self.status("vault") != "pass" or self.status("ima") != "pass":
            return False
        if mode == "release":
            return self.zone == "onedrive" and self.status("cloud") == "pass"
        if mode == "delete":
            # OneDrive 同步目录内删除会同步删除云端原件，必须走释放
            return self.zone == "external" and self.status("cloud") == "pass"
        return False


@dataclass
class ActionResult:
    path: Path
    action: str
    status: str  # success / pending / skipped / failed
    reason: str = ""
    size_bytes: int = 0
    freed_bytes: int = 0


class CleanupService:
    """扫描与执行入口；UI 只通过这个类操作文件。"""

    def __init__(self, settings: Settings, config) -> None:
        self.settings = settings
        self.config = config
        self._cleanup_dir: Path | None = None
        self._lock_fd: int | None = None
        self._batch_id = ""

    # ---------------- 路径与日志 ----------------

    @property
    def cleanup_dir(self) -> Path:
        if self._cleanup_dir is not None:
            return self._cleanup_dir
        preferred = Path(self.settings.output_dir) / "cleanup"
        try:
            preferred.mkdir(parents=True, exist_ok=True)
            probe = preferred / ".write_test"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
            self._cleanup_dir = preferred
        except OSError:
            from src.appconfig import app_data_dir

            fallback = app_data_dir() / "cleanup"
            fallback.mkdir(parents=True, exist_ok=True)
            self._cleanup_dir = fallback
        return self._cleanup_dir

    @property
    def operations_path(self) -> Path:
        return self.cleanup_dir / "operations.jsonl"

    @property
    def ima_path(self) -> Path:
        return self.cleanup_dir / "ima_imports.jsonl"

    @property
    def cloud_path(self) -> Path:
        return self.cleanup_dir / "cloud_attestations.jsonl"

    @property
    def state_path(self) -> Path:
        return self.cleanup_dir / "state.json"

    def open_log_dir(self) -> None:
        os.startfile(str(self.cleanup_dir))  # type: ignore[attr-defined]

    def open_recycle_bin(self) -> None:
        os.startfile("shell:RecycleBinFolder")  # type: ignore[attr-defined]

    def _rotate(self, path: Path) -> None:
        try:
            if not path.exists() or path.stat().st_size <= LOG_MAX_BYTES:
                return
            for index in range(LOG_KEEP - 1, 0, -1):
                source = path.with_name(f"{path.name}.{index}")
                target = path.with_name(f"{path.name}.{index + 1}")
                if source.exists():
                    source.replace(target)
            path.replace(path.with_name(f"{path.name}.1"))
        except OSError:
            pass

    def _append(self, path: Path, entry: dict[str, Any]) -> bool:
        try:
            self._rotate(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except OSError as exc:
            logger.error("清理日志写入失败：%s", exc)
            return False

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    rows.append(data)
        except OSError:
            pass
        return rows

    def load_ima_records(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for row in self._read_jsonl(self.ima_path):
            source_id = str(row.get("source_id") or "")
            if source_id:
                records[source_id] = row
        return records

    def load_cloud_attestations(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for row in self._read_jsonl(self.cloud_path):
            source_id = str(row.get("source_id") or "")
            if source_id:
                records[source_id] = row
        return records

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, data: dict[str, Any]) -> None:
        try:
            temp = self.state_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.state_path)
        except OSError as exc:
            logger.warning("清理状态写入失败：%s", exc)

    def first_run_done(self) -> bool:
        return bool(self._read_state().get("first_run_done"))

    def mark_first_run(self) -> None:
        state = self._read_state()
        state["first_run_done"] = True
        state["schema"] = 1
        self._write_state(state)

    def save_last_summary(self, summary: dict[str, Any]) -> None:
        state = self._read_state()
        state["schema"] = 1
        state["last_batch_id"] = self._batch_id
        state["last_summary"] = summary
        self._write_state(state)

    def interrupted_actions(self) -> list[dict[str, Any]]:
        """只有 intent、没有 result 的动作；不自动重试，只提示用户。"""
        rows = self._read_jsonl(self.operations_path)
        results = {
            (row.get("batch_id"), row.get("source_id"), row.get("action"))
            for row in rows
            if row.get("event") == "result"
        }
        interrupted: list[dict[str, Any]] = []
        for row in rows:
            if row.get("event") != "intent":
                continue
            key = (row.get("batch_id"), row.get("source_id"), row.get("action"))
            if key not in results:
                interrupted.append(row)
        return interrupted

    def new_batch_id(self) -> str:
        self._batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
        return self._batch_id

    # ---------------- 互斥锁 ----------------

    def acquire_lock(self) -> None:
        """用 OS 文件锁互斥：进程被强杀时锁由系统释放，不会留下永久死锁。"""
        path = self.cleanup_dir / "cleanup.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR)
        try:
            if msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError("另一个清理窗口正在运行，请先关闭它。") from exc
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.truncate(fd, 0)
            os.write(fd, f"{os.getpid()} {_now()}".encode("utf-8"))
        except OSError:
            pass
        self._lock_fd = fd

    def release_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            os.lseek(self._lock_fd, 0, os.SEEK_SET)
            if msvcrt is not None:
                msvcrt.locking(self._lock_fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            os.close(self._lock_fd)
        except OSError:
            pass
        self._lock_fd = None

    # ---------------- 扫描与校验 ----------------

    def _deny_roots(self) -> list[Path]:
        roots = [Path(self.settings.vault_dir), Path(self.settings.output_dir), self.cleanup_dir]
        roots.extend(STATIC_DENY_ROOTS)
        return roots

    def _check_managed(self, path: Path) -> Check:
        try:
            resolved = path.resolve(strict=True)
            watch = Path(self.settings.watch_dir).resolve(strict=True)
        except OSError as exc:
            return Check("fail", f"路径不可解析：{exc}")
        if not _same_path(resolved.parent, watch):
            return Check("fail", "不在收件箱直接子文件内")
        if _has_reparse_point(path):
            return Check("fail", "路径含符号链接 / junction / 重解析点")
        for root in self._deny_roots():
            try:
                denied = root.resolve(strict=False)
            except OSError:
                denied = root
            if _under(resolved, denied):
                return Check("fail", f"位于拒绝目录：{denied}")
        if path.name.startswith("."):
            return Check("fail", "隐藏文件不处理")
        return Check("pass")

    def _has_later_failure(self, path: Path, data: dict[str, Any]) -> bool:
        """清单完成之后又出现过失败（例如重试失败），不能当作已完成。"""
        log_path = Path(self.settings.failure_log)
        if not log_path.exists():
            return False
        finished = _parse_time(data.get("finished_at"))
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return True
        try:
            target = os.path.normcase(str(path.resolve()))
        except OSError:
            target = os.path.normcase(str(path))
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            try:
                recorded = os.path.normcase(str(Path(str(row.get("source") or "")).resolve()))
            except OSError:
                recorded = os.path.normcase(str(row.get("source") or ""))
            if recorded != target:
                continue
            failed_at = _parse_time(row.get("time"))
            if finished is None or failed_at is None or failed_at >= finished:
                return True
        return False

    def _check_processed(self, path: Path, source_id: str, data: dict[str, Any] | None) -> tuple[Check, list[Path]]:
        if not source_id:
            return Check("fail", "无法计算来源指纹"), []
        if not data:
            return Check("fail", "缺少 NoteFlow 处理清单（旧笔记或未处理完成）"), []
        if data.get("status") != "complete":
            return Check("fail", f"处理未完成：status={data.get('status')}"), []
        if self._has_later_failure(path, data):
            return Check("fail", "处理完成后又出现过失败记录"), []
        if data.get("recognition_mode") != "real":
            return Check("fail", f"非真实识别：{data.get('recognition_mode')}"), []
        try:
            total = int(data.get("pages_total") or 0)
            ok = int(data.get("pages_ok") or 0)
            failed = int(data.get("pages_failed") or 0)
        except (TypeError, ValueError):
            return Check("fail", "处理清单页数无效"), []
        if total <= 0 or ok != total or failed:
            return Check("fail", f"页数不完整：成功 {ok}/{total}，失败 {failed}"), []
        notes = [Path(str(item)) for item in (data.get("notes") or []) if str(item).strip()]
        if not notes or len(notes) < ok:
            return Check("fail", "处理清单没有完整笔记列表"), []
        try:
            stat = path.stat()
            if int(data.get("source_size") or -1) != stat.st_size:
                return Check("fail", "来源文件大小与处理时不一致"), notes
        except OSError:
            return Check("fail", "来源文件不可读"), notes
        return Check("pass"), notes

    def _check_cloud(self, path: Path, source_id: str, attrs: int, attestations: dict[str, dict[str, Any]]) -> tuple[Check, str]:
        if attrs == INVALID_FILE_ATTRIBUTES:
            return Check("unknown", "无法读取文件属性"), "external"
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            resolved = path
        roots: list[Path] = []
        try:
            roots = [root.resolve(strict=False) for root in onedrive.detect_onedrive_paths()]
        except Exception:
            roots = []
        for root in roots:
            if _under(resolved, root):
                if attrs & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_OFFLINE):
                    return Check("pass", "OneDrive 云端占位可用"), "onedrive"
                if attrs & (FILE_ATTRIBUTE_PINNED | FILE_ATTRIBUTE_UNPINNED):
                    return Check("pass", "OneDrive 已接管该文件"), "onedrive"
                return Check("unknown", "在 OneDrive 目录内但未显示云端占位属性，可能尚未同步"), "onedrive"
        record = attestations.get(source_id)
        if record and record.get("status") == "success":
            return Check("pass", f"已登记云端副本：{record.get('provider') or '未知云盘'}"), "external"
        return Check("fail", "非 OneDrive 文件，且未登记云端副本"), "external"

    def _check_vault(self, manifest_data: dict[str, Any] | None, source_id: str, source_file: str, source_size: int) -> Check:
        if not manifest_data:
            return Check("fail", "没有处理清单，无法确认 vault 笔记")
        notes = [Path(str(item)) for item in (manifest_data.get("notes") or []) if str(item).strip()]
        if not notes:
            return Check("fail", "处理清单没有笔记路径")
        try:
            vault = Path(self.settings.vault_dir).resolve(strict=True)
        except OSError:
            return Check("fail", "vault 目录不可用")
        for note in notes:
            try:
                resolved = note.resolve(strict=True)
            except OSError:
                return Check("fail", f"笔记不存在：{note.name}")
            if not _under(resolved, vault):
                return Check("fail", f"笔记不在 vault 内：{note.name}")
            if resolved.suffix.lower() != ".md" or resolved.stat().st_size <= 0:
                return Check("fail", f"笔记为空或不是 md：{note.name}")
            meta = organizer.read_frontmatter(resolved)
            note_source_id = str(meta.get("source_id") or "")
            if note_source_id:
                if note_source_id != source_id:
                    return Check("fail", f"笔记来源指纹不一致：{note.name}")
                continue
            # 旧版笔记没有 source_id，只接受文件名 + 大小都能对上的情况
            if str(meta.get("source_file") or "") != source_file:
                return Check("fail", f"笔记缺少来源指纹：{note.name}")
            try:
                if int(meta.get("source_size") or -1) != source_size:
                    return Check("fail", f"笔记来源大小不一致：{note.name}")
            except (TypeError, ValueError):
                return Check("fail", f"笔记来源大小无效：{note.name}")
        return Check("pass")

    def _check_ima(self, manifest_data: dict[str, Any] | None, source_id: str, records: dict[str, dict[str, Any]]) -> Check:
        record = records.get(source_id)
        if not record:
            return Check("fail", "未登记 ima 导入")
        if record.get("status") != "success":
            return Check("fail", f"ima 导入记录不是成功：{record.get('status')}")
        notes = [str(item) for item in (record.get("notes") or []) if str(item).strip()]
        expected = {str(item) for item in ((manifest_data or {}).get("notes") or [])}
        if not notes or not (set(notes) & expected):
            return Check("fail", "ima 登记没有对应的笔记")
        return Check("pass")

    def _build_candidate(
        self,
        path: Path,
        ima_records: dict[str, dict[str, Any]],
        attestations: dict[str, dict[str, Any]],
    ) -> Candidate:
        try:
            stat = path.stat()
            source_id = manifest.source_id_for(path)
        except OSError:
            return Candidate(path, "", 0, 0, INVALID_FILE_ATTRIBUTES, "external", None, [], {"managed": Check("fail", "文件不可读")})
        attrs = _file_attributes(path)
        local_bytes = _local_alloc_bytes(path)
        manifest_data = manifest.read_manifest(self.settings.output_dir, source_id)
        checks: dict[str, Check] = {}
        checks["managed"] = self._check_managed(path)
        processed, notes = self._check_processed(path, source_id, manifest_data)
        checks["processed"] = processed
        cloud, zone = self._check_cloud(path, source_id, attrs, attestations)
        checks["cloud"] = cloud
        checks["vault"] = self._check_vault(manifest_data, source_id, path.name, stat.st_size)
        checks["ima"] = self._check_ima(manifest_data, source_id, ima_records)
        return Candidate(path, source_id, stat.st_size, local_bytes, attrs, zone, manifest_data, notes, checks)

    def scan(self) -> list[Candidate]:
        watch = Path(self.settings.watch_dir)
        if not watch.is_dir():
            return []
        ima_records = self.load_ima_records()
        attestations = self.load_cloud_attestations()
        candidates: list[Candidate] = []
        for path in sorted(watch.iterdir(), key=lambda item: item.name.lower()):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if path.suffix.lower() not in CLEANUP_EXTENSIONS:
                continue
            candidates.append(self._build_candidate(path, ima_records, attestations))
        return candidates

    # ---------------- 登记 ----------------

    def register_ima(self, candidate: Candidate, status: str = "success", note: str = "") -> None:
        if status == "success" and not candidate.notes:
            raise ValueError("没有可登记的 vault 笔记")
        entry = {
            "time": _now(),
            "source_id": candidate.source_id,
            "source_file": candidate.path.name,
            "notes": [str(item) for item in candidate.notes],
            "method": "manual",
            "status": status,
            "note": note,
        }
        if not self._append(self.ima_path, entry):
            raise OSError("ima 登记写入失败")

    def register_cloud(self, candidate: Candidate, provider: str, reference: str, confirmed: bool) -> None:
        if candidate.zone == "onedrive":
            raise ValueError("OneDrive 同步目录内不允许彻底删除，无需登记云端副本")
        if not confirmed or not provider.strip():
            raise ValueError("请填写云盘名称并确认云端仍保留该文件")
        entry = {
            "time": _now(),
            "source_id": candidate.source_id,
            "source_file": candidate.path.name,
            "provider": provider.strip(),
            "reference": reference.strip(),
            "status": "success",
        }
        if not self._append(self.cloud_path, entry):
            raise OSError("云端副本登记写入失败")

    # ---------------- 执行 ----------------

    def _journal_intent(self, candidate: Candidate, action: str) -> bool:
        return self._append(
            self.operations_path,
            {
                "time": _now(),
                "event": "intent",
                "batch_id": self._batch_id,
                "action": action,
                "source_id": candidate.source_id,
                "path": str(candidate.path),
                "size_bytes": candidate.size_bytes,
                "local_alloc_bytes": candidate.local_bytes,
                "checks": {key: check.status for key, check in candidate.checks.items()},
            },
        )

    def _journal_result(self, candidate: Candidate, action: str, status: str, reason: str, freed: int = 0) -> None:
        self._append(
            self.operations_path,
            {
                "time": _now(),
                "event": "result",
                "batch_id": self._batch_id,
                "action": action,
                "result": status,
                "source_id": candidate.source_id,
                "path": str(candidate.path),
                "size_bytes": candidate.size_bytes,
                "freed_bytes": int(freed),
                "checks": {key: check.status for key, check in candidate.checks.items()},
                "reason": reason,
                "app_version": manifest.APP_VERSION,
            },
        )

    def execute(self, candidate: Candidate, mode: str) -> ActionResult:
        try:
            fresh = self._build_candidate(
                candidate.path,
                self.load_ima_records(),
                self.load_cloud_attestations(),
            )
        except Exception as exc:
            return ActionResult(candidate.path, mode, "failed", f"重新校验失败：{exc}")
        if fresh.source_id != candidate.source_id:
            return ActionResult(candidate.path, mode, "skipped", "文件在扫描后被修改，已跳过")
        if not fresh.eligible(mode):
            reasons = "；".join(fresh.reasons) or "未通过校验"
            return ActionResult(candidate.path, mode, "skipped", reasons, fresh.size_bytes)
        if not self._journal_intent(fresh, mode):
            return ActionResult(candidate.path, mode, "failed", "操作日志不可写，未执行", fresh.size_bytes)
        if mode == "release":
            result = self._release(fresh)
        elif mode == "delete":
            result = self._delete(fresh)
        else:
            result = ActionResult(fresh.path, mode, "failed", f"未知策略：{mode}", fresh.size_bytes)
        self._journal_result(fresh, mode, result.status, result.reason, result.freed_bytes)
        return result

    def _release(self, candidate: Candidate) -> ActionResult:
        before_alloc, before_known = _measure_local_alloc(candidate.path)
        before_attrs = candidate.attrs
        try:
            completed = subprocess.run(
                ["attrib", "+U", "-P", str(candidate.path)],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ActionResult(candidate.path, "release", "failed", f"调用 attrib 失败：{exc}", candidate.size_bytes)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            return ActionResult(candidate.path, "release", "failed", f"attrib 返回 {completed.returncode}：{detail}", candidate.size_bytes)

        deadline = time.time() + 10
        while time.time() < deadline:
            attrs = _file_attributes(candidate.path)
            alloc, known = _measure_local_alloc(candidate.path)
            released = bool(attrs & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_OFFLINE))
            if released or (before_alloc > 0 and alloc < before_alloc * 0.5):
                if before_known and known:
                    freed = max(0, before_alloc - alloc)
                    reason = f"本地占用 {format_bytes(before_alloc)} → {format_bytes(alloc)}"
                else:
                    # 分配大小读数不可用：只报告已脱水，不承诺具体释放字节
                    freed = 0
                    reason = "已脱水为仅云端文件（本地占用读数不可用，未统计释放字节）"
                return ActionResult(
                    candidate.path,
                    "release",
                    "success",
                    reason,
                    candidate.size_bytes,
                    freed,
                )
            time.sleep(0.5)
        return ActionResult(
            candidate.path,
            "release",
            "pending",
            f"已请求释放（属性 0x{before_attrs:x}），OneDrive 尚未完成，请稍后查看",
            candidate.size_bytes,
        )

    def _delete(self, candidate: Candidate) -> ActionResult:
        try:
            from send2trash import send2trash
        except ImportError:
            return ActionResult(candidate.path, "delete", "failed", "缺少 send2trash 依赖，未删除", candidate.size_bytes)
        try:
            send2trash(str(candidate.path))
        except Exception as exc:
            return ActionResult(candidate.path, "delete", "failed", f"移入回收站失败：{exc}", candidate.size_bytes)
        if candidate.path.exists():
            return ActionResult(candidate.path, "delete", "failed", "回收站操作后文件仍存在", candidate.size_bytes)
        return ActionResult(candidate.path, "delete", "success", "已移入回收站", candidate.size_bytes, candidate.local_bytes)


def candidate_display_status(candidate: Candidate, mode: str) -> tuple[str, str]:
    """给 UI 用的状态与原因；不参与执行判断。"""
    if candidate.eligible(mode):
        return "可清理", ""
    if candidate.status("managed") != "pass":
        return "跳过", candidate.checks["managed"].reason
    for key in ("processed", "vault", "ima"):
        if candidate.status(key) != "pass":
            return "跳过", candidate.checks[key].reason
    if candidate.status("cloud") != "pass":
        return "跳过", candidate.checks["cloud"].reason
    if mode == "delete" and candidate.zone == "onedrive":
        return "跳过", "OneDrive 同步目录内只允许释放，禁止彻底删除"
    return "跳过", "未通过校验"
