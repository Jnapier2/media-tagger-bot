# Release notes

MediaTaggerBot 0.5.9 strengthens the safeguards around automated media organization:

- write modes require a complete recursive scan;
- proposed changes remain reviewable before application;
- journaled operations verify metadata writes and file renames;
- exact target collisions are routed to human review; and
- the managed release is verified before configuration or credentials are loaded.

The public repository excludes credentials, user media, runtime databases, logs, and support exports.

- the v2.17.6 alignment keeps a stable canonical entrypoint, a launcher-derived project root, project-local outputs, and cross-working-directory regression coverage; and
- release metadata now labels the source baseline as a Git commit SHA-1 instead of misidentifying it as SHA-256.

Copyright © 2026 Gateway Information Group LLC. All rights reserved.
