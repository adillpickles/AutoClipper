"""Tests for `autoclipper.ingest.media`: audio download (yt-dlp) and normalization (FFmpeg).

No test reaches the internet. Most replace yt-dlp with fake downloaders or run yt-dlp's real error
handling with a fake Twitch extractor. The HLS tests run the real yt-dlp against generated HLS
served by `hls_server` on 127.0.0.1 only (an ephemeral port, short timeouts). Tests that use
`make_test_media` or are marked `ffmpeg` run the real FFmpeg (tests/conftest.py).
"""

import gc
import io
import json
import os
import shutil
import subprocess
import wave
from array import array
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, Any

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError

from autoclipper.ffmpeg import (
    CommandResult,
    FFmpegCommand,
    FFmpegExitError,
    FFmpegOutputError,
    ffmpeg_command,
    probe,
    run_command,
)
from autoclipper.ingest.media import (
    AUDIO_FORMAT_SELECTOR,
    MAX_RETRY_SLEEP_S,
    AudioDownloadError,
    DownloadedMedia,
    DownloadIntegrityError,
    DownloadNetworkError,
    DownloadRequest,
    DownloadToolError,
    InvalidVideoIdError,
    NoAudioOnlyFormatError,
    VodAccessRestrictedError,
    VodUnavailableError,
    YtDlpDownloader,
    download_audio,
    normalize_audio,
    normalized_audio_path,
    prepare_vod_audio,
    raw_audio_dir,
    validate_video_id,
    vod_url,
    ytdlp_options,
)

if TYPE_CHECKING:
    from yt_dlp.extractor.common import _InfoDict

VIDEO_ID = "9000000001"
STEM = f"{VIDEO_ID}.download"
GENERATE_TIMEOUT_S = 60.0


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


# --- Video IDs and paths -----------------------------------------------------------------------


def test_valid_video_id_is_returned_unchanged() -> None:
    assert validate_video_id(VIDEO_ID) == VIDEO_ID


@pytest.mark.parametrize(
    "video_id",
    [
        "",
        "v9000000001",
        "12a",
        "../1",
        "1/2",
        "1\\2",
        "1.2",
        " 1",
        "1\n",
        "\uff11\uff12",
        "1" * 21,
        123,
    ],
)
def test_invalid_video_id_is_rejected(video_id: str) -> None:
    with pytest.raises(InvalidVideoIdError, match="invalid Twitch video ID"):
        validate_video_id(video_id)


def test_vod_url_and_paths_are_built_from_the_id(data_dir: Path) -> None:
    assert vod_url(VIDEO_ID) == f"https://www.twitch.tv/videos/{VIDEO_ID}"
    assert raw_audio_dir(data_dir) == data_dir / "raw" / "audio"
    assert normalized_audio_path(data_dir, VIDEO_ID) == data_dir / "interim/audio/9000000001.wav"


# --- Fake FFmpeg -------------------------------------------------------------------------------


def probe_output(
    *,
    format_name: str = "wav",
    codec_name: str = "pcm_s16le",
    sample_rate_hz: int = 16_000,
    channels: int = 1,
    duration_s: float = 3.0,
    audio: bool = True,
) -> bytes:
    stream = {
        "index": 0,
        "codec_type": "audio",
        "codec_name": codec_name,
        "sample_rate": str(sample_rate_hz),
        "channels": channels,
    }
    return json.dumps(
        {
            "format": {"format_name": format_name, "duration": str(duration_s)},
            "streams": [stream] if audio else [],
        }
    ).encode()


class FakeFFmpeg:
    """A fake `Runner`.

    ffprobe returns `output_probe` for WAVs (``.wav`` or ``.wav.part``) and `source_probe` for
    anything else. ffmpeg with ``-progress`` (the download check) reports `played_s`; any other
    ffmpeg command writes its output file.
    """

    def __init__(
        self,
        output_probe: bytes = probe_output(),
        source_probe: bytes | None = None,
        ffmpeg_returncode: int = 0,
        played_s: float = 4.0,
    ) -> None:
        self.output_probe = output_probe
        self.source_probe = output_probe if source_probe is None else source_probe
        self.ffmpeg_returncode = ffmpeg_returncode
        self.played_s = played_s
        self.commands: list[FFmpegCommand] = []

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        self.commands.append(command)
        if command.program == "ffprobe":
            is_wav = command.args[-1].endswith((".wav", ".wav.part"))
            return CommandResult(0, stdout=self.output_probe if is_wav else self.source_probe)
        if "-progress" in command.args:
            progress = f"out_time_us={round(self.played_s * 1e6)}\nprogress=end\n"
            return CommandResult(0, stdout=progress.encode())
        Path(command.args[-1]).write_bytes(b"wav")
        return CommandResult(self.ffmpeg_returncode, stderr=b"error")


def downloaded_probe(
    duration_s: float = 4.0, format_name: str = "mpegts", played_s: float | None = None
) -> FakeFFmpeg:
    """A fake FFmpeg for downloads: AAC audio of `duration_s` seconds in `format_name`, which
    plays for `played_s` seconds (default `duration_s`)."""
    return FakeFFmpeg(
        source_probe=probe_output(
            format_name=format_name, codec_name="aac", sample_rate_hz=48_000, duration_s=duration_s
        ),
        played_s=duration_s if played_s is None else played_s,
    )


