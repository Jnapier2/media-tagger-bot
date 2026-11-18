# Release notes

## Unpublished startup-evidence candidate — 2026-09-18

Build `MTB-0.5.9-PUBLIC-20260918-BOOTPRIV1` changes the minimal startup-failure support export, not media matching, tagging, renaming, review or rollback behavior. Export entries summarize cached evidence and do not include original controls, logs or arbitrary error text. The verifier's Requests expectation is aligned with the unchanged 2.33.0 lock.

The complete candidate must be evaluated separately; it is not a published or Windows-qualified release. Atomic no-overwrite export requires filesystem hard-link support. Unsupported filesystems fail closed. Raw local logs/status stay private. Native Windows, exact declared dependencies, endpoint-security testing and independent review are not established by local source tests.

## Source maintenance - 2026-09-08

Checkout metadata uses explicit line endings so release integrity checks agree across Git configurations. Test helpers use isolated temporary project roots to keep repeat runs independent. Source provenance retains the public version and artifact identity.

MediaTaggerBot 0.5.9 strengthens the safeguards around automated media organization:

- write modes require a complete recursive scan;
- proposed changes remain reviewable before application;
- journaled operations verify metadata writes and file renames;
- exact target collisions are routed to human review; and
- the managed release is verified before configuration or credentials are loaded.

The public repository excludes credentials, user media, runtime databases, logs, and support exports.

- the release keeps a stable canonical entrypoint, a launcher-derived project root, project-local outputs, and cross-working-directory regression coverage; and
- release metadata now labels the source baseline as a Git commit SHA-1 instead of misidentifying it as SHA-256.

Copyright © 2026 Gateway Information Group LLC. All rights reserved.
