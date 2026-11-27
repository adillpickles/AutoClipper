"""Clean, cross-platform filename formatting and sanitization for exports and highlights.

Produces human-readable filenames safe on Windows, macOS, and Linux:
- Vertical clip exports: ``<channel>_<YYYY-MM-DD>_<moment>_<hh-mm-ss>.mp4``
- Highlights reels: ``<channel>_<YYYY-MM-DD>_highlights.mp4``

Internal artifact files remain keyed by opaque render ID (``exp_<hex>.mp4``,
``hl_<hex>.mp4``); these helpers format the download (``Content-Disposition``), zip archive,
and output-folder copy names.
"""

import math
import re
import unicodedata
from datetime import UTC, datetime
from typing import Final

# Device names Windows rejects with or without an extension (case-insensitive).
WINDOWS_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

DEFAULT_MAX_FILENAME_BYTES: Final = 200
_STRIP_CATEGORIES: Final[frozenset[str]] = frozenset({"Cf", "Cs", "Co", "Cn"})
_IGNORABLE_LETTERS_AND_MARKS: Final = re.compile(
    r"[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180d\u180f\ufe00-\ufe0f"
    r"\U000e0100-\U000e01ef]"
)
_MULTI_HYPHEN_RE: Final = re.compile(r"-{2,}")


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate `text` so its UTF-8 encoding fits within `max_bytes` on a codepoint boundary."""
    if max_bytes <= 0 or not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes]
    while cut:
        try:
            decoded = cut.decode("utf-8")
            break
        except UnicodeDecodeError:
            cut = cut[:-1]
    else:
        return ""
    # Do not leave a separator at either edge of a truncated component.
    return decoded.strip(" .-_")


def _guard_reserved(stem: str, *, max_bytes: int) -> str:
    """Prefix `_` when the base name (before first `.`) is a Windows reserved device name."""
    base = stem.split(".", 1)[0].casefold()
    if base not in WINDOWS_RESERVED_NAMES:
        return stem
    prefixed = f"_{stem}"
    if len(prefixed.encode("utf-8")) <= max_bytes:
        return prefixed
    trimmed = _truncate_utf8(stem, max(1, max_bytes - 1))
    return f"_{trimmed}" if trimmed else "_clip"


def sanitize_filename_component(
    text: str,
    *,
    fallback: str = "clip",
    max_bytes: int = 80,
) -> str:
    """Sanitize one filename component for Windows, macOS, and Linux.

    Normalizes Unicode with NFKC, strips invisible formatting characters, and keeps letters,
    numbers, combining marks, hyphens, and underscores. Controls and illegal path characters
    become separators. Guards Windows device names and enforces a UTF-8 byte length limit.
    """
    normalized = unicodedata.normalize("NFKC", text)
    chars: list[str] = []
    for ch in normalized:
        category = unicodedata.category(ch)
        if category in _STRIP_CATEGORIES or _IGNORABLE_LETTERS_AND_MARKS.fullmatch(ch):
            continue
        major = category[0]
        if ch in ("-", "_") or major in ("L", "N"):
            chars.append(ch)
        elif major == "M":
            # Keep combining marks only after a non-separator base character.
            if chars and chars[-1] != "-":
                chars.append(ch)
        else:
            chars.append("-")

    collapsed = _MULTI_HYPHEN_RE.sub("-", "".join(chars)).strip(" .-_")
    collapsed = unicodedata.normalize("NFC", collapsed)
    if not collapsed:
        safe_fallback = fallback.strip(" .-_") or "clip"
        collapsed = (
            safe_fallback
            if safe_fallback == "clip"
            else sanitize_filename_component(safe_fallback, fallback="clip", max_bytes=max_bytes)
        )

    bounded = _truncate_utf8(collapsed, max_bytes)
    if not bounded:
        bounded = _truncate_utf8(fallback.strip(" .-_") or "clip", max_bytes) or "clip"
    return _guard_reserved(bounded, max_bytes=max_bytes)


def sanitize_filename(
    stem: str,
    *,
    extension: str = ".mp4",
    max_bytes: int = DEFAULT_MAX_FILENAME_BYTES,
) -> str:
    """Sanitize a complete filename stem and append `extension` within `max_bytes` UTF-8 bytes."""
    ext = extension if extension.startswith(".") else f".{extension}"
    ext_bytes = len(ext.encode("utf-8"))
    stem_budget = max(1, max_bytes - ext_bytes)
    safe_stem = sanitize_filename_component(stem, fallback="clip", max_bytes=stem_budget)
    return f"{safe_stem}{ext}"


def format_vod_date(created_at: datetime) -> str:
    """Format a timezone-aware timestamp as UTC ``YYYY-MM-DD``."""
    utc_dt = (
        created_at.replace(tzinfo=UTC) if created_at.tzinfo is None else created_at.astimezone(UTC)
    )
    return utc_dt.strftime("%Y-%m-%d")


def format_offset_hms(seconds: float) -> str:
    """Format a non-negative VOD offset in seconds as ``HH-MM-SS``."""
    total = 0 if not math.isfinite(seconds) or seconds < 0 else math.floor(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}-{minutes:02d}-{secs:02d}"


def build_clip_filename(
    *,
    channel: str | None,
    created_at: datetime | None,
    title: str | None,
    rank: int | None,
    start_s: float,
    fallback_id: str,
    max_bytes: int = DEFAULT_MAX_FILENAME_BYTES,
) -> str:
    """Build ``<channel>_<YYYY-MM-DD>_<moment title or rank>_<hh-mm-ss>.mp4``."""
    id_fallback = sanitize_filename_component(fallback_id, fallback="vod", max_bytes=48)
    channel_part = sanitize_filename_component(channel or "", fallback=id_fallback, max_bytes=48)
    date_part = format_vod_date(created_at) if created_at is not None else "unknown-date"
    moment_fallback = f"rank-{rank}" if rank is not None else "clip"
    moment_part = sanitize_filename_component(title or "", fallback=moment_fallback, max_bytes=72)
    time_part = format_offset_hms(start_s)

    ext = ".mp4"
    stem_budget = max(1, max_bytes - len(ext.encode("utf-8")))
    fixed_bytes = len(f"{channel_part}_{date_part}__{time_part}".encode())
    remaining_for_moment = stem_budget - fixed_bytes
    if remaining_for_moment < len(moment_part.encode("utf-8")):
        if remaining_for_moment >= 4:
            moment_part = (
                _truncate_utf8(moment_part, remaining_for_moment)
                or _truncate_utf8(moment_fallback, remaining_for_moment)
                or "clip"
            )
        else:
            # Extremely tight budget: truncate the whole stem cleanly.
            raw_stem = f"{channel_part}_{date_part}_{moment_part}_{time_part}"
            bounded = _guard_reserved(
                _truncate_utf8(raw_stem, stem_budget) or "clip", max_bytes=stem_budget
            )
            return f"{bounded}{ext}"

    stem = _guard_reserved(
        f"{channel_part}_{date_part}_{moment_part}_{time_part}",
        max_bytes=stem_budget,
    )
    return f"{stem}{ext}"


def build_highlights_filename(
    *,
    channel: str | None,
    created_at: datetime | None,
    fallback_id: str,
    max_bytes: int = DEFAULT_MAX_FILENAME_BYTES,
) -> str:
    """Build ``<channel>_<YYYY-MM-DD>_highlights.mp4`` within the byte budget."""
    id_fallback = sanitize_filename_component(fallback_id, fallback="vod", max_bytes=48)
    date_part = format_vod_date(created_at) if created_at is not None else "unknown-date"
    suffix = f"_{date_part}_highlights.mp4"
    channel_budget = max_bytes - len(suffix.encode("utf-8"))
    if channel_budget < 1:
        raise ValueError("filename byte limit leaves no room for the channel")
    channel_part = sanitize_filename_component(channel or "", fallback=id_fallback, max_bytes=48)
    bounded = _truncate_utf8(channel_part, channel_budget) or "v"
    return f"{bounded}{suffix}"


def collision_key(filename: str) -> str:
    """Return the normalized case-insensitive key used for cross-platform collision checks."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", filename).casefold())


def deduplicate_filename(
    filename: str,
    used_casefolded: set[str],
    *,
    max_bytes: int = DEFAULT_MAX_FILENAME_BYTES,
) -> str:
    """Return a case-insensitively unique filename, appending ``_2``, ``_3``, ... on collision.

    Mutates `used_casefolded` to claim the returned filename's normalized case-folded form.
    """
    key = collision_key(filename)
    if key not in used_casefolded:
        used_casefolded.add(key)
        return filename

    dot = filename.rfind(".")
    if dot > 0:
        stem, ext = filename[:dot], filename[dot:]
    else:
        stem, ext = filename, ""
    ext_bytes = len(ext.encode("utf-8"))

    counter = 2
    while True:
        suffix = f"_{counter}"
        budget = max(1, max_bytes - ext_bytes - len(suffix.encode("utf-8")))
        trimmed = _truncate_utf8(stem, budget) or "clip"
        trimmed = _guard_reserved(trimmed, max_bytes=budget)
        candidate = f"{trimmed}{suffix}{ext}"
        cand_key = collision_key(candidate)
        if cand_key not in used_casefolded:
            used_casefolded.add(cand_key)
            return candidate
        counter += 1
