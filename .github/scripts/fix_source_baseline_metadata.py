from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import subprocess
from pathlib import Path

ROOT = Path.cwd()
TEMP_PATHS = {
    ".github/scripts/fix_source_baseline_metadata.py",
    ".github/workflows/fix_source_baseline_metadata.yml",
}
OLD_BUILD = "MTB-0.5.9-PUBLIC-20260809-02"
NEW_BUILD = "MTB-0.5.9-PUBLIC-20260810-03"
BASELINE_COMMIT = "79e95bffce4a18cefd1b91b6b709f67dc45ca22c"
OLD_PARAMETER_SHA = "655564a81adeff17ddad1e33b1453ae64bde0f405a41e740e3b3a7f65934d2e0"
PARAMETER_SHA = "5dd39656afa5e8bcd0159e5ffa163d4de92a9ad4cb05c26aa63acf424ffe371f"
DATE = "2026-08-10 CDT / America/Chicago"
RIGHTS = "Copyright © 2026 Gateway Information Group LLC. All rights reserved."
SELF_MARKER = "SELF_REFERENTIAL_SEE_ZIP_SHA256_SIDECAR"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tracked_files() -> list[str]:
    return subprocess.check_output(["git", "ls-files"], text=True).splitlines()


def advance_current_build_identity() -> list[str]:
    skip = {"CHANGELOG.md", "MANIFEST.csv", "MANIFEST.json", *TEMP_PATHS}
    text_suffixes = {
        ".bat", ".cmd", ".csv", ".json", ".md", ".ps1", ".py",
        ".toml", ".txt", ".yaml", ".yml",
    }
    replaced: list[str] = []
    for relative in tracked_files():
        path = ROOT / relative
        if relative in skip or not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        text = read_text(path)
        if OLD_BUILD in text:
            write_text(path, text.replace(OLD_BUILD, NEW_BUILD))
            replaced.append(relative)
    required = {
        "PACKAGE_METADATA.json",
        "SOURCE_PROVENANCE.md",
        "Start_MediaTaggerBot.bat",
        "VERSION.txt",
        "src/mediataggerbot/__init__.py",
        "tests/test_v059_runtime_identity_gate.py",
    }
    missing = sorted(required - set(replaced))
    if missing:
        raise SystemExit(f"Current build identity was not found in required files: {missing}")
    return replaced


