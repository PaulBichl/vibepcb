"""Corner fixes: snapped ends, square tees, bends to 45 deg, stubs, via exits, tucks into pads."""

from __future__ import annotations

import itertools
import math
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import (
    REROUTE_STEP_MM,
    REROUTE_STUB_ANGLE_STEP_DEG,
    REROUTE_STUB_MM,
    REROUTE_STUB_SAMPLE_DEG,
    REROUTE_STUB_TRIES,
    KicadProject,
    _design_clearances,
    _mm,
    _segment_node,
)
from vibepcb.kicad.fixes.copper import _Copper, _copper, _drop, _source
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from collections.abc import Callable


def snap_near_ends(project: KicadProject) -> list[str]:
    """Join track ends that overlap another track of their net without meeting it exactly.

    KiCad connects tracks whose copper overlaps and measures the angle between them; the
    router leaves ends a few microns apart (two "ends" of one corner) or an end beside another
    track inside its copper. The end moves onto the other track's nearest end (if that is
    within reach) or onto its centreline, so the corner/junction fixes see a real vertex or T.
    Moves that would break the clearance are skipped.
    """
    clearance, edge_clearance = _design_clearances(project)
    changes: list[str] = []
    for _ in range(SNAP_MAX_ROUNDS):  # one end per round: every snap changes what the next one sees
        change = _snap_one_end(project, clearance, edge_clearance)
        if change is None:
            break
        changes.append(change)
    return changes


SNAP_MAX_ROUNDS = 500


T_ON_LINE_MM = 1e-3  # an end this close to another track's centreline is a T junction, not a near miss


def _snap_one_end(project: KicadProject, clearance: float, edge_clearance: float) -> str | None:
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()
    for s in copper.segs:
        for k, (end, far) in enumerate(((s.a, s.b), (s.b, s.a))):
            if copper.on_pad_or_via(end, s.net, s.layer) or copper.attached(end, s):
                continue  # the end is a pad's or via's connection (its cap may just reach a pad): moving it cuts that
            same = [t for t in copper.segs_by_net.get(s.net, []) if t is not s and t.layer == s.layer]
            if any(min(math.dist(end, t.a), math.dist(end, t.b)) <= 1e-6 for t in same):
                continue  # already meets a track end exactly
            if any(tracks.point_seg_distance(end, t.a, t.b) <= T_ON_LINE_MM for t in same):
                continue  # a T on the track's centreline (square_tees' foot may lie next to a corner): pulling it
                # onto the corner made the two fixes undo each other every pass
            ends_near = [q for t in same for q in (t.a, t.b) if 1e-6 < math.dist(q, end) <= (s.width + t.width) / 2]
            if ends_near:  # two ends of one corner, a little apart
                target = min(ends_near, key=lambda q: math.dist(q, end))
            else:
                if any(tracks.point_seg_distance(end, t.a, t.b) <= t.width / 2 for t in same):
                    continue  # already on a track (a T)
                reach = [t for t in same if tracks.point_seg_distance(end, t.a, t.b) <= (s.width + t.width) / 2]
                if not reach:
                    continue
                t = min(reach, key=lambda t: tracks.point_seg_distance(end, t.a, t.b))
                ab = (t.b[0] - t.a[0], t.b[1] - t.a[1])
                u = ((end[0] - t.a[0]) * ab[0] + (end[1] - t.a[1]) * ab[1]) / (ab[0] ** 2 + ab[1] ** 2)
                target = (t.a[0] + u * ab[0], t.a[1] + u * ab[1])
            half = s.width / 2
            if any(
                o.distance(target, far) < clearance + half for o in obstacles if o.net != s.net and s.layer in o.layers
            ):
                continue
            if any(e.distance(target, far) < edge_clearance + half for e in copper.edges if s.layer in e.layers):
                continue
            node = sexpr.child(_source(s), ("start", "end")[k])
            assert node is not None
            node[1:3] = [Sym(_mm(target[0])), Sym(_mm(target[1]))]
            return f"{s.net} at ({_mm(end[0])}, {_mm(end[1])}): track end joined to the track it overlapped"
    return None


def square_tees(project: KicadProject) -> list[str]:
    """Make a track that ends on the side of another track of its net meet it at 90 deg.

    KiCad's ``track_angle`` rule measures such T-junctions too; where the branch arrives at
    an acute angle, its end slides along the other track to the foot of the perpendicular
    from the branch's far end. A branch meeting another at an acute angle in a junction of
    3+ tracks leaves the junction the same way (the others stay joined there). Done only if the new piece keeps its clearance, the foot lies
    within the other track and no bend sharper than 135 deg appears at the far end.
    """
    clearance, edge_clearance = _design_clearances(project)
    changes: list[str] = []
    for _ in range(SQUARE_TEES_MAX_PASSES):
        done = _square_tees_pass(project, clearance, edge_clearance)
        changes += done
        if not done:
            break
    return changes


