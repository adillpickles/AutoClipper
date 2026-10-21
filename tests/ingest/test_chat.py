"""Tests for autoclipper.ingest.chat: TwitchDownloaderCLI wrapper and chat replay parser."""

import copy
import gzip
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from autoclipper.ingest.chat import (
    PINNED_TOOL_VERSION,
    ChatDownloadError,
    ChatMessage,
    ChatReplay,
    CommandResult,
    build_chatdownload_command,
    download_chat,
    find_twitchdownloader,
    parse_chat_json,
    redact_mentions,
    run_command,
)
from autoclipper.privacy import pseudonymize

FIXTURES = Path(__file__).parent / "fixtures"
VIDEO_ID = "9000000100"
SALT = SecretStr("fake-salt-5b1e")
BANNER = f"TwitchDownloaderCLI {PINNED_TOOL_VERSION} Copyright (c) lay295 and contributors\n"

# Every login, display name and user ID in the fixtures, including ones that only appear inside
# message text (user_005) or in malformed comments (user_007, user_008, user_009).
RAW_IDENTIFIERS = (
    *(f"user_00{n}" for n in range(1, 10)),
    "テスト六",
    *(f"90000000{n:02d}" for n in range(1, 9)),
)


# Any: parsed JSON test data, indexed freely by the tests that mutate it.
def _fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "chat_replay.json").read_text(encoding="utf-8"))


def _dump(root: object) -> bytes:
    return json.dumps(root, ensure_ascii=False).encode("utf-8")


def _parse(root: object) -> ChatReplay:
    return parse_chat_json(_dump(root), video_id=VIDEO_ID, salt=SALT)


def _hash(user_id: str) -> str:
    return pseudonymize(user_id, SALT)


def _assert_no_raw_identifiers(text: str) -> None:
    leaked = [raw for raw in RAW_IDENTIFIERS if raw.casefold() in text.casefold()]
    assert not leaked


def _assert_clean_error(exc: BaseException) -> None:
    # The message, notes and any chained exception must not carry chat content.
    _assert_no_raw_identifiers(" ".join([str(exc), repr(exc), *getattr(exc, "__notes__", [])]))
    assert exc.__cause__ is None
    assert exc.__context__ is None or exc.__suppress_context__


# --- Parsing -----------------------------------------------------------------------------------


def test_parses_well_formed_comments_into_chat_messages_rows() -> None:
    replay = _parse(_fixture())

    def row(offset_s: float, user_id: str, text: str, **fields: object) -> ChatMessage:
        base = ChatMessage(
            video_id=VIDEO_ID,
            offset_s=offset_s,
            user_hash=_hash(user_id),
            text=text,
            emote_ids=(),
            n_emotes=0,
            is_subscriber=False,
            is_moderator=False,
            bits=0,
            source="replay",
        )
        return replace(base, **fields)

    assert replay.messages == (
        row(
            1.5, "9000000001", "hello chat Kappa", emote_ids=("25",), n_emotes=1, is_subscriber=True
        ),
        row(3.25, "9000000002", "@user welcome! @user, gg", is_moderator=True),
        row(5.0, "9000000003", "Cheer100 nice play OMEGALUL", is_subscriber=True, bits=100),
        row(
            7.0,
            "9000000001",
            "user subscribed at Tier 1. They've subscribed for 3 months! hype",
            is_subscriber=True,
        ),
        row(8.5, "9000000004", "user gifted a Tier 1 sub to user! "),
        row(
            10.0,
            "9000000002",
            "user is right, mail someone@user.com or see example.tv/@user",
            is_moderator=True,
        ),
        row(
            12.0,
            "9000000006",
            "LUL LUL pepeLaugh",
            emote_ids=("425618", "425618"),
            n_emotes=2,
            is_subscriber=None,
            is_moderator=None,
        ),
        row(13.0, "9000000001", "@user こんにちは user", is_subscriber=True),
    )
    assert replay.video_id == VIDEO_ID
    assert replay.format_version == "1.4.0"
    assert replay.tool_version is None
    assert not replay.third_party_emotes_detectable


def test_offsets_are_floats() -> None:
    assert all(type(message.offset_s) is float for message in _parse(_fixture()).messages)


def test_same_commenter_gets_the_same_hash_and_others_differ() -> None:
    hashes = [message.user_hash for message in _parse(_fixture()).messages]

    assert hashes[0] == hashes[3] == hashes[7]
    assert len(set(hashes)) == 5


def test_a_different_salt_gives_different_hashes() -> None:
    other = parse_chat_json(_dump(_fixture()), video_id=VIDEO_ID, salt=SecretStr("other-salt"))

    assert {m.user_hash for m in other.messages}.isdisjoint(
        {m.user_hash for m in _parse(_fixture()).messages}
    )


