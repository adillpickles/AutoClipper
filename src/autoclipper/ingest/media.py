"""Download a Twitch VOD's audio with yt-dlp and normalize it to 16 kHz mono WAV with FFmpeg.

Files live under the data directory (docs/architecture/overview.md §5). Callers pass it in,
normally ``get_settings().data_dir``:

- ``raw/audio/<video_id>.<ext>``: the audio as downloaded, never edited. The extension is
  whatever yt-dlp reports for the chosen format (``mp4`` for Twitch's HLS formats).
- ``interim/audio/<video_id>.wav``: 16 kHz mono 16-bit PCM for feature extraction.

Downloading VOD media through yt-dlp is unofficial and needs an approved source basis before it
is used on real data (docs/data/data-governance.md, OQ-17). Only public VODs are supported: no
cookies or login are ever passed, so subscriber-only VODs fail with `VodAccessRestrictedError`.

Resuming: yt-dlp downloads to ``<video_id>.download.<ext>`` (with ``.part`` and ``.ytdl`` files
while in progress, which it resumes on the next run), and the file is renamed to
``<video_id>.<ext>`` only after yt-dlp has finished downloading and post-processing it. A file
named ``<video_id>.<ext>`` therefore means a complete download, and it is never fetched again.
Delete it to download again.

Download failures raise a subclass of `AudioDownloadError`, so the CLI can map them to exit codes
(docs/development/conventions.md §7). yt-dlp has no error codes, so `classify_download_error`
reads its exception types and messages; see that function for the heuristics.
"""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from autoclipper.ffmpeg import (
    FFmpegOutputError,
    MediaInfo,
    Runner,
    ffmpeg_command,
    probe,
    run_command,
)

log = logging.getLogger(__name__)

VOD_URL_TEMPLATE: Final = "https://www.twitch.tv/videos/{video_id}"

# yt-dlp format selector (README "FORMAT SELECTION"): `bestaudio` is the best audio-only format
# (Twitch VODs normally offer one); if a VOD has none, `worst` takes the lowest-quality format
# with both video and audio (on Twitch the lowest resolution, so a small download), since only
# its audio is used.
DEFAULT_FORMAT_SELECTOR: Final = "bestaudio/worst"

# yt-dlp's API defaults to 0 fragment retries (its CLI uses 10); a VOD is thousands of HLS
# fragments, so retry each one with capped exponential backoff (conventions §6).
NETWORK_RETRIES: Final = 10
MAX_RETRY_SLEEP_S: Final = 30.0

# What `normalize_audio` writes.
SAMPLE_RATE_HZ: Final = 16_000
CHANNELS: Final = 1
CODEC: Final = "pcm_s16le"

# Decoding and resampling run far faster than real time; an hour covers multi-day VODs on slow
# machines.
NORMALIZE_TIMEOUT_S: Final = 3600.0

# yt-dlp's file name (without extension) while a download is incomplete.
_STAGING_SUFFIX: Final = ".download"

# Twitch video IDs are decimal digits. Only ASCII digits: `\d` would also accept other scripts'.
_VIDEO_ID: Final = re.compile(r"[0-9]{1,20}")
_EXTENSION: Final = re.compile(r"[A-Za-z0-9]{1,10}")

# Upper bound for yt-dlp's message inside our exception messages.
_MESSAGE_CHARS: Final = 1000


# --- Paths -------------------------------------------------------------------------------------


class InvalidVideoIdError(ValueError):
    """A video ID is not a Twitch video ID (a string of ASCII digits)."""


def validate_video_id(video_id: str) -> str:
    """Return `video_id` unchanged if it is a Twitch video ID, so it is safe in paths and URLs.

    Raises:
        InvalidVideoIdError: `video_id` is not a string of 1 to 20 ASCII digits (so no path
            separators, dots, ``v`` prefix or whitespace).
    """
    if not isinstance(video_id, str) or _VIDEO_ID.fullmatch(video_id) is None:
        raise InvalidVideoIdError(
            f"invalid Twitch video ID {video_id!r}: expected a string of digits, e.g. '9000000001'"
        )
    return video_id


def vod_url(video_id: str) -> str:
    """Return the Twitch page URL of a VOD, e.g. ``https://www.twitch.tv/videos/9000000001``."""
    return VOD_URL_TEMPLATE.format(video_id=validate_video_id(video_id))


def raw_audio_dir(data_dir: Path) -> Path:
    """Return the directory that holds downloaded audio: ``<data_dir>/raw/audio``."""
    return data_dir / "raw" / "audio"


