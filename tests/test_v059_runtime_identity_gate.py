from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from mediataggerbot import __build_id__, __package_id__, __version__
from mediataggerbot.package_identity import verify_runtime_identity, write_identity_gate_support_export

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_release(tmp_path: Path, version: str = "9.9.9", build_id: str = "TEST-BUILD-1") -> Path:
    root = tmp_path / "release"
    (root / "src").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "config" / "config.toml").write_text("media_root = ''\n", encoding="utf-8")
    (root / "VERSION.txt").write_text(
        f"package_id=media-tagger-bot\nversion=v{version}\nbuild_id={build_id}\n", encoding="utf-8"
    )
    (root / "PACKAGE_METADATA.json").write_text(
        json.dumps({"package_id": "media-tagger-bot", "version": f"v{version}", "build_id": build_id}),
        encoding="utf-8",
    )
    rows = []
    for relative, managed in [
        ("VERSION.txt", True),
        ("PACKAGE_METADATA.json", True),
        ("src/app.py", True),
        ("config/config.toml", False),
    ]:
        path = root / relative
        rows.append({
            "path": relative,
            "package_managed": managed,
            "size_bytes": path.stat().st_size,
            "sha256": _sha(path),
        })
    manifest = {
        "package_id": "media-tagger-bot",
        "version": f"v{version}",
        "build_id": build_id,
        "files": rows,
    }
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return root


def test_clean_identity_gate_passes_and_mutable_config_is_excluded(tmp_path: Path) -> None:
    root = _synthetic_release(tmp_path)
    (root / "config" / "config.toml").write_text("media_root = 'D:/Music'\n", encoding="utf-8")
    status = verify_runtime_identity(root, "9.9.9", "TEST-BUILD-1")
    assert status["gate_result"] == "PASS"
    assert status["package_managed_count"] == 3
    assert status["package_verified_count"] == 3
    assert status["package_unmanaged_count"] == 1
    assert status["config_or_credentials_loaded_before_gate"] is False
    assert status["authentication_prevented_until_pass"] is True


def test_managed_file_hash_mismatch_blocks_same_version_package(tmp_path: Path) -> None:
    root = _synthetic_release(tmp_path)
    (root / "src" / "app.py").write_text("print('old mixed file')\n", encoding="utf-8")
    status = verify_runtime_identity(root, "9.9.9", "TEST-BUILD-1")
    assert status["gate_result"] == "BLOCK"
    assert any(row["type"] == "managed_sha256_mismatch" and row["path"] == "src/app.py" for row in status["mismatches"])


def test_identity_control_disagreement_blocks(tmp_path: Path) -> None:
    root = _synthetic_release(tmp_path)
    metadata = json.loads((root / "PACKAGE_METADATA.json").read_text(encoding="utf-8"))
    metadata["build_id"] = "OTHER-BUILD"
    (root / "PACKAGE_METADATA.json").write_text(json.dumps(metadata), encoding="utf-8")
    status = verify_runtime_identity(root, "9.9.9", "TEST-BUILD-1")
    assert status["gate_result"] == "BLOCK"
    assert any(row["type"] == "build_id_mismatch" and row["source"] == "PACKAGE_METADATA.json" for row in status["mismatches"])