def test_counts_malformed_comments_per_reason() -> None:
    replay = _parse(_fixture())

    assert dict(replay.malformed_reasons) == {
        "bad_badges": 1,
        "bad_bits": 1,
        "bad_commenter": 1,
        "bad_fragments": 1,
        "bad_message": 1,
        "bad_offset": 1,
        "not_an_object": 1,
        "wrong_video": 1,
    }
    assert replay.n_malformed == 8


@pytest.mark.parametrize("offset", [-1.0, None, True, "1.5", 10**400])
def test_rejects_invalid_offsets(offset: object) -> None:
    root = _fixture()
    root["comments"] = [copy.deepcopy(root["comments"][0])]
    root["comments"][0]["content_offset_seconds"] = offset

    assert dict(_parse(root).malformed_reasons) == {"bad_offset": 1}


def test_missing_bits_field_means_zero_bits() -> None:
    root = _fixture()
    del root["comments"][2]["message"]["bits_spent"]

    assert _parse(root).messages[2].bits == 0


def test_logs_malformed_counts_by_reason(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="autoclipper"):
        _parse(_fixture())

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        f"skipped malformed chat messages video_id={VIDEO_ID} n=8 reasons=bad_badges:1,"
        "bad_bits:1,bad_commenter:1,bad_fragments:1,bad_message:1,bad_offset:1,"
        "not_an_object:1,wrong_video:1"
    ]


def test_raw_identities_never_reach_output_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        replay = _parse(_fixture())

    _assert_no_raw_identifiers(repr(replay))
    _assert_no_raw_identifiers(caplog.text)


def test_counts_third_party_emotes_when_the_file_embeds_them() -> None:
    root = _fixture()
    emote = {"id": "fake-emote", "imageScale": 2, "data": "", "width": 28, "height": 28}
    root["embeddedData"] = {
        "thirdParty": [emote | {"name": "OMEGALUL"}, emote | {"name": "pepeLaugh"}],
        "firstParty": [],
        "twitchBadges": [],
        "twitchBits": [],
    }

    replay = _parse(root)

    assert replay.third_party_emotes_detectable
    assert [m.n_emotes for m in replay.messages] == [1, 0, 1, 0, 0, 0, 3, 0]
    assert replay.messages[6].emote_ids == ("425618", "425618")


# --- Redaction ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@user_001 hi", "@user hi"),
        ("hi @User_001!", "hi @user!"),
        ("@USER_001's turn", "@user's turn"),
        ("(@user_001) and [@user_002]", "(@user) and [@user]"),
        ("@user_001,@user_002.", "@user,@user."),
        ("@user_001: gg", "@user: gg"),
        ("@user-001", "@user-001"),  # logins have no "-", so the mention ends there
        ("@テスト六さん hi", "@user hi"),
        ("@@user_001", "@@user"),
        ("someone@example.com", "someone@user.com"),
        ("https://example.tv/@user_001/clip", "https://example.tv/@user/clip"),
        ("@ alone and trailing @", "@ alone and trailing @"),
        ("no mention", "no mention"),
    ],
)
def test_redact_mentions(text: str, expected: str) -> None:
    assert redact_mentions(text) == expected


def test_known_names_are_redacted_as_whole_words_only() -> None:
    root = _fixture()
    root["comments"][0]["message"]["body"] = "USER_002 and user_0022 and xuser_002 and User_002!"

    assert _parse(root).messages[0].text == "user and user_0022 and xuser_002 and user!"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("x gifted a Tier 1 sub to viewer_042! ", "x gifted a Tier 1 sub to user! "),
        ("the Gift Sub they got from viewer_042!", "the Gift Sub they got from user!"),
        (
            "Gift they got from viewer_042 to the community!",
            "Gift they got from user to the community!",
        ),
        ("We added 1 Gift Subs to viewer_042's gift! ", "We added 1 Gift Subs to user's gift! "),
        ("they got from an anonymous user!", "they got from an anonymous user!"),
    ],
)
def test_redacts_viewer_names_in_gift_notices(body: str, expected: str) -> None:
    root = _fixture()
    root["comments"][0]["message"]["body"] = body

    assert _parse(root).messages[0].text == expected


# --- File format -------------------------------------------------------------------------------


def _set_version(root: dict[str, Any], version: object) -> None:
    root["FileInfo"]["Version"] = version


FORMAT_ERRORS: dict[str, Callable[[dict[str, Any]], object]] = {
    "older format": lambda root: _set_version(root, {"Major": 1, "Minor": 3, "Patch": 2}),
    "newer format": lambda root: _set_version(root, {"Major": 2, "Minor": 0, "Patch": 0}),
    "text version": lambda root: _set_version(root, {"Major": "1", "Minor": 4, "Patch": 0}),
    "no FileInfo": lambda root: root.pop("FileInfo"),
    "other video": lambda root: root["video"].update(id="9000000999"),
    "no video": lambda root: root.pop("video"),
    "comments not a list": lambda root: root.update(comments={"user_001": "hi"}),
    "bad embeddedData": lambda root: root.update(embeddedData={"thirdParty": [{"id": "1"}]}),
}


