from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BAT = PROJECT_ROOT / "Start_MediaTaggerBot.bat"


def test_bat_menu_labels_and_routes_are_complete() -> None:
    text = BAT.read_text(encoding="utf-8")
    labels = re.findall(r"(?im)^:([A-Za-z0-9_-]+)\s*$", text)
    assert len(labels) == len(set(label.casefold() for label in labels))
    label_set = {label.casefold() for label in labels}
    targets = re.findall(r"(?im)\b(?:goto|call\s+:)([A-Za-z0-9_-]+)\b", text)
    missing = sorted({target for target in targets if target.casefold() not in label_set})
    assert missing == []
    for choice in range(1, 15):
        assert f'if "%CHOICE%"=="{choice}" goto ' in text
    assert 'if "%CHOICE%"=="0" exit /b 0' in text


def test_bat_uses_environment_transport_not_root_argv_quoting() -> None:
    text = BAT.read_text(encoding="utf-8")
    assert "MEDIATAGGERBOT_ROOT_OVERRIDE" in text
    assert "MEDIATAGGERBOT_ROLLBACK_MANIFEST" in text
    assert '--root "%ROOT_ARG%"' not in text
    assert '--rollback-manifest "%ROLLBACK_ARG%"' not in text
    lowered = text.casefold()
    assert "powershell.exe" not in lowered
    assert "-executionpolicy" not in lowered
    assert "legacy powershell launcher detected" in lowered


def test_bat_menu_modes_match_python_modes() -> None:
    text = BAT.read_text(encoding="utf-8")
    for mode in [
        "preflight",
        "scan-only",
        "dry-run",
        "apply-safe",
        "apply-all",
        "diagnostics",
        "rollback",
        "set-root",
        "validate-config",
        "repair",
        "request-stop",
    ]:
        assert mode in text


def test_bat_uses_hash_checked_public_dependency_install() -> None:
    text = BAT.read_text(encoding="utf-8")
    lowered = text.casefold()
    assert "requirements.lock.txt" in lowered
    assert "--require-hashes" in lowered
    assert "--no-index" not in lowered
    assert "\\wheels" not in lowered
    assert "pip install --disable-pip-version-check -r" not in lowered
    assert "request-stop" in lowered
    for version in ["3.11", "3.12", "3.13", "3.14"]:
        assert version in text


def test_public_dependency_lock_is_complete() -> None:
    lock = PROJECT_ROOT / "requirements.lock.txt"
    wheels = PROJECT_ROOT / "wheels"
    assert lock.exists()
    assert not wheels.exists()
    lock_text = lock.read_text(encoding="utf-8")
    for package in ["requests", "mutagen", "charset-normalizer", "idna", "urllib3", "certifi"]:
        assert f"{package}==" in lock_text
    assert lock_text.count("--hash=sha256:") == 9


def test_request_stop_bypasses_runtime_rebuild_and_uses_stdlib_control_script() -> None:
    text = BAT.read_text(encoding="utf-8")
    lowered = text.casefold()
    branch = lowered.index('if /i "%mode%"=="request-stop" goto execute_request_stop_control')
    ensure = lowered.index("call :ensure_runtime", branch)
    assert branch < ensure
    control_start = lowered.index(":execute_request_stop_control")
    control_end = lowered.index(":set_launcher_environment", control_start)
    control = lowered[control_start:control_end]
    assert "scripts\\request_stop.py" in control
    assert "pip install" not in control
    assert "rmdir /s /q" not in control
    assert "-m venv" not in control
    assert "it will not create, delete, rebuild, or install into .venv" in control


def test_bat_declares_launcher_attestation_fields() -> None:
    text = BAT.read_text(encoding="utf-8")
    for key in [
        "MEDIATAGGERBOT_LAUNCHER_KIND",
        "MEDIATAGGERBOT_LAUNCHER_VERSION",
        "MEDIATAGGERBOT_LAUNCHER_PROJECT_ROOT",
        "MEDIATAGGERBOT_BATCH_LOG",
    ]:
        assert key in text


def test_bat_uses_windows_crlf_without_utf8_bom() -> None:
    data = BAT.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")
