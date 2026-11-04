"""Tests for chat features per window (docs/ml/features.md § Chat features)."""

import json
import math
from pathlib import Path

import polars as pl
import pytest

from autoclipper.features.chat import (
    FEATURE_COLUMNS,
    Lexicon,
    LexiconError,
    compute_chat_features,
    load_lexicon,
)
from autoclipper.labels.windows import make_windows, windows_for_vods
from autoclipper.synthetic import generate_dataset

LEXICON_PATH = Path(__file__).parents[2] / "configs" / "lexicon.yaml"


@pytest.fixture(scope="module")
def lexicon() -> Lexicon:
    return load_lexicon(LEXICON_PATH)


def messages(*rows: tuple[float, str, str], video_id: str = "v", bits: int = 0) -> pl.DataFrame:
    """Messages as (offset_s, user_hash, text); emotes counted as KEKW/Pog tokens."""
    return pl.DataFrame(
        {
            "video_id": [video_id] * len(rows),
            "offset_s": [r[0] for r in rows],
            "user_hash": [r[1] for r in rows],
            "text": [r[2] for r in rows],
            "n_emotes": [sum(t in {"KEKW", "Pog"} for t in r[2].split()) for r in rows],
            "bits": [bits] * len(rows),
        },
        schema={
            "video_id": pl.String,
            "offset_s": pl.Float64,
            "user_hash": pl.String,
            "text": pl.String,
            "n_emotes": pl.Int64,
            "bits": pl.Int64,
        },
    )


def features(lexicon: Lexicon, duration_s: float, *rows: tuple[float, str, str]) -> pl.DataFrame:
    return compute_chat_features(make_windows("v", duration_s), messages(*rows), lexicon=lexicon)


def column(frame: pl.DataFrame, name: str) -> list[float]:
    return frame[name].to_list()


def test_output_has_one_float_row_per_window_and_a_feature_version(lexicon: Lexicon) -> None:
    frame = features(lexicon, 30.0, (1.0, "a", "hi"))

    assert frame.columns == ["window_id", *FEATURE_COLUMNS, "feature_version"]
    assert frame["window_id"].to_list() == ["v:0", "v:1", "v:2"]
    assert all(frame.schema[c] == pl.Float64 for c in FEATURE_COLUMNS)
    assert frame["feature_version"].unique().to_list() == ["chat-v1+lexicon-v1"]


def test_rates_are_per_second_of_the_window(lexicon: Lexicon) -> None:
    frame = features(
        lexicon,
        20.0,
        *[(float(i), "a" if i % 2 else "b", "hello") for i in range(5)],  # 5 messages, 2 chatters
    )

    assert column(frame, "chat_msg_rate") == [0.5, 0.0]
    assert column(frame, "chat_unique_rate") == [0.2, 0.0]


def test_a_short_final_window_divides_by_its_own_length(lexicon: Lexicon) -> None:
    frame = features(lexicon, 16.0, (12.0, "a", "x"), (13.0, "b", "y"), (14.0, "c", "z"))

    assert column(frame, "chat_msg_rate") == [0.0, pytest.approx(0.5)]  # 3 messages in 6 s


def test_empty_chat_gives_zeros_not_nulls(lexicon: Lexicon) -> None:
    frame = features(lexicon, 40.0)

    assert frame.height == 4
    for name in FEATURE_COLUMNS:
        assert frame[name].null_count() == 0
        assert column(frame, name) == [0.0] * 4


def test_a_message_on_a_window_boundary_belongs_to_the_later_window(lexicon: Lexicon) -> None:
    frame = features(lexicon, 30.0, (9.999, "a", "x"), (10.0, "b", "y"), (20.0, "c", "z"))

    assert column(frame, "chat_msg_rate") == [0.1, 0.1, 0.1]


