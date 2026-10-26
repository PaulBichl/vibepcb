from __future__ import annotations

import re
from typing import TYPE_CHECKING

from synth import write

from vibepcb import board as board_mod
from vibepcb import inspect

if TYPE_CHECKING:
    from pathlib import Path

ROUNDRECT = (
    '(footprint "t:q" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000077") (at 15 10 0)'
    '(property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))'
    '(pad "1" smd roundrect (at 0 0) (size 1.65 0.45) (layers "F.Cu" "F.Mask") (roundrect_rratio 0.5)'
    ' (net 1 "A")))'
)


def test_rounded_pad_is_drawn_full_size(tmp_path: Path) -> None:
    """A stadium pad's points are its centre line: drawn from them it was a zero-height (invisible) rect."""
    project = write(tmp_path, parts=ROUNDRECT)
    svg = inspect.render(project, board_mod.Board.from_project(project), (12, 8, 18, 12), layers=["F.Cu"])
    sizes = [
        (float(w), float(h))
        for w, h in re.findall(r'<rect x="[^"]+" y="[^"]+" width="([^"]+)" height="([^"]+)" fill="#', svg)
    ]
    assert any(abs(w - 1.65) < 0.01 and abs(h - 0.45) < 0.01 for w, h in sizes), sizes
