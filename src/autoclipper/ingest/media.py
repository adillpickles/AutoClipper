"""Download a Twitch VOD's audio with yt-dlp and normalize it to 16 kHz mono WAV with FFmpeg.

Files live under the data directory (docs/architecture/overview.md §5). Callers pass it in,
normally ``get_settings().data_dir``:

- ``raw/audio/<video_id>.<ext>``: the audio-only stream as downloaded, never edited. The
  extension names the container ffprobe finds: ``ts`` (MPEG-TS) for Twitch's HLS segments,
  otherwise the one yt-dlp reports.
- ``interim/audio/<video_id>.wav``: 16 kHz mono 16-bit PCM for feature extraction.

Downloading VOD media through yt-dlp is unofficial and needs an approved source basis before it
is used on real data (docs/data/data-governance.md, OQ-17). Only public VODs and audio-only
formats are downloaded: no cookies or login are passed, and there is no fallback to formats with
video.

yt-dlp only downloads: its own FFmpeg use (remuxing, fix-ups, FFmpeg as HLS downloader) is turned
off, so every FFmpeg and FFprobe run goes through `autoclipper.ffmpeg`, with a timeout.

Resuming: yt-dlp downloads to ``<video_id>.download.<ext>`` (with ``.part`` and ``.ytdl`` files
while in progress, which it resumes on the next run). A partial download that can't be resumed
safely is deleted first (`YtDlpDownloader`). A finished download is probed and checked against
the duration the source reports, and only then renamed to ``<video_id>.<ext>``. A file with that
name is therefore a complete, checked download, and it is never fetched again. Delete it to
download again.

Download failures raise a subclass of `AudioDownloadError`, so the CLI can map them to exit codes
(docs/development/conventions.md §7). yt-dlp has no error codes, so `classify_download_error`
reads its exception types and messages; see that function for the heuristics.
"""

import contextvars
import gc
import io
import json
import logging
import math
import os
import re
import traceback
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from autoclipper.ffmpeg import (
    FFmpegExitError,
    FFmpegOutputError,
    MediaInfo,
    Runner,
    ffmpeg_command,
    probe,
    run_command,
)

log = logging.getLogger(__name__)

VOD_URL_TEMPLATE: Final = "https://www.twitch.tv/videos/{video_id}"

# yt-dlp format selector (README "FORMAT SELECTION"): the best audio-only format. Twitch VODs
# normally offer one. There is deliberately no fallback to formats with video (#19 is audio only,
# and data governance asks for minimal downloads): without one, NoAudioOnlyFormatError.
AUDIO_FORMAT_SELECTOR: Final = "bestaudio"

# yt-dlp's API defaults to 0 fragment retries (its CLI uses 10); a VOD is thousands of HLS
# fragments, so retry each one with capped exponential backoff (conventions §6).
NETWORK_RETRIES: Final = 10
MAX_RETRY_SLEEP_S: Final = 30.0

# A finished download that plays for longer or shorter than the duration the source reports
# (Twitch: whole seconds from the VOD's metadata), by more than the larger of these, is treated
# as damaged, e.g. audio duplicated by a broken resume. Not yet measured on real Twitch VODs;
# adjust if real downloads trip it.
DOWNLOAD_DURATION_TOLERANCE_S: Final = 2.0
DOWNLOAD_DURATION_TOLERANCE_FRACTION: Final = 0.002
# Measuring that reads the whole file without decoding it: seconds even for a long VOD.
DOWNLOAD_CHECK_TIMEOUT_S: Final = 600.0

# The WAV may differ from its source's container duration by the larger of these (encoder delay
# and frame rounding are tens of milliseconds); more means part of the source didn't decode, or
# its timestamps jump back (repeated audio).
NORMALIZED_DURATION_TOLERANCE_S: Final = 0.5
NORMALIZED_DURATION_TOLERANCE_FRACTION: Final = 0.001

# What `normalize_audio` writes.
SAMPLE_RATE_HZ: Final = 16_000
CHANNELS: Final = 1
CODEC: Final = "pcm_s16le"

