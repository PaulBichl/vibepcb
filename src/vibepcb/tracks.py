"""Pure track geometry: polyline clean-up, corner chamfering, dangling-end detection.

Works on plain data (``Seg`` / ``Obstacle``), not on the S-expression tree; ``fixes``
extracts the board's copper into these, calls the functions here and writes back.
Coordinates are KiCad board millimetres (y down).
"""

from __future__ import annotations

import functools
import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

type Point = tuple[float, float]
type Box = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax

EPS = 0.01  # mm: points closer than this are the same point (tracks are >= 0.1 mm wide)
ON_EDGE_MM = 0.001  # a track end this close outside a pad is on its edge: KiCad connects it
SPIKE_DEG = 10.0  # a polyline that turns back by more than 170 deg is a spike
MIN_CORNER_DEG = 135.0  # interior angle: 180 = straight, 135 = a 45 deg bend
CORNER_TOLERANCE_DEG = 0.5
FAR_MM = 1.0
CHAMFER_STEPS_MM = (0.5, 0.35, 0.25, 0.15, 0.08)


@dataclass
class Seg:
    a: Point
    b: Point
    width: float
    layer: str
    net: str
    source: object = None  # the tree node it came from (opaque here)


@dataclass
class Obstacle:
    """Copper of some net: a capsule (``points`` = 1-2 points, ``radius``) or a convex polygon."""

    net: str
    layers: frozenset[str]
    points: tuple[Point, ...]
    radius: float = 0.0
    polygon: bool = False
    routed: bool = False  # copper the router added: it can be ripped up again

    @property
    def centre(self) -> Point:
        """The pad/via position: KiCad's connection anchor for it."""
        return sum(p[0] for p in self.points) / len(self.points), sum(p[1] for p in self.points) / len(self.points)

    @functools.cached_property
    def box(self) -> tuple[float, float, float, float]:
        xs, ys = [p[0] for p in self.points], [p[1] for p in self.points]
        return min(xs) - self.radius, min(ys) - self.radius, max(xs) + self.radius, max(ys) + self.radius

    def distance(self, a: Point, b: Point) -> float:
        """Edge-to-centreline distance from segment ``a``-``b`` to this copper.

        Beyond ``FAR_MM`` only a lower bound (the bounding-box gap) is returned: every
        caller compares against much smaller clearances.
        """
        x0, y0, x1, y1 = self.box
        gap = math.hypot(
            max(x0 - max(a[0], b[0]), 0.0, min(a[0], b[0]) - x1), max(y0 - max(a[1], b[1]), 0.0, min(a[1], b[1]) - y1)
        )
        if gap > FAR_MM:
            return gap
        if self.polygon:
            if _in_convex(a, self.points) or _in_convex(b, self.points):
                return 0.0
            ring = list(zip(self.points, self.points[1:] + self.points[:1], strict=True))
            return min(seg_seg_distance(a, b, p, q) for p, q in ring)
        p, q = self.points[0], self.points[-1]
        return max(0.0, seg_seg_distance(a, b, p, q) - self.radius)


# --- vector helpers ---------------------------------------------------------------


def _sub(p: Point, q: Point) -> Point:
    return p[0] - q[0], p[1] - q[1]


def _cross(u: Point, v: Point) -> float:
    return u[0] * v[1] - u[1] * v[0]


def point_seg_distance(p: Point, a: Point, b: Point) -> float:
    ab, ap = _sub(b, a), _sub(p, a)
    length2 = ab[0] ** 2 + ab[1] ** 2
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, (ap[0] * ab[0] + ap[1] * ab[1]) / length2))
    return math.dist(p, (a[0] + t * ab[0], a[1] + t * ab[1]))


def via_takes(centre: Point, radius: float, s: Seg) -> bool:
    """Whether KiCad connects track ``s`` to a via: the via centre on the track, or a track end
    inside the via (EPS margin). Mere copper overlap is not enough: KiCad reported a track
    ending 0.22 mm from the centre of a 0.2 mm-radius via unconnected (la-bga54e, KiCad 10)."""
    return point_seg_distance(centre, s.a, s.b) <= s.width / 2 - EPS or any(
        math.dist(centre, e) <= radius - EPS for e in (s.a, s.b)
    )


def on_track_body(p: Point, a: Point, b: Point, half: float) -> bool:
    """``p`` within ``half`` of segment a-b, not counting the round end caps.

    KiCad connects a pad to a track whose body crosses the pad centre; a centre covered only
    by the track's end cap (track ending just short of the pad) is not connected.
    """
    ab, ap = _sub(b, a), _sub(p, a)
    length2 = ab[0] ** 2 + ab[1] ** 2
    if length2 == 0:
        return False
    length = math.sqrt(length2)
    along = (ap[0] * ab[0] + ap[1] * ab[1]) / length
    return -EPS <= along <= length + EPS and point_seg_distance(p, a, b) <= half


