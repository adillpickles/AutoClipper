"""Measure the chat parse and write path of `autoclipper ingest` on a synthetic replay (#115).

Writes a fake TwitchDownloader 1.4.0 chat replay (made-up names and IDs only) to a temporary
directory, then times `parse_chat_json` and the ingest command's Parquet writer and reports the
process's peak memory. Each run should be a fresh process, so peak memory covers one run only.

Peak memory needs ``psutil`` (Windows peak working set, or peak RSS elsewhere); without it,
POSIX systems fall back to ``resource`` and Windows reports none.

Usage:
    uv run --with psutil python scripts/bench_chat_ingest.py --messages 200000

To compare with an older version of the code, put that version's ``src`` first on
``PYTHONPATH`` (for example a ``git worktree`` of ``origin/main``) and run the same command.
"""

import argparse
import gc
import importlib
import json
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from autoclipper.ingest import chat
from autoclipper.ingest.vod import _write_chat  # pyright: ignore[reportPrivateUsage]

VIDEO_ID = "9000000100"
_WORDS = ("lol", "W", "pog", "chat", "omg", "no", "way", "bro", "what", "clip", "LUL", "insane")
_EMOTES = (("Kappa", "25"), ("PogChamp", "305954156"), ("LUL", "425618"))


def write_replay(path: Path, n_messages: int, *, seed: int = 7, n_users: int = 20_000) -> None:
    """Write a chat replay shaped like TwitchDownloaderCLI's output, about 650 bytes a message."""
    rng = random.Random(seed)
    duration_s = max(3600, n_messages // 5)
    header = {
        "FileInfo": {
            "Version": {"Major": 1, "Minor": 4, "Patch": 0},
            "CreatedAt": "2026-10-02T09:00:00.0000000+00:00",
            "UpdatedAt": "0001-01-01T00:00:00",
        },
        "streamer": {"name": "Streamer_000", "login": "streamer_000", "id": 9000000000},
        "clipper": None,
        "video": {"title": "Synthetic stream", "id": VIDEO_ID, "start": 0, "end": duration_s},
    }
    with path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(header)[:-1] + ', "comments": [')
        for i in range(n_messages):
            file.write(("," if i else "") + json.dumps(_comment(rng, i, n_messages, n_users)))
        file.write('], "embeddedData": null}')


def _comment(rng: random.Random, i: int, n_messages: int, n_users: int) -> dict[str, object]:
    user = rng.randrange(n_users)
    text = " ".join(rng.choice(_WORDS) for _ in range(rng.randrange(1, 8)))
    if rng.random() < 0.15:
        text = f"@user_{rng.randrange(n_users):05d} {text}"
    fragments: list[dict[str, object]] = [{"text": text, "emoticon": None}]
    emoticons: list[dict[str, object]] = []
    body = text
    if rng.random() < 0.4:
        name, emote_id = rng.choice(_EMOTES)
        fragments += [
            {"text": " ", "emoticon": None},
            {"text": name, "emoticon": {"emoticon_id": emote_id}},
        ]
        begin = len(body) + 1
        emoticons.append({"_id": emote_id, "begin": begin, "end": begin + len(name)})
        body = f"{body} {name}"
    badges = [{"_id": "subscriber", "version": "12"}] if rng.random() < 0.5 else []
    if rng.random() < 0.02:
        badges.append({"_id": "moderator", "version": "1"})
    return {
        "_id": f"00000000-0000-4000-8000-{i:012d}",
        "created_at": "2026-10-01T18:00:00Z",
        "channel_id": "9000000000",
        "content_type": "video",
        "content_id": VIDEO_ID,
        "content_offset_seconds": round(i * max(3600, n_messages // 5) / n_messages, 3),
        "commenter": {
            "display_name": f"User_{user:05d}",
            "_id": str(9100000000 + user),
            "name": f"user_{user:05d}",
            "bio": None,
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "logo": None,
        },
        "message": {
            "body": body,
            "bits_spent": 0,
            "fragments": fragments,
            "user_badges": badges,
            "user_color": "#1E90FF",
            "emoticons": emoticons,
        },
    }


def _memory_mib() -> tuple[float | None, float | None]:
    """(current, peak) memory of this process in MiB, where it can be measured."""
    current = peak = None
    try:
        psutil: Any = importlib.import_module("psutil")
    except ImportError:
        pass
    else:
        info = psutil.Process().memory_info()
        current = info.rss / 2**20
        if hasattr(info, "peak_wset"):  # Windows
            peak = info.peak_wset / 2**20
    if peak is None and sys.platform != "win32":
        import resource

        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak = max_rss / (2**20 if sys.platform == "darwin" else 2**10)  # bytes on macOS, else KiB
    return current, peak


def main() -> None:
    """Write the replay, run the parse and the write once, and print the measurements."""
    parser = argparse.ArgumentParser(description="Time chat parse and write.")
    parser.add_argument("--messages", type=int, default=200_000)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        replay_path, parquet_path = Path(tmp) / "replay.json", Path(tmp) / "chat.parquet"
        write_replay(replay_path, args.messages)
        size_mib = replay_path.stat().st_size / 2**20
        gc.collect()
        base_mib, _ = _memory_mib()

        start = time.perf_counter()
        data = replay_path.read_bytes()  # as download_chat does
        replay = chat.parse_chat_json(data, video_id=VIDEO_ID, salt=SecretStr("bench-salt"))
        parsed = time.perf_counter()
        del data
        _write_chat(parquet_path, replay)
        written = time.perf_counter()
        _, peak_mib = _memory_mib()

        print(f"code: {Path(chat.__file__).parent.parent}")
        print(f"replay: {len(replay.messages):,} messages, {size_mib:.1f} MiB")
        print(f"parse_chat_json: {parsed - start:.2f} s")
        print(f"write chat Parquet: {written - parsed:.2f} s")
        print(f"total: {written - start:.2f} s")
        if peak_mib is None:
            print("peak memory: not measured (install psutil)")
        elif base_mib is None:
            print(f"peak memory: {peak_mib:.0f} MiB")
        else:
            print(f"peak memory: {peak_mib:.0f} MiB ({peak_mib - base_mib:.0f} MiB above start)")


if __name__ == "__main__":
    main()
