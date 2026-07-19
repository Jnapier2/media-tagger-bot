# API and Integration Notes

Integration boundary for MediaTaggerBot v0.5.4. Provider behavior is covered with controlled responses; this repository does not contain live credentials or deterministic copies of third-party responses.

## Integration registry

| Provider/tool | Role | Authentication | Default pacing/timeout | Verification boundary |
|---|---|---|---|---|
| MusicBrainz | Primary recording/artist/release identity and genres | Meaningful User-Agent; no read API key | 1.05 s minimum interval; bounded connect/read timeout | Controlled fixtures + client tests; live user call not claimed |
| AcoustID | Chromaprint-to-MusicBrainz association | Application client key | 0.40 s minimum interval; bounded retries | Controlled fixtures; live key not included |
| Last.fm | Corrected spelling, MBID/top-tag corroboration | Optional API key | 0.25 s minimum interval | Optional; controlled tests only |
| Discogs | Optional release/style corroboration | Optional token | 1.10 s minimum interval | Disabled by default; controlled tests only |
| fpcalc | Preferred Chromaprint generator | None | 120 s bounded subprocess timeout | Capability/fixtures; binary not bundled |
| FFmpeg/ffprobe | Duration/media inspection; Chromaprint fallback when muxer exists | None | Bounded subprocess timeouts | Controlled behavior tests; binary not bundled |
| ExifTool | Optional video metadata fallback | None | Bounded process call when used | Optional; not bundled |

## Caching

- API responses use `state\api_cache.sqlite3` with TTL, schema/integrity checks, and noncritical quarantine/rebuild.
- Fingerprints are cached by full path, size, and mtime.
- Scanner inventory uses `state\inventory_cache.sqlite3`, keyed by full path, size, mtime, and scanner capability signature.
- Stable identity memory stores only strong, nonambiguous identities.

Moved or changed files naturally invalidate file-signature caches. Cache databases are reproducible and are not treated as rollback truth.

## Resilience

Each provider has separate connection/read timeouts, transient-only bounded retries, `Retry-After` support, exponential backoff with jitter, circuit breaker, redacted errors, and telemetry. Optional provider failure degrades to the next evidence source.

## User-Agent and secrets

Set a real `[project].contact`. API keys may be supplied through config or environment variables. Diagnostics show presence/status only and redact common credential/token forms. No real credentials are bundled.

## FFmpeg Chromaprint fallback

`fpcalc` remains preferred. When absent, the bot probes the installed FFmpeg once for a Chromaprint muxer and can fingerprint the first 120 seconds while using ffprobe/duration hints for the whole-file duration required by AcoustID. If neither backend is available, acoustic lookup is skipped and the rest of the matching hierarchy continues.

## Verification boundary

No live provider response is treated as a deterministic release fixture. Candidate ranking, conflicts, retries, rate limits, and circuit breaking are tested with controlled responses. The first Windows dry-run confirms current provider behavior, credentials, network/VPN, and quotas.

## v0.5.4 identifier and circuit policy

ISRC input is normalized and must match the formal 12-character structure before MusicBrainz lookup. A permanent 4xx or item-specific response is recorded but does not increase the transient circuit failure streak. Only connection failures, timeouts, rate limits, and retryable server statuses can open the temporary provider circuit. Circuit-skip logging is bounded to avoid one warning per media file.


## v0.5.4 bounded maintenance research

- SQLite recommends `PRAGMA optimize=0x10002` when opening long-lived connections and `PRAGMA optimize` periodically or before close. MediaTaggerBot applies this only to its local writable cache and journal connections.
- Windows still exposes a 260-character MAX_PATH compatibility boundary unless applications and system policy opt into long paths. The bot therefore budgets generated full paths conservatively rather than requesting a system policy change.
- MusicBrainz Picard recommends clustering and lookup when files are grouped by album. Automatic album-cluster identity promotion was not enabled in this release because the real v0.5.2 evidence requires stronger validation before widening Apply-safe.