@pytest.mark.parametrize("mutate", FORMAT_ERRORS.values(), ids=FORMAT_ERRORS.keys())
def test_unexpected_file_format_raises(mutate: Callable[[dict[str, Any]], object]) -> None:
    root = _fixture()
    mutate(root)

    with pytest.raises(ChatDownloadError) as excinfo:
        _parse(root)

    _assert_clean_error(excinfo.value)


@pytest.mark.parametrize(
    "data",
    [
        (FIXTURES / "chat_wrong_format.json").read_bytes(),
        gzip.compress(_dump(_fixture())),
        b"user_001: hello, not json",
        b'{"FileInfo": {"Version": user_001}}',
        "user_001 テスト六".encode("utf-16"),
        b'["user_001"]',
    ],
    ids=["wrong format file", "gzip", "plain text", "broken json", "utf-16", "json array"],
)
def test_data_that_is_not_a_chat_replay_raises(data: bytes) -> None:
    with pytest.raises(ChatDownloadError) as excinfo:
        parse_chat_json(data, video_id=VIDEO_ID, salt=SALT)

    _assert_clean_error(excinfo.value)


def test_accepts_a_utf8_byte_order_mark() -> None:
    data = b"\xef\xbb\xbf" + _dump(_fixture())

    assert len(parse_chat_json(data, video_id=VIDEO_ID, salt=SALT).messages) == 8


FULL_WIDTH_123 = "".join(chr(0xFF11 + i) for i in range(3))  # "123" in full-width digits


@pytest.mark.parametrize("video_id", ["", "abc", "user_001", "-o", "123 ", FULL_WIDTH_123])
def test_rejects_invalid_video_ids_without_echoing_them(video_id: str) -> None:
    with pytest.raises(ValueError, match="digits only") as excinfo:
        parse_chat_json(_dump(_fixture()), video_id=video_id, salt=SALT)

    assert "user_001" not in str(excinfo.value)


@pytest.mark.parametrize("salt", ["", "   "])
def test_rejects_a_blank_salt(salt: str) -> None:
    with pytest.raises(ValueError, match="AUTOCLIPPER_CHAT_HASH_SALT"):
        parse_chat_json(_dump(_fixture()), video_id=VIDEO_ID, salt=SecretStr(salt))


# --- TwitchDownloaderCLI wrapper ---------------------------------------------------------------


class FakeRunner:
    """Records commands and plays TwitchDownloaderCLI: writes ``payload`` to the --output path."""

    def __init__(
        self, *, payload: bytes | None, returncode: int = 0, stdout: str = BANNER, stderr: str = ""
    ) -> None:
        self.payload = payload
        self.result = CommandResult(returncode, stdout, stderr)
        self.commands: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> CommandResult:
        self.commands.append(list(args))
        if self.payload is not None:
            Path(args[args.index("--output") + 1]).write_bytes(self.payload)
        return self.result


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    name = "TwitchDownloaderCLI.exe" if sys.platform == "win32" else "TwitchDownloaderCLI"
    binary = tmp_path / "bin" / name
    binary.parent.mkdir()
    binary.write_bytes(b"")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def raw_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "raw" / "chat" / f"{VIDEO_ID}.json"


def test_builds_the_chatdownload_command(fake_binary: Path, raw_path: Path) -> None:
    command = build_chatdownload_command(fake_binary, VIDEO_ID, raw_path)

    assert command == [
        str(fake_binary),
        "chatdownload",
        "--id",
        VIDEO_ID,
        "--output",
        str(raw_path),
        "--collision",
        "Overwrite",
    ]


def test_embed_images_adds_the_flag(fake_binary: Path, raw_path: Path) -> None:
    command = build_chatdownload_command(fake_binary, VIDEO_ID, raw_path, embed_images=True)

    assert command[-1] == "--embed-images"


