"""The copper model of a board for the track fixes (segments, vias, pads, zones, keepouts)."""

from __future__ import annotations

import dataclasses
import functools
import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.kicad.fixes.base import COPPER_LAYERS, _copper_layers, _fp_to_board, _layer, _net_names, _net_of, _num
from vibepcb.kicad.fixes.references import _graphic_points, _pad_corners
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibepcb.core.sexpr import Node


@dataclass
class _Copper:
    names: dict[str, str]
    segs: list[tracks.Seg]
    vias: list[tuple[list[Node], tracks.Point, float, str]]  # node, centre, radius, net
    pads: list[tracks.Obstacle]
    rect_pads: set[int]  # indices of pads whose copper is their bounding box (rect / roundrect)
    edges: list[tracks.Obstacle]
    zones: dict[tuple[str, str], list[list[tracks.Point]]]  # (net, layer) -> polygons
    keepouts: list[tracks.Obstacle] = field(default_factory=list)  # areas tracks may not enter
    no_pour: dict[str, list[list[tracks.Point]]] = field(default_factory=dict)  # layer -> keepouts without pour

    def poured(self, q: tracks.Point, net: str, layer: str) -> bool:
        """``q`` lies in a zone outline of ``net`` on ``layer`` outside every pour keepout there (filled or not)."""
        return any(tracks.point_in_polygon(q, poly) for poly in self.zones.get((net, layer), [])) and not any(
            tracks.point_in_polygon(q, poly) for poly in self.no_pour.get(layer, [])
        )

    def obstacles(self) -> list[tracks.Obstacle]:
        out = [tracks.Obstacle(s.net, frozenset({s.layer}), (s.a, s.b), s.width / 2) for s in self.segs]
        out += [tracks.Obstacle(net, frozenset(COPPER_LAYERS), (c,), r) for _, c, r, net in self.vias]
        return out + self.pads + self.keepouts

    @functools.cached_property
    def pads_by_net(self) -> dict[str, list[tracks.Obstacle]]:
        out: dict[str, list[tracks.Obstacle]] = {}
        for o in self.pads:
            out.setdefault(o.net, []).append(o)
        return out

    @functools.cached_property
    def pad_index_by_net(self) -> dict[str, tracks.BoxIndex]:
        return {net: tracks.BoxIndex([o.box for o in pads]) for net, pads in self.pads_by_net.items()}

    @functools.cached_property
    def vias_by_net(self) -> dict[str, list[tuple[tracks.Point, float]]]:
        out: dict[str, list[tuple[tracks.Point, float]]] = {}
        for _, c, r, net in self.vias:
            out.setdefault(net, []).append((c, r))
        return out

    @functools.cached_property
    def via_index_by_net(self) -> dict[str, tracks.BoxIndex]:
        """Per net, the vias' copper boxes (same order as ``vias_by_net``): a point tests only the vias around it."""
        return {
            net: tracks.BoxIndex([(c[0] - r, c[1] - r, c[0] + r, c[1] + r) for c, r in vias])
            for net, vias in self.vias_by_net.items()
        }

    def _vias_at(self, p: tracks.Point, net: str) -> list[tuple[tracks.Point, float]]:
        index = self.via_index_by_net.get(net)
        if index is None:
            return []
        vias = self.vias_by_net[net]
        e = tracks.EPS
        return [vias[i] for i in index.overlapping(p[0] - e, p[1] - e, p[0] + e, p[1] + e)]

    @functools.cached_property
    def segs_by_net(self) -> dict[str, list[tracks.Seg]]:
        out: dict[str, list[tracks.Seg]] = {}
        for s in self.segs:
            out.setdefault(s.net, []).append(s)
        return out

    def on_pad_or_via(self, p: tracks.Point, net: str, layer: str) -> bool:
        """Whether a point (a polyline node) lies on a pad or via of its net (no width involved)."""
        if any(math.dist(p, c) <= r + tracks.EPS for c, r in self._vias_at(p, net)):
            return True
        return any(layer in o.layers and o.distance(p, p) <= tracks.EPS for o in self.pads_by_net.get(net, []))

    def attached(self, p: tracks.Point, s: tracks.Seg) -> bool:
        """Whether track end p of s connects to a pad or via the way KiCad sees it.

        A pad takes a track end whose end cap reaches its copper (:func:`tracks.pad_reach`); a via a track end
        inside it (see :func:`tracks.via_takes`).
        """
        if any(math.dist(p, c) <= r - tracks.EPS for c, r in self._vias_at(p, s.net)):
            return True
        index = self.pad_index_by_net.get(s.net)
        if index is None:
            return False
        pads = self.pads_by_net[s.net]
        # a point outside a pad's bounding box plus the track's reach (its end cap) never connects to the pad
        e = tracks.pad_reach(s.width)
        return any(
            s.layer in pads[i].layers and pads[i].distance(p, p) <= e
            for i in index.overlapping(p[0] - e, p[1] - e, p[0] + e, p[1] + e)
        )

    def same_anchor(self, p: tracks.Point, q: tracks.Point, net: str, layer: str) -> bool:
        """Whether p and q both lie on one pad or via of the net (on that layer)."""
        if any(math.dist(p, c) <= r and math.dist(q, c) <= r for c, r in self.vias_by_net.get(net, [])):
            return True
        e = tracks.ON_EDGE_MM
        return any(
            layer in o.layers and o.distance(p, p) <= e and o.distance(q, q) <= e for o in self.pads_by_net.get(net, [])
        )

    def in_zone(self, p: tracks.Point, net: str, layer: str) -> bool:
        return any(tracks.point_in_polygon(p, poly) for poly in self.zones.get((net, layer), []))


