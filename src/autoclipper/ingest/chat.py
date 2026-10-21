"""Chat replay ingestion: run TwitchDownloaderCLI, then parse its JSON into pseudonymized messages.

The JSON layout was read from TwitchDownloader's source at release tag ``1.56.5`` (commit
``f8335ca``), which writes chat format version 1.4.0. docs/data/data-sources.md §2 lists every
field this module reads, with links to the source. Other format versions are rejected with
`ChatDownloadError` rather than guessed at.

Privacy (docs/data/data-governance.md §2): the output never contains a commenter's user ID, login
or display name. Each message's author becomes ``user_hash``, the salted HMAC of the commenter's
stable Twitch user ID (``commenter._id``). Message text is redacted in three steps:

1. Gift-sub notices that name another viewer ("gifted a Tier 1 sub to <name>!", "the Gift Sub
   they got from <name>!", "Gift Subs to <name>'s gift!") have that name replaced with ``user``.
2. Every ``@`` followed by one or more characters that are neither whitespace nor ASCII
   punctuation (``_`` excepted) becomes ``@user``. This covers Twitch logins (letters, digits,
   ``_``, any case) and localized display names, wherever the ``@`` appears: inside an e-mail
   address (``name@example.com`` becomes ``name@user.com``) or a URL (``site.tv/@name``) too.
   Over-redacting is preferred to leaking a login typed without a space.
3. Any run of word characters (Unicode letters, digits and ``_``) that equals, ignoring case, the
   login or display name of any commenter in the file becomes ``user``. This removes names typed
   without ``@`` and the author's own name at the start of subscription notices.

Names typed without ``@`` by people who never chat in the VOD, and names glued to other letters
(e.g. CJK text without spaces), can still remain: text stays potentially identifying, and whether
it may be kept at all is OQ-9. Messages live in memory only; nothing here writes them to disk.

Logs and exception messages carry counts, reasons, IDs of videos and version numbers, never chat
content or tool output (which may contain chat content).
"""

import json
import logging
import math
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, TypeGuard

from pydantic import SecretStr

from autoclipper.privacy import pseudonymize

log = logging.getLogger(__name__)

TOOL_NAME: Final = "TwitchDownloaderCLI"
"""Name of the TwitchDownloader command-line binary, looked up on ``PATH``."""

PINNED_TOOL_VERSION: Final = "1.56.5"
"""The TwitchDownloaderCLI release whose source this parser was checked against."""

SUPPORTED_FORMAT_VERSIONS: Final = frozenset({"1.4.0"})
"""Chat JSON ``FileInfo.Version`` values this parser accepts (1.4.0 is what 1.56.5 writes)."""

_SETUP_DOCS: Final = "docs/development/setup.md"
_RELEASES_URL: Final = "https://github.com/lay295/TwitchDownloader/releases"
_SUBSCRIBER_BADGES: Final = frozenset({"subscriber", "founder"})
_MODERATOR_BADGE: Final = "moderator"

# The banner TwitchDownloaderCLI prints first on stdout: "TwitchDownloaderCLI 1.56.5 Copyright ...".
# Release builds strip the "+<git hash>" suffix; debug builds keep it.
_BANNER_VERSION = re.compile(rf"\b{TOOL_NAME} (\d+\.\d+\.\d+)(?:\+[0-9a-f]+)?\b")
_VIDEO_ID = re.compile(r"[0-9]+")
# "@" + characters other than whitespace and ASCII punctuation; "_" (0x5F) stays allowed.
_MENTION = re.compile(r"@[^\s!-/:-@\[-^`{-~]+")
_WORD = re.compile(r"\w+")
# Viewer names in gift-sub notices, as TwitchDownloader's own tests show them
# (TwitchDownloaderCore.Tests/ToolTests/HighlightIconsTests.cs at 1.56.5).
_GIFT_NOTICE_NAME = re.compile(
    r"(?<=\bsub to )\w+(?=!)"
    r"|(?<=\bthey got from )\w+(?=!| to )"
    r"|(?<=\bGift Subs to )\w+(?='s gift!)"
)


