# Operations Guide

## Operating model

The repository folder is the project root. Configuration, logs, state, temporary files, reports, and diagnostics remain under that root; `paths.media_root` is the only expected external data binding.

Use this progression for every new library or configuration:

```text
Repair/check -> Set media root -> Preflight -> Scan-only -> Dry-run -> Apply-safe sample
```

A full apply requires proof that every reachable subfolder was checked. File limits, exclusions, access failures, skipped links, and graceful stops make traversal partial and block mutation.

## Identity and apply policy

Matching favors trusted embedded MusicBrainz recording IDs or valid ISRCs, then AcoustID/Chromaprint, then multi-candidate MusicBrainz text search. Optional Last.fm and Discogs lookups provide corroboration or enrichment.

`apply-safe` requires strong artist/title agreement, acceptable duration evidence when available, independent corroboration, no ambiguity blocker, and a non-fallback genre. A high text-search score alone is not authoritative.

## Apply transaction

```text
complete-scan gate
    -> journal planned operation
    -> confirm source size and modification time
    -> write embedded metadata
    -> read back and verify metadata
    -> rename
    -> verify target
    -> finalize journal and rollback record
```

Supported formats must pass embedded-write verification before `apply-safe` can rename them. A sidecar alone is not considered success. The application may temporarily clear a file’s read-only attribute for one bounded retry and then restores it; it does not change ACLs, ownership, endpoint protection, or execution policy.

## Reports and recovery

Review the HTML summary and the `needs_review`, prior-identity, repository-conflict, rollback, and scan-coverage reports generated under `exports/`.

Rollback validates the full manifest before moving any file and restores filenames only. It cannot recover overwritten tags. Preserve independent backups before applying changes.

Diagnostics creates an allowlisted, redacted archive with a bounded entry and byte budget. It excludes media, virtual environments, caches, prior diagnostic archives, and active staging.

## Optional tools

`fpcalc` provides the preferred acoustic fingerprint. FFmpeg/`ffprobe` improves duration and container inspection. ExifTool expands video metadata support. The application reports missing capabilities during preflight and does not silently download these tools.
