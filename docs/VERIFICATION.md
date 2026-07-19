# Verification

This document records checks performed against the curated public repository, not the original private release archive.

## Current result

Verified on 2026-07-18 with Windows and CPython 3.12.13:

```text
Python compile:                     pass
Pytest:                             111 passed, 1 skipped
Smoke test:                         pass
BAT routes and dependency policy:  pass
BAT CRLF / no UTF-8 BOM:            pass
Focused credential and path scan:  no sensitive values found
Private config and release files:   absent
```

The skipped test covers directory-symlink traversal on a host where creating directory symlinks was unavailable. Provider behavior is tested with controlled responses; no live credentials or network-provider results are claimed.

## Behavioral coverage

The suite exercises recursive traversal evidence, dry-run non-mutation, Apply-safe match gates, invalid identifier rejection, transient and permanent API failure handling, embedded-write verification, filename/path budgeting, cache recovery, operation journaling, diagnostics redaction, rollback containment, and Windows launcher routing.

## Reproduce

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m compileall -q src scripts tests
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\smoke_test.py
```

The GitHub Actions workflow repeats compile and test checks on supported Python versions. It does not invoke live metadata providers or mutate a media library.

## Public-repository boundary

The public repository intentionally excludes runtime configuration, API credentials, media, logs, caches, state databases, generated reports, diagnostics, private transfer records, package manifests, and the original bundled wheelhouse. Dependencies remain exact-version and hash checked through `requirements.lock.txt`, but installation uses the configured Python package index.

No static archive-entry or repository file-count claim is made because the source tree can evolve independently of the private release package.
