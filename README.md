# MediaTaggerBot v0.5.9

[![CI](https://github.com/Jnapier2/media-tagger-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/Jnapier2/media-tagger-bot/actions/workflows/ci.yml)

MediaTaggerBot is a local-first Windows utility for reviewing, matching, tagging, and safely renaming music files. It combines public metadata evidence with local file analysis, keeps uncertain matches visible for review, and records every applied change so it can be verified or rolled back.

## What changed in v0.5.9

- Adds the v2.17.5 runtime release-identity and managed-file integrity gate.
- Verifies the running package ID, version, build ID, `VERSION.txt`, `MANIFEST.json`, `PACKAGE_METADATA.json`, and every immutable `package_managed=true` file before loading runtime configuration or credentials.
- Fails closed on mixed or stale release files while retaining local status, logs, recovery guidance, and a bounded diagnostic export.
- Preserves complete-scan, confidence, ambiguity, dry-run, journal, readback, and rollback controls.
- Keeps computer recognition informational and nonrestrictive; it cannot block launch, assign ownership, or require a cross-computer handoff.

## Safe workflow

1. Extract the complete release into a fresh local folder.
2. Run `Start_MediaTaggerBot.bat`.
3. Use **Preflight** before any apply operation.
4. Review the dry-run report and unresolved/ambiguous items.
5. Apply only after the release identity gate and readiness checks pass.
6. Retain the journal and rollback manifest until the result is independently reviewed.

## Runtime boundary

The application does not commit credentials, runtime databases, media, logs, state, or user-specific configuration. `config/config.example.toml` documents the schema; the actual `config/config.toml` remains local and is intentionally excluded from public source and managed-file hashing.

A runtime identity failure blocks configuration and credential loading, authenticated API work, and media mutation. It does not rewrite release files. Recovery is to preserve the diagnostic evidence and re-extract a complete verified package into a clean folder.

## Dependencies

- Python 3.11–3.14
- `requests==2.33.0`
- `mutagen==1.47.0`
- `pytest==9.0.3` for tests
- `setuptools==83.0.0` for the isolated build backend
- FFprobe is optional and used only when available

The exact runtime transitive set is recorded in `requirements.lock.txt`; the public repository does not bundle third-party wheels or executables. Third-party packages retain their own licenses.

## Verification

```powershell
py -3.13 -m pip install -r requirements.lock.txt --require-hashes
py -3.13 -m pip install -e .
py -3.13 -m pip install -r requirements-test.txt
py -3.13 -m pip check
py -3.13 -m pytest -q
```

The sanitized public-source tree was derived from the user-confirmed v0.5.9 release package with SHA-256:

`7b359401997725ee93e2249f41fe6ed26fe7e74ca044141a87215956965b15ac`

Private operating counts, media-library contents, credentials, support exports, working notes, and machine-specific evidence are not included.

## Boundaries

- Use only on media you own or are authorized to modify.
- Matching evidence is advisory until reviewed; uncertain files remain held rather than forced into a match.
- Public source and CI do not claim every filesystem, codec, provider, or physical Windows environment has been exercised.
- The repository is source code, not a signed Windows installer.

## Portfolio and rights

[Portfolio](https://jerry-napier-portfolio.netlify.app/) · [GitHub profile](https://github.com/Jnapier2)

Copyright © 2026 Gateway Information Group LLC. All rights reserved.

This notice does not create or replace a software license. Third-party components retain their respective notices and licenses.

## Execution identity and project-local outputs

`Start_MediaTaggerBot.bat` is the stable, unversioned, project-qualified Windows entrypoint. It resolves the project root from its own location and delegates to `python -m mediataggerbot`. Runtime-owned configuration, logs, state, temporary files, exports, diagnostics, reports, caches, and backups remain under that project root. The user-selected media library is the only normal external data root and is validated separately. A cross-working-directory regression test prevents the caller's current directory from becoming project authority.
