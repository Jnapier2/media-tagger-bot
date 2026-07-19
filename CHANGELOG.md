# Changelog

This file preserves the project’s technical release history. Packaging-only artifacts mentioned in older entries are not part of the curated public repository.

## v0.5.4 — 2026-07-18 CDT

Low-friction usability and stability release. This point release preserves the v0.5.3 production-safety gates and adds three bounded improvements based on current SQLite, Windows, and MusicBrainz/Picard guidance.

### Ease of use and stability

- Added a conservative complete-path budget (`naming.max_full_path_length`, default 240) in addition to the existing filename-length limit. Deep-folder targets and numbered collision suffixes are shortened before rename rather than failing late in Explorer, media players, archive tools, or legacy Win32 components.
- Added a non-mutating write-readiness probe. Dry-run now previews permission/active-lock failures for candidates that would otherwise qualify for Apply-safe, and Apply-safe repeats the probe immediately before mutation.
- Added `write_readiness_status` to full and exception reports plus `write_readiness_blocker_count` to the summary.
- Added SQLite `PRAGMA optimize=0x10002` on writable cache/journal open and bounded `PRAGMA optimize` before close. Maintenance failures are noncritical for disposable caches and never weaken the fail-closed operation journal.
- Added preflight and diagnostic visibility for the full-path budget, write-readiness probing, and SQLite optimization policy.

### Compatibility and scope

- No new dependency, menu option, background service, or per-file confirmation was added.
- No change was made to the eight-genre taxonomy, stable-ID/fingerprint safety hierarchy, recursive scan proof, rollback containment, or v0.5.2 text-identity quarantine.
- Album/folder clustering was researched because Picard recommends cluster/lookup for album-grouped files, but automatic cluster-driven identity promotion remains deferred until it can be validated on real library evidence without reintroducing false-match risk.

### Verification

- Added regression tests for complete-path budgeting, collision suffix budgeting, parent-path exhaustion, normal/blocked write-readiness probes, and SQLite optimization telemetry.

## v0.5.3 — 2026-07-14 CDT

Production-evidence safety remediation release. This point release preserves the recursive scanner, naming policy, asset metadata, offline runtime, transaction journal, rollback, and diagnostics architecture while tightening Apply-safe around real false-match and permission evidence.

### Critical fixes

- Text-search score alone is no longer sufficient for Apply-safe. Added non-generic artist/title, similarity, duration, and independent-corroboration gates.
- Invalid ISRC-like values are rejected before MusicBrainz calls. Permanent 4xx/item errors can no longer open the global transient circuit.
- Prior MediaTaggerBot text-search identities are quarantined; their embedded IDs are not trusted as direct shortcuts and the old text identity-memory namespace is ignored.
- Durable identity memory v2 accepts only stable-ID or acoustic-fingerprint identities.
- Apply-safe blocks blind fallback genre assignment.
- Supported formats must receive verified embedded metadata before rename; a sidecar-only result is reported as an error.
- Added one bounded read-only-attribute retry with original mode restoration; ACLs and security products remain untouched.
- Hard per-file failures now produce nonzero `completed_with_errors` terminal truth instead of clean `completed_verified`.
- Removed filename dashes created solely from trailing Windows-invalid punctuation.

### Reporting and recovery

- Added `prior_text_identity_review_<run_id>.csv` for focused review of files touched by earlier text-search provenance.
- Added production remediation documentation and updated the runbook/known-good/transfer boundary.
- Filename rollback remains filename-only; metadata restoration requires backups.

### Verification

- Added production-evidence regression tests for title-only false matches, prior-text trust bypass, fallback genre, malformed ISRC, permanent 400 circuit behavior, sidecar-only write failure, punctuation cleanup, and R&B subgenre spelling.

## v0.5.2 — 2026-07-13 CDT

Asset-metadata and searchability alignment release. This is a compatible upgrade from v0.5.1; matching thresholds, filename policy, recursive traversal, and mutation safety are preserved.

### Asset metadata and provenance