# Decoding and resampling run far faster than real time; an hour covers multi-day VODs on slow
# machines.
NORMALIZE_TIMEOUT_S: Final = 3600.0

# yt-dlp's file name (without extension) while a download is incomplete.
_STAGING_SUFFIX: Final = ".download"

# File extensions for containers whose ffprobe name differs from what yt-dlp reports: Twitch's
# HLS formats say "mp4", but without yt-dlp's FFmpeg remux the file is the MPEG-TS segments.
_CONTAINER_EXTENSIONS: Final = {"mpegts": "ts"}

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


class NoAudioOnlyFormatError(AudioDownloadError):
    """The VOD offers no audio-only format; formats with video are never downloaded instead."""

    headline = (
        "VOD {video_id} offers no audio-only format, and AutoClipper doesn't download video "
        "to get the audio (issue #19, docs/data/data-governance.md)"
    )


class DownloadNetworkError(AudioDownloadError):
    """A network request failed after retries. Running again resumes the partial download."""

    headline = "network failure while downloading VOD {video_id}; run again to resume"


class DownloadIntegrityError(AudioDownloadError):
    """The finished download was unreadable, had no audio or had the wrong duration.

    It has been deleted, with any partial files, so running again starts from the beginning.
    """

    headline = (
        "the download of VOD {video_id} was damaged and has been deleted; run again to "
        "download it from the start"
    )


class DownloadToolError(AudioDownloadError):
    """yt-dlp failed for another reason, e.g. a Twitch change it doesn't handle yet."""

    headline = (
        "yt-dlp could not download VOD {video_id}; if this persists, update it "
        "(uv lock --upgrade-package yt-dlp)"
    )