def test_output_must_be_a_json_file(fake_binary: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.json"):
        build_chatdownload_command(fake_binary, VIDEO_ID, tmp_path / "chat.txt")


def test_download_runs_the_tool_parses_and_deletes_the_raw_file(
    fake_binary: Path, raw_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeRunner(payload=_dump(_fixture()), stderr="user_001 User_002 テスト六\n")

    with caplog.at_level(logging.DEBUG):
        replay = download_chat(VIDEO_ID, raw_path, salt=SALT, binary=fake_binary, runner=runner)

    assert runner.commands == [build_chatdownload_command(fake_binary, VIDEO_ID, raw_path)]
    assert replay.tool_version == PINNED_TOOL_VERSION
    assert len(replay.messages) == 8
    assert replay.n_malformed == 8
    assert not raw_path.exists()
    _assert_no_raw_identifiers(repr(replay))
    _assert_no_raw_identifiers(caplog.text)


def test_download_keeps_the_raw_file_on_request(fake_binary: Path, raw_path: Path) -> None:
    runner = FakeRunner(payload=_dump(_fixture()))

    download_chat(VIDEO_ID, raw_path, salt=SALT, binary=fake_binary, runner=runner, keep_raw=True)

    assert raw_path.is_file()


def test_download_records_and_warns_about_an_untested_tool_version(
    fake_binary: Path, raw_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Debug builds keep the "+<git hash>" suffix in the banner.
    runner = FakeRunner(payload=_dump(_fixture()), stdout="TwitchDownloaderCLI 1.57.0+abc123 (c)\n")

    replay = download_chat(VIDEO_ID, raw_path, salt=SALT, binary=fake_binary, runner=runner)

    assert replay.tool_version == "1.57.0"
    assert "tool_version=1.57.0 pinned_version=1.56.5" in caplog.text


@pytest.mark.parametrize(
    ("runner", "match"),
    [
        (
            FakeRunner(payload=None, returncode=1, stdout=BANNER + "user_001\n", stderr="user_001"),
            "exit code 1",
        ),
        (FakeRunner(payload=b"{}", stdout="user_001 progress 100%\n"), "version banner"),
        (FakeRunner(payload=None), "wrote no file"),
    ],
    ids=["tool fails", "no version banner", "no output file"],
)
def test_download_failures_raise_without_tool_output(
    fake_binary: Path, raw_path: Path, runner: FakeRunner, match: str
) -> None:
    with pytest.raises(ChatDownloadError, match=match) as excinfo:
        download_chat(VIDEO_ID, raw_path, salt=SALT, binary=fake_binary, runner=runner)

    _assert_clean_error(excinfo.value)


def test_download_keeps_the_raw_file_when_parsing_fails(fake_binary: Path, raw_path: Path) -> None:
    runner = FakeRunner(payload=(FIXTURES / "chat_wrong_format.json").read_bytes())

    with pytest.raises(ChatDownloadError) as excinfo:
        download_chat(VIDEO_ID, raw_path, salt=SALT, binary=fake_binary, runner=runner)

    assert raw_path.is_file()
    assert str(raw_path) in excinfo.value.__notes__[0]
    _assert_clean_error(excinfo.value)


def test_download_reports_a_tool_that_cannot_start(fake_binary: Path, raw_path: Path) -> None:
    def runner(args: Sequence[str]) -> CommandResult:
        raise PermissionError(13, "Permission denied")

    with pytest.raises(ChatDownloadError, match="could not start"):
        download_chat(VIDEO_ID, raw_path, salt=SALT, binary=fake_binary, runner=runner)


@pytest.mark.parametrize(
    ("video_id", "salt", "raw_name", "match"),
    [
        ("abc", SALT, "chat.json", "digits only"),
        (VIDEO_ID, SecretStr(" "), "chat.json", "AUTOCLIPPER_CHAT_HASH_SALT"),
        (VIDEO_ID, SALT, "chat.txt", r"\.json"),
    ],
)
def test_download_checks_its_arguments_before_anything_else(
    tmp_path: Path, video_id: str, salt: SecretStr, raw_name: str, match: str
) -> None:
    # The binary doesn't exist either: argument errors are reported first.
    runner = FakeRunner(payload=_dump(_fixture()))
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match=match):
        download_chat(video_id, tmp_path / raw_name, salt=salt, binary=missing, runner=runner)

    assert runner.commands == []


def test_missing_tool_explains_how_to_install_it(tmp_path: Path, raw_path: Path) -> None:
    runner = FakeRunner(payload=_dump(_fixture()))

    with pytest.raises(ChatDownloadError) as excinfo:
        download_chat(VIDEO_ID, raw_path, salt=SALT, binary=tmp_path / "missing", runner=runner)

    message = str(excinfo.value)
    assert "docs/development/setup.md" in message
    assert "https://github.com/lay295/TwitchDownloader/releases" in message
    assert PINNED_TOOL_VERSION in message
    assert runner.commands == []


def test_finds_the_tool_on_path(fake_binary: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(fake_binary.parent))

    assert find_twitchdownloader() == fake_binary


def test_missing_tool_on_path_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(ChatDownloadError, match="not found on PATH"):
        find_twitchdownloader()


def test_run_command_captures_exit_code_and_output() -> None:
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"

    result = run_command([sys.executable, "-c", script])

    assert result == CommandResult(3, "out\n", "err\n")