def repair_package_metadata() -> None:
    path = ROOT / "PACKAGE_METADATA.json"
    metadata = json.loads(read_text(path))
    mislabeled = metadata.pop("source_baseline_sha256", None)
    if mislabeled != "46d117514bf4e8a529127549b23d37a0eb0fcf12":
        raise SystemExit(f"Unexpected source_baseline_sha256 value: {mislabeled!r}")
    metadata["build_id"] = NEW_BUILD
    metadata["release_date"] = "2026-08-10"
    metadata["generated_cdt"] = DATE
    metadata["source_baseline"] = (
        f"MediaTaggerBot v0.5.9 public source commit {BASELINE_COMMIT}"
    )
    metadata["source_baseline_commit_sha"] = BASELINE_COMMIT
    metadata["source_baseline_commit_hash_algorithm"] = "git-sha1"
    metadata["metadata_correction"] = (
        "The prior source_baseline_sha256 key held a 40-character Git commit SHA-1. "
        "This build uses an explicit commit-SHA field and algorithm label."
    )
    write_text(path, json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")


def repair_documentation() -> None:
    provenance_path = ROOT / "SOURCE_PROVENANCE.md"
    provenance = read_text(provenance_path)
    provenance = provenance.replace(
        "- Parameter baseline: ChatGPT New Thread Parameters v2.17.5",
        "- Parameter baseline: ChatGPT New Thread Parameters v2.17.6",
    ).replace(OLD_PARAMETER_SHA, PARAMETER_SHA)
    anchor = f"- Runtime build ID: `{NEW_BUILD}`\n"
    if anchor not in provenance:
        raise SystemExit("SOURCE_PROVENANCE.md did not receive the current build ID")
    if "- Source baseline commit:" not in provenance:
        provenance = provenance.replace(
            anchor,
            anchor + f"- Source baseline commit: `{BASELINE_COMMIT}` (Git SHA-1)\n",
            1,
        )
    if "ChatGPT New Thread Parameters v2.17.5" in provenance or OLD_PARAMETER_SHA in provenance:
        raise SystemExit("SOURCE_PROVENANCE.md still contains the retired parameter baseline")
    write_text(provenance_path, provenance)

    readme_path = ROOT / "README.md"
    readme = read_text(readme_path)
    old_readme = "- Adds the v2.17.5 runtime release-identity and managed-file integrity gate."
    new_readme = (
        "- Uses the current v2.17.6 delivery baseline while retaining the v2.17.5 "
        "runtime release-identity and managed-file integrity gate."
    )
    if old_readme not in readme:
        raise SystemExit("README baseline marker was not found")
    write_text(readme_path, readme.replace(old_readme, new_readme, 1))

    release_notes_path = ROOT / "RELEASE_NOTES.md"
    release_notes = read_text(release_notes_path)
    old_tail = (
        f"\n{RIGHTS}\n"
        "- v2.17.6 alignment: stable canonical entrypoint, launcher-derived project root, "
        "project-local outputs, and cross-working-directory regression coverage.\n"
    )
    new_tail = (
        "\n- the v2.17.6 alignment keeps a stable canonical entrypoint, a launcher-derived "
        "project root, project-local outputs, and cross-working-directory regression coverage; and\n"
        "- release metadata now labels the source baseline as a Git commit SHA-1 instead of "
        "misidentifying it as SHA-256.\n\n"
        f"{RIGHTS}\n"
    )
    if old_tail not in release_notes:
        raise SystemExit("The expected RELEASE_NOTES.md tail was not found")
    release_notes = release_notes.replace(old_tail, new_tail, 1)
    if release_notes.rstrip().splitlines()[-1] != RIGHTS:
        raise SystemExit("Release notes rights notice is not the final line")
    write_text(release_notes_path, release_notes)

    changelog_path = ROOT / "CHANGELOG.md"
    changelog = read_text(changelog_path)
    heading = "# Changelog\n\n"
    section = (
        f"## v0.5.9 build {NEW_BUILD}\n\n"
        "- Corrected the source-baseline metadata key so a Git commit SHA-1 is no longer labeled SHA-256.\n"
        "- Reconciled the current v2.17.6 parameter baseline in source-provenance documentation.\n"
        "- Reordered release notes so the rights notice remains the final release statement.\n"
        "- Regenerated the managed-file inventory and retained the v0.5.9 runtime and dependency behavior.\n\n"
    )
    if not changelog.startswith(heading):
        raise SystemExit("Unexpected CHANGELOG.md heading")
    if NEW_BUILD not in changelog:
        changelog = heading + section + changelog[len(heading):]
    write_text(changelog_path, changelog)


def add_semantic_regression_test() -> None:
    path = ROOT / "tests/test_v2176_entrypoint_outputs.py"
    text = read_text(path)
    if "import re\n" not in text:
        text = text.replace("import json\n", "import json\nimport re\n", 1)
    anchor = '    outputs = metadata["project_local_outputs"]\n'
    assertions = (
        '    assert "source_baseline_sha256" not in metadata\n'
        '    assert metadata["source_baseline_commit_hash_algorithm"] == "git-sha1"\n'
        '    assert re.fullmatch(r"[0-9a-f]{40}", metadata["source_baseline_commit_sha"])\n'
    )
    if assertions not in text:
        if anchor not in text:
            raise SystemExit("v2.17.6 metadata assertion anchor was not found")
        text = text.replace(anchor, anchor + assertions, 1)
    write_text(path, text)


def regenerate_manifests(replaced_paths: list[str]) -> dict[str, object]:
    manifest_path = ROOT / "MANIFEST.json"
    csv_path = ROOT / "MANIFEST.csv"
    manifest = json.loads(read_text(manifest_path))
    records = manifest.get("files")
    if not isinstance(records, list):
        raise SystemExit("MANIFEST.json files is not a list")
    record_by_path = {str(row.get("path")): row for row in records if isinstance(row, dict)}
    if len(record_by_path) != len(records):
        raise SystemExit("MANIFEST.json contains duplicate or invalid path records")

    final_tracked = [
        relative for relative in tracked_files()
        if (ROOT / relative).is_file() and relative not in TEMP_PATHS
    ]
    if set(final_tracked) != set(record_by_path):
        missing = sorted(set(final_tracked) - set(record_by_path))
        extra = sorted(set(record_by_path) - set(final_tracked))
        raise SystemExit(f"Manifest/source inventory drift; missing={missing}, extra={extra}")

    manifest["build_id"] = NEW_BUILD
    manifest["generated_cdt"] = DATE
    manifest["file_count"] = len(records)
    changed_paths = set(replaced_paths) | {
        "CHANGELOG.md",
        "MANIFEST.csv",
        "MANIFEST.json",
        "PACKAGE_METADATA.json",
        "README.md",
        "RELEASE_NOTES.md",
        "SOURCE_PROVENANCE.md",
        "tests/test_v2176_entrypoint_outputs.py",
    }

    for relative, row in record_by_path.items():
        row["version"] = "v0.5.9"
        if relative in changed_paths:
            row["modified_cdt"] = DATE
        if relative in {"MANIFEST.csv", "MANIFEST.json"}:
            continue
        path = ROOT / relative
        if row.get("package_managed") is True:
            row["size_bytes"] = path.stat().st_size
            row["sha256"] = sha256_file(path)
        else:
            row["size_bytes"] = None
            row["sha256"] = None

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
    expected_fields = [
        "asset_id", "path", "title", "purpose", "asset_class", "role", "format",
        "project_slug", "version", "status", "sensitivity", "source_of_truth",
        "tags", "aliases", "lineage", "created_cdt", "modified_cdt", "size_bytes",
        "sha256", "package_managed", "rights_holder", "copyright_year",
        "rights_notice", "rights_scope", "license",
    ]
    if fieldnames != expected_fields:
        raise SystemExit(f"Unexpected MANIFEST.csv columns: {fieldnames}")

    csv_record = record_by_path["MANIFEST.csv"]
    json_record = record_by_path["MANIFEST.json"]
    csv_record["package_managed"] = True
    json_record["package_managed"] = False
    csv_record["modified_cdt"] = DATE
    json_record["modified_cdt"] = DATE

    def make_csv(csv_size: int, json_size: int) -> bytes:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in records:
            out = dict(row)
            if out["path"] == "MANIFEST.csv":
                out["size_bytes"] = csv_size
                out["sha256"] = SELF_MARKER
                out["package_managed"] = True
            elif out["path"] == "MANIFEST.json":
                out["size_bytes"] = json_size
                out["sha256"] = SELF_MARKER
                out["package_managed"] = False
            for key in ("tags", "aliases"):
                if isinstance(out.get(key), list):
                    out[key] = ";".join(str(value) for value in out[key])
            writer.writerow({key: out.get(key) for key in fieldnames})
        return buffer.getvalue().encode("utf-8-sig")

    def make_json(json_size: int, csv_bytes: bytes) -> bytes:
        csv_record["size_bytes"] = len(csv_bytes)
        csv_record["sha256"] = sha256_bytes(csv_bytes)
        json_record["size_bytes"] = json_size
        json_record["sha256"] = SELF_MARKER
        return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    csv_guess = csv_path.stat().st_size
    json_guess = manifest_path.stat().st_size
    previous_state: tuple[int, int, str] | None = None
    for _ in range(30):
        csv_bytes = make_csv(csv_guess, json_guess)
        json_bytes = make_json(json_guess, csv_bytes)
        next_state = (len(csv_bytes), len(json_bytes), sha256_bytes(csv_bytes))
        if (
            next_state == previous_state
            and csv_guess == len(csv_bytes)
            and json_guess == len(json_bytes)
        ):
            break
        previous_state = next_state
        csv_guess = len(csv_bytes)
        json_guess = len(json_bytes)
    else:
        raise SystemExit("Self-referential manifest size convergence failed")

    csv_bytes = make_csv(csv_guess, json_guess)
    json_bytes = make_json(json_guess, csv_bytes)
    if len(csv_bytes) != csv_guess or len(json_bytes) != json_guess:
        raise SystemExit("Final manifest size fixed point did not hold")
    csv_path.write_bytes(csv_bytes)
    manifest_path.write_bytes(json_bytes)

    return {
        "new_build_id": NEW_BUILD,
        "records": len(records),
        "package_managed": sum(row.get("package_managed") is True for row in records),
        "replaced_build_identity_paths": sorted(replaced_paths),
        "manifest_csv_bytes": len(csv_bytes),
        "manifest_json_bytes": len(json_bytes),
    }


def final_semantic_preflight() -> None:
    metadata = json.loads(read_text(ROOT / "PACKAGE_METADATA.json"))
    manifest = json.loads(read_text(ROOT / "MANIFEST.json"))
    if "source_baseline_sha256" in metadata:
        raise SystemExit("Retired source_baseline_sha256 key remains")
    if metadata["source_baseline_commit_hash_algorithm"] != "git-sha1":
        raise SystemExit("Git commit hash algorithm is not explicit")
    if not re.fullmatch(r"[0-9a-f]{40}", metadata["source_baseline_commit_sha"]):
        raise SystemExit("Source baseline commit is not a 40-character Git SHA-1")
    if metadata["build_id"] != NEW_BUILD or manifest["build_id"] != NEW_BUILD:
        raise SystemExit("Build identity did not converge")
    if metadata["parameter_package_sha256"] != PARAMETER_SHA:
        raise SystemExit("Parameter package digest changed unexpectedly")


def main() -> int:
    replaced_paths = advance_current_build_identity()
    repair_package_metadata()
    repair_documentation()
    add_semantic_regression_test()
    receipt = regenerate_manifests(replaced_paths)
    final_semantic_preflight()
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