def test_messages_after_the_last_window_are_not_counted(lexicon: Lexicon) -> None:
    # The 4 s tail of a 24 s VOD has no window (shorter than half a window).
    frame = features(lexicon, 24.0, (21.0, "a", "x"), (25.0, "a", "y"), (-1.0, "a", "z"))

    assert column(frame, "chat_msg_rate") == [0.0, 0.0]


def test_a_vod_where_every_window_is_the_same_scores_zero(lexicon: Lexicon) -> None:
    rows = [(10.0 * w + s, f"u{s}", "same") for w in range(6) for s in range(3)]
    frame = features(lexicon, 60.0, *rows)

    assert column(frame, "chat_msg_rate") == [0.3] * 6
    for name in ("chat_msg_rate_z", "chat_unique_rate_z", "chat_msg_rate_rz"):
        assert column(frame, name) == [0.0] * 6
    for k in (1, 2, 3):
        assert column(frame, f"chat_msg_rate_lead{k}") == [0.0] * 6


def test_z_score_uses_the_vods_mean_and_population_std(lexicon: Lexicon) -> None:
    # Rates 0, 0, 0.3: mean 0.1, population std sqrt(0.02).
    frame = features(lexicon, 30.0, (20.0, "a", "x"), (21.0, "b", "x"), (22.0, "c", "x"))

    std = math.sqrt(0.02)
    assert column(frame, "chat_msg_rate_z") == [
        pytest.approx(-0.1 / std),
        pytest.approx(-0.1 / std),
        pytest.approx(0.2 / std),
    ]


def test_rolling_z_compares_with_the_previous_windows_only(lexicon: Lexicon) -> None:
    # Rates 0.1, 0.2, 0.3, 1.0. Window 2 is compared with 0.1 and 0.2 (mean 0.15, std 0.05).
    counts = [1, 2, 3, 10]
    rows = [(10.0 * w + i * 0.5, f"u{i}", f"m{i}") for w, n in enumerate(counts) for i in range(n)]
    frame = features(lexicon, 40.0, *rows)

    rz = column(frame, "chat_msg_rate_rz")
    assert rz[:2] == [0.0, 0.0]  # fewer than two earlier windows
    assert rz[2] == pytest.approx((0.3 - 0.15) / 0.05)
    previous = [0.1, 0.2, 0.3]
    mean = sum(previous) / 3
    std = math.sqrt(sum((p - mean) ** 2 for p in previous) / 3)
    assert rz[3] == pytest.approx((1.0 - mean) / std)


def test_rolling_z_looks_back_30_windows_and_not_past_the_vod_start(lexicon: Lexicon) -> None:
    # 40 windows: 1 message each, except 10 messages in windows 0-4 and in window 39.
    counts = [10] * 5 + [1] * 34 + [10]
    rows = [(10.0 * w + i * 0.1, f"u{i}", "x") for w, n in enumerate(counts) for i in range(n)]
    windows = pl.concat([make_windows("a", 400.0), make_windows("b", 30.0)])
    chat = pl.concat([messages(*rows, video_id="a"), messages((25.0, "u", "x"), video_id="b")])

    frame = compute_chat_features(windows, chat, lexicon=lexicon)

    rz = frame.filter(pl.col("window_id").str.starts_with("a:"))["chat_msg_rate_rz"].to_list()
    # Window 39 sees windows 9-38 only: all 1 message, zero std, so 0 (an expanding baseline
    # would include the busy start and give a large value).
    assert rz[39] == 0.0
    # Window 34 sees windows 4-33: one busy window and 29 quiet ones.
    previous = counts[4:34]
    mean = sum(previous) / 30 / 10
    std = math.sqrt(sum((c / 10 - mean) ** 2 for c in previous) / 30)
    assert rz[34] == pytest.approx((0.1 - mean) / std)
    # VOD b starts fresh: its first windows have no earlier windows of their own.
    assert frame.filter(pl.col("window_id").str.starts_with("b:"))["chat_msg_rate_rz"].to_list()[
        :2
    ] == [0.0, 0.0]


