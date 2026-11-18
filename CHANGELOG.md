# Changelog

## v0.5.9 build MTB-0.5.9-PUBLIC-20260918-BOOTPRIV1 — unpublished candidate

- Replaced raw-file startup-failure export with five bounded cached-evidence summary entries. Rejected control bytes, log bodies, and arbitrary error strings are excluded before staging.
- Preserved the startup BLOCK decision, configuration/authentication ordering, media-processing code, existing launcher name, and all existing test methods.
- Added summary-privacy, publication-failure and actual startup-path regressions. Output uses project-local staging and no-overwrite hard-link publication; unsupported filesystems fail closed.
- Corrected the runtime verifier's Requests expectation from 2.32.5 to the already-pinned 2.33.0. Added lock/SBOM parity and synthetic distribution-RECORD acceptance/rejection coverage; dependencies are not upgraded.
- Reconciled candidate identity and managed hashes. Corrected the canonical manifest's 40-character Git commit identifier field so it is no longer labeled SHA-256; retained original provenance in the untouched baseline.
- Source and extracted-package test results are recorded separately from exact pinned-dependency, hosted CI, physical Windows and Norton acceptance. Those latter checks remain outstanding.

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