# Message patterns, checked in this order on the messages of the whole exception chain. The
# phrases are from yt-dlp 2026.08.19 (yt_dlp/extractor/twitch.py, extractor/common.py,
# YoutubeDL.py, downloader/common.py, downloader/fragment.py); yt-dlp may reword them, so
# unmatched errors fall through to DownloadToolError rather than being guessed.
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
_NO_AUDIO_FORMAT: Final = re.compile(r"requested format is not available", re.IGNORECASE)
# "[download] Got error: ... Giving up after N retries" is how yt-dlp's downloaders report
# requests that failed on every retry; the HTTP error itself isn't attached.
_NETWORK: Final = re.compile(
    r"unable to download (?:video data|webpage|json metadata|m3u8 information)"
    r"|\[download\] got error|giving up after \d+ retries"
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
    3. `NoAudioOnlyFormatError`: yt-dlp says the requested format is not available.
    4. `DownloadNetworkError`: an exception is one of yt-dlp's network errors
       (``TransportError``, ``HTTPError`` with another status, ``ContentTooShortError``) or a
       message describes a failed request, retries given up, a timeout or a lost connection.
    5. `DownloadToolError`: anything else (extractor broke, an unexpected exception).

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
    if _NO_AUDIO_FORMAT.search(messages):
        return NoAudioOnlyFormatError(video_id, detail)
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
        stem: The file name without extension; the downloader appends ``.<ext>``. Every file the
            downloader writes, partial ones included, is named ``<stem>.<something>``.
    """

    url: str
    output_dir: Path
    stem: str


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """What a `Downloader` returns.

    Attributes:
        path: The complete file, ``<output_dir>/<stem>.<ext>``.
        duration_s: The media's duration as the source reports it (yt-dlp's ``duration``), in
            seconds, or None if the source doesn't say.
    """

    path: Path
    duration_s: float | None


class Downloader(Protocol):
    """Downloads `request.url` to ``<output_dir>/<stem>.<ext>`` (audio only).

    It continues partial downloads of the same request, and returns only once the file is
    complete. `YtDlpDownloader` is the real implementation; tests pass a fake.
    """

    def __call__(self, request: DownloadRequest, /) -> DownloadedMedia:
        """Download and return the complete file."""
        ...


def ytdlp_options(request: DownloadRequest) -> dict[str, object]:
    """Return the yt-dlp options (``YoutubeDL`` params) for `request`.

    Names and meanings are from the ``YoutubeDL`` class docstring and
    ``yt_dlp/downloader/common.py`` / ``fragment.py`` of yt-dlp 2026.08.19.
    """
    return {
        "format": AUDIO_FORMAT_SELECTOR,
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
        # No FFmpeg inside yt-dlp: its own downloaders (not FFmpeg) for every protocol, no
        # fix-up remuxes, and a location without FFmpeg for anything that looks for it
        # (`_no_ffmpeg_location`). FFmpeg runs only through autoclipper.ffmpeg.
        "external_downloader": {"default": "native"},
        "fixup": "never",
        "ffmpeg_location": str(_no_ffmpeg_location(request)),
        "noprogress": True,
        # No ANSI color codes in messages, which end up in logs and exception messages.
        "color": "never",
        "logger": _YtDlpLogger(),
    }


def _retry_sleep_s(n: int) -> float:
    # yt-dlp calls this with n = retries so far (0 for the first retry).
    return min(2.0**n, MAX_RETRY_SLEEP_S)


def _no_ffmpeg_location(request: DownloadRequest) -> Path:
    # A path that never exists: yt-dlp then treats FFmpeg as not installed, without running it.
    return request.output_dir / f"{request.stem}.no-ffmpeg"


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

    def __call__(self, request: DownloadRequest, /) -> DownloadedMedia:
        """Download with yt-dlp; its exceptions propagate unchanged."""
        # yt-dlp passes `paths` through os.path.expandvars, so a directory name such as
        # `$HOME` or `%TEMP%` would send the download elsewhere. This reads no setting.
        home = str(request.output_dir)
        if os.path.expandvars(home) != home:
            raise ValueError(f"yt-dlp would expand environment variables in {home}; rename it")
        _discard_unresumable_partials(request.output_dir, request.stem)
        options = {**ytdlp_options(request), **self._extra_options}
        try:
            # A copied context keeps the FFmpeg location set in `_extract` inside this call.
            info = contextvars.copy_context().run(_extract, request, options)
        except BaseException as exc:
            _release_failed_download(exc, request)
            raise
        # After post-processing, the final path of each downloaded format is in
        # info["requested_downloads"][i]["filepath"] (YoutubeDL.process_video_result).
        downloads = info.get("requested_downloads") if isinstance(info, dict) else None
        if not isinstance(downloads, list) or len(downloads) != 1:
            raise RuntimeError(f"expected yt-dlp to report one downloaded file, got {downloads!r}")
        filepath = downloads[0].get("filepath") if isinstance(downloads[0], dict) else None
        if not isinstance(filepath, str):
            raise RuntimeError(f"yt-dlp reported no file path for the download: {downloads[0]!r}")
        duration: object = info.get("duration") if isinstance(info, dict) else None
        duration_s = None
        if isinstance(duration, int | float) and not isinstance(duration, bool):
            duration_s = float(duration) if math.isfinite(duration) and duration > 0 else None
        return DownloadedMedia(Path(filepath), duration_s)


def _extract(request: DownloadRequest, options: dict[str, object]) -> object:
    import yt_dlp  # Deferred: importing yt-dlp takes about a second.
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    # yt-dlp's HLS downloader checks whether FFmpeg is installed by running `ffmpeg -bsfs` and
    # `ffprobe -bsfs` (no timeout). That check ignores the `ffmpeg_location` option (a "Fixme" in
    # yt_dlp/downloader/external.py) and reads this context variable, which yt-dlp's CLI sets
    # for --ffmpeg-location. The ignore: yt-dlp declares it with default None, so Pyright infers
    # ContextVar[None].
    FFmpegPostProcessor._ffmpeg_location.set(str(_no_ffmpeg_location(request)))  # pyright: ignore[reportArgumentType]
    # The ignore: Pyright's bundled yt-dlp stubs (typeshed, 2026.3.17) type params as a
    # TypedDict that disagrees with yt-dlp's docs and source (e.g. `retry_sleep_functions`
    # keys 'http'/'extractor', float delays; a logger needing stdout/stderr methods).
    with yt_dlp.YoutubeDL(options) as ydl:  # pyright: ignore[reportArgumentType]
        return ydl.extract_info(request.url, download=True)


def _discard_unresumable_partials(directory: Path, stem: str) -> None:
    """Delete a partial download that yt-dlp would append to instead of resuming.

    yt-dlp 2026.08.19 resumes an HLS download from the fragment number in ``<name>.ytdl``. When
    that file is missing or unreadable but ``<name>.part`` holds data, it starts again from the
    first fragment and appends to the ``.part`` file, duplicating the audio
    (``_prepare_frag_download`` in yt_dlp/downloader/fragment.py). Deleting the partial files
    makes it start clean. A plain HTTP download (which has no ``.ytdl``) then restarts instead
    of resuming; Twitch VODs are HLS.
    """
    if not directory.is_dir():
        return
    part_name = re.compile(rf"{re.escape(stem)}\.{_EXTENSION.pattern}\.part")
    for part in directory.iterdir():
        if not part_name.fullmatch(part.name) or part.stat().st_size == 0:
            continue
        if not _readable_checkpoint(part.with_suffix(".ytdl")):
            log.warning("restarting download: partial file has no usable .ytdl path=%s", part)
            _delete_staging_files(directory, stem)
            return


def _readable_checkpoint(path: Path) -> bool:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    # The shape yt-dlp writes: {"downloader": {"current_fragment": {"index": <int>}, ...}}.
    downloader = data.get("downloader") if isinstance(data, dict) else None
    fragment = downloader.get("current_fragment") if isinstance(downloader, dict) else None
    index = fragment.get("index") if isinstance(fragment, dict) else None
    return isinstance(index, int) and not isinstance(index, bool) and index >= 0


def _delete_staging_files(directory: Path, stem: str) -> None:
    for path in directory.iterdir():
        if path.name.startswith(f"{stem}.") and path.is_file():
            path.unlink(missing_ok=True)


def _release_failed_download(exc: BaseException, request: DownloadRequest) -> None:
    """Close what a failed yt-dlp call left open, so a retry can use its files.

    When a fragment request fails for good, yt-dlp 2026.08.19 raises without closing its
    ``.part`` stream (the HTTP downloader's error skips the fragment downloader's cleanup) or the
    HTTP error responses it kept for retrying. Both stay open while the exception's traceback is
    referenced, and on Windows an open file can't be renamed or deleted, so a retry would fail.

    The ``.part`` stream is closed explicitly. Then the frames in the exception's tracebacks are
    cleared (the traceback keeps file and line numbers) and garbage is collected, which frees the
    rest now instead of whenever the caller drops the exception. Their ResourceWarnings, about
    yt-dlp's leak rather than this code, are ignored while that happens.
    """
    _close_files_left_open(request.output_dir, request.stem)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        for error in _exception_chain(exc):
            traceback.clear_frames(error.__traceback__)
        gc.collect()


def _close_files_left_open(directory: Path, stem: str) -> None:
    # The garbage collector's object list finds the stream wherever it is referenced.
    directory = directory.resolve()
    for obj in gc.get_objects():
        if not isinstance(obj, io.IOBase):
            continue
        name = getattr(obj, "name", None)
        if not isinstance(name, str) or not Path(name).name.startswith(f"{stem}."):
            continue
        try:
            if not obj.closed and Path(name).resolve().parent == directory:
                obj.close()
        except (OSError, ValueError) as exc:
            log.warning("could not close a file yt-dlp left open path=%s error=%s", name, exc)


def download_audio(
    video_id: str,
    *,
    data_dir: Path,
    downloader: Downloader | None = None,
    runner: Runner | None = None,
) -> Path:
    """Download a VOD's audio to ``<data_dir>/raw/audio/<video_id>.<ext>``, resuming if needed.

    Returns at once if a complete download exists (see the module docstring for how partial and
    complete downloads are told apart). A new download is checked with FFmpeg before it is kept:
    it must have an audio stream and, when the source reports a duration, play for that long
    within `DOWNLOAD_DURATION_TOLERANCE_S` or `DOWNLOAD_DURATION_TOLERANCE_FRACTION`, whichever
    is larger.

    Args:
        video_id: The Twitch video ID, digits only.
        data_dir: The data directory, normally ``get_settings().data_dir``.
        downloader: Does the download. Defaults to `YtDlpDownloader`; tests pass a fake.
        runner: Executes FFmpeg and FFprobe for the check. Defaults to the real binaries; tests
            pass a fake.

    Returns:
        The path of the complete download.

    Raises:
        InvalidVideoIdError: `video_id` is not a Twitch video ID. Checked before anything else.
        VodUnavailableError, VodAccessRestrictedError, NoAudioOnlyFormatError,
            DownloadNetworkError, DownloadToolError: The download failed; see
            `classify_download_error`.
        DownloadIntegrityError: The finished download was damaged; it has been deleted.
        AudioDownloadError: More than one complete download exists for the VOD.
        FFmpegError: FFmpeg or FFprobe could not run (not installed, timed out).
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
    request = DownloadRequest(vod_url(video_id), audio_dir, stem)
    download = YtDlpDownloader() if downloader is None else downloader
    log.info("downloading audio video_id=%s url=%s", video_id, request.url)
    try:
        media = download(request)
    except Exception as exc:  # Every downloader failure becomes a typed error (module docs).
        raise classify_download_error(exc, video_id) from exc

    extension = _staged_extension(media.path, audio_dir, stem)
    if extension is None:
        raise DownloadToolError(
            video_id, f"the downloader returned {media.path}, expected an existing {stem}.<ext>"
        )
    try:
        info = probe(media.path, runner=runner)
        problem = _download_problem(info, media.duration_s, runner)
    except (FFmpegExitError, FFmpegOutputError) as exc:
        _delete_staging_files(audio_dir, stem)
        raise DownloadIntegrityError(video_id, f"FFmpeg can't read it: {exc}") from exc
    if problem is not None:
        _delete_staging_files(audio_dir, stem)
        raise DownloadIntegrityError(video_id, problem)

    final = audio_dir / f"{video_id}.{_container_extension(info) or extension}"
    media.path.replace(final)
    log.info("downloaded audio video_id=%s path=%s", video_id, final)
    return final


