"""Tests for the synthetic dataset generator (autoclipper.synthetic)."""

import itertools
import json
import re
import wave
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyarrow.parquet as pq
import pytest

from autoclipper.ffmpeg import CommandResult, FFmpegCommand, FFmpegExitError, probe
from autoclipper.synthetic import (
    GeneratedDataset,
    OutputDirError,
    VideoOptions,
    default_output_dir,
    generate_dataset,
)
from autoclipper.synthetic.scenario import build_scenario

SEED = 7
TS = "timestamp[us, tz=UTC]"
CLIPS = f"processed/clips/synthetic-seed-{SEED}.parquet"

# The proposed tables in docs/architecture/data-model.md: (column, Arrow type, nullable).
EXPECTED_SCHEMAS = {
    "processed/broadcasters.parquet": [
        ("broadcaster_id", "string", False),
        ("login", "string", False),
        ("display_name", "string", False),
        ("broadcaster_type", "string", False),
        ("fetched_at", TS, False),
    ],
    "processed/vods.parquet": [
        ("video_id", "string", False),
        ("broadcaster_id", "string", False),
        ("stream_id", "string", True),
        ("title", "string", False),
        ("created_at", TS, False),
        ("duration_s", "double", False),
        ("type", "string", False),
        ("language", "string", False),
        ("view_count", "int64", False),
        (
            "muted_segments",
            "list<element: struct<offset_s: double not null, duration_s: double not null> "
            "not null>",
            False,
        ),
        ("game_ids", "list<element: string not null>", True),
        ("fetched_at", TS, False),
    ],
    CLIPS: [
        ("clip_id", "string", False),
        ("video_id", "string", True),
        ("broadcaster_id", "string", False),
        ("creator_hash", "string", False),
        ("title", "string", False),
        ("game_id", "string", False),
        ("language", "string", False),
        ("created_at", TS, False),
        ("vod_offset_s", "double", True),
        ("duration_s", "double", False),
        ("view_count", "int64", False),
        ("is_featured", "bool", True),
        ("fetched_at", TS, False),
    ],
    "interim/chat/9100000001.parquet": [
        ("video_id", "string", False),
        ("offset_s", "double", False),
        ("user_hash", "string", False),
        ("text", "string", False),
        ("emote_ids", "list<element: string not null>", False),
        ("n_emotes", "int64", False),
        ("is_subscriber", "bool", True),
        ("is_moderator", "bool", True),
        ("bits", "int64", False),
        ("source", "string", False),
    ],
    "catalog.parquet": [
        ("object_type", "string", False),
        ("object_key", "string", False),
        ("status", "string", False),
        ("tool", "string", False),
        ("stage_version", "string", False),
        ("created_at", TS, False),
        ("error", "string", True),
    ],
}
VIDEO_IDS = ("9100000001", "9100000002", "9100000003")
HEX64 = re.compile(r"[0-9a-f]{64}")


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> GeneratedDataset:
    return generate_dataset(tmp_path_factory.mktemp("synthetic") / "out", seed=SEED)


def _rows(dataset: GeneratedDataset, name: str) -> list[dict[str, Any]]:
    return pq.read_table(dataset.out_dir / name).to_pylist()


def _manifest(dataset: GeneratedDataset) -> dict[str, Any]:
    return json.loads((dataset.out_dir / "manifest.json").read_text(encoding="utf-8"))


def _samples(dataset: GeneratedDataset, video_id: str) -> np.ndarray:
    with wave.open(str(dataset.out_dir / "interim" / "audio" / f"{video_id}.wav"), "rb") as wav:
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")


def _vod(dataset: GeneratedDataset, video_id: str) -> dict[str, Any]:
    return next(vod for vod in _manifest(dataset)["vods"] if vod["video_id"] == video_id)


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# --- Determinism --------------------------------------------------------------------------------


def test_same_seed_gives_byte_identical_files(dataset: GeneratedDataset, tmp_path: Path) -> None:
    again = generate_dataset(tmp_path / "again", seed=SEED)

    assert again.files == dataset.files
    assert _file_bytes(again.out_dir) == _file_bytes(dataset.out_dir)


def test_different_seed_gives_different_data(dataset: GeneratedDataset, tmp_path: Path) -> None:
    other = _file_bytes(generate_dataset(tmp_path / "other", seed=SEED + 1).out_dir)
    ours = _file_bytes(dataset.out_dir)

    for name in ("processed/vods.parquet", "interim/chat/9100000001.parquet"):
        assert other[name] != ours[name]
    for video_id in VIDEO_IDS:
        assert other[f"interim/audio/{video_id}.wav"] != ours[f"interim/audio/{video_id}.wav"]