def test_unsorted_interleaved_messages_give_the_same_features(lexicon: Lexicon) -> None:
    windows = pl.concat([make_windows("a", 30.0), make_windows("b", 30.0)])
    chat = pl.concat(
        [
            messages(*[(i * 1.7 % 30, f"u{i % 4}", "KEKW") for i in range(40)], video_id="a"),
            messages(*[(i * 2.3 % 30, f"u{i % 3}", "W") for i in range(25)], video_id="b"),
        ]
    )

    ordered = compute_chat_features(windows, chat.sort(["video_id", "offset_s"]), lexicon=lexicon)
    shuffled = compute_chat_features(
        windows.reverse(), chat.sample(fraction=1.0, shuffle=True, seed=7), lexicon=lexicon
    )

    assert ordered.equals(shuffled)


def test_lead_and_lag_are_neighbours_z_and_zero_past_the_vod_edges(lexicon: Lexicon) -> None:
    rows = [
        (10.0 * w + i * 0.1, f"u{i}", "x") for w, n in enumerate([1, 4, 2, 8]) for i in range(n)
    ]
    frame = features(lexicon, 40.0, *rows)

    z = column(frame, "chat_msg_rate_z")
    assert column(frame, "chat_msg_rate_lead1") == [z[1], z[2], z[3], 0.0]
    assert column(frame, "chat_msg_rate_lead3") == [z[3], 0.0, 0.0, 0.0]
    assert column(frame, "chat_msg_rate_lag1") == [0.0, z[0], z[1], z[2]]
    assert column(frame, "chat_msg_rate_lag2") == [0.0, 0.0, z[0], z[1]]


def test_vods_are_normalized_separately_and_neighbours_never_cross_vods(
    lexicon: Lexicon,
) -> None:
    windows = pl.concat([make_windows("a", 20.0), make_windows("b", 20.0)])
    chat = pl.concat(
        [
            messages((1.0, "u", "x"), video_id="a"),
            messages(*[(11.0 + i * 0.1, f"u{i}", "x") for i in range(10)], video_id="b"),
        ]
    )

    frame = compute_chat_features(windows, chat, lexicon=lexicon)

    assert column(frame, "chat_msg_rate_z") == [1.0, -1.0, -1.0, 1.0]
    assert column(frame, "chat_msg_rate_lead1") == [-1.0, 0.0, 1.0, 0.0]


def test_clip_links_are_removed_before_counting(lexicon: Lexicon) -> None:
    frame = features(
        lexicon,
        10.0,
        (1.0, "a", "https://clips.twitch.tv/SyntheticClip01-abc"),
        (2.0, "b", "look twitch.tv/user_001/clip/AbcDef-123"),
        (3.0, "c", "user_001 created a clip"),
        (3.5, "e", "https://m.twitch.tv/clip/AbcDef"),
        (4.0, "d", "clip it lol"),  # an ordinary reaction stays
    )

    assert column(frame, "chat_msg_rate") == [0.1]
    assert column(frame, "chat_keyword_rate") == [0.1]


def test_text_shape_features(lexicon: Lexicon) -> None:
    frame = features(
        lexicon,
        10.0,
        (1.0, "a", "ABC def"),  # 3 of 6 letters uppercase, 2 tokens, 7 characters
        (2.0, "b", "KEKW KEKW"),  # 2 emotes in 2 tokens, 4 of 4 letters... x2
        (3.0, "c", "123 !!"),  # no letters
    )

    letters, upper = 6 + 8, 3 + 8
    assert column(frame, "chat_caps_frac") == [pytest.approx(upper / letters)]
    assert column(frame, "chat_emote_frac") == [pytest.approx(2 / 6)]
    assert column(frame, "chat_emote_rate") == [pytest.approx(0.2)]
    assert column(frame, "chat_mean_len") == [pytest.approx((7 + 9 + 6) / 3)]


