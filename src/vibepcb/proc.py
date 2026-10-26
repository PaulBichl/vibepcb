"""Subprocess helper: text output decoded as UTF-8 on every platform (not the locale code page)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def run(
    cmd: Sequence[str], *, cwd: Path | None = None, timeout: float | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` capturing stdout/stderr as UTF-8 text (undecodable bytes replaced), never raising on exit code."""
    return subprocess.run(
        list(cmd),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
