from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
}
INVALID_FILENAME_CHARS = '<>:"/\\|?*'
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f]")
WHITESPACE_RE = re.compile(r"\s+")
SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s&;]+)"), r"\1<redacted>"),
    (re.compile(r"(?i)(client[_-]?key\s*[=:]\s*)([^\s&;]+)"), r"\1<redacted>"),
    (re.compile(r"(?i)(token\s*[=:]\s*)([^\s&;]+)"), r"\1<redacted>"),
    (re.compile(r"(?i)(Authorization:\s*)([^\r\n]+)"), r"\1<redacted>"),
    (re.compile(r"(?i)(api_key=)[^&\s]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(client=)[^&\s]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(key=)[^&\s]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(Discogs\s+token=)[^\s&;]+"), r"\1<redacted>"),
]


def redact_sensitive_text(text: str) -> str:
    """Redact common API key/token shapes before writing logs or diagnostic artifacts."""
    redacted = str(text)
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def which(name: str) -> str | None:
    return shutil.which(name)


def run_command(args: list[str], timeout: int | float, cwd: Path | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", f"Timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(block_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


@contextmanager
def atomic_text_writer(
    path: Path,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Iterator[TextIO]:
    """Write a text artifact to a same-directory temp file, then atomically publish it.

    Reports can be large and may be interrupted during finalization.  Keeping the
    destination untouched until the writer closes, flushes, and fsyncs prevents a
    half-written CSV/JSONL/HTML file from looking like a completed run artifact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    handle: TextIO | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding=encoding,
            dir=str(path.parent),
            delete=False,
            newline=newline,
        )
        temp_name = handle.name
        try:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
            handle = None
        os.replace(temp_name, path)
        _fsync_directory_best_effort(path.parent)
        temp_name = None
    finally:
        if handle is not None and not handle.closed:
            handle.close()
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def write_text_atomic(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = "\n",
) -> None:
    with atomic_text_writer(path, encoding=encoding, newline=newline) as handle:
        handle.write(text)


def write_json_atomic(path: Path, data: Any) -> None:
    """Durably replace a JSON file without exposing a partially written destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False, newline="\n") as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.replace(temp_name, path)
        _fsync_directory_best_effort(path.parent)
        temp_name = None
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _fsync_directory_best_effort(directory: Path) -> None:
    """Persist a rename where the platform exposes directory fsync; harmless elsewhere."""
    flags = getattr(os, "O_RDONLY", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(directory), flags)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def sanitize_component(value: str, slash_replacement: str = "-", collapse_whitespace: bool = True) -> str:
    value = str(value or "").strip()
    value = value.replace("/", slash_replacement).replace("\\", slash_replacement)
    trans = str.maketrans({ch: "-" for ch in INVALID_FILENAME_CHARS if ch not in "/\\"})
    value = value.translate(trans)
    value = CONTROL_CHARS_RE.sub("", value)
    if collapse_whitespace:
        value = WHITESPACE_RE.sub(" ", value)
    # Remove edge punctuation produced solely by replacing Windows-invalid
    # characters (for example "Is This Love?" must not become "Is This Love-").
    value = value.strip(" .-_")
    if not value:
        value = "Unknown"
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"{value}_"
    return value


def truncate_filename_stem(stem: str, extension: str, max_length: int) -> str:
    if len(stem) + len(extension) <= max_length:
        return stem
    allowed = max(20, max_length - len(extension))
    return stem[:allowed].rstrip(" .")


def compact_list(values: Iterable[Any], limit: int = 12) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def normalize_display_text(value: str | None, unicode_form: str = "NFC") -> str:
    """Normalize technical text noise without changing intentional spelling or style.

    Repository spellings such as ``t.A.T.u.``, ``P!nk`` and ``...Baby One More
    Time`` must keep their punctuation.  This function therefore performs only
    Unicode normalization, control/zero-width cleanup and whitespace folding.
    It deliberately does *not* title-case, transliterate, strip diacritics, or
    replace punctuation.
    """
    if value is None:
        return ""
    text = unicodedata.normalize(unicode_form, str(value))
    cleaned: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("Z") or category == "Cc":
            # Convert line breaks/control separators to a space rather than joining words.
            cleaned.append(" ")
        elif char in {"\u200b", "\ufeff"}:
            # Remove zero-width space/BOM, but preserve ZWNJ/ZWJ because they can
            # be meaningful in scripts and emoji artist names.
            continue
        else:
            cleaned.append(char)
    return WHITESPACE_RE.sub(" ", "".join(cleaned)).strip()



def normalize_joinphrase(value: str | None, unicode_form: str = "NFC") -> str:
    """Normalize a MusicBrainz-style join phrase while preserving edge spaces."""
    if value is None:
        return ""
    raw = str(value)
    core = normalize_display_text(raw, unicode_form=unicode_form)
    if not core:
        return ""
    leading = " " if raw[:1].isspace() else ""
    trailing = " " if raw[-1:].isspace() else ""
    return leading + core + trailing

def normalize_text(value: str | None) -> str:
    """Backward-compatible name for display-safe normalization."""
    return normalize_display_text(value)


def comparison_key(value: str | None) -> str:
    """Create an accent/punctuation-insensitive key for repository comparison.

    This is only for matching and consensus checks.  It is never written back
    to metadata or used as the visible filename spelling.
    """
    text = normalize_display_text(value, unicode_form="NFKD").casefold()
    text = text.replace("&", " and ")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    pieces: list[str] = []
    for ch in text:
        if ch.isalnum():
            pieces.append(ch)
        elif ch.isspace():
            pieces.append(" ")
        # Punctuation/symbols are comparison-only noise and are removed.
    return WHITESPACE_RE.sub(" ", "".join(pieces)).strip()


def text_similarity(left: str | None, right: str | None) -> float:
    """Return 0..1 similarity using comparison-only normalized keys."""
    left_key = comparison_key(left)
    right_key = comparison_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    return SequenceMatcher(None, left_key, right_key).ratio()


def safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except Exception:
        return str(path)
