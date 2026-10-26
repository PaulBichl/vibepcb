"""Where a board's DRC errors are: the violations grouped into spots, each with its nets and the pads around it.

``check.txt`` lists errors one by one; on a busy board they are a few crowded spots. ``group`` clusters the
error positions (single linkage), ``describe`` names what meets there, so the agent knows which part to move
(or which area to ``inspect``) without reading a hundred lines.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vibepcb import routes, tracks

NET_RE = re.compile(r"\[([^\]]*)\]")


@dataclass
class Error:
    kind: str
    pos: tracks.Point
    nets: tuple[str, ...]


@dataclass
class Spot:
    errors: list[Error] = field(default_factory=list)

    @property
    def box(self) -> tuple[float, float, float, float]:
        xs = [e.pos[0] for e in self.errors]
        ys = [e.pos[1] for e in self.errors]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def centre(self) -> tracks.Point:
        x0, y0, x1, y1 = self.box
        return (x0 + x1) / 2, (y0 + y1) / 2


def drc_errors(drc: dict[str, Any]) -> list[Error]:
    """The error-severity violations of a kicad-cli DRC report, each at the midpoint of its items."""
    out = []
    for v in drc.get("violations", []):
        if v.get("severity") != "error" or not v.get("items"):
            continue
        items = v["items"]
        x = sum(i["pos"]["x"] for i in items) / len(items)
        y = sum(i["pos"]["y"] for i in items) / len(items)
        nets = tuple(sorted({m for i in items for m in NET_RE.findall(i.get("description", "")) if m}))
        out.append(Error(v["type"], (x, y), nets))
    return out


def group(errors: list[Error], radius: float = 1.5) -> list[Spot]:
    """Single-linkage clusters: errors closer than ``radius`` mm share a spot. Most errors first."""
    parent = list(range(len(errors)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(errors):
        for j in range(i):
            if math.dist(a.pos, errors[j].pos) < radius:
                parent[find(i)] = find(j)
    spots: dict[int, Spot] = {}
    for i, e in enumerate(errors):
        spots.setdefault(find(i), Spot()).errors.append(e)
    return sorted(spots.values(), key=lambda s: (-len(s.errors), s.centre))


def describe(spot: Spot, pads: list[routes.Pad], origin: tracks.Point | None = None, margin: float = 1.0) -> list[str]:
    """Lines for one spot: where (board mm, and TSX mm from ``origin`` = board centre), kinds, nets, pads nearby."""
    x0, y0, x1, y1 = spot.box
    cx, cy = spot.centre
    where = f"({x0:.1f},{y0:.1f})-({x1:.1f},{y1:.1f})"
    if origin is not None:
        where += f"  tsx ({cx - origin[0]:.1f}, {origin[1] - cy:.1f})"
    kinds = Counter(e.kind for e in spot.errors)
    nets = Counter(n for e in spot.errors for n in e.nets)
    near = [p for p in pads if x0 - margin <= p.centre[0] <= x1 + margin and y0 - margin <= p.centre[1] <= y1 + margin]
    refs = Counter(p.ref for p in near)
    return [
        f"{len(spot.errors)} error(s) {where}",
        "  kinds: " + ", ".join(f"{k} x{n}" for k, n in kinds.most_common()),
        "  nets:  " + ", ".join(f"{k} x{n}" for k, n in nets.most_common(8)),
        "  parts: " + (", ".join(f"{r} ({n} pads)" for r, n in refs.most_common(6)) or "-"),
        f"  inspect: --area {x0 - 2:.1f} {y0 - 2:.1f} {x1 + 2:.1f} {y1 + 2:.1f}",
    ]
