"""Assembly drawings of an :class:`~vibepcb.output.assembly.Assembly`: a printable PDF and an interactive HTML.

The base of every view is ``kicad-cli pcb export svg`` (board outline from Edge.Cuts, pads from the side's
mask layer, black-and-white), embedded under a board -> page transform that mirrors the bottom side in x
(the board turned over, as the person holding it sees it). Part outlines (courtyards), orientation marks and
reference labels are drawn on top in page coordinates, so text is never mirrored. Labels go inside the part when
they fit, else next to it, else on a leader line; :func:`place_labels` never lets two labels overlap.

``write_pdf``: A4 landscape pages (title + assembly order table, one overall drawing per side, two assembly steps
per page), SVG pages joined by ``rsvg-convert``. ``render_html``: one self-contained file (inline SVG, CSS, JS).
"""

from __future__ import annotations

import html
import json
import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vibepcb.core import proc
from vibepcb.output import assembly as asm

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vibepcb.output.assembly import Assembly, Box, Part, Point, Step

PAGE_W, PAGE_H = 297.0, 210.0  # A4 landscape, mm (SVG user unit = 1 mm)
MARGIN = 8.0
FOOTER_H = 7.0
FONT = "DejaVu Sans, Arial, Helvetica, sans-serif"
LABEL_MIN = 1.5  # mm: smallest reference text inside a part
LABEL_OUT = 1.8  # mm: reference text next to a part / on a leader
STEPS_PER_PAGE = 2
RING_MAX = 6  # a step with up to this many parts rings its small ones (more rings clutter a dense step)
RING_PART = 6.0  # mm on the page: parts smaller than this are hard to spot without a ring
# DejaVu Sans advance widths (em), an upper bound for Arial / Helvetica too: text boxes for collision tests
_WIDTH = dict.fromkeys("0123456789", 0.64) | {
    "A": 0.69, "B": 0.69, "C": 0.72, "D": 0.77, "E": 0.64, "F": 0.58, "G": 0.78, "H": 0.76, "I": 0.30, "J": 0.50,
    "K": 0.69, "L": 0.58, "M": 0.87, "N": 0.75, "O": 0.79, "P": 0.67, "Q": 0.79, "R": 0.72, "S": 0.67, "T": 0.62,
    "U": 0.74, "V": 0.69, "W": 0.99, "X": 0.69, "Y": 0.67, "Z": 0.69, " ": 0.32, ".": 0.32, ",": 0.32, "-": 0.37,
    "/": 0.34, "(": 0.39, ")": 0.39, ":": 0.34, "_": 0.50, "+": 0.84, "i": 0.28, "l": 0.28, "j": 0.28, "f": 0.36,
    "t": 0.40, "r": 0.42, "m": 0.98, "w": 0.82,
}  # fmt: skip
PRINT_CSS = """
text { font-family: %(font)s; fill: #111; }
.board-bg { fill: #fff; }
.kpads { color: #b9b9b9; }
.kedge { color: #222; }
.body { fill: none; stroke: #8c8c8c; stroke-width: 0.16; }
.other .body { stroke: #b0b0b0; stroke-dasharray: 0.6 0.5; }
.tp .body { stroke: #9a9a9a; stroke-dasharray: 0.4 0.4; }
.done .body { fill: #dcdcdc; stroke: #9a9a9a; }
.hl .body { fill: #ffb300; fill-opacity: 0.55; stroke: #c25e00; stroke-width: 0.45; }
.ring { fill: none; stroke: #e07800; stroke-width: 0.4; stroke-opacity: 0.8; }
.markpad { fill: #d0021b; fill-opacity: 0.75; }
.mark { stroke: #d0021b; fill: none; stroke-linecap: round; }
.markdot { fill: #d0021b; }
.ref { font-weight: bold; paint-order: stroke; stroke: #fff; stroke-width: 0.35; stroke-linejoin: round; }
.tp .ref { fill: #777; font-weight: normal; font-style: italic; }
.leader { stroke: #333; stroke-width: 0.16; fill: none; }
.leaderdot { fill: #333; }
.hole { fill: none; stroke: #444; stroke-width: 0.2; }
.dim { stroke: #555; stroke-width: 0.15; fill: none; }
.dimtext { fill: #555; }
.halo { paint-order: stroke; stroke: #fff; stroke-width: 0.6; }
.frame { fill: none; stroke: #222; stroke-width: 0.3; }
.rule { stroke: #bbb; stroke-width: 0.15; }
.head { font-weight: bold; }
.muted { fill: #666; }
.warn { fill: #b00016; }
.box { fill: none; stroke: #222; stroke-width: 0.25; }
.badge { fill: #222; }
.badge-text { fill: #fff; font-weight: bold; }
.legend-hl { fill: #ffb300; fill-opacity: 0.55; stroke: #c25e00; stroke-width: 0.45; }
.legend-done { fill: #dcdcdc; stroke: #9a9a9a; stroke-width: 0.16; }
""" % {"font": FONT}  # noqa: UP031 - CSS braces


class AssemblyError(RuntimeError):
    """A drawing could not be made (kicad-cli / rsvg-convert failed)."""


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    """Advance width of ``text`` (mm) at font size ``size``; bold glyphs are ~10 % wider."""
    return sum(_WIDTH.get(c, 0.62 if c.islower() else 0.72) for c in text) * size * (1.13 if bold else 1.03)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _f(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


# --- view: board mm -> page mm -------------------------------------------------


@dataclass(frozen=True)
class View:
    """Board -> page transform. The bottom side is mirrored in x: the board turned over around its vertical axis."""

    side: str
    scale: float
    outline: Box  # board extent
    ox: float  # page position of the board's left / top edge
    oy: float

    @property
    def mirrored(self) -> bool:
        return self.side == "bottom"

    def page(self, x: float, y: float) -> Point:
        bx0, by0, bx1, _ = self.outline
        if self.mirrored:
            x = bx0 + bx1 - x
        return self.ox + (x - bx0) * self.scale, self.oy + (y - by0) * self.scale

    def box(self, points: Iterable[Point]) -> Box:
        pts = [self.page(x, y) for x, y in points]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def matrix(self) -> str:
        bx0, by0, bx1, _ = self.outline
        s = self.scale
        a, e = (-s, self.ox + bx1 * s) if self.mirrored else (s, self.ox - bx0 * s)
        return f"matrix({_f(a)} 0 0 {_f(s)} {_f(e)} {_f(self.oy - by0 * s)})"

    @property
    def board_box(self) -> Box:
        bx0, by0, bx1, by1 = self.outline
        return self.ox, self.oy, self.ox + (bx1 - bx0) * self.scale, self.oy + (by1 - by0) * self.scale


def fit_view(side: str, outline: Box, area: Box, *, max_scale: float = 12.0) -> View:
    """The view that fits the board into ``area`` (page mm), centred."""
    bw, bh = outline[2] - outline[0], outline[3] - outline[1]
    aw, ah = area[2] - area[0], area[3] - area[1]
    scale = min(aw / bw, ah / bh, max_scale)
    return View(side, scale, outline, area[0] + (aw - bw * scale) / 2, area[1] + (ah - bh * scale) / 2)


# --- base drawing (kicad-cli) ----------------------------------------------------


@dataclass(frozen=True)
class Base:
    """Board-coordinate SVG content per side: pads (mask layer) and the outline (Edge.Cuts); colours are
    ``__INK__`` / ``__PAPER__`` placeholders, the outline width ``__EDGEW__``."""

    pads: dict[str, str]
    edge: str


_SVG_BODY = re.compile(r"<svg\b[^>]*>(.*)</svg>", re.DOTALL)


def _inner(svg: str) -> str:
    m = _SVG_BODY.search(svg)
    body = m[1] if m else ""
    body = re.sub(r"<title>.*?</title>|<desc>.*?</desc>", "", body, flags=re.DOTALL)
    body = re.sub(r"#000000", "__INK__", body, flags=re.IGNORECASE)
    return re.sub(r"#FFFFFF", "__PAPER__", body, flags=re.IGNORECASE)


def kicad_base(pcb: Path, kicad_cli: str | None = None) -> Base | None:
    """The base drawing from ``kicad-cli pcb export svg`` (None without kicad-cli)."""
    cli = kicad_cli or shutil.which("kicad-cli")
    if cli is None:
        return None
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="vibepcb-assembly-") as tmp:
        for name, layer in (("top", "F.Mask"), ("bottom", "B.Mask"), ("edge", "Edge.Cuts")):
            target = Path(tmp) / f"{name}.svg"
            result = proc.run([cli, "pcb", "export", "svg", "-o", str(target), "--mode-single", "--layers", layer,
                               "--black-and-white", "--exclude-drawing-sheet", "--drill-shape-opt", "2", str(pcb)])  # fmt: skip
            if result.returncode != 0 or not target.exists():
                msg = f"kicad-cli pcb export svg ({layer}) failed:\n{result.stdout}{result.stderr}"
                raise AssemblyError(msg)
            out[name] = _inner(target.read_text(encoding="utf-8"))
    edge = re.sub(r"stroke-width:\s*[0-9.]+", "stroke-width:__EDGEW__", out.pop("edge"))
    return Base(pads=out, edge=edge)