def test_manifest_lists_every_file_with_its_sha256(dataset: GeneratedDataset) -> None:
    manifest = _manifest(dataset)

    listed = {entry["path"] for entry in manifest["files"]}
    assert listed | {"manifest.json"} == set(dataset.files) == set(_file_bytes(dataset.out_dir))
    assert all(HEX64.fullmatch(entry["sha256"]) for entry in manifest["files"])
    assert manifest["seed"] == SEED
    assert "PROPOSED" in manifest["note"]


# --- Schemas and IDs ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(EXPECTED_SCHEMAS))
def test_tables_follow_the_proposed_data_model(dataset: GeneratedDataset, name: str) -> None:
    schema = pq.read_schema(dataset.out_dir / name)

    assert [(f.name, str(f.type), f.nullable) for f in schema] == EXPECTED_SCHEMAS[name]


def test_every_vod_has_a_chat_table_and_a_wav(dataset: GeneratedDataset) -> None:
    for video_id in VIDEO_IDS:
        assert f"interim/chat/{video_id}.parquet" in dataset.files
        assert f"interim/audio/{video_id}.wav" in dataset.files


def test_vods_are_a_few_minutes_from_fake_broadcasters(dataset: GeneratedDataset) -> None:
    vods = _rows(dataset, "processed/vods.parquet")
    broadcasters = _rows(dataset, "processed/broadcasters.parquet")

    assert [vod["video_id"] for vod in vods] == list(VIDEO_IDS)
    assert all(120 <= vod["duration_s"] <= 360 for vod in vods)
    assert {b["login"] for b in broadcasters} == {"user_001", "user_002"}
    assert {vod["broadcaster_id"] for vod in vods} == {b["broadcaster_id"] for b in broadcasters}
    assert all(b["broadcaster_id"].startswith("900000000") for b in broadcasters)


# --- Clips ---------------------------------------------------------------------------------------


def test_clips_include_overlapping_touching_near_touching_and_disjoint_pairs(
    dataset: GeneratedDataset,
) -> None:
    clips = _rows(dataset, CLIPS)
    gaps: list[float] = []  # start of each clip minus the end of the previous one, per VOD
    for video_id in VIDEO_IDS:
        spans = sorted(
            (c["vod_offset_s"], c["vod_offset_s"] + c["duration_s"])
            for c in clips
            if c["video_id"] == video_id and c["vod_offset_s"] is not None
        )
        gaps += [start - prev_end for (_, prev_end), (start, _) in itertools.pairwise(spans)]

    assert any(gap < 0 for gap in gaps), "overlapping"
    assert 0.0 in gaps, "touching: one clip ends exactly where the next starts"
    assert any(0 < gap <= 5 for gap in gaps), "near-touching, within the proposed GAP_S"
    assert any(gap > 10 for gap in gaps), "disjoint"


def test_exactly_one_clip_lacks_an_offset_and_one_lacks_a_video(
    dataset: GeneratedDataset,
) -> None:
    clips = _rows(dataset, CLIPS)

    no_offset = [c for c in clips if c["vod_offset_s"] is None and c["video_id"] is not None]
    no_video = [c for c in clips if c["video_id"] is None]
    assert len(no_offset) == 1
    assert len(no_video) == 1
    assert no_video[0]["vod_offset_s"] is None  # as the API returns it without a VOD link


def test_one_vod_has_no_clips(dataset: GeneratedDataset) -> None:
    clips = _rows(dataset, CLIPS)

    assert "9100000003" not in {c["video_id"] for c in clips}


@pytest.mark.parametrize("seed", range(0, 200, 7))
def test_every_seed_keeps_the_scenario_apart(seed: int) -> None:
    # Jitter must never push a clip past the VOD's end, a chat burst into a quiet stretch or a
    # loud burst into muted audio, whatever the seed.
    scenario = build_scenario(seed)

    for vod in scenario.vods:
        quiet_start, quiet_end = vod.plan.quiet
        for clip in scenario.clips:
            offset_s = cast(float | None, clip["vod_offset_s"])
            if clip["video_id"] == vod.plan.video_id and offset_s is not None:
                assert offset_s + cast(float, clip["duration_s"]) <= vod.duration_s
        for event in vod.events:
            start, end = event.chat_burst_s
            assert end <= vod.duration_s
            assert end <= quiet_start or start >= quiet_end
            if vod.plan.muted is not None:
                muted_start, muted_s = vod.plan.muted
                burst_start, burst_end = event.audio_burst_s
                assert burst_end <= muted_start or burst_start >= muted_start + muted_s