def crossing(a: Point, b: Point, c: Point, d: Point) -> Point | None:
    """Where segments a-b and c-d properly cross (not parallel), else None."""
    r, q = _sub(b, a), _sub(d, c)
    den = _cross(r, q)
    if abs(den) < 1e-12:
        return None
    t = _cross(_sub(c, a), q) / den
    u = _cross(_sub(c, a), r) / den
    if not (0.0 < t < 1.0 and 0.0 < u < 1.0):
        return None
    return a[0] + t * r[0], a[1] + t * r[1]


def seg_seg_distance(a: Point, b: Point, c: Point, d: Point) -> float:
    d1, d2, d3, d4 = (
        _cross(_sub(b, a), _sub(c, a)),
        _cross(_sub(b, a), _sub(d, a)),
        _cross(_sub(d, c), _sub(a, c)),
        _cross(_sub(d, c), _sub(b, c)),
    )
    if d1 * d2 < 0 and d3 * d4 < 0:  # proper crossing
        return 0.0
    return min(
        point_seg_distance(a, c, d),
        point_seg_distance(b, c, d),
        point_seg_distance(c, a, b),
        point_seg_distance(d, a, b),
    )


def _in_convex(p: Point, poly: tuple[Point, ...]) -> bool:
    signs = {
        _cross(_sub(q, s), _sub(p, s)) > 0
        for s, q in zip(poly, poly[1:] + poly[:1], strict=True)
        if math.dist(s, q) > 0
    }
    return len(signs) == 1


def point_in_polygon(p: Point, poly: list[Point]) -> bool:
    """Even-odd rule (works for KiCad's non-convex fill polygons)."""
    inside = False
    x, y = p
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def interior_angle(prev: Point, p: Point, nxt: Point) -> float:
    """Angle at ``p`` between the two legs in degrees (180 = straight, 0 = doubling back)."""
    u, v = _sub(prev, p), _sub(nxt, p)
    nu, nv = math.hypot(*u), math.hypot(*v)
    if nu < EPS or nv < EPS:
        return 180.0
    cos = max(-1.0, min(1.0, (u[0] * v[0] + u[1] * v[1]) / (nu * nv)))
    return math.degrees(math.acos(cos))


def _key(p: Point) -> tuple[int, int]:
    return round(p[0] / EPS), round(p[1] / EPS)


# --- chains -----------------------------------------------------------------------


@dataclass
class Chain:
    points: list[Point]
    layer: str
    net: str
    segs: list[Seg]  # the originals it replaces
    necked: dict[tuple[Point, Point], float] = field(default_factory=dict)  # new pieces made narrower

    def width_at(self, a: Point, b: Point) -> float:
        """Width for a new piece a-b: the narrowest piece it runs along, else the nearest one.

        "Runs along" = its midpoint lies on that piece (it is part of it) or that piece's
        midpoint lies on it (merged pieces). Pieces include the originals - tscircuit necks
        tracks down near pads - and new pieces made narrower (``necked``).
        """
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        pieces = [(s.a, s.b, s.width) for s in self.segs] + [(p, q, w) for (p, q), w in self.necked.items()]
        along = [
            w
            for p, q, w in pieces
            if point_seg_distance(mid, p, q) <= EPS
            or point_seg_distance(((p[0] + q[0]) / 2, (p[1] + q[1]) / 2), a, b) <= EPS
        ]
        if along:
            return min(along)
        return min(self.segs, key=lambda s: point_seg_distance(mid, s.a, s.b)).width


def chains(segs: list[Seg], split_at: Callable[[Point, str, str], bool]) -> list[Chain]:
    """Split tracks into polylines between junctions, loose ends and ``split_at`` points.

    ``split_at`` marks points (pads) where a track passing through is cut into two that each
    end there; elsewhere a track passing through a via stays one polyline (see ``Keeps``).
    """
    ends: dict[tuple[str, str, tuple[int, int]], list[int]] = {}
    for i, s in enumerate(segs):
        for p in (s.a, s.b):
            ends.setdefault((s.net, s.layer, _key(p)), []).append(i)

    def is_node(p: Point, s: Seg) -> bool:
        return len(ends[s.net, s.layer, _key(p)]) != 2 or split_at(p, s.net, s.layer)

    used: set[int] = set()
    out = []
    for start, s in enumerate(segs):
        if start in used:
            continue
        for origin in (s.a, s.b):
            if not is_node(origin, s):
                continue
            points, members, i, p = [origin], [], start, origin
            while True:
                used.add(i)
                members.append(segs[i])
                q = segs[i].b if _key(segs[i].a) == _key(p) else segs[i].a
                points.append(q)
                if is_node(q, segs[i]):
                    break
                nxt = [j for j in ends[s.net, s.layer, _key(q)] if j != i]
                if not nxt or nxt[0] in used:
                    break
                i, p = nxt[0], q
            out.append(Chain(points, s.layer, s.net, members))
            break
    return out  # closed loops without any node are left alone