# --- Downloading -------------------------------------------------------------------------------


class FakeDownloader:
    """Writes ``<output_dir>/<stem>.<ext>`` like yt-dlp and records each request."""

    def __init__(
        self, extension: str = "mp4", source: Path | None = None, duration_s: float | None = 4.0
    ) -> None:
        self.extension = extension
        self.source = source
        self.duration_s = duration_s
        self.requests: list[DownloadRequest] = []

    def __call__(self, request: DownloadRequest, /) -> DownloadedMedia:
        self.requests.append(request)
        path = request.output_dir / f"{request.stem}.{self.extension}"
        if self.source is None:
            path.write_bytes(b"audio")
        else:
            shutil.copyfile(self.source, path)
        return DownloadedMedia(path, self.duration_s)


def test_download_requests_the_vod_url_into_the_raw_audio_dir(data_dir: Path) -> None:
    downloader = FakeDownloader()

    path = download_audio(
        VIDEO_ID, data_dir=data_dir, downloader=downloader, runner=downloaded_probe()
    )

    assert downloader.requests == [
        DownloadRequest(vod_url(VIDEO_ID), raw_audio_dir(data_dir), STEM)
    ]
    # MPEG-TS is named .ts, whatever extension yt-dlp gave it.
    assert path == raw_audio_dir(data_dir) / f"{VIDEO_ID}.ts"
    assert path.read_bytes() == b"audio"
    assert sorted(p.name for p in path.parent.iterdir()) == [f"{VIDEO_ID}.ts"]


def test_mp4_download_keeps_the_extension_ytdlp_reports(data_dir: Path) -> None:
    runner = downloaded_probe(format_name="mov,mp4,m4a,3gp,3g2,mj2")

    path = download_audio(
        VIDEO_ID, data_dir=data_dir, downloader=FakeDownloader("m4a"), runner=runner
    )

    assert path.name == f"{VIDEO_ID}.m4a"


def test_complete_download_is_not_downloaded_again(data_dir: Path) -> None:
    downloader = FakeDownloader()
    first = download_audio(
        VIDEO_ID, data_dir=data_dir, downloader=downloader, runner=downloaded_probe()
    )

    second = download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert second == first
    assert len(downloader.requests) == 1


def test_partial_download_is_handed_back_to_the_downloader_to_resume(data_dir: Path) -> None:
    audio_dir = raw_audio_dir(data_dir)
    audio_dir.mkdir(parents=True)
    for leftover in (f"{STEM}.mp4.part", f"{STEM}.mp4.ytdl", f"{STEM}.mp4.part-Frag3"):
        (audio_dir / leftover).write_bytes(b"partial")
    downloader = FakeDownloader()

    download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader, runner=downloaded_probe())

    # The same stem, so yt-dlp finds and continues its .part/.ytdl files.
    assert [request.stem for request in downloader.requests] == [STEM]


def test_downloads_of_other_vods_are_not_mistaken_for_this_one(data_dir: Path) -> None:
    audio_dir = raw_audio_dir(data_dir)
    audio_dir.mkdir(parents=True)
    (audio_dir / f"{VIDEO_ID}0.mp4").write_bytes(b"other")
    downloader = FakeDownloader()

    download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader, runner=downloaded_probe())

    assert len(downloader.requests) == 1


def test_several_complete_downloads_are_an_error(data_dir: Path) -> None:
    audio_dir = raw_audio_dir(data_dir)
    audio_dir.mkdir(parents=True)
    for name in (f"{VIDEO_ID}.mp4", f"{VIDEO_ID}.m4a"):
        (audio_dir / name).write_bytes(b"audio")

    with pytest.raises(AudioDownloadError, match="several downloads"):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=FakeDownloader())


def test_invalid_video_id_fails_before_anything_is_created(data_dir: Path) -> None:
    downloader = FakeDownloader()

    with pytest.raises(InvalidVideoIdError):
        download_audio("../9000000001", data_dir=data_dir, downloader=downloader)

    assert downloader.requests == []
    assert not data_dir.exists()


@pytest.mark.parametrize(
    "returned",
    [
        "missing",  # the file doesn't exist
        "outside",  # a file in another directory
        "wrong-stem",  # not <stem>.<ext>
        "odd-extension",  # extension with a dot
    ],
)
def test_unexpected_downloader_result_is_a_tool_error(data_dir: Path, returned: str) -> None:
    def downloader(request: DownloadRequest, /) -> DownloadedMedia:
        paths = {
            "missing": request.output_dir / f"{request.stem}.mp4",
            "outside": data_dir / f"{request.stem}.mp4",
            "wrong-stem": request.output_dir / "other.mp4",
            "odd-extension": request.output_dir / f"{request.stem}.f1.mp4",
        }
        path = paths[returned]
        if returned != "missing":
            path.write_bytes(b"audio")
        return DownloadedMedia(path, None)

    with pytest.raises(DownloadToolError, match="the downloader returned"):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert not (raw_audio_dir(data_dir) / f"{VIDEO_ID}.mp4").exists()


