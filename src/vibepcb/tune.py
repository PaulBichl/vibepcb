"""Length tuning: meanders on the short nets of a length group (``[length_group.<name>]`` in vibepcb.toml).

KiCad's DRC checks a group's skew (longest - shortest net length <= ``max_skew``); nothing in the routing
makes the lengths match. Here every net shorter than the group's target gets bumps on its longest straight
track pieces until it is long enough:

    ___      ___
   |   |    |   |        a bump: up, across, down, every corner cut at 45 degrees (no bend sharper than
 __|   |____|   |__      135 degrees), height solved so the added length comes out exact

Each new piece keeps the board clearance to other nets' copper (:mod:`vibepcb.drc`); a bump that does not fit
goes to the other side, then lower bumps, then the next straight piece. Target: ``tune_to`` (mm, the spread
the group is tuned to) or, without it, only the nets that break ``max_skew`` are brought to 90 % of it.
"""

from __future__ import annotations

import copy
import fnmatch
import itertools
import math
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibepcb import board as board_mod
from vibepcb import drc, fixes, sexpr, tracks
from vibepcb.sexpr import Sym

if TYPE_CHECKING:
    from vibepcb.fixes import KicadProject
    from vibepcb.sexpr import Node

CHAMFER_MM = 0.15  # the 45-degree cut at each corner of a bump
MIN_HEIGHT_MM = 0.5  # lower bumps are not worth their corners
MAX_HEIGHT_MM = 2.0
END_MARGIN_MM = 0.6  # bumps keep away from a piece's ends (pads, vias, bends)
DEFAULT_SPREAD = 0.9  # without tune_to: violators are brought to this share of max_skew


@dataclass
class Bumps:
    """Meander on a straight piece a-b: ``n`` bumps of height ``h`` on side ``side`` (+1 left, -1 right)."""

    n: int
    h: float
    side: int
    gap: float  # flat run between bumps

    def width(self) -> float:
        return 4 * CHAMFER_MM + self.gap  # base of one bump: 4 chamfers + its flat top (= gap)

    def extra(self) -> float:
        return self.n * bump_extra(self.h)


def bump_extra(h: float) -> float:
    """Length one bump of height h adds over its base (4 chamfers of c, 2 legs of h - 2c)."""
    c = CHAMFER_MM
    return 4 * c * math.sqrt(2) + 2 * (h - 2 * c) - 4 * c


def bump_height(extra: float) -> float:
    """The bump height that adds ``extra`` (inverse of :func:`bump_extra`)."""
    c = CHAMFER_MM
    return (extra - 4 * c * math.sqrt(2) + 4 * c) / 2 + 2 * c


def meander(a: tracks.Point, b: tracks.Point, bumps: Bumps) -> list[tracks.Point]:
    """The polyline replacing a-b: flat, the bumps centred on the piece, flat."""
    length = math.dist(a, b)
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    vx, vy = -uy * bumps.side, ux * bumps.side
    c, h, top = CHAMFER_MM, bumps.h, bumps.gap
    pitch = bumps.width() + bumps.gap
    start = (length - (bumps.n * pitch - bumps.gap)) / 2

    def at(u: float, v: float) -> tracks.Point:
        return a[0] + ux * u + vx * v, a[1] + uy * u + vy * v

    pts = [a]
    for k in range(bumps.n):
        u0 = start + k * pitch
        for du, dv in (
            (0, 0),
            (c, c),
            (c, h - c),
            (2 * c, h),
            (2 * c + top, h),
            (3 * c + top, h - c),
            (3 * c + top, c),
            (4 * c + top, 0),
        ):
            pts.append(at(u0 + du, dv))
    pts.append(b)
    return pts


def net_lengths(copper: fixes._Copper) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in copper.segs:
        out[s.net] = out.get(s.net, 0.0) + math.dist(s.a, s.b)
    return out