PAD_INSET_MM = 0.1  # a trimmed track ends this far inside the pad/via edge


def drop_inside_start(points: list[Point], inside: Callable[[Point], bool]) -> list[Point]:
    """For a polyline starting at a via centre: drop the following points still inside the via.

    The track then runs straight from the centre (where KiCad connects it) to its first
    point outside, instead of zig-zagging inside the via. Applied to both ends.
    """
    pts = list(points)
    for _ in range(2):
        if inside(pts[0]):
            while len(pts) > 2 and inside(pts[1]):
                del pts[1]
        pts.reverse()
    return pts


def trim_ends(points: list[Point], region_of: Callable[[Point], object]) -> list[Point]:
    """Cut the part of a polyline that wanders around inside the pad (or via) it starts/ends in.

    tscircuit often enters a pad with a few tiny zig-zag pieces; inside the pad they add no
    connection, only sharp corners, and two tracks entering one pad meet there at an angle.
    The polyline now ends ``PAD_INSET_MM`` inside the pad edge, on its line in (moved
    further in when it stopped closer to the edge).
    """
    pts = list(points)
    for _ in range(2):
        region = region_of(pts[0])
        if region is not None:
            while len(pts) > 2 and region_of(pts[1]) == region:
                del pts[0]
            inside, outside = pts[0], pts[1]
            if region_of(outside) != region:
                for _ in range(20):  # bisect for the edge crossing
                    mid = ((inside[0] + outside[0]) / 2, (inside[1] + outside[1]) / 2)
                    inside, outside = (mid, outside) if region_of(mid) == region else (inside, mid)
                length = math.dist(inside, pts[0])
                if length > EPS:  # end PAD_INSET_MM inside the edge, on the track's line in
                    end = (
                        inside[0] + (pts[0][0] - inside[0]) * PAD_INSET_MM / length,
                        inside[1] + (pts[0][1] - inside[1]) * PAD_INSET_MM / length,
                    )
                    if region_of(end) == region:
                        pts[0] = end
        pts.reverse()
    return pts


type Keeps = Callable[[Point, Point, Point], bool]
"""``keeps(old, a, b)``: may vertex ``old`` be replaced by a piece a-b? Vertices on a pad or
via the track passes through must stay connected to it; elsewhere anything goes."""


def _anywhere(old: Point, a: Point, b: Point) -> bool:
    return True


def simplify(points: list[Point], keeps: Keeps = _anywhere) -> list[Point]:
    """Drop duplicate points, spikes (the track doubling back) and points on a straight line."""
    pts = [p for i, p in enumerate(points) if i == 0 or math.dist(p, points[i - 1]) > EPS]
    changed = True
    while changed and len(pts) > 2:
        changed = False
        for i in range(1, len(pts) - 1):
            angle = interior_angle(pts[i - 1], pts[i], pts[i + 1])
            straight = point_seg_distance(pts[i], pts[i - 1], pts[i + 1]) < EPS and angle > 90
            if (angle < SPIKE_DEG or straight) and keeps(pts[i], pts[i - 1], pts[i + 1]):
                del pts[i]
                changed = True
                break
    return pts


def _is_sharp(prev: Point, p: Point, nxt: Point) -> bool:
    return interior_angle(prev, p, nxt) < MIN_CORNER_DEG - CORNER_TOLERANCE_DEG


def sharp_corners(points: list[Point]) -> list[int]:
    return [i for i in range(1, len(points) - 1) if _is_sharp(points[i - 1], points[i], points[i + 1])]


SHORTCUT_LEG_MM = 0.3  # corners with a leg this short are cut straight across


def round_off_corners(
    points: list[Point], fits: Callable[[Point, Point], bool], keeps: Keeps = _anywhere, max_passes: int = 6
) -> list[Point]:
    """Remove every corner sharper than 135 deg where the new copper keeps its clearances.

    Acute corners and corners with a very short leg are cut straight across (the vertex
    goes); other sharp corners get a 45 deg style chamfer; where neither fits, the corner
    point moves a little to open the bend (``_move_corner``). Chamfering a corner of interior
    angle t leaves two corners of 90 + t/2, so 90 deg corners need one pass and acute ones
    more. ``fits(a, b)`` says whether a new piece a-b keeps its clearances; corners where
    nothing fits stay as they are.
    """
    pts = list(points)
    for _ in range(max_passes):
        changed = False
        i = 1
        while i < len(pts) - 1:
            prev, p, nxt = pts[i - 1], pts[i], pts[i + 1]
            if not _is_sharp(prev, p, nxt):
                i += 1
                continue
            legs = math.dist(prev, p), math.dist(p, nxt)
            if (
                (interior_angle(prev, p, nxt) < 90 or min(legs) < SHORTCUT_LEG_MM)
                and keeps(p, prev, nxt)
                and fits(prev, nxt)
            ):
                del pts[i]
                changed = True
                continue
            for d in (d for d in CHAMFER_STEPS_MM if d <= 0.45 * min(legs)):
                u, v = _sub(prev, p), _sub(nxt, p)
                p1 = (p[0] + u[0] / legs[0] * d, p[1] + u[1] / legs[0] * d)
                p2 = (p[0] + v[0] / legs[1] * d, p[1] + v[1] / legs[1] * d)
                if keeps(p, p1, p2) and fits(p1, p2):
                    pts[i : i + 1] = [p1, p2]
                    changed = True
                    break
            else:
                moved = _move_corner(pts, i, fits, keeps)
                if moved is not None:
                    pts[i] = moved
                    changed = True
            i += 1
        if not changed:
            break
    return pts


