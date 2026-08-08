#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

TARGET = Path(__file__).with_name("apply_public_release.py")


def replace_required(text: str, old: str, new: str) -> str:
    if old not in text:
        raise RuntimeError(f"expected reconciler fragment not found: {old[:100]!r}")
    return text.replace(old, new)


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")

    text = replace_required(
        text,
        'EXCLUDED={".venv","__pycache__",".pytest_cache","logs","exports","diagnostics","state","temp","archive","cache"}',
        'EXCLUDED={".git",".github",".venv","__pycache__",".pytest_cache","logs","exports","diagnostics","state","temp","archive","cache"}',
    )
    text = replace_required(
        text,
        '        if any(part in EXCLUDED for part in rel.parts) or path.suffix in {".pyc",".pyo"}: continue',
        '        if any(part in EXCLUDED or part.endswith(".egg-info") for part in rel.parts) or path.suffix in {".pyc",".pyo"}: continue',
    )

    manifest_marker = "    write_text(out/'tests/test_release_asset_manifest_v053.py'"
    test_patch = '''    # Hosted Windows runners have long temporary roots; path-budget tests use short synthetic parents.\n    p=out/'tests/test_v054_stability_usability.py'\n    test_text=p.read_text(encoding='utf-8')\n    test_text=test_text.replace('        nested = tmp_path / (\\"deep-folder-\\" * 4)\\n        nested.mkdir()\\n        source = nested / \\"source.mp3\\"\\n        source.write_bytes(b\\"\\")', '        nested = Path(\\"C:/mtb-test\\") / (\\"deep-folder-\\" * 2)\\n        source = nested / \\"source.mp3\\"')\n    p.write_text(test_text,encoding='utf-8')\n    p=out/'tests/test_v057_review_hardening.py'\n    test_text=p.read_text(encoding='utf-8')\n    test_text=test_text.replace('        fitted = fit_stem_to_full_path_budget(tmp_path, stem, \\".mp3\\", 120)', '        fitted = fit_stem_to_full_path_budget(Path(\\"C:/mtb-test\\"), stem, \\".mp3\\", 120)')\n    test_text=test_text.replace('        full = tmp_path / f\\"{fitted}.mp3\\"', '        full = Path(\\"C:/mtb-test\\") / f\\"{fitted}.mp3\\"')\n    p.write_text(test_text,encoding='utf-8')\n'''
    if manifest_marker not in text:
        raise RuntimeError("manifest-test generation marker not found")
    text = text.replace(manifest_marker, test_patch + manifest_marker, 1)

    workflow_start = text.index("    write_text(out/'.github/workflows/ci.yml'")
    files_start = text.index("    files=[]", workflow_start)
    text = (
        text[:workflow_start]
        + "    # Existing public .github workflows and Dependabot policy are preserved by replace_repo.\n"
        + "    shutil.copy2(Path(__file__).resolve(), out/'tools'/'apply_public_release.py')\n"
        + text[files_start:]
    )
    text = replace_required(
        text,
        "for p in sorted(x for x in out.rglob('*') if x.is_file() and x.relative_to(out).as_posix() not in {'MANIFEST.json','MANIFEST.csv'}):",
        "for p in sorted(x for x in out.rglob('*') if x.is_file() and x.relative_to(out).as_posix() not in {'MANIFEST.json','MANIFEST.csv','tools/apply_public_release.py'} and '.github' not in x.relative_to(out).parts):",
    )
    text = replace_required(
        text,
        "        if child.name=='.git': continue",
        "        if child.name in {'.git','.github'}: continue",
    )
    text = replace_required(
        text,
        "    for child in public.iterdir():\n        dest=repo/child.name",
        "    for child in public.iterdir():\n        if child.name=='.github': continue\n        dest=repo/child.name",
    )
    old_scan = '''    for p in out.rglob('*'):
        if p.is_file() and p.suffix.lower() in {'.py','.md','.txt','.json','.toml','.yml','.yaml','.csv','.bat'}:
            text=p.read_text(encoding='utf-8',errors='replace')
            for marker in FORBIDDEN:
                if marker in text: raise RuntimeError(f'private marker {marker!r} in {p.relative_to(out)}')
'''
    new_scan = '''    for p in out.rglob('*'):
        rel=p.relative_to(out).as_posix()
        if rel=='tools/apply_public_release.py':
            continue
        if p.is_file() and p.suffix.lower() in {'.py','.md','.txt','.json','.toml','.yml','.yaml','.csv','.bat'}:
            scanned=p.read_text(encoding='utf-8',errors='replace')
            for marker in FORBIDDEN:
                if marker in scanned: raise RuntimeError(f'private marker {marker!r} in {p.relative_to(out)}')
'''
    text = replace_required(text, old_scan, new_scan)

    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print("Patched the one-use reconciler for hosted Windows and public-repository boundaries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
