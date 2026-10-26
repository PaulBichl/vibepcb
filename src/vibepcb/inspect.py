"""Pictures of the board for the agent that routes it: a crop with a mm grid, pads labelled, one net highlighted.

``render`` draws the routing model (:mod:`vibepcb.board`) as SVG; ``cli`` turns it into a PNG. Axis labels
are board millimetres, so waypoints and corridors for ``routes.toml`` can be read off the picture.
"""

from __future__ import annotations

import fnmatch
import html
import math
from typing import TYPE_CHECKING

from vibepcb import fixes, routes

if TYPE_CHECKING:
    from vibepcb import board as board_mod
    from vibepcb.fixes import KicadProject

LAYER_COLOURS = {"F.Cu": "#d33", "B.Cu": "#36c"}
INNER_COLOUR = "#2a2"
DIM = 0.18


def _colour(layer: str) -> str:
    return LAYER_COLOURS.get(layer, INNER_COLOUR)


def _fmt(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def render(
    project: KicadProject,
    board: board_mod.Board,
    box: tuple[float, float, float, float],
    *,
    px_per_mm: float = 40.0,
    layers: list[str] | None = None,
    net: str | None = None,
    labels: bool = True,
) -> str:
    """SVG of the area ``box`` (x0, y0, x1, y1 in board mm). ``net``: pattern of nets drawn at full strength."""
    x0, y0, x1, y1 = box
    w, h = (x1 - x0) * px_per_mm, (y1 - y0) * px_per_mm
    shown = layers or board.layers
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(w)}" height="{_fmt(h)}" '
        f'viewBox="{_fmt(x0)} {_fmt(y0)} {_fmt(x1 - x0)} {_fmt(y1 - y0)}">',
        f'<rect x="{_fmt(x0)}" y="{_fmt(y0)}" width="{_fmt(x1 - x0)}" height="{_fmt(y1 - y0)}" fill="#fff"/>',
    ]

    def strong(n: str) -> bool:
        return net is None or fnmatch.fnmatchcase(n, net)

    def inside(a: tuple[float, float], b: tuple[float, float], pad: float = 1.0) -> bool:
        return not (
            max(a[0], b[0]) < x0 - pad
            or min(a[0], b[0]) > x1 + pad
            or max(a[1], b[1]) < y0 - pad
            or min(a[1], b[1]) > y1 + pad
        )

    # grid: every 1 mm light, every 5 mm darker with its coordinate
    for gx in range(math.ceil(x0), math.floor(x1) + 1):
        major = gx % 5 == 0
        out.append(
            f'<line x1="{gx}" y1="{_fmt(y0)}" x2="{gx}" y2="{_fmt(y1)}" stroke="{"#bbb" if major else "#eee"}" '
            f'stroke-width="{0.03 if major else 0.015}"/>'
        )
        if major:
            out.append(f'<text x="{gx + 0.05}" y="{_fmt(y0 + 0.5)}" font-size="0.45" fill="#666">{gx}</text>')
    for gy in range(math.ceil(y0), math.floor(y1) + 1):
        major = gy % 5 == 0
        out.append(
            f'<line x1="{_fmt(x0)}" y1="{gy}" x2="{_fmt(x1)}" y2="{gy}" stroke="{"#bbb" if major else "#eee"}" '
            f'stroke-width="{0.03 if major else 0.015}"/>'
        )
        if major:
            out.append(f'<text x="{_fmt(x0 + 0.05)}" y="{gy - 0.05}" font-size="0.45" fill="#666">{gy}</text>')
    for e in board.edges:
        a, b = e.points[0], e.points[-1]
        if inside(a, b):
            out.append(
                f'<line x1="{_fmt(a[0])}" y1="{_fmt(a[1])}" x2="{_fmt(b[0])}" y2="{_fmt(b[1])}" stroke="#cc0" stroke-width="0.15"/>'
            )
    # bottom layer first, so the top layer's copper is on top
    for layer in reversed(shown):
        colour = _colour(layer)
        for s in board.copper.segs:
            if s.layer != layer or not inside(s.a, s.b):
                continue
            op = 0.85 if strong(s.net) else DIM
            out.append(
                f'<line x1="{_fmt(s.a[0])}" y1="{_fmt(s.a[1])}" x2="{_fmt(s.b[0])}" y2="{_fmt(s.b[1])}" stroke="{colour}" '
                f'stroke-width="{_fmt(s.width)}" stroke-linecap="round" opacity="{op}"/>'
            )
        for o in board.copper.pads:
            if layer not in o.layers or not inside(o.box[:2], o.box[2:]):
                continue
            # the box, not the points: a rounded pad's points are its centre line (copper = line + radius)
            bx0, by0, bx1, by1 = o.box
            op = 0.8 if strong(o.net) else DIM
            out.append(
                f'<rect x="{_fmt(bx0)}" y="{_fmt(by0)}" width="{_fmt(bx1 - bx0)}" '
                f'height="{_fmt(by1 - by0)}" fill="{colour}" opacity="{op}"/>'
            )
    for _, c, r, n in board.copper.vias:
        if x0 - 1 <= c[0] <= x1 + 1 and y0 - 1 <= c[1] <= y1 + 1:
            op = 1.0 if strong(n) else DIM + 0.1
            out.append(
                f'<circle cx="{_fmt(c[0])}" cy="{_fmt(c[1])}" r="{_fmt(r)}" fill="#888" stroke="#333" stroke-width="0.03" opacity="{op}"/>'
            )
    if labels:
        for p in routes.board_pads(project):
            if x0 <= p.centre[0] <= x1 and y0 <= p.centre[1] <= y1 and strong(p.net):
                out.append(
                    f'<text x="{_fmt(p.centre[0])}" y="{_fmt(p.centre[1] + 0.12)}" font-size="0.35" text-anchor="middle" '
                    f'fill="#000">{html.escape(p.label)}</text>'
                )
    out.append("</svg>")
    return "\n".join(out)


def summary(project: KicadProject, box: tuple[float, float, float, float], net: str | None = None) -> list[str]:
    """Text next to the picture: the pads in the area with their nets and layers."""
    x0, y0, x1, y1 = box
    lines = []
    for p in routes.board_pads(project):
        if x0 <= p.centre[0] <= x1 and y0 <= p.centre[1] <= y1 and (net is None or fnmatch.fnmatchcase(p.net, net)):
            layers = ",".join(sorted(layer.removesuffix(".Cu") for layer in p.layers if layer in fixes.COPPER_LAYERS))
            lines.append(f"{p.label:<10} {p.net or '-':<22} ({p.centre[0]:.2f}, {p.centre[1]:.2f}) {layers}")
    return lines