def _zone_polygons(zone: list[Node], *, filled: bool) -> list[tuple[str, list[tracks.Point]]]:
    """(layer, polygon) of a zone: its fill (``filled``) or its outline on each of its layers."""
    out = []
    if filled:
        for poly in sexpr.children(zone, "filled_polygon"):
            pts = [(_num(xy, 1), _num(xy, 2)) for xy in sexpr.walk(poly) if sexpr.head(xy) == "xy"]
            out.append((_layer(poly), pts))
        return out
    outline = sexpr.child(zone, "polygon")
    pts = [(_num(xy, 1), _num(xy, 2)) for xy in sexpr.walk(outline or []) if sexpr.head(xy) == "xy"]
    return [(layer, pts) for layer in _copper_layers(zone)] if pts else []


KEEPOUT_NET = "<keepout>"  # pseudo net of keepout areas: other copper to every real net


KEEPOUT_VIA_NET = "<via-keepout>"  # keepout areas that only forbid vias


KEEPOUT_INSET_MM = 0.09  # <= any board clearance (fab minimum): copper never ends up inside the keepout


def _segments(pcb: list[Node], names: dict[str, str]) -> list[tracks.Seg]:
    return [
        tracks.Seg(
            (_num(sexpr.child(s, "start"), 1), _num(sexpr.child(s, "start"), 2)),
            (_num(sexpr.child(s, "end"), 1), _num(sexpr.child(s, "end"), 2)),
            _num(sexpr.child(s, "width"), 1),
            _layer(s),
            _net_of(s, names),
            s,
        )
        for s in sexpr.children(pcb, "segment")
    ]


def _with_new_segments(copper: _Copper, pcb: list[Node]) -> _Copper:
    """``copper`` after only track segments changed: pads, vias, outline, zones and keepouts are kept, the
    segments are read again (rebuilding the whole model after every net made `check` take many minutes)."""
    return dataclasses.replace(copper, segs=_segments(pcb, copper.names))


def _pad_obstacle(
    pad: list[Node], to_board: Callable[[float, float], tuple[float, float]], net: str, layers: frozenset[str]
) -> tracks.Obstacle:
    """A pad's copper as KiCad sees it: circles and ovals round, rounded rectangles with their corner radius
    (``roundrect_rratio`` x the short side), everything else (rect, custom) its bounding box.

    The box alone overstated a rounded pad's corners: a track end 0.04 mm off a roundrect pad's box corner
    counted as connected, KiCad (measuring the real corner, 0.1 mm away) did not (bench-psu rev C, INA226).
    """
    shape = str(pad[3])
    size = sexpr.child(pad, "size")
    w, h = _num(size, 1), _num(size, 2)
    if sexpr.child(pad, "primitives") is None and shape in ("circle", "oval", "roundrect") and w > 0 and h > 0:
        pad_at = sexpr.child(pad, "at")
        angle = math.radians(_num(pad_at, 3))
        px, py = to_board(_num(pad_at, 1), _num(pad_at, 2))

        def board(u: float, v: float) -> tuple[float, float]:
            return px + u * math.cos(angle) + v * math.sin(angle), py - u * math.sin(angle) + v * math.cos(angle)

        if shape == "circle":
            return tracks.Obstacle(net, layers, ((px, py),), w / 2)
        if shape == "oval":
            r = min(w, h) / 2
            half = (w - h) / 2 if w >= h else (h - w) / 2
            ends = ((-half, 0.0), (half, 0.0)) if w >= h else ((0.0, -half), (0.0, half))
            return tracks.Obstacle(net, layers, (board(*ends[0]), board(*ends[1])), r)
        ratio = _num(sexpr.child(pad, "roundrect_rratio"), 1, 0.25)
        r = min(max(ratio, 0.0), 0.5) * min(w, h)
        hw, hh = max(w / 2 - r, 1e-4), max(h / 2 - r, 1e-4)
        corners = tuple(board(u, v) for u, v in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)))
        return tracks.Obstacle(net, layers, corners, r, polygon=True)
    return tracks.Obstacle(net, layers, tuple(_pad_corners(pad, to_board)), polygon=True)


