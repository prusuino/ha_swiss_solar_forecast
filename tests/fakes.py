"""An in-memory stand-in for the HTTP session, shared by the tests.

It serves files from byte strings the way the real host does: HEAD with a
Content-Length, GET whole or as a byte range. Nothing leaves the machine.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeResponse:
    """What `session.get()` hands out, usable as an async context manager."""

    def __init__(
        self, status: int = 200, body: bytes = b"", headers: dict[str, str] | None = None, payload: Any = None
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.payload = payload

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self.body

    async def json(self, content_type: str | None = None) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _Raises:
    """A request that fails before an answer arrives."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aenter__(self) -> FakeResponse:
        raise self.error

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeSession:
    """Answers GET and HEAD from memory.

    `routes` maps a URL to bytes (a file), a ready `FakeResponse`, an
    exception to raise, or a callable `(method, url, headers)` returning
    one of these. `fallback(url)` is asked for URLs that are not listed;
    what is still unknown answers 404. Every request is kept in `calls`.
    """

    def __init__(
        self,
        routes: dict[str, Any] | None = None,
        *,
        fallback: Callable[[str], Any] | None = None,
        head_length: bool = True,
        honour_range: bool = True,
    ) -> None:
        self.routes = dict(routes or {})
        self.fallback = fallback
        self.head_length = head_length
        self.honour_range = honour_range
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def urls(self, method: str | None = None) -> list[str]:
        return [url for verb, url, _headers in self.calls if method is None or verb == method]

    def _answer(self, method: str, url: str, headers: dict[str, str] | None) -> Any:
        headers = dict(headers or {})
        self.calls.append((method, url, headers))
        route = self.routes.get(url)
        if route is None and self.fallback is not None:
            route = self.fallback(url)
        if callable(route):
            route = route(method, url, headers)
        if route is None:
            return FakeResponse(404)
        if isinstance(route, Exception):
            return _Raises(route)
        if isinstance(route, FakeResponse):
            return route
        return self._file(method, route, headers)

    def _file(self, method: str, body: bytes, headers: dict[str, str]) -> FakeResponse:
        total = len(body)
        if method == "HEAD":
            return FakeResponse(200, headers={"Content-Length": str(total)} if self.head_length else {})
        wanted = headers.get("Range")
        if wanted and self.honour_range:
            first, last = (int(part) for part in wanted.removeprefix("bytes=").split("-"))
            last = min(last, total - 1)
            return FakeResponse(206, body[first : last + 1], {"Content-Range": f"bytes {first}-{last}/{total}"})
        return FakeResponse(200, body, {"Content-Length": str(total)})

    def get(self, url: str, *, headers: dict[str, str] | None = None, **_kwargs: Any) -> Any:
        return self._answer("GET", url, headers)

    def head(self, url: str, *, headers: dict[str, str] | None = None, **_kwargs: Any) -> Any:
        return self._answer("HEAD", url, headers)
