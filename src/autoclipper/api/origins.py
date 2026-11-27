"""Reject state-changing browser requests from origins outside the CORS allowlist.

CORS stops other sites from reading responses, but a simple cross-origin request still reaches
the server. Routes that change local state (tool downloads, settings) check ``Origin`` too.
Requests without an ``Origin`` header (the CLI, curl, tests) are allowed.
"""

from collections.abc import Sequence
from typing import Final

from fastapi import Request, Response

from autoclipper.schemas import ApiError, ErrorResponse

_JSON_TYPE: Final = "application/json; charset=utf-8"


class OriginCheck:
    """An allowlist of browser origins; empty allows every origin."""

    def __init__(self, allowed_origins: Sequence[str]) -> None:
        self._allowed = frozenset(_normalize(o) for o in allowed_origins if o)

    def allows(self, request: Request) -> bool:
        """Whether `request` has no ``Origin`` header or an allowed one."""
        origin = request.headers.get("origin")
        return origin is None or not self._allowed or _normalize(origin) in self._allowed


def _normalize(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def origin_forbidden(message: str) -> Response:
    """The ``403 origin_not_allowed`` envelope."""
    body = ErrorResponse(error=ApiError(code="origin_not_allowed", message=message))
    return Response(
        content=body.model_dump_json(),
        status_code=403,
        media_type=_JSON_TYPE,
        headers={"Cache-Control": "no-store"},
    )