class ChatDownloadError(Exception):
    """Downloading or parsing a chat replay failed, or the file is not in the expected format.

    Messages never contain chat content, usernames or the tool's output.
    """


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message, shaped like a row of the proposed ``chat_messages`` table.

    See docs/architecture/data-model.md § ``chat_messages``. That table is still a proposal and a
    transient parsing representation (OQ-9); these records are not meant to be stored as is.

    Attributes:
        video_id: The Twitch video ID the chat belongs to.
        offset_s: Seconds from VOD start (TwitchDownloader's ``content_offset_seconds``).
        user_hash: `autoclipper.privacy.pseudonymize` of the commenter's Twitch user ID.
        text: The message with mentions and known names redacted (see the module docstring).
        emote_ids: Native Twitch emote IDs, one entry per occurrence, in message order.
        n_emotes: ``len(emote_ids)`` plus third-party (BTTV/FFZ/7TV) emotes, which are only
            counted when the file embeds the third-party emote list (`ChatReplay`).
        is_subscriber: True if the badges include ``subscriber`` or ``founder``; None if the
            message has no badge list.
        is_moderator: True if the badges include ``moderator`` (not ``broadcaster``); None if the
            message has no badge list.
        bits: TwitchDownloader's ``bits_spent``, or 0 if the field is absent. TwitchDownloader
            fills it from the first cheer in the message text, so later cheers are not added.
        source: ``"replay"`` for chat from TwitchDownloader; ``"live"`` is reserved for live chat.
    """

    video_id: str
    offset_s: float
    user_hash: str
    text: str
    emote_ids: tuple[str, ...]
    n_emotes: int
    is_subscriber: bool | None
    is_moderator: bool | None
    bits: int
    source: Literal["replay", "live"]


@dataclass(frozen=True, slots=True)
class ChatReplay:
    """The parsed chat of one VOD.

    Attributes:
        video_id: The Twitch video ID.
        messages: The well-formed messages, in file order (TwitchDownloader sorts by offset).
        malformed_reasons: How many comments were skipped, per reason (e.g. ``bad_offset``).
        format_version: The file's ``FileInfo.Version``, e.g. ``"1.4.0"``.
        third_party_emotes_detectable: Whether the file embeds the channel's third-party emote
            list (TwitchDownloaderCLI ``--embed-images``). If False, ``n_emotes`` counts native
            emotes only.
        tool_version: The TwitchDownloaderCLI version that wrote the file, or None if the file was
            parsed without running the tool.
    """

    video_id: str
    messages: tuple[ChatMessage, ...]
    malformed_reasons: Mapping[str, int]
    format_version: str
    third_party_emotes_detectable: bool
    tool_version: str | None = None

    @property
    def n_malformed(self) -> int:
        """Number of comments skipped because they were malformed."""
        return sum(self.malformed_reasons.values())


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Exit code and decoded output of an external command."""

    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]
"""Runs a command given as an argument list. Tests pass a fake that records the commands."""


def run_command(args: Sequence[str]) -> CommandResult:
    """Run ``args`` without a shell and with no stdin, and capture its output.

    Raises:
        OSError: The program could not be started.
    """
    completed = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def find_twitchdownloader(binary: str | Path | None = None) -> Path:
    """Return the path of the TwitchDownloaderCLI executable.

    Args:
        binary: A path to the executable, or a command name to look up on ``PATH``. None looks up
            ``TwitchDownloaderCLI`` on ``PATH``.

    Raises:
        ChatDownloadError: No such executable was found. The message says how to install it.
    """
    found = shutil.which(binary if binary is not None else TOOL_NAME)
    if found is None:
        where = f"at {binary}" if binary is not None else "on PATH"
        raise ChatDownloadError(
            f"{TOOL_NAME} was not found {where}. Download release {PINNED_TOOL_VERSION} from "
            f"{_RELEASES_URL} and put it on PATH, or pass its path (see {_SETUP_DOCS})."
        )
    return Path(found)


def build_chatdownload_command(
    binary: Path, video_id: str, output_path: Path, *, embed_images: bool = False
) -> list[str]:
    """Return the TwitchDownloaderCLI ``chatdownload`` argument list for one VOD's chat as JSON.

    ``--collision Overwrite`` keeps the tool from prompting when ``output_path`` exists (stdin is
    closed). ``--embed-images`` also embeds the BTTV/FFZ/7TV emote lists, which makes third-party
    emotes countable, at the cost of downloading every emote image into the file.

    Raises:
        ValueError: ``video_id`` is not all digits, or ``output_path`` does not end in ``.json``
            (the tool picks its output format from the extension).
    """
    _check_video_id(video_id)
    _check_output_path(output_path)
    command = [str(binary), "chatdownload", "--id", video_id, "--output", str(output_path)]
    command += ["--collision", "Overwrite"]
    if embed_images:
        command.append("--embed-images")
    return command