def test_unexpected_exception_from_the_downloader_is_a_tool_error(data_dir: Path) -> None:
    def downloader(request: DownloadRequest, /) -> DownloadedMedia:
        raise KeyError("value")

    with pytest.raises(DownloadToolError) as caught:
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert isinstance(caught.value.__cause__, KeyError)
    assert caught.value.video_id == VIDEO_ID


@pytest.mark.parametrize(
    ("reported_s", "played_s"),
    [(4.0, 6.037), (4.0, 1.0), (7200.0, 7180.0)],
    ids=["repeated-segments", "truncated", "long-vod-20s-short"],
)
def test_download_that_plays_for_the_wrong_duration_is_deleted(
    data_dir: Path, reported_s: float, played_s: float
) -> None:
    audio_dir = raw_audio_dir(data_dir)
    audio_dir.mkdir(parents=True)
    (audio_dir / f"{STEM}.mp4.part-Frag3.part").write_bytes(b"leftover")
    # The container duration matches: MPEG-TS takes it from the first and last timestamps.
    runner = downloaded_probe(reported_s, played_s=played_s)

    with pytest.raises(DownloadIntegrityError, match="has been deleted") as caught:
        download_audio(
            VIDEO_ID,
            data_dir=data_dir,
            downloader=FakeDownloader(duration_s=reported_s),
            runner=runner,
        )

    assert f"plays for {played_s:.3f} s, but the source reports {reported_s:.3f} s" in str(
        caught.value
    )
    assert list(audio_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("reported_s", "played_s"),
    [(4.0, 4.032), (4.0, 5.9), (None, 99.0), (7200.0, 7186.0)],
    ids=["aac-priming", "within-2s", "duration-unknown", "within-0.2-percent"],
)
def test_download_within_the_duration_tolerance_is_kept(
    data_dir: Path, reported_s: float | None, played_s: float
) -> None:
    runner = downloaded_probe(4.0, played_s=played_s)

    path = download_audio(
        VIDEO_ID, data_dir=data_dir, downloader=FakeDownloader(duration_s=reported_s), runner=runner
    )

    assert path.is_file()
    played_checks = [command for command in runner.commands if "-progress" in command.args]
    assert len(played_checks) == (0 if reported_s is None else 1)


@pytest.mark.parametrize(
    "runner",
    [
        FakeFFmpeg(source_probe=probe_output(format_name="mpegts", audio=False)),
        FakeFFmpeg(source_probe=b"not json"),
    ],
    ids=["no-audio-stream", "unreadable"],
)
def test_download_without_readable_audio_is_deleted(data_dir: Path, runner: FakeFFmpeg) -> None:
    with pytest.raises(DownloadIntegrityError):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=FakeDownloader(), runner=runner)

    assert list(raw_audio_dir(data_dir).iterdir()) == []


def test_ytdlp_options_resume_retry_and_never_skip_fragments(tmp_path: Path) -> None:
    options = ytdlp_options(DownloadRequest("https://example.invalid/v", tmp_path, STEM))

    assert options["paths"] == {"home": str(tmp_path)}
    assert options["outtmpl"] == {"default": f"{STEM}.%(ext)s"}
    assert options["continuedl"] is True
    assert options["nopart"] is False
    assert options["skip_unavailable_fragments"] is False
    assert options["color"] == "never"
    assert options["retries"] == options["fragment_retries"] == 10
    sleep_functions = options["retry_sleep_functions"]
    assert isinstance(sleep_functions, dict)
    assert [sleep_functions["fragment"](n) for n in (0, 1, 3, 20)] == [1, 2, 8, MAX_RETRY_SLEEP_S]


def test_ytdlp_options_download_audio_only_without_ffmpeg(tmp_path: Path) -> None:
    options = ytdlp_options(DownloadRequest("https://example.invalid/v", tmp_path, STEM))

    assert options["format"] == AUDIO_FORMAT_SELECTOR == "bestaudio"
    assert options["external_downloader"] == {"default": "native"}
    assert options["fixup"] == "never"
    location = options["ffmpeg_location"]
    assert isinstance(location, str)
    assert not Path(location).exists()


# --- yt-dlp format selection and error mapping, without network ----------------------------------

# How Twitch's formats look after yt-dlp's extractor (shape from yt_dlp/extractor/twitch.py and
# its m3u8 parser); the URLs are never fetched.
_AUDIO_ONLY = {"format_id": "Audio_Only", "vcodec": "none", "acodec": "mp4a.40.2", "tbr": 160.0}
_VIDEO_160P = {"format_id": "160p30", "vcodec": "avc1.4D401F", "acodec": "mp4a.40.2", "height": 160}
_VIDEO_1080P = {
    "format_id": "1080p60",
    "vcodec": "avc1.64002A",
    "acodec": "mp4a.40.2",
    "height": 1080,
}
_STORYBOARD = {"format_id": "sb0", "vcodec": "none", "acodec": "none", "ext": "mhtml"}