SQUARE_TEES_MAX_PASSES = 20


def _square_tees_pass(project: KicadProject, clearance: float, edge_clearance: float) -> list[str]:
    """One pass of :func:`square_tees` on the copper as it is now.

    A track moved in this pass, or its new body, is stale geometry for the rest of the pass:
    tracks touching either wait for the next pass (two branches of one junction, each acute
    to the other, would otherwise both move onto the other's old position and meet nothing).
    """
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()
    changes = []
    touched: set[int] = set()
    for s in copper.segs:
        if id(s) in touched:
            continue
        for k, (end, far) in enumerate(((s.a, s.b), (s.b, s.a))):
            if copper.on_pad_or_via(end, s.net, s.layer):
                continue
            same = [t for t in copper.segs_by_net.get(s.net, []) if t is not s and t.layer == s.layer]
            sharing = [t for t in same if min(math.dist(end, t.a), math.dist(end, t.b)) <= tracks.EPS]
            if any(id(t) in touched for t in sharing):
                continue
            if not sharing:  # a T: the end lies on another track's side
                body = next(
                    (t for t in same if tracks.point_seg_distance(end, t.a, t.b) <= t.width / 2),
                    None,
                )
            elif len(sharing) >= 2:  # a junction: leave it and join an acute neighbour's side instead
                body = next(
                    (
                        t
                        for t in sharing
                        if tracks.interior_angle(far, end, t.b if math.dist(end, t.a) <= tracks.EPS else t.a)
                        < tracks.MIN_JUNCTION_DEG - 1
                    ),
                    None,
                )
            else:  # a plain bend: moving the end would cut the other track off
                continue
            if body is None or id(body) in touched:
                continue
            angle = min(tracks.interior_angle(far, end, body.a), tracks.interior_angle(far, end, body.b))
            if angle >= tracks.MIN_JUNCTION_DEG - 1:
                continue
            ab = (body.b[0] - body.a[0], body.b[1] - body.a[1])
            length2 = ab[0] ** 2 + ab[1] ** 2
            t = ((far[0] - body.a[0]) * ab[0] + (far[1] - body.a[1]) * ab[1]) / length2
            foot = (body.a[0] + t * ab[0], body.a[1] + t * ab[1])
            margin = tracks.EPS / math.sqrt(length2)
            if not margin < t < 1 - margin or math.dist(foot, far) <= tracks.EPS:
                continue
            half = s.width / 2
            if any(
                o.distance(foot, far) < clearance + half for o in obstacles if o.net != s.net and s.layer in o.layers
            ):
                continue
            if any(e.distance(foot, far) < edge_clearance + half for e in copper.edges if s.layer in e.layers):
                continue
            # the bend at the far end, where the branch continues
            nxt = [
                (o.b if math.dist(o.a, far) <= tracks.EPS else o.a)
                for o in copper.segs_by_net.get(s.net, [])
                if o is not s and o.layer == s.layer and min(math.dist(o.a, far), math.dist(o.b, far)) <= tracks.EPS
            ]
            if len(nxt) == 1 and tracks.interior_angle(foot, far, nxt[0]) < tracks.MIN_CORNER_DEG:
                continue
            node = sexpr.child(_source(s), ("start", "end")[k])
            assert node is not None
            # the end may also be a pad's connection (its cap reaching the pad): keep the move only if the net's
            # pads stay as connected as before
            pads, vias = copper.pads_by_net.get(s.net, []), copper.vias_by_net.get(s.net, [])
            net_segs = copper.segs_by_net.get(s.net, [])
            moved = tracks.Seg(foot, far, s.width, s.layer, s.net)
            if not tracks.still_connected(
                tracks.pad_groups(pads, vias, net_segs),
                tracks.pad_groups(pads, vias, [moved if o is s else o for o in net_segs]),
            ):
                continue
            node[1:3] = [Sym(_mm(foot[0])), Sym(_mm(foot[1]))]
            changes.append(f"{s.net} at ({_mm(end[0])}, {_mm(end[1])}): T-junction squared ({angle:.0f} -> 90 deg)")
            touched |= {id(s), id(body)}
            break
    return changes


def _polyline_ends(copper: _Copper) -> dict[tuple[str, str, tuple[int, int]], list[tuple[tracks.Seg, str]]]:
    """Track ends per (net, layer, point): each track with the end (``start``/``end``) lying there."""
    ends: dict[tuple[str, str, tuple[int, int]], list[tuple[tracks.Seg, str]]] = {}
    for s in copper.segs:
        ends.setdefault((s.net, s.layer, tracks._key(s.a)), []).append((s, "start"))
        ends.setdefault((s.net, s.layer, tracks._key(s.b)), []).append((s, "end"))
    return ends


