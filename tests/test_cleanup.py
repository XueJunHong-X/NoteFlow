"""清理本地缓存的核心逻辑测试：不碰真实 vault / OneDrive，只用临时目录。

运行：
    .venv\\Scripts\\python.exe -m unittest tests.test_cleanup -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from config import Settings
from src import cleanup, manifest
from src.appconfig import AppConfig


def _settings(root: Path) -> Settings:
    settings = Settings(
        watch_dir=root / "inbox",
        output_dir=root / "output",
        api_key="test",
        api_base="http://127.0.0.1",
        model="test",
    )
    settings.ensure_dirs()
    (root / "vault").mkdir(parents=True, exist_ok=True)
    return settings


def _config(root: Path, **kwargs) -> AppConfig:
    return AppConfig(
        watch_dir=str(root / "inbox"),
        output_dir=str(root / "output"),
        vault_dir=str(root / "vault"),
        cleanup_enabled=True,
        cleanup_delete_allowed=True,
        **kwargs,
    )


def _prepare_note(settings: Settings, source: Path) -> Path:
    source_id = manifest.source_id_for(source)
    note = settings.vault_dir / "工程制图" / "工程制图-p001.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\n"
        f'source: "{source.name}"\n'
        'source_type: "goodnotes"\n'
        f'source_id: "{source_id}"\n'
        f'source_file: "{source.name}"\n'
        f"source_size: {source.stat().st_size}\n"
        "---\n\n# 测试笔记\n",
        encoding="utf-8",
    )
    manifest.write_manifest(
        settings.output_dir,
        source,
        source_id=source_id,
        source_type="goodnotes",
        recognition_mode="real",
        pages_total=1,
        pages_ok=1,
        pages_failed=0,
        status="complete",
        started_at="2026-11-20T10:00:00",
        vault_dir=str(settings.vault_dir),
        notes=[str(note)],
    )
    return note


def _register_ima(service: cleanup.CleanupService, note: Path, source: Path) -> None:
    source_id = manifest.source_id_for(source)
    service._append(
        service.ima_path,
        {
            "time": "2026-11-20T11:00:00",
            "source_id": source_id,
            "source_file": source.name,
            "notes": [str(note)],
            "method": "manual",
            "status": "success",
        },
    )


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _settings(self.root)
        self.config = _config(self.root)
        self.service = cleanup.CleanupService(self.settings, self.config)
        self.source = self.settings.watch_dir / "工程制图.goodnotes"
        self.source.write_bytes(b"fake-goodnotes-data")
        self.note = _prepare_note(self.settings, self.source)

    def tearDown(self) -> None:
        self.service.release_lock()
        self._tmp.cleanup()

    def test_default_config_is_off(self) -> None:
        config = AppConfig()
        self.assertFalse(config.cleanup_enabled)
        self.assertFalse(config.cleanup_delete_allowed)
        self.assertEqual(config.cleanup_mode, "release")

    def test_manifest_roundtrip_and_source_id(self) -> None:
        source_id = manifest.source_id_for(self.source)
        data = manifest.read_manifest(self.settings.output_dir, source_id)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["notes"], [str(self.note)])
        self.assertEqual(len(source_id), 16)

    def test_external_file_needs_cloud_attestation_before_delete(self) -> None:
        _register_ima(self.service, self.note, self.source)
        candidates = self.service.scan()
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.zone, "external")
        self.assertEqual(candidate.status("vault"), "pass")
        self.assertEqual(candidate.status("ima"), "pass")
        self.assertEqual(candidate.status("cloud"), "fail")
        self.assertFalse(candidate.eligible("delete"))

        self.service.register_cloud(candidate, "百度网盘", "demo-id", True)
        refreshed = self.service.scan()[0]
        self.assertEqual(refreshed.status("cloud"), "pass")
        self.assertTrue(refreshed.eligible("delete"))
        self.assertFalse(refreshed.eligible("release"))

    def test_onedrive_file_can_release_but_not_delete(self) -> None:
        _register_ima(self.service, self.note, self.source)
        with mock.patch.object(cleanup.onedrive, "detect_onedrive_paths", return_value=[self.settings.watch_dir]):
            with mock.patch.object(cleanup, "_file_attributes", return_value=cleanup.FILE_ATTRIBUTE_PINNED):
                candidate = self.service.scan()[0]
        self.assertEqual(candidate.zone, "onedrive")
        self.assertEqual(candidate.status("cloud"), "pass")
        self.assertTrue(candidate.eligible("release"))
        self.assertFalse(candidate.eligible("delete"))
        status, reason = cleanup.candidate_display_status(candidate, "delete")
        self.assertEqual(status, "跳过")
        self.assertIn("只允许释放", reason)

    def test_release_uses_unpin_and_verifies_attributes(self) -> None:
        _register_ima(self.service, self.note, self.source)
        with mock.patch.object(cleanup.onedrive, "detect_onedrive_paths", return_value=[self.settings.watch_dir]):
            with mock.patch.object(cleanup, "_file_attributes", return_value=cleanup.FILE_ATTRIBUTE_PINNED):
                with mock.patch.object(cleanup, "_local_alloc_bytes", return_value=1000):
                    candidate = self.service.scan()[0]

        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(cleanup.onedrive, "detect_onedrive_paths", return_value=[self.settings.watch_dir]):
            with mock.patch.object(
                cleanup,
                "_file_attributes",
                side_effect=[cleanup.FILE_ATTRIBUTE_PINNED, cleanup.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS],
            ):
                with mock.patch.object(
                    cleanup,
                    "_measure_local_alloc",
                    side_effect=[(1000, True), (1000, True), (0, True)],
                ):
                    with mock.patch.object(cleanup.subprocess, "run", return_value=completed) as run:
                        result = self.service.execute(candidate, "release")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.freed_bytes, 1000)
        self.assertEqual(result.reason, "本地占用 1000 B → 0 B")
        args = run.call_args.args[0]
        self.assertEqual(args[0], "attrib")
        self.assertIn("+U", args)
        self.assertIn("-P", args)
        self.assertNotIn("+P", args)

    def test_release_reports_dehydrated_without_bytes_when_alloc_unknown(self) -> None:
        _register_ima(self.service, self.note, self.source)
        with mock.patch.object(cleanup.onedrive, "detect_onedrive_paths", return_value=[self.settings.watch_dir]):
            with mock.patch.object(cleanup, "_file_attributes", return_value=cleanup.FILE_ATTRIBUTE_PINNED):
                candidate = self.service.scan()[0]

        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(cleanup.onedrive, "detect_onedrive_paths", return_value=[self.settings.watch_dir]):
            with mock.patch.object(
                cleanup,
                "_file_attributes",
                side_effect=[cleanup.FILE_ATTRIBUTE_PINNED, cleanup.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS],
            ):
                with mock.patch.object(
                    cleanup,
                    "_measure_local_alloc",
                    side_effect=[(1000, True), (1000, False), (0, False)],
                ):
                    with mock.patch.object(cleanup.subprocess, "run", return_value=completed):
                        result = self.service.execute(candidate, "release")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.freed_bytes, 0)
        self.assertIn("已脱水", result.reason)
        self.assertIn("未统计释放字节", result.reason)
        self.assertNotIn("→", result.reason)

    @unittest.skipIf(os.name != "nt", "占位属性判定依赖 Windows 文件属性")
    def test_measure_local_alloc_placeholder_file_reports_zero(self) -> None:
        target = self.root / "占位文件.md"
        target.write_bytes(b"x" * 123)
        flags = cleanup.FILE_ATTRIBUTE_OFFLINE | cleanup.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        with mock.patch.object(cleanup, "_file_attributes", return_value=flags):
            size, known = cleanup._measure_local_alloc(target)
        self.assertEqual(size, 0)
        self.assertTrue(known)

    @unittest.skipIf(os.name != "nt", "实际占用读取依赖 Windows API")
    def test_measure_local_alloc_regular_file_reports_known_size(self) -> None:
        target = self.root / "普通文件.md"
        target.write_bytes(b"y" * 123)
        size, known = cleanup._measure_local_alloc(target)
        self.assertGreater(size, 0)
        self.assertTrue(known)

    def test_measure_local_alloc_read_failure_marks_unknown(self) -> None:
        target = self.root / "读数失败.md"
        target.write_bytes(b"z" * 123)
        fake_ctypes = types.SimpleNamespace(WinDLL=mock.Mock(side_effect=OSError("kernel32 不可用")))
        with mock.patch.object(cleanup, "_file_attributes", return_value=cleanup.FILE_ATTRIBUTE_PINNED):
            with mock.patch.object(cleanup, "ctypes", fake_ctypes):
                size, known = cleanup._measure_local_alloc(target)
        self.assertEqual(size, target.stat().st_size)
        self.assertFalse(known)

    def test_delete_goes_to_recycle_bin_only(self) -> None:
        _register_ima(self.service, self.note, self.source)
        candidate = self.service.scan()[0]
        self.service.register_cloud(candidate, "百度网盘", "", True)
        candidate = self.service.scan()[0]

        fake_module = types.ModuleType("send2trash")

        def fake_send(path: str) -> None:
            Path(path).unlink()

        fake_module.send2trash = fake_send
        with mock.patch.dict(sys.modules, {"send2trash": fake_module}):
            result = self.service.execute(candidate, "delete")
        self.assertEqual(result.status, "success")
        self.assertFalse(self.source.exists())

    def test_wal_marks_interrupted_intent(self) -> None:
        _register_ima(self.service, self.note, self.source)
        candidate = self.service.scan()[0]
        self.service.new_batch_id()
        self.assertTrue(self.service._journal_intent(candidate, "release"))
        interrupted = self.service.interrupted_actions()
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0]["action"], "release")

    def test_subdirectory_is_not_managed(self) -> None:
        nested = self.settings.watch_dir / "nested"
        nested.mkdir()
        nested_file = nested / "笔记.png"
        nested_file.write_bytes(b"data")
        candidates = self.service.scan()
        paths = {item.path for item in candidates}
        self.assertNotIn(nested_file, paths)


if __name__ == "__main__":
    unittest.main()