def download_chat(
    video_id: str,
    raw_path: Path,
    *,
    salt: SecretStr,
    binary: str | Path | None = None,
    runner: CommandRunner = run_command,
    embed_images: bool = False,
    keep_raw: bool = False,
) -> ChatReplay:
    """Download a VOD's chat replay with TwitchDownloaderCLI and parse it.

    The raw file holds usernames. It is deleted after a successful parse unless ``keep_raw`` is
    set (docs/data/data-governance.md §4), and kept if parsing fails so it can be inspected.

    Args:
        video_id: The Twitch video ID (digits only).
        raw_path: Where the tool writes the raw JSON, e.g. ``<data_dir>/raw/chat/<video_id>.json``.
            Its directory is created if needed; an existing file is overwritten.
        salt: The HMAC key for ``user_hash`` (``get_settings().require_chat_hash_salt()``).
        binary: The TwitchDownloaderCLI executable; see `find_twitchdownloader`.
        runner: Runs the command; the default starts a subprocess.
        embed_images: Pass ``--embed-images`` so third-party emotes can be counted.
        keep_raw: Keep the raw file after a successful parse.

    Returns:
        The parsed chat, with ``tool_version`` read from the tool's startup banner.

    Raises:
        ValueError: ``video_id``, ``raw_path`` or ``salt`` is invalid. Nothing is run.
        ChatDownloadError: The tool is missing, fails, does not report its version or writes no
            file, or the file is not a chat replay this parser supports.
    """
    _check_video_id(video_id)
    _check_output_path(raw_path)
    _check_salt(salt)
    command = build_chatdownload_command(
        find_twitchdownloader(binary), video_id, raw_path, embed_images=embed_images
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading chat replay video_id=%s", video_id)
    try:
        result = runner(command)
    except OSError as exc:
        raise ChatDownloadError(f"could not start {command[0]}: {exc.strerror}") from None
    if result.returncode != 0:
        raise ChatDownloadError(
            f"{TOOL_NAME} chatdownload failed for video_id={video_id} with exit code "
            f"{result.returncode}. Its output is not shown because it can contain chat content; "
            f"run the command by hand to see it."
        )
    match = _BANNER_VERSION.search(result.stdout)
    if match is None:
        raise ChatDownloadError(
            f"{TOOL_NAME} did not print its version banner ({TOOL_NAME} <version>) on stdout, "
            f"so the download can't be traced to a tool version. Raw file, if any: {raw_path}"
        )
    tool_version = match.group(1)
    if tool_version != PINNED_TOOL_VERSION:
        log.warning(
            "untested tool version tool=%s tool_version=%s pinned_version=%s",
            TOOL_NAME,
            tool_version,
            PINNED_TOOL_VERSION,
        )
    if not raw_path.is_file():
        raise ChatDownloadError(f"{TOOL_NAME} exited successfully but wrote no file at {raw_path}")
    try:
        replay = parse_chat_json(raw_path.read_bytes(), video_id=video_id, salt=salt)
    except ChatDownloadError as exc:
        exc.add_note(
            f"The raw file was kept at {raw_path}; it holds usernames, delete it after use."
        )
        raise
    if not keep_raw:
        raw_path.unlink()
    log.info(
        "downloaded chat replay video_id=%s tool_version=%s raw_kept=%s",
        video_id,
        tool_version,
        keep_raw,
    )
    return replace(replay, tool_version=tool_version)


def parse_chat_json(data: bytes, *, video_id: str, salt: SecretStr) -> ChatReplay:
    """Parse a TwitchDownloader chat JSON file into pseudonymized `ChatMessage` records.

    Comments that don't have the expected shape are skipped, counted per reason and logged by
    count only. A file that isn't a supported chat replay of ``video_id`` raises instead.

    Args:
        data: The file's bytes (UTF-8 JSON, as TwitchDownloaderCLI writes it).
        video_id: The Twitch video ID the file must belong to.
        salt: The HMAC key for ``user_hash``.

    Raises:
        ValueError: ``video_id`` is not all digits, or ``salt`` is blank.
        ChatDownloadError: The data is not JSON, has an unsupported or missing
            ``FileInfo.Version``, belongs to another video, or lacks the ``comments`` list.
    """
    _check_video_id(video_id)
    _check_salt(salt)
    root = _load_json(data)
    format_version = _format_version(root)
    video = root.get("video")
    if not isinstance(video, dict) or video.get("id") != video_id:
        raise ChatDownloadError(f"the chat file is not the chat of video_id={video_id}")
    comments = root.get("comments")
    if not isinstance(comments, list):
        raise ChatDownloadError("the chat file has no comments list")
    third_party = _third_party_emote_names(root.get("embeddedData"))

    redact = _Redactor(_commenter_names(comments))
    hashes: dict[str, str] = {}
    messages: list[ChatMessage] = []
    malformed: Counter[str] = Counter()
    for comment in comments:
        try:
            messages.append(
                _parse_comment(comment, video_id, salt, redact, third_party or frozenset(), hashes)
            )
        except _MalformedCommentError as exc:
            malformed[exc.reason] += 1

    if malformed:
        log.warning(
            "skipped malformed chat messages video_id=%s n=%d reasons=%s",
            video_id,
            malformed.total(),
            ",".join(f"{reason}:{n}" for reason, n in sorted(malformed.items())),
        )
    log.info(
        "parsed chat replay video_id=%s n_messages=%d n_malformed=%d format_version=%s "
        "third_party_emotes=%s",
        video_id,
        len(messages),
        malformed.total(),
        format_version,
        third_party is not None,
    )
    return ChatReplay(
        video_id=video_id,
        messages=tuple(messages),
        malformed_reasons=MappingProxyType(dict(sorted(malformed.items()))),
        format_version=format_version,
        third_party_emotes_detectable=third_party is not None,
    )


def redact_mentions(text: str) -> str:
    """Replace every ``@`` mention in ``text`` with ``@user`` (rule 2 in the module docstring)."""
    return _MENTION.sub("@user", text)


class _MalformedCommentError(Exception):
    """A comment is skipped. ``reason`` is a fixed label; the comment's content is never kept."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Redactor:
    """Applies the three text-redaction rules from the module docstring."""

    def __init__(self, known_names: Collection[str]) -> None:
        self._known_names = frozenset(known_names)

    def __call__(self, text: str) -> str:
        text = _GIFT_NOTICE_NAME.sub("user", text)
        text = redact_mentions(text)
        return _WORD.sub(self._replace_known_name, text)

    def _replace_known_name(self, match: re.Match[str]) -> str:
        word = match.group()
        return "user" if word.casefold() in self._known_names else word


def _check_video_id(video_id: str) -> None:
    # The value is not echoed: a wrong argument could be anything, including a username.
    if not _VIDEO_ID.fullmatch(video_id):
        raise ValueError("video_id must be a Twitch video ID made of digits only")


def _check_output_path(output_path: Path) -> None:
    if output_path.suffix.lower() != ".json":
        raise ValueError("output_path must end in .json; the tool picks the format by extension")


def _check_salt(salt: SecretStr) -> None:
    # Checked up front so a bad salt fails before a long download, not on the first message.
    if not salt.get_secret_value().strip():
        raise ValueError("the salt is empty or blank; set AUTOCLIPPER_CHAT_HASH_SALT")


def _load_json(data: bytes) -> dict[str, object]:
    # Errors are raised `from None`: decode and JSON errors keep the whole input on the exception.
    if data[:2] == b"\x1f\x8b":
        raise ChatDownloadError("the chat file is gzip-compressed; download it without compression")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ChatDownloadError("the chat file is not UTF-8 text") from None
    try:
        root = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChatDownloadError(
            f"the chat file is not valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from None
    if not isinstance(root, dict):
        raise ChatDownloadError("the chat file is not a JSON object")
    return root


def _format_version(root: dict[str, object]) -> str:
    file_info = root.get("FileInfo")
    version = file_info.get("Version") if isinstance(file_info, dict) else None
    parts = (
        [version.get(key) for key in ("Major", "Minor", "Patch")]
        if isinstance(version, dict)
        else []
    )
    if len(parts) != 3 or not all(_is_count(part) for part in parts):
        raise ChatDownloadError(
            "the file has no valid FileInfo.Version, so it is not a TwitchDownloader chat replay"
        )
    format_version = ".".join(str(part) for part in parts)
    if format_version not in SUPPORTED_FORMAT_VERSIONS:
        raise ChatDownloadError(
            f"unsupported chat file format version {format_version}; this parser supports "
            f"{', '.join(sorted(SUPPORTED_FORMAT_VERSIONS))} (TwitchDownloaderCLI "
            f"{PINNED_TOOL_VERSION})"
        )
    return format_version


def _third_party_emote_names(embedded: object) -> frozenset[str] | None:
    """Names of the embedded BTTV/FFZ/7TV emotes, or None if the file embeds none."""
    if embedded is None:
        return None
    entries = embedded.get("thirdParty") if isinstance(embedded, dict) else None
    if not isinstance(entries, list):
        raise ChatDownloadError("the chat file's embeddedData has no thirdParty list")
    names = [entry.get("name") if isinstance(entry, dict) else None for entry in entries]
    if not all(isinstance(name, str) and name for name in names):
        raise ChatDownloadError("the chat file's embeddedData.thirdParty has an emote without name")
    return frozenset(name for name in names if isinstance(name, str))


def _commenter_names(comments: list[object]) -> set[str]:
    """Case-folded logins and display names of every commenter, malformed comments included."""
    names: set[str] = set()
    for comment in comments:
        commenter = comment.get("commenter") if isinstance(comment, dict) else None
        if isinstance(commenter, dict):
            for key in ("name", "display_name"):
                value = commenter.get(key)
                if isinstance(value, str) and value.strip():
                    names.add(value.strip().casefold())
    return names


def _parse_comment(
    comment: object,
    video_id: str,
    salt: SecretStr,
    redact: _Redactor,
    third_party: frozenset[str],
    hashes: dict[str, str],
) -> ChatMessage:
    if not isinstance(comment, dict):
        raise _MalformedCommentError("not_an_object")
    if comment.get("content_id") != video_id:
        raise _MalformedCommentError("wrong_video")
    offset = comment.get("content_offset_seconds")
    if not isinstance(offset, int | float) or isinstance(offset, bool):
        raise _MalformedCommentError("bad_offset")
    try:
        offset_s = float(offset)
    except OverflowError:  # an integer too large for a float
        raise _MalformedCommentError("bad_offset") from None
    if not math.isfinite(offset_s) or offset_s < 0:
        raise _MalformedCommentError("bad_offset")
    commenter = comment.get("commenter")
    user_id = commenter.get("_id") if isinstance(commenter, dict) else None
    if not isinstance(user_id, str) or not user_id.strip():
        raise _MalformedCommentError("bad_commenter")
    message = comment.get("message")
    body = message.get("body") if isinstance(message, dict) else None
    if not isinstance(message, dict) or not isinstance(body, str):
        raise _MalformedCommentError("bad_message")
    bits = message.get("bits_spent", 0)
    if not _is_count(bits):
        raise _MalformedCommentError("bad_bits")
    emote_ids, n_third_party = _emotes(message.get("fragments"), third_party)
    is_subscriber, is_moderator = _badge_flags(message.get("user_badges"))

    if user_id not in hashes:
        hashes[user_id] = pseudonymize(user_id, salt)
    return ChatMessage(
        video_id=video_id,
        offset_s=offset_s,
        user_hash=hashes[user_id],
        text=redact(body),
        emote_ids=emote_ids,
        n_emotes=len(emote_ids) + n_third_party,
        is_subscriber=is_subscriber,
        is_moderator=is_moderator,
        bits=bits,
        source="replay",
    )


def _emotes(fragments: object, third_party: frozenset[str]) -> tuple[tuple[str, ...], int]:
    """Native emote IDs, and the number of whitespace-separated third-party emote names."""
    if not isinstance(fragments, list):
        raise _MalformedCommentError("bad_fragments")
    emote_ids: list[str] = []
    n_third_party = 0
    for fragment in fragments:
        if not isinstance(fragment, dict):
            raise _MalformedCommentError("bad_fragments")
        text, emoticon = fragment.get("text"), fragment.get("emoticon")
        if not isinstance(text, str):
            raise _MalformedCommentError("bad_fragments")
        if emoticon is None:
            n_third_party += sum(token in third_party for token in text.split())
            continue
        emote_id = emoticon.get("emoticon_id") if isinstance(emoticon, dict) else None
        if not isinstance(emote_id, str) or not emote_id:
            raise _MalformedCommentError("bad_fragments")
        emote_ids.append(emote_id)
    return tuple(emote_ids), n_third_party


def _badge_flags(badges: object) -> tuple[bool | None, bool | None]:
    """``(is_subscriber, is_moderator)`` from the badge list; unknown without one."""
    if badges is None:
        return None, None
    if not isinstance(badges, list):
        raise _MalformedCommentError("bad_badges")
    badge_ids = [badge.get("_id") if isinstance(badge, dict) else None for badge in badges]
    if not all(isinstance(badge_id, str) for badge_id in badge_ids):
        raise _MalformedCommentError("bad_badges")
    return bool(_SUBSCRIBER_BADGES.intersection(badge_ids)), _MODERATOR_BADGE in badge_ids


def _is_count(value: object) -> TypeGuard[int]:
    """True for a non-negative JSON integer (``bool`` is excluded: it subclasses ``int``)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