def normalized_audio_path(data_dir: Path, video_id: str) -> Path:
    """Return where `normalize_audio` writes a VOD's WAV: ``<data_dir>/interim/audio/<id>.wav``."""
    return data_dir / "interim" / "audio" / f"{validate_video_id(video_id)}.wav"


# --- Errors ------------------------------------------------------------------------------------


class AudioDownloadError(Exception):
    """Downloading a VOD's audio failed. Base class of the download errors below.

    Attributes:
        video_id: The VOD that could not be downloaded.
    """

    headline: str = "downloading the audio of VOD {video_id} failed"
    video_id: str

    def __init__(self, video_id: str, detail: str) -> None:
        self.video_id = video_id
        detail = " ".join(detail.split())
        if len(detail) > _MESSAGE_CHARS:
            detail = detail[: _MESSAGE_CHARS - 3] + "..."
        super().__init__(f"{self.headline.format(video_id=video_id)}: {detail}")


class VodUnavailableError(AudioDownloadError):
    """The VOD doesn't exist (any more): deleted, expired, unpublished or geo-blocked."""

    headline = (
        "VOD {video_id} is unavailable (deleted, expired, never existed or blocked in this region)"
    )


class VodAccessRestrictedError(AudioDownloadError):
    """The VOD is subscriber-only or needs a login, which AutoClipper never uses."""

    headline = (
        "VOD {video_id} is subscriber-only or requires a login; only public VODs can be "
        "downloaded (docs/data/data-governance.md)"
    )


class DownloadNetworkError(AudioDownloadError):
    """A network request failed after retries. Running again resumes the partial download."""

    headline = "network failure while downloading VOD {video_id}; run again to resume"


class DownloadToolError(AudioDownloadError):
    """yt-dlp failed for another reason, e.g. a Twitch change it doesn't handle yet."""

    headline = (
        "yt-dlp could not download VOD {video_id}; if this persists, update it "
        "(uv lock --upgrade-package yt-dlp)"
    )


# Message patterns, checked in this order on the messages of the whole exception chain. The
# phrases are from yt-dlp 2026.08.19 (yt_dlp/extractor/twitch.py, extractor/common.py,
# YoutubeDL.py, downloader/fragment.py); yt-dlp may reword them, so unmatched errors fall through
# to DownloadToolError rather than being guessed.
_RESTRICTED: Final = re.compile(
    r"subscriber-only|logged in|registered users|login required"
    r"|vod_manifest_restricted|unauthorized_entitlements",
    re.IGNORECASE,
)
_UNAVAILABLE: Final = re.compile(
    r"does not exist|has been (?:deleted|removed)|no longer available|video unavailable"
    r"|not available from your location",
    re.IGNORECASE,
)
_NETWORK: Final = re.compile(
    r"unable to download (?:video data|webpage|json metadata|m3u8 information)"
    r"|fragment \d+ not found|content too short|timed out|connection (?:reset|refused|aborted)"
    r"|name resolution|getaddrinfo failed|network is unreachable",
    re.IGNORECASE,
)
_GONE_HTTP_STATUSES: Final = frozenset({404, 410})


def classify_download_error(exc: BaseException, video_id: str) -> AudioDownloadError:
    """Turn an exception raised while downloading into the matching `AudioDownloadError`.

    yt-dlp reports every failure as ``yt_dlp.utils.DownloadError`` and keeps the original
    exception in ``exc_info``, ``cause``, ``__cause__`` or ``__context__``. All of them are
    examined, first matching rule wins:

    1. `VodAccessRestrictedError`: a message mentions subscriber-only content, logging in or
       Twitch's restriction error codes.
    2. `VodUnavailableError`: a message says the video does not exist, was deleted or is
       geo-blocked; or an exception is a ``GeoRestrictedError``, or an HTTP 404/410 error.
    3. `DownloadNetworkError`: an exception is one of yt-dlp's network errors
       (``TransportError``, ``HTTPError`` with another status, ``ContentTooShortError``) or a
       message describes a failed request, timeout or lost connection.
    4. `DownloadToolError`: anything else (extractor broke, no suitable format, post-processing
       failed, an unexpected exception).

    These are heuristics on yt-dlp's wording, which can change; check them when upgrading.

    Args:
        exc: The exception the downloader raised.
        video_id: The VOD being downloaded, for the error message.

    Returns:
        The error to raise (from `exc`).
    """
    # Deferred: importing yt-dlp takes about a second.
    from yt_dlp.networking.exceptions import HTTPError, TransportError
    from yt_dlp.utils import ContentTooShortError, GeoRestrictedError

    chain = _exception_chain(exc)
    messages = " | ".join(str(error) for error in chain)
    detail = str(exc) or type(exc).__name__
    if _RESTRICTED.search(messages):
        return VodAccessRestrictedError(video_id, detail)
    if _UNAVAILABLE.search(messages) or any(
        isinstance(error, GeoRestrictedError)
        or (isinstance(error, HTTPError) and error.status in _GONE_HTTP_STATUSES)
        for error in chain
    ):
        return VodUnavailableError(video_id, detail)
    if _NETWORK.search(messages) or any(
        isinstance(error, TransportError | HTTPError | ContentTooShortError) for error in chain
    ):
        return DownloadNetworkError(video_id, detail)
    return DownloadToolError(video_id, detail)