class FakeTwitchVodIE(InfoExtractor):
    """Stands in for yt-dlp's Twitch VOD extractor; `scenario` returns info or raises."""

    _VALID_URL = r"https://www\.twitch\.tv/videos/(?P<id>\d+)"

    def __init__(self, scenario: Callable[["FakeTwitchVodIE", str], "_InfoDict"]) -> None:
        super().__init__()
        self.scenario = scenario

    def _real_extract(self, url: str) -> "_InfoDict":
        return self.scenario(self, self._match_id(url))


def formats_info(*formats: Mapping[str, object]) -> Callable[[InfoExtractor, str], "_InfoDict"]:
    def scenario(_ie: InfoExtractor, video_id: str) -> "_InfoDict":
        full = [
            {"url": f"https://example.invalid/{f['format_id']}", "ext": "mp4", **f} for f in formats
        ]
        return {"id": f"v{video_id}", "title": "title", "formats": full}

    return scenario


def run_ytdlp(scenario: Callable[..., "_InfoDict"], *, download: bool) -> object:
    """Run yt-dlp's real YoutubeDL with our options and only the fake extractor."""
    request = DownloadRequest(vod_url(VIDEO_ID), Path("unused"), STEM)
    ydl = YoutubeDL(ytdlp_options(request), auto_init=False)  # pyright: ignore[reportArgumentType]
    ydl.add_info_extractor(FakeTwitchVodIE(scenario))
    return ydl.extract_info(request.url, download=download)


def test_format_selector_picks_the_audio_only_format() -> None:
    scenario = formats_info(_VIDEO_160P, _AUDIO_ONLY, _VIDEO_1080P, _STORYBOARD)

    info = run_ytdlp(scenario, download=False)

    assert isinstance(info, dict)
    assert info["format_id"] == "Audio_Only"


@pytest.mark.parametrize(
    "formats",
    [(_VIDEO_1080P, _VIDEO_160P, _STORYBOARD), (_STORYBOARD,)],
    ids=["video-only", "storyboard-only"],
)
def test_vod_without_an_audio_only_format_is_a_typed_error_not_a_video_download(
    data_dir: Path, formats: tuple[Mapping[str, object], ...]
) -> None:
    def downloader(_request: DownloadRequest, /) -> DownloadedMedia:
        run_ytdlp(formats_info(*formats), download=False)  # selection only: nothing to fetch
        raise AssertionError("the selection should fail")

    with pytest.raises(NoAudioOnlyFormatError, match="offers no audio-only format"):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert list(raw_audio_dir(data_dir).iterdir()) == []


def http_error(status: int, reason: str) -> HTTPError:
    return HTTPError(Response(BytesIO(b"{}"), "https://example.invalid", {}, status, reason))


def raises_from(inner: Exception, message: str) -> Callable[[InfoExtractor, str], "_InfoDict"]:
    # Like InfoExtractor._download_webpage: a request error re-raised as an ExtractorError.
    def scenario(_ie: InfoExtractor, _video_id: str) -> "_InfoDict":
        try:
            raise inner
        except Exception as exc:
            raise ExtractorError(f"{message}: {exc}", cause=exc) from exc

    return scenario


def subscriber_only(ie: InfoExtractor, _video_id: str) -> "_InfoDict":
    # yt_dlp/extractor/twitch.py, _extract_twitch_m3u8_formats, for an anonymous request.
    try:
        raises_from(http_error(403, "Forbidden"), "Unable to download m3u8 information")(ie, "")
    except ExtractorError:
        ie.raise_login_required(
            "You must be logged into an account that has access to this subscriber-only content"
        )
    raise AssertionError("unreachable")


def raises(message: str, *, expected: bool = False) -> Callable[[InfoExtractor, str], "_InfoDict"]:
    def scenario(_ie: InfoExtractor, _video_id: str) -> "_InfoDict":
        raise ExtractorError(message, expected=expected)

    return scenario


def geo_restricted(ie: InfoExtractor, _video_id: str) -> "_InfoDict":
    ie.raise_geo_restricted()
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        pytest.param(
            raises(f"Video {VIDEO_ID} does not exist", expected=True),
            VodUnavailableError,
            id="deleted",
        ),
        pytest.param(geo_restricted, VodUnavailableError, id="geo-restricted"),
        pytest.param(
            raises_from(http_error(404, "Not Found"), "Unable to download m3u8 information"),
            VodUnavailableError,
            id="http-404",
        ),
        pytest.param(subscriber_only, VodAccessRestrictedError, id="subscriber-only-anonymous"),
        pytest.param(
            raises("Your account does not have access to this subscriber-only content"),
            VodAccessRestrictedError,
            id="subscriber-only-logged-in",
        ),
        pytest.param(
            raises_from(TransportError("timed out"), "Unable to download JSON metadata"),
            DownloadNetworkError,
            id="timeout",
        ),
        pytest.param(
            raises_from(http_error(503, "Service Unavailable"), "Unable to download webpage"),
            DownloadNetworkError,
            id="http-503",
        ),
        pytest.param(raises("Unable to extract access token"), DownloadToolError, id="extractor"),
    ],
)
def test_ytdlp_failures_become_typed_errors(
    data_dir: Path, scenario: Callable[..., "_InfoDict"], expected: type[AudioDownloadError]
) -> None:
    def downloader(_request: DownloadRequest, /) -> DownloadedMedia:
        run_ytdlp(scenario, download=True)
        raise AssertionError("the scenario should fail")

    with pytest.raises(AudioDownloadError) as caught:
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert type(caught.value) is expected
    assert caught.value.video_id == VIDEO_ID
    assert VIDEO_ID in str(caught.value)