def _far(seg: tracks.Seg, end: str) -> tracks.Point:
    return seg.b if end == "start" else seg.a


def _set_end(seg: tracks.Seg, end: str, p: tracks.Point) -> None:
    node = sexpr.child(_source(seg), end)
    assert node is not None
    node[1:3] = [Sym(_mm(p[0])), Sym(_mm(p[1]))]


def _corner_at(
    ends: dict[tuple[str, str, tuple[int, int]], list[tuple[tracks.Seg, str]]],
    copper: _Copper,
    seg: tracks.Seg,
    p: tracks.Point,
) -> tracks.Point | None:
    """The far end of the one other track bending at ``p`` with ``seg`` (a plain bend), else None."""
    if copper.on_pad_or_via(p, seg.net, seg.layer):
        return None
    others = [(t, e) for t, e in ends.get((seg.net, seg.layer, tracks._key(p)), []) if t is not seg]
    return _far(*others[0]) if len(others) == 1 else None


def _fits(
    copper: _Copper, obstacles: list[tracks.Obstacle], seg: tracks.Seg, a: tracks.Point, b: tracks.Point,
    clearance: float, edge_clearance: float,
) -> bool:  # fmt: skip
    half = seg.width / 2
    return all(
        o.distance(a, b) >= clearance + half for o in obstacles if o.net != seg.net and seg.layer in o.layers
    ) and all(e.distance(a, b) >= edge_clearance + half for e in copper.edges if seg.layer in e.layers)


def _snapped(d: tuple[float, float]) -> tuple[float, float]:
    """Direction ``d`` turned to the nearest multiple of 45 deg (unit length)."""
    step = math.pi / 4
    angle = round(math.atan2(d[1], d[0]) / step) * step
    return math.cos(angle), math.sin(angle)


def _line_cross(
    p: tracks.Point, d: tuple[float, float], q: tracks.Point, e: tuple[float, float]
) -> tracks.Point | None:
    det = d[0] * e[1] - d[1] * e[0]
    if abs(det) < 1e-9:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / det
    return p[0] + t * d[0], p[1] + t * d[1]


SNAP_BEND_DEG = 10.0  # bends this far below 135 deg are snapped to the 45 deg grid


CORNER_TOLERANCE_DEG = 1.0  # sharp_corners' allowance for KiCad's rounding


CORNER_FIX_MAX_PASSES = 200  # one change per pass (the copper is rebuilt after each)


SNAP_MOVE_MM = 0.2  # the corner moves at most this far


def snap_bends_to_45(project: KicadProject) -> list[str]:
    """Move the corner of a bend just short of 135 deg so both its tracks lie on the 45 deg grid.

    The router leaves bends of 130-134 deg where one piece drifts a degree or two off the grid (seen: a 45 deg
    diagonal meeting a track 1.3 deg off horizontal, 133.7 deg). The corner moves (at most :data:`SNAP_MOVE_MM`)
    to where the two pieces, each turned to the nearest 45 deg direction about its far end, cross. Done only if both
    pieces keep their clearance and the bends at their far ends don't get sharper than 135 deg (or than they were);
    only bends sharp_corners reports are touched.
    """
    clearance, edge_clearance = _design_clearances(project)
    changes: list[str] = []
    skip: set[tuple[int, int]] = set()  # corners tried without success
    for _ in range(CORNER_FIX_MAX_PASSES):
        copper = _copper(project.pcb)
        obstacles = copper.obstacles()
        ends = _polyline_ends(copper)
        done = False
        for (net, layer, key), items in ends.items():
            if key in skip:
                continue
            if len(items) != 2:
                continue
            (s1, e1), (s2, e2) = items
            v = s1.a if e1 == "start" else s1.b
            a, b = _far(s1, e1), _far(s2, e2)
            angle = tracks.interior_angle(a, v, b)
            if not tracks.MIN_CORNER_DEG - SNAP_BEND_DEG <= angle < tracks.MIN_CORNER_DEG - CORNER_TOLERANCE_DEG:
                continue  # only bends sharp_corners reports
            if copper.on_pad_or_via(v, net, layer):
                continue
            new = _line_cross(a, _snapped((v[0] - a[0], v[1] - a[1])), b, _snapped((v[0] - b[0], v[1] - b[1])))
            if (
                new is None
                or math.dist(new, v) > SNAP_MOVE_MM
                or tracks.interior_angle(a, new, b) < tracks.MIN_CORNER_DEG - 1e-6
            ):
                continue
            ok = _fits(copper, obstacles, s1, a, new, clearance, edge_clearance) and _fits(
                copper, obstacles, s2, new, b, clearance, edge_clearance
            )
            for seg, far in ((s1, a), (s2, b)):  # the bends at the far ends
                nxt = _corner_at(ends, copper, seg, far)
                if nxt is not None:
                    before, after = tracks.interior_angle(v, far, nxt), tracks.interior_angle(new, far, nxt)
                    ok = ok and after >= min(tracks.MIN_CORNER_DEG, before) - 1e-6
            if not ok:
                skip.add(tracks._key(v))
                continue
            _set_end(s1, e1, new)
            _set_end(s2, e2, new)
            changes.append(
                f"{net} bend at ({_mm(v[0])}, {_mm(v[1])}): {angle:.1f} -> {tracks.interior_angle(a, new, b):.1f} deg"
            )
            done = True
            break
        if not done:
            break
    return changes


