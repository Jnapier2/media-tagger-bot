# Optional portable media tools

MediaTaggerBot automatically adds these project-local locations to `PATH` when launched from the BAT menu:

- `tools\chromaprint\fpcalc.exe`
- `tools\ffmpeg\bin\ffmpeg.exe`
- `tools\ffmpeg\bin\ffprobe.exe`
- `tools\exiftool\exiftool.exe`

You may instead install the tools normally on Windows. No executable is bundled here, and the bot does not download or silently update third-party binaries.

`fpcalc` enables AcoustID fingerprinting. `ffprobe` supplies duration for difficult media containers. `exiftool` expands embedded metadata support for video containers. Preflight reports which executables are actually found.