- Upgraded the canonical JSON/CSV release manifest to `asset-metadata-v1`: stable asset IDs, title/purpose, class/role/format, project slug, version/status, sensitivity, source-of-truth flag, controlled tags/aliases, lineage, timestamps, size, and SHA256.
- Added compact metadata headers/comments to key documentation, launcher, and config templates.
- Added `ASSET_METADATA_POLICY.md` describing the two-tier no-sidecar-sprawl policy and privacy boundary.
- Added one per-run JSON/CSV asset registry under `exports\<run_id>\`; it discovers only project-owned outputs for that run and stores project-relative paths.
- Added stable managed-media asset IDs from MusicBrainz recording ID, ISRC, or AcoustID, plus asset status/class/tags/lineage/schema fields in ID3, MP4, Vorbis-style metadata, supported ExifTool containers, and existing fallback sidecars.
- Weak filename/tag-only matches intentionally receive no invented path-derived asset identity.
- Active Python/BAT logs are marked mutable rather than assigned a checksum that would become stale after final log lines; oversized non-key reports are indexed with an explicit hash-budget status.
- Added compact asset-metadata policy evidence to preflight/environment and bounded support diagnostics.

### Stability and compatibility

- Preserved all v0.5.1 dependency-light control modes, BAT attestation, Windows-path recovery, offline hash-locked wheelhouse, complete-scan apply gate, source guard, metadata readback, verified rename, operation journal, rollback containment, graceful stop, caches, and diagnostics.
- Kept XMP `Identifier` reserved for MusicBrainz/ISRC verification; MediaTaggerBot asset identity uses a separate XMP relation field so video readback safety is not weakened.
- Runtime asset-registry generation is best-effort and cannot mask the selected mode's real exit code.

### Verification

- 96 automated tests pass, including seven v0.5.2 asset-metadata/release-registry tests.
- Direct preflight and diagnostics runs generated project-relative runtime asset manifests with verified report/diagnostic hashes and correctly marked mutable/self-referential records.
- Final package verification additionally checks manifest coverage, embedded/header metadata coverage, ZIP comment metadata, CRC integrity, checksum sidecar, no secrets, no runtime folders, and no bundled media.

### Rollback

Use v0.5.1. The asset metadata fields are additive; media tags written by v0.5.2 remain ordinary custom metadata fields and do not alter audio/video payloads.

## v0.5.1 — 2026-07-13 CDT

Completion and recovery hardening release against the v2.16.4 triage/exit parameters. This is a compatible upgrade from v0.5.0; the matching and naming policy is preserved.

### Critical and high-priority fixes

- Split dependency-light control modes from the media runtime. Diagnostics, repair, set-root, config validation, rollback, and request-stop no longer create, rebuild, validate, or install into `.venv`.
- Added BAT-to-Python launcher attestation for launcher kind, version, project root, and transcript path. Processing fails closed on a stale or mismatched BAT handoff while direct Python remains supported.
- Prevented diagnostics and request-stop from overwriting an active run's `last_run_status.json` or `last_run_exit.json`.
- Put every mutating/config-repair route behind the owner-aware single-instance lock. Repair cannot quarantine launchers while another run is active.
- Added reversible, checksum-verified quarantine for the exact obsolete `Launch_MediaTaggerBot.ps1`; it is archived, never deleted.
- Added semantic config validation and fail-closed project-local runtime directories. Absolute or traversal values for logs, exports, state, diagnostics, and temp cannot redirect runtime output outside the project.
- Hardened the portable request-stop parser with the same project-containment rule, including malformed or partially trusted configs.
- Added process-start identity to lock ownership so PID reuse cannot falsely identify an unrelated process as the active bot.
- Strengthened rollback validation with cross-record path-graph rejection, both-path-exists collision rejection, and optional post-apply size/mtime stale-source checks before any move.

### Throughput and prime-directive improvements

- Added a persistent inventory cache keyed by full path, size, modified time, and scanner capability signature. Repeated scans reuse unchanged tags/duration while automatically invalidating changed or moved files.
- Added an FFmpeg Chromaprint fallback when `fpcalc` is absent and the installed FFmpeg exposes the Chromaprint muxer. `fpcalc` remains preferred.
- Made report files atomic: CSV, JSONL, HTML, summaries, coverage, review queues, and rollback outputs are staged and finalized rather than leaving partial files on interruption.
- Added inventory-cache and launcher-attestation evidence to preflight, summaries, state, and bounded support diagnostics.

### BAT/menu and upgrade behavior

- Preserved one BAT launcher with menu choices 0–14 and direct-Python execution; no PowerShell execution-policy dependency was reintroduced.
- Normalized the shipped BAT to Windows CRLF without a UTF-8 BOM and added a byte-level packaging regression test.
- Recovery/control modes use any supported project or system Python 3.11–3.14 without installing media dependencies.
- Manual config editing validates through the dependency-light control path.
- Runtime setup remains offline and hash-locked for processing modes only.

### Verification

- Expanded the functional suite from 69 to 91 tests.
- Added tests for dependency-free control imports, active-run state preservation, repair locking, launcher quarantine, launcher attestation, inventory-cache invalidation, portable-stop containment, atomic reports, semantic config containment, PID reuse, rollback path graphs/collisions/stale files, and release-version consistency.
- Verified real generated MP3 and MP4 files through scan-only, dry-run, apply-safe, apply-all, metadata readback, rename verification, and filename rollback in the build environment.
- Native Windows `cmd.exe`, Norton-on execution, live provider credentials, and user-library mutation remain explicit environment validations rather than claimed evidence.


## v0.5.0 — 2026-07-12 CDT / 2026-07-13 UTC

Deep parameter-alignment and long-run triage release against v2.16.4.

### Critical fixes

- Made `apply-safe` and `apply-all` fail closed before cache, journal, provider initialization, or media mutation when recursive coverage is incomplete.
- Added whole-manifest rollback validation with absolute-path, media-root containment, same-folder, same-extension, collision, size, and identical-path gates.
- Reworked diagnostic staging so reports/state/config/environment are parsed or text-sanitized before packaging; media/project/user/UNC roots and secrets are redacted.
- Replaced runtime online dependency installation with a bundled, exact-version, hash-locked Windows AMD64 wheelhouse for CPython 3.11–3.14 using `--no-index` and `--require-hashes`.

### High stability and recovery

- Added BAT option 14 and owner-token-bound graceful-stop requests; active runs finalize partial evidence and return exit code 75 between bounded operations.
- Added run-exit schema separating verified, not-fully-verified, partial/rushed, skipped/deferred/blocked, actual error/timeout, exact-output, and next-action evidence.
- Added scanner directory/file progress callbacks, owner heartbeat refresh, and stop evidence in recursive coverage reports.
- Added rotating Python logs.
- Added API cache schema/integrity check, corruption quarantine/rebuild, and nonfatal read/write failure behavior.
- Added operation-journal schema/integrity gate and exception-safe connection cleanup.
- Added a total uncompressed-size cap, deterministic budget selection, and minimal fallback ZIP for support diagnostics.
- Closed replaced logging handlers and read-only SQLite connections deterministically.

### Verification and package integrity

- Corrected release test evidence to use pytest rather than zero-test `unittest discover` output.
- Expanded the suite from 57 to 69 tests, including adversarial traversal, rollback, stop-owner, cache/journal, diagnostic-redaction, fallback-export, and control-state cases.
- Added locked dependency/Wheel SHA256 verification and offline resolver checks for Windows AMD64 CPython 3.11, 3.12, 3.13, and 3.14.
- Added v0.5.0 omission coverage, truthful work-window exit, Norton compatibility boundary, and exact-package verification documentation.

### Preserved behavior

- v0.4.1 Windows path/TOML recovery and BAT-direct Python launch.
- v0.4.0 recursive scanner, candidate ranking, version-aware identity, canonicalization, genre mapping, metadata verification, rename verification, journaling, reports, and no-delete model.

## v0.4.1 - 2026-07-11

### Windows path and BAT repair

- Fixed the confirmed drive-root quoting bug where a trailing backslash such as `D:\` could reach Python as `D:\"`.
- Root, rollback-manifest, and config-backup values now use environment transport instead of fragile quoted argv values.
- Reworked the BAT menu into explicit routes for every visible selection and corrected transcript formatting.
- Added guarded Notepad editing with a pre-edit backup and automatic post-save validation.

