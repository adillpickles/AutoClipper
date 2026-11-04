"""The base class of the errors the export modules raise."""


class ExportError(Exception):
    """An export can't be made from the given request.

    Each export module defines its specific errors as subclasses. FFmpeg failures are not export
    errors: they raise `autoclipper.ffmpeg.FFmpegError`, so a caller can tell a bad request from a
    failing external tool.
    """
