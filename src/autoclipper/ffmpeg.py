"""Run FFmpeg and FFprobe as external programs, and read media metadata with `probe`.

Every invocation is an `FFmpegCommand`: an argument list, never a shell string. Code that needs
FFmpeg builds commands with `ffmpeg_command` or `ffprobe_command` (plain data, so unit tests can
compare them without FFmpeg installed) and executes them with `run_command`, which hands them to
a `Runner`. The default runner, `run_subprocess`, starts the binary found on ``PATH``; tests pass
a fake runner that records commands and returns canned results.

Every failure raises a subclass of `FFmpegError`, so a caller can map all of them to one exit code
(docs/development/conventions.md §7, external failure):

- `FFmpegNotFoundError`: the program is not on ``PATH``.
- `FFmpegTimeoutError`: the process ran longer than its timeout and was killed.
- `FFmpegExitError`: the process exited with a non-zero code.
- `FFmpegOutputError`: the process succeeded, but its output is missing something or can't be
  parsed.

Error messages are bounded: they carry the end of the program's stderr (`STDERR_TAIL_BYTES`) and
a shortened command line (`COMMAND_SUMMARY_CHARS`), whatever the program printed.
"""

import json
import logging
import math
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Protocol

log = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Install instructions that FFmpegNotFoundError points to.
SETUP_DOC = "docs/development/setup.md"

# Global options that `ffmpeg_command` puts first. -nostdin: never read keyboard commands from
# stdin (the runner gives the process no stdin anyway). -loglevel error: stderr holds only errors,
# so the tail in an FFmpegExitError is the useful part. -nostats: no progress lines either, even
# when a command raises the log level to debug something.
FFMPEG_GLOBAL_OPTIONS: tuple[str, ...] = (
    "-hide_banner",
    "-nostdin",
    "-nostats",
    "-loglevel",
    "error",
)
FFPROBE_GLOBAL_OPTIONS: tuple[str, ...] = ("-hide_banner", "-loglevel", "error")

# Default timeouts. Both read only file headers, so they finish in well under a second normally;
# the margin covers slow disks and antivirus scans on first launch (Windows).
PROBE_TIMEOUT_S = 60.0
VERSION_TIMEOUT_S = 30.0

# Upper bounds for what an exception message carries.
STDERR_TAIL_BYTES = 4096
COMMAND_SUMMARY_CHARS = 1000

# The ffprobe fields `probe` reads. Asking for only these (instead of -show_format and
# -show_streams) keeps the output small and leaves out metadata tags, which can hold any text.
_PROBE_ENTRIES = (
    "format=format_name,duration"
    ":stream=index,codec_type,codec_name,width,height,sample_rate,channels"
)

# The first line of `ffmpeg -version` / `ffprobe -version`: "<name> version <version> Copyright…".
_VERSION_LINE = re.compile(r"(?P<name>\S+) version (?P<version>\S+)")

# FFmpeg's fatal-level message when it won't overwrite an existing output file because -y is
# missing. Some versions, 9.0.2 among them, then exit with code 0 without writing anything. It is
# a line of its own, after any prefix the -loglevel flags add (time or datetime, then level):
# "12:00:00.000 [fatal] File 'out.wav' already exists. Exiting." Only whole lines count, so a
# metadata title that quotes the message (FFmpeg prints titles at -loglevel info) doesn't.
_REFUSED_OVERWRITE = re.compile(
    rb"^(?:(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}\.\d{3} )?(?:\[fatal\] )?"
    rb"File '.*' already exists\. Exiting\.\r?$",
    re.MULTILINE,
)

# ANSI color codes ("\x1b[38;5;0m", "\x1b[0m") that FFmpeg puts around its messages when stderr
# is a terminal or AV_LOG_FORCE_COLOR is set. They're removed before looking for the line above.
_ANSI_COLOR = re.compile(rb"\x1b\[[0-9;]*m")


# --- Commands ----------------------------------------------------------------------------------


type CommandArg = str | os.PathLike[str]


