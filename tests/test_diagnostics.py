from __future__ import annotations

import json
import zipfile
from pathlib import Path

from mediataggerbot.config import load_config
from mediataggerbot.diagnostics import EXPORT20_MAX_FILES, write_diagnostics_export
from mediataggerbot.utils import write_json_atomic

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_diagnostics_is_export20_and_includes_scan_state(tmp_path: Path):
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    cfg.data["paths"]["diagnostics_dir"] = str(tmp_path / "diagnostics")
    cfg.data["paths"]["temp_dir"] = str(tmp_path / "temp")
    cfg.data["paths"]["state_dir"] = str(tmp_path / "state")
    cfg.diagnostics_dir.mkdir(parents=True, exist_ok=True)
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(cfg.state_dir / "last_run_status.json", {"status": "running"})
    write_json_atomic(cfg.state_dir / "last_scan_coverage.json", {"all_reachable_subfolders_checked": True})
    write_json_atomic(cfg.state_dir / "last_api_metrics.json", {"status": "ok", "requests_sent": 2})
    write_json_atomic(cfg.state_dir / "last_journal_reconciliation.json", {"checked": 1, "retryable": 1})

    path = write_diagnostics_export(cfg, "test_run", "diagnostics")

    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert len(names) <= EXPORT20_MAX_FILES
        assert "state/last_run_status.json" in names
        assert "state/last_scan_coverage.json" in names
        assert "state/last_api_metrics.json" in names
        assert "state/last_journal_reconciliation.json" in names
        assert "README.md" in names
        assert "CHANGELOG.md" in names
        assert "SECURITY.md" in names
        # Inactive lock/journal details may be compacted into the mandatory summary so
        # higher-value transfer/manifest/changelog evidence fits inside Export20.
        summary = json.loads(archive.read("diagnostic_summary.json"))
        assert "operation_journal_status" in summary
        assert "lock_status" in summary
        assert "graceful_stop_status" in summary
        assert summary["scan_policy"]["recursive"] is True
        assert summary["scan_policy"]["complete_signal"] == "all_reachable_subfolders_checked=true"
        assert summary["sqlite_runtime"]["single_writer_enforced_by_process_lock"] is True
        assert summary["sqlite_runtime"]["sqlite_version"]