def _exception_chain(exc: BaseException, limit: int = 20) -> list[BaseException]:
    chain: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending and len(chain) < limit:
        current = pending.pop(0)
        if any(current is seen for seen in chain):
            continue
        chain.append(current)
        # yt-dlp's DownloadError and ExtractorError keep the original as a sys.exc_info() tuple.
        exc_info: object = getattr(current, "exc_info", None)
        original = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) == 3 else None
        linked = (original, getattr(current, "cause", None), current.__cause__, current.__context__)
        pending.extend(error for error in linked if isinstance(error, BaseException))
    return chain


# --- Downloading -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """One download for a `Downloader`.

    Attributes:
        url: The page URL to download from.
        output_dir: The directory to write into.
        stem: The file name without extension; the downloader appends ``.<ext>``.
        format_selector: A yt-dlp format selector, e.g. `DEFAULT_FORMAT_SELECTOR`.
    """

    url: str
    output_dir: Path
    stem: str
    format_selector: str


class Downloader(Protocol):
    """Downloads `request.url` to ``<output_dir>/<stem>.<ext>`` and returns that path.

    It continues partial downloads of the same request, and returns only once the file is
    complete. `YtDlpDownloader` is the real implementation; tests pass a fake.
    """

    def __call__(self, request: DownloadRequest, /) -> Path:
        """Download and return the path of the complete file."""
        ...


def ytdlp_options(request: DownloadRequest) -> dict[str, object]:
    """Return the yt-dlp options (``YoutubeDL`` params) for `request`.

    Names and meanings are from the ``YoutubeDL`` class docstring and
    ``yt_dlp/downloader/common.py`` / ``fragment.py`` of yt-dlp 2026.08.19.
    """
    return {
        "format": request.format_selector,
        # The directory goes in `paths`, not in the template, so `%(...)s` in it isn't a field.
        # yt-dlp still expands environment variables in it; YtDlpDownloader rejects those.
        "paths": {"home": str(request.output_dir)},
        "outtmpl": {"default": f"{request.stem}.%(ext)s"},
        # Resume .part files (and HLS progress in .ytdl files) instead of starting over.
        "continuedl": True,
        "nopart": False,
        "retries": NETWORK_RETRIES,
        "fragment_retries": NETWORK_RETRIES,
        # Fail instead of silently leaving a gap in the audio when a fragment can't be fetched.
        "skip_unavailable_fragments": False,
        "retry_sleep_functions": {
            "http": _retry_sleep_s,
            "fragment": _retry_sleep_s,
            "extractor": _retry_sleep_s,
        },
        "noprogress": True,
        # No ANSI color codes in messages, which end up in logs and exception messages.
        "color": "never",
        "logger": _YtDlpLogger(),
    }


def _retry_sleep_s(n: int) -> float:
    # yt-dlp calls this with n = retries so far (0 for the first retry).
    return min(2.0**n, MAX_RETRY_SLEEP_S)


class _YtDlpLogger:
    """Sends yt-dlp's console output to `logging` instead of stdout/stderr."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp message=%s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp message=%s", msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp warning=%s", msg)

    def error(self, msg: str) -> None:
        # yt-dlp raises the error afterwards, and the caller reports it.
        log.debug("yt-dlp error=%s", msg)