@dataclass(frozen=True, slots=True)
class FFmpegCommand:
    """One FFmpeg or FFprobe invocation as an argument list.

    Build commands with `ffmpeg_command` or `ffprobe_command`; construct one directly only for a
    different program or option set (for example ``FFmpegCommand(("ffmpeg", "-version"))``).

    Attributes:
        args: The program (a name looked up on ``PATH``, or a path to an executable) followed by
            its arguments, exactly as the process receives them. No shell ever parses them, so
            arguments need no quoting or escaping.
    """

    args: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.args or not self.args[0]:
            raise ValueError("an FFmpeg command needs a program name as its first argument")
        for arg in self.args:
            if not isinstance(arg, str):
                raise TypeError(f"command arguments must be str, got {type(arg).__name__}: {arg!r}")

    @property
    def program(self) -> str:
        """The program to run: the first argument."""
        return self.args[0]

    def __str__(self) -> str:
        # Shell-quoted for reading in logs and error messages only; never executed.
        return shlex.join(self.args)


def ffmpeg_command(*args: CommandArg) -> FFmpegCommand:
    """Build an ``ffmpeg`` command: `FFMPEG_GLOBAL_OPTIONS`, then `args`.

    Overwriting is the caller's choice: pass ``-y`` to replace an existing output file. Without
    it FFmpeg refuses (it can't ask, because it has no stdin), and `run_command` raises
    `FFmpegExitError`, also on FFmpeg versions that exit with code 0 in that case.

    Args:
        args: Arguments in order. Strings are used unchanged. Paths (`os.PathLike`) become
            absolute path strings, so FFmpeg can't read a relative file name as an option
            (``-x.mp4``) or as a URL protocol (``clip:1.mp4``).

    Returns:
        The command; nothing runs until it is passed to `run_command`.
    """
    return FFmpegCommand((FFMPEG, *FFMPEG_GLOBAL_OPTIONS, *map(_to_arg, args)))


def ffprobe_command(*args: CommandArg) -> FFmpegCommand:
    """Build an ``ffprobe`` command: `FFPROBE_GLOBAL_OPTIONS`, then `args`.

    Args:
        args: Arguments in order, converted as in `ffmpeg_command`.

    Returns:
        The command; nothing runs until it is passed to `run_command`.
    """
    return FFmpegCommand((FFPROBE, *FFPROBE_GLOBAL_OPTIONS, *map(_to_arg, args)))


def _to_arg(value: CommandArg) -> str:
    if isinstance(value, str):
        return value
    return os.fspath(Path(value).absolute())


# --- Errors ------------------------------------------------------------------------------------


class FFmpegError(Exception):
    """An FFmpeg or FFprobe invocation failed. Base class of every error this module raises."""


class FFmpegNotFoundError(FFmpegError):
    """The program is not on ``PATH`` (FFmpeg is not installed, or the terminal predates it).

    Attributes:
        program: The program name or path that could not be found.
    """

    program: str

    def __init__(self, program: str) -> None:
        self.program = program
        super().__init__(
            f"{program!r} was not found on PATH. Install FFmpeg, which provides ffmpeg and "
            f"ffprobe, as described in {SETUP_DOC}, then open a new terminal so PATH is reloaded."
        )


class FFmpegTimeoutError(FFmpegError):
    """The process ran longer than its timeout and was killed.

    Its output files may be incomplete; write to a temporary path and rename on success.

    Attributes:
        command: The command that timed out.
        timeout_s: The timeout that was exceeded, in seconds.
        stderr_tail: The end of what the process wrote to stderr before it was killed (at most
            `STDERR_TAIL_BYTES` bytes, decoded).
    """

    command: FFmpegCommand
    timeout_s: float
    stderr_tail: str

    def __init__(self, command: FFmpegCommand, timeout_s: float, stderr: bytes) -> None:
        self.command = command
        self.timeout_s = timeout_s
        self.stderr_tail = _stderr_tail(stderr)
        super().__init__(
            f"{command.program} did not finish within {timeout_s:g} s and was killed: "
            f"{_summarize(command)}\n{self.stderr_tail or '(no output on stderr)'}"
        )