def _fallback_pads(parts: Iterable[Part], side: str) -> str:
    """Pads drawn from the footprint data (no kicad-cli): rectangles, ellipses for round pads."""
    items = []
    for part in parts:
        for pad in part.pads:
            if part.side != side and not pad.tht:
                continue
            pts = " ".join(f"{_f(x)},{_f(y)}" for x, y in pad.corners)
            items.append(f'<polygon points="{pts}" fill="__INK__"/>')
    return "".join(items)


def _base_svg(base: Base | None, assembly: Assembly, view: View, *, paper: str) -> str:
    if base is not None:
        pads, edge = base.pads[view.side], base.edge
    else:
        x0, y0, x1, y1 = assembly.outline
        pads = _fallback_pads(assembly.parts, view.side)
        edge = (f'<rect x="{_f(x0)}" y="{_f(y0)}" width="{_f(x1 - x0)}" height="{_f(y1 - y0)}" '
                'style="fill:none;stroke:__INK__;stroke-width:__EDGEW__"/>')  # fmt: skip
    edge = edge.replace("__EDGEW__", _f(0.35 / view.scale))
    sub = {"__INK__": "currentColor", "__PAPER__": paper}
    for key, value in sub.items():
        pads, edge = pads.replace(key, value), edge.replace(key, value)
    return f'<g transform="{view.matrix}"><g class="kpads">{pads}</g><g class="kedge">{edge}</g></g>'


# --- label placement -------------------------------------------------------------


@dataclass(frozen=True)
class Label:
    ref: str
    text: str
    x: float  # centre, page mm
    y: float
    size: float
    vertical: bool = False
    leader: tuple[Point, Point] | None = None  # label edge -> part

    @property
    def box(self) -> Box:
        w, h = text_width(self.text, self.size, bold=True), self.size * 0.78
        if self.vertical:
            w, h = h, w
        return self.x - w / 2, self.y - h / 2, self.x + w / 2, self.y + h / 2


class _Grid:
    """Boxes in 4 mm buckets: overlap queries without testing every box."""

    def __init__(self, cell: float = 4.0) -> None:
        self.cell = cell
        self.items: dict[tuple[int, int], list[tuple[Box, str]]] = {}

    def _cells(self, b: Box) -> Iterable[tuple[int, int]]:
        c = self.cell
        for i in range(math.floor(b[0] / c), math.floor(b[2] / c) + 1):
            for j in range(math.floor(b[1] / c), math.floor(b[3] / c) + 1):
                yield i, j

    def add(self, b: Box, key: str) -> None:
        for cell in self._cells(b):
            self.items.setdefault(cell, []).append((b, key))

    def hits(self, b: Box, *, ignore: str | None = None) -> list[str]:
        found = []
        for cell in self._cells(b):
            for other, key in self.items.get(cell, ()):
                if key != ignore and _overlap(b, other):
                    found.append(key)
        return found


def _overlap(a: Box, b: Box, eps: float = 1e-6) -> bool:
    return a[0] < b[2] - eps and b[0] < a[2] - eps and a[1] < b[3] - eps and b[1] < a[3] - eps


def _inside(a: Box, b: Box) -> bool:
    return a[0] >= b[0] and a[1] >= b[1] and a[2] <= b[2] and a[3] <= b[3]


def _grow(b: Box, d: float) -> Box:
    return b[0] - d, b[1] - d, b[2] + d, b[3] + d


def _segment_hits_box(p: Point, q: Point, b: Box) -> bool:
    """Liang-Barsky: whether segment p-q passes through the open box ``b``."""
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for denom, num in ((-dx, p[0] - b[0]), (dx, b[2] - p[0]), (-dy, p[1] - b[1]), (dy, b[3] - p[1])):
        if abs(denom) < 1e-12:
            if num < 0:
                return False
            continue
        t = num / denom
        if denom < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return t1 - t0 > 1e-6


def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0


def _clip_to_box(inner: Point, outer: Point, b: Box) -> Point:
    """Where the segment from ``inner`` (inside ``b``) towards ``outer`` leaves ``b``."""
    dx, dy = outer[0] - inner[0], outer[1] - inner[1]
    t = 1.0
    for d, lo, hi, s in ((dx, b[0], b[2], inner[0]), (dy, b[1], b[3], inner[1])):
        if d > 1e-12:
            t = min(t, (hi - s) / d)
        elif d < -1e-12:
            t = min(t, (lo - s) / d)
    return inner[0] + dx * t, inner[1] + dy * t


def place_labels(
    items: Sequence[tuple[str, str, Box]],
    obstacles: Sequence[tuple[Box, str]],
    bounds: Box,
    *,
    size: float,
    outside_size: float = LABEL_OUT,
    min_size: float = LABEL_MIN,
    max_leader: float = 90.0,
    pads: dict[str, list[Box]] | None = None,
) -> list[Label]:
    """Reference labels for ``items`` (``key, text, part box`` in page mm), none overlapping another label.

    In order of preference: inside the part's box (horizontal, then vertical, ``size`` down to ``min_size``),
    next to it (no other part or label covered), on a leader line from a free spot nearby (the shortest one that
    crosses no label and as few parts / leaders as possible); as a last resort a free spot anywhere in ``bounds``.
    ``obstacles`` (``box, key``) are what an outside label must not cover: parts (its own included), marks.
    ``pads``: per key, the part's own pads; an inside label keeps off them when there is room.
    """
    labels: dict[str, Label] = {}
    placed = _Grid()  # label boxes
    blocked = _Grid()
    for b, key in obstacles:
        blocked.add(b, key)
    targets = _Grid()  # the labelled parts: a label right next to another one reads as its label
    for key, _, b in items:
        targets.add(b, key)
    leaders: list[tuple[Point, Point]] = []
    rest = []
    sizes = [round(size - 0.1 * i, 2) for i in range(round((size - min_size) / 0.1) + 1)]
    preferred = [s for s in sizes if s >= size * 0.8] or sizes[:1]
    smaller = [s for s in sizes if s not in preferred]
    pad = 0.15
    for key, text, b in sorted(items, key=lambda it: -(it[2][2] - it[2][0]) * (it[2][3] - it[2][1])):
        room = _grow(b, -pad)
        own_pads = (pads or {}).get(key, [])
        found = None
        for options in (preferred, smaller):
            for vertical in (False, True):
                for avoid in [own_pads, []] if own_pads else [[]]:
                    for s in options:
                        found = _inside_label(key, text, room, s, vertical, placed, blocked, avoid)
                        if found:
                            break
                    if found:
                        break
                if found:
                    break
            if found:
                break
        if found:
            labels[key] = found
            placed.add(found.box, key)
        else:
            rest.append((key, text, b))

    def crowding(it: tuple[str, str, Box]) -> int:
        return len(blocked.hits(_grow(it[2], 4.0)))

    for key, text, b in sorted(rest, key=lambda it: (-crowding(it), it[0])):
        label = _adjacent(key, text, b, outside_size, bounds, placed, blocked, leaders, targets)
        if label is None:
            label = _leader(key, text, b, outside_size, bounds, placed, blocked, leaders, max_leader, targets)
        if label is None:  # nowhere free: overlap parts, never labels
            label = _leader(key, text, b, outside_size, bounds, placed, None, leaders, max(PAGE_W, PAGE_H), targets)
        if label is None:
            label = Label(key, text, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, min_size)
        labels[key] = label
        placed.add(label.box, key)
        if label.leader:
            leaders.append(label.leader)
    return [labels[key] for key, _, _ in items]