def _download_problem(
    info: MediaInfo, expected_duration_s: float | None, runner: Runner | None
) -> str | None:
    if not info.audio_streams:
        return f"{info.path.name} has no audio stream"
    if expected_duration_s is None:
        return None
    played_s = _played_duration_s(info.path, runner)
    if not _durations_agree(
        played_s,
        expected_duration_s,
        tolerance_s=DOWNLOAD_DURATION_TOLERANCE_S,
        tolerance_fraction=DOWNLOAD_DURATION_TOLERANCE_FRACTION,
    ):
        return (
            f"{info.path.name} plays for {played_s:.3f} s, but the source reports "
            f"{expected_duration_s:.3f} s (audio duplicated or missing)"
        )
    return None


def _played_duration_s(path: Path, runner: Runner | None) -> float:
    """Return how long FFmpeg reads the first audio stream to be, by copying it to nowhere.

    ffprobe's container duration can't be used: for MPEG-TS it comes from the first and last
    timestamps, so segments that a broken resume wrote twice don't change it. FFmpeg, reading
    the stream, moves timestamps that jump back to the end, and the copy gets longer (as the WAV
    from `normalize_audio` would). This reads the whole file but decodes nothing.
    """
    command = ffmpeg_command(
        *("-progress", "pipe:1", "-i", path, "-map", "0:a:0"),
        *("-c", "copy", "-f", "null", "-"),
    )
    result = run_command(command, timeout_s=DOWNLOAD_CHECK_TIMEOUT_S, runner=runner)
    # -progress prints key=value blocks; the last out_time_us is where the copy ended.
    times = re.findall(rb"^out_time_us=(\d+)\r?$", result.stdout, re.MULTILINE)
    if not times:
        raise FFmpegOutputError(f"ffmpeg reported no out_time_us while reading {path}")
    return int(times[-1]) / 1_000_000