def test_repeats_count_messages_seen_in_the_last_10_seconds_across_windows(
    lexicon: Lexicon,
) -> None:
    frame = features(
        lexicon,
        30.0,
        (8.0, "a", "GG"),
        (12.0, "b", "gg "),  # repeats 8.0 (case and spacing ignored), in the next window
        (13.0, "c", "other"),
        (22.5, "d", "gg"),  # 10.5 s after 12.0: not a repeat
    )

    assert column(frame, "chat_repeat_frac") == [0.0, 0.5, 0.0]


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("KEKW", "laugh"),
        ("that was so funny LMAO", "laugh"),
        ("hahahaha", "laugh"),
        ("W", "hype"),
        ("LETS GOOOO", "hype"),
        ("no way!!!", "hype"),
        ("???", "shock"),
        ("WTF was that", "shock"),
        ("D:", "shock"),
        ("someone clip it", "keyword"),
        ("😂😂😂", "laugh"),
        ("KEKW!", "laugh"),
        ("LOLOL", "laugh"),
        ("lmaooo", "laugh"),
        ("W!", "hype"),
        ("LETSGOOO", "hype"),
        ("nooo way", "shock"),
    ],
)
def test_reaction_messages_are_counted_by_category(
    lexicon: Lexicon, text: str, category: str
) -> None:
    frame = features(lexicon, 10.0, (1.0, "a", text))

    assert column(frame, f"chat_{category}_rate") == [0.1]


@pytest.mark.parametrize("text", ["w", "what time is it", "Wow", "lollipop", "wait?"])
def test_ordinary_messages_are_not_reactions(lexicon: Lexicon, text: str) -> None:
    frame = features(lexicon, 10.0, (1.0, "a", text))

    for category in ("laugh", "hype", "shock"):
        assert column(frame, f"chat_{category}_rate") == [0.0], category


def test_events_are_cheers_subs_and_raids(lexicon: Lexicon) -> None:
    chat = pl.concat(
        [
            messages((1.0, "a", "Cheer100 nice"), bits=100),
            messages(
                (2.0, "b", "user subscribed at Tier 1. They've subscribed for 3 months!"),
                (3.0, "c", "user gifted a Tier 1 sub to user!"),
                (4.0, "d", "5 raiders from user have joined!"),
                (5.0, "e", "subscribe to me"),
            ),
        ]
    )

    frame = compute_chat_features(make_windows("v", 10.0), chat, lexicon=lexicon)

    assert column(frame, "chat_event_count") == [4.0]


def test_messages_of_vods_without_windows_are_ignored(lexicon: Lexicon) -> None:
    chat = pl.concat([messages((1.0, "a", "x")), messages((1.0, "a", "x"), video_id="other")])

    frame = compute_chat_features(make_windows("v", 10.0), chat, lexicon=lexicon)

    assert column(frame, "chat_msg_rate") == [0.1]


def test_invalid_inputs_are_rejected(lexicon: Lexicon) -> None:
    windows = make_windows("v", 20.0)
    with pytest.raises(ValueError, match="missing columns: bits"):
        compute_chat_features(windows, messages().drop("bits"), lexicon=lexicon)
    shifted = windows.with_columns(
        window_id=pl.format("v:shifted{}", pl.col("index")), start_s=pl.col("start_s") + 5.0
    )
    overlapping = pl.concat([windows, shifted])
    with pytest.raises(ValueError, match="overlap"):
        compute_chat_features(overlapping, messages(), lexicon=lexicon)
    with pytest.raises(ValueError, match="not finite"):
        compute_chat_features(windows, messages((float("nan"), "a", "x")), lexicon=lexicon)
    chat = messages((1.0, "a", "x")).with_columns(user_hash=pl.lit(None, pl.String))
    with pytest.raises(ValueError, match="nulls in: user_hash"):
        compute_chat_features(windows, chat, lexicon=lexicon)
    duplicated = pl.concat([windows, windows])
    with pytest.raises(ValueError, match="more than once"):
        compute_chat_features(duplicated, messages(), lexicon=lexicon)


