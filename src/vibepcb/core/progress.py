"""Stage reporting for long pipeline steps: timestamps, durations and a closing summary.

Library code does not print: :class:`Stages` hands finished lines to an ``emit`` callback
(``cli.py`` wires it to stdout; without one the stages are only timed).
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def format_duration(seconds: float) -> str:
    """``42 s`` / ``3 min 05 s`` / ``1 h 02 min`` - what a person wants to read for a build step."""
    total = round(seconds)
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60} min {total % 60:02d} s"
    return f"{total // 3600} h {total % 3600 // 60:02d} min"


class Stages:
    """Times named stages of a command and reports their start (with elapsed time) through ``emit``."""

    def __init__(
        self,
        emit: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.emit = emit
        self._clock = clock
        self._started = clock()
        self.done: list[tuple[str, float]] = []

    def say(self, line: str) -> None:
        """A line in the stage log with the elapsed time since the command started."""
        if self.emit is not None:
            self.emit(f"[{format_duration(self._clock() - self._started):>9}] {line}")

    @contextlib.contextmanager
    def stage(self, label: str) -> Iterator[None]:
        self.say(f"{label} ...")
        started = self._clock()
        try:
            yield
        finally:
            self.done.append((label, self._clock() - started))

    def summary(self) -> list[str]:
        """One line per finished stage plus the total; empty when no stage ran."""
        if not self.done:
            return []
        width = max(len(label) for label, _ in self.done)
        lines = [f"  {label:<{width}}  {format_duration(spent):>9}" for label, spent in self.done]
        lines.append(f"  {'total':<{width}}  {format_duration(self._clock() - self._started):>9}")
        return lines
