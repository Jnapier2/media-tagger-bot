# Changelog

## v0.5.9 build MTB-0.5.9-PUBLIC-20260810-03

- Corrected the source-baseline metadata key so a Git commit SHA-1 is no longer labeled SHA-256.
- Reconciled the current v2.17.6 parameter baseline in source-provenance documentation.
- Reordered release notes so the rights notice remains the final release statement.
- Regenerated the managed-file inventory and retained the v0.5.9 runtime and dependency behavior.

## v0.5.9 build MTB-0.5.9-PUBLIC-20260809-02

- Aligned the stable `Start_MediaTaggerBot.bat` entrypoint and execution namespace with v2.17.6.
- Removed the caller-current-directory fallback from project-root discovery.
- Recorded project-local output roots and added cross-working-directory regression coverage.
- Preserved the v0.5.9 user-facing version, runtime identity gate, dependency lock, and media-processing behavior.

## 0.5.9 — Runtime identity and integrity gate

- Added pre-configuration release identity verification.
- Added SHA-256 verification for every immutable package-managed file.
- Added fail-closed mixed-release handling and bounded pre-auth diagnostic evidence.
- Preserved complete-scan, dry-run, apply journal, readback, and rollback controls.
- Public-source hardening raises target-collision evidence in Export20 selection so it cannot be crowded out by lower-priority state snapshots.
- Published a sanitized source tree that excludes credentials, runtime data, media, private operating evidence, and internal handoff material.

Source package provenance SHA-256: `7b359401997725ee93e2249f41fe6ed26fe7e74ca044141a87215956965b15ac`

Copyright © 2026 Gateway Information Group LLC. All rights reserved.