### Config bootstrap and recovery

- Changed set-root persistence to readable TOML literal path strings, with escaped basic-string fallback for paths containing apostrophes.
- Added full-document parse validation, exact media-root round-trip verification, fsync, timestamped backup, and atomic replacement.
- Added narrow automatic repair for the common invalid `media_root = "D:\Music"` Windows-path syntax.
- Added a safe in-memory fallback config so diagnostics can always export evidence without modifying an invalid config.
- Processing modes fail closed on unrelated config errors; Set media root can rebuild from the shipped example while preserving the invalid original.
- Added config-load/recovery status to preflight, repair, environment summaries, and bounded support diagnostics.
- Manual invalid edits are preserved as rejected copies while the known-good pre-edit config is restored.

### Verification

- Added Windows drive-root, UNC, spaces, apostrophe, trailing-quote, malformed-TOML, diagnostics-fallback, set-root rebuild, and BAT route/label regression tests.
- Preserved the v0.4.0 identity, scanning, reporting, journal, and apply-safety engine unchanged except for the startup/config boundary.

## v0.4.0 - 2026-07-10

### Identity accuracy

- Replaced first-result text trust with multi-candidate MusicBrainz scoring and a configurable runner-up margin.
- Added automatic `apply-safe` blockers for close text, ISRC, and fingerprint candidates.
- Added conservative presentation-noise cleanup plus material version awareness for remix, edit, live, acoustic, instrumental, demo, clean/explicit, mono/stereo, and named remix qualifiers.
- Added a small MusicBrainz video-recording tie-break for video media.
- Added stable identity memory keyed by MBID, ISRC, or fingerprint; ambiguous/conflicted identities are never stored as reusable truth.
- Added a bounded MusicBrainz artist-genre fallback only when stronger genre terms do not map to the required eight buckets.
- Added acoustic duplicate-cluster reporting.

### Apply and batch stability

- Added per-file exception isolation so one damaged file does not terminate a multi-thousand-file run.
- Added source-size/modified-time verification immediately before apply.
- Added metadata readback verification and an `apply-safe` no-rename gate when verification fails.
- Added post-rename source/target/size verification.
- Added case-only canonical spelling support through a short reversible two-step rename.
- Added a durable SQLite operation journal with startup reconciliation of crash-left operations.
- Replaced simple stale-file locking with atomic owner token, PID/host liveness, and heartbeat checks.
- Added run-wide target reservations to prevent dry-run/apply planning collisions.
- Added durable temp-write/fsync/atomic-replace behavior for JSON state and validated set-root TOML updates.

### API and diagnostics

- Added separate connect/read timeouts, true retry-count semantics, transient-only retries, HTTP `Retry-After`, exponential backoff with jitter, and per-provider circuit breakers.
- Added per-provider request/cache/retry/wait/error telemetry.
- Hardened API error redaction.
- Upgraded bounded support diagnostics with lock, operation-journal, API metrics, identity/stability policy, and SQLite runtime/concurrency mitigation evidence.
- Added a warning when MusicBrainz contact information remains a placeholder.

### Verification

- Added regression coverage for candidate ambiguity, named remix selection, video tie-breaking, identity memory safety, source-change guards, apply journaling, metadata verification, stale-lock recovery, case-only rename, set-root path metacharacters, API rate-limit recovery, diagnostics evidence, and artist-genre fallback.
- Automated test count increased from 23 to 44 before final package verification.

## v0.3.0 - 2026-07-10

### Canonical naming and repository verification

- Added stable-ID-first canonicalization for artist, recording, and album spelling.
- MusicBrainz artist entity names now drive the uniform visible artist while printed artist credits are preserved separately.
- Fixed unsafe normalization that could remove periods from stylized names; visible punctuation, diacritics, symbols, and capitalization are now preserved.
- Added Unicode NFC normalization and Unicode-aware comparison-only keys.
- Added Last.fm `track.getInfo` autocorrect/MBID/top-tag cross-check in one cached request.
- Added optional bounded Discogs track/release cross-check.
- Added stable-ID override file for artist MBID, recording MBID, and release-group MBID preferences.
- Added canonicalization statuses/scores, repository agreement/conflict evidence, and apply-safe conflict gating for text-only matches.
- Added source artist credit and MusicBrainz artist IDs to embedded metadata/provenance.
- Added `canonical_name_changes`, `name_variant_clusters`, and `repository_name_conflicts` CSV reports.
- Enhanced repeat-run tag reading, including MP4 freeform suffix detection and persisted MusicBrainz artist IDs.
- Updated diagnostics with canonicalization policy and consistency reports while retaining bounded file and byte limits.
- Added six canonicalization tests; total automated tests increased from 16 to 23.


## v0.2.0 - 2026-07-10

Recursive-coverage and low-friction enrichment release.

Changed:

- Replaced implicit `rglob` discovery with explicit, deterministic, error-isolated recursive traversal.
- Added strict proof fields: directories visited/discovered, deepest media depth, extension/depth counts, limits, exclusions, skipped directory links, access failures, and `all_reachable_subfolders_checked`.
- Removed named directory exclusions from the default configuration so ordinary subfolders are not silently omitted.
- Made file limits, exclusions, unreadable directories, and skipped symlink/junction entries prevent a false complete-scan signal.
- Added scan coverage JSON/CSV, persisted last scan state, and coverage details in console, summary, diagnostics, and HTML output.
- Added embedded MusicBrainz recording-ID and ISRC lookup before fingerprinting/text search.
- Added a signature-aware successful-fingerprint cache to avoid repeated `fpcalc` work on unchanged files.
- Added repeat-run fast skip for bot-managed files already carrying the exact target name and required tags.
- Added exception-only `needs_review.csv` and duplicate-recording candidate reporting without deletion.
- Added partial report creation on user interruption during matching and atomic runtime progress state.
- Preserved detailed processing totals/scan coverage in the terminal runtime state instead of overwriting them with a generic completion record.
- Pinned the two Python runtime dependencies to the versions used for release verification.
- Expanded interoperable MusicBrainz, AcoustID, ISRC, and MediaTaggerBot provenance metadata fields.
- Wired previously passive settings for report formats, naming pattern, Last.fm subgenre preference, MusicBrainz genre preference, subgenre word limit, ampersand replacement, and same-folder validation.
- Added optional project-local tool paths and `tools/README.md`.
- Hardened diagnostics with dynamic version files, last-run/last-scan state, integration registry, collector isolation, a 2 MB candidate cap, prioritized reports, checksum finalization, and bounded integrity enforcement.
- Corrected crossover genre priority so `dance pop` maps to Pop before broad `dance` handling.
- Added recursive, identifier shortcut, repeat-skip, and diagnostics automated tests.

Preserved:

- BAT-direct Python launcher; no PowerShell-script execution.
- Same-folder output, no-delete behavior, dry-run/apply-safe/apply-all gates, collision safety, rollback manifests, API cache, rate limits, retries/backoff, and path repair modes.

## v0.1.4 - 2026-07-09

User-log repair release.

Changed:

- Confirmed from five Windows transcripts that preflight, dry-run, repair, and diagnostics all failed before Python because the unsigned PowerShell launcher was blocked by execution policy.
- Replaced the BAT-to-PowerShell chain with a BAT-direct-to-Python launcher.
- Removed `Launch_MediaTaggerBot.ps1` from the clean package to eliminate the stale/broken runtime path.
- Added Python 3.13/3.12/3.11/3.11+ detection, project-local `.venv` creation, dependency import checks, and clear launcher exit codes.
- Added stale `.venv` detection/rebuild for moved/copied projects while preserving config, logs, state, exports, diagnostics, and media.
- Added launcher state to preflight and diagnostics, including legacy PowerShell presence and execution-policy independence.
- Updated project-root detection to recognize `Start_MediaTaggerBot.bat`.
- Updated README, runbook, transfer brief, known-good state, verification notes, coverage ledger, manifest, and full batch output.