NECK_MAX_MM = 0.6  # only short new pieces (chamfers, cuts) may be narrower than their track


MOVE_STEPS_MM = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
MOVE_DIRECTIONS = 32


def _move_corner(pts: list[Point], i: int, fits: Callable[[Point, Point], bool], keeps: Keeps) -> Point | None:
    """A nearby spot for corner ``i`` where its bend opens up, for corners too cramped to chamfer.

    The two legs must keep their clearances, the bend at the new spot must be wider than
    before, and the neighbouring bends must not turn sharp. Nearest such spot wins.
    """
    prev, p, nxt = pts[i - 1], pts[i], pts[i + 1]
    before = interior_angle(prev, p, nxt)

    def bends_ok(q: Point) -> bool:
        if i >= 2 and _is_sharp(pts[i - 2], prev, q) and not _is_sharp(pts[i - 2], prev, p):
            return False
        return not (i + 2 < len(pts) and _is_sharp(q, nxt, pts[i + 2]) and not _is_sharp(p, nxt, pts[i + 2]))

    for r in MOVE_STEPS_MM:
        best = None
        for k in range(MOVE_DIRECTIONS):
            t = 2 * math.pi * k / MOVE_DIRECTIONS
            q = (p[0] + r * math.cos(t), p[1] + r * math.sin(t))
            angle = interior_angle(prev, q, nxt)
            if angle <= before + 1 or min(math.dist(prev, q), math.dist(q, nxt)) < EPS:
                continue
            if (
                keeps(p, prev, q)
                and keeps(p, q, nxt)
                and bends_ok(q)
                and fits(prev, q)
                and fits(q, nxt)
                and (best is None or angle > best[0])
            ):
                best = (angle, q)
        if best is not None:
            return best[1]
    return None


def clearance_check(
    chain: Chain,
    obstacles: Iterable[Obstacle],
    edges: Iterable[Obstacle],
    clearance: float,
    edge_clearance: float,
    min_width: float,
) -> Callable[[Point, Point], bool]:
    """``fits(a, b)``: a new piece of ``chain`` from a to b keeps its clearance to other nets' copper and the board edge.

    A short piece that doesn't fit at the track's width may neck down to ``min_width``
    (recorded in ``chain.necked``), like a router does next to a pad.
    """
    copper = [o for o in obstacles if o.net != chain.net and chain.layer in o.layers]
    outline = [e for e in edges if chain.layer in e.layers]

    def room(a: Point, b: Point) -> float:
        """Widest track a-b could be."""
        return 2 * min(
            [o.distance(a, b) - clearance for o in copper]
            + [e.distance(a, b) - edge_clearance for e in outline]
            + [math.inf]
        )

    def fits(a: Point, b: Point) -> bool:
        width, available = chain.width_at(a, b), room(a, b)
        if available >= width:
            return True
        if math.dist(a, b) <= NECK_MAX_MM and available >= min_width:
            chain.necked[a, b] = max(min_width, math.floor(available * 100) / 100)
            return True
        return False

    return fits


def _boxes(segs: list[Seg]) -> list[tuple[float, float, float, float]]:
    return [
        (
            min(s.a[0], s.b[0]) - s.width,
            min(s.a[1], s.b[1]) - s.width,
            max(s.a[0], s.b[0]) + s.width,
            max(s.a[1], s.b[1]) + s.width,
        )
        for s in segs
    ]


GRID_MM = 2.0


