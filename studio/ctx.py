"""Which user the current code is running for.

Web requests get it from the session cookie (set by the middleware in server.py). Background threads have no
request, so they set it themselves from the job, recording or post they are working on. Every module that
touches user data asks here, which is what keeps one user's creators, clips and accounts away from
another's."""
from __future__ import annotations

import collections
import contextlib
import contextvars
from collections.abc import Callable, Iterator
from typing import Any

_uid: contextvars.ContextVar[str | None] = contextvars.ContextVar("peakcut_uid", default=None)


class NoUser(RuntimeError):
    pass


def uid() -> str:
    u = _uid.get()
    if u is None:
        raise NoUser("No signed-in user in this context")
    return u


def uid_or_none() -> str | None:
    return _uid.get()


def set_uid(u: str | None) -> None:
    """For a thread that works for one user for its whole life."""
    _uid.set(u)


@contextlib.contextmanager
def as_user(u: str | None) -> Iterator[None]:
    token = _uid.set(u)
    try:
        yield
    finally:
        _uid.reset(token)


def bind(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap fn so it runs as the current user, even on another thread (threads do not inherit this)."""
    u = _uid.get()

    def run(*a: Any, **k: Any) -> Any:
        token = _uid.set(u)
        try:
            return fn(*a, **k)
        finally:
            _uid.reset(token)

    return run


# A number per user that goes up whenever something they can see changes (a job, a creator, an account).
# The page sends back the number it last saw; if it is the same, the server answers "nothing new" without
# touching the disk or building anything. With many people online this is what keeps the page checks cheap.
_ver: dict[str, int] = collections.defaultdict(int)
_notify_hook: Callable[[str], Any] | None = None


def bump(u: str | None = None) -> None:
    u = u or _uid.get()
    if u:
        _ver[u] += 1
        if _notify_hook is not None:
            try:
                _notify_hook(u)
            except Exception:
                pass


def version(u: str | None = None) -> int:
    return _ver[u or uid()]
