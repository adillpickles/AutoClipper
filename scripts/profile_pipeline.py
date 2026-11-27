"""Profile the AutoClipper analysis and export pipelines on synthetic data.

Generates a long synthetic VOD (default 4 hours of 16 kHz mono audio and chat messages) and a
synthetic test video in a temporary directory, measures wall-clock time per stage, counts
FFmpeg/FFprobe subprocess invocations in the export/highlights paths, and prints cProfile hot
spots for the slowest Python stages.

Usage:
    uv run python scripts/profile_pipeline.py
    uv run python scripts/profile_pipeline.py --duration-hours 6 --chat-rate 15
    uv run python scripts/profile_pipeline.py --skip-export
"""

import argparse
import cProfile
import io
import json
import pstats
import random
import tempfile
import time
import wave
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from pydantic import SecretStr

from autoclipper.export.clip import ClipSpec, render_clip
from autoclipper.export.cut import Interval
from autoclipper.export.highlights import HighlightsRequest, Segment, render_highlights
from autoclipper.features import audio as audio_features
from autoclipper.features.audio import (
    SAMPLE_RATE_HZ,
    compute_audio_features,
    detect_onsets,
    frame_features,
)
from autoclipper.features.chat import compute_chat_features, load_lexicon
from autoclipper.ffmpeg import CommandResult, FFmpegCommand, run_command, run_subprocess
from autoclipper.ingest.chat import parse_chat_json
from autoclipper.labels.annotations import label_windows
from autoclipper.labels.moments import merge_clips_into_moments
from autoclipper.labels.windows import make_windows
from autoclipper.models.baselines import ChatLoudness, random_scorers
from autoclipper.models.candidates import MomentInput, generate_candidates
from autoclipper.models.trained import LightGBMScorer, LogisticRegressionScorer
from autoclipper.synthetic.media import VideoOptions, video_command

LEXICON_PATH = Path(__file__).resolve().parents[1] / "configs" / "lexicon.yaml"
BASE_TIME = datetime(2026, 9, 7, 18, 0, tzinfo=UTC)
_CALM_LINES = (
    "hi chat",
    "what game is this",
    "lol",
    "gg",
    "true",
    "nice",
    "o7",
    "this song is good",
    "hydrate",
    "first time here",
    "W",
    "same",
    "chat is slow",
    "that was close",
    "brb",
)
_HYPE_LINES = (
    "NO WAY KEKW",
    "CLIP IT OMEGALUL",
    "WHAT PogChamp",
    "LETS GO W W W",
    "insane LUL",
    "HOLY monkaS",
    "no shot Kappa",
)


@dataclass
class StageTiming:
    """Recorded wall-clock timing for one pipeline stage."""

    name: str
    elapsed_s: float
    detail: str = ""