def _inside_label(
    key: str, text: str, room: Box, size: float, vertical: bool, placed: _Grid, blocked: _Grid, pads: list[Box]
) -> Label | None:
    """A label inside ``room`` (centred, else against one of its sides), covering no other part / label / mark
    (nor one of ``pads``)."""
    probe = Label(key, text, 0, 0, size, vertical).box
    w, h = probe[2] - probe[0], probe[3] - probe[1]
    if w > room[2] - room[0] or h > room[3] - room[1]:
        return None
    cx, cy = (room[0] + room[2]) / 2, (room[1] + room[3]) / 2
    spots = [(cx, cy), (cx, room[1] + h / 2), (cx, room[3] - h / 2), (room[0] + w / 2, cy), (room[2] - w / 2, cy)]
    for x, y in spots:
        label = Label(key, text, x, y, size, vertical)
        box = label.box
        if placed.hits(box) or blocked.hits(_grow(box, -0.05), ignore=key):
            continue
        if any(_overlap(box, p) for p in pads):
            continue
        return label
    return None


def _free(box: Box, bounds: Box, placed: _Grid, blocked: _Grid | None, leaders: list[tuple[Point, Point]]) -> bool:
    if not _inside(box, bounds) or placed.hits(_grow(box, 0.25)):
        return False
    if blocked is not None and blocked.hits(_grow(box, 0.1)):
        return False
    return not any(_segment_hits_box(p, q, _grow(box, 0.15)) for p, q in leaders)


def _adjacent(
    key: str,
    text: str,
    b: Box,
    size: float,
    bounds: Box,
    placed: _Grid,
    blocked: _Grid,
    leaders: list[tuple[Point, Point]],
    targets: _Grid,
) -> Label | None:
    """A label right next to its part, clear of every other labelled part (else it is ambiguous)."""
    w, h = text_width(text, size, bold=True), size * 0.78
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    g = 0.35
    spots = [
        (b[2] + g + w / 2, cy), (b[0] - g - w / 2, cy), (cx, b[1] - g - h / 2), (cx, b[3] + g + h / 2),
        (b[2] + g + w / 2, b[1] - g - h / 2), (b[0] - g - w / 2, b[1] - g - h / 2),
        (b[2] + g + w / 2, b[3] + g + h / 2), (b[0] - g - w / 2, b[3] + g + h / 2),
    ]  # fmt: skip
    for x, y in spots:
        label = Label(key, text, x, y, size)
        if _free(label.box, bounds, placed, blocked, leaders) and not targets.hits(_grow(label.box, 1.0), ignore=key):
            return label
    return None


def _leader(
    key: str,
    text: str,
    b: Box,
    size: float,
    bounds: Box,
    placed: _Grid,
    blocked: _Grid | None,
    leaders: list[tuple[Point, Point]],
    max_leader: float,
    targets: _Grid,
) -> Label | None:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    h = size * 0.78
    best: tuple[float, Label] | None = None
    r = max(b[2] - b[0], b[3] - b[1]) / 2 + h + 1.0
    first_hit = None
    while r <= max_leader and (first_hit is None or r <= first_hit + 4.0):
        steps = max(16, int(2 * math.pi * r / 1.5))
        for i in range(steps):
            a = 2 * math.pi * i / steps
            x, y = cx + r * math.cos(a), cy + r * math.sin(a)
            label = Label(key, text, x, y, size)
            box = label.box
            if not _free(box, bounds, placed, blocked, leaders):
                continue
            start = _clip_to_box((x, y), (cx, cy), _grow(box, 0.3))
            end = (cx, cy)
            if any(_segment_hits_box(start, end, _grow(o, -0.05)) for o in _boxes_near(placed, start, end)):
                continue
            crossings = sum(_segments_cross(start, end, p, q) for p, q in leaders)
            parts = 0 if blocked is None else len(set(_segment_keys(blocked, start, end, key)))
            beside = bool(targets.hits(_grow(box, 0.8), ignore=key))
            cost = r + 8.0 * crossings + 5.0 * parts + 4.0 * beside  # across / beside another part is ambiguous
            if best is None or cost < best[0]:
                best = (cost, Label(key, text, x, y, size, leader=(start, end)))
            first_hit = first_hit if first_hit is not None else r
        r += 1.2
    return None if best is None else best[1]


def _segment_bbox(p: Point, q: Point) -> Box:
    return min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1])


def _boxes_near(grid: _Grid, p: Point, q: Point) -> list[Box]:
    seen: dict[Box, None] = {}
    for cell in grid._cells(_segment_bbox(p, q)):
        for b, _ in grid.items.get(cell, ()):
            seen[b] = None
    return list(seen)


def _segment_keys(grid: _Grid, p: Point, q: Point, own: str) -> list[str]:
    keys = []
    for cell in grid._cells(_segment_bbox(p, q)):
        for b, key in grid.items.get(cell, ()):
            if key != own and _segment_hits_box(p, q, b):
                keys.append(key)
    return keys


# --- one side --------------------------------------------------------------------


def _poly(points: Iterable[Point], cls: str) -> str:
    return f'<polygon class="{cls}" points="{" ".join(f"{_f(x)},{_f(y)}" for x, y in points)}"/>'


def _mark_svg(part: Part, view: View, *, size: float) -> tuple[str, list[Box]]:
    """The orientation mark of a part (page coordinates) and the boxes it covers."""
    mark = part.mark
    pad = part.pad(mark.pad) if mark is not None else None
    if mark is None or pad is None:
        return "", []
    corners = [view.page(x, y) for x, y in pad.corners]
    out = [_poly(corners, "markpad")]
    px, py = view.page(pad.x, pad.y)
    if mark.kind == "pin1" or len({p.number for p in part.pads}) != 2:
        cx, cy = view.page(part.x, part.y)
        dx, dy = px - cx, py - cy
    else:
        other = next(p for p in part.pads if p.number and p.number != pad.number)
        ox, oy = view.page(other.x, other.y)
        dx, dy = px - ox, py - oy
    length = math.hypot(dx, dy) or 1.0
    dx, dy = (dx / length, dy / length) if length > 1e-9 else (-1.0, 0.0)
    reach = max((x - px) * dx + (y - py) * dy for x, y in corners)  # pad edge along the direction
    across = max(abs(-(x - px) * dy + (y - py) * dx) for x, y in corners)
    boxes: list[Box] = []
    if mark.kind == "pin1":
        r = size * 0.32
        mx, my = px + dx * (reach + r + 0.25), py + dy * (reach + r + 0.25)
        out.append(f'<circle class="markdot" cx="{_f(mx)}" cy="{_f(my)}" r="{_f(r)}"/>')
        boxes.append((mx - r, my - r, mx + r, my + r))
    elif mark.kind == "cathode":
        w = max(size * 0.22, 0.3)
        mx, my = px + dx * (reach + w / 2 + 0.2), py + dy * (reach + w / 2 + 0.2)
        half = max(across, size * 0.35)
        a, b = (mx - dy * half, my + dx * half), (mx + dy * half, my - dx * half)
        out.append(f'<line class="mark" x1="{_f(a[0])}" y1="{_f(a[1])}" x2="{_f(b[0])}" y2="{_f(b[1])}" '
                   f'stroke-width="{_f(w)}"/>')  # fmt: skip
        boxes.append(_grow(_segment_bbox(a, b), w / 2))
    else:  # plus
        arm, w = size * 0.42, max(size * 0.16, 0.25)
        mx, my = px + dx * (reach + arm + 0.35), py + dy * (reach + arm + 0.35)
        out.append(f'<path class="mark" d="M{_f(mx - arm)} {_f(my)}H{_f(mx + arm)}M{_f(mx)} {_f(my - arm)}'
                   f'V{_f(my + arm)}" stroke-width="{_f(w)}"/>')  # fmt: skip
        boxes.append((mx - arm, my - arm, mx + arm, my + arm))
    return "".join(out), boxes