@pytest.mark.parametrize(
    "message",
    [
        # yt_dlp/downloader/fragment.py and downloader/common.py report these without the
        # HTTP error attached.
        "ERROR: fragment 12 not found, unable to continue",
        "ERROR: \r[download] Got error: HTTP Error 503: Service Unavailable. "
        "Giving up after 10 retries",
    ],
    ids=["fragment-not-found", "retries-exhausted"],
)
def test_message_only_download_failures_are_network_errors(data_dir: Path, message: str) -> None:
    def downloader(_request: DownloadRequest, /) -> DownloadedMedia:
        raise DownloadError(message)

    with pytest.raises(DownloadNetworkError, match="run again to resume"):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)


def test_error_message_is_one_bounded_line(data_dir: Path) -> None:
    def downloader(_request: DownloadRequest, /) -> DownloadedMedia:
        raise ExtractorError("Unable to extract\n" + "x" * 5000)

    with pytest.raises(DownloadToolError) as caught:
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    message = str(caught.value)
    assert "\n" not in message
    assert len(message) < 1200
    assert message.endswith("...")


def test_ytdlp_downloader_fetches_a_local_file_once(
    make_test_media: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A file:// URL exercises the real downloader end to end without network.
    source = make_test_media("source.m4a", video_size=None)
    request = DownloadRequest(source.as_uri(), tmp_path / "out", STEM)
    request.output_dir.mkdir()
    downloader = YtDlpDownloader({"enable_file_urls": True})

    media = downloader(request)
    modified = media.path.stat().st_mtime_ns
    again = downloader(request)

    assert media.path == request.output_dir / f"{STEM}.m4a"
    assert media.path.read_bytes() == source.read_bytes()
    assert again == media
    assert media.path.stat().st_mtime_ns == modified  # complete files are not fetched again
    assert capsys.readouterr().out == ""  # yt-dlp's output goes to logging


def test_ytdlp_downloader_rejects_a_directory_that_yt_dlp_would_expand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_TEST_ELSEWHERE", "elsewhere")
    output_dir = tmp_path / "$AUTOCLIPPER_TEST_ELSEWHERE"

    with pytest.raises(ValueError, match="expand environment variables"):
        YtDlpDownloader()(DownloadRequest("https://example.invalid/v", output_dir, STEM))


# --- Real yt-dlp against local HLS -------------------------------------------------------------

HLS_DURATION_S = 4.0


@dataclass
class HlsServer:
    """Generated HLS audio (a master playlist with CODECS, like Twitch's) on 127.0.0.1.

    Attributes:
        url: The master playlist.
        segments: The content of each MPEG-TS segment, in playlist order. A correct download is
            their concatenation (yt-dlp's HLS downloader appends them unchanged).
        failing: Request paths answered with HTTP 503, e.g. ``"/seg1.ts"``.
        requests: Every request path, in order.
    """

    url: str
    segments: list[bytes]
    failing: set[str] = field(default_factory=set)
    requests: list[str] = field(default_factory=list)


@pytest.fixture
def hls_server(tmp_path: Path) -> Iterator[HlsServer]:
    web = tmp_path / "web"
    web.mkdir()
    tone = f"sine=frequency=440:sample_rate=48000:duration={HLS_DURATION_S}"
    command = ffmpeg_command(
        *("-f", "lavfi", "-i", tone, "-ac", "2", "-c:a", "aac"),
        *("-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod"),
        *("-master_pl_name", "master.m3u8", "-hls_segment_filename", web / "seg%d.ts"),
        web / "audio.m3u8",
    )
    run_command(command, timeout_s=GENERATE_TIMEOUT_S)
    segments = sorted(web.glob("seg*.ts"), key=lambda path: int(path.stem.removeprefix("seg")))
    failing: set[str] = set()
    requests: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            if self.path in failing:
                self.send_error(503, "Synthetic failure")
                return
            super().do_GET()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(web)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/master.m3u8"
        yield HlsServer(url, [path.read_bytes() for path in segments], failing, requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def hls_downloader(hls: HlsServer) -> Callable[[DownloadRequest], DownloadedMedia]:
    """The real YtDlpDownloader, fetching `hls` instead of Twitch, with fast retries."""
    real = YtDlpDownloader(
        {
            "retries": 1,
            "fragment_retries": 1,
            "socket_timeout": 5,
            "retry_sleep_functions": {"http": lambda n: 0, "fragment": lambda n: 0},
        }
    )

    def download(request: DownloadRequest, /) -> DownloadedMedia:
        return real(replace(request, url=hls.url))

    return download


def open_files_in(directory: Path) -> list[str]:
    names: list[str] = []
    for obj in gc.get_objects():
        if isinstance(obj, io.IOBase) and not obj.closed:
            name = getattr(obj, "name", None)
            if isinstance(name, str) and Path(name).parent == directory:
                names.append(name)
    return names


@pytest.mark.ffmpeg
def test_hls_download_resumes_after_a_failed_fragment(
    hls_server: HlsServer, data_dir: Path
) -> None:
    download = hls_downloader(hls_server)
    hls_server.failing.add("/seg2.ts")
    with pytest.raises(DownloadNetworkError):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=download)
    hls_server.failing.clear()
    hls_server.requests.clear()

    path = download_audio(VIDEO_ID, data_dir=data_dir, downloader=download)

    assert "/seg0.ts" not in hls_server.requests  # resumed, not restarted
    assert path.read_bytes() == b"".join(hls_server.segments)


@pytest.mark.ffmpeg
@pytest.mark.parametrize("checkpoint", ["corrupt", "missing"])
def test_partial_hls_download_without_a_usable_checkpoint_restarts_instead_of_appending(
    hls_server: HlsServer, data_dir: Path, checkpoint: str
) -> None:
    download = hls_downloader(hls_server)
    hls_server.failing.add("/seg2.ts")
    with pytest.raises(DownloadNetworkError):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=download)
    ytdl = raw_audio_dir(data_dir) / f"{STEM}.mp4.ytdl"
    if checkpoint == "corrupt":
        ytdl.write_text("{", encoding="utf-8")  # interrupted while yt-dlp rewrote it
    else:
        ytdl.unlink()  # e.g. yt-dlp finished, deleted it, then failed to rename the .part file
    hls_server.failing.clear()

    path = download_audio(VIDEO_ID, data_dir=data_dir, downloader=download)

    # yt-dlp alone appends the restarted download to the old .part file: segments 0 and 1
    # twice, 6.04 s of audio (the container duration still says 4.03 s).
    assert path.read_bytes() == b"".join(hls_server.segments)


