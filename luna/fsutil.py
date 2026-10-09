"""Windows-tolerant atomic file writes (tmp + ``os.replace``).

On Windows ``os.replace`` (and opening the tmp file) raises ``PermissionError [WinError 5]`` while
another process (AV scanner, search indexer, a monitor) briefly holds the file. Both steps are
retried with bounded backoff; after the budget the error is re-raised and the destination keeps its
previous content.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

BACKOFF_S: tuple[float, ...] = (0.1, 0.2, 0.4, 0.8, 1.6, 3.2)

_os_replace = os.replace  # indirection for tests


def _write_text(path: Path, text: str) -> None:
    path.write_text(text)


def _retry(op: Callable[[], None], sleep: Callable[[float], None] | None) -> None:
    for delay in (*BACKOFF_S, None):
        try:
            op()
            return
        except PermissionError:
            if delay is None:
                raise
            (sleep or time.sleep)(delay)


def replace_with_retry(src: Path, dst: Path, sleep: Callable[[float], None] | None = None) -> None:
    """``os.replace(src, dst)`` retried on PermissionError (0.1 .. 3.2 s backoff, then re-raise)."""
    _retry(lambda: _os_replace(src, dst), sleep)


def write_text_atomic(path: Path, text: str, sleep: Callable[[float], None] | None = None) -> None:
    """Write `text` to a sibling ``.tmp`` then replace `path`; both steps retried on PermissionError."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    _retry(lambda: _write_text(tmp, text), sleep)
    replace_with_retry(tmp, path, sleep)
