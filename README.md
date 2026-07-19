# MediaTaggerBot

[![CI](https://github.com/Jnapier2/media-tagger-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/Jnapier2/media-tagger-bot/actions/workflows/ci.yml)

MediaTaggerBot is a local-first workflow for standardizing audio and music-video libraries when a wrong match is more costly than an unresolved file.

MediaTaggerBot turns inconsistent local filenames and tags into a predictable library structure:

```text
Artist - Title - Genre.ext
Artist - Title - Genre - Subgenre.ext
```

Built for Windows, it combines public metadata with local evidence, records why each match was accepted, and places dry-run review and conservative apply gates between identification and file mutation.

## Workflow safeguards

- Evidence hierarchy: embedded stable IDs and acoustic fingerprints outrank text matching.
- Safe operating modes: preflight, recursive scan, and dry-run precede any write operation.
- Conservative apply gate: `apply-safe` rejects ambiguous, weakly corroborated, fallback-genre, or incomplete-scan candidates.
- Transaction discipline: a durable SQLite journal, source-change checks, metadata readback, and rename verification make partial failures visible.
- Operational resilience: bounded retries, provider-specific circuit breakers, cache integrity checks, single-instance ownership, and graceful stop requests.
- Recovery evidence: rollback manifests restore filenames; diagnostics are allowlisted, size-bounded, and redacted.

```text
recursive scan
    -> identity evidence
    -> confidence and ambiguity gates
    -> dry-run review
    -> journaled metadata write and verification
    -> verified rename
```

## Quick start

Requirements:

- Windows 10 or later
- 64-bit CPython 3.11–3.14
- Network access for Python dependencies and enabled metadata providers
- Optional: `fpcalc`, `ffprobe`/FFmpeg, and ExifTool; see [tools/README.md](tools/README.md)

The easiest path is to clone the repository and run `Start_MediaTaggerBot.bat`. The launcher creates a project-local virtual environment and installs the exact hash-checked dependencies in `requirements.lock.txt`.

For a manual setup:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e .
copy config\config.example.toml config\config.toml
.venv\Scripts\python -m mediataggerbot --mode preflight
```

Set `paths.media_root` and a meaningful `project.contact` value in the generated `config/config.toml`. API credentials can also be provided through `ACOUSTID_CLIENT_KEY`, `LASTFM_API_KEY`, and `DISCOGS_USER_TOKEN` environment variables.

## Recommended operating sequence

1. Back up the media you intend to test.
2. Run **Repair/check** and set the media root.
3. Run **Preflight**, then **Scan-only**.
4. Review a **Dry-run** and its exception reports.
5. Test **Apply-safe** on a small, backed-up sample before widening scope.

`apply-all` intentionally accepts weaker evidence and should not be treated as the default. A limited, interrupted, excluded, or otherwise incomplete traversal blocks apply modes.

## Safety and privacy boundary

- The application changes files only in an explicit apply mode.
- Filename rollback does not restore overwritten embedded metadata; backups remain the recovery source.
- Enabled providers may receive artist/title, identifiers, fingerprints, duration, or release context needed for matching. No media file is uploaded by this code.
- Runtime configuration, logs, caches, reports, diagnostics, and media are excluded from version control.
- Use the project only with media you are authorized to inspect and modify.

See [API and integration notes](docs/API_NOTES.md) and [operations](docs/OPERATIONS.md) for the implementation boundary.

## Development

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m compileall -q src scripts tests
.venv\Scripts\python -m pytest -q
```

The test suite uses controlled provider responses and temporary media fixtures. It does not require live API credentials.

## Status

MediaTaggerBot v0.5.4 is a local application, not a managed metadata service. Review the configuration and dry-run output for your environment before any mutation.

## License

Copyright © 2026 Gateway Information Group LLC. All rights reserved. Limited evaluation rights are described in [LICENSE.md](LICENSE.md). Third-party packages and optional tools remain under their respective licenses.