class BoxIndex:
    """Uniform grid over bounding boxes: which boxes may contain a point / overlap a box."""

    def __init__(self, boxes: list[tuple[float, float, float, float]]) -> None:
        self.boxes = boxes
        self.cells: dict[tuple[int, int], list[int]] = {}
        for i, (x0, y0, x1, y1) in enumerate(boxes):
            for cx in range(math.floor(x0 / GRID_MM), math.floor(x1 / GRID_MM) + 1):
                for cy in range(math.floor(y0 / GRID_MM), math.floor(y1 / GRID_MM) + 1):
                    self.cells.setdefault((cx, cy), []).append(i)

    def overlapping(self, x0: float, y0: float, x1: float, y1: float) -> set[int]:
        """Indices of boxes that overlap (or touch) the given box."""
        found: set[int] = set()
        boxes = self.boxes
        for cx in range(math.floor(x0 / GRID_MM), math.floor(x1 / GRID_MM) + 1):
            for cy in range(math.floor(y0 / GRID_MM), math.floor(y1 / GRID_MM) + 1):
                for i in self.cells.get((cx, cy), ()):
                    bx0, by0, bx1, by1 = boxes[i]
                    if bx0 <= x1 and x0 <= bx1 and by0 <= y1 and y0 <= by1:
                        found.add(i)
        return found

    def at(self, p: Point, skip: int = -1) -> list[int]:
        """Indices of boxes containing p (ascending, except ``skip``)."""
        boxes = self.boxes
        return sorted(
            i
            for i in self.cells.get((math.floor(p[0] / GRID_MM), math.floor(p[1] / GRID_MM)), ())
            if i != skip and boxes[i][0] <= p[0] <= boxes[i][2] and boxes[i][1] <= p[1] <= boxes[i][3]
        )


def pad_groups(
    pads: list[Obstacle], vias: list[tuple[Point, float]], segs: list[Seg], anchors: Iterable[int] = ()
) -> list[set[int]]:
    """Which of one net's pads are joined by its tracks and vias (pours not counted).

    Connections as KiCad makes them: a track end inside a pad or a pad centre on a track
    (of a layer it is on); a track over a via's centre or ending on the via's copper
    (:func:`via_takes`); a track end on another track of the same layer; a via inside a pad.
    ``anchors``: indices of vias that count like pads (vias into a plane of the net: a pad's
    track to one is its connection even though the plane isn't modelled); they appear in the
    groups as ``len(pads) + index``.
    """
    find = _connect(pads, vias, segs)
    groups: dict[int, set[int]] = {}
    for i in [*range(len(pads)), *(len(pads) + v for v in anchors)]:
        groups.setdefault(find(i), set()).add(i)
    return list(groups.values())


def unreached(pads: list[Obstacle], vias: list[tuple[Point, float]], segs: list[Seg]) -> tuple[set[int], set[int]]:
    """Indices of one net's vias and tracks joined to none of its pads (islands; pours not counted)."""
    find = _connect(pads, vias, segs)
    rooted = {find(i) for i in range(len(pads))}
    n_pads, n_vias = len(pads), len(vias)
    return (
        {v for v in range(n_vias) if find(n_pads + v) not in rooted},
        {k for k in range(len(segs)) if find(n_pads + n_vias + k) not in rooted},
    )


def _connect(pads: list[Obstacle], vias: list[tuple[Point, float]], segs: list[Seg]) -> Callable[[int], int]:
    """Union-find over pads, then vias, then tracks (see :func:`pad_groups`); returns ``find``."""
    n_pads, n_vias = len(pads), len(vias)
    parent = list(range(n_pads + n_vias + len(segs)))
    boxes = _boxes(segs)
    seg_index = BoxIndex(boxes)
    pad_index = BoxIndex([p.box for p in pads])
    # a via a track takes (via_takes) has its copper box overlapping the track's box: only those are tested
    # (testing every via against every track made `check` take hours on a board with hundreds of GND vias)
    via_index = BoxIndex([(c[0] - r, c[1] - r, c[0] + r, c[1] + r) for c, r in vias])
    centres = [p.centre for p in pads]

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for v, (centre, _) in enumerate(vias):
        for i in pad_index.overlapping(*centre, *centre):  # outside a pad's box: never inside the pad
            if pads[i].distance(centre, centre) <= 0:
                union(i, n_pads + v)
    for k, s in enumerate(segs):
        node = n_pads + n_vias + k
        half = s.width / 2
        sx0, sy0 = min(s.a[0], s.b[0]) - half, min(s.a[1], s.b[1]) - half
        sx1, sy1 = max(s.a[0], s.b[0]) + half, max(s.a[1], s.b[1]) + half
        for i in pad_index.overlapping(sx0, sy0, sx1, sy1):
            pad = pads[i]
            if s.layer in pad.layers and (
                any(pad.distance(e, e) <= ON_EDGE_MM for e in (s.a, s.b)) or on_track_body(centres[i], s.a, s.b, half)
            ):
                union(node, i)
        for v in via_index.overlapping(sx0, sy0, sx1, sy1):
            centre, radius = vias[v]
            if via_takes(centre, radius, s):
                union(node, n_pads + v)
        for e in (s.a, s.b):
            for m in seg_index.at(e, k):
                t = segs[m]
                if t.layer == s.layer and point_seg_distance(e, t.a, t.b) <= t.width / 2:
                    union(node, n_pads + n_vias + m)
    return find