# --- Chat ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_chat_bursts_a_few_seconds_after_each_planted_moment(
    dataset: GeneratedDataset, video_id: str
) -> None:
    offsets = np.array([r["offset_s"] for r in _rows(dataset, f"interim/chat/{video_id}.parquet")])
    vod = _vod(dataset, video_id)
    events = vod["planted_events"]
    bursts = [event["chat_burst_s"] for event in events]

    def rate(start: float, end: float) -> float:
        return np.count_nonzero((offsets >= start) & (offsets < end)) / (end - start)

    burst_s = sum(end - start for start, end in bursts)
    n_in_bursts = sum(rate(start, end) * (end - start) for start, end in bursts)
    calm_rate = (len(offsets) - n_in_bursts) / (vod["duration_s"] - burst_s)
    assert offsets.min() >= 0
    assert offsets.max() < vod["duration_s"]
    assert list(offsets) == sorted(offsets)
    for event, (start, end) in zip(events, bursts, strict=True):
        assert 3 <= start - event["time_s"] <= 8
        assert rate(start, end) > 5 * calm_rate
        # Chat reacts after the moment: the seconds before it are ordinary chat.
        assert rate(event["time_s"] - 10, event["time_s"]) < rate(start, end) / 2


@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_chat_is_silent_in_the_quiet_stretch(dataset: GeneratedDataset, video_id: str) -> None:
    offsets = [r["offset_s"] for r in _rows(dataset, f"interim/chat/{video_id}.parquet")]
    quiet_start, quiet_end = _vod(dataset, video_id)["quiet_chat_s"]

    assert quiet_end - quiet_start >= 15
    assert not [t for t in offsets if quiet_start <= t < quiet_end]


def test_chat_has_emotes_and_exactly_one_clip_link(dataset: GeneratedDataset) -> None:
    rows = [r for v in VIDEO_IDS for r in _rows(dataset, f"interim/chat/{v}.parquet")]
    clip_ids = {c["clip_id"] for c in _rows(dataset, CLIPS)}

    assert any(r["emote_ids"] for r in rows), "native Twitch emotes"
    assert any(r["n_emotes"] > len(r["emote_ids"]) for r in rows), "third-party emotes"
    assert all(r["n_emotes"] >= len(r["emote_ids"]) for r in rows)
    links = [r["text"] for r in rows if "clips.twitch.tv/" in r["text"]]
    assert len(links) == 1
    assert links[0].rsplit("/", 1)[1] in clip_ids


def test_chatters_appear_only_as_hashes(dataset: GeneratedDataset) -> None:
    rows = [r for v in VIDEO_IDS for r in _rows(dataset, f"interim/chat/{v}.parquet")]

    assert all(HEX64.fullmatch(r["user_hash"]) for r in rows)
    assert len({r["user_hash"] for r in rows}) > 20
    assert not [r["text"] for r in rows if re.search(r"user_\d|93\d{8}", r["text"])]


# --- Audio ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_wav_is_16_khz_mono_pcm_as_long_as_the_vod(
    dataset: GeneratedDataset, video_id: str
) -> None:
    with wave.open(str(dataset.out_dir / "interim" / "audio" / f"{video_id}.wav"), "rb") as wav:
        params = wav.getparams()

    assert (params.nchannels, params.sampwidth, params.framerate) == (1, 2, 16_000)
    assert params.nframes == round(_vod(dataset, video_id)["duration_s"] * 16_000)


