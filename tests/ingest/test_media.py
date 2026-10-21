"""Tests for `autoclipper.ingest.media`: audio download (yt-dlp) and normalization (FFmpeg).

No test touches the network. yt-dlp is replaced by fake downloaders; error mapping runs through
yt-dlp's real error handling with a fake Twitch extractor in place of the real one. Tests that use
`make_test_media` run the real FFmpeg (tests/conftest.py).
"""

import json
import os
import shutil
from collections.abc import Callable, Mapping
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import ExtractorError

from autoclipper.ffmpeg import CommandResult, FFmpegCommand, FFmpegExitError, FFmpegOutputError
from autoclipper.ingest.media import (
    DEFAULT_FORMAT_SELECTOR,
    MAX_RETRY_SLEEP_S,
    AudioDownloadError,
    DownloadNetworkError,
    DownloadRequest,
    DownloadToolError,
    InvalidVideoIdError,
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


# --- Downloading -------------------------------------------------------------------------------


class FakeDownloader:
    """Writes ``<output_dir>/<stem>.<ext>`` like yt-dlp and records each request."""

    def __init__(self, extension: str = "mp4", source: Path | None = None) -> None:
        self.extension = extension
        self.source = source
        self.requests: list[DownloadRequest] = []

    def __call__(self, request: DownloadRequest, /) -> Path:
        self.requests.append(request)
        path = request.output_dir / f"{request.stem}.{self.extension}"
        if self.source is None:
            path.write_bytes(b"audio")
        else:
            shutil.copyfile(self.source, path)
        return path


def test_download_requests_the_vod_url_into_the_raw_audio_dir(data_dir: Path) -> None:
    downloader = FakeDownloader()

    path = download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert downloader.requests == [
        DownloadRequest(vod_url(VIDEO_ID), raw_audio_dir(data_dir), STEM, DEFAULT_FORMAT_SELECTOR)
    ]
    assert path == raw_audio_dir(data_dir) / f"{VIDEO_ID}.mp4"
    assert path.read_bytes() == b"audio"
    assert sorted(p.name for p in path.parent.iterdir()) == [f"{VIDEO_ID}.mp4"]


def test_complete_download_is_not_downloaded_again(data_dir: Path) -> None:
    downloader = FakeDownloader()
    first = download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    second = download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert second == first
    assert len(downloader.requests) == 1


def test_partial_download_is_handed_back_to_the_downloader_to_resume(data_dir: Path) -> None:
    audio_dir = raw_audio_dir(data_dir)
    audio_dir.mkdir(parents=True)
    for leftover in (f"{STEM}.mp4.part", f"{STEM}.mp4.ytdl", f"{STEM}.mp4.part-Frag3"):
        (audio_dir / leftover).write_bytes(b"partial")
    downloader = FakeDownloader()

    download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    # The same stem, so yt-dlp finds and continues its .part/.ytdl files.
    assert [request.stem for request in downloader.requests] == [STEM]


def test_downloads_of_other_vods_are_not_mistaken_for_this_one(data_dir: Path) -> None:
    audio_dir = raw_audio_dir(data_dir)
    audio_dir.mkdir(parents=True)
    (audio_dir / f"{VIDEO_ID}0.mp4").write_bytes(b"other")
    downloader = FakeDownloader()

    download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

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
    def downloader(request: DownloadRequest, /) -> Path:
        paths = {
            "missing": request.output_dir / f"{request.stem}.mp4",
            "outside": data_dir / f"{request.stem}.mp4",
            "wrong-stem": request.output_dir / "other.mp4",
            "odd-extension": request.output_dir / f"{request.stem}.f1.mp4",
        }
        path = paths[returned]
        if returned != "missing":
            path.write_bytes(b"audio")
        return path

    with pytest.raises(DownloadToolError, match="the downloader returned"):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert not (raw_audio_dir(data_dir) / f"{VIDEO_ID}.mp4").exists()


def test_unexpected_exception_from_the_downloader_is_a_tool_error(data_dir: Path) -> None:
    def downloader(request: DownloadRequest, /) -> Path:
        raise KeyError("value")

    with pytest.raises(DownloadToolError) as caught:
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert isinstance(caught.value.__cause__, KeyError)
    assert caught.value.video_id == VIDEO_ID


def test_ytdlp_options_resume_retry_and_never_skip_fragments(tmp_path: Path) -> None:
    request = DownloadRequest("https://example.invalid/v", tmp_path, STEM, "bestaudio")

    options = ytdlp_options(request)

    assert options["format"] == "bestaudio"
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
    request = DownloadRequest(vod_url(VIDEO_ID), Path("unused"), STEM, DEFAULT_FORMAT_SELECTOR)
    ydl = YoutubeDL(ytdlp_options(request), auto_init=False)  # pyright: ignore[reportArgumentType]
    ydl.add_info_extractor(FakeTwitchVodIE(scenario))
    return ydl.extract_info(request.url, download=download)


@pytest.mark.parametrize(
    ("formats", "expected"),
    [
        ((_VIDEO_160P, _AUDIO_ONLY, _VIDEO_1080P, _STORYBOARD), "Audio_Only"),
        ((_VIDEO_1080P, _VIDEO_160P, _STORYBOARD), "160p30"),  # no audio-only format
    ],
)
def test_format_selector_prefers_audio_only_then_the_lowest_quality_video(
    formats: tuple[Mapping[str, object], ...], expected: str
) -> None:
    info = run_ytdlp(formats_info(*formats), download=False)

    assert isinstance(info, dict)
    assert info["format_id"] == expected


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
        # Only storyboards: yt-dlp's "Requested format is not available".
        pytest.param(formats_info(_STORYBOARD), DownloadToolError, id="no-audio-format"),
    ],
)
def test_ytdlp_failures_become_typed_errors(
    data_dir: Path, scenario: Callable[..., "_InfoDict"], expected: type[AudioDownloadError]
) -> None:
    def downloader(_request: DownloadRequest, /) -> Path:
        run_ytdlp(scenario, download=True)
        raise AssertionError("the scenario should fail")

    with pytest.raises(AudioDownloadError) as caught:
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)

    assert type(caught.value) is expected
    assert caught.value.video_id == VIDEO_ID
    assert VIDEO_ID in str(caught.value)