def still_connected(before: list[set[int]], after: list[set[int]]) -> bool:
    """Every group of pads joined before is still joined (possibly into a bigger group)."""
    return all(any(group <= bigger for bigger in after) for group in before)


def _direction_from(p: Point, s: Seg) -> Point:
    """Unit vector along s pointing away from p (p being one of its ends or on its body)."""
    far = s.b if math.dist(p, s.a) <= math.dist(p, s.b) else s.a
    d = _sub(far, p)
    n = math.hypot(*d)
    return (d[0] / n, d[1] / n) if n > 0 else (0.0, 0.0)


def sharp_junctions(segs: list[Seg]) -> list[int]:
    """Indices of tracks meeting another track of their net at a sharp angle where three or more meet.

    Covers junctions of three or more track ends and a track ending on another's body
    (a T), where the angle between the ending track and either direction of the body counts.
    """
    ends: dict[tuple[str, str, tuple[int, int]], list[int]] = {}
    for i, s in enumerate(segs):
        for p in (s.a, s.b):
            ends.setdefault((s.net, s.layer, _key(p)), []).append(i)
    limit = MIN_CORNER_DEG - CORNER_TOLERANCE_DEG
    out: set[int] = set()

    def angle(u: Point, v: Point) -> float:
        return math.degrees(math.acos(max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))))

    for (_, _, key), users in ends.items():
        if len(users) < 3:
            continue
        p = (key[0] * EPS, key[1] * EPS)
        dirs = {i: _direction_from(p, segs[i]) for i in users}
        for i, j in itertools.combinations(users, 2):
            if angle(dirs[i], dirs[j]) < limit:
                out |= {i, j}
    for i, s in enumerate(segs):
        for p in (s.a, s.b):
            if len(ends[s.net, s.layer, _key(p)]) != 1:
                continue
            for j, t in enumerate(segs):
                if j == i or t.net != s.net or t.layer != s.layer or min(math.dist(p, t.a), math.dist(p, t.b)) <= EPS:
                    continue
                if point_seg_distance(p, t.a, t.b) <= t.width / 2:
                    u = _direction_from(p, s)
                    d = _sub(t.b, t.a)
                    n = math.hypot(*d)
                    if n == 0:
                        continue
                    v = (d[0] / n, d[1] / n)
                    if min(angle(u, v), angle(u, (-v[0], -v[1]))) < limit:
                        out |= {i, j}
    return sorted(out, key=lambda i: math.dist(segs[i].a, segs[i].b))


def tees(segs: list[Seg]) -> list[tuple[int, int, int, Point]]:
    """T-junctions as ``(ending track, which end 0/1, through track, point on it)``.

    A track end lying on another track's body (not at its ends). KiCad only counts
    end-to-end contacts as connected track ends, so the through track needs a vertex there
    and the ending track's end is moved exactly onto it.
    """
    index = BoxIndex(_boxes(segs))
    out = []
    for i, s in enumerate(segs):
        for end, p in enumerate((s.a, s.b)):
            for j in index.at(p, i):
                t = segs[j]
                if t.net != s.net or t.layer != s.layer or min(math.dist(p, t.a), math.dist(p, t.b)) <= EPS:
                    continue
                if point_seg_distance(p, t.a, t.b) <= t.width / 2:
                    out.append((i, end, j, project(p, t.a, t.b)))
                    break
    return out


def project(p: Point, a: Point, b: Point) -> Point:
    """The point of segment a-b nearest to p."""
    ab, ap = _sub(b, a), _sub(p, a)
    length2 = ab[0] ** 2 + ab[1] ** 2
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, (ap[0] * ab[0] + ap[1] * ab[1]) / length2))
    return a[0] + t * ab[0], a[1] + t * ab[1]


MIN_JUNCTION_DEG = 90.0  # branches meeting at a junction: no acute angles (3 branches can't all be 135 apart)


def junctions(segs: list[Seg]) -> dict[tuple[str, str, tuple[int, int]], list[tuple[int, int]]]:
    """Points where 3+ track ends of one net/layer meet: ``{(net, layer, key): [(track, end 0/1)]}``."""
    out: dict[tuple[str, str, tuple[int, int]], list[tuple[int, int]]] = {}
    for i, s in enumerate(segs):
        for end, p in enumerate((s.a, s.b)):
            out.setdefault((s.net, s.layer, _key(p)), []).append((i, end))
    return {k: v for k, v in out.items() if len(v) >= 3}


def min_branch_angle(j: Point, others: list[Point]) -> float:
    """Smallest angle between any two branches leaving j towards ``others``."""
    return min((interior_angle(a, j, b) for a, b in itertools.combinations(others, 2)), default=180.0)