STUB_COLLAPSE_MM = 0.6  # a piece this short between two bends may be dropped


STUB_MOVE_MM = 1.0  # the new corner lies at most this far from the dropped piece's ends


def collapse_bend_stubs(project: KicadProject) -> list[str]:
    """Drop a short piece between two bends and extend its neighbours until they meet, where that bend is sharp.

    The router turns a 45 deg diagonal onto a straight via a short third direction (seen: a diagonal, a 0.33 mm
    vertical stub, then a horizontal: 135 + 90 deg, the 90 deg bend reported); the diagonal running on to the
    horizontal is one 135 deg bend. Both bends must be plain (two tracks, not on a pad or via of the net) and nothing
    may join the piece mid-way; done only if the extended pieces keep their clearance, the new bend is no sharper than
    135 deg (or than the sharper old one) and the bends at their far ends don't get sharper. The path keeps its ends,
    so it is safe on nets restored as routed (:func:`corner_touch_up`).
    """
    clearance, edge_clearance = _design_clearances(project)
    changes: list[str] = []
    skip: set[frozenset[tuple[int, int]]] = set()
    for _ in range(CORNER_FIX_MAX_PASSES):
        copper = _copper(project.pcb)
        obstacles = copper.obstacles()
        ends = _polyline_ends(copper)
        done = False
        for s in copper.segs:
            length = math.dist(s.a, s.b)
            key = _piece_key(s.a, s.b)
            if key in skip or not tracks.EPS < length <= STUB_COLLAPSE_MM:
                continue
            sides = []
            for p in (s.a, s.b):
                others = [(t, e) for t, e in ends.get((s.net, s.layer, tracks._key(p)), []) if t is not s]
                if len(others) != 1 or copper.on_pad_or_via(p, s.net, s.layer):
                    break
                sides.append((p, *others[0]))
            if len(sides) != 2 or sides[0][1] is sides[1][1]:
                continue
            (pa, ta, ea), (pb, tb, eb) = sides
            a, b = _far(ta, ea), _far(tb, eb)
            before = min(tracks.interior_angle(a, pa, pb), tracks.interior_angle(pa, pb, b))
            if before >= tracks.MIN_CORNER_DEG - CORNER_TOLERANCE_DEG:
                continue  # nothing sharp_corners would report
            skip.add(key)
            x = _line_cross(a, (pa[0] - a[0], pa[1] - a[1]), b, (pb[0] - b[0], pb[1] - b[1]))
            if (
                x is None
                or (x[0] - a[0]) * (pa[0] - a[0]) + (x[1] - a[1]) * (pa[1] - a[1]) <= 0  # ahead of both pieces
                or (x[0] - b[0]) * (pb[0] - b[0]) + (x[1] - b[1]) * (pb[1] - b[1]) <= 0
                or max(math.dist(x, pa), math.dist(x, pb)) > STUB_MOVE_MM
            ):
                continue
            after = tracks.interior_angle(a, x, b)
            if after < min(tracks.MIN_CORNER_DEG, before) - 1e-6:
                continue
            mid = [  # something joining the piece between its ends (a T, a via or pad centre on it)
                q
                for t in copper.segs_by_net.get(s.net, [])
                if t.layer == s.layer and t is not s
                for q in (t.a, t.b)
                if min(math.dist(q, pa), math.dist(q, pb)) > tracks.EPS
            ] + [c for c, _ in copper.vias_by_net.get(s.net, [])]
            mid += [o.centre for o in copper.pads_by_net.get(s.net, []) if s.layer in o.layers]
            if any(tracks.point_seg_distance(q, pa, pb) <= s.width / 2 for q in mid):
                continue
            ok = _fits(copper, obstacles, ta, a, x, clearance, edge_clearance) and _fits(
                copper, obstacles, tb, b, x, clearance, edge_clearance
            )
            for seg, far, old in ((ta, a, pa), (tb, b, pb)):  # the bends at the far ends
                nxt = _corner_at(ends, copper, seg, far)
                if nxt is not None:
                    was = tracks.interior_angle(old, far, nxt)
                    ok = ok and tracks.interior_angle(x, far, nxt) >= min(tracks.MIN_CORNER_DEG, was) - 1e-6
            if not ok:
                continue
            _set_end(ta, ea, x)
            _set_end(tb, eb, x)
            _drop(project.pcb, _source(s))
            changes.append(
                f"{s.net} {length:.2f} mm piece between bends at ({_mm(pa[0])}, {_mm(pa[1])}) dropped:"
                f" {before:.1f} -> {after:.1f} deg"
            )
            done = True
            break
        if not done:
            break
    return changes