def _dimensions(view: View) -> tuple[str, list[Box]]:
    """Board width under the board, height left of it."""
    x0, y0, x1, y1 = view.board_box
    width, height = view.outline[2] - view.outline[0], view.outline[3] - view.outline[1]
    off, t = 4.0, 2.2
    by, lx = y1 + off, x0 - off
    out = [
        f'<path class="dim" d="M{_f(x0)} {_f(y1 + 0.8)}V{_f(by + 1)}M{_f(x1)} {_f(y1 + 0.8)}V{_f(by + 1)}'
        f'M{_f(x0)} {_f(by)}H{_f(x1)}M{_f(x0 - 0.8)} {_f(y0)}H{_f(lx - 1)}M{_f(x0 - 0.8)} {_f(y1)}H{_f(lx - 1)}'
        f'M{_f(lx)} {_f(y0)}V{_f(y1)}"/>',
        _arrow(x0, by, 1, 0), _arrow(x1, by, -1, 0), _arrow(lx, y0, 0, 1), _arrow(lx, y1, 0, -1),
        f'<text class="dimtext halo" x="{_f((x0 + x1) / 2)}" y="{_f(by - 0.6)}" font-size="{t}" text-anchor="middle" '
        f'>{width:.1f} mm</text>',
        f'<text class="dimtext" x="{_f(lx - 0.6)}" y="{_f((y0 + y1) / 2)}" font-size="{t}" text-anchor="middle" '
        f'transform="rotate(-90 {_f(lx - 0.6)} {_f((y0 + y1) / 2)})">{height:.1f} mm</text>',
    ]  # fmt: skip
    tw = text_width(f"{width:.1f} mm", t)
    boxes = [
        ((x0 + x1) / 2 - tw / 2, by - t, (x0 + x1) / 2 + tw / 2, by + 0.5),
        (lx - t - 0.6, (y0 + y1) / 2 - tw / 2, lx + 0.5, (y0 + y1) / 2 + tw / 2),
    ]
    return "".join(out), boxes


def _arrow(x: float, y: float, dx: float, dy: float) -> str:
    s, w = 1.2, 0.45
    return (f'<path class="dimtext" d="M{_f(x)} {_f(y)}L{_f(x + dx * s - dy * w)} {_f(y + dy * s + dx * w)}'
            f'L{_f(x + dx * s + dy * w)} {_f(y + dy * s - dx * w)}Z"/>')  # fmt: skip


@dataclass(frozen=True)
class Drawing:
    svg: str
    labels: list[Label]


def draw_side(
    assembly: Assembly,
    view: View,
    base: Base | None,
    bounds: Box,
    *,
    highlight: frozenset[str] | None = None,
    done: frozenset[str] = frozenset(),
    label_size: float | None = None,
    dimensions: bool = True,
    interactive: bool = False,
    paper: str = "#ffffff",
) -> Drawing:
    """One side of the board in page coordinates: base, part outlines, marks, labels.

    ``highlight``: the parts of an assembly step (only they get labels and marks; None = an overall drawing,
    every part labelled and marked). ``done``: parts of earlier steps (filled grey).
    """
    side = view.side
    mine = [p for p in assembly.parts if p.side == side]
    through = [p for p in assembly.parts if p.side != side and p.tht]
    size = label_size or min(3.2, max(1.8, 0.85 * view.scale))
    out = [_base_svg(base, assembly, view, paper=paper)]
    obstacles: list[tuple[Box, str]] = []
    for part in through:  # through-hole parts of the other side: their pins come through here
        out.append(f'<g class="part other" data-ref="{_esc(part.ref)}">'
                   f'{_poly((view.page(x, y) for x, y in part.outline), "body")}</g>')  # fmt: skip
        obstacles += [(view.box(pad.corners), "") for pad in part.pads if pad.tht]
    labelled = []
    pad_boxes: dict[str, list[Box]] = {}
    for part in sorted(mine, key=lambda p: (p.ref not in (highlight or ()), asm.natural_key(p.ref))):
        state = []
        if part.testpoint:
            state.append("tp")
        if highlight is not None and part.ref in highlight:
            state.append("hl")
        elif part.ref in done:
            state.append("done")
        box = view.box(part.outline)
        obstacles.append((box, part.ref))
        show = highlight is None or part.ref in highlight
        mark, mark_boxes = _mark_svg(part, view, size=size) if show else ("", [])
        obstacles += [(b, f"{part.ref}#mark") for b in mark_boxes]  # not even its own label covers a mark
        cls = " ".join(["part", *state])
        ring = ""
        small = max(box[2] - box[0], box[3] - box[1]) < RING_PART
        if highlight is not None and part.ref in highlight and len(highlight) <= RING_MAX and small:
            r = max(math.hypot(box[2] - box[0], box[3] - box[1]) / 2 + 1.0, 2.6)
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            ring = f'<circle class="ring" cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(r)}"/>'
        out.append(f'<g class="{cls}" data-ref="{_esc(part.ref)}">{ring}'
                   f'{_poly((view.page(x, y) for x, y in part.outline), "body")}{mark}</g>')  # fmt: skip
        if show:
            labelled.append((part.ref, part.ref, box))
            pad_boxes[part.ref] = [view.box(p.corners) for p in part.pads]
    for hole in assembly.holes:
        hx, hy = view.page(hole.x, hole.y)
        r = hole.diameter / 2 * view.scale
        out.append(f'<circle class="hole" cx="{_f(hx)}" cy="{_f(hy)}" r="{_f(r)}"/>')
        obstacles.append(((hx - r, hy - r, hx + r, hy + r), ""))
    if dimensions:
        dims, dim_boxes = _dimensions(view)
        out.append(dims)
        obstacles += [(b, "") for b in dim_boxes]
    labels = place_labels(
        labelled, obstacles, bounds, size=size, outside_size=min(size, LABEL_OUT + 0.2), pads=pad_boxes
    )
    out.append(_labels_svg(labels, assembly, interactive=interactive))
    return Drawing("".join(out), labels)


def _labels_svg(labels: list[Label], assembly: Assembly, *, interactive: bool) -> str:
    by_ref = assembly.by_ref
    out = []
    for label in labels:
        tp = " tp" if by_ref[label.ref].testpoint else ""
        attrs = f' data-ref="{_esc(label.ref)}"' if interactive else ""
        out.append(f'<g class="lbl{tp}"{attrs}>')
        if label.leader:
            (sx, sy), (ex, ey) = label.leader
            out.append(f'<line class="leader" x1="{_f(sx)}" y1="{_f(sy)}" x2="{_f(ex)}" y2="{_f(ey)}"/>'
                       f'<circle class="leaderdot" cx="{_f(ex)}" cy="{_f(ey)}" r="0.3"/>')  # fmt: skip
        baseline = label.size * 0.36
        if label.vertical:
            x, y = label.x + baseline, label.y
            rot = f' transform="rotate(-90 {_f(x)} {_f(y)})"'
        else:
            x, y, rot = label.x, label.y + baseline, ""
        out.append(f'<text class="ref" x="{_f(x)}" y="{_f(y)}" font-size="{_f(label.size)}" '
                   f'text-anchor="middle"{rot}>{_esc(label.text)}</text></g>')  # fmt: skip
    return "".join(out)


# --- PDF pages ---------------------------------------------------------------------