def test_duplicate_and_unsafe_manifest_paths_block(tmp_path: Path) -> None:
    root = _synthetic_release(tmp_path)
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"].append(dict(manifest["files"][2]))
    manifest["files"].append({
        "path": "../outside.py", "package_managed": True, "size_bytes": 1, "sha256": "0" * 64
    })
    (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    status = verify_runtime_identity(root, "9.9.9", "TEST-BUILD-1")
    kinds = {row["type"] for row in status["mismatches"]}
    assert "duplicate_manifest_path" in kinds
    assert "unsafe_managed_path" in kinds
    assert status["gate_result"] == "BLOCK"


def test_missing_package_managed_flag_blocks(tmp_path: Path) -> None:
    root = _synthetic_release(tmp_path)
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"][2].pop("package_managed")
    (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    status = verify_runtime_identity(root, "9.9.9", "TEST-BUILD-1")
    assert any(row["type"] == "missing_or_invalid_package_managed_flag" for row in status["mismatches"])
    assert status["gate_result"] == "BLOCK"


def test_blocked_identity_support_export_is_bounded_and_contains_control_evidence(tmp_path: Path) -> None:
    root = _synthetic_release(tmp_path)
    (root / "src" / "app.py").write_text("tampered\n", encoding="utf-8")
    status = verify_runtime_identity(root, "9.9.9", "TEST-BUILD-1")
    path = write_identity_gate_support_export(root, "blocked-run", "apply-safe", status)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert len(names) <= 20
        assert {"runtime_identity_status.json", "VERSION.txt", "MANIFEST.json", "PACKAGE_METADATA.json"} <= names
        captured = json.loads(archive.read("runtime_identity_status.json"))
        assert captured["gate_result"] == "BLOCK"
    sidecar = path.with_suffix(path.suffix + ".sha256.txt")
    assert sidecar.exists()
    assert _sha(path) in sidecar.read_text(encoding="utf-8")


def test_main_orders_integrity_gate_before_config_and_credentials() -> None:
    text = (PROJECT_ROOT / "src" / "mediataggerbot" / "main.py").read_text(encoding="utf-8")
    gate = text.index("identity_status = verify_runtime_identity(")
    config = text.index("config = load_config_resilient(")
    assert gate < config
    assert "config_or_credentials_loaded_before_gate" in (PROJECT_ROOT / "src" / "mediataggerbot" / "package_identity.py").read_text(encoding="utf-8")


def test_running_release_declares_package_version_and_build_identity() -> None:
    assert __package_id__ == "media-tagger-bot"
    assert __version__ == "0.5.9"
    assert __build_id__ == "MTB-0.5.9-PUBLIC-20260810-03"



def test_missing_control_file_blocks(tmp_path: Path) -> None:
    root = _synthetic_release(tmp_path)
    (root / "PACKAGE_METADATA.json").unlink()
    status = verify_runtime_identity(root, "9.9.9", "TEST-BUILD-1")
    assert status["gate_result"] == "BLOCK"
    assert any(row["type"] == "missing_control_file" and row["path"] == "PACKAGE_METADATA.json" for row in status["mismatches"])


def test_absolute_backslash_colon_and_traversal_manifest_paths_block(tmp_path: Path) -> None:
    root = _synthetic_release(tmp_path)
    for bad in ["/absolute/file.py", "C:/outside.py", "src\\app.py", "../outside.py", "src/file:ads"]:
        manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
        manifest["files"].append({
            "path": bad,
            "package_managed": True,
            "size_bytes": 1,
            "sha256": "0" * 64,
        })
        (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        status = verify_runtime_identity(root, "9.9.9", "TEST-BUILD-1")
        assert status["gate_result"] == "BLOCK"
        assert any(row["type"] == "unsafe_managed_path" and row.get("path") == bad for row in status["mismatches"])
        # restore a clean manifest for the next bad path
        root = _synthetic_release(tmp_path / bad.replace("/", "_").replace("\\", "_").replace(":", "_"))


def test_managed_symlink_out_of_root_blocks(tmp_path: Path) -> None:
    root = _synthetic_release(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    link = root / "src" / "linked.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"].append({
        "path": "src/linked.py",
        "package_managed": True,
        "size_bytes": outside.stat().st_size,
        "sha256": _sha(outside),
    })
    (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    status = verify_runtime_identity(root, "9.9.9", "TEST-BUILD-1")
    assert status["gate_result"] == "BLOCK"
    assert any(row["type"] in {"managed_path_out_of_root", "managed_file_not_regular"} for row in status["mismatches"])


def test_main_blocks_mixed_release_before_config_loader(tmp_path: Path, monkeypatch) -> None:
    import mediataggerbot.main as main_module

    root = _synthetic_release(tmp_path, version=__version__, build_id=__build_id__)
    (root / "src" / "app.py").write_text("mixed old file\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "find_project_root", lambda *_args, **_kwargs: root)
    monkeypatch.setattr(
        main_module,
        "load_config_resilient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("config loader must not run")),
    )
    assert main_module.main(["--mode", "preflight"]) == 6
    status = json.loads((root / "state" / "runtime_identity_status.json").read_text(encoding="utf-8"))
    assert status["gate_result"] == "BLOCK"
    assert status["config_or_credentials_loaded_before_gate"] is False
    assert list((root / "diagnostics").glob("*IDENTITY_BLOCK.zip"))


def test_identity_evidence_persistence_failure_fails_closed_before_config(tmp_path: Path, monkeypatch) -> None:
    import mediataggerbot.main as main_module

    root = tmp_path / "release"
    root.mkdir()
    monkeypatch.setattr(main_module, "find_project_root", lambda *_args, **_kwargs: root)
    monkeypatch.setattr(
        main_module,
        "verify_runtime_identity",
        lambda *_args, **_kwargs: {
            "gate_result": "PASS",
            "package_managed_count": 1,
            "package_verified_count": 1,
            "mismatch_count": 0,
            "mismatches": [],
        },
    )
    monkeypatch.setattr(main_module, "write_runtime_identity_status", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("state blocked")))
    monkeypatch.setattr(
        main_module,
        "load_config_resilient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("config loader must not run")),
    )
    assert main_module.main(["--mode", "preflight"]) == 6