def straighten_via_exits(project: KicadProject) -> list[str]:
    """Replace the stubs a track takes inside a via's copper by one piece from the via centre.

    The router can leave a track a few short pieces inside a via before it heads off (seen: a diagonal and a 0.14 mm
    stub, their corner 0.302 mm from a 0.3 mm via's centre, a 92 deg bend the via copper all but covers). Walking out
    of the via along a plain polyline, every corner within via radius + half the track width is dropped and the first
    piece reaching beyond starts at the via centre, where such a chain hides a bend sharp_corners reports. If that
    straight piece would hit something, a new piece leaves the via at 45 deg to it and joins it partway (a 135 deg
    bend). Done only if the new copper keeps its clearance and no bend gets sharper than 135 deg (or than it was).
    """
    clearance, edge_clearance = _design_clearances(project)
    changes: list[str] = []
    for _ in range(CORNER_FIX_MAX_PASSES):
        copper = _copper(project.pcb)
        obstacles = copper.obstacles()
        ends = _polyline_ends(copper)
        done = False
        for _, centre, radius, net in copper.vias:
            for s in copper.segs_by_net.get(net, []):
                start = next((e for e, p in (("start", s.a), ("end", s.b)) if math.dist(p, centre) <= radius), None)
                if start is None:
                    continue
                chain, seg, end = [(s, start)], s, start
                while True:  # walk outwards while the corners stay inside the via copper
                    p = _far(seg, end)
                    if math.dist(p, centre) > radius + seg.width / 2:
                        break
                    others = [(t, e) for t, e in ends.get((net, seg.layer, tracks._key(p)), []) if t is not seg]
                    if len(others) != 1:
                        chain = []
                        break
                    seg, end = others[0]
                    chain.append((seg, end))
                if len(chain) < 2:
                    continue
                points = [s.a if start == "start" else s.b] + [_far(piece, e) for piece, e in chain]
                corners = [
                    tracks.interior_angle(points[i - 1], points[i], points[i + 1])
                    for i in range(1, len(points) - 1)
                    if not copper.on_pad_or_via(points[i], net, s.layer)
                ]
                if not corners or min(corners) >= tracks.MIN_CORNER_DEG - CORNER_TOLERANCE_DEG:
                    continue  # nothing sharp_corners would report
                last, last_end = chain[-1]
                q = _far(last, last_end)
                old_from = last.a if last_end == "start" else last.b
                nxt = _corner_at(ends, copper, last, q)
                direct = _fits(copper, obstacles, last, centre, q, clearance, edge_clearance) and (
                    nxt is None
                    or tracks.interior_angle(centre, q, nxt)
                    >= min(tracks.MIN_CORNER_DEG, tracks.interior_angle(old_from, q, nxt)) - 1e-6
                )
                join = None  # else: leave the via at 45 deg to the last piece and join it partway (a 135 deg bend)
                length = math.dist(old_from, q)
                if not direct and length > tracks.EPS:
                    u = ((q[0] - old_from[0]) / length, (q[1] - old_from[1]) / length)
                    t = (centre[0] - old_from[0]) * u[0] + (centre[1] - old_from[1]) * u[1]
                    foot = (old_from[0] + t * u[0], old_from[1] + t * u[1])
                    along = t + math.dist(centre, foot)
                    x = (old_from[0] + along * u[0], old_from[1] + along * u[1])
                    if tracks.EPS < along < length - tracks.EPS and _fits(
                        copper, obstacles, last, centre, x, clearance, edge_clearance
                    ):
                        join = x
                if not direct and join is None:
                    continue
                for piece, _ in chain[:-1]:
                    _drop(project.pcb, _source(piece))
                if direct:
                    _set_end(last, last_end, centre)
                else:
                    assert join is not None
                    _set_end(last, last_end, join)
                    project.pcb.append(_segment_node(_source(last), centre, join, last.width))
                changes.append(
                    f"{net} via at ({_mm(centre[0])}, {_mm(centre[1])}): {len(chain) - 1} stub(s) inside the via dropped"
                )
                done = True
                break
            if done:
                break
        if not done:
            break
    return changes


def corner_touch_up(project: KicadProject) -> list[str]:
    """The corner fixes that never cut a path (:func:`straighten_via_exits`, :func:`snap_bends_to_45`,
    :func:`collapse_bend_stubs`, :func:`tuck_corners_into_pads`): run again on
    the finished board, where a net restored as routed (``restore_nets``) skipped the clean-up. The caller re-checks
    connectivity with KiCad and undoes them if anything got disconnected."""
    return (
        straighten_via_exits(project)
        + snap_bends_to_45(project)
        + collapse_bend_stubs(project)
        + tuck_corners_into_pads(project)
    )