def _page(body: str, *, number: int, total: int, assembly: Assembly, title: str) -> str:
    rev = f" rev {assembly.revision}" if assembly.revision else ""
    y = PAGE_H - MARGIN + 0.2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W}mm" height="{PAGE_H}mm" '
        f'viewBox="0 0 {PAGE_W} {PAGE_H}"><style>{PRINT_CSS}</style>'
        f'<rect x="0" y="0" width="{PAGE_W}" height="{PAGE_H}" class="board-bg"/>{body}'
        f'<line class="rule" x1="{MARGIN}" y1="{PAGE_H - MARGIN - FOOTER_H + 2}" x2="{PAGE_W - MARGIN}" '
        f'y2="{PAGE_H - MARGIN - FOOTER_H + 2}"/>'
        f'<text x="{MARGIN}" y="{_f(y)}" font-size="2.6" class="head">{_esc(assembly.name)}{_esc(rev)} - assembly</text>'
        f'<text x="{PAGE_W / 2}" y="{_f(y)}" font-size="2.6" text-anchor="middle">{_esc(title)}</text>'
        f'<text x="{PAGE_W - MARGIN}" y="{_f(y)}" font-size="2.6" text-anchor="end">page {number} / {total}</text>'
        "</svg>"
    )  # fmt: skip


def _side_title(side: str) -> tuple[str, str]:
    if side == "bottom":
        return (
            "BOTTOM SIDE - seen from below",
            "board turned over around its vertical axis: left and right are swapped against the top view",
        )
    return "TOP SIDE - seen from above", "as in the KiCad layout"


def _badge(x: float, y: float, text: str, size: float = 3.0) -> tuple[str, float]:
    w = text_width(text, size, bold=True) + 2.4
    return (
        f'<rect class="badge" x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" height="{_f(size * 1.5)}" rx="0.8"/>'
        f'<text class="badge-text" x="{_f(x + 1.2)}" y="{_f(y + size * 1.08)}" font-size="{size}">{_esc(text)}</text>',
        w,
    )


def _legend(x: float, y: float, *, step: bool = False) -> str:
    """Mark legend, one row (page mm)."""
    s = 2.4
    items = []
    cx = x
    items.append(f'<circle class="markdot" cx="{_f(cx + 0.8)}" cy="{_f(y - 0.8)}" r="0.8"/>')
    items.append(f'<text x="{_f(cx + 2.4)}" y="{_f(y)}" font-size="{s}">pin 1</text>')
    cx += 2.4 + text_width("pin 1", s) + 4
    items.append(f'<line class="mark" x1="{_f(cx + 0.8)}" y1="{_f(y - 2.2)}" x2="{_f(cx + 0.8)}" y2="{_f(y + 0.6)}" '
                 'stroke-width="0.6"/>')  # fmt: skip
    items.append(f'<text x="{_f(cx + 2.4)}" y="{_f(y)}" font-size="{s}">cathode (K)</text>')
    cx += 2.4 + text_width("cathode (K)", s) + 4
    items.append(f'<path class="mark" d="M{_f(cx)} {_f(y - 0.8)}H{_f(cx + 1.8)}M{_f(cx + 0.9)} {_f(y - 1.7)}'
                 f'V{_f(y + 0.1)}" stroke-width="0.45"/>')  # fmt: skip
    items.append(f'<text x="{_f(cx + 2.8)}" y="{_f(y)}" font-size="{s}">+ (polarised capacitor)</text>')
    cx += 2.8 + text_width("+ (polarised capacitor)", s) + 4
    items.append(f'<rect class="markpad" x="{_f(cx)}" y="{_f(y - 1.8)}" width="2.4" height="2"/>')
    items.append(f'<text x="{_f(cx + 3.2)}" y="{_f(y)}" font-size="{s}">the marked pad</text>')
    cx += 3.2 + text_width("the marked pad", s) + 4
    if step:
        items.append(f'<rect class="legend-hl" x="{_f(cx)}" y="{_f(y - 1.9)}" width="3" height="2.2"/>')
        items.append(f'<text x="{_f(cx + 3.8)}" y="{_f(y)}" font-size="{s}">this step</text>')
        cx += 3.8 + text_width("this step", s) + 4
        items.append(f'<rect class="legend-done" x="{_f(cx)}" y="{_f(y - 1.9)}" width="3" height="2.2"/>')
        items.append(f'<text x="{_f(cx + 3.8)}" y="{_f(y)}" font-size="{s}">earlier steps</text>')
    return "".join(items)


def _wrap(text: str, size: float, width: float, *, bold: bool = False) -> list[str]:
    """``text`` in lines no wider than ``width`` (mm): broken after spaces / commas / semicolons, a word longer
    than a line after the character where it stops fitting."""

    def fits(t: str) -> bool:
        return text_width(t, size, bold=bold) <= width

    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in re.split(r"(?<=[ ,;-])|(?=\()", paragraph):
            if line and not fits(line + word.rstrip()):
                lines.append(line.rstrip())
                line = word.lstrip()
            else:
                line += word
            while not fits(line.rstrip()) and len(line) > 1:  # one word wider than the column
                cut = max(i for i in range(1, len(line)) if fits(line[:i]) or i == 1)
                lines.append(line[:cut])
                line = line[cut:]
        lines.append(line.rstrip())
    return lines or [""]


def _text_lines(x: float, y: float, lines: list[str], size: float, cls: str = "", lead: float = 1.3) -> str:
    c = f' class="{cls}"' if cls else ""
    return "".join(
        f'<text{c} x="{_f(x)}" y="{_f(y + i * size * lead)}" font-size="{_f(size)}">{_esc(line)}</text>'
        for i, line in enumerate(lines)
    )


def _summary_lines(assembly: Assembly) -> list[str]:
    fitted = [p for p in assembly.parts if p.in_bom and not p.dnp and not p.testpoint]
    per_side = {s: sum(p.side == s for p in fitted) for s in asm.SIDES}
    tht = sum(p.tht for p in fitted)
    order: list[str] = []
    for step in assembly.steps:
        label = "through-hole" if step.rank >= asm.THT_RANK else f"{step.side} SMD"
        if not order or order[-1] != label:
            order.append(label)
    lines = [
        f"{len(fitted)} parts in {len(assembly.steps)} steps: top {per_side['top']}, bottom {per_side['bottom']}"
        f" ({tht} through-hole)",
        "Order: " + " -> ".join(order) + "; within a side: fine-pitch / leadless ICs, ICs, small parts, large parts, "
        "connectors",
    ]
    if assembly.testpoints:
        lines.append("Not assembled (test points, bare pads): " + ", ".join(p.ref for p in assembly.testpoints))
    if assembly.not_fitted:
        lines.append("Not fitted (DNP / not in the BOM): " + ", ".join(p.ref for p in assembly.not_fitted))
    return lines


TABLE_COLS = (
    ("Step", 10.0),
    ("Side", 14.0),
    ("Value", 36.0),
    ("Package", 40.0),
    ("Qty", 8.0),
    ("References", 74.0),
    ("Note", 0.0),
)


def _table_rows(steps: list[Step], size: float) -> list[tuple[Step, list[list[str]], float]]:
    total = PAGE_W - 2 * MARGIN
    widths = [w for _, w in TABLE_COLS]
    widths[-1] = total - sum(widths[:-1])
    rows = []
    for step in steps:
        side = "THT " + step.side if step.rank >= asm.THT_RANK else step.side
        cells = [
            str(step.number),
            side,
            step.value,
            step.footprint,
            str(len(step.refs)),
            ", ".join(step.refs),
            step.note,
        ]
        wrapped = [
            _wrap(c, size, w - 1.6, bold=i in (0, 2)) for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ]
        height = max(len(w) for w in wrapped) * size * 1.25 + 1.2
        rows.append((step, wrapped, height))
    return rows


def _table(rows: list[tuple[Step, list[list[str]], float]], x: float, y: float, size: float) -> str:
    total = PAGE_W - 2 * MARGIN
    widths = [w for _, w in TABLE_COLS]
    widths[-1] = total - sum(widths[:-1])
    out = [f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(total)}" height="{_f(size * 1.9)}" fill="#e8e8e8"/>']
    cx = x
    for (name, _), w in zip(TABLE_COLS, widths, strict=True):
        out.append(
            f'<text class="head" x="{_f(cx + 0.8)}" y="{_f(y + size * 1.35)}" font-size="{_f(size)}">{name}</text>'
        )
        cx += w
    y += size * 1.9
    for step, wrapped, height in rows:
        cx = x
        for i, (lines, w) in enumerate(zip(wrapped, widths, strict=True)):
            cls = "warn" if i == len(widths) - 1 and step.note else ("head" if i in (0, 2) else "")
            out.append(_text_lines(cx + 0.8, y + size * 1.1, lines, size, cls, lead=1.25))
            cx += w
        y += height
        out.append(f'<line class="rule" x1="{_f(x)}" y1="{_f(y)}" x2="{_f(x + total)}" y2="{_f(y)}"/>')
    return "".join(out)