def move_junction(j: Point, others: list[Point], fits: Callable[[Point, Point], bool]) -> Point | None:
    """A nearby spot for junction j whose branches (j -> each of ``others``) meet at no acute angle.

    Nearest spot that reaches ``MIN_JUNCTION_DEG`` wins, else the one improving the
    smallest angle most; every branch must keep its clearances.
    """
    before = min_branch_angle(j, others)
    best: tuple[float, Point] | None = None
    for r in MOVE_STEPS_MM:
        for k in range(MOVE_DIRECTIONS):
            t = 2 * math.pi * k / MOVE_DIRECTIONS
            q = (j[0] + r * math.cos(t), j[1] + r * math.sin(t))
            if any(math.dist(q, o) < 2 * EPS for o in others):
                continue
            angle = min_branch_angle(q, others)
            if angle <= before + 1 or (best is not None and angle <= best[0]):
                continue
            if all(fits(q, o) for o in others):
                best = (angle, q)
        if best is not None and best[0] >= MIN_JUNCTION_DEG:
            return best[1]
    return best[1] if best is not None else None


COVER_SAMPLE_MM = 0.05


def _touch(s: Seg, t: Seg) -> bool:
    """Whether KiCad would connect the two tracks: an end of one lies on the other."""
    return any(point_seg_distance(e, t.a, t.b) <= t.width / 2 for e in (s.a, s.b)) or any(
        point_seg_distance(e, s.a, s.b) <= s.width / 2 for e in (t.a, t.b)
    )


def _linked(x: Point, y: Point, segs: list[Seg]) -> bool:
    """Whether track ends at x and at y are connected through ``segs`` (end-to-end or end-on-track)."""
    start = [s for s in segs if min(math.dist(x, s.a), math.dist(x, s.b)) <= EPS]
    goal = {id(s) for s in segs if min(math.dist(y, s.a), math.dist(y, s.b)) <= EPS}
    seen, todo = {id(s) for s in start}, list(start)
    while todo:
        s = todo.pop()
        if id(s) in goal:
            return True
        for t in segs:
            if id(t) not in seen and _touch(s, t):
                seen.add(id(t))
                todo.append(t)
    return False


def redundant(segs: list[Seg]) -> list[int]:
    """Indices of tracks running entirely inside other copper of their own net.

    The autorouter routes each connection separately and sometimes lays two routes of a
    net (nearly) on top of each other; where they part again they form acute junctions.
    A track whose centreline is covered by the net's other tracks is a candidate if no end
    of it is the only partner of another track end (that track would be left touching
    mid-track) and its shared ends stay connected without it - narrowest and shortest
    first, re-checking against what is left. Pads are not modelled here: callers verify
    the net's pad connectivity (``pad_groups``) before dropping a candidate.
    """
    alive = set(range(len(segs)))
    by_group: dict[tuple[str, str], list[int]] = {}
    for i, s in enumerate(segs):
        by_group.setdefault((s.net, s.layer), []).append(i)

    def samples(s: Seg) -> list[Point]:
        n = max(2, math.ceil(math.dist(s.a, s.b) / COVER_SAMPLE_MM) + 1)
        return [(s.a[0] + (s.b[0] - s.a[0]) * k / (n - 1), s.a[1] + (s.b[1] - s.a[1]) * k / (n - 1)) for k in range(n)]

    removed = []
    for i in sorted(alive, key=lambda i: (segs[i].width, math.dist(segs[i].a, segs[i].b))):
        s = segs[i]
        others = [segs[j] for j in by_group[s.net, s.layer] if j != i and j in alive]
        if any({_key(o.a), _key(o.b)} == {_key(s.a), _key(s.b)} and o.width >= s.width for o in others):
            alive.discard(i)  # an exact copy of another track (possibly reversed)
            removed.append(i)
            continue
        # KiCad connects items through track *ends*: whatever met this track at an end must
        # still find another track end there, and those ends must stay connected to each other
        meeting = {e: sum(math.dist(e, q) <= EPS for o in others for q in (o.a, o.b)) for e in (s.a, s.b)}
        shared = [e for e, n in meeting.items() if n]
        if any(n == 1 for n in meeting.values()):
            continue  # its single neighbour there would be left touching mid-track only
        covered = all(any(point_seg_distance(p, o.a, o.b) <= o.width / 2 - EPS for o in others) for p in samples(s))
        # tracks ending on this one (T-junctions) must land on the remaining copper as well
        landing = [q for o in others for q in (o.a, o.b) if point_seg_distance(q, s.a, s.b) <= s.width / 2]
        held = all(
            any(point_seg_distance(q, o.a, o.b) <= o.width / 2 for o in others if q not in (o.a, o.b)) for q in landing
        )
        if covered and held and (len(shared) < 2 or _linked(s.a, s.b, others)):
            alive.discard(i)
            removed.append(i)
    return sorted(removed)