def tune(project: KicadProject) -> list[str]:
    """Meanders for every length group of the board config; one line per tuned net (or one that could not be)."""
    groups = project.config.get("length_group", {})
    if not groups:
        return []
    model = board_mod.Board.from_project(project)
    lengths = net_lengths(model.copper)
    changes = []
    for name, group in groups.items():
        members = sorted(n for n in lengths if any(fnmatch.fnmatchcase(n, p) for p in group["nets"]))
        if len(members) < 2:
            continue
        longest = max(lengths[n] for n in members)
        spread = float(group.get("tune_to", float(group["max_skew"]) * DEFAULT_SPREAD))
        if "tune_to" not in group and longest - min(lengths[n] for n in members) <= float(group["max_skew"]):
            continue  # inside the rule: nothing to do
        for net in members:
            need = longest - spread - lengths[net]
            if need <= 0:
                continue
            added = _tune_net(project, model, net, need)
            if added >= need - 0.01:
                changes.append(f"{name}: {net} +{added:.2f} mm (to {lengths[net] + added:.2f}, longest {longest:.2f})")
            else:
                changes.append(f"{name}: {net} needs +{need:.2f} mm, room for +{added:.2f} only")
    return changes


def _tune_net(project: KicadProject, model: board_mod.Board, net: str, need: float) -> float:
    """Add up to ``need`` mm to the net with bumps on its longest straight pieces; returns what was added."""
    added = 0.0
    pieces = sorted((s for s in model.copper.segs if s.net == net), key=lambda s: -math.dist(s.a, s.b))
    for seg in pieces:
        if need - added <= 0.01:
            break
        found = _fit(model, seg, need - added)
        if found is None:
            continue
        pts, extra = found
        _replace(project, seg, pts)
        for p, q in itertools.pairwise(pts):
            model.add_track(net, seg.layer, p, q, seg.width)
        added += extra
    return added


def _fit(model: board_mod.Board, seg: tracks.Seg, need: float) -> tuple[list[tracks.Point], float] | None:
    """The meander on ``seg`` adding as much of ``need`` as fits (exactly ``need`` if possible)."""
    usable = math.dist(seg.a, seg.b) - 2 * END_MARGIN_MM
    gap = max(seg.width + model.rules.clearance(seg.net, seg.net), 2 * seg.width) + 0.05  # flat run between legs
    pitch = 4 * CHAMFER_MM + 2 * gap
    if usable < pitch:
        return None
    max_n = int((usable + gap) // pitch)
    best: tuple[list[tracks.Point], float] | None = None
    for n in range(1, max_n + 1):  # fewest bumps first: low bumps where one or two do
        h = bump_height(need / n)
        if h > MAX_HEIGHT_MM:
            continue
        h = max(h, MIN_HEIGHT_MM)
        for side in (1, -1):
            bumps = Bumps(n, h, side, gap)
            pts = meander(seg.a, seg.b, bumps)
            if _clear(model, seg, pts):
                return pts, bumps.extra()
    # not all of it: the most that fits (max height, as many bumps as fit)
    for n in range(max_n, 0, -1):
        for side in (1, -1):
            bumps = Bumps(n, MAX_HEIGHT_MM, side, gap)
            pts = meander(seg.a, seg.b, bumps)
            if _clear(model, seg, pts):
                best = (pts, bumps.extra())
                break
        if best:
            break
    return best


def _clear(model: board_mod.Board, seg: tracks.Seg, pts: list[tracks.Point]) -> bool:
    return all(
        drc.blocking_track(model, seg.net, seg.layer, p, q, seg.width) is None for p, q in itertools.pairwise(pts)
    )


def _replace(project: KicadProject, seg: tracks.Seg, pts: list[tracks.Point]) -> None:
    """The segment node of ``seg`` becomes one node per meander piece (same width, layer, net; content UUIDs)."""
    node = fixes._source(seg)
    index = next(i for i, n in enumerate(project.pcb) if n is node)
    new: list[list[Node]] = []
    for p, q in itertools.pairwise(pts):
        piece = copy.deepcopy(node)
        for key, xy in (("start", p), ("end", q)):
            child = sexpr.child(piece, key)
            if child is not None:
                child[1:3] = [Sym(fixes._mm(xy[0])), Sym(fixes._mm(xy[1]))]
        uid = sexpr.child(piece, "uuid")
        if uid is not None:
            key = f"tune:{seg.net}:{fixes._mm(p[0])},{fixes._mm(p[1])}:{fixes._mm(q[0])},{fixes._mm(q[1])}"
            new_uid = str(uuid.uuid5(uuid.NAMESPACE_OID, key))
            uid[1] = Sym(new_uid) if isinstance(uid[1], Sym) else new_uid
        new.append(piece)
    project.pcb[index : index + 1] = new