def title_pages(assembly: Assembly) -> list[tuple[str, str]]:
    """Title page(s): board data, summary, warnings, legend and the assembly order table (continued as needed)."""
    size = 2.25
    rows = _table_rows(assembly.steps, size)
    rev = f"revision {assembly.revision}" if assembly.revision else "revision -"
    head = [
        f'<text class="head" x="{MARGIN}" y="{MARGIN + 8}" font-size="8">{_esc(assembly.name)}</text>',
        f'<text x="{_f(MARGIN + text_width(assembly.name, 8, bold=True) + 3)}" y="{MARGIN + 8}" font-size="5" class="muted">'
        "assembly drawing - hand soldering</text>",
        _text_lines(
            MARGIN, MARGIN + 15,
            [f"{rev} \u00b7 design date {assembly.date or '-'} \u00b7 board {assembly.width:.1f} x "
             f"{assembly.height:.1f} mm, {assembly.copper_layers} copper layers, {assembly.thickness:g} mm thick"],
            3.2,
        ),
        _text_lines(MARGIN, MARGIN + 21, _summary_lines(assembly), 2.8, lead=1.35),
    ]  # fmt: skip
    y = MARGIN + 21 + len(_summary_lines(assembly)) * 2.8 * 1.35
    if assembly.warnings:
        lines = [f"CHECK: {w}" for w in assembly.warnings]
        head.append(_text_lines(MARGIN, y + 1, lines, 2.8, "warn", lead=1.35))
        y += len(lines) * 2.8 * 1.35 + 1
    head.append(f'<text class="head" x="{MARGIN}" y="{_f(y + 3)}" font-size="2.8">Marks on the drawings:</text>')
    head.append(_legend(MARGIN + 38, y + 3))
    hint = "Tick each reference on the step pages when it is soldered."
    if assembly.side_parts("bottom"):
        hint = (
            "Bottom-side drawings are seen from below (board turned over left-right), references stay readable. " + hint
        )
    head.append(f'<text class="muted" x="{MARGIN}" y="{_f(y + 7.5)}" font-size="2.6">{_esc(hint)}</text>')
    y += 11
    pages: list[tuple[str, str]] = []
    bottom = PAGE_H - MARGIN - FOOTER_H
    body = list(head)
    body.append(f'<text class="head" x="{MARGIN}" y="{_f(y + 2)}" font-size="3.4">Assembly order</text>')
    y += 4
    chunk: list[tuple[Step, list[list[str]], float]] = []
    start = y
    for row in rows:
        if chunk and start + size * 1.9 + sum(r[2] for r in chunk) + row[2] > bottom:
            body.append(_table(chunk, MARGIN, start, size))
            pages.append(("".join(body), "assembly order"))
            body, chunk, start = [], [], MARGIN + 4
            body.append(
                f'<text class="head" x="{MARGIN}" y="{MARGIN + 2}" font-size="3.4">Assembly order (cont.)</text>'
            )
        chunk.append(row)
    body.append(_table(chunk, MARGIN, start, size))
    pages.append(("".join(body), "assembly order"))
    return pages


def overview_page(assembly: Assembly, side: str, base: Base | None) -> tuple[str, str]:
    title, sub = _side_title(side)
    top = MARGIN + 13
    area = (MARGIN + 9, top + 3, PAGE_W - MARGIN - 4, PAGE_H - MARGIN - FOOTER_H - 7)
    view = fit_view(side, assembly.outline, area)
    bounds = (MARGIN, top, PAGE_W - MARGIN, PAGE_H - MARGIN - FOOTER_H + 1)
    drawing = draw_side(assembly, view, base, bounds)
    count = sum(1 for p in assembly.parts if p.side == side and not p.testpoint)
    badge, w = _badge(MARGIN, MARGIN, title, 4.0)
    body = [
        badge,
        f'<text x="{_f(MARGIN + w + 3)}" y="{MARGIN + 4.4}" font-size="3">{_esc(sub)}</text>',
        f'<text class="muted" x="{MARGIN}" y="{MARGIN + 10.5}" font-size="2.5">{count} parts on this side, '
        f"scale {view.scale:.2f} : 1</text>",
        _legend(PAGE_W - MARGIN - 150, MARGIN + 10.5),
        drawing.svg,
    ]
    return "".join(body), f"{side} side"


def step_panels(assembly: Assembly, base: Base | None) -> list[tuple[str, str]]:
    """Two steps per page: the step's parts highlighted on their side (earlier steps filled grey), its data
    and a checkbox per reference."""
    pages = []
    panel_h = (PAGE_H - 2 * MARGIN - FOOTER_H - 6) / STEPS_PER_PAGE
    done_by_side: dict[str, set[str]] = {s: set() for s in asm.SIDES}
    total = len(assembly.steps)
    steps = assembly.steps
    for i in range(0, total, STEPS_PER_PAGE):
        body = [_legend(MARGIN, MARGIN + 2.5, step=True)]
        for j, step in enumerate(steps[i : i + STEPS_PER_PAGE]):
            y0 = MARGIN + 5 + j * panel_h
            body.append(_step_panel(assembly, base, step, y0, panel_h, frozenset(done_by_side[step.side]), total))
            done_by_side[step.side].update(step.refs)
            if j:
                body.append(
                    f'<line class="rule" x1="{MARGIN}" y1="{_f(y0 - 1)}" x2="{PAGE_W - MARGIN}" y2="{_f(y0 - 1)}"/>'
                )
        last = steps[min(i + STEPS_PER_PAGE, total) - 1].number
        pages.append(("".join(body), f"steps {steps[i].number}-{last}" if last != steps[i].number else f"step {last}"))
    return pages