class YtDlpDownloader:
    """The real `Downloader`: yt-dlp's Python API (``yt_dlp.YoutubeDL``).

    Args:
        extra_options: yt-dlp options that override `ytdlp_options`; meant for tests.
    """

    def __init__(self, extra_options: Mapping[str, object] | None = None) -> None:
        self._extra_options = dict(extra_options or {})

    def __call__(self, request: DownloadRequest, /) -> Path:
        """Download with yt-dlp; its exceptions propagate unchanged."""
        import yt_dlp  # Deferred: importing yt-dlp takes about a second.

        # yt-dlp passes `paths` through os.path.expandvars, so a directory name such as
        # `$HOME` or `%TEMP%` would send the download elsewhere. This reads no setting.
        home = str(request.output_dir)
        if os.path.expandvars(home) != home:
            raise ValueError(f"yt-dlp would expand environment variables in {home}; rename it")
        options = {**ytdlp_options(request), **self._extra_options}
        # The ignore: Pyright's bundled yt-dlp stubs (typeshed, 2026.3.17) type params as a
        # TypedDict that disagrees with yt-dlp's docs and source (e.g. `retry_sleep_functions`
        # keys 'http'/'extractor', float delays; a logger needing stdout/stderr methods).
        with yt_dlp.YoutubeDL(options) as ydl:  # pyright: ignore[reportArgumentType]
            info: object = ydl.extract_info(request.url, download=True)
        # After post-processing, the final path of each downloaded format is in
        # info["requested_downloads"][i]["filepath"] (YoutubeDL.process_video_result).
        downloads = info.get("requested_downloads") if isinstance(info, dict) else None
        if not isinstance(downloads, list) or len(downloads) != 1:
            raise RuntimeError(f"expected yt-dlp to report one downloaded file, got {downloads!r}")
        filepath = downloads[0].get("filepath") if isinstance(downloads[0], dict) else None
        if not isinstance(filepath, str):
            raise RuntimeError(f"yt-dlp reported no file path for the download: {downloads[0]!r}")
        return Path(filepath)


def download_audio(
    video_id: str,
    *,
    data_dir: Path,
    downloader: Downloader | None = None,
    format_selector: str = DEFAULT_FORMAT_SELECTOR,
) -> Path:
    """Download a VOD's audio to ``<data_dir>/raw/audio/<video_id>.<ext>``, resuming if needed.

    Returns at once if a complete download exists (see the module docstring for how partial and
    complete downloads are told apart).

    Args:
        video_id: The Twitch video ID, digits only.
        data_dir: The data directory, normally ``get_settings().data_dir``.
        downloader: Does the download. Defaults to `YtDlpDownloader`; tests pass a fake.
        format_selector: The yt-dlp format selector.

    Returns:
        The path of the complete download.

    Raises:
        InvalidVideoIdError: `video_id` is not a Twitch video ID. Checked before anything else.
        VodUnavailableError, VodAccessRestrictedError, DownloadNetworkError, DownloadToolError:
            The download failed; see `classify_download_error`.
        AudioDownloadError: More than one complete download exists for the VOD.
    """
    validate_video_id(video_id)
    audio_dir = raw_audio_dir(data_dir)
    existing = _complete_downloads(audio_dir, video_id)
    if len(existing) > 1:
        names = ", ".join(path.name for path in existing)
        raise AudioDownloadError(
            video_id, f"found several downloads in {audio_dir} ({names}); delete all but one"
        )
    if existing:
        log.info("audio already downloaded video_id=%s path=%s", video_id, existing[0])
        return existing[0]

    audio_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{video_id}{_STAGING_SUFFIX}"
    request = DownloadRequest(vod_url(video_id), audio_dir, stem, format_selector)
    download = YtDlpDownloader() if downloader is None else downloader
    log.info("downloading audio video_id=%s url=%s", video_id, request.url)
    try:
        staged = download(request)
    except Exception as exc:  # Every downloader failure becomes a typed error (module docs).
        raise classify_download_error(exc, video_id) from exc

    extension = _staged_extension(staged, audio_dir, stem)
    if extension is None:
        raise DownloadToolError(
            video_id, f"the downloader returned {staged}, expected an existing {stem}.<ext> file"
        )
    final = audio_dir / f"{video_id}.{extension}"
    staged.replace(final)
    log.info("downloaded audio video_id=%s path=%s", video_id, final)
    return final


def _complete_downloads(audio_dir: Path, video_id: str) -> list[Path]:
    if not audio_dir.is_dir():
        return []
    # `<id>.<ext>` only: staging files (`<id>.download.<ext>[.part]`) have more dots.
    name = re.compile(rf"{re.escape(video_id)}\.{_EXTENSION.pattern}")
    return sorted(p for p in audio_dir.iterdir() if name.fullmatch(p.name) and p.is_file())


