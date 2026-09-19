"""Synthetic tests for the standalone bootstrap export; no media or credentials.

Copyright © 2026 Gateway Information Group LLC. All rights reserved.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "mediataggerbot" / "package_identity.py"
spec = importlib.util.spec_from_file_location("bootstrap_privacy_under_test", MODULE)
assert spec is not None and spec.loader is not None
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


class BootstrapExportPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "synthetic project"
        self.root.mkdir()
        self.status = {
            "gate_result": "BLOCK", "package_managed_count": 5,
            "package_verified_count": 4, "package_unmanaged_count": 1,
            "manifest_record_count": 6, "mismatch_count": 1,
            "authenticated_activity_permitted": False,
            "config_or_credentials_loaded_before_gate": False,
            "authentication_prevented_until_pass": True,
            "control_file_hashes": {name: "a" * 64 for name in identity.CONTROL_FILES},
            "mismatches": [{"type": "invalid_control_file", "path": "PACKAGE_METADATA.json"}],
        }

    def export(self, **kwargs) -> Path:
        return identity.write_identity_gate_support_export(
            self.root, kwargs.pop("run_id", "synthetic-run"),
            kwargs.pop("mode", "preflight"), kwargs.pop("status", self.status), **kwargs)

    def contents(self, path: Path) -> tuple[dict[str, bytes], dict]:
        with zipfile.ZipFile(path) as z:
            self.assertIsNone(z.testzip())
            files = {name: z.read(name) for name in z.namelist()}
        return files, json.loads(files["runtime_identity_status.json"])

    def test_rejected_controls_never_copied(self) -> None:
        marker = "SYNTHETIC_CONTROL_MARKER_NOT_A_SECRET"
        for name in identity.CONTROL_FILES:
            (self.root / name).write_text(marker, encoding="utf-8")
        before = {name: (self.root / name).read_bytes() for name in identity.CONTROL_FILES}
        path = self.export()
        files, summary = self.contents(path)
        self.assertNotIn(marker.encode(), b"".join(files.values()))
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
        self.assertFalse(summary["original_contents_included"])
        for name in identity.CONTROL_FILES:
            self.assertFalse(json.loads(files[name])["original_bytes_included"])

    def test_log_body_and_cached_errors_never_copied(self) -> None:
        marker = "SYNTHETIC_LOG_AND_ERROR_MARKER_NOT_A_SECRET"
        log = self.root / "bootstrap.log"
        log.write_text("token=" + marker, encoding="utf-8")
        self.status["mismatches"][0].update(error=marker, actual=marker, expected=marker)
        files, _ = self.contents(self.export(log_path=log))
        self.assertNotIn(marker.encode(), b"".join(files.values()))
        self.assertNotIn("current_log_tail.txt", files)

    def test_arbitrary_identity_labels_not_serialized(self) -> None:
        marker = "SYNTHETIC_PRIVATE_LABEL"
        self.status.update(runtime_build_id=marker, runtime_version=marker, custom=marker,
                           control_identities={"running_code": {"build_id": marker}})
        self.status["mismatches"].append({"type": marker, "path": marker, "error": marker})
        path = self.export(run_id=marker, mode=marker)
        files, _ = self.contents(path)
        self.assertNotIn(marker, path.name)
        self.assertNotIn(marker.encode(), b"".join(files.values()))

    def test_no_control_document_or_log_body_reads(self) -> None:
        for name in (*identity.CONTROL_FILES, "RUNBOOK.md", "RELEASE_NOTES.md", "bootstrap.log"):
            (self.root / name).write_text("SYNTHETIC_BODY", encoding="utf-8")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("No body reads")), \
             mock.patch.object(Path, "read_text", side_effect=AssertionError("No body reads")):
            path = self.export(log_path=self.root / "bootstrap.log")
        self.assertTrue(path.is_file())

    def test_control_symlink_is_not_followed(self) -> None:
        outside = self.root.parent / "private synthetic control"
        outside.write_text("SYNTHETIC_OUTSIDE_DATA", encoding="utf-8")
        try:
            (self.root / "MANIFEST.json").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("OS does not permit symlink creation")
        files, _ = self.contents(self.export())
        self.assertNotIn(b"SYNTHETIC_OUTSIDE_DATA", b"".join(files.values()))
        self.assertEqual(outside.read_text(), "SYNTHETIC_OUTSIDE_DATA")

    def test_infinite_and_nested_unknown_values_do_not_reach_output(self) -> None:
        self.status["custom"] = self.status
        self.status["package_verified_count"] = float("inf")
        self.status["gate_result"] = ["SYNTHETIC_VALUE"]
        files, summary = self.contents(self.export())
        self.assertEqual(summary["gate_result"], "UNKNOWN")
        self.assertIsNone(summary["package_verified_count"])
        self.assertNotIn(b"Infinity", files["runtime_identity_status.json"])

    def test_minimal_unknown_status_does_not_invent_gate_or_auth_result(self) -> None:
        _, summary = self.contents(self.export(status={}))
        self.assertEqual(summary["gate_result"], "UNKNOWN")
        self.assertIsNone(summary["authenticated_activity_permitted"])
        self.assertIsNone(summary["config_or_credentials_loaded_before_gate"])
        self.assertIsNone(summary["package_verified_count"])

    def test_reason_counts_and_valid_cached_hashes_remain_useful(self) -> None:
        _, summary = self.contents(self.export())
        self.assertEqual(summary["gate_result"], "BLOCK")
        self.assertEqual(summary["package_verified_count"], 4)
        self.assertEqual(summary["mismatches"][0], {"type": "invalid_control_file", "source": "PACKAGE_METADATA.json"})
        self.assertEqual(summary["control_file_hashes"]["MANIFEST.json"], "a" * 64)
        self.assertEqual(summary["evidence_scope"], "cached_identity_report_not_a_new_verification")

    def test_cached_status_write_failure_reason_remains_visible(self) -> None:
        self.status["mismatches"] = [{"type": "runtime_identity_evidence_write_failed", "path": "state/runtime_identity_status.json", "error": "SYNTHETIC_PRIVATE_ERROR"}]
        files, summary = self.contents(self.export())
        self.assertEqual(summary["mismatches"][0]["type"], "runtime_identity_evidence_write_failed")
        self.assertEqual(summary["mismatches"][0]["source"], "state/runtime_identity_status.json")
        self.assertNotIn(b"SYNTHETIC_PRIVATE_ERROR", b"".join(files.values()))

    def test_mismatch_and_byte_budgets(self) -> None:
        self.status["mismatches"] = [{"type": "invalid_control_file", "error": "X" * 10000}] * 10000
        path = self.export()
        files, summary = self.contents(path)
        self.assertLessEqual(len(files), 20)
        self.assertLessEqual(sum(map(len, files.values())), 65536)
        self.assertEqual(len(summary["mismatches"]), 64)
        self.assertEqual(summary["mismatches_omitted"], 9936)

    def test_caller_status_is_not_mutated(self) -> None:
        original = copy.deepcopy(self.status)
        self.export()
        self.assertEqual(self.status, original)

    def test_sidecar_and_regular_archive_entries(self) -> None:
        path = self.export()
        with zipfile.ZipFile(path) as z:
            for item in z.infolist():
                self.assertTrue(stat.S_ISREG(item.external_attr >> 16))
                self.assertNotIn("/", item.filename)
                self.assertFalse(item.is_dir())
        sidecar = path.with_suffix(path.suffix + ".sha256.txt")
        self.assertEqual(sidecar.read_text().strip(), hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name)
        self.assertEqual(path.parent, self.root / "diagnostics")
        self.assertEqual(list((self.root / "temp").iterdir()), [])

    def test_repeat_capture_does_not_overwrite_old_evidence(self) -> None:
        first = self.export()
        first_bytes = first.read_bytes()
        second = self.export()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), first_bytes)

    def test_linked_output_directories_are_rejected(self) -> None:
        for name in ("diagnostics", "temp"):
            with self.subTest(name=name):
                root = self.root / name.upper(); root.mkdir()
                outside = self.root.parent / ("external_" + name); outside.mkdir()
                try:
                    (root / name).symlink_to(outside, target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest("OS does not permit symlink creation")
                with self.assertRaises(ValueError):
                    identity.write_identity_gate_support_export(root, "test", "test", self.status)
                self.assertEqual(list(outside.iterdir()), [])

    def test_reparse_output_directory_is_rejected(self) -> None:
        (self.root / "diagnostics").mkdir()
        original = Path.lstat
        def mocked(path):
            if path == self.root / "diagnostics":
                return type("SyntheticStat", (), {"st_mode": stat.S_IFDIR, "st_file_attributes": 0x400})()
            return original(path)
        with mock.patch.object(Path, "lstat", new=mocked), self.assertRaises(ValueError):
            self.export()
        self.assertEqual(list((self.root / "diagnostics").iterdir()), [])

    def test_insufficient_space_fails_without_publishing(self) -> None:
        import shutil
        with mock.patch.object(shutil, "disk_usage", return_value=type("SyntheticDisk", (), {"free": 0})()), self.assertRaises(OSError):
            self.export()
        self.assertFalse((self.root / "diagnostics").exists())

    def test_serialization_failure_has_no_raw_fallback(self) -> None:
        with mock.patch.object(identity.json, "dumps", side_effect=ValueError("synthetic privacy failure")), self.assertRaises(ValueError):
            self.export()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_archive_write_failure_removes_only_owned_staging(self) -> None:
        (self.root / "temp").mkdir()
        sentinel = self.root / "temp" / "user-owned.txt"; sentinel.write_text("preserve")
        with mock.patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("synthetic disk failure")), self.assertRaises(OSError):
            self.export()
        self.assertEqual(list((self.root / "diagnostics").iterdir()), [])
        self.assertEqual(list((self.root / "temp").iterdir()), [sentinel])

    def test_publication_collision_never_overwrites_existing_evidence(self) -> None:
        real = os.link
        recorded = []
        def occupied(source, target):
            Path(target).write_bytes(b"SYNTHETIC_EXISTING_EVIDENCE")
            recorded.append(Path(target))
            return real(source, target)
        with mock.patch.object(identity.os, "link", side_effect=occupied), self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(recorded[0].read_bytes(), b"SYNTHETIC_EXISTING_EVIDENCE")
        self.assertEqual(list((self.root / "temp").iterdir()), [])

    def test_sidecar_failure_preserves_published_zip(self) -> None:
        real = os.link
        def fail_sidecar(source, target):
            if str(target).endswith(".sha256.txt"):
                raise PermissionError("synthetic companion failure")
            return real(source, target)
        with mock.patch.object(identity.os, "link", side_effect=fail_sidecar), self.assertRaisesRegex(RuntimeError, "archive preserved"):
            self.export()
        files = list((self.root / "diagnostics").glob("*.zip"))
        self.assertEqual(len(files), 1)
        self.contents(files[0])
        self.assertEqual(list((self.root / "temp").iterdir()), [])

    def test_invalid_status_fails_before_creating_outputs(self) -> None:
        with self.assertRaises(ValueError):
            self.export(status=["not a status"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unexpected_summary_field_fails_privacy_validation(self) -> None:
        contaminated = identity._identity_export_summary(self.status)
        contaminated["unexpected"] = "SYNTHETIC_UNAPPROVED_FIELD"
        with mock.patch.object(identity, "_identity_export_summary", return_value=contaminated), self.assertRaisesRegex(ValueError, "privacy"):
            self.export()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_no_verifier_rerun_or_managed_rehash(self) -> None:
        real_sha = identity._sha256
        def diagnostic_hash_only(path):
            self.assertEqual(Path(path).suffix, ".zip")
            self.assertEqual(Path(path).parent, self.root / "temp")
            return real_sha(path)
        with mock.patch.object(identity, "verify_runtime_identity", side_effect=AssertionError("gate rerun forbidden")), \
             mock.patch.object(identity, "_sha256", side_effect=diagnostic_hash_only):
            self.export()


if __name__ == "__main__":
    unittest.main()


def _main_block_fixture(tmp_path, monkeypatch):
    from mediataggerbot import __build_id__, __version__
    import mediataggerbot.main as main_module
    from test_v059_runtime_identity_gate import _synthetic_release

    root = _synthetic_release(tmp_path, version=__version__, build_id=__build_id__)
    monkeypatch.setattr(main_module, "find_project_root", lambda *_a, **_k: root)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Configuration or application collection must not run after identity BLOCK")
    for name in ("load_config_resilient", "copy_example_config_if_missing", "write_diagnostics_export"):
        monkeypatch.setattr(main_module, name, forbidden)
    return main_module, root


def test_main_identity_block_export_omits_rejected_control(tmp_path, monkeypatch):
    main_module, root = _main_block_fixture(tmp_path, monkeypatch)
    marker = "SYNTHETIC_REJECTED_CONTROL_NO_REAL_SECRET"
    metadata = root / "PACKAGE_METADATA.json"
    value = json.loads(metadata.read_text(encoding="utf-8"))
    value["fixture_note"] = marker
    metadata.write_text(json.dumps(value), encoding="utf-8")
    original = metadata.read_bytes()
    assert main_module.main(["--mode", "preflight"]) == 6
    assert metadata.read_bytes() == original
    archives = list((root / "diagnostics").glob("*IDENTITY_BLOCK.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as z:
        assert marker.encode() not in b"".join(z.read(name) for name in z.namelist())
        summary = json.loads(z.read("runtime_identity_status.json"))
    assert summary["gate_result"] == "BLOCK"
    assert summary["authenticated_activity_permitted"] is False


def test_main_status_write_failure_export_omits_error_body(tmp_path, monkeypatch):
    main_module, root = _main_block_fixture(tmp_path, monkeypatch)
    marker = "SYNTHETIC_PERSISTENCE_ERROR_NO_REAL_SECRET"
    def fail_persist(*_args, **_kwargs):
        raise PermissionError(marker)
    monkeypatch.setattr(main_module, "write_runtime_identity_status", fail_persist)
    assert main_module.main(["--mode", "preflight"]) == 6
    archives = list((root / "diagnostics").glob("*IDENTITY_BLOCK.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as z:
        assert marker.encode() not in b"".join(z.read(name) for name in z.namelist())
        summary = json.loads(z.read("runtime_identity_status.json"))
    assert summary["gate_result"] == "BLOCK"
    assert any(item["type"] == "runtime_identity_evidence_write_failed" for item in summary["mismatches"])


def test_main_export_publication_failure_stays_blocked(tmp_path, monkeypatch):
    main_module, root = _main_block_fixture(tmp_path, monkeypatch)
    (root / "src" / "app.py").write_text("synthetic mismatch\n", encoding="utf-8")
    import mediataggerbot.package_identity as live_identity
    def unavailable(*_args, **_kwargs):
        raise OSError("synthetic filesystem has no hard-link support")
    monkeypatch.setattr(live_identity.os, "link", unavailable)
    assert main_module.main(["--mode", "preflight"]) == 6
    assert not list((root / "diagnostics").glob("*.zip"))
    assert not list((root / "temp").glob("identity_export_*"))
    assert (root / "state" / "runtime_identity_status.json").is_file()
