"""Schematic sheet size: the smallest paper that holds the drawing."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import KicadProject, _bbox, _mm, _num
from vibepcb.kicad.fixes.schematic import _pin_positions

if TYPE_CHECKING:
    from vibepcb.core.sexpr import Node


PAPER_SIZES_MM = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}


SHEET_FRAME_MM = 12.0  # border + margin on each side


TITLE_BLOCK_MM = (115.0, 40.0)  # KiCad's title block in the bottom-right corner (+ margin)


SCHEMATIC_GRID_MM = 1.27


_SHEET_META = {"version", "generator", "generator_version", "uuid", "paper", "title_block", "lib_symbols", "sheet_instances", "symbol_instances", "embedded_fonts"}  # fmt: skip


_COORDS = {"at", "xy", "start", "end", "center", "mid"}


def _sheet_placement(points: list[tuple[float, float]]) -> tuple[str, float, float] | None:
    """Smallest paper + grid-aligned shift that puts ``points`` inside the frame, clear of the title block.

    Tries the drawing centred on the sheet, then pushed to the top, then to the top-left.
    """
    xmin, ymin, xmax, ymax = _bbox(points, 2 * SCHEMATIC_GRID_MM)  # room for texts sticking out
    grid, frame = SCHEMATIC_GRID_MM, SHEET_FRAME_MM
    for paper, (w, h) in PAPER_SIZES_MM.items():
        if xmax - xmin > w - 2 * frame or ymax - ymin > h - 2 * frame:
            continue
        block_x, block_y = w - frame - TITLE_BLOCK_MM[0], h - frame - TITLE_BLOCK_MM[1]
        centred_x, centred_y = (w - xmax - xmin) / 2, (h - ymax - ymin) / 2
        for dx, dy in ((centred_x, centred_y), (centred_x, frame - ymin), (frame - xmin, frame - ymin)):
            dx, dy = round(dx / grid) * grid, round(dy / grid) * grid
            if not any(x + dx > block_x and y + dy > block_y for x, y in points):
                return paper, dx, dy
    return None


def fit_schematic_sheet(project: KicadProject) -> list[str]:
    """Put the schematic on the smallest landscape ISO sheet that holds it, centred, with a title block.

    tscircuit picks a huge sheet (A0/A1) and draws in one corner of it, so the PDF is
    mostly empty paper with unreadably small parts, and the title block stays empty.
    """
    sch = project.sch
    items = [n for n in sch if isinstance(n, list) and sexpr.head(n) not in _SHEET_META]
    points = [(_num(c, 1), _num(c, 2)) for item in items for c in sexpr.walk(item) if sexpr.head(c) in _COORDS]
    points += list(_pin_positions(sch).values())
    if not points:
        return []
    placement = _sheet_placement(points)
    if placement is None:
        return []
    paper, dx, dy = placement

    changes = []
    paper_node = sexpr.child(sch, "paper")
    if paper_node is None:
        paper_node = [Sym("paper"), paper]
        sch.insert(next(i for i, n in enumerate(sch) if sexpr.head(n) == "lib_symbols"), paper_node)
    if str(paper_node[1]) != paper:
        changes.append(f"paper {paper_node[1]} -> {paper}")
        paper_node[1:] = [paper]
    if abs(dx) >= SCHEMATIC_GRID_MM / 2 or abs(dy) >= SCHEMATIC_GRID_MM / 2:
        for item in items:
            for c in sexpr.walk(item):
                if sexpr.head(c) in _COORDS and len(c) > 2:
                    c[1], c[2] = Sym(_mm(_num(c, 1) + dx)), Sym(_mm(_num(c, 2) + dy))
        changes.append(f"drawing moved by ({_mm(dx)}, {_mm(dy)}) mm onto the sheet")
    if sexpr.child(sch, "title_block") is None:
        stem = project.sch_path.stem
        today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
        block: list[Node] = [
            Sym("title_block"),
            [Sym("title"), stem],
            [Sym("date"), today],
            [Sym("comment"), Sym("1"), f"VibePCB: generated from boards/{stem}/index.circuit.tsx"],
        ]
        sch.insert(sch.index(paper_node) + 1, block)
        changes.append(f"title block: {stem}")
    return changes