def _staged_extension(staged: Path, audio_dir: Path, stem: str) -> str | None:
    if not staged.is_file() or staged.resolve().parent != audio_dir.resolve():
        return None
    prefix = f"{stem}."
    extension = staged.name[len(prefix) :] if staged.name.startswith(prefix) else ""
    return extension if _EXTENSION.fullmatch(extension) else None


# --- Normalizing -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NormalizedAudio:
    """A VOD's audio as 16 kHz mono 16-bit PCM WAV.

    Attributes:
        path: The WAV file.
        duration_s: Its duration in seconds, as ffprobe reads it from the file.
    """

    path: Path
    duration_s: float


def normalize_audio(
    source: Path,
    video_id: str,
    *,
    data_dir: Path,
    runner: Runner | None = None,
    timeout_s: float = NORMALIZE_TIMEOUT_S,
    force: bool = False,
) -> NormalizedAudio:
    """Convert a VOD's first audio stream to ``<data_dir>/interim/audio/<video_id>.wav``.

    The WAV is 16 kHz, mono (channels mixed down), 16-bit PCM. FFmpeg writes a temporary file,
    which is probed and checked before it is renamed into place, so the WAV path never holds a
    partial or wrong file. If the WAV exists already, it is checked and returned (pass `force` to
    convert again).

    Args:
        source: The downloaded media file; any container FFmpeg reads, with or without video.
        video_id: The Twitch video ID, digits only.
        data_dir: The data directory, normally ``get_settings().data_dir``.
        runner: Executes FFmpeg and FFprobe. Defaults to the real binaries; tests pass a fake.
        timeout_s: Seconds the conversion may take.
        force: Convert again even if the WAV exists.

    Returns:
        The WAV's path and probed duration.

    Raises:
        InvalidVideoIdError: `video_id` is not a Twitch video ID.
        FileNotFoundError: `source` is not an existing file.
        FFmpegError: FFmpeg or FFprobe failed (as for `autoclipper.ffmpeg.run_command` and
            `probe`), for example because `source` has no audio stream; `FFmpegOutputError` if
            the WAV is not 16 kHz mono 16-bit PCM.
    """
    output = normalized_audio_path(data_dir, video_id)
    if not source.is_file():
        raise FileNotFoundError(f"audio file not found: {source}")
    if output.exists() and not force:
        info = _check_normalized(probe(output, runner=runner))
        log.info("audio already normalized video_id=%s path=%s", video_id, output)
        return NormalizedAudio(output, info.duration_s)

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f"{output.name}.part")
    command = ffmpeg_command(
        "-y",  # A leftover partial file from an interrupted run is replaced.
        "-i",
        source,
        "-map",
        "0:a:0",  # The first audio stream only; fails if there is none.
        "-map_metadata",
        "-1",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE_HZ),
        "-c:a",
        CODEC,
        "-f",
        "wav",  # Needed: the .part extension doesn't name a format.
        "-rf64",
        "auto",  # RF64 header past 4 GiB (about 37 hours at 16 kHz mono), plain WAV below.
        partial,
    )
    try:
        run_command(command, timeout_s=timeout_s, runner=runner)
        info = _check_normalized(probe(partial, runner=runner))
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    log.info(
        "normalized audio video_id=%s path=%s duration_s=%.3f", video_id, output, info.duration_s
    )
    return NormalizedAudio(output, info.duration_s)


def _check_normalized(info: MediaInfo) -> MediaInfo:
    streams = info.audio_streams
    if (
        len(info.streams) != 1
        or len(streams) != 1
        or (streams[0].codec_name, streams[0].sample_rate_hz, streams[0].channels)
        != (CODEC, SAMPLE_RATE_HZ, CHANNELS)
    ):
        raise FFmpegOutputError(
            f"{info.path} is not {SAMPLE_RATE_HZ} Hz mono {CODEC} audio; streams: {info.streams}"
        )
    return info


def prepare_vod_audio(
    video_id: str,
    *,
    data_dir: Path,
    downloader: Downloader | None = None,
    runner: Runner | None = None,
) -> NormalizedAudio:
    """Download a VOD's audio (`download_audio`) and normalize it (`normalize_audio`).

    Both steps skip work that is already done, so running this again after a failure resumes.

    Raises:
        The exceptions of `download_audio` and `normalize_audio`.
    """
    source = download_audio(video_id, data_dir=data_dir, downloader=downloader)
    return normalize_audio(source, video_id, data_dir=data_dir, runner=runner)