TUCK_REACH_MM = 0.5  # a sharp bend this close to a pad of its net may move onto it


TUCK_STEPS = 20


TUCK_GRID_MM = 0.025  # spots tried on the pad when none on the line to its centre fits


TUCK_GRID_MAX = 60  # grid steps along the pad's longer side at most


def tuck_corners_into_pads(project: KicadProject) -> list[str]:
    """Move a sharp bend or an acute junction lying just outside a pad of its net onto that pad.

    The router sometimes turns a track, or joins several, a hair before the pad they end at (seen: a USB-C D- pad,
    45.8 deg 0.1 mm off the pad; three VCC tracks meeting at 41 deg beside a VBUS pad); on the pad the vertex is
    pad copper, which neither KiCad's ``track_angle`` nor :func:`sharp_corners` judges. Every track ending at the
    vertex moves with it, towards the pad centre, to the first spot inside the pad where all of them keep their
    clearance; if none on that line fits, to the nearest spot on the pad that does (seen: a hairpin at a 0.5 mm
    pitch DAC pin, whose way to the pad centre passed the neighbouring pin).
    """
    clearance, edge_clearance = _design_clearances(project)
    changes = []
    for net, p, angle in _acute_vertices(project):
        copper = _copper(project.pcb)
        at = [
            s
            for s in copper.segs_by_net.get(net, [])
            if math.dist(s.a, p) <= tracks.EPS or math.dist(s.b, p) <= tracks.EPS
        ]
        layers = {s.layer for s in at}
        if len(at) < 2 or len(layers) != 1:
            continue
        layer = next(iter(layers))
        pads = [o for o in copper.pads_by_net.get(net, []) if layer in o.layers and o.distance(p, p) <= TUCK_REACH_MM]
        half = max(s.width for s in at) / 2

        def deep(q: tracks.Point, o: tracks.Obstacle, half: float = half) -> bool:  # the vertex's copper all on the pad
            return all(
                o.distance(c, c) <= tracks.EPS
                for c in ((q[0] + half, q[1]), (q[0] - half, q[1]), (q[0], q[1] + half), (q[0], q[1] - half))
            )

        if not pads or any(deep(p, o) for o in pads):
            continue
        pad = min(pads, key=lambda o: o.distance(p, p))
        fars = [s.b if math.dist(s.a, p) <= tracks.EPS else s.a for s in at]
        others = [o for o in copper.obstacles() if o.net != net and layer in o.layers]
        pairs = list(zip(at, fars, strict=True))
        ends = _polyline_ends(copper)
        nexts = [_corner_at(ends, copper, s, f) for s, f in pairs]  # the bends at the tracks' far ends

        def clear(
            q: tracks.Point,
            p: tracks.Point = p,
            pairs: list[tuple[tracks.Seg, tracks.Point]] = pairs,
            nexts: list[tracks.Point | None] = nexts,
            others: list[tracks.Obstacle] = others,
            edges: list[tracks.Obstacle] = copper.edges,
        ) -> bool:
            for (_, f), nxt in zip(pairs, nexts, strict=True):  # no far bend gets sharper than 135 deg (or than it was)
                if (
                    nxt is not None
                    and tracks.interior_angle(q, f, nxt)
                    < min(tracks.MIN_CORNER_DEG, tracks.interior_angle(p, f, nxt)) - 1e-6
                ):
                    return False
            return all(
                o.distance(f, q) >= clearance + s.width / 2 - tracks.EPS for s, f in pairs for o in others
            ) and all(e.distance(f, q) >= edge_clearance + s.width / 2 for s, f in pairs for e in edges)

        line = [
            (p[0] + (pad.centre[0] - p[0]) * k / TUCK_STEPS, p[1] + (pad.centre[1] - p[1]) * k / TUCK_STEPS)
            for k in range(1, TUCK_STEPS + 1)
        ]
        spot = next((q for q in line if deep(q, pad) and clear(q)), None)
        if spot is None:  # off the line to the centre (a hairpin beside a fine-pitch neighbour): nearest spot that fits
            x0, y0, x1, y1 = pad.box
            step = max(TUCK_GRID_MM, max(x1 - x0, y1 - y0) / TUCK_GRID_MAX)
            grid = [
                (x0 + i * step, y0 + j * step)
                for i in range(int((x1 - x0) / step) + 1)
                for j in range(int((y1 - y0) / step) + 1)
            ]
            spot = next((q for q in sorted(grid, key=lambda q: math.dist(q, p)) if deep(q, pad) and clear(q)), None)
        if spot is None:  # no room on the pad: a track may end at a via of the net next to the vertex instead
            if (moved := _reanchor_at_via(copper, net, p, at, fars, others, clearance)) is not None:
                changes.append(f"{net} track meeting others at {angle:.1f} deg at ({_mm(p[0])}, {_mm(p[1])}) {moved}")
            continue
        for s in at:
            key = "start" if math.dist(s.a, p) <= tracks.EPS else "end"
            node = sexpr.child(_source(s), key)
            assert node is not None
            node[1:3] = [Sym(_mm(spot[0])), Sym(_mm(spot[1]))]
        changes.append(
            f"{net} {len(at)} track(s) meeting at {angle:.1f} deg at ({_mm(p[0])}, {_mm(p[1])}) moved onto its pad"
        )
    return changes