# --- Lexicon -----------------------------------------------------------------------------------


def write_lexicon(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "lexicon.yaml"
    path.write_text(json.dumps(data), encoding="utf-8")  # JSON is valid YAML
    return path


def minimal_lexicon() -> dict[str, object]:
    entry = {"words": ["x"]}
    return {
        "version": 2,
        "categories": dict.fromkeys(("laugh", "hype", "shock", "keyword"), entry),
        "events": entry,
        "clip_filter": entry,
    }


def test_lexicon_version_is_part_of_the_feature_version(tmp_path: Path) -> None:
    lexicon = load_lexicon(write_lexicon(tmp_path, minimal_lexicon()))

    assert lexicon.feature_version == "chat-v1+lexicon-v2"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"version": "1"}, "version"),
        ({"categories": {"laugh": {"words": ["x"]}}}, "exactly"),
        ({"events": {"patterns": ["(unclosed"]}}, "doesn't compile"),
        ({"clip_filter": {"words": [""]}}, "non-blank"),
        ({"events": {}}, "no entries"),
        ({"events": {"regexes": ["x"]}}, "mapping of emotes"),
    ],
)
def test_malformed_lexicon_is_rejected(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    with pytest.raises(LexiconError, match=message):
        load_lexicon(write_lexicon(tmp_path, {**minimal_lexicon(), **change}))


def test_missing_lexicon_file_is_a_lexicon_error(tmp_path: Path) -> None:
    with pytest.raises(LexiconError, match="can't read"):
        load_lexicon(tmp_path / "missing.yaml")


# --- Synthetic dataset -------------------------------------------------------------------------


def test_synthetic_chat_bursts_stand_out_and_the_clip_link_is_removed(
    tmp_path: Path, lexicon: Lexicon
) -> None:
    dataset = generate_dataset(tmp_path / "synthetic", seed=1)
    root = dataset.out_dir
    vods = pl.read_parquet(root / "processed" / "vods.parquet")
    chat = pl.concat(
        [pl.read_parquet(p) for p in sorted((root / "interim" / "chat").glob("*.parquet"))]
    )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    windows = windows_for_vods(vods)

    frame = windows.join(compute_chat_features(windows, chat, lexicon=lexicon), on="window_id")

    assert frame.height == windows.height
    for vod in manifest["vods"]:
        own = frame.filter(pl.col("video_id") == vod["video_id"])
        for event in vod["planted_events"]:
            start, end = event["chat_burst_s"]
            burst = own.filter((pl.col("start_s") < end) & (pl.col("end_s") > start))
            # The busiest window of each burst is well above the VOD's average.
            assert (burst["chat_msg_rate_z"] > 1.0).any(), (
                vod["video_id"],
                event["case"],
            )
        quiet_start, quiet_end = vod["quiet_chat_s"]
        quiet = own.filter((pl.col("start_s") >= quiet_start) & (pl.col("end_s") <= quiet_end))
        assert quiet.height > 0
        assert quiet["chat_msg_rate"].sum() == 0.0
    linked = chat.filter(pl.col("text").str.contains("clips.twitch.tv"))
    assert linked.height == 1
    in_window = frame.filter(
        (pl.col("video_id") == linked["video_id"][0])
        & (pl.col("start_s") <= linked["offset_s"][0])
        & (pl.col("end_s") > linked["offset_s"][0])
    )
    others = chat.filter(
        (pl.col("video_id") == linked["video_id"][0])
        & (pl.col("offset_s") >= in_window["start_s"][0])
        & (pl.col("offset_s") < in_window["end_s"][0])
    )
    seconds = in_window["end_s"][0] - in_window["start_s"][0]
    assert in_window["chat_msg_rate"][0] == pytest.approx((others.height - 1) / seconds)
