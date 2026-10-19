"""Ratsnest: the connections a board's copper does not make yet, as straight pad-to-pad links.

Per net, the pads its tracks and vias already join (:func:`vibepcb.tracks.pad_groups`) form groups; the
groups are linked by a minimum spanning tree over pad centres (Kruskal on pad pairs of different groups,
so each link joins the two nearest pads of two groups). Nets in ``[planes] nets`` of the board config
and nets with a copper pour (zone) are skipped: the plane / pour joins them.

On an unrouted board (``.work/unrouted.kicad_pcb`` from ``fix`` in manual mode, or a fresh placement) the
total link length and the number of links of different nets crossing each other are the placement score:
fewer crossings, fewer layer changes and detours for the router.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vibepcb import fixes, routes, tracks

if TYPE_CHECKING:
    from vibepcb.fixes import KicadProject

CELL_MM = 10.0  # crossings are also counted per cell of this size (where the placement is tangled)


@dataclass(frozen=True)
class Link:
    """One unrouted connection: the straight line between two pads of a net."""

    net: str
    a: str  # pad label REF.PAD
    b: str
    pa: tracks.Point
    pb: tracks.Point

    @property
    def length(self) -> float:
        return math.dist(self.pa, self.pb)


def unrouted(project: KicadProject) -> list[Link]:
    """The links still missing per net (a minimum spanning tree between the groups its copper joins)."""
    copper = fixes._copper(project.pcb)
    skip = set(project.config.get("planes", {}).get("nets", [])) | {net for net, _ in copper.zones}
    by_net: dict[str, list[routes.Pad]] = {}
    for p in routes.board_pads(project):
        if p.net and p.net not in skip:
            by_net.setdefault(p.net, []).append(p)
    out: list[Link] = []
    for net in sorted(by_net):
        members = by_net[net]
        if len(members) < 2:
            continue
        out += _spanning_links(net, members, _groups(copper, net, len(members)))
    return out


def _groups(copper: fixes._Copper, net: str, count: int) -> list[int]:
    """Pad index -> group id (pads the net's tracks and vias join share one)."""
    obstacles = copper.pads_by_net.get(net, [])  # same order as routes.board_pads
    if len(obstacles) != count:
        return list(range(count))
    vias = [(c, r) for _, c, r, n in copper.vias if n == net]
    segs = [s for s in copper.segs if s.net == net]
    group_of = list(range(count))
    for g, grp in enumerate(tracks.pad_groups(obstacles, vias, segs)):
        for i in grp:
            group_of[i] = g
    return group_of


def _spanning_links(net: str, members: list[routes.Pad], group_of: list[int]) -> list[Link]:
    """Kruskal over pad pairs of different groups: each accepted pair joins two groups."""
    parent = {g: g for g in group_of}

    def find(g: int) -> int:
        while parent[g] != g:
            parent[g] = parent[parent[g]]
            g = parent[g]
        return g

    pairs = sorted(
        (math.dist(members[i].centre, members[j].centre), i, j)
        for i in range(len(members))
        for j in range(i + 1, len(members))
        if group_of[i] != group_of[j]
    )
    out = []
    for _, i, j in pairs:
        gi, gj = find(group_of[i]), find(group_of[j])
        if gi == gj:
            continue
        parent[gi] = gj
        a, b = members[i], members[j]
        out.append(Link(net, a.label, b.label, a.centre, b.centre))
    return out


def crossing_points(links: list[Link]) -> list[tracks.Point]:
    """Where the straight links of different nets cross (each pair once)."""
    boxed = sorted(
        (min(k.pa[0], k.pb[0]), max(k.pa[0], k.pb[0]), min(k.pa[1], k.pb[1]), max(k.pa[1], k.pb[1]), k) for k in links
    )
    out = []
    for i, (x0, x1, y0, y1, k) in enumerate(boxed):
        for u0, _, v0, v1, m in boxed[i + 1 :]:
            if u0 > x1:
                break  # sorted by left edge: no later link reaches back
            if m.net == k.net or v0 > y1 or v1 < y0:
                continue
            if (p := tracks.crossing(k.pa, k.pb, m.pa, m.pb)) is not None:
                out.append(p)
    return out


def crossings(links: list[Link]) -> int:
    """Number of crossings between the straight links of different nets."""
    return len(crossing_points(links))


def cell_crossings(links: list[Link], cell: float = CELL_MM) -> Counter[tuple[int, int]]:
    """Crossings per grid cell (cell index = floor(coordinate / cell))."""
    return Counter((math.floor(x / cell), math.floor(y / cell)) for x, y in crossing_points(links))


def score(project: KicadProject, worst: int = 5) -> dict[str, Any]:
    """Placement metric: link count, total length (mm), crossings and the ``worst`` cells
    (``((x0, y0), count)``: the cell's top-left corner in mm)."""
    links = unrouted(project)
    cells = cell_crossings(links)
    return {
        "links": len(links),
        "length": sum(k.length for k in links),
        "crossings": sum(cells.values()),
        "cells": [((cx * CELL_MM, cy * CELL_MM), n) for (cx, cy), n in cells.most_common(worst)],
    }