@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_wav_is_loud_at_each_planted_moment(dataset: GeneratedDataset, video_id: str) -> None:
    samples = _samples(dataset, video_id).astype(np.float64)
    seconds = samples[: len(samples) // 16_000 * 16_000].reshape(-1, 16_000)
    median_rms = np.median(np.sqrt((seconds**2).mean(axis=1)))

    for event in _vod(dataset, video_id)["planted_events"]:
        start = round(event["audio_burst_s"][0] * 16_000)
        burst = samples[start : start + 16_000]
        assert np.sqrt((burst**2).mean()) > 5 * median_rms


def test_muted_segment_is_silent_and_matches_the_vods_table(dataset: GeneratedDataset) -> None:
    vods = {v["video_id"]: v for v in _rows(dataset, "processed/vods.parquet")}

    muted = {vid: v["muted_segments"] for vid, v in vods.items() if v["muted_segments"]}
    assert list(muted) == ["9100000002"]
    (segment,) = muted["9100000002"]
    samples = _samples(dataset, "9100000002")
    start = round(segment["offset_s"] * 16_000)
    end = round((segment["offset_s"] + segment["duration_s"]) * 16_000)
    assert not samples[start:end].any()
    assert samples[start - 16_000 : start].any()
    assert samples[end : end + 16_000].any()


# --- Catalog -------------------------------------------------------------------------------------


def test_catalog_has_an_ok_row_per_vod_and_object(dataset: GeneratedDataset) -> None:
    catalog = _rows(dataset, "catalog.parquet")

    keys = {(r["object_type"], r["object_key"]) for r in catalog}
    assert keys == {(t, v) for t in ("vod_meta", "clips", "chat", "audio") for v in VIDEO_IDS}
    assert all(r["status"] == "ok" and r["error"] is None for r in catalog)


# --- Output directory ----------------------------------------------------------------------------


def test_default_output_dir_is_under_the_data_dir(tmp_path: Path) -> None:
    assert default_output_dir(tmp_path, 3) == tmp_path / "synthetic" / "seed-3"


def test_refuses_a_directory_with_other_files_even_with_force(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "real.parquet").write_bytes(b"not synthetic")

    for force in (False, True):
        with pytest.raises(OutputDirError, match="not empty"):
            generate_dataset(tmp_path / "out", seed=1, force=force)
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["real.parquet"]


def test_replaces_an_earlier_dataset_only_with_force(tmp_path: Path) -> None:
    generate_dataset(tmp_path / "out", seed=1)

    with pytest.raises(OutputDirError, match="--force"):
        generate_dataset(tmp_path / "out", seed=2)
    generate_dataset(tmp_path / "out", seed=2, force=True)

    assert json.loads((tmp_path / "out" / "manifest.json").read_text("utf-8"))["seed"] == 2


def test_fills_an_existing_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()

    assert (generate_dataset(tmp_path / "out", seed=1).out_dir / "manifest.json").is_file()


def test_rejects_a_negative_seed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="seed"):
        generate_dataset(tmp_path / "out", seed=-1)
    assert list(tmp_path.iterdir()) == []


# --- Video ---------------------------------------------------------------------------------------


class FakeFFmpeg:
    """Records FFmpeg commands; fails the encode at `fail_at` (1-based), if given."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.commands: list[FFmpegCommand] = []
        self.fail_at = fail_at

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        self.commands.append(command)
        if command.args[1:] == ("-version",):
            return CommandResult(0, b"ffmpeg version 9.9-fake Copyright\n")
        if len(self.commands) == self.fail_at:
            return CommandResult(1, stderr=b"encoder exploded")
        return CommandResult(0)


def test_builds_one_test_pattern_command_per_vod(tmp_path: Path) -> None:
    runner = FakeFFmpeg()
    options = VideoOptions(duration_s=2.5, size=(160, 90), frame_rate=5)

    dataset = generate_dataset(tmp_path / "out", seed=1, video=options, runner=runner)

    encodes = [c.args for c in runner.commands if "-version" not in c.args]
    assert len(encodes) == len(VIDEO_IDS)
    for args, video_id in zip(encodes, VIDEO_IDS, strict=True):
        assert "testsrc2=size=160x90:rate=5:duration=2.5" in args
        assert args[args.index("-t") + 1] == "2.5"
        assert Path(args[-1]).parts[-3:] == ("raw", "video", f"{video_id}.mp4")
        assert any(a.endswith(f"{video_id}.wav") for a in args)
    assert _manifest(dataset)["environment"]["ffmpeg"] == "9.9-fake"


def test_failed_video_leaves_no_partial_output(tmp_path: Path) -> None:
    with pytest.raises(FFmpegExitError):
        generate_dataset(tmp_path / "out", seed=1, video=VideoOptions(), runner=FakeFFmpeg(2))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kwargs",
    [{"duration_s": 0.0}, {"duration_s": float("nan")}, {"size": (321, 180)}, {"frame_rate": 0}],
)
def test_video_options_reject_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="video"):
        VideoOptions(**kwargs)


@pytest.mark.ffmpeg
def test_generates_playable_video_with_the_vods_audio(tmp_path: Path) -> None:
    options = VideoOptions(duration_s=2.0)

    dataset = generate_dataset(tmp_path / "out", seed=1, video=options)

    for video_id in VIDEO_IDS:
        info = probe(dataset.out_dir / "raw" / "video" / f"{video_id}.mp4")
        (video,) = info.video_streams
        (audio,) = info.audio_streams
        assert (video.width, video.height) == (320, 180)
        assert audio.codec_name == "aac"
        assert info.duration_s == pytest.approx(2.0, abs=0.1)