class FFmpegExitError(FFmpegError):
    """The process failed: a non-zero exit code, or FFmpeg refused to overwrite an output file.

    FFmpeg won't overwrite an existing output file without ``-y``, and some versions (9.0.2 among
    them) then exit with code 0 without writing anything; `run_command` reports that as this error
    too, with `returncode` 0.

    Attributes:
        command: The command that failed.
        returncode: The exit code (negative on POSIX when a signal ended the process).
        stderr_tail: The end of what the process wrote to stderr (at most `STDERR_TAIL_BYTES`
            bytes, decoded). With the default ``-loglevel error`` this is the error message.
    """

    command: FFmpegCommand
    returncode: int
    stderr_tail: str

    def __init__(self, command: FFmpegCommand, returncode: int, stderr: bytes) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr_tail = _stderr_tail(stderr)
        # Versions differ in the exit code after refusing (0 for 9.0.2, 1 for 5.1), so the
        # message, not the code, decides which headline fits.
        if _refused_overwrite(stderr):
            headline = f"{command.program} did not overwrite an existing output file (pass -y)"
        else:
            headline = f"{command.program} exited with code {returncode}"
        super().__init__(
            f"{headline}: {_summarize(command)}\n{self.stderr_tail or '(no output on stderr)'}"
        )


class FFmpegOutputError(FFmpegError):
    """The process succeeded, but its output is missing something or can't be parsed."""


def _refused_overwrite(stderr: bytes) -> bool:
    return _REFUSED_OVERWRITE.search(_ANSI_COLOR.sub(b"", stderr)) is not None


