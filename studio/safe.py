"""Small helpers for what is safe to show a user."""
from __future__ import annotations

import re
from typing import Any

_PATH = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s'\"]*[\\/][^\s'\"]+")


def friendly(e: Exception | str | Any, limit: int = 600) -> str:
    """An error message for the user: trimmed, with any server file paths removed. A missing ffmpeg, which Windows
    reports only as "the system cannot find the file specified", is said in plain words with what to do."""
    from . import tools
    return tools.explain(e) or _PATH.sub("[file]", str(e)[-limit:])