def dangling(segs: list[Seg], connected: Callable[[Point, Seg], bool]) -> list[int]:
    """Indices of tracks that are (or, once their neighbours go, become) loose at one end."""
    alive = set(range(len(segs)))
    boxes = _boxes(segs)
    index = BoxIndex(boxes)

    def touches(p: Point, s: Seg, others: Iterable[int]) -> bool:
        """End p of s lies on another track, or another track's end lies on s right at p."""
        return any(
            segs[j].net == s.net
            and segs[j].layer == s.layer
            and (
                point_seg_distance(p, segs[j].a, segs[j].b) <= segs[j].width / 2
                or any(math.dist(q, p) <= s.width / 2 for q in (segs[j].a, segs[j].b))
            )
            for j in others
        )

    # worklist: a track that goes can only loosen tracks with an end on it - their boxes overlap its box - so
    # only those are looked at again (re-scanning every track after each removal was quadratic on long chains)
    todo = sorted(alive)
    queued = set(todo)
    while todo:
        i = todo.pop()
        queued.discard(i)
        if i not in alive:
            continue
        s = segs[i]
        near = set(index.at(s.a, i) + index.at(s.b, i))
        others = [j for j in near if j in alive]
        if not all(connected(p, s) or touches(p, s, others) for p in (s.a, s.b)):
            alive.discard(i)
            for j in index.overlapping(*boxes[i]):
                if j in alive and j not in queued:
                    todo.append(j)
                    queued.add(j)
    return sorted(set(range(len(segs))) - alive)


GRID_HEADINGS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
TURN_COST = 0.15  # mm-equivalent per 45 deg turn: prefer few bends


def grid_route(
    start: Point,
    goal: Point,
    free: Callable[[Point], bool],
    step: float,
    start_headings: set[int] | None = None,
    end_ok: Callable[[Point, Point], bool] | None = None,
    max_states: int = 400_000,
) -> list[Point] | None:
    """A 45-degree maze route from ``start`` to ``goal`` (a polyline), or None.

    The grid is aligned to ``start``; moves go to the 8 neighbours and turn by at most 45 deg
    at a time, so no bend is sharper than 135 deg. ``free(p)``: may the track centre pass p.
    ``start_headings``: allowed first directions (indices into ``GRID_HEADINGS``).
    The route ends with a straight piece from a grid node within one step of ``goal`` to
    ``goal``, if ``end_ok(node, goal)`` accepts it. Collinear nodes are merged.
    """

    def at(ix: int, iy: int) -> Point:
        return start[0] + ix * step, start[1] + iy * step

    def h(ix: int, iy: int) -> float:
        return math.dist(at(ix, iy), goal)

    heap: list[tuple[float, float, int, int, int]] = []
    best: dict[tuple[int, int, int], float] = {}
    parent: dict[tuple[int, int, int], tuple[int, int, int] | None] = {}
    for d in start_headings if start_headings is not None else range(8):
        dx, dy = GRID_HEADINGS[d]
        cost = step * math.hypot(dx, dy)
        if free(at(dx, dy)):
            state = (dx, dy, d)
            best[state] = cost
            parent[state] = None
            heapq.heappush(heap, (cost + h(dx, dy), cost, dx, dy, d))
    popped = 0
    while heap and popped < max_states:
        _, cost, ix, iy, d = heapq.heappop(heap)
        popped += 1
        if cost > best.get((ix, iy, d), math.inf):
            continue
        node = at(ix, iy)
        if math.dist(node, goal) <= step * 1.5 and (end_ok is None or end_ok(node, goal)):
            states = [(ix, iy, d)]
            while (prev := parent[states[-1]]) is not None:
                states.append(prev)
            points = [start, *(at(x, y) for x, y, _ in reversed(states)), goal]
            return _merge_collinear(points)
        for turn in (-1, 0, 1):
            nd = (d + turn) % 8
            dx, dy = GRID_HEADINGS[nd]
            nx, ny = ix + dx, iy + dy
            if not free(at(nx, ny)):
                continue
            ncost = cost + step * math.hypot(dx, dy) + (TURN_COST if turn else 0.0)
            if ncost < best.get((nx, ny, nd), math.inf):
                best[nx, ny, nd] = ncost
                parent[nx, ny, nd] = (ix, iy, d)
                heapq.heappush(heap, (ncost + h(nx, ny), ncost, nx, ny, nd))
    return None


def _merge_collinear(points: list[Point]) -> list[Point]:
    out = [points[0]]
    for p in points[1:]:
        if math.dist(p, out[-1]) <= EPS / 10:
            continue
        if len(out) >= 2 and abs(_cross(_sub(out[-1], out[-2]), _sub(p, out[-1]))) <= 1e-9:
            out[-1] = p
        else:
            out.append(p)
    return out
