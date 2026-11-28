"""Table-driven tests for cross-platform export filename formatting and sanitization."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

import pytest

from autoclipper.export.filenames import (
    WINDOWS_RESERVED_NAMES,
    build_archive_filename,
    build_clip_filename,
    build_highlights_filename,
    deduplicate_filename,
    format_offset_hms,
    format_vod_date,
    sanitize_filename,
    sanitize_filename_component,
)


@pytest.mark.parametrize(
    ("raw", "fallback", "expected"),
    [
        pytest.param("user_001", "vod", "user_001", id="plain-ascii"),
        pytest.param(
            "Loud audio & laughing chat", "rank-1", "Loud-audio-laughing-chat", id="spaces-and-amp"
        ),
        pytest.param('bad<>:"/\\|?*chars', "clip", "bad-chars", id="illegal-chars"),
        pytest.param("CON", "clip", "_CON", id="windows-con"),
        pytest.param("nul", "clip", "_nul", id="windows-nul-lower"),
        pytest.param("COM1", "clip", "_COM1", id="windows-com1"),
        pytest.param("LPT9", "clip", "_LPT9", id="windows-lpt9"),
        pytest.param("COM\u00b9", "clip", "_COM1", id="nfkc-superscript-reserved"),
        pytest.param("   ...---___   ", "rank-3", "rank-3", id="only-separators-uses-fallback"),
        pytest.param("", "rank-2", "rank-2", id="empty-uses-fallback"),
        pytest.param("Café stream!", "vod", "Café-stream", id="unicode-preserved"),
        pytest.param("加藤純一", "vod", "加藤純一", id="cjk-preserved"),
        pytest.param("e\u0301clair", "clip", "éclair", id="nfd-normalized-to-nfc"),
        pytest.param("\u202eevil\x00name\x1f", "clip", "evil-name", id="bidi-and-control-stripped"),
        pytest.param("../../etc/passwd", "clip", "etc-passwd", id="path-traversal-stripped"),
        pytest.param("trailing. . ", "clip", "trailing", id="trailing-dots-spaces"),
        pytest.param("🎮🔥", "rank-4", "rank-4", id="symbols-only-falls-back"),
    ],
)
def test_sanitize_filename_component(raw: str, fallback: str, expected: str) -> None:
    assert sanitize_filename_component(raw, fallback=fallback) == expected


@pytest.mark.parametrize("reserved", sorted(WINDOWS_RESERVED_NAMES))
def test_all_windows_reserved_names_are_guarded(reserved: str) -> None:
    sanitized = sanitize_filename_component(reserved, fallback="clip")
    assert sanitized.casefold() not in WINDOWS_RESERVED_NAMES
    assert sanitized.startswith("_")


def test_sanitize_filename_component_enforces_utf8_byte_limit() -> None:
    # 3-byte UTF-8 characters: 40 chars = 120 bytes, cap at 30 bytes must split on boundary.
    long_cjk = "あ" * 40
    result = sanitize_filename_component(long_cjk, fallback="clip", max_bytes=30)
    assert len(result.encode("utf-8")) <= 30
    assert result == "あ" * 10


def test_sanitize_filename_appends_mp4_and_respects_max_bytes() -> None:
    long_stem = "a" * 300
    name = sanitize_filename(long_stem, extension=".mp4", max_bytes=120)
    assert name.endswith(".mp4")
    assert len(name.encode("utf-8")) <= 120


def test_format_vod_date_converts_to_utc() -> None:
    # 2026-10-13 02:30:00+05:00 is 2026-10-12 21:30:00 UTC.
    local_dt = datetime(2026, 10, 13, 2, 30, tzinfo=timezone(timedelta(hours=5)))
    assert format_vod_date(local_dt) == "2026-10-12"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00-00-00"),
        (40.9, "00-00-40"),
        (118.0, "00-01-58"),
        (3661.2, "01-01-01"),
        (-5.0, "00-00-00"),
        (float("nan"), "00-00-00"),
    ],
)
def test_format_offset_hms(seconds: float, expected: str) -> None:
    assert format_offset_hms(seconds) == expected


def test_build_clip_filename_uses_moment_title_or_falls_back_to_rank() -> None:
    ts = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
    titled = build_clip_filename(
        channel="User 001",
        created_at=ts,
        title="Loud audio & laughing chat",
        rank=1,
        start_s=118.0,
        fallback_id="9000000001",
    )
    assert titled == "User-001_2026-10-12_Loud-audio-laughing-chat_00-01-58.mp4"

    untitled = build_clip_filename(
        channel=None,
        created_at=None,
        title=None,
        rank=3,
        start_s=40.0,
        fallback_id="9000000001",
    )
    assert untitled == "9000000001_unknown-date_rank-3_00-00-40.mp4"


def test_build_clip_filename_respects_max_bytes_with_unicode() -> None:
    ts = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
    name = build_clip_filename(
        channel="日本語チャンネル" * 5,
        created_at=ts,
        title="とても長いタイトル" * 10,
        rank=1,
        start_s=3661.0,
        fallback_id="9000000001",
        max_bytes=120,
    )
    assert name.endswith(".mp4")
    assert len(name.encode("utf-8")) <= 120


def test_build_highlights_filename() -> None:
    ts = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
    assert (
        build_highlights_filename(channel="User_001", created_at=ts, fallback_id="9000000001")
        == "User_001_2026-10-12_highlights.mp4"
    )
    assert (
        build_highlights_filename(channel="CON", created_at=None, fallback_id="9000000001")
        == "_CON_unknown-date_highlights.mp4"
    )
    bounded = build_highlights_filename(
        channel="ExtremelyLongStreamerName",
        created_at=ts,
        fallback_id="9000000001",
        max_bytes=32,
    )
    assert bounded.endswith("_2026-10-12_highlights.mp4")
    assert len(bounded.encode("utf-8")) <= 32
    with pytest.raises(ValueError, match="no room for the channel"):
        build_highlights_filename(channel="User", created_at=ts, fallback_id="vod", max_bytes=25)


@pytest.mark.parametrize("kind", ["clips", "highlights"])
def test_build_archive_filename_names_the_vod_and_kind(
    kind: Literal["clips", "highlights"],
) -> None:
    ts = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
    assert (
        build_archive_filename(
            channel="User 001", created_at=ts, fallback_id="9000000001", kind=kind
        )
        == f"User-001_2026-10-12_{kind}.zip"
    )
    assert (
        build_archive_filename(channel=None, created_at=None, fallback_id="9000000001", kind=kind)
        == f"9000000001_unknown-date_{kind}.zip"
    )


def test_sanitize_normalizes_combining_marks_after_stripping_invisible() -> None:
    assert sanitize_filename_component("e\u200b\u0301clair") == "éclair"
    assert sanitize_filename_component("e\u034f\ufe0f\u0301clair") == "éclair"


def test_deduplicate_filename_is_case_insensitive_and_bounded() -> None:
    used: set[str] = set()
    first = deduplicate_filename("User_2026-10-12_Clip_00-01-00.mp4", used)
    second = deduplicate_filename("user_2026-10-12_clip_00-01-00.mp4", used)
    third = deduplicate_filename("User_2026-10-12_Clip_00-01-00.mp4", used)

    assert first == "User_2026-10-12_Clip_00-01-00.mp4"
    assert second == "user_2026-10-12_clip_00-01-00_2.mp4"
    assert third == "User_2026-10-12_Clip_00-01-00_3.mp4"

    # Unicode canonical equivalence (NFC vs NFD) and casefold collisions (Greek sigma).
    used_unicode: set[str] = set()
    assert deduplicate_filename("e\u0301clair.mp4", used_unicode) == "e\u0301clair.mp4"
    assert deduplicate_filename("\u00e9clair.mp4", used_unicode) == "\u00e9clair_2.mp4"
    assert deduplicate_filename("\u03a3.mp4", used_unicode) == "\u03a3.mp4"
    assert deduplicate_filename("\u03c2.mp4", used_unicode) == "\u03c2_2.mp4"
    assert deduplicate_filename("\u03c3.mp4", used_unicode) == "\u03c3_3.mp4"
    assert deduplicate_filename("ᾀ.mp4", used_unicode) == "ᾀ.mp4"
    assert deduplicate_filename("ἀι.mp4", used_unicode) == "ἀι_2.mp4"
    assert deduplicate_filename("ẞ.mp4", used_unicode) == "ẞ.mp4"
    assert deduplicate_filename("ss.mp4", used_unicode) == "ss_2.mp4"
    assert deduplicate_filename("I.mp4", used_unicode) == "I.mp4"
    assert deduplicate_filename("\u0131.mp4", used_unicode) == "\u0131.mp4"
    assert deduplicate_filename("\u13a0.mp4", used_unicode) == "\u13a0.mp4"
    assert deduplicate_filename("ꭰ.mp4", used_unicode) == "ꭰ_2.mp4"

    # Bounded length when suffix is appended.
    used_short: set[str] = set()
    base = sanitize_filename("a" * 30, max_bytes=20)
    a = deduplicate_filename(base, used_short, max_bytes=20)
    b = deduplicate_filename(base, used_short, max_bytes=20)
    assert len(a.encode("utf-8")) <= 20
    assert len(b.encode("utf-8")) <= 20
    assert a != b