REANCHOR_REACH_MM = 0.6  # a via of the net this close to an acute vertex may take one of its tracks


def _reanchor_at_via(
    copper: _Copper,
    net: str,
    p: tracks.Point,
    at: list[tracks.Seg],
    fars: list[tracks.Point],
    others: list[tracks.Obstacle],
    clearance: float,
) -> str | None:
    """Re-point one track of an acute vertex from the vertex to a nearby via of its net (the vertex keeps its other
    tracks, so nothing comes apart); the first that keeps its clearance, shortest track first. Returns the change."""
    vias = [c for c, r in copper.vias_by_net.get(net, []) if tracks.EPS < math.dist(c, p) <= REANCHOR_REACH_MM]
    for c in sorted(vias, key=lambda c: (math.dist(c, p), c)):
        for s, f in sorted(zip(at, fars, strict=True), key=lambda sf: math.dist(sf[1], p)):
            if math.dist(f, c) <= tracks.EPS or any(
                math.dist(c, t.a) <= tracks.EPS or math.dist(c, t.b) <= tracks.EPS for t in [s]
            ):
                continue
            if all(o.distance(f, c) >= clearance + s.width / 2 - tracks.EPS for o in others):
                key = "start" if math.dist(s.a, p) <= tracks.EPS else "end"
                node = sexpr.child(_source(s), key)
                assert node is not None
                node[1:3] = [Sym(_mm(c[0])), Sym(_mm(c[1]))]
                return f"re-pointed to the via at ({_mm(c[0])}, {_mm(c[1])})"
    return None


def _acute_vertices(project: KicadProject) -> list[tuple[str, tracks.Point, float]]:
    """(net, point, smallest angle) of every track vertex with a bend sharper than 135 deg (two tracks, also where
    :func:`sharp_corners` lets a nearby pad or a third track end count as cover) or two of its tracks meeting at an
    acute angle (a junction of three or more)."""
    out = {(n, (round(q[0], 6), round(q[1], 6))): a for n, q, a in sharp_corners(project)}
    copper = _copper(project.pcb)
    for net, segs in copper.segs_by_net.items():
        ends: dict[tuple[str, tuple[int, int]], list[tuple[tracks.Point, tracks.Point]]] = {}
        for s in segs:
            for e, f in ((s.a, s.b), (s.b, s.a)):
                ends.setdefault((s.layer, tracks._key(e)), []).append((e, f))
        for group in ends.values():
            if len(group) < 2:
                continue
            p = group[0][0]
            worst = min(tracks.interior_angle(f, p, g) for (_, f), (_, g) in itertools.combinations(group, 2))
            if worst < (tracks.MIN_CORNER_DEG - CORNER_TOLERANCE_DEG if len(group) == 2 else ACUTE_DEG):
                key = (net, (round(p[0], 6), round(p[1], 6)))
                out[key] = min(worst, out.get(key, 180.0))
    return [(n, q, a) for (n, q), a in sorted(out.items())]


ACUTE_DEG = 89.0  # KiCad's track_angle minimum (the house rule)


def _piece_key(a: tracks.Point, b: tracks.Point) -> frozenset[tuple[int, int]]:
    return frozenset((tracks._key(a), tracks._key(b)))


def _anchored_pieces(copper: _Copper, run: tracks.Chain, mine: set[int]) -> set[int]:
    """Indices of the run's pieces (``points[i]``-``points[i + 1]``) that something else joins mid-run.

    KiCad connects a track end lying on another track's copper (a T), a via whose centre is on a
    track and a pad whose centre is on one; :func:`tracks.chains` only splits runs at shared end
    points, so rerouting such a piece would cut the T-branch, via or pad off.
    """
    pts = run.points
    spots = [
        e for t in copper.segs_by_net.get(run.net, []) if t.layer == run.layer and id(t) not in mine for e in (t.a, t.b)
    ]
    spots = [q for q in spots if min(math.dist(q, pts[0]), math.dist(q, pts[-1])) > tracks.EPS]
    ends = (pts[0], pts[-1])
    # vias and pads the run ends on are kept by the reroute anyway (it keeps the run's ends)
    spots += [c for c, r in copper.vias_by_net.get(run.net, []) if all(math.dist(e, c) > r + tracks.EPS for e in ends)]
    spots += [
        o.centre
        for o in copper.pads_by_net.get(run.net, [])
        if run.layer in o.layers and all(o.distance(e, e) > tracks.ON_EDGE_MM for e in ends)
    ]
    return {
        i
        for i, (a, b) in enumerate(itertools.pairwise(pts))
        if any(tracks.point_seg_distance(q, a, b) <= run.width_at(a, b) / 2 for q in spots)
    }