class CountingRunner:
    """Wraps `run_subprocess` to record FFmpeg/FFprobe call counts and durations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float, str]] = []

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        """Run `command` via `run_subprocess` and record its program, duration and summary."""
        start = time.perf_counter()
        result = run_subprocess(command, timeout_s=timeout_s)
        elapsed = time.perf_counter() - start
        kind = _classify_ffmpeg_call(command)
        self.calls.append((kind, elapsed, " ".join(command.args[:8])))
        return result

    def summary(self) -> str:
        """Compact summary of subprocess counts and total time by call kind."""
        counts: Counter[str] = Counter()
        totals: dict[str, float] = {}
        for kind, elapsed, _ in self.calls:
            counts[kind] += 1
            totals[kind] = totals.get(kind, 0.0) + elapsed
        parts = [f"{kind}={counts[kind]} ({totals[kind]:.2f}s)" for kind in sorted(counts)]
        return f"total_calls={len(self.calls)} " + ", ".join(parts)


def _classify_ffmpeg_call(command: FFmpegCommand) -> str:
    args = command.args
    if command.program == "ffprobe":
        joined = " ".join(args)
        if "packet=" in joined:
            if "flags" in joined:
                return "ffprobe:keyframe"
            return "ffprobe:stream_ends"
        if "format=format_name,duration,start_time" in joined:
            return "ffprobe:media_and_times"
        if "format=start_time" in joined and "stream=index,codec_type,start_time" in joined:
            return "ffprobe:stream_times"
        return "ffprobe:probe"
    if "-f" in args and "concat" in args:
        return "ffmpeg:concat"
    return "ffmpeg:encode"


def _write_long_wav(path: Path, duration_s: float, seed: int, burst_times_s: list[float]) -> None:
    """Stream a synthetic 16 kHz mono 16-bit WAV in chunks to avoid high memory usage."""
    rng = np.random.default_rng(seed)
    total_samples = round(duration_s * SAMPLE_RATE_HZ)
    chunk_samples = SAMPLE_RATE_HZ * 60  # 1 minute per chunk
    phase_base = 2 * np.pi * 150.0 * np.arange(SAMPLE_RATE_HZ // 2) / SAMPLE_RATE_HZ
    voice_period = (
        (np.sin(phase_base) + 0.5 * np.sin(2 * phase_base))
        * (0.5 + 0.5 * np.sin(phase_base / 37.5))
    ).astype(np.float32)
    burst_ranges = [
        (round((t - 0.5) * SAMPLE_RATE_HZ), round((t + 3.0) * SAMPLE_RATE_HZ))
        for t in burst_times_s
    ]

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE_HZ)
        written = 0
        while written < total_samples:
            n = min(chunk_samples, total_samples - written)
            voice = np.resize(voice_period, n) * np.float32(0.05)
            chunk = rng.standard_normal(n, dtype=np.float32) * np.float32(0.01) + voice
            chunk_end = written + n
            for b_start, b_end in burst_ranges:
                lo = max(written, b_start)
                hi = min(chunk_end, b_end)
                if lo < hi:
                    chunk[lo - written : hi - written] += rng.standard_normal(
                        hi - lo, dtype=np.float32
                    ) * np.float32(0.45)
            pcm = np.round(np.clip(chunk, -1.0, 1.0) * 32767).astype("<i2")
            wav.writeframes(pcm.tobytes())
            written = chunk_end


def _generate_chat_df(
    video_id: str, duration_s: float, chat_rate: float, burst_times_s: list[float], seed: int
) -> pl.DataFrame:
    """Build a synthetic `chat_messages` DataFrame with background chat and hype bursts."""
    rng = np.random.default_rng(seed)
    n_bg = max(1, int(duration_s * chat_rate))
    bg_offsets = np.sort(rng.uniform(0.0, duration_s, size=n_bg))
    burst_offsets_list = [
        rng.uniform(t + 3.0, min(duration_s - 0.1, t + 15.0), size=max(40, int(chat_rate * 15)))
        for t in burst_times_s
    ]
    burst_offsets = np.concatenate(burst_offsets_list) if burst_offsets_list else np.empty(0)
    offsets = np.concatenate([bg_offsets, burst_offsets])
    is_burst = np.concatenate(
        [np.zeros(len(bg_offsets), dtype=bool), np.ones(len(burst_offsets), dtype=bool)]
    )
    order = np.argsort(offsets, kind="mergesort")
    offsets = np.round(offsets[order], 3)
    is_burst = is_burst[order]

    n_total = len(offsets)
    user_pool = [f"{i:064x}" for i in range(1, 501)]
    user_idx = rng.integers(0, len(user_pool), size=n_total)
    users = [user_pool[i] for i in user_idx]

    calm_idx = rng.integers(0, len(_CALM_LINES), size=n_total)
    hype_idx = rng.integers(0, len(_HYPE_LINES), size=n_total)
    texts = [
        _HYPE_LINES[h] if burst else _CALM_LINES[c]
        for burst, c, h in zip(is_burst.tolist(), calm_idx.tolist(), hype_idx.tolist(), strict=True)
    ]
    n_emotes = np.where(is_burst, 1, rng.integers(0, 2, size=n_total)).astype(np.int64)
    bits = np.where(is_burst & (rng.random(n_total) < 0.02), 100, 0).astype(np.int64)

    return pl.DataFrame(
        {
            "video_id": [video_id] * n_total,
            "offset_s": offsets.astype(np.float64),
            "user_hash": users,
            "text": texts,
            "emote_ids": [[] for _ in range(n_total)],
            "n_emotes": n_emotes,
            "is_subscriber": [True] * n_total,
            "is_moderator": [False] * n_total,
            "bits": bits,
            "source": ["replay"] * n_total,
        }
    )


def _generate_clips_df(
    video_id: str, broadcaster_id: str, burst_times_s: list[float], seed: int
) -> pl.DataFrame:
    """Build synthetic viewer clips around planted bursts.

    Each clip's ``vod_offset_s`` is its end, as Helix reports it (#126).
    """
    py_rng = random.Random(seed)
    rows: list[dict[str, object]] = []
    idx = 0
    for burst_t in burst_times_s:
        for _ in range(py_rng.randint(1, 4)):
            idx += 1
            start = max(0.0, burst_t - py_rng.uniform(10.0, 25.0))
            dur = py_rng.uniform(20.0, 35.0)
            rows.append(
                {
                    "clip_id": f"SynthClip{idx:05d}",
                    "video_id": video_id,
                    "broadcaster_id": broadcaster_id,
                    "creator_hash": f"{idx:064x}",
                    "title": "clip",
                    "game_id": "9400000001",
                    "language": "en",
                    "created_at": BASE_TIME + timedelta(seconds=start + dur + 10),
                    "vod_offset_s": start + dur,
                    "duration_s": dur,
                    "view_count": py_rng.randint(20, 500),
                    "is_featured": False,
                    "fetched_at": BASE_TIME + timedelta(days=8, seconds=idx),
                }
            )
    return pl.DataFrame(rows)


def _make_chat_json_bytes(
    chat_df: pl.DataFrame, video_id: str, duration_s: float, max_messages: int
) -> bytes:
    """Build a TwitchDownloaderCLI 1.4.0 JSON payload for profiling `parse_chat_json`."""
    subset = chat_df.head(max_messages)
    comments: list[dict[str, object]] = []
    for idx, row in enumerate(subset.iter_rows(named=True)):
        u = (idx % 200) + 1
        offset_s = float(row["offset_s"])
        body = str(row["text"])
        if idx % 20 == 0:
            body = f"@viewer_{u:03d} {body}"
        comments.append(
            {
                "_id": f"00000000-0000-4000-8000-{idx:012d}",
                "created_at": (BASE_TIME + timedelta(seconds=offset_s)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "channel_id": "9000000001",
                "content_type": "video",
                "content_id": video_id,
                "content_offset_seconds": offset_s,
                "commenter": {
                    "display_name": f"Viewer_{u:03d}",
                    "_id": f"9300{u:06d}",
                    "name": f"viewer_{u:03d}",
                },
                "message": {
                    "body": body,
                    "bits_spent": int(row["bits"]),
                    "fragments": [{"text": body, "emoticon": None}],
                    "user_badges": [{"_id": "subscriber", "version": "1"}],
                },
            }
        )
    payload = {
        "FileInfo": {"Version": {"Major": 1, "Minor": 4, "Patch": 0}},
        "streamer": {"name": "User_001", "login": "user_001", "id": 9000000001},
        "video": {
            "title": "synthetic long vod",
            "id": video_id,
            "created_at": BASE_TIME.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "start": 0,
            "end": round(duration_s),
            "length": round(duration_s),
        },
        "comments": comments,
    }
    return json.dumps(payload).encode("utf-8")


def _profile_call[T](fn: Callable[[], T], top_n: int) -> tuple[T, float, str]:
    """Run `fn()` under cProfile and return `(result, elapsed_s, formatted_stats)`."""
    prof = cProfile.Profile()
    start = time.perf_counter()
    result = prof.runcall(fn)
    elapsed = time.perf_counter() - start
    buf = io.StringIO()
    stats = pstats.Stats(prof, stream=buf).sort_stats("cumtime")
    stats.print_stats(top_n)
    return result, elapsed, buf.getvalue()


def main() -> None:
    """Run the synthetic profiling suite and print timings and cProfile summaries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-hours", type=float, default=4.0)
    parser.add_argument("--chat-rate", type=float, default=10.0, help="Background chat msgs/sec.")
    parser.add_argument("--chat-json-messages", type=int, default=50_000)
    parser.add_argument("--video-duration-s", type=float, default=90.0)
    parser.add_argument("--highlights-segments", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--profile-top", type=int, default=15)
    parser.add_argument("--skip-export", action="store_true")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    duration_s = args.duration_hours * 3600.0
    if not args.skip_export:
        seg_len_s = 4.0
        lead_s = 2.0
        slack_s = 0.25
        span_s = args.video_duration_s - lead_s - seg_len_s - slack_s
        if args.video_duration_s < 6.0:
            raise SystemExit("--video-duration-s must be at least 6.0 when export is enabled")
        if args.highlights_segments < 1 or span_s <= 0:
            raise SystemExit(
                "highlights segments do not fit in --video-duration-s; "
                "lengthen the source or ask for fewer segments"
            )
    video_id = "9900000001"
    broadcaster_id = "9000000001"
    # Moments plus the 60 s negative buffer need room between bursts, or a short VOD has no
    # negative training windows and model fitting aborts before any timings are printed.
    burst_gap_s = 180.0
    n_bursts = max(1, min(int(args.duration_hours * 6), int(duration_s // burst_gap_s)))
    burst_times_s = [round((i + 0.5) * (duration_s / n_bursts), 1) for i in range(n_bursts)]
    timings: list[StageTiming] = []
    profiles: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="autoclipper-profile-") as tmp_name:
        tmp = Path(tmp_name)
        wav_path = tmp / f"{video_id}.wav"
        print(
            f"Synthesizing {args.duration_hours:.1f}h VOD "
            f"(~{int(duration_s * args.chat_rate):,} bg chat msgs, {n_bursts} bursts)..."
        )
        t0 = time.perf_counter()
        _write_long_wav(wav_path, duration_s, args.seed, burst_times_s)
        wav_gen_s = time.perf_counter() - t0
        chat_df = _generate_chat_df(video_id, duration_s, args.chat_rate, burst_times_s, args.seed)
        clips_df = _generate_clips_df(video_id, broadcaster_id, burst_times_s, args.seed)
        print(
            f"Setup complete: wav={wav_path.stat().st_size / (1 << 20):.1f} MiB "
            f"({wav_gen_s:.2f}s), chat={chat_df.height:,} rows, clips={clips_df.height} rows\n"
        )

        # 1. Chat replay JSON parsing (unprofiled wall clock; cProfile adds heavy call overhead)
        chat_json_bytes = _make_chat_json_bytes(
            chat_df, video_id, duration_s, args.chat_json_messages
        )
        salt = SecretStr("profile-synthetic-salt")
        t0 = time.perf_counter()
        replay = parse_chat_json(chat_json_bytes, video_id=video_id, salt=salt)
        timings.append(
            StageTiming(
                "parse_chat_json", time.perf_counter() - t0, f"{len(replay.messages):,} msgs"
            )
        )
        if args.profile_top > 0:
            _, _, profiles["parse_chat_json"] = _profile_call(
                lambda: parse_chat_json(chat_json_bytes, video_id=video_id, salt=salt),
                args.profile_top,
            )

        # 2. Standalone Polars Parquet IO (ingest uses PyArrow in autoclipper.ingest.vod)
        chat_pq = tmp / "chat.parquet"
        t0 = time.perf_counter()
        chat_df.write_parquet(chat_pq)
        timings.append(
            StageTiming(
                "polars_write_chat_parquet", time.perf_counter() - t0, f"{chat_df.height:,} rows"
            )
        )
        t0 = time.perf_counter()
        chat_df = pl.read_parquet(chat_pq)
        timings.append(
            StageTiming(
                "polars_read_chat_parquet", time.perf_counter() - t0, f"{chat_df.height:,} rows"
            )
        )

        # 3. Windows
        t0 = time.perf_counter()
        windows = make_windows(video_id, duration_s, muted_segments=[(1800.0, 120.0)])
        timings.append(
            StageTiming("make_windows", time.perf_counter() - t0, f"{windows.height:,} windows")
        )

        # 4. Chat features
        lexicon = load_lexicon(LEXICON_PATH)
        t0 = time.perf_counter()
        chat_feats = compute_chat_features(windows, chat_df, lexicon=lexicon)
        timings.append(
            StageTiming(
                "compute_chat_features",
                time.perf_counter() - t0,
                f"{windows.height:,} windows, {chat_df.height:,} msgs",
            )
        )
        if args.profile_top > 0:
            _, _, profiles["compute_chat_features"] = _profile_call(
                lambda: compute_chat_features(windows, chat_df, lexicon=lexicon),
                args.profile_top,
            )

        # 5. Audio features (whole + sub-stages)
        t0 = time.perf_counter()
        ff = frame_features(wav_path)
        timings.append(
            StageTiming("audio.frame_features", time.perf_counter() - t0, f"{len(ff.rms):,} frames")
        )
        t0 = time.perf_counter()
        onsets = detect_onsets(ff.flux)
        timings.append(
            StageTiming(
                "audio.detect_onsets", time.perf_counter() - t0, f"{int(onsets.sum())} onsets"
            )
        )
        t0 = time.perf_counter()
        audio_feats = compute_audio_features(windows, wav_path)
        timings.append(
            StageTiming(
                "compute_audio_features",
                time.perf_counter() - t0,
                f"{windows.height:,} windows",
            )
        )
        if args.profile_top > 0:
            # cProfile only sees the calling thread. When frame math runs on a pool, a serial pass
            # shows that math; the wall clock above stays the path the pipeline actually runs.
            workers = getattr(audio_features, "_MAX_WORKERS", 1)
            if isinstance(workers, int) and workers > 1:
                audio_features._MAX_WORKERS = 1
            try:
                _, _, prof_text = _profile_call(
                    lambda: compute_audio_features(windows, wav_path), args.profile_top
                )
            finally:
                if isinstance(workers, int) and workers > 1:
                    audio_features._MAX_WORKERS = workers
            profiles["compute_audio_features (serial)"] = prof_text

        # 6. Moments & window labeling
        t0 = time.perf_counter()
        moments_res = merge_clips_into_moments(clips_df)
        timings.append(
            StageTiming(
                "merge_clips_into_moments",
                time.perf_counter() - t0,
                f"{clips_df.height} clips -> {moments_res.moments.height} moments",
            )
        )
        t0 = time.perf_counter()
        labels_df = label_windows(
            windows,
            moments_res.moments,
            clips_df,
            broadcaster_id=broadcaster_id,
            split="train",
            mature=True,
        )
        timings.append(
            StageTiming("label_windows", time.perf_counter() - t0, f"{windows.height:,} windows")
        )
        if args.profile_top > 0:
            _, _, profiles["label_windows"] = _profile_call(
                lambda: label_windows(
                    windows,
                    moments_res.moments,
                    clips_df,
                    broadcaster_id=broadcaster_id,
                    split="train",
                    mature=True,
                ),
                args.profile_top,
            )

        # 7. Scoring (heuristics + trained models)
        features = (
            chat_feats.drop("feature_version")
            .join(audio_feats.drop("feature_version"), on="window_id")
            .join(
                labels_df.select("window_id", "label_pos", "train_eligible", "sample_weight"),
                on="window_id",
            )
        )
        t0 = time.perf_counter()
        scores_cl = ChatLoudness().score(features)
        timings.append(
            StageTiming(
                "score.chat_loudness", time.perf_counter() - t0, f"{features.height:,} rows"
            )
        )
        scorers_100 = random_scorers()
        t0 = time.perf_counter()
        for sc in scorers_100:
            sc.score(features)
        timings.append(
            StageTiming(
                "score.random_x100",
                time.perf_counter() - t0,
                f"100 seeds x {features.height:,} rows",
            )
        )

        train_rows = features.filter(pl.col("train_eligible"))
        n_pos = int(train_rows["label_pos"].sum()) if train_rows.height else 0
        if train_rows.height == 0 or n_pos in (0, train_rows.height):
            timings.append(
                StageTiming(
                    "train.skipped",
                    0.0,
                    "need both classes; widen the VOD or space the planted bursts",
                )
            )
        else:
            train_x = train_rows.drop("label_pos", "train_eligible", "sample_weight")
            train_y = train_rows["label_pos"]
            train_w = train_rows["sample_weight"]
            score_x = features.drop("label_pos", "train_eligible", "sample_weight")

            logreg = LogisticRegressionScorer()
            t0 = time.perf_counter()
            logreg.fit(train_x, train_y, weights=train_w)
            timings.append(StageTiming("train.logreg.fit", time.perf_counter() - t0))
            t0 = time.perf_counter()
            logreg.score(score_x)
            timings.append(StageTiming("score.logreg", time.perf_counter() - t0))

            lgbm = LightGBMScorer(n_estimators=100, seed=args.seed)
            t0 = time.perf_counter()
            lgbm.fit(train_x, train_y, weights=train_w)
            timings.append(StageTiming("train.lightgbm.fit", time.perf_counter() - t0))
            t0 = time.perf_counter()
            lgbm.score(score_x)
            timings.append(StageTiming("score.lightgbm", time.perf_counter() - t0))

        # 8. Candidate generation (with and without pause snapping)
        scored_windows = windows.select("window_id", "start_s", "end_s").join(
            scores_cl, on="window_id"
        )
        moment_inputs = [
            MomentInput(
                str(r["moment_id"]),
                float(r["start_s"]),
                float(r["end_s"]),
                tuple(str(c) for c in r["clip_ids"]),
            )
            for r in moments_res.moments.iter_rows(named=True)
        ]
        t0 = time.perf_counter()
        cands_no_snap = generate_candidates(
            scored_windows,
            duration_s=duration_s,
            features=features,
            moments=moment_inputs,
        )
        timings.append(
            StageTiming(
                "generate_candidates(no_snap)",
                time.perf_counter() - t0,
                f"{len(cands_no_snap)} candidates",
            )
        )

        t0 = time.perf_counter()
        cands_snap = generate_candidates(
            scored_windows,
            duration_s=duration_s,
            features=features,
            moments=moment_inputs,
            audio_path=wav_path,
        )
        timings.append(
            StageTiming(
                "generate_candidates(pause_snap)",
                time.perf_counter() - t0,
                f"{len(cands_snap)} candidates",
            )
        )
        if args.profile_top > 0:
            _, _, profiles["generate_candidates(pause_snap)"] = _profile_call(
                lambda: generate_candidates(
                    scored_windows,
                    duration_s=duration_s,
                    features=features,
                    moments=moment_inputs,
                    audio_path=wav_path,
                ),
                args.profile_top,
            )

        # 9. Export & Highlights path
        if not args.skip_export:
            short_wav = tmp / "export_src.wav"
            _write_long_wav(short_wav, args.video_duration_s, args.seed + 1, [20.0, 50.0, 75.0])
            src_mp4 = tmp / "export_src.mp4"
            run_command(
                video_command(
                    short_wav,
                    src_mp4,
                    args.video_duration_s,
                    VideoOptions(size=(1280, 720), frame_rate=30),
                ),
                timeout_s=180,
            )

            clip_runner = CountingRunner()
            clip_out = tmp / "clip_out.mp4"
            clip_start_s = 15.0 if args.video_duration_s >= 46.0 else 1.0
            clip_len_s = min(30.0, args.video_duration_s - clip_start_s - 1.0)
            t0 = time.perf_counter()
            render_clip(
                ClipSpec(
                    source=src_mp4,
                    interval=Interval(clip_start_s, clip_start_s + clip_len_s),
                    output=clip_out,
                    layout="center_crop",
                    captions=False,
                ),
                runner=clip_runner,
            )
            timings.append(
                StageTiming(
                    f"export.render_clip({clip_len_s:g}s)",
                    time.perf_counter() - t0,
                    clip_runner.summary(),
                )
            )

            n_seg = args.highlights_segments
            seg_len_s = 4.0
            lead_s = 2.0
            slack_s = 0.25
            span_s = args.video_duration_s - lead_s - seg_len_s - slack_s
            step = 0.0 if n_seg == 1 else span_s / (n_seg - 1)
            segments = [
                Segment(
                    f"c{i + 1:03d}",
                    round(lead_s + i * step, 3),
                    round(lead_s + i * step + seg_len_s, 3),
                )
                for i in range(n_seg)
            ]
            hl_runner = CountingRunner()
            hl_out = tmp / "highlights_out.mp4"
            t0 = time.perf_counter()
            render_highlights(
                HighlightsRequest(
                    source=src_mp4,
                    segments=segments,
                    output=hl_out,
                    order="chronological",
                    captions=False,
                ),
                runner=hl_runner,
            )
            timings.append(
                StageTiming(
                    f"export.render_highlights({n_seg}x4s)",
                    time.perf_counter() - t0,
                    hl_runner.summary(),
                )
            )

    print("=== Stage Timings ===")
    for item in timings:
        print(f"  {item.name:<34} {item.elapsed_s:8.3f} s   {item.detail}")

    if profiles:
        print("\n=== Top cProfile Hot Spots ===")
        for name in (
            "parse_chat_json",
            "compute_chat_features",
            "compute_audio_features (serial)",
            "label_windows",
            "generate_candidates(pause_snap)",
        ):
            if name in profiles:
                print(f"\n--- {name} ---")
                print(profiles[name])

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                [
                    {"name": t.name, "elapsed_s": round(t.elapsed_s, 6), "detail": t.detail}
                    for t in timings
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