def _step_panel(
    assembly: Assembly, base: Base | None, step: Step, y0: float, h: float, done: frozenset[str], total: int
) -> str:
    min_info = 90.0
    full = (MARGIN + 7, y0 + 3, PAGE_W - MARGIN - min_info - 10, y0 + h - 4)
    view = fit_view(step.side, assembly.outline, full)
    # the drawing as large as the panel height allows, left-aligned; the info column takes the rest
    view = View(view.side, view.scale, view.outline, full[0], view.oy)
    right = view.board_box[2] + 8
    info_w = PAGE_W - MARGIN - right - 2
    bounds = (MARGIN, y0, right - 1, y0 + h - 1)
    drawing = draw_side(assembly, view, base, bounds, highlight=frozenset(step.refs), done=done,
                        label_size=min(3.0, max(2.0, 0.7 * view.scale)), dimensions=False)  # fmt: skip
    x = right + 2
    out = [drawing.svg, f'<line class="rule" x1="{_f(x - 2)}" y1="{_f(y0)}" x2="{_f(x - 2)}" y2="{_f(y0 + h - 2)}"/>']
    out.append(f'<text class="head" x="{_f(x)}" y="{_f(y0 + 6)}" font-size="5">Step {step.number}'
               f'<tspan class="muted" font-size="3"> / {total}</tspan></text>')  # fmt: skip
    side = f"{step.side.upper()} side" + (" (seen from below)" if step.side == "bottom" else "")
    if step.rank >= asm.THT_RANK:
        side = f"THROUGH-HOLE, {side}"
    head_w = text_width(f"Step {step.number}", 5, bold=True) + text_width(f" / {total}", 3, bold=True)
    badge, _ = _badge(x + head_w + 3, y0 + 1.6, side, 2.6)
    out.append(badge)
    y = y0 + 13
    out.append(_text_lines(x, y, _wrap(step.value, 4.4, info_w - 4, bold=True)[:2], 4.4, "head"))
    y += 4.4 * 1.3 * min(2, len(_wrap(step.value, 4.4, info_w - 4, bold=True))) + 0.5
    details = [f"package {step.footprint}", f"qty {len(step.refs)} - {step.category}"]
    if step.mpn and step.mpn != step.value:
        details.insert(1, f"MPN {step.mpn}")
    if step.lcsc:
        details.insert(1, f"LCSC {step.lcsc}")
    lines = [line for d in details for line in _wrap(d, 2.7, info_w - 4)]
    out.append(_text_lines(x, y, lines, 2.7))
    y += len(lines) * 2.7 * 1.3 + 1
    if step.note:
        note = [line for n in step.note.split("; ") for line in _wrap(n, 2.6, info_w - 4)]
        out.append(_text_lines(x, y, note, 2.6, "warn"))
        y += len(note) * 2.6 * 1.3 + 1
    col_w = max(max(text_width(r, 2.8) for r in step.refs) + 6, 16.0)
    cols = max(1, int((info_w - 4) // col_w))
    for k, ref in enumerate(step.refs):
        cx, cy = x + (k % cols) * col_w, y + 1 + (k // cols) * 4.6
        if cy > y0 + h - 2:
            break
        out.append(f'<rect class="box" x="{_f(cx)}" y="{_f(cy - 2.6)}" width="3" height="3"/>'
                   f'<text x="{_f(cx + 4.2)}" y="{_f(cy)}" font-size="2.8">{_esc(ref)}</text>')  # fmt: skip
    return "".join(out)


def pdf_pages(assembly: Assembly, base: Base | None) -> list[str]:
    """Every page of the PDF as a standalone SVG document."""
    pages = title_pages(assembly)
    side_order = list(dict.fromkeys([s.side for s in assembly.steps] + list(asm.SIDES)))
    pages += [overview_page(assembly, side, base) for side in side_order if assembly.side_parts(side)]
    pages += step_panels(assembly, base)
    total = len(pages)
    return [
        _page(body, number=i, total=total, assembly=assembly, title=title) for i, (body, title) in enumerate(pages, 1)
    ]


def write_pdf(assembly: Assembly, base: Base | None, target: Path, work_dir: Path) -> Path:
    """``target`` (PDF) from :func:`pdf_pages`; the page SVGs stay in ``work_dir``."""
    rsvg = shutil.which("rsvg-convert")
    if rsvg is None:
        msg = "rsvg-convert not found (librsvg): needed for the assembly PDF"
        raise AssemblyError(msg)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    files = []
    for i, svg in enumerate(pdf_pages(assembly, base), 1):
        page = work_dir / f"page-{i:03d}.svg"
        page.write_text(svg, encoding="utf-8")
        files.append(str(page))
    target.parent.mkdir(parents=True, exist_ok=True)
    result = proc.run([rsvg, "-f", "pdf", "-o", str(target), *files])
    if result.returncode != 0:
        msg = f"rsvg-convert failed: {result.stderr.strip()}"
        raise AssemblyError(msg)
    return target


# --- HTML --------------------------------------------------------------------------


def _html_view(assembly: Assembly, side: str, base: Base | None) -> str:
    w, h = 270.0, 270.0 * assembly.height / assembly.width + 16
    area = (9.0, 3.0, w - 4, h - 7)
    view = fit_view(side, assembly.outline, area)
    drawing = draw_side(assembly, view, base, (0, 0, w, h), interactive=True, paper="var(--paper)")
    return (
        f'<svg class="board" data-side="{side}" viewBox="0 0 {_f(w)} {_f(h)}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="{side} side">{drawing.svg}</svg>'
    )


def render_html(assembly: Assembly, base: Base | None) -> str:
    """The interactive assembly page: one self-contained HTML file (no network)."""
    parts = {
        p.ref: {"value": p.value, "footprint": p.footprint, "side": p.side, "note": p.note, "mpn": p.mpn,
                "lcsc": p.lcsc}
        for p in assembly.parts
    }  # fmt: skip
    steps = [
        {"n": s.number, "side": s.side, "tht": s.rank >= asm.THT_RANK, "category": s.category, "value": s.value,
         "footprint": s.footprint, "mpn": s.mpn, "lcsc": s.lcsc, "refs": list(s.refs), "note": s.note}
        for s in assembly.steps
    ]  # fmt: skip
    data = {
        "board": assembly.name,
        "revision": assembly.revision,
        "parts": parts,
        "steps": steps,
        "summary": _summary_lines(assembly),
        "warnings": assembly.warnings,
    }
    views = "".join(_html_view(assembly, side, base) for side in asm.SIDES if assembly.side_parts(side))
    rev = f" rev {assembly.revision}" if assembly.revision else ""
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return (
        HTML_TEMPLATE.replace("__TITLE__", _esc(f"{assembly.name}{rev} assembly"))
        .replace("__VIEWS__", views)
        .replace("__DATA__", payload)
        .replace("__FONT__", FONT)
    )


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --paper: #ffffff; --ink: #161616; --muted: #666; --line: #d6d6d6; --pad: #b9b9b9; --edge: #222;
  --body: #8c8c8c; --hl: #ffb300; --hl-line: #c25e00; --done: #d9d9d9; --mark: #d0021b; --row: #fff6dc;
  --chip: #f0f0f0; --ok: #1d7a35; color-scheme: light dark; }
@media (prefers-color-scheme: dark) { :root { --paper: #15171a; --ink: #e8e8e8; --muted: #9a9a9a; --line: #33373c;
  --pad: #5b6066; --edge: #d8d8d8; --body: #80868d; --hl: #ffb300; --hl-line: #ffcf5a; --done: #3a3e44;
  --mark: #ff4d5e; --row: #3a3220; --chip: #25292e; --ok: #4cc26d; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 14px/1.4 __FONT__; }
header { padding: 10px 16px; border-bottom: 1px solid var(--line); }
header h1 { font-size: 19px; margin: 0 0 2px; }
header p { margin: 2px 0; color: var(--muted); font-size: 13px; }
.warn { color: var(--mark); }
main { display: grid; grid-template-columns: minmax(320px, 430px) 1fr; height: calc(100vh - 92px); }
@media (max-width: 860px) { main { grid-template-columns: 1fr; height: auto; } #list { max-height: 50vh; } }
#list { overflow: auto; border-right: 1px solid var(--line); }
#views { overflow: auto; padding: 8px 12px; }
.tabs { display: flex; gap: 6px; margin-bottom: 6px; align-items: center; flex-wrap: wrap; }
.tabs button { font: inherit; padding: 4px 12px; border: 1px solid var(--line); background: var(--chip);
  color: var(--ink); border-radius: 4px; cursor: pointer; }
.tabs button.on { background: var(--ink); color: var(--paper); }
.tabs .hint { color: var(--muted); font-size: 13px; }
.progress { margin-left: auto; color: var(--muted); font-size: 13px; }
svg.board { width: 100%; height: auto; max-height: calc(100vh - 150px); display: none; }
svg.board.on { display: block; }
svg text { font-family: __FONT__; fill: var(--ink); }
.kpads { color: var(--pad); } .kedge { color: var(--edge); }
.body { fill: transparent; stroke: var(--body); stroke-width: 0.16; }
.other .body { stroke-dasharray: 0.6 0.5; opacity: 0.6; }
.tp .body { stroke-dasharray: 0.4 0.4; }
.part { cursor: pointer; }
.part:hover .body { stroke: var(--hl-line); stroke-width: 0.4; }
.part.done .body { fill: var(--done); }
.part.hl .body { fill: var(--hl); fill-opacity: 0.6; stroke: var(--hl-line); stroke-width: 0.45; }
.markpad { fill: var(--mark); fill-opacity: 0.75; }
.mark { stroke: var(--mark); fill: none; stroke-linecap: round; }
.markdot { fill: var(--mark); }
.ref { font-weight: bold; paint-order: stroke; stroke: var(--paper); stroke-width: 0.35; stroke-linejoin: round;
  pointer-events: none; }
.lbl.tp .ref { fill: var(--muted); font-weight: normal; font-style: italic; }
.leader { stroke: var(--muted); stroke-width: 0.16; } .leaderdot { fill: var(--muted); }
.hole { fill: none; stroke: var(--muted); stroke-width: 0.2; }
.dim { stroke: var(--muted); stroke-width: 0.15; fill: none; } .dimtext { fill: var(--muted); }
.halo { paint-order: stroke; stroke: var(--paper); stroke-width: 0.6; }
.side-head { position: sticky; top: 0; background: var(--paper); padding: 6px 12px; font-weight: bold;
  border-bottom: 1px solid var(--line); z-index: 1; }
.step { padding: 6px 12px; border-bottom: 1px solid var(--line); cursor: pointer; }
.step:hover, .step.on { background: var(--row); }
.step.complete .title { color: var(--ok); }
.step .title { font-weight: bold; }
.step .meta { color: var(--muted); font-size: 12px; }
.step .note { color: var(--mark); font-size: 12px; }
.refs { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.refs label { background: var(--chip); border-radius: 3px; padding: 1px 6px 1px 3px; font-size: 13px;
  display: inline-flex; align-items: center; gap: 3px; cursor: pointer; }
.refs label.hover { outline: 2px solid var(--hl-line); }
.refs input { margin: 0; }
#tip { position: fixed; pointer-events: none; background: var(--ink); color: var(--paper); padding: 4px 8px;
  border-radius: 4px; font-size: 13px; display: none; z-index: 5; max-width: 340px; }
.extra { padding: 8px 12px; color: var(--muted); font-size: 13px; }
</style>
</head>
<body>
<header><h1 id="h"></h1><p id="summary"></p><p id="warnings" class="warn"></p></header>
<main>
<div id="list"></div>
<div id="views">
<div class="tabs"><button data-side="top">Top</button><button data-side="bottom">Bottom (seen from below)</button>
<span class="hint" id="hint"></span><span class="progress" id="progress"></span></div>
__VIEWS__
</div>
</main>
<div id="tip"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("data").textContent);
  var KEY = "vibepcb-assembly:" + D.board + ":" + (D.revision || "");
  var checked = {};
  try { checked = JSON.parse(window.localStorage.getItem(KEY) || "{}") || {}; } catch (e) { checked = {}; }
  function save() { try { window.localStorage.setItem(KEY, JSON.stringify(checked)); } catch (e) { /* private mode */ } }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text; return e; }
  document.getElementById("h").textContent = D.board + (D.revision ? " rev " + D.revision : "") + " - assembly";
  document.getElementById("summary").textContent = D.summary.join(" \\u00b7 ");
  document.getElementById("warnings").textContent = D.warnings.map(function (w) { return "CHECK: " + w; }).join("  ");
  var stepOf = {}, rows = {}, chips = {};
  var list = document.getElementById("list"), lastHead = "";
  D.steps.forEach(function (s) {
    var head = s.tht ? "Through-hole parts (last)" : (s.side === "bottom" ? "Bottom side SMD (seen from below)" : "Top side SMD");
    if (head !== lastHead) { list.appendChild(el("div", "side-head", head)); lastHead = head; }
    var row = el("div", "step"); row.dataset.n = s.n;
    row.appendChild(el("div", "title", s.n + ". " + s.value + "  \\u00d7" + s.refs.length));
    var meta = [s.footprint, s.category, s.side + " side"];
    if (s.lcsc) meta.push("LCSC " + s.lcsc);
    if (s.mpn && s.mpn !== s.value) meta.push("MPN " + s.mpn);
    row.appendChild(el("div", "meta", meta.join(" \\u00b7 ")));
    if (s.note) row.appendChild(el("div", "note", s.note));
    var refs = el("div", "refs");
    s.refs.forEach(function (r) {
      stepOf[r] = s;
      var lab = el("label"); var box = el("input"); box.type = "checkbox"; box.checked = !!checked[r];
      box.addEventListener("click", function (ev) { ev.stopPropagation(); });
      box.addEventListener("change", function () { if (box.checked) checked[r] = 1; else delete checked[r];
        save(); refresh(); });
      lab.appendChild(box); lab.appendChild(document.createTextNode(r));
      lab.addEventListener("mouseenter", function () { hoverRef(r, true); });
      lab.addEventListener("mouseleave", function () { hoverRef(r, false); });
      chips[r] = lab; refs.appendChild(lab);
    });
    row.appendChild(refs);
    row.addEventListener("click", function () { select(s.n); });
    rows[s.n] = row; list.appendChild(row);
  });
  var extra = [];
  Object.keys(D.parts).forEach(function (r) { if (!stepOf[r]) extra.push(r); });
  if (extra.length) list.appendChild(el("div", "extra", "Not assembled: " + extra.join(", ")));
  var current = null;
  function show(side) {
    document.querySelectorAll("svg.board").forEach(function (s) { s.classList.toggle("on", s.dataset.side === side); });
    document.querySelectorAll(".tabs button").forEach(function (b) { b.classList.toggle("on", b.dataset.side === side); });
    document.getElementById("hint").textContent = side === "bottom" ?
      "mirrored: the board turned over left-right" : "";
  }
  function partEls(r) { return document.querySelectorAll('[data-ref="' + r.replace(/"/g, "") + '"]'); }
  function select(n) {
    current = n;
    Object.keys(rows).forEach(function (k) { rows[k].classList.toggle("on", +k === n); });
    document.querySelectorAll(".hl").forEach(function (e) { e.classList.remove("hl"); });
    var s = D.steps[n - 1]; if (!s) return;
    s.refs.forEach(function (r) { partEls(r).forEach(function (e) { e.classList.add("hl"); }); });
    show(s.side);
  }
  function refresh() {
    var done = 0, total = 0;
    D.steps.forEach(function (s) {
      var all = true;
      s.refs.forEach(function (r) { total++; if (checked[r]) done++; else all = false;
        partEls(r).forEach(function (e) { if (e.classList.contains("part")) e.classList.toggle("done", !!checked[r]); }); });
      rows[s.n].classList.toggle("complete", all);
    });
    document.getElementById("progress").textContent = done + " / " + total + " soldered";
  }
  var tip = document.getElementById("tip");
  function hoverRef(r, on) {
    if (chips[r]) chips[r].classList.toggle("hover", on);
    partEls(r).forEach(function (e) { if (e.classList.contains("part")) e.querySelector(".body").style.strokeWidth = on ? "0.6" : ""; });
  }
  document.querySelectorAll("svg.board .part").forEach(function (g) {
    var r = g.dataset.ref, p = D.parts[r];
    g.addEventListener("mouseenter", function () {
      hoverRef(r, true);
      var s = stepOf[r];
      tip.textContent = r + ": " + (p ? p.value + " \\u00b7 " + p.footprint : "") + (s ? " \\u00b7 step " + s.n : " \\u00b7 not assembled") +
        (p && p.note ? " \\u00b7 " + p.note : "");
      tip.style.display = "block";
      if (s && rows[s.n]) rows[s.n].scrollIntoView({block: "nearest"});
    });
    g.addEventListener("mousemove", function (ev) { tip.style.left = (ev.clientX + 14) + "px"; tip.style.top = (ev.clientY + 12) + "px"; });
    g.addEventListener("mouseleave", function () { hoverRef(r, false); tip.style.display = "none"; });
    g.addEventListener("click", function () { var s = stepOf[r]; if (s) { select(s.n); rows[s.n].scrollIntoView({block: "center"}); } });
  });
  document.querySelectorAll(".tabs button").forEach(function (b) { b.addEventListener("click", function () { show(b.dataset.side); }); });
  refresh();
  if (D.steps.length) select(D.steps[0].n); else show("top");
})();
</script>
</body>
</html>
"""


def write(
    pcb: Path, out_dir: Path, work_dir: Path, *, circuit_json: Path | None = None, bom_csv: Path | None = None
) -> list[Path]:
    """``<stem>-assembly.pdf`` and ``<stem>-assembly.html`` of the board ``pcb`` into ``out_dir``
    (page SVGs in ``work_dir``)."""
    assembly = asm.collect(pcb, pcb.with_suffix(".kicad_sch"), circuit_json=circuit_json, bom_csv=bom_csv)
    base = kicad_base(pcb)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = write_pdf(assembly, base, out_dir / f"{pcb.stem}-assembly.pdf", work_dir)
    page = out_dir / f"{pcb.stem}-assembly.html"
    page.write_text(render_html(assembly, base), encoding="utf-8")
    return [pdf, page]