def _copper(pcb: list[Node], *, filled_zones: bool = False) -> _Copper:
    names = _net_names(pcb)
    segs = _segments(pcb, names)
    vias = []
    for v in sexpr.children(pcb, "via"):
        at = sexpr.child(v, "at")
        vias.append((v, (_num(at, 1), _num(at, 2)), _num(sexpr.child(v, "size"), 1) / 2, _net_of(v, names)))
    pads: list[tracks.Obstacle] = []
    rect_pads: set[int] = set()
    for fp in sexpr.children(pcb, "footprint"):
        to_board = _fp_to_board(fp)
        for pad in sexpr.children(fp, "pad"):
            layers = _copper_layers(pad) if str(pad[2]) != "np_thru_hole" else frozenset(COPPER_LAYERS)
            if layers:
                if str(pad[3]) in ("rect", "roundrect") and sexpr.child(pad, "primitives") is None:
                    rect_pads.add(len(pads))
                pads.append(_pad_obstacle(pad, to_board, _net_of(pad, names), layers))
    edges = []
    for g in pcb:
        if (
            isinstance(g, list)
            and sexpr.head(g) in ("gr_line", "gr_rect", "gr_arc", "gr_poly")
            and _layer(g) == "Edge.Cuts"
        ):
            pts = _graphic_points(g)
            edges += [tracks.Obstacle("<edge>", frozenset(COPPER_LAYERS), (a, b)) for a, b in itertools.pairwise(pts)]
    zones: dict[tuple[str, str], list[list[tracks.Point]]] = {}
    keepouts = []
    no_pour: dict[str, list[list[tracks.Point]]] = {}
    for zone in sexpr.children(pcb, "zone"):
        keepout = sexpr.child(zone, "keepout")
        if keepout is None:
            for layer, poly in _zone_polygons(zone, filled=filled_zones):
                zones.setdefault((_net_of(zone, names), layer), []).append(poly)
        else:
            blocked = {
                str(r[0]) for r in keepout[1:] if isinstance(r, list) and len(r) > 1 and str(r[1]) == "not_allowed"
            }
            net = KEEPOUT_NET if "tracks" in blocked else KEEPOUT_VIA_NET if "vias" in blocked else None
            if net is not None:
                for layer, poly in _zone_polygons(zone, filled=False):
                    keepouts.append(tracks.Obstacle(net, frozenset({layer}), _inset(poly), polygon=True))
            if "copperpour" in blocked:
                for layer, poly in _zone_polygons(zone, filled=False):
                    no_pour.setdefault(layer, []).append(poly)
    return _Copper(names, segs, vias, pads, rect_pads, edges, zones, keepouts, no_pour)


def _inset(poly: list[tracks.Point], by: float = KEEPOUT_INSET_MM) -> tuple[tracks.Point, ...]:
    """An axis-aligned rectangle shrunk by ``by``: the fixes keep the copper clearance to every
    obstacle, but copper may run right up to a keepout's edge. Other shapes stay as they are."""
    xs, ys = sorted({round(p[0], 6) for p in poly}), sorted({round(p[1], 6) for p in poly})
    if len(xs) != 2 or len(ys) != 2 or xs[1] - xs[0] <= 2 * by or ys[1] - ys[0] <= 2 * by:
        return tuple(poly)
    (x0, x1), (y0, y1) = (xs[0] + by, xs[1] - by), (ys[0] + by, ys[1] - by)
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _source(seg: tracks.Seg) -> list[Node]:
    """The ``(segment ...)`` node a ``tracks.Seg`` was read from."""
    assert isinstance(seg.source, list)
    return seg.source


def _drop(tree: list[Node], node: list[Node]) -> None:
    """Remove exactly this node (by identity, not ``==``: duplicate tracks are equal), if still there."""
    for k, n in enumerate(tree):
        if n is node:
            del tree[k]
            return
