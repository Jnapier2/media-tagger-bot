# Operations

MediaTaggerBot should be run from an extracted, writable project folder. The release identity gate runs before the normal configuration loader. When the gate blocks, preserve the generated identity status and diagnostic ZIP, then restore the full release in a clean folder instead of copying individual files between versions.

Runtime data belongs in project-local ignored folders such as `logs`, `state`, `diagnostics`, `exports`, and `temp`. Do not commit media, credentials, cache databases, journals containing private paths, or support exports.

Copyright © 2026 Gateway Information Group LLC. All rights reserved.