def _container_extension(info: MediaInfo) -> str | None:
    # ffprobe's format_name can list several names, e.g. "mov,mp4,m4a,3gp,3g2,mj2".
    names = info.format_name.split(",")
    return next((_CONTAINER_EXTENSIONS[n] for n in names if n in _CONTAINER_EXTENSIONS), None)


def _durations_agree(
    actual_s: float, expected_s: float, *, tolerance_s: float, tolerance_fraction: float
) -> bool:
    return abs(actual_s - expected_s) <= max(tolerance_s, tolerance_fraction * expected_s)


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

    The WAV is 16 kHz, mono (channels mixed down), 16-bit PCM, and keeps the source's timeline:
    gaps in the audio's timestamps become silence, so a time in the WAV is the same time in the
    source. FFmpeg writes a temporary file, which is probed and checked before it is renamed into
    place: the format must be right, and its duration must match the source's container
    duration within `NORMALIZED_DURATION_TOLERANCE_S` or
    `NORMALIZED_DURATION_TOLERANCE_FRACTION`, whichever is larger. A shorter WAV means part of
    the source couldn't be decoded (FFmpeg skips undecodable data and still exits with 0); a
    longer one, that the source's timestamps jump back. If the WAV exists already and passes the
    same checks, it is returned (pass `force` to convert again).

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
            `probe`); `FFmpegOutputError` if `source` has no audio stream, or the WAV has the
            wrong format or duration (it is not kept).
    """
    output = normalized_audio_path(data_dir, video_id)
    if not source.is_file():
        raise FileNotFoundError(f"audio file not found: {source}")
    source_info = probe(source, runner=runner)
    if not source_info.audio_streams:
        raise FFmpegOutputError(f"{source} has no audio stream")
    if output.exists() and not force:
        try:
            info = _check_normalized(probe(output, runner=runner), source_info)
        except (FFmpegExitError, FFmpegOutputError) as exc:
            log.warning(
                "converting again video_id=%s existing=%s problem=%s", video_id, output, exc
            )
        else:
            log.info("audio already normalized video_id=%s path=%s", video_id, output)
            return NormalizedAudio(output, info.duration_s)

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f"{output.name}.part")
    command = ffmpeg_command(
        "-y",  # A leftover partial file from an interrupted run is replaced.
        "-i",
        source,
        "-map",
        "0:a:0",  # The first audio stream only.
        "-map_metadata",
        "-1",
        "-af",
        # Follow the timestamps: fill gaps with silence and drop overlaps, starting at 0.
        # Without it FFmpeg joins the samples, and everything after a gap moves earlier.
        "aresample=async=1:first_pts=0",
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
        info = _check_normalized(probe(partial, runner=runner), source_info)
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    log.info(
        "normalized audio video_id=%s path=%s duration_s=%.3f", video_id, output, info.duration_s
    )
    return NormalizedAudio(output, info.duration_s)


def _check_normalized(info: MediaInfo, source: MediaInfo) -> MediaInfo:
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
    if not _durations_agree(
        info.duration_s,
        source.duration_s,
        tolerance_s=NORMALIZED_DURATION_TOLERANCE_S,
        tolerance_fraction=NORMALIZED_DURATION_TOLERANCE_FRACTION,
    ):
        reason = (
            "part of the source could not be decoded (damaged or truncated file)"
            if info.duration_s < source.duration_s
            else "the source's timestamps jump back (repeated audio)"
        )
        raise FFmpegOutputError(
            f"{info.path} is {info.duration_s:.3f} s long, but {source.path} is "
            f"{source.duration_s:.3f} s: {reason}"
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
    source = download_audio(video_id, data_dir=data_dir, downloader=downloader, runner=runner)
    return normalize_audio(source, video_id, data_dir=data_dir, runner=runner)
