"""Tests for the default ranking's rules (#192; autoclipper.models.ranking_rules)."""

import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from autoclipper.analysis import Candidate
from autoclipper.features.chat import compute_chat_features, load_lexicon
from autoclipper.labels.windows import make_windows
from autoclipper.models.baselines import ChatLoudness
from autoclipper.models.candidates import generate_candidates
from autoclipper.models.ranking_rules import (
    FLAGS,
    RankingRules,
    RankingRulesError,
    RuledScores,
    apply_ranking_rules,
    candidate_flags,
    load_ranking_rules,
    silent_seconds,
)

ROOT = Path(__file__).parents[2]
RULES_PATH = ROOT / "configs" / "ranking.yaml"
LEXICON_PATH = ROOT / "configs" / "lexicon.yaml"
DURATION_S = 3600.0
N_WINDOWS = int(DURATION_S // 10)


def vod(**columns: dict[int, float]) -> pl.DataFrame:
    """A 1-hour VOD of 10 s windows: quiet everywhere except the given window indexes.

    Each keyword is a feature column; its dict maps window index to value. Unlisted windows get
    0 (silence 0.0, so audio is audible), except ``chat_msg_rate`` (1 msg/s everywhere).
    """
    defaults = {"chat_msg_rate": 1.0}
    frame: dict[str, list[str] | list[float]] = {"window_id": [f"v:{i}" for i in range(N_WINDOWS)]}
    frame["start_s"] = [i * 10.0 for i in range(N_WINDOWS)]
    frame["end_s"] = [i * 10.0 + 10.0 for i in range(N_WINDOWS)]
    for name, values in columns.items():
        default = defaults.get(name, 0.0)
        frame[name] = [values.get(i, default) for i in range(N_WINDOWS)]
    return pl.DataFrame(frame)


def scores_of(frame: pl.DataFrame, peaks: dict[int, float]) -> pl.DataFrame:
    return frame.select("window_id", "start_s", "end_s").with_columns(
        score=pl.Series([peaks.get(i, 0.0) for i in range(frame.height)], dtype=pl.Float64)
    )


def adjusted(ruled: RuledScores, index: int) -> float:
    return float(ruled.scores["score"][index])


def test_the_shipped_config_equals_the_built_in_defaults() -> None:
    # The API and `analyze_vod(ranking_rules=None)` use the defaults; the CLI reads the file.
    assert load_ranking_rules(RULES_PATH) == RankingRules()


def test_intro_and_outro_windows_are_down_weighted_and_flagged() -> None:
    features = vod()
    first, middle, last = 4, N_WINDOWS // 2, N_WINDOWS - 3
    scores = scores_of(features, {first: 6.0, middle: 6.0, last: 6.0})

    ruled = apply_ranking_rules(scores, features, duration_s=DURATION_S, rules=RankingRules())

    # Median score 0: an edge window keeps half its height (edge_factor 0.5).
    assert adjusted(ruled, first) == pytest.approx(3.0)
    assert adjusted(ruled, last) == pytest.approx(3.0)
    assert adjusted(ruled, middle) == 6.0
    assert ruled.flags == {f"v:{first}": ("stream_start",), f"v:{last}": ("stream_end",)}


def test_a_short_vod_skips_the_edge_rules() -> None:
    features = vod().head(30)  # 5 minutes: shorter than intro_s + outro_s
    scores = scores_of(features, {3: 5.0})

    ruled = apply_ranking_rules(scores, features, duration_s=300.0, rules=RankingRules())

    assert adjusted(ruled, 3) == 5.0
    assert ruled.flags == {}


def test_silent_audio_is_down_weighted_but_a_twitch_mute_is_not() -> None:
    silent, muted, loud = 100, 200, 300
    features = vod(
        audio_silence_frac={silent: 1.0, muted: 1.0, loud: 0.1},
        audio_is_muted={muted: 1.0},
    )
    scores = scores_of(features, {silent: 4.0, muted: 4.0, loud: 4.0})

    ruled = apply_ranking_rules(scores, features, duration_s=DURATION_S, rules=RankingRules())

    assert adjusted(ruled, silent) == pytest.approx(2.0)
    assert ruled.flags[f"v:{silent}"] == ("silent_audio",)
    # Twitch-muted audio keeps the baselines' rule (counted as missing), not a silence penalty.
    assert adjusted(ruled, muted) == 4.0
    assert adjusted(ruled, loud) == 4.0
    assert silent_seconds(features, RankingRules()) == 10.0


def test_rules_multiply_and_only_lower_windows_above_the_median() -> None:
    features = vod(audio_silence_frac={2: 1.0, 3: 1.0})
    rest = dict.fromkeys(range(N_WINDOWS), 1.0)  # median 1.0
    scores = scores_of(features, {**rest, 2: 5.0, 3: -1.0})

    ruled = apply_ranking_rules(scores, features, duration_s=DURATION_S, rules=RankingRules())

    # Start of the stream and silent: a quarter of the height above the median is left.
    assert adjusted(ruled, 2) == pytest.approx(1.0 + 0.25 * 4.0)
    assert ruled.flags["v:2"] == ("stream_start", "silent_audio")
    # Below the median nothing changes, and no flag is claimed.
    assert adjusted(ruled, 3) == -1.0
    assert "v:3" not in ruled.flags


def chat_window_features(
    windows: pl.DataFrame, rows: list[tuple[float, str, str]], *, duration_s: float = DURATION_S
) -> pl.DataFrame:
    """Real chat features for messages (offset_s, user, text) over `windows`, audible audio."""
    chat = pl.DataFrame(
        {
            "video_id": "v",
            "offset_s": [r[0] for r in rows],
            "user_hash": [r[1] for r in rows],
            "text": [r[2] for r in rows],
            "n_emotes": 0,
            "bits": 0,
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
    features = compute_chat_features(windows, chat, lexicon=load_lexicon(LEXICON_PATH))
    return features.with_columns(audio_silence_frac=pl.lit(0.1), audio_is_muted=pl.lit(0.0))


def burst(start_s: float, texts: list[str], n: int, tag: str) -> list[tuple[float, str, str]]:
    """`n` messages spread over the 10 s window starting at `start_s`, cycling through `texts`."""
    return [(start_s + i * 10.0 / n, f"{tag}{i}", texts[i % len(texts)]) for i in range(n)]


def score_at(ruled: RuledScores, start_s: float) -> float:
    row = ruled.scores.filter(pl.col("start_s") == start_s)
    return float(row["score"][0])


def flags_at(ruled: RuledScores, windows: pl.DataFrame, start_s: float) -> tuple[str, ...]:
    window_id = windows.filter(pl.col("start_s") == start_s)["window_id"][0]
    return ruled.flags.get(window_id, ())


def test_an_intro_greeting_wave_is_flagged_but_mid_stream_kill_waves_are_not() -> None:
    """Greetings count only as whole messages, and only at the stream's start and end: a
    mid-stream "BYE BYE" kill wave or an outraged "HEY" stays a clippable reaction."""
    windows = make_windows("v", DURATION_S)
    intro, kill_wave, outrage = 60.0, 1800.0, 2400.0
    rows = [
        *burst(intro, ["hi chat", "hello", "LATE", "hiii", "HeyGuys", "first", "im late"], 80, "g"),
        *burst(kill_wave, ["BYE BYE", "byeee", "cya", "get out", "BYE", "peepoBye"], 80, "k"),
        *burst(outrage, ["HEY", "hey what was that", "HEY!!", "HEYYY", "hey"], 80, "o"),
    ]
    features = chat_window_features(windows, rows)
    scores = windows.select("window_id", "start_s", "end_s").with_columns(
        score=pl.when(pl.col("start_s").is_in([intro, kill_wave, outrage])).then(4.0).otherwise(0.0)
    )

    ruled = apply_ranking_rules(scores, features, duration_s=DURATION_S, rules=RankingRules())

    assert flags_at(ruled, windows, intro) == ("stream_start", "greetings")
    assert score_at(ruled, intro) == pytest.approx(1.0)  # 0.5 x 0.5 of the height
    # The kill wave is nearly all farewells and the outrage mostly "hey", but mid-stream.
    kill_row = features.filter(pl.col("window_id") == "v:180")
    assert kill_row["chat_farewell_rate"][0] / kill_row["chat_msg_rate"][0] > 0.6
    for start in (kill_wave, outrage):
        assert flags_at(ruled, windows, start) == ()
        assert score_at(ruled, start) == 4.0


def test_whole_message_greetings_only() -> None:
    windows = make_windows("v", 20.0)
    texts = ["hey what was that", "he said bye", "hello there friend", "bye bye bye lol"]
    features = chat_window_features(windows, burst(0.0, texts, 8, "a"), duration_s=20.0)

    assert features["chat_greeting_rate"][0] == 0.0
    assert features["chat_farewell_rate"][0] == 0.0


def test_small_channel_hype_is_not_answer_spam() -> None:
    """A small chat (1.2 msg/s) reacting in different words is answer-shaped
    (chat_answer_frac 0.83) but not spam, even with the rule switched on."""
    windows = make_windows("v", DURATION_S)
    hype = 1800.0
    # A quiet 0.2 msg/s chat, then 12 messages in 10 s, all different, two of them reactions.
    rows = [(s + 0.5, f"u{s % 7}", "ok") for s in range(0, int(DURATION_S), 5) if s // 10 != 180]
    texts = ["bro what", "nahh", "hes cooked", "no shot", "he did not", "cmon man"]
    texts += ["not again", "why", "how", "stop", "KEKW", "LMAO"]
    rows += burst(hype, texts, 12, "h")
    features = chat_window_features(windows, rows)
    scores = windows.select("window_id", "start_s", "end_s").with_columns(
        score=pl.when(pl.col("start_s") == hype).then(4.0).otherwise(0.0)
    )
    hype_row = features.filter(pl.col("window_id") == "v:180")
    assert hype_row["chat_msg_rate"][0] == pytest.approx(1.2)
    assert hype_row["chat_answer_frac"][0] == pytest.approx(10 / 12)  # 0.83 > answer_frac_min

    for rules in (RankingRules(), RankingRules(answer_factor=0.5)):
        ruled = apply_ranking_rules(scores, features, duration_s=DURATION_S, rules=rules)
        assert flags_at(ruled, windows, hype) == ()
        assert score_at(ruled, hype) == 4.0


def test_answer_spam_is_off_by_default_and_needs_a_busy_window_when_on() -> None:
    windows = make_windows("v", DURATION_S)
    game, small = 1800.0, 2400.0
    rows = [(s + 0.5, f"u{s % 40}", "ok") for s in range(int(DURATION_S))]  # 1 msg/s
    countries = [f"country{i}" for i in range(80)]
    rows += burst(game, countries, 80, "c")  # 8 msg/s, every answer different
    rows += burst(small, countries[:20], 20, "s")  # answer-shaped but only 2 msg/s
    features = chat_window_features(windows, rows)
    scores = windows.select("window_id", "start_s", "end_s").with_columns(
        score=pl.when(pl.col("start_s").is_in([game, small])).then(4.0).otherwise(0.0)
    )

    off = apply_ranking_rules(scores, features, duration_s=DURATION_S, rules=RankingRules())
    on = apply_ranking_rules(
        scores, features, duration_s=DURATION_S, rules=RankingRules(answer_factor=0.5)
    )

    assert RankingRules().answer_factor == 1.0
    assert off.flags == {}
    assert score_at(off, game) == 4.0
    assert flags_at(on, windows, game) == ("answer_spam",)
    assert score_at(on, game) == pytest.approx(2.0)
    assert flags_at(on, windows, small) == ()  # under answer_min_msgs


def test_disabled_rules_and_missing_columns_change_nothing() -> None:
    features = vod(audio_silence_frac={2: 1.0})
    scores = scores_of(features, {2: 5.0, 100: 5.0})

    off = apply_ranking_rules(
        scores, features, duration_s=DURATION_S, rules=RankingRules(enabled=False)
    )
    no_features = apply_ranking_rules(scores, None, duration_s=DURATION_S, rules=RankingRules())

    assert off.scores.equals(scores)
    assert off.flags == {}
    # Without features only the edge rule can match.
    assert adjusted(no_features, 2) == pytest.approx(2.5)
    assert no_features.flags == {"v:2": ("stream_start",)}


def test_a_mid_stream_hype_moment_is_unaffected_end_to_end() -> None:
    """Real chat features: a laughing burst mid-stream keeps its score and rank, while a louder
    chat burst of greetings at the start over silent audio drops below it."""
    lexicon = load_lexicon(LEXICON_PATH)
    windows = make_windows("v", DURATION_S)
    rows: list[tuple[float, str, str]] = []
    rng = np.random.default_rng(0)
    for second in range(int(DURATION_S)):  # a calm 0.5 msg/s baseline
        if rng.random() < 0.5:
            rows.append((second + 0.5, f"u{second % 40}", "ok"))
    intro = ["hi", "hello", "LATE", "hey chat", "first", "im late", "yo chat", "hiii"]
    rows += [(30.0 + i * 0.05, f"g{i}", intro[i % len(intro)]) for i in range(200)]
    hype = ["KEKW", "LMAO", "KEKW KEKW", "OMEGALUL", "lol"]
    rows += [(1800.0 + i * 0.1, f"h{i}", hype[i % len(hype)]) for i in range(100)]
    chat = pl.DataFrame(
        {
            "video_id": "v",
            "offset_s": [r[0] for r in rows],
            "user_hash": [r[1] for r in rows],
            "text": [r[2] for r in rows],
            "n_emotes": 0,
            "bits": 0,
        }
    )
    features = compute_chat_features(windows, chat, lexicon=lexicon)
    silence = [1.0 if start < 120 else 0.1 for start in windows["start_s"].to_list()]
    features = features.with_columns(
        audio_rms_max_z=pl.lit(0.0),
        audio_silence_frac=pl.Series(silence),
        audio_is_muted=pl.lit(0.0),
    )
    raw = (
        ChatLoudness()
        .score(features)
        .join(windows.select("window_id", "start_s", "end_s"), on="window_id")
    )
    ruled = apply_ranking_rules(raw, features, duration_s=DURATION_S, rules=RankingRules())

    before = generate_candidates(raw, duration_s=DURATION_S, features=features)
    after = generate_candidates(ruled.scores, duration_s=DURATION_S, features=features)

    assert before[0].start_s < 60  # the plain sum ranks the greeting wave first
    assert 1770 <= after[0].start_s <= 1800  # the laughing burst wins once the rules apply
    hype_before = next(c for c in before if 1700 < c.start_s < 1810)
    assert after[0].score == hype_before.score  # unaffected
    flags = candidate_flags(after, ruled)
    intro_candidate = next(c for c in after if c.start_s < 60)
    assert flags[intro_candidate.candidate_id] == ("stream_start", "silent_audio", "greetings")
    assert after[0].candidate_id not in flags


def test_candidate_flags_follow_the_window_holding_the_score() -> None:
    features = vod()
    scores = scores_of(features, {1: 6.0, 2: 4.0})
    ruled = apply_ranking_rules(scores, features, duration_s=DURATION_S, rules=RankingRules())
    candidate = Candidate(
        candidate_id="c001",
        rank=1,
        score=adjusted(ruled, 1),
        start_s=0.0,
        end_s=30.0,
        source="discovered",
        n_viewer_clips=0,
        top_signals=(),
    )

    assert candidate_flags([candidate], ruled) == {"c001": ("stream_start",)}
    assert candidate_flags([candidate], RuledScores(scores, {})) == {}


def test_every_flag_has_an_explanation() -> None:
    from autoclipper.models.explain import _RANKING_FLAG_TEXT  # pyright: ignore[reportPrivateUsage]

    assert set(_RANKING_FLAG_TEXT) == set(FLAGS)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"intro_s": 60}, "version"),
        ({"version": 1, "intro_seconds": 60}, "unknown keys: intro_seconds"),
        ({"version": 1, "edge_factor": 1.5}, "between 0 and 1"),
        ({"version": 1, "intro_s": -1}, ">= 0"),
        ({"version": 1, "intro_s": "long"}, "must be a number"),
        ({"version": 1, "enabled": "yes"}, "true or false"),
        ([1, 2], "not a mapping"),
    ],
)
def test_malformed_rules_are_rejected(tmp_path: Path, data: object, message: str) -> None:
    path = tmp_path / "ranking.yaml"
    path.write_text(json.dumps(data), encoding="utf-8")  # JSON is valid YAML

    with pytest.raises(RankingRulesError, match=message):
        load_ranking_rules(path)


def test_left_out_keys_keep_their_defaults(tmp_path: Path) -> None:
    path = tmp_path / "ranking.yaml"
    path.write_text("version: 1\nintro_s: 60\n", encoding="utf-8")

    assert load_ranking_rules(path) == RankingRules(intro_s=60.0)
    with pytest.raises(RankingRulesError, match="can't read"):
        load_ranking_rules(tmp_path / "missing.yaml")


def test_without_a_path_the_rules_fall_back_to_the_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoclipper.models.ranking_rules import resolve_ranking_rules

    monkeypatch.chdir(tmp_path)  # no configs/ranking.yaml here
    assert resolve_ranking_rules(None) == RankingRules()
    with pytest.raises(RankingRulesError, match="can't read"):
        resolve_ranking_rules(tmp_path / "missing.yaml")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ranking.yaml").write_text("version: 1\nenabled: false\n")
    assert resolve_ranking_rules(None) == RankingRules(enabled=False)