Preserved:

- Matching, genre mapping, metadata writers, reports, rollback, cache, rate limits, retries/backoff, path set/repair modes, and safety thresholds.

## v0.1.3 - 2026-07-09

Timer-safe engineering pass against current v2.16.2 omission-control parameters.

Changed:

- Added `OMISSION_COVERAGE_LEDGER_v0.1.3.md` so broad review/package requests have an explicit item-status ledger instead of silent omissions.
- Added input-assurance reporting for the target media path: recognized -> validated -> normalized -> mapped -> exercised -> confirmed.
- Added input-assurance details to preflight JSON, repair reports, environment summaries, and diagnostic summaries.
- Hardened API exception logging by redacting common API key/token shapes before writing request failure messages.
- Extended diagnostics to include the v0.1.3 coverage ledger when available, while preserving the bounded file cap.
- Updated docs, known-good notes, API notes, transfer brief, manifest, and full batch output for v2.16.2.

Preserved:

- Existing v0.1.2 path/relocation repair behavior.
- Existing matching, genre mapping, metadata writing, report generation, rollback manifest, cache, and apply thresholds.
- Same-folder rename default, no-delete safety model, dry-run/apply-safe/apply-all behavior, and BAT menu flow.

## v0.1.2 - 2026-07-09

Timer-safe engineering pass against v2.15.1 relocation-resilience parameters.

Changed:

- Added relocation/path resilience layer in `src/mediataggerbot/pathing.py`.
- Added `set-root` mode to save a new media folder path into `config\config.toml` with automatic config backup.
- Added `repair` mode for non-destructive project-root, runtime-folder, media-root, and stale-path checks.
- Updated BAT menu with `Set media root` and `Repair/check` options.
- Updated PowerShell launcher to include `set-root`/`repair` and reduce repeated pip-install friction with a dependency-check marker.
- Replaced `-ExecutionPolicy Bypass` in the BAT shim with `RemoteSigned` to avoid normalizing a bypass-style launcher.
- Upgraded diagnostics export to schema v2 with path status, API status, export manifest, diagnostic summary, redacted log tail, deterministic allowlist collection, ZIP integrity test, and bounded cap enforcement.
- Added config settings for stale lock age and progress-log cadence.
- Updated docs, known-good notes, API notes, transfer brief, manifest, and full batch output.

## v0.1.1 - 2026-07-08

- Added `Start_MediaTaggerBot.bat`, a Windows BAT menu shim for users who prefer double-click/Command Prompt workflows.
- Added full batch-output logging under `logs\batch_runs\YYYYMMDD_HHMMSS_<mode>.txt`.
- Added `-NoPause` support to `Launch_MediaTaggerBot.ps1` so BAT runs can complete cleanly while preserving a full transcript.
- Updated README/RUNBOOK/TRANSFER/KNOWN_GOOD/VERIFICATION notes for BAT-first operation.
- Preserved the existing PowerShell launcher and Python bot behavior; this is a launcher/UX patch release.

## v0.1.0 - 2026-07-08

Initial build.

Added:

- Windows-first PowerShell launcher.
- Python package with CLI modes: preflight, scan-only, dry-run, apply-safe, apply-all, diagnostics, rollback.
- Recursive audio/video scanning.
- Existing tag and duration read via Mutagen and optional ffprobe.
- Optional AcoustID fingerprint matching via fpcalc.
- MusicBrainz lookup/search enrichment.
- Optional Last.fm top-tag enrichment.
- Optional Discogs release/style enrichment.
- Strict eight-bucket main genre mapper.
- Optional subgenre selection.
- Safe Windows filename builder for `Artist - Title - Genre - Subgenre.ext`.
- MP3 ID3 metadata writing.
- MP4/M4A/M4V/MOV metadata writing.
- FLAC/Ogg/Opus-style metadata writing where Mutagen supports it.
- Optional ExifTool metadata fallback for video files.
- Sidecar metadata JSON for unsupported embedded writers.
- CSV, JSONL, HTML, summary JSON, rollback manifest reports.
- Bounded, redacted support diagnostics ZIP.
- Single-instance lock.
