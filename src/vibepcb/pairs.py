"""Coupled differential pairs: both tracks of a pair run side by side at a constant gap.

The pair is routed as one wide track along its centre line (width 2w + gap, the partner net counting as the
same net), and the centre line is then split into the two tracks, offset by (w + gap) / 2 to either side with
mitred 45-degree corners. Only the short pieces from each pad to its track run uncoupled (``[diff_pair]
max_uncoupled`` in vibepcb.toml is KiCad's limit for them). Which offset is P is chosen so the pair does not
have to cross over; the length difference of the two tracks (inner and outer side of the corners) is reported.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb import drc, router, tracks

if TYPE_CHECKING:
    from vibepcb.board import Board

CENTRE_SLACK_MM = 0.05  # the centre line is a little wider: the mitred outer corners stay inside its clearance


@dataclass
class End:
    """One pad of the pair: where it is, its layers, the point its track leaves from (outside the part)."""

    centre: tracks.Point
    layers: frozenset[str]
    launch: tracks.Point


@dataclass
class PairRoute:
    routes: list[router.Route] = field(default_factory=list)  # coupled P, coupled N, then the pad links
    uncoupled: float = 0.0  # longest pad-link length of either net, both ends together
    skew: float = 0.0  # length P - length N


def offset(points: list[tracks.Point], d: float) -> list[tracks.Point]:
    """The polyline moved sideways by d (positive: to the left of the direction of travel), mitred corners."""
    out = []
    for i, p in enumerate(points):
        normals = []
        for seg in ([points[i - 1], p] if i > 0 else None, [p, points[i + 1]] if i < len(points) - 1 else None):
            if seg is None:
                continue
            (ax, ay), (bx, by) = seg
            n = math.hypot(bx - ax, by - ay)
            normals.append((-(by - ay) / n, (bx - ax) / n))
        mx, my = sum(n[0] for n in normals), sum(n[1] for n in normals)
        m = math.hypot(mx, my)
        mx, my = mx / m, my / m
        cos_half = mx * normals[0][0] + my * normals[0][1]  # the miter: d / cos(half the turn)
        out.append((p[0] + mx * d / cos_half, p[1] + my * d / cos_half))
    return out


def _octilinear(a: tracks.Point, b: tracks.Point) -> bool:
    """a-b runs in one of the 8 grid directions (horizontal, vertical, 45 degrees)."""
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return dx < tracks.EPS or dy < tracks.EPS or abs(dx - dy) < tracks.EPS


def route_pair(
    board: Board,
    nets: tuple[str, str],
    starts: tuple[End, End],
    goals: tuple[End, End],
    width: float,
    gap: float,
    layers: list[str],
) -> PairRoute:
    """Route the pair P / N from ``starts`` to ``goals`` (each (P end, N end)) coupled; raises RouteFailure."""
    p_net, n_net = nets
    s = ((starts[0].launch[0] + starts[1].launch[0]) / 2, (starts[0].launch[1] + starts[1].launch[1]) / 2)
    g = ((goals[0].launch[0] + goals[1].launch[0]) / 2, (goals[0].launch[1] + goals[1].launch[1]) / 2)
    common = [layer for layer in layers if all(layer in e.layers for e in (*starts, *goals))]
    if not common:
        msg = "the pair's pads share no routing layer"
        raise router.RouteFailure(msg)
    board.aliases[p_net] = frozenset({n_net})
    try:
        centre = router.route(board, p_net, s, g, router.Spec(common[:1], 2 * width + gap + CENTRE_SLACK_MM, None))
    finally:
        del board.aliases[p_net]
    layer = centre.pieces[0][0]
    line = [centre.pieces[0][1], *(b for _, _, b in centre.pieces)]
    # the search's last piece closes from the grid to the goal at any angle: offset with a miter, a short
    # odd piece throws the two tracks into each other. It goes; the pad links cover the gap
    while len(line) > 2 and not _octilinear(line[-2], line[-1]):
        line.pop()
    half = (width + gap) / 2
    left, right = offset(line, half), offset(line, -half)
    cost_left = math.dist(left[0], starts[0].centre) + math.dist(left[-1], goals[0].centre)
    cost_right = math.dist(right[0], starts[0].centre) + math.dist(right[-1], goals[0].centre)
    p_line, n_line = (left, right) if cost_left <= cost_right else (right, left)
    result = PairRoute()
    for net, pts in ((p_net, p_line), (n_net, n_line)):
        r = router.Route(net, width, [(layer, a, b) for a, b in itertools.pairwise(pts)])
        for _, a, b in r.pieces:
            if (blocker := drc.blocking_track(board, net, layer, a, b, width)) is not None:
                msg = "coupled track does not fit"
                raise router.RouteFailure(msg, blocker)
        r.commit(board)
        result.routes.append(r)
    links = {p_net: 0.0, n_net: 0.0}
    for net, end, pts, k in (
        (p_net, starts[0], p_line, 0),
        (n_net, starts[1], n_line, 0),
        (p_net, goals[0], p_line, -1),
        (n_net, goals[1], n_line, -1),
    ):
        if math.dist(end.centre, pts[k]) < tracks.EPS:
            continue
        spec = router.Spec([layer], width, None)
        link = router.route(board, net, end.centre, pts[k], spec, start_layers=[layer], goal_layers=[layer])
        link.commit(board)
        result.routes.append(link)
        links[net] += link.length
    result.uncoupled = max(links.values())
    lengths = {net: sum(r.length for r in result.routes if r.net == net) for net in nets}
    result.skew = lengths[p_net] - lengths[n_net]
    return result