def _route_with_stub(
    start: tracks.Point,
    goal: tracks.Point,
    before: tracks.Point | None,
    after: tracks.Point | None,
    free: Callable[[tracks.Point], bool],
    fits: Callable[[tracks.Point, tracks.Point], bool],
    leaves_ok: Callable[[tracks.Point, tracks.Point, tracks.Point | None], bool],
) -> list[tracks.Point] | None:
    """A grid route start -> goal for :func:`reroute_tracks`; where no grid heading may leave
    ``start`` (a junction on a track that is not quite axis-aligned: every heading meets it at
    89 deg or less), one short off-grid stub leaves it first and the grid route continues from
    the stub's end, turning at most 45 deg there."""

    def end_ok(node: tracks.Point, goal: tracks.Point) -> bool:
        return fits(node, goal) and leaves_ok(goal, node, after)

    heads = {
        d for d, (dx, dy) in enumerate(tracks.GRID_HEADINGS) if leaves_ok(start, (start[0] + dx, start[1] + dy), before)
    }
    if heads:
        return tracks.grid_route(start, goal, free, REROUTE_STEP_MM, heads, end_ok)
    # the directions the junction rule allows can be a sliver (1 deg): sample finely, then try the
    # middle of each allowed range first, the rest nearest the goal first
    towards = math.atan2(goal[1] - start[1], goal[0] - start[0])
    sampled = [math.radians(k * REROUTE_STUB_SAMPLE_DEG) for k in range(round(360 / REROUTE_STUB_SAMPLE_DEG))]
    allowed = [a for a in sampled if leaves_ok(start, (start[0] + math.cos(a), start[1] + math.sin(a)), before)]
    ranges: list[list[float]] = []
    for a in allowed:
        if ranges and a - ranges[-1][-1] <= math.radians(1.5 * REROUTE_STUB_SAMPLE_DEG):
            ranges[-1].append(a)
        else:
            ranges.append([a])
    angles = [(r[0] + r[-1]) / 2 for r in ranges] + sorted(
        allowed[:: round(REROUTE_STUB_ANGLE_STEP_DEG / REROUTE_STUB_SAMPLE_DEG)],
        key=lambda a: abs(math.remainder(a - towards, 2 * math.pi)),
    )
    for a in angles[:REROUTE_STUB_TRIES]:  # each try is a full maze search
        direction = (math.cos(a), math.sin(a))
        for length in REROUTE_STUB_MM:
            stub = (start[0] + length * direction[0], start[1] + length * direction[1])
            if not (free(stub) and fits(start, stub)):
                continue
            turns = {
                d
                for d, (dx, dy) in enumerate(tracks.GRID_HEADINGS)
                if tracks.interior_angle(start, stub, (stub[0] + dx, stub[1] + dy)) >= tracks.MIN_CORNER_DEG - 0.5
            }
            route = tracks.grid_route(stub, goal, free, REROUTE_STEP_MM, turns, end_ok)
            if route is not None:
                return [start, *route]
    return None


def sharp_corners(project: KicadProject) -> list[tuple[str, tracks.Point, float]]:
    """``(net, position, interior angle)`` of every bend sharper than 135 deg between exactly two tracks.

    The bend half of the corner rule (KiCad's DRC covers acute angles, see
    :func:`strict_track_rules`); uses the tolerance KiCad needs for its own rounding.
    Bends lying on a pad or via of their net don't count: the pad copper covers them.
    """
    copper = _copper(project.pcb)
    ends: dict[tuple[str, str, tuple[int, int]], list[tuple[tracks.Point, tracks.Point]]] = {}
    for s in copper.segs:
        ends.setdefault((s.net, s.layer, tracks._key(s.a)), []).append((s.a, s.b))
        ends.setdefault((s.net, s.layer, tracks._key(s.b)), []).append((s.b, s.a))
    out = []
    for (net, layer, _), pairs in ends.items():
        if len(pairs) == 2:
            (p, a), (_, b) = pairs
            angle = tracks.interior_angle(a, p, b)
            if angle < tracks.MIN_CORNER_DEG - CORNER_TOLERANCE_DEG and not copper.on_pad_or_via(p, net, layer):
                out.append((net, p, angle))
    return sorted(out)