@pytest.mark.ffmpeg
def test_failed_hls_download_leaves_no_open_files_while_its_error_is_kept(
    hls_server: HlsServer, data_dir: Path
) -> None:
    download = hls_downloader(hls_server)
    hls_server.failing.add("/seg1.ts")
    with pytest.raises(DownloadNetworkError) as kept:
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=download)

    assert open_files_in(raw_audio_dir(data_dir)) == []
    hls_server.failing.clear()
    # On Windows the retry failed to rename the .part file while the error was kept.
    path = download_audio(VIDEO_ID, data_dir=data_dir, downloader=download)
    assert path.read_bytes() == b"".join(hls_server.segments)
    assert kept.value.video_id == VIDEO_ID


@pytest.mark.ffmpeg
def test_exhausted_hls_fragment_retries_are_a_network_error(
    hls_server: HlsServer, data_dir: Path
) -> None:
    hls_server.failing.add("/seg1.ts")

    with pytest.raises(DownloadNetworkError, match="HTTP Error 503"):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=hls_downloader(hls_server))


@pytest.mark.ffmpeg
def test_finished_but_unrenamed_download_is_checked_and_kept_without_downloading_again(
    hls_server: HlsServer, data_dir: Path
) -> None:
    download = hls_downloader(hls_server)
    first = download_audio(VIDEO_ID, data_dir=data_dir, downloader=download)
    first.rename(first.with_name(f"{STEM}.mp4"))  # as if the rename had failed
    hls_server.requests.clear()

    path = download_audio(VIDEO_ID, data_dir=data_dir, downloader=download)

    assert path == first
    assert not any(request.endswith(".ts") for request in hls_server.requests)


@pytest.mark.ffmpeg
def test_download_with_repeated_segments_is_deleted(hls_server: HlsServer, data_dir: Path) -> None:
    # What a broken resume leaves: segments 0 and 1, then all of them again.
    repeated = data_dir / "repeated.ts"
    repeated.parent.mkdir(parents=True)
    repeated.write_bytes(b"".join(hls_server.segments[:2] + hls_server.segments))
    assert probe(repeated).duration_s == pytest.approx(HLS_DURATION_S, abs=0.1)  # looks fine
    downloader = FakeDownloader("mp4", source=repeated, duration_s=HLS_DURATION_S)

    with pytest.raises(DownloadIntegrityError, match=r"plays for 6\.0\d\d s"):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert list(raw_audio_dir(data_dir).iterdir()) == []