def test_fragment_lost_after_retries_is_a_network_error(data_dir: Path) -> None:
    # yt_dlp/downloader/fragment.py reports this without an exception attached.
    from yt_dlp.utils import DownloadError

    def downloader(_request: DownloadRequest, /) -> Path:
        raise DownloadError("ERROR: fragment 12 not found, unable to continue")

    with pytest.raises(DownloadNetworkError, match="run again to resume"):
        download_audio(VIDEO_ID, data_dir=data_dir, downloader=downloader)


def test_error_message_is_one_bounded_line(data_dir: Path) -> None:
    def downloader(_request: DownloadRequest, /) -> Path:
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
    request = DownloadRequest(source.as_uri(), tmp_path / "out", STEM, DEFAULT_FORMAT_SELECTOR)
    request.output_dir.mkdir()
    downloader = YtDlpDownloader({"enable_file_urls": True})

    path = downloader(request)
    modified = path.stat().st_mtime_ns
    again = downloader(request)

    assert path == request.output_dir / f"{STEM}.m4a"
    assert path.read_bytes() == source.read_bytes()
    assert again == path
    assert path.stat().st_mtime_ns == modified  # complete files are not fetched again
    assert capsys.readouterr().out == ""  # yt-dlp's output goes to logging


def test_ytdlp_downloader_rejects_a_directory_that_yt_dlp_would_expand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_TEST_ELSEWHERE", "elsewhere")
    output_dir = tmp_path / "$AUTOCLIPPER_TEST_ELSEWHERE"
    request = DownloadRequest("https://example.invalid/v", output_dir, STEM, "bestaudio")

    with pytest.raises(ValueError, match="expand environment variables"):
        YtDlpDownloader()(request)


# --- Normalizing -------------------------------------------------------------------------------


def probe_output(
    *, sample_rate_hz: int = 16_000, channels: int = 1, duration_s: float = 3.0
) -> bytes:
    stream = {
        "index": 0,
        "codec_type": "audio",
        "codec_name": "pcm_s16le",
        "sample_rate": str(sample_rate_hz),
        "channels": channels,
    }
    return json.dumps(
        {"format": {"format_name": "wav", "duration": str(duration_s)}, "streams": [stream]}
    ).encode()


class FakeFFmpeg:
    """A fake `Runner`: ffmpeg writes its output file, ffprobe returns `probe_stdout`."""

    def __init__(self, probe_stdout: bytes = probe_output(), ffmpeg_returncode: int = 0) -> None:
        self.probe_stdout = probe_stdout
        self.ffmpeg_returncode = ffmpeg_returncode
        self.commands: list[FFmpegCommand] = []

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        self.commands.append(command)
        if command.program == "ffprobe":
            return CommandResult(0, stdout=self.probe_stdout)
        Path(command.args[-1]).write_bytes(b"wav")
        return CommandResult(self.ffmpeg_returncode, stderr=b"error")


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
    convert, check = runner.commands
    assert convert.args[-1] == os.fspath(partial.absolute())
    args = " ".join(convert.args[:-1])
    assert f"-i {source.absolute()} -map 0:a:0" in args
    assert "-ac 1 -ar 16000 -c:a pcm_s16le -f wav" in args
    assert check.program == "ffprobe"
    assert check.args[-1] == os.fspath(partial.absolute())
    assert audio.path == output
    assert audio.duration_s == pytest.approx(3.0)
    assert output.read_bytes() == b"wav"
    assert not partial.exists()


@pytest.mark.parametrize(
    "probe_stdout",
    [probe_output(sample_rate_hz=44_100), probe_output(channels=2)],
    ids=["sample-rate", "channels"],
)
def test_wrong_output_format_is_an_error_and_leaves_no_file(
    source: Path, data_dir: Path, probe_stdout: bytes
) -> None:
    with pytest.raises(FFmpegOutputError, match="16000 Hz mono pcm_s16le"):
        normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=FakeFFmpeg(probe_stdout))

    assert list(normalized_audio_path(data_dir, VIDEO_ID).parent.iterdir()) == []


def test_ffmpeg_failure_leaves_no_partial_file(source: Path, data_dir: Path) -> None:
    with pytest.raises(FFmpegExitError):
        normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=FakeFFmpeg(ffmpeg_returncode=1))

    assert list(normalized_audio_path(data_dir, VIDEO_ID).parent.iterdir()) == []


def test_existing_output_is_checked_and_reused_unless_forced(source: Path, data_dir: Path) -> None:
    normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=FakeFFmpeg())
    runner = FakeFFmpeg()

    reused = normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=runner)
    assert [command.program for command in runner.commands] == ["ffprobe"]

    normalize_audio(source, VIDEO_ID, data_dir=data_dir, runner=runner, force=True)
    assert [command.program for command in runner.commands] == ["ffprobe", "ffmpeg", "ffprobe"]
    assert reused.duration_s == pytest.approx(3.0)


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
    from autoclipper.ffmpeg import probe

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


def test_prepare_downloads_then_normalizes(
    make_test_media: Callable[..., Path], data_dir: Path
) -> None:
    downloader = FakeDownloader("m4a", source=make_test_media("tone.m4a", video_size=None))

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