def _stderr_tail(stderr: bytes) -> str:
    truncated = len(stderr) > STDERR_TAIL_BYTES
    # FFmpeg writes UTF-8; undecodable bytes (a cut multi-byte character, a legacy-encoded file
    # name) become U+FFFD instead of hiding the error behind a UnicodeDecodeError.
    text = stderr[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace").replace("\r\n", "\n")
    if truncated:
        # The first line is probably cut; drop it if there is a later one.
        _first, newline, rest = text.partition("\n")
        text = "[earlier output truncated]\n" + (rest if newline else text)
    return text.strip()


def _summarize(command: FFmpegCommand) -> str:
    text = str(command)
    if len(text) <= COMMAND_SUMMARY_CHARS:
        return text
    # Keep both ends: the program and inputs come first, the output path last.
    half = COMMAND_SUMMARY_CHARS // 2
    return f"{text[:half]} [...] {text[-half:]}"


# --- Running -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What a finished process returned.

    Attributes:
        returncode: The exit code.
        stdout: Everything the process wrote to standard output.
        stderr: Everything the process wrote to standard error.
    """

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class Runner(Protocol):
    """Executes an `FFmpegCommand` and returns its result, whatever the exit code.

    `run_subprocess` is the real implementation. A runner raises `FFmpegNotFoundError` when the
    program can't be found and `FFmpegTimeoutError` when it runs longer than `timeout_s`.
    `run_command` decides what counts as failure (`FFmpegExitError`), so a fake runner in a test
    only needs to record the command and return a `CommandResult`.
    """

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        """Run `command`, killing it after `timeout_s` seconds, and return its result."""
        ...


def find_program(program: str) -> Path:
    """Locate a program the way `run_subprocess` does, with `shutil.which`.

    Args:
        program: A name to look up on ``PATH`` (``"ffmpeg"``), or a path to an executable.

    Returns:
        The full path of the executable.

    Raises:
        FFmpegNotFoundError: The program is not on ``PATH`` or not executable.
    """
    found = shutil.which(program)
    if found is None:
        raise FFmpegNotFoundError(program)
    return Path(found)


def run_subprocess(command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
    """Run `command` as a child process and capture its output (the default `Runner`).

    The process gets no stdin and its stdout and stderr are captured in memory, so commands
    should write media to files, not to stdout. Use `run_command` rather than calling this
    directly: it validates the timeout and checks the exit code.

    Args:
        command: The command. Its program is resolved with `find_program`, and that exact
            executable is started (on Windows, starting a bare name would search other
            directories first).
        timeout_s: Seconds the process may run before it is killed.

    Returns:
        The exit code and captured output, whatever the exit code.

    Raises:
        FFmpegNotFoundError: The program is not on ``PATH``.
        FFmpegTimeoutError: The process ran longer than `timeout_s`; it has been killed.
        FFmpegError: The program exists but could not be started (for example, permission
            denied).
    """
    executable = find_program(command.program)
    try:
        # An argument list and no shell: nothing in the arguments is interpreted.
        completed = subprocess.run(
            [os.fspath(executable), *command.args[1:]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegTimeoutError(command, timeout_s, exc.stderr or b"") from exc
    except FileNotFoundError as exc:
        # Removed between the lookup and the start.
        raise FFmpegNotFoundError(command.program) from exc
    except OSError as exc:
        raise FFmpegError(f"could not start {command.program} ({executable}): {exc}") from exc
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def run_command(
    command: FFmpegCommand, *, timeout_s: float, runner: Runner | None = None
) -> CommandResult:
    """Run `command` and return its output; raise if it can't run, times out or fails.

    Args:
        command: The command to run.
        timeout_s: Seconds the process may run before it is killed. Required because FFmpeg
            jobs range from milliseconds (a probe) to many minutes (an encode); choose one that
            fits the job, for example a multiple of the media duration.
        runner: Executes the command. Defaults to `run_subprocess`; tests pass a fake.

    Returns:
        The result of a process that exited with code 0.

    Raises:
        ValueError: `timeout_s` is not a positive, finite number.
        FFmpegNotFoundError: The program is not on ``PATH``.
        FFmpegTimeoutError: The process ran longer than `timeout_s`.
        FFmpegExitError: The process exited with a non-zero code, or FFmpeg refused to
            overwrite an existing output file because ``-y`` is missing.
    """
    if not (math.isfinite(timeout_s) and timeout_s > 0):
        raise ValueError(f"timeout_s must be a positive number of seconds, got {timeout_s!r}")
    execute = run_subprocess if runner is None else runner
    log.debug("ffmpeg run program=%s timeout_s=%g command=%s", command.program, timeout_s, command)
    started = time.monotonic()
    result = execute(command, timeout_s=timeout_s)
    elapsed_s = time.monotonic() - started
    if result.returncode != 0 or _refused_overwrite(result.stderr):
        raise FFmpegExitError(command, result.returncode, result.stderr)
    log.debug("ffmpeg done program=%s elapsed_s=%.3f", command.program, elapsed_s)
    return result


# --- Version -----------------------------------------------------------------------------------


def ffmpeg_version(
    program: str = FFMPEG, *, runner: Runner | None = None, timeout_s: float = VERSION_TIMEOUT_S
) -> str:
    """Return the version of the installed FFmpeg (or FFprobe) as the build reports it.

    The version is the word after ``version`` on the first line of ``<program> -version``. It is
    returned unchanged because builds format it differently: ``"7.1"`` (release),
    ``"6.1.1-3ubuntu5"`` (Ubuntu), ``"9.0.2-full_build-www.gyan.dev"`` (Gyan.dev, Windows),
    ``"n7.1"`` or ``"N-117000-g1a2b3c4d5e-20240901"`` (git builds). Use it to record provenance,
    for example in a catalog ``tool`` column, not to compare versions.

    Args:
        program: ``"ffmpeg"`` (default), ``"ffprobe"``, or a path to either.
        runner: Executes the command. Defaults to `run_subprocess`; tests pass a fake.
        timeout_s: Seconds ``-version`` may take.

    Returns:
        The version string, e.g. ``"9.0.2-full_build-www.gyan.dev"``.

    Raises:
        FFmpegNotFoundError, FFmpegTimeoutError, FFmpegExitError: As for `run_command`.
        FFmpegOutputError: The first line of the output does not name a version.
    """
    result = run_command(FFmpegCommand((program, "-version")), timeout_s=timeout_s, runner=runner)
    first_line = result.stdout.decode("utf-8", errors="replace").strip().partition("\n")[0]
    match = _VERSION_LINE.match(first_line.strip())
    if match is None:
        raise FFmpegOutputError(
            f"could not read a version from `{program} -version`; its first line was "
            f"{_preview(first_line)}"
        )
    return match["version"]


# --- Probe -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VideoStream:
    """A video stream in a media file.

    Attributes:
        index: The stream's index in the file, as in FFmpeg's ``-map 0:<index>``.
        codec_name: FFmpeg's short codec name, e.g. ``"h264"``.
        width: Frame width in pixels, as stored (rotation metadata is not applied).
        height: Frame height in pixels, as stored.
    """

    codec_type: ClassVar[Literal["video"]] = "video"

    index: int
    codec_name: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class AudioStream:
    """An audio stream in a media file.

    Attributes:
        index: The stream's index in the file, as in FFmpeg's ``-map 0:<index>``.
        codec_name: FFmpeg's short codec name, e.g. ``"aac"`` or ``"pcm_s16le"``.
        sample_rate_hz: Samples per second per channel, in Hz.
        channels: Number of audio channels (1 = mono, 2 = stereo).
    """

    codec_type: ClassVar[Literal["audio"]] = "audio"

    index: int
    codec_name: str
    sample_rate_hz: int
    channels: int


@dataclass(frozen=True, slots=True)
class OtherStream:
    """A stream that is neither video nor audio (subtitles, data, attachments).

    Attributes:
        index: The stream's index in the file, as in FFmpeg's ``-map 0:<index>``.
        codec_type: FFmpeg's stream type, e.g. ``"subtitle"`` or ``"data"``.
        codec_name: FFmpeg's short codec name, or None when FFmpeg doesn't recognize the codec.
    """

    index: int
    codec_type: str
    codec_name: str | None


type Stream = VideoStream | AudioStream | OtherStream


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """What `probe` read from a media file.

    Attributes:
        path: The file that was probed.
        format_name: FFmpeg's container format name; for some formats a comma-separated list of
            names, e.g. ``"mov,mp4,m4a,3gp,3g2,mj2"`` for MP4.
        duration_s: Duration of the whole file in seconds, as the container reports it.
        streams: Every stream in the file, in file order.
    """

    path: Path
    format_name: str
    duration_s: float
    streams: tuple[Stream, ...]

    @property
    def video_streams(self) -> tuple[VideoStream, ...]:
        """The video streams, in file order."""
        return tuple(s for s in self.streams if isinstance(s, VideoStream))

    @property
    def audio_streams(self) -> tuple[AudioStream, ...]:
        """The audio streams, in file order."""
        return tuple(s for s in self.streams if isinstance(s, AudioStream))


def probe(
    path: Path, *, runner: Runner | None = None, timeout_s: float = PROBE_TIMEOUT_S
) -> MediaInfo:
    """Read a media file's container format, duration and streams with ffprobe.

    Runs ``ffprobe -print_format json -show_entries …`` and validates every field it uses. Nothing
    is estimated: a missing or invalid field is an error. In particular, `MediaInfo.duration_s` is
    the container (format) duration only. Files without one, such as a raw ``.h264`` or ``.aac``
    stream, raise `FFmpegOutputError` instead of falling back to stream durations, which can
    disagree with each other; remux such a file into a container (MP4, MKV, WAV) first.

    Args:
        path: The media file.
        runner: Executes the command. Defaults to `run_subprocess`; tests pass a fake.
        timeout_s: Seconds ffprobe may take.

    Returns:
        The file's format, duration and streams.

    Raises:
        FileNotFoundError: `path` is not an existing file. Checked before ffprobe runs.
        FFmpegNotFoundError, FFmpegTimeoutError: As for `run_command`.
        FFmpegExitError: ffprobe failed, for example because the file is not a media file.
        FFmpegOutputError: The output is not the expected JSON, or a required field is missing
            or invalid (no duration, a video stream without a size, an audio stream without a
            sample rate, ...).
    """
    if not path.is_file():
        raise FileNotFoundError(f"media file not found: {path}")
    command = ffprobe_command("-print_format", "json", "-show_entries", _PROBE_ENTRIES, path)
    result = run_command(command, timeout_s=timeout_s, runner=runner)
    return _parse_probe_output(result.stdout, path)


def _parse_probe_output(stdout: bytes, path: Path) -> MediaInfo:
    try:
        return _parse_probe_json(stdout, path)
    except _InvalidProbeOutput as exc:
        raise FFmpegOutputError(f"unexpected ffprobe output for {path}: {exc}") from None


def _parse_probe_json(stdout: bytes, path: Path) -> MediaInfo:
    try:
        data: object = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise _InvalidProbeOutput(f"not valid JSON ({exc})") from exc
    root = _as_object(data, "the output")

    container = _as_object(root.get("format"), "'format'")
    format_name = _get_str(container, "format_name", "format")
    if not _is_present(container.get("duration")):
        raise _InvalidProbeOutput(
            "the container reports no duration (raw streams such as .h264 or .aac have none); "
            "remux the file into a container such as MP4, MKV or WAV"
        )
    duration_s = _get_float(container, "duration", "format")

    raw_streams = root.get("streams")
    if not isinstance(raw_streams, list):
        raise _InvalidProbeOutput(f"'streams' should be a list, got {_preview(raw_streams)}")
    streams: list[Stream] = []
    for position, raw_stream in enumerate(raw_streams):
        stream = _as_object(raw_stream, f"stream #{position}")
        index = _get_int(stream, "index", f"stream #{position}", minimum=0)
        where = f"stream {index}"
        codec_type = _get_str(stream, "codec_type", where)
        if codec_type == "video":
            streams.append(
                VideoStream(
                    index=index,
                    codec_name=_get_str(stream, "codec_name", where),
                    width=_get_int(stream, "width", where, minimum=1),
                    height=_get_int(stream, "height", where, minimum=1),
                )
            )
        elif codec_type == "audio":
            streams.append(
                AudioStream(
                    index=index,
                    codec_name=_get_str(stream, "codec_name", where),
                    sample_rate_hz=_get_int(stream, "sample_rate", where, minimum=1),
                    channels=_get_int(stream, "channels", where, minimum=1),
                )
            )
        else:
            codec_name = None
            if _is_present(stream.get("codec_name")):
                codec_name = _get_str(stream, "codec_name", where)
            streams.append(OtherStream(index=index, codec_type=codec_type, codec_name=codec_name))

    return MediaInfo(
        path=path, format_name=format_name, duration_s=duration_s, streams=tuple(streams)
    )


# --- JSON field helpers (ffprobe output) -------------------------------------------------------


class _InvalidProbeOutput(ValueError):
    """A field in ffprobe's JSON is missing or invalid; `_parse_probe_output` adds the path."""


def _is_present(value: object) -> bool:
    # ffprobe leaves unknown values out of its JSON, but some builds print "N/A" instead.
    return value is not None and value != "N/A"


def _as_object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _InvalidProbeOutput(f"{where} should be a JSON object, got {_preview(value)}")
    return value


def _get_present(container: dict[str, object], key: str, where: str) -> object:
    value = container.get(key)
    if not _is_present(value):
        raise _InvalidProbeOutput(f"{where} has no {key!r}")
    return value


def _get_str(container: dict[str, object], key: str, where: str) -> str:
    value = _get_present(container, key, where)
    if not isinstance(value, str) or not value:
        raise _InvalidProbeOutput(
            f"{where} {key!r} should be a non-empty string, got {_preview(value)}"
        )
    return value


def _get_int(container: dict[str, object], key: str, where: str, *, minimum: int) -> int:
    value = _get_present(container, key, where)
    # ffprobe writes some integers as JSON numbers (width) and others as strings (sample_rate).
    number: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        number = int(value)
    if number is None or number < minimum:
        raise _InvalidProbeOutput(
            f"{where} {key!r} should be an integer >= {minimum}, got {_preview(value)}"
        )
    return number


def _get_float(container: dict[str, object], key: str, where: str) -> float:
    value = _get_present(container, key, where)
    number: float | None = None
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            number = None
    if number is None or not math.isfinite(number) or number < 0:
        raise _InvalidProbeOutput(
            f"{where} {key!r} should be a number of seconds >= 0, got {_preview(value)}"
        )
    return number


def _preview(value: object, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