@pytest.mark.ffmpeg
def test_hls_download_starts_no_ffmpeg_and_keeps_mpegts_for_normalization(
    hls_server: HlsServer, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    # yt-dlp caches the FFmpeg version per process; start empty so its check would run here.
    monkeypatch.setattr(FFmpegPostProcessor, "_version_cache", {None: None})
    started: list[str] = []
    real_init = subprocess.Popen.__init__

    # Any: the arguments are forwarded to Popen unchanged.
    def recording_init(self: subprocess.Popen[bytes], *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        command = args[0] if args else kwargs["args"]
        program = command if isinstance(command, str | bytes | os.PathLike) else next(iter(command))
        started.append(Path(os.fsdecode(program)).stem)
        real_init(self, *args, **kwargs)

    output_dir = raw_audio_dir(data_dir)
    output_dir.mkdir(parents=True)
    with monkeypatch.context() as patched:
        patched.setattr(subprocess.Popen, "__init__", recording_init)
        media = hls_downloader(hls_server)(DownloadRequest("unused", output_dir, STEM))

    assert [name for name in started if name in {"ffmpeg", "ffprobe"}] == []
    assert probe(media.path).format_name == "mpegts"  # not remuxed by yt-dlp
    path = download_audio(VIDEO_ID, data_dir=data_dir, downloader=hls_downloader(hls_server))
    assert path.name == f"{VIDEO_ID}.ts"
    audio = normalize_audio(path, VIDEO_ID, data_dir=data_dir)
    assert audio.duration_s == pytest.approx(HLS_DURATION_S, abs=0.1)


# --- Normalizing -------------------------------------------------------------------------------


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "source.mp4"
    path.write_bytes(b"media")
    return path


def test_normalize_converts_to_16_khz_mono_pcm_via_a_partial_file(
    source: Path, data_dir: Path
) -> None:
    runner = FakeFFmpeg()

    audio = normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=runner)

    output = normalized_audio_path(data_dir, VIDEO_ID)
    partial = output.with_name(f"{output.name}.part")
    check_source, convert, check_output = runner.commands
    assert check_source.args[-1] == os.fspath(source.absolute())
    assert convert.args[-1] == os.fspath(partial.absolute())
    args = " ".join(convert.args[:-1])
    assert f"-i {source.absolute()} -map 0:a:0" in args
    assert "-af aresample=async=1:first_pts=0 -ac 1 -ar 16000 -c:a pcm_s16le -f wav" in args
    assert check_output.args[-1] == os.fspath(partial.absolute())
    assert audio.path == output
    assert audio.duration_s == pytest.approx(3.0)
    assert output.read_bytes() == b"wav"
    assert not partial.exists()


@pytest.mark.parametrize(
    "output_probe",
    [probe_output(sample_rate_hz=44_100), probe_output(channels=2)],
    ids=["sample-rate", "channels"],
)
def test_wrong_output_format_is_an_error_and_leaves_no_file(
    source: Path, data_dir: Path, output_probe: bytes
) -> None:
    runner = FakeFFmpeg(output_probe, source_probe=probe_output())
    with pytest.raises(FFmpegOutputError, match="16000 Hz mono pcm_s16le"):
        normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=runner)

    assert list(normalized_audio_path(data_dir, VIDEO_ID).parent.iterdir()) == []


def test_output_shorter_than_the_source_is_an_error_and_leaves_no_file(
    source: Path, data_dir: Path
) -> None:
    runner = FakeFFmpeg(probe_output(duration_s=4.9), source_probe=probe_output(duration_s=10.0))

    with pytest.raises(FFmpegOutputError, match=r"could not be decoded"):
        normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=runner)

    assert list(normalized_audio_path(data_dir, VIDEO_ID).parent.iterdir()) == []


def test_source_without_audio_is_an_error_before_converting(source: Path, data_dir: Path) -> None:
    runner = FakeFFmpeg(source_probe=probe_output(format_name="mpegts", audio=False))

    with pytest.raises(FFmpegOutputError, match="has no audio stream"):
        normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=runner)

    assert [command.program for command in runner.commands] == ["ffprobe"]


def test_ffmpeg_failure_leaves_no_partial_file(source: Path, data_dir: Path) -> None:
    with pytest.raises(FFmpegExitError):
        normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=FakeFFmpeg(ffmpeg_returncode=1))

    assert list(normalized_audio_path(data_dir, VIDEO_ID).parent.iterdir()) == []


def test_existing_output_is_checked_and_reused_unless_forced(source: Path, data_dir: Path) -> None:
    normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=FakeFFmpeg())
    runner = FakeFFmpeg()

    reused = normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=runner)
    assert [command.program for command in runner.commands] == ["ffprobe", "ffprobe"]

    runner.commands.clear()
    normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=runner, force=True)
    assert [command.program for command in runner.commands] == ["ffprobe", "ffmpeg", "ffprobe"]
    assert reused.duration_s == pytest.approx(3.0)


def test_existing_output_that_fails_the_checks_is_converted_again(
    source: Path, data_dir: Path
) -> None:
    output = normalized_audio_path(data_dir, VIDEO_ID)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"old")
    runner = FakeFFmpeg(probe_output(duration_s=1.0), source_probe=probe_output(duration_s=3.0))
    with pytest.raises(FFmpegOutputError):
        normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=runner)

    assert [command.program for command in runner.commands] == [
        "ffprobe",
        "ffprobe",
        "ffmpeg",
        "ffprobe",
    ]


