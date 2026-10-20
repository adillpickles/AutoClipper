"""Tests for `autoclipper.ffmpeg`: commands as data, running them, errors, version and probe.

Most tests need no FFmpeg: a fake runner returns canned output, and the real subprocess runner is
exercised with a Python child process standing in for FFmpeg. Tests marked `ffmpeg` (or using
`make_test_media`) run the real binaries; see tests/conftest.py.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from autoclipper.ffmpeg import (
    FFMPEG,
    FFMPEG_GLOBAL_OPTIONS,
    FFPROBE,
    FFPROBE_GLOBAL_OPTIONS,
    SETUP_DOC,
    STDERR_TAIL_BYTES,
    AudioStream,
    CommandResult,
    FFmpegCommand,
    FFmpegError,
    FFmpegExitError,
    FFmpegNotFoundError,
    FFmpegOutputError,
    FFmpegTimeoutError,
    OtherStream,
    VideoStream,
    ffmpeg_command,
    ffmpeg_version,
    ffprobe_command,
    probe,
    run_command,
)


class RecordingRunner:
    """A fake `Runner`: records every command and returns the same canned result."""

    def __init__(self, result: CommandResult | None = None) -> None:
        self.result = CommandResult(0) if result is None else result
        self.calls: list[tuple[FFmpegCommand, float]] = []

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        self.calls.append((command, timeout_s))
        return self.result


def python_command(code: str, *args: str) -> FFmpegCommand:
    # The real runner starts any program. A Python child stands in for FFmpeg, so these tests run
    # without FFmpeg and control exactly what the process does.
    return FFmpegCommand((sys.executable, "-c", code, *args))


# --- Commands ----------------------------------------------------------------------------------


def test_ffmpeg_command_starts_with_program_and_global_options() -> None:
    command = ffmpeg_command("-i", "in.mp4", "out.wav")

    assert command.args == (FFMPEG, *FFMPEG_GLOBAL_OPTIONS, "-i", "in.mp4", "out.wav")
    assert command.program == FFMPEG
    assert {"-nostdin", "-nostats"} <= set(command.args)


def test_ffprobe_command_starts_with_program_and_global_options() -> None:
    command = ffprobe_command("-show_format", "in.mp4")

    assert command.args == (FFPROBE, *FFPROBE_GLOBAL_OPTIONS, "-show_format", "in.mp4")


def test_path_arguments_become_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    command = ffmpeg_command("-i", Path("-input.mp4"))

    # Absolute, so FFmpeg can't read the leading "-" as an option.
    assert command.args[-1] == os.fspath(tmp_path / "-input.mp4")


@pytest.mark.parametrize(
    "arg",
    ["-", "clip:1.mp4", "C:\\Program Files\\x y.mp4", "say \"hi\" 'there'", "100%", "", " "],
)
def test_string_arguments_are_passed_unchanged(arg: str) -> None:
    assert ffmpeg_command(arg).args[-1] == arg


def test_path_with_spaces_and_non_ascii_stays_one_unquoted_argument(tmp_path: Path) -> None:
    # On Windows this is a backslash path such as C:\...\my clips\clip 1 é.mp4.
    path = tmp_path / "my clips" / "clip 1 é.mp4"

    command = ffmpeg_command("-i", path, "-f", "null", "-")

    assert command.args[-4:] == (os.fspath(path), "-f", "null", "-")


def test_commands_are_immutable_values() -> None:
    command = ffmpeg_command("-version")

    assert command == ffmpeg_command("-version")
    assert hash(command) == hash(ffmpeg_command("-version"))
    with pytest.raises(FrozenInstanceError):
        # Assigning to a frozen dataclass field on purpose.
        command.args = ("rm",)  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize(
    ("args", "error"),
    [
        pytest.param((), ValueError, id="empty"),
        pytest.param(("",), ValueError, id="empty-program"),
        pytest.param(("ffmpeg", 1), TypeError, id="int-argument"),
        pytest.param(("ffmpeg", Path("in.mp4")), TypeError, id="path-argument"),
    ],
)
def test_command_rejects_invalid_arguments(
    args: tuple[object, ...], error: type[Exception]
) -> None:
    with pytest.raises(error):
        FFmpegCommand(args)  # pyright: ignore[reportArgumentType] - invalid on purpose.


def test_str_is_a_shell_quoted_command_line() -> None:
    assert str(FFmpegCommand(("ffmpeg", "-i", "my clip.mp4"))) == "ffmpeg -i 'my clip.mp4'"


# --- run_command and errors (fake runner) ------------------------------------------------------


def test_run_command_returns_the_result_and_passes_the_timeout() -> None:
    runner = RecordingRunner(CommandResult(0, stdout=b"out", stderr=b"warn"))
    command = ffmpeg_command("-version")

    result = run_command(command, timeout_s=12.5, runner=runner)

    assert result == CommandResult(0, stdout=b"out", stderr=b"warn")
    assert runner.calls == [(command, 12.5)]


def test_non_zero_exit_raises_exit_error_with_code_and_stderr() -> None:
    runner = RecordingRunner(CommandResult(1, stderr=b"in.mp4: No such file or directory\n"))
    command = ffmpeg_command("-i", "in.mp4", "out.wav")

    with pytest.raises(FFmpegExitError) as excinfo:
        run_command(command, timeout_s=5, runner=runner)

    error = excinfo.value
    assert error.returncode == 1
    assert error.command == command
    assert error.stderr_tail == "in.mp4: No such file or directory"
    assert "ffmpeg exited with code 1" in str(error)
    assert "No such file or directory" in str(error)


def test_exit_error_keeps_only_a_bounded_stderr_tail() -> None:
    stderr = b"".join(b"noise line %d\n" % i for i in range(100_000)) + b"the real error\n"
    runner = RecordingRunner(CommandResult(1, stderr=stderr))

    with pytest.raises(FFmpegExitError) as excinfo:
        run_command(ffmpeg_command("-version"), timeout_s=5, runner=runner)

    tail = excinfo.value.stderr_tail
    assert tail.startswith("[earlier output truncated]\n")
    assert tail.endswith("the real error")
    assert "noise line 0\n" not in tail
    assert len(tail) <= STDERR_TAIL_BYTES + len("[earlier output truncated]\n")
    assert len(str(excinfo.value)) < 2 * STDERR_TAIL_BYTES


def test_exit_error_decodes_invalid_utf8_stderr() -> None:
    runner = RecordingRunner(CommandResult(1, stderr=b"bad name \xff\xfe.mp4: Invalid data\n"))

    with pytest.raises(FFmpegExitError) as excinfo:
        run_command(ffmpeg_command("-version"), timeout_s=5, runner=runner)

    assert excinfo.value.stderr_tail == "bad name \ufffd\ufffd.mp4: Invalid data"


def test_refusing_to_overwrite_is_an_exit_error_even_with_exit_code_zero() -> None:
    # FFmpeg 9.0.2 without -y on an existing output: this stderr, exit code 0, nothing written.
    stderr = b"File '/clips/out.wav' already exists. Exiting.\nError opening output file.\n"
    runner = RecordingRunner(CommandResult(0, stderr=stderr))

    with pytest.raises(FFmpegExitError) as excinfo:
        run_command(ffmpeg_command("-i", "in.mp4", "/clips/out.wav"), timeout_s=5, runner=runner)

    assert excinfo.value.returncode == 0
    assert "did not overwrite an existing output file (pass -y)" in str(excinfo.value)
    assert "already exists" in excinfo.value.stderr_tail


def test_refusing_to_overwrite_with_a_nonzero_exit_code_gets_the_same_headline() -> None:
    # Regression test: FFmpeg 5.1 (Debian 12) prints the same message but exits with code 1.
    stderr = b"File '/clips/out.wav' already exists. Exiting.\n"
    runner = RecordingRunner(CommandResult(1, stderr=stderr))

    with pytest.raises(FFmpegExitError) as excinfo:
        run_command(ffmpeg_command("-i", "in.mp4", "/clips/out.wav"), timeout_s=5, runner=runner)

    assert excinfo.value.returncode == 1
    assert "did not overwrite an existing output file (pass -y)" in str(excinfo.value)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"File '/clips/out.wav' already exists. Exiting.\r\n", id="crlf"),
        pytest.param(b"[fatal] File '/clips/out.wav' already exists. Exiting.\n", id="level"),
        pytest.param(
            b"12:00:00.000 [fatal] File '/clips/out.wav' already exists. Exiting.\n",
            id="time-and-level",
        ),
        pytest.param(
            b"2026-10-20 12:00:00.000 File '/clips/out.wav' already exists. Exiting.\n",
            id="datetime",
        ),
    ],
)
def test_refusing_to_overwrite_is_recognized_after_log_prefixes(line: bytes) -> None:
    # As FFmpeg 9.0.2 prints it on Windows, and with the -loglevel flags time, datetime and level.
    runner = RecordingRunner(CommandResult(0, stderr=b"Input #0, wav, from 'in.wav':\n" + line))

    with pytest.raises(FFmpegExitError, match=r"did not overwrite an existing output file"):
        run_command(ffmpeg_command("-i", "in.wav", "/clips/out.wav"), timeout_s=5, runner=runner)


_METADATA_QUOTING_THE_REFUSAL = [
    pytest.param(
        b"    title           : File '/clips/out.wav' already exists. Exiting.\n", id="value"
    ),
    pytest.param(
        b"[info]     title           : File '/clips/out.wav' already exists. Exiting.\r\n",
        id="value-after-level",
    ),
    pytest.param(b"    File '/clips/out.wav' already exists. Exiting.: x\n", id="key"),
]


@pytest.mark.parametrize("line", _METADATA_QUOTING_THE_REFUSAL)
def test_metadata_quoting_the_refusal_does_not_fail_a_successful_run(line: bytes) -> None:
    # Regression test: at -loglevel info FFmpeg prints input metadata, and a title containing the
    # message made a run that exited with code 0 and wrote its output raise FFmpegExitError.
    result = CommandResult(0, stderr=b"Input #0, wav, from 'in.wav':\n  Metadata:\n" + line)
    command = ffmpeg_command("-loglevel", "info", "-y", "-i", "in.wav", "/clips/out.wav")

    assert run_command(command, timeout_s=5, runner=RecordingRunner(result)) == result


@pytest.mark.parametrize("line", _METADATA_QUOTING_THE_REFUSAL)
def test_metadata_quoting_the_refusal_does_not_change_the_exit_error_headline(line: bytes) -> None:
    runner = RecordingRunner(CommandResult(1, stderr=line + b"Conversion failed!\n"))

    with pytest.raises(FFmpegExitError, match="ffmpeg exited with code 1"):
        run_command(ffmpeg_command("-y", "-i", "in.wav", "out.wav"), timeout_s=5, runner=runner)


def test_exit_error_without_stderr_says_so() -> None:
    with pytest.raises(FFmpegExitError, match=r"\(no output on stderr\)"):
        run_command(
            ffmpeg_command("-version"), timeout_s=5, runner=RecordingRunner(CommandResult(2))
        )


def test_exit_error_shortens_very_long_commands() -> None:
    command = ffmpeg_command("-filter_complex", "x" * 100_000, "out.mp4")

    with pytest.raises(FFmpegExitError) as excinfo:
        run_command(command, timeout_s=5, runner=RecordingRunner(CommandResult(1)))

    message = str(excinfo.value)
    assert len(message) < 2_000
    assert " [...] " in message
    assert message.split("\n")[0].endswith("out.mp4")


@pytest.mark.parametrize("timeout_s", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected_before_running(timeout_s: float) -> None:
    runner = RecordingRunner()

    with pytest.raises(ValueError, match="timeout_s"):
        run_command(ffmpeg_command("-version"), timeout_s=timeout_s, runner=runner)

    assert runner.calls == []


@pytest.mark.parametrize(
    "error", [FFmpegNotFoundError, FFmpegTimeoutError, FFmpegExitError, FFmpegOutputError]
)
def test_every_error_is_an_ffmpeg_error(error: type[Exception]) -> None:
    # Callers catch FFmpegError once to report any FFmpeg failure (conventions §7).
    assert issubclass(error, FFmpegError)


# --- run_subprocess (real child processes, no FFmpeg needed) -----------------------------------


def test_subprocess_runner_passes_arguments_unchanged() -> None:
    args = [
        "C:\\Program Files\\My Clips\\clip 1.mp4",
        "trailing backslash\\",
        'say "hi"',
        "ünïcode 日本",
        "a:b",
        "",
        "%PATH% $HOME a;b|c&d",
    ]
    code = "import json, sys; sys.stdout.write(json.dumps(sys.argv[1:]))"

    result = run_command(python_command(code, *args), timeout_s=60)

    assert json.loads(result.stdout) == args


def test_subprocess_runner_gives_the_process_no_stdin() -> None:
    # A process that reads stdin gets end-of-file at once instead of waiting for input.
    code = "import sys; sys.stdout.write(repr(sys.stdin.read()))"

    result = run_command(python_command(code), timeout_s=60)

    assert result.stdout == b"''"


def test_subprocess_non_zero_exit_raises_with_bounded_stderr_tail() -> None:
    code = (
        "import sys\n"
        "sys.stderr.write(''.join(f'noise {i}\\n' for i in range(100_000)))\n"
        "sys.stderr.write('the real error\\n')\n"
        "sys.exit(3)\n"
    )

    with pytest.raises(FFmpegExitError) as excinfo:
        run_command(python_command(code), timeout_s=60)

    assert excinfo.value.returncode == 3
    assert excinfo.value.stderr_tail.endswith("the real error")
    assert len(excinfo.value.stderr_tail) <= STDERR_TAIL_BYTES + len("[earlier output truncated]\n")


def test_missing_program_raises_not_found_with_install_guidance() -> None:
    command = FFmpegCommand(("autoclipper-no-such-program", "-version"))

    with pytest.raises(FFmpegNotFoundError) as excinfo:
        run_command(command, timeout_s=5)

    assert excinfo.value.program == "autoclipper-no-such-program"
    assert SETUP_DOC in str(excinfo.value)


@pytest.mark.parametrize(
    ("os_error", "expected"),
    [
        # The program disappears between the PATH lookup and the start.
        pytest.param(FileNotFoundError(2, "gone"), FFmpegNotFoundError, id="removed"),
        pytest.param(PermissionError(13, "Access is denied"), FFmpegError, id="not-startable"),
    ],
)
def test_subprocess_start_failure_raises_ffmpeg_error(
    monkeypatch: pytest.MonkeyPatch, os_error: OSError, expected: type[FFmpegError]
) -> None:
    def fail_to_start(*_args: object, **_kwargs: object) -> None:
        raise os_error

    # Simulates the OS refusing to start the process, which can't be staged portably.
    monkeypatch.setattr(subprocess, "run", fail_to_start)

    with pytest.raises(expected) as excinfo:
        run_command(python_command("pass"), timeout_s=5)

    assert type(excinfo.value) is expected
    assert excinfo.value.__cause__ is os_error


@pytest.fixture
def empty_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH (and, for Windows lookups, the working directory) with no programs on it."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", os.fspath(empty))
    monkeypatch.chdir(empty)
    return empty


@pytest.mark.usefixtures("empty_path")
def test_ffmpeg_missing_from_path_raises_not_found() -> None:
    with pytest.raises(FFmpegNotFoundError, match="'ffmpeg' was not found on PATH"):
        run_command(ffmpeg_command("-version"), timeout_s=5)


def test_probe_with_ffprobe_missing_from_path_raises_not_found(empty_path: Path) -> None:
    media = empty_path / "clip.mp4"
    media.write_bytes(b"")

    with pytest.raises(FFmpegNotFoundError, match="'ffprobe' was not found on PATH"):
        probe(media)


def test_timeout_kills_the_process_and_raises() -> None:
    started = time.monotonic()

    with pytest.raises(FFmpegTimeoutError) as excinfo:
        run_command(python_command("import time; time.sleep(60)"), timeout_s=0.5)

    assert excinfo.value.timeout_s == 0.5
    assert "did not finish within 0.5 s and was killed" in str(excinfo.value)
    # The child was killed rather than waited for (it would sleep for 60 s).
    assert time.monotonic() - started < 30


# --- ffmpeg_version (fake runner) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("first_line", "version"),
    [
        pytest.param(
            "ffmpeg version 9.0.2-full_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg dev",
            "9.0.2-full_build-www.gyan.dev",
            id="gyan-release",
        ),
        pytest.param(
            "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers",
            "6.1.1-3ubuntu5",
            id="ubuntu",
        ),
        pytest.param("ffmpeg version n7.1 Copyright (c) 2000-2024", "n7.1", id="git-tag"),
        pytest.param(
            "ffmpeg version N-117000-g1a2b3c4d5e-20240901 Copyright (c) 2000-2024",
            "N-117000-g1a2b3c4d5e-20240901",
            id="git-master",
        ),
        pytest.param(
            "ffmpeg version 2026-10-01-git-0123abcd-essentials_build-www.gyan.dev Copyright",
            "2026-10-01-git-0123abcd-essentials_build-www.gyan.dev",
            id="gyan-git",
        ),
        pytest.param("ffmpeg version 7.1\r", "7.1", id="crlf-without-copyright"),
        pytest.param("ffprobe version 9.0.2 Copyright (c) 2007-2026", "9.0.2", id="ffprobe"),
    ],
)
def test_ffmpeg_version_reads_the_word_after_version(first_line: str, version: str) -> None:
    stdout = f"{first_line}\nbuilt with gcc 16.2.0\nconfiguration: --enable-gpl\n".encode()

    assert ffmpeg_version(runner=RecordingRunner(CommandResult(0, stdout=stdout))) == version


def test_ffmpeg_version_runs_the_program_with_version_flag() -> None:
    runner = RecordingRunner(CommandResult(0, stdout=b"ffprobe version 7.1 Copyright\n"))

    ffmpeg_version(FFPROBE, runner=runner)

    assert [command.args for command, _timeout_s in runner.calls] == [(FFPROBE, "-version")]


@pytest.mark.parametrize("stdout", [b"", b"\n\n", b"Usage: ffmpeg [options]\n", b"ffmpeg 7.1\n"])
def test_ffmpeg_version_rejects_unrecognized_output(stdout: bytes) -> None:
    with pytest.raises(FFmpegOutputError, match="could not read a version"):
        ffmpeg_version(runner=RecordingRunner(CommandResult(0, stdout=stdout)))


# --- probe (fake runner) -----------------------------------------------------------------------


# `Any`: the tests below mutate this parsed-JSON shape freely.
def sample_probe_output() -> dict[str, Any]:
    """ffprobe JSON for a one-hour 1080p H.264/AAC MP4, as `probe` requests it."""
    return {
        "programs": [],
        "stream_groups": [],
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
            },
            {
                "index": 1,
                "codec_name": "aac",
                "codec_type": "audio",
                "sample_rate": "48000",
                "channels": 2,
            },
        ],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "3600.250000"},
    }


@pytest.fixture
def media_file(tmp_path: Path) -> Path:
    """An existing (empty) file; the fake runner supplies what ffprobe would report."""
    path = tmp_path / "vod 1.mp4"
    path.write_bytes(b"")
    return path


def fake_ffprobe(output: object) -> RecordingRunner:
    return RecordingRunner(CommandResult(0, stdout=json.dumps(output).encode()))


def test_probe_reads_format_duration_and_streams(media_file: Path) -> None:
    info = probe(media_file, runner=fake_ffprobe(sample_probe_output()))

    assert info.path == media_file
    assert info.format_name == "mov,mp4,m4a,3gp,3g2,mj2"
    assert info.duration_s == pytest.approx(3600.25)
    assert info.video_streams == (VideoStream(index=0, codec_name="h264", width=1920, height=1080),)
    assert info.audio_streams == (
        AudioStream(index=1, codec_name="aac", sample_rate_hz=48_000, channels=2),
    )
    assert [stream.codec_type for stream in info.streams] == ["video", "audio"]


def test_probe_runs_ffprobe_with_json_output_on_the_file(media_file: Path) -> None:
    runner = fake_ffprobe(sample_probe_output())

    probe(media_file, runner=runner, timeout_s=7)

    [(command, timeout_s)] = runner.calls
    assert command.program == FFPROBE
    assert command.args[-1] == os.fspath(media_file)
    assert command.args[command.args.index("-print_format") + 1] == "json"
    assert timeout_s == 7


@pytest.mark.parametrize("name", ["missing.mp4", "."])
def test_probe_of_missing_file_raises_before_running_ffprobe(tmp_path: Path, name: str) -> None:
    runner = fake_ffprobe(sample_probe_output())

    with pytest.raises(FileNotFoundError, match="media file not found"):
        probe(tmp_path / name, runner=runner)

    assert runner.calls == []


def test_probe_accepts_integers_written_as_numbers_or_strings(media_file: Path) -> None:
    output = sample_probe_output()
    output["streams"][0].update(width="1280", height="720")
    output["streams"][1].update(sample_rate=44_100, channels="1")
    output["format"]["duration"] = 12

    info = probe(media_file, runner=fake_ffprobe(output))

    assert (info.video_streams[0].width, info.video_streams[0].height) == (1280, 720)
    assert (info.audio_streams[0].sample_rate_hz, info.audio_streams[0].channels) == (44_100, 1)
    assert info.duration_s == 12.0


def test_probe_keeps_streams_that_are_neither_video_nor_audio(media_file: Path) -> None:
    output = sample_probe_output()
    output["streams"] += [
        {"index": 2, "codec_name": "mov_text", "codec_type": "subtitle"},
        {"index": 3, "codec_type": "data"},
    ]

    info = probe(media_file, runner=fake_ffprobe(output))

    assert info.streams[2:] == (
        OtherStream(index=2, codec_type="subtitle", codec_name="mov_text"),
        OtherStream(index=3, codec_type="data", codec_name=None),
    )
    assert len(info.video_streams) == len(info.audio_streams) == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(
            lambda out: out.pop("format"), "'format' should be a JSON object", id="no-format"
        ),
        pytest.param(
            lambda out: out["format"].pop("format_name"),
            "format has no 'format_name'",
            id="no-name",
        ),
        pytest.param(
            lambda out: out["format"].pop("duration"), "reports no duration", id="no-duration"
        ),
        pytest.param(
            lambda out: out["format"].update(duration="N/A"),
            "reports no duration",
            id="n/a-duration",
        ),
        pytest.param(
            lambda out: out["format"].update(duration="soon"),
            "'duration' should be a number of seconds",
            id="text-duration",
        ),
        pytest.param(
            lambda out: out["format"].update(duration="-1.5"),
            "'duration' should be a number of seconds >= 0",
            id="negative-duration",
        ),
        pytest.param(
            lambda out: out["format"].update(duration="inf"),
            "'duration' should be a number of seconds",
            id="infinite-duration",
        ),
        pytest.param(
            lambda out: out["format"].update(duration=True),
            "'duration' should be a number of seconds",
            id="bool-duration",
        ),
        pytest.param(lambda out: out.pop("streams"), "'streams' should be a list", id="no-streams"),
        pytest.param(
            lambda out: out["streams"].append("video"),
            "stream #2 should be a JSON object",
            id="stream-not-object",
        ),
        pytest.param(
            lambda out: out["streams"][0].pop("index"), "stream #0 has no 'index'", id="no-index"
        ),
        pytest.param(
            lambda out: out["streams"][0].pop("codec_type"),
            "stream 0 has no 'codec_type'",
            id="no-codec-type",
        ),
        pytest.param(
            lambda out: out["streams"][0].pop("codec_name"),
            "stream 0 has no 'codec_name'",
            id="video-no-codec",
        ),
        pytest.param(
            lambda out: out["streams"][1].update(codec_name=""),
            "stream 1 'codec_name' should be a non-empty string",
            id="audio-empty-codec",
        ),
        pytest.param(
            lambda out: out["streams"][0].pop("width"), "stream 0 has no 'width'", id="no-width"
        ),
        pytest.param(
            lambda out: out["streams"][0].update(height=0),
            "stream 0 'height' should be an integer >= 1",
            id="zero-height",
        ),
        pytest.param(
            lambda out: out["streams"][0].update(width=True),
            "stream 0 'width' should be an integer >= 1",
            id="bool-width",
        ),
        pytest.param(
            lambda out: out["streams"][1].pop("sample_rate"),
            "stream 1 has no 'sample_rate'",
            id="no-sample-rate",
        ),
        pytest.param(
            lambda out: out["streams"][1].update(sample_rate="44.1k"),
            "stream 1 'sample_rate' should be an integer >= 1",
            id="text-sample-rate",
        ),
        pytest.param(
            lambda out: out["streams"][1].pop("channels"),
            "stream 1 has no 'channels'",
            id="no-channels",
        ),
    ],
)
def test_probe_rejects_missing_or_invalid_fields(
    media_file: Path, mutate: Callable[[dict[str, Any]], object], message: str
) -> None:
    output = sample_probe_output()
    mutate(output)

    with pytest.raises(FFmpegOutputError) as excinfo:
        probe(media_file, runner=fake_ffprobe(output))

    assert f"unexpected ffprobe output for {media_file}: " in str(excinfo.value)
    assert message in str(excinfo.value)


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        pytest.param(b"", "not valid JSON", id="empty"),
        pytest.param(b"{\n", "not valid JSON", id="truncated"),
        pytest.param(b"[]", "the output should be a JSON object", id="list"),
        pytest.param(b"null", "the output should be a JSON object", id="null"),
    ],
)
def test_probe_rejects_output_that_is_not_a_json_object(
    media_file: Path, stdout: bytes, message: str
) -> None:
    with pytest.raises(FFmpegOutputError, match=message):
        probe(media_file, runner=RecordingRunner(CommandResult(0, stdout=stdout)))


# --- Real FFmpeg -------------------------------------------------------------------------------


def test_probe_reads_a_generated_video_with_tone(make_test_media: Callable[..., Path]) -> None:
    # Spaces and non-ASCII characters in the directory and file name, which on Windows also
    # means backslashes: the path must reach FFmpeg and ffprobe unchanged.
    media = make_test_media(
        "dir with spaces é/clip 1 ü.mp4",
        duration_s=1.0,
        video_size=(320, 180),
        sample_rate_hz=48_000,
        channels=2,
    )

    info = probe(media)

    assert info.duration_s == pytest.approx(1.0, abs=0.1)
    assert "mp4" in info.format_name.split(",")
    assert info.video_streams == (VideoStream(index=0, codec_name="h264", width=320, height=180),)
    [audio] = info.audio_streams
    assert (audio.codec_name, audio.sample_rate_hz, audio.channels) == ("aac", 48_000, 2)


def test_probe_reads_a_generated_audio_only_tone(make_test_media: Callable[..., Path]) -> None:
    media = make_test_media(
        "tone.wav", duration_s=2.0, video_size=None, sample_rate_hz=16_000, channels=1
    )

    info = probe(media)

    assert info.format_name == "wav"
    assert info.duration_s == pytest.approx(2.0, abs=0.05)
    assert info.video_streams == ()
    assert info.audio_streams == (
        AudioStream(index=0, codec_name="pcm_s16le", sample_rate_hz=16_000, channels=1),
    )


def test_real_ffmpeg_refusing_to_overwrite_raises(make_test_media: Callable[..., Path]) -> None:
    media = make_test_media("tone.wav", video_size=None)
    command = ffmpeg_command("-f", "lavfi", "-i", "sine=duration=0.5", media)  # no -y

    with pytest.raises(FFmpegExitError, match="did not overwrite an existing output file"):
        run_command(command, timeout_s=60)

    assert probe(media).duration_s == pytest.approx(1.0, abs=0.05)  # still the original file


def test_real_ffmpeg_input_title_quoting_the_refusal_is_not_a_refusal(
    make_test_media: Callable[..., Path], tmp_path: Path
) -> None:
    # Regression test: at -loglevel info FFmpeg prints the input's title, and this one made the
    # successful run below raise FFmpegExitError.
    source = make_test_media("tone.wav", video_size=None)
    titled = tmp_path / "titled.wav"
    output = tmp_path / "out.wav"
    title = f"title=File '{output}' already exists. Exiting."
    run_command(ffmpeg_command("-i", source, "-metadata", title, titled), timeout_s=60)

    run_command(ffmpeg_command("-loglevel", "info", "-y", "-i", titled, output), timeout_s=60)

    assert probe(output).duration_s == pytest.approx(1.0, abs=0.05)


@pytest.mark.ffmpeg
@pytest.mark.parametrize("program", [FFMPEG, FFPROBE])
def test_installed_version_is_a_single_word(program: str) -> None:
    version = ffmpeg_version(program)

    assert version
    assert version.split() == [version]


@pytest.mark.ffmpeg
def test_probe_of_a_file_that_is_not_media_raises_exit_error(tmp_path: Path) -> None:
    fake = tmp_path / "not media.mp4"
    fake.write_text("not a video", encoding="utf-8")

    with pytest.raises(FFmpegExitError) as excinfo:
        probe(fake)

    assert excinfo.value.returncode != 0
    assert "Invalid data found when processing input" in excinfo.value.stderr_tail
