# Security Policy

## Supported code

Security fixes are considered for the latest revision on the default branch. Historical release notes are retained for context but do not represent separately supported versions.

## Reporting a vulnerability

Use GitHub’s private vulnerability-reporting or security-advisory feature for issues that could expose credentials, local paths, media metadata, or enable unintended file mutation. If private reporting is unavailable, contact the repository owner privately through their GitHub profile before opening a public issue.

Include the affected component, a minimal reproduction, expected and observed behavior, and impact. Redact API tokens, personal paths, logs, diagnostics, and media samples. Do not attach third-party media or real credentials.

Routine defects without sensitive details may be reported in a public issue.

## Operator responsibilities

- Keep `config/config.toml`, environment files, logs, state databases, reports, diagnostics, and media out of commits.
- Test with copies and review dry-run evidence before using an apply mode.
- Treat rollback manifests as filename recovery only; preserve independent media backups.
- Obtain API credentials directly from providers and follow their terms and rate limits.
- Do not weaken endpoint protection or system security policy to run the project.