def test_missing_source_is_an_error(data_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        normalize_audio(tmp_path / "missing.mp4", VIDEO_ID, data_dir=data_dir, runner=FakeFFmpeg())


@pytest.mark.parametrize(
    ("name", "video_size", "sample_rate_hz"),
    [("tone.m4a", None, 44_100), ("tone.m4a", None, 48_000), ("tone.mp4", (64, 36), 48_000)],
    ids=["m4a-44.1kHz", "m4a-48kHz", "mp4-with-video-48kHz"],
)
def test_normalize_real_tone_to_16_khz_mono_keeping_its_duration(
    make_test_media: Callable[..., Path],
    data_dir: Path,
    name: str,
    video_size: tuple[int, int] | None,
    sample_rate_hz: int,
) -> None:
    tone = make_test_media(
        name, duration_s=3.0, video_size=video_size, sample_rate_hz=sample_rate_hz, channels=2
    )
    source_duration_s = probe(tone).duration_s

    audio = normalize_audio(tone, VIDEO_ID, data_dir=data_dir)

    info = probe(audio.path)
    (stream,) = info.streams
    assert (stream.codec_type, stream.codec_name) == ("audio", "pcm_s16le")
    assert (info.audio_streams[0].sample_rate_hz, info.audio_streams[0].channels) == (16_000, 1)
    assert audio.duration_s == pytest.approx(info.duration_s)
    assert audio.duration_s == pytest.approx(source_duration_s, abs=0.05)
    assert audio.duration_s == pytest.approx(3.0, abs=0.05)


def first_loud_sample_s(wav_path: Path, threshold: int = 3000) -> float:
    with wave.open(os.fspath(wav_path), "rb") as wav:
        rate = wav.getframerate()
        samples = array("h", wav.readframes(wav.getnframes()))
    return next(i for i, sample in enumerate(samples) if abs(sample) > threshold) / rate


@pytest.mark.ffmpeg
def test_normalize_keeps_the_timeline_across_a_timestamp_gap(
    tmp_path: Path, data_dir: Path
) -> None:
    # Silence with a beep at 3.2 s; the packets between 1 s and 3 s are dropped but the
    # timestamps kept, so the source is 4 s long with a 2 s hole.
    gap = tmp_path / "gap.mp4"
    beep = "aevalsrc=if(between(t\\,3.2\\,3.4)\\,0.5*sin(2*PI*440*t)\\,0):s=48000:d=4"
    command = ffmpeg_command(
        *("-f", "lavfi", "-i", beep, "-af", "aselect='not(between(t,1,3))'", "-c:a", "aac"), gap
    )
    run_command(command, timeout_s=GENERATE_TIMEOUT_S)

    audio = normalize_audio(gap, VIDEO_ID, data_dir=data_dir)

    # Without filling the gap the WAV is 2 s long and the beep moves to 1.2 s.
    assert audio.duration_s == pytest.approx(4.0, abs=0.05)
    assert first_loud_sample_s(audio.path) == pytest.approx(3.2, abs=0.05)


@pytest.mark.ffmpeg
def test_normalize_rejects_a_truncated_source_instead_of_keeping_a_short_wav(
    tmp_path: Path, data_dir: Path
) -> None:
    tone = tmp_path / "tone.mp4"
    command = ffmpeg_command(
        *("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=10", "-ac", "2"),
        *("-c:a", "aac", "-movflags", "+faststart"),
        tone,
    )
    run_command(command, timeout_s=GENERATE_TIMEOUT_S)
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(tone.read_bytes()[: tone.stat().st_size // 2])

    # FFmpeg decodes about 4.9 s, reports the bad data and still exits with 0.
    with pytest.raises(FFmpegOutputError, match="could not be decoded"):
        normalize_audio(truncated, VIDEO_ID, data_dir=data_dir)

    assert list(normalized_audio_path(data_dir, VIDEO_ID).parent.iterdir()) == []


def test_prepare_downloads_then_normalizes(
    make_test_media: Callable[..., Path], data_dir: Path
) -> None:
    source = make_test_media("tone.m4a", video_size=None)
    downloader = FakeDownloader("m4a", source=source, duration_s=1.0)

    audio = prepare_vod_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert (raw_audio_dir(data_dir) / f"{VIDEO_ID}.m4a").is_file()
    assert audio.path == normalized_audio_path(data_dir, VIDEO_ID)
    assert audio.duration_s == pytest.approx(1.0, abs=0.05)


# --- Live --------------------------------------------------------------------------------------

LIVE_VOD_ENV = "AUTOCLIPPER_LIVE_AUDIO_VOD_ID"


@pytest.mark.live
def test_live_download_and_normalize_a_real_vod(tmp_path: Path) -> None:
    """Download and normalize one real, public, short VOD.

    Run this only after real-data use is authorized for the VOD's source
    (docs/data/data-governance.md, OQ-17), with ``-m live`` and the VOD's ID in
    ``AUTOCLIPPER_LIVE_AUDIO_VOD_ID``. It downloads the whole VOD's audio into ``tmp_path``.
    """
    video_id = os.environ.get(LIVE_VOD_ENV, "").strip()
    if not video_id:
        pytest.skip(f"set {LIVE_VOD_ENV} to an authorized public VOD's ID to run this test")

    audio = prepare_vod_audio(video_id, data_dir=tmp_path / "data")

    assert audio.duration_s > 0
