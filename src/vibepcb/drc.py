"""Incremental clearance checks on the routing model (:mod:`vibepcb.board`).

The same numbers KiCad's DRC uses (net-class clearance = the larger of the two nets', board-edge
clearance, hole-to-hole gap, keepouts), so a route that fits here passes KiCad's check. A check
returns the blocking item, ``None`` when the copper fits. ``ignore``: nets whose *routed* copper (tracks and
vias the router added, not pads) is treated as absent: the router may cross it and rip those nets up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibepcb import fixes, tracks

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vibepcb.board import Board

EDGE_NET = "<edge>"
HOLE_NET = "<hole>"
NO_NETS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Blocker:
    """What a new piece of copper runs into."""

    kind: str  # "copper" | "keepout" | "edge" | "hole"
    net: str
    at: tracks.Point  # a point of the blocking item
    gap: float  # edge-to-edge distance found (negative: overlap)
    needed: float  # clearance required

    def __str__(self) -> str:
        what = {"copper": f"net {self.net}", "keepout": "a keepout", "edge": "the board edge", "hole": "a hole"}
        return f"{what[self.kind]} near ({self.at[0]:.2f}, {self.at[1]:.2f}): gap {self.gap:.3f} < {self.needed:.3f} mm"


def _margin(board: Board, size: float) -> float:
    """Search radius around new copper of the given size: the largest clearance there is."""
    rules = board.rules
    return size + max([rules.default.clearance, rules.edge_clearance, *(c.clearance for c in rules.classes.values())])


def _skipped(o: tracks.Obstacle, net: str, ignore: frozenset[str]) -> bool:
    return o.net == net or (o.routed and o.net in ignore)


def scan_track(
    board: Board, net: str, layer: str, a: tracks.Point, b: tracks.Point, width: float, ignore: frozenset[str] = NO_NETS
) -> Iterator[Blocker]:
    """Everything a track of ``net`` from a to b on ``layer`` violates."""
    m = _margin(board, width / 2)
    near = board.near(min(a[0], b[0]) - m, min(a[1], b[1]) - m, max(a[0], b[0]) + m, max(a[1], b[1]) + m)
    for o in near:
        if _skipped(o, net, ignore) or layer not in o.layers:
            continue
        keepout = o.net == fixes.KEEPOUT_NET
        need = 0.0 if keepout else board.rules.clearance(net, o.net)
        gap = o.distance(a, b) - width / 2
        if gap < need - tracks.EPS / 10:
            yield Blocker("keepout" if keepout else "copper", o.net, o.centre, gap, need)
    need = board.rules.edge_clearance
    for e in board.edges:
        gap = e.distance(a, b) - width / 2
        if gap < need - tracks.EPS / 10:
            yield Blocker("edge", EDGE_NET, e.centre, gap, need)


def scan_via(
    board: Board, net: str, centre: tracks.Point, diameter: float, drill: float, ignore: frozenset[str] = NO_NETS
) -> Iterator[Blocker]:
    """Everything a via of ``net`` at ``centre`` violates (it spans every layer)."""
    m = _margin(board, diameter / 2)
    near = board.near(centre[0] - m, centre[1] - m, centre[0] + m, centre[1] + m)
    for o in near:
        if _skipped(o, net, ignore):
            continue
        keepout = o.net == fixes.KEEPOUT_NET
        need = 0.0 if keepout else board.rules.clearance(net, o.net)
        gap = o.distance(centre, centre) - diameter / 2
        if gap < need - tracks.EPS / 10:
            yield Blocker("keepout" if keepout else "copper", o.net, o.centre, gap, need)
    need = board.rules.edge_clearance
    for e in board.edges:
        gap = e.distance(centre, centre) - diameter / 2
        if gap < need - tracks.EPS / 10:
            yield Blocker("edge", EDGE_NET, e.centre, gap, need)
    need = board.rules.hole_to_hole
    for h in board.holes:
        if h.net in ignore and h.routed:
            continue
        gap = math.dist(centre, h.centre) - drill / 2 - h.radius
        if gap < need - tracks.EPS / 10 and not (h.net == net and math.dist(centre, h.centre) < tracks.EPS):
            yield Blocker("hole", h.net, h.centre, gap, need)


def blocking_track(
    board: Board, net: str, layer: str, a: tracks.Point, b: tracks.Point, width: float, ignore: frozenset[str] = NO_NETS
) -> Blocker | None:
    """The first thing a track violates, else ``None``."""
    return next(scan_track(board, net, layer, a, b, width, ignore), None)


def blocking_via(
    board: Board, net: str, centre: tracks.Point, diameter: float, drill: float, ignore: frozenset[str] = NO_NETS
) -> Blocker | None:
    """The first thing a via violates, else ``None``."""
    return next(scan_via(board, net, centre, diameter, drill, ignore), None)


def fits_track(board: Board, net: str, layer: str, a: tracks.Point, b: tracks.Point, width: float) -> bool:
    return blocking_track(board, net, layer, a, b, width) is None


def fits_via(board: Board, net: str, centre: tracks.Point, diameter: float, drill: float) -> bool:
    return blocking_via(board, net, centre, diameter, drill) is None
