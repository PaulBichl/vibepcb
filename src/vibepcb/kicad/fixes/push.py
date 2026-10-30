"""Pushing tracks clear of other copper, separating ends on pads, antennas."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import (
    COPPER_LAYERS,
    KicadProject,
    _design_clearances,
    _design_min_track_width,
    _mm,
    _segment_node,
)
from vibepcb.kicad.fixes.board import PAD_EDGE_MARGIN_MM
from vibepcb.kicad.fixes.copper import _Copper, _copper, _drop, _source
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibepcb.core.sexpr import Node


NECK_MAX_LEN_MM = 1.0  # longer pieces are not narrowed (a power track keeps its width over any real length)


PUSH_STEPS_MM = tuple(round(0.02 * k, 2) for k in range(1, 16))  # up to 0.3 mm


def push_tracks_clear(project: KicadProject) -> list[str]:
    """Slide track pieces sideways that sit closer than the clearance to another net's copper.

    The router occasionally squeezes a track past a via or between two others a few
    hundredths of a mm too close. A piece whose ends are free corners (two tracks meet
    there, no pad or via) or lie on a pad of its net moves perpendicular to itself, its
    neighbours following, in small steps up to 0.3 mm, either side; an end on a pad must stay
    inside it (KiCad still connects it), every changed piece must keep the clearance and no
    corner may get sharper than 135 deg (or than it was). A piece of at most
    :data:`NECK_MAX_LEN_MM` wedged between two obstacles, where no push clears both, is necked
    down (widest width first, to the board's minimum track width) and pushed as little as needed.
    """
    clearance, edge_clearance = _design_clearances(project)
    min_width = _design_min_track_width(project)
    changes: list[str] = []
    for _ in range(3):  # a push can open room for the next one
        copper = _copper(project.pcb)
        obstacles = copper.obstacles()
        pushed = False
        for s in copper.segs:
            others = [o for o in obstacles if o.net != s.net and s.layer in o.layers]
            if all(o.distance(s.a, s.b) >= clearance + s.width / 2 - tracks.EPS / 10 for o in others):
                continue
            same = [t for t in copper.segs_by_net.get(s.net, []) if t.layer == s.layer and t is not s]
            # per end: the neighbour track meeting there and its far end (it follows), or the pad
            # the end lies on (the end may slide inside it); a via or junction fixes it
            ends: list[tuple[tracks.Seg, tracks.Point] | tracks.Obstacle] = []
            for e in (s.a, s.b):
                meet = [t for t in same if min(math.dist(t.a, e), math.dist(t.b, e)) <= tracks.EPS]
                on_via = any(math.dist(e, c) <= r + tracks.EPS for c, r in copper.vias_by_net.get(s.net, []))
                pad = next(
                    (o for o in copper.pads_by_net.get(s.net, []) if s.layer in o.layers and o.distance(e, e) <= 0),
                    None,
                )
                if pad is not None and not on_via:
                    ends.append(pad)
                elif len(meet) == 1 and not copper.on_pad_or_via(e, s.net, s.layer):
                    t = meet[0]
                    ends.append((t, t.b if math.dist(t.a, e) <= tracks.EPS else t.a))
            if len(ends) != 2:
                continue
            d = (s.b[0] - s.a[0], s.b[1] - s.a[1])
            n = math.hypot(*d)
            if n <= tracks.EPS:
                continue
            normal = (-d[1] / n, d[0] / n)

            edges = [e for e in copper.edges if s.layer in e.layers]

            def ok(
                a: tracks.Point,
                b: tracks.Point,
                width: float,
                others: list[tracks.Obstacle] = others,
                edges: list[tracks.Obstacle] = edges,
            ) -> bool:
                return all(o.distance(a, b) >= clearance + width / 2 for o in others) and all(
                    e.distance(a, b) >= edge_clearance + width / 2 for e in edges
                )

            corners = [
                tracks.interior_angle(end[1], p, q)
                for end, p, q in ((ends[0], s.a, s.b), (ends[1], s.b, s.a))
                if not isinstance(end, tracks.Obstacle)
            ]

            def attempt(
                step: float,
                sign: int,
                width: float,
                s: tracks.Seg = s,
                ends: list[tuple[tracks.Seg, tracks.Point] | tracks.Obstacle] = ends,
                normal: tuple[float, float] = normal,
                corners: list[float] = corners,
                ok: Callable[..., bool] = ok,
            ) -> bool:
                off = (normal[0] * step * sign, normal[1] * step * sign)
                na = (s.a[0] + off[0], s.a[1] + off[1])
                nb = (s.b[0] + off[0], s.b[1] + off[1])
                if not ok(na, nb, width):
                    return False
                new_corners, moves = [], [(s, s.a, na), (s, s.b, nb)]
                for end, old_p, new_p, other in ((ends[0], s.a, na, nb), (ends[1], s.b, nb, na)):
                    if isinstance(end, tracks.Obstacle):  # stays inside its pad, clear of the edge
                        m = PAD_EDGE_MARGIN_MM
                        probes = ((new_p[0] + m, new_p[1]), (new_p[0] - m, new_p[1]),
                                  (new_p[0], new_p[1] + m), (new_p[0], new_p[1] - m))  # fmt: skip
                        if any(end.distance(q, q) > 0 for q in probes):
                            return False
                        continue
                    t, far = end
                    if step and not ok(far, new_p, t.width):
                        return False
                    new_corners.append(tracks.interior_angle(far, new_p, other))
                    moves.append((t, old_p, new_p))
                # no corner sharper than 135 deg, or than it already was
                if new_corners and min(new_corners) < min(tracks.MIN_CORNER_DEG, *corners) - 1e-6:
                    return False
                for seg, old, new_point in moves:
                    key = "start" if math.dist(seg.a, old) <= tracks.EPS else "end"
                    node = sexpr.child(_source(seg), key)
                    assert node is not None
                    node[1:3] = [Sym(_mm(new_point[0])), Sym(_mm(new_point[1]))]
                where = f"{s.net} track at ({_mm(s.a[0])}, {_mm(s.a[1])})"
                if width < s.width:
                    width_node = sexpr.child(_source(s), "width")
                    assert width_node is not None
                    width_node[1] = Sym(_mm(width))
                    changes.append(f"{where} necked {_mm(s.width)} -> {_mm(width)} mm"
                                   + (f", pushed {step} mm" if step else ""))  # fmt: skip
                else:
                    changes.append(f"{where} pushed {step} mm clear")
                return True

            pushed = any(attempt(step, sign, s.width) for step in PUSH_STEPS_MM for sign in (1, -1))
            if not pushed and math.dist(s.a, s.b) <= NECK_MAX_LEN_MM:
                # wedged between two obstacles: neck the short piece down (like a router next to a pad),
                # widest width first, the smallest push that clears it
                widths = []
                w = math.floor(s.width * 100 - 1e-6) / 100
                while w >= min_width - 1e-9:
                    widths.append(round(w, 2))
                    w -= 0.01
                pushed = any(
                    attempt(step, sign, w)
                    for w in widths
                    for step in (0.0, *PUSH_STEPS_MM)
                    for sign in ((1,) if step == 0 else (1, -1))
                )
            if pushed:
                break  # copper changed: rebuild before the next one
        if not pushed:
            break
    return changes


SEPARATE_STEPS_MM = (0.05, 0.1, 0.15, 0.2, 0.3)


def separate_ends_on_pads(project: KicadProject) -> list[str]:
    """Pull apart track ends that meet at an acute angle on a pad or via of their net.

    Two routes entering one pad often end on the same point; KiCad's ``track_angle`` rule then
    measures the angle between them although the pad copper joins them anyway. Each end but
    the first moves back along its own track (no new bend) while it stays inside the same pad,
    or the same via, as the junction, so it keeps its connection.
    """
    copper = _copper(project.pcb)
    groups: dict[tuple[str, str, tuple[int, int]], list[tuple[tracks.Seg, int]]] = {}
    for s in copper.segs:
        for k, e in enumerate((s.a, s.b)):
            groups.setdefault((s.net, s.layer, tracks._key(e)), []).append((s, k))
    changes = []
    for (net, layer, _), ends in groups.items():
        if len(ends) < 2:
            continue
        j = ends[0][0].a if ends[0][1] == 0 else ends[0][0].b
        if not copper.on_pad_or_via(j, net, layer):
            continue
        far = [s.b if k == 0 else s.a for s, k in ends]
        if tracks.min_branch_angle(j, far) >= tracks.MIN_JUNCTION_DEG:
            continue
        moved = 0
        for (s, k), other in list(zip(ends, far, strict=True))[1:]:
            length = math.dist(j, other)
            for step in SEPARATE_STEPS_MM:
                if step >= length - tracks.EPS:
                    break
                q = (j[0] + (other[0] - j[0]) * step / length, j[1] + (other[1] - j[1]) * step / length)
                # on the same pad / via as the junction: one beside it (a via next to a pad) would cut
                # the link the junction made between the two
                if copper.attached(q, s) and copper.same_anchor(j, q, net, layer):
                    node = sexpr.child(_source(s), "start" if k == 0 else "end")
                    assert node is not None
                    node[1:3] = [Sym(_mm(q[0])), Sym(_mm(q[1]))]
                    moved += 1
                    break
        if moved:
            changes.append(f"{net} at ({_mm(j[0])}, {_mm(j[1])}): {moved} track end(s) separated on the pad/via")
    return changes + _pull_back_overlapping_ends(project)


PULL_BACK_STEPS_MM = tuple(round(0.05 * k, 2) for k in range(1, 21))  # up to 1 mm


def _pull_back_overlapping_ends(project: KicadProject) -> list[str]:
    """A track ending on a pad/via whose copper overlaps another track of its net at an acute
    angle (KiCad measures it) retreats along its own line until they no longer touch, as long
    as its end stays on the same pad/via."""
    copper = _copper(project.pcb)
    changes = []
    touched: set[int] = set()
    for s in copper.segs:
        if id(s) in touched:
            continue
        for k, (end, far) in enumerate(((s.a, s.b), (s.b, s.a))):
            if not copper.attached(end, s):
                continue
            crossing = [
                t
                for t in copper.segs_by_net.get(s.net, [])
                if t is not s
                and t.layer == s.layer
                and id(t) not in touched
                and min(math.dist(e, f) for e in (s.a, s.b) for f in (t.a, t.b)) > tracks.EPS  # no shared corner
                and tracks.seg_seg_distance(s.a, s.b, t.a, t.b) <= (s.width + t.width) / 2
                and _line_angle(s, t) < tracks.MIN_JUNCTION_DEG - 1
                # only where t is joined to the same pad/via anyway: else the overlap may be its link
                and any(copper.same_anchor(end, f, s.net, s.layer) for f in (t.a, t.b))
            ]
            if not crossing:
                continue
            length = math.dist(end, far)
            for step in PULL_BACK_STEPS_MM:
                if step >= length - tracks.EPS:
                    break
                q = (end[0] + (far[0] - end[0]) * step / length, end[1] + (far[1] - end[1]) * step / length)
                if not (copper.attached(q, s) and copper.same_anchor(end, q, s.net, s.layer)):
                    break
                if all(tracks.seg_seg_distance(q, far, t.a, t.b) > (s.width + t.width) / 2 for t in crossing):
                    node = sexpr.child(_source(s), ("start", "end")[k])
                    assert node is not None
                    node[1:3] = [Sym(_mm(q[0])), Sym(_mm(q[1]))]
                    touched.add(id(s))
                    changes.append(f"{s.net} at ({_mm(end[0])}, {_mm(end[1])}): end pulled back off a crossing track")
                    break
            break
    return changes


def _line_angle(s: tracks.Seg, t: tracks.Seg) -> float:
    """Angle between the two tracks' lines in degrees, 0..90."""
    u, v = (s.b[0] - s.a[0], s.b[1] - s.a[1]), (t.b[0] - t.a[0], t.b[1] - t.a[1])
    nu, nv = math.hypot(*u), math.hypot(*v)
    if nu == 0 or nv == 0:
        return 90.0
    return math.degrees(math.acos(min(1.0, abs(u[0] * v[0] + u[1] * v[1]) / (nu * nv))))


def _trim_stubs_past_pads(copper: _Copper, connected: Callable[[tracks.Point, tracks.Seg], bool]) -> int:
    """Cut a track end that runs past a pad of its net (loose there, the pad centre on the track) back to the pad.

    A route that starts beyond its pad (at an escape end the clean-up then removed as redundant) crosses
    the pad and leaves a stub; dropping the whole segment would cut the pad off, it is its only link.
    """
    trimmed = 0
    for seg in copper.segs:
        mates = [t for t in copper.segs_by_net.get(seg.net, []) if t is not seg and t.layer == seg.layer]

        def free(p: tracks.Point, seg: tracks.Seg = seg, mates: list[tracks.Seg] = mates) -> bool:
            return not connected(p, seg) and not any(
                tracks.point_seg_distance(p, t.a, t.b) <= t.width / 2
                or min(math.dist(p, t.a), math.dist(p, t.b)) <= seg.width / 2
                for t in mates
            )

        loose = [p for p in (seg.a, seg.b) if free(p)]
        if len(loose) != 1:
            continue
        pad = next(
            (
                o
                for o in copper.pads_by_net.get(seg.net, [])
                if seg.layer in o.layers and tracks.on_track_body(o.centre, seg.a, seg.b, seg.width / 2)
            ),
            None,
        )
        other = seg.b if loose[0] == seg.a else seg.a
        if pad is None or math.dist(pad.centre, loose[0]) <= tracks.EPS or math.dist(pad.centre, other) <= tracks.EPS:
            continue
        node = sexpr.child(_source(seg), "start" if loose[0] == seg.a else "end")
        assert node is not None
        node[1:3] = [Sym(_mm(pad.centre[0])), Sym(_mm(pad.centre[1]))]
        if loose[0] == seg.a:
            seg.a = pad.centre
        else:
            seg.b = pad.centre
        trimmed += 1
    return trimmed


def remove_antennas(project: KicadProject, *, filled_zones: bool = False) -> list[str]:
    """Delete track stubs with a loose end and vias that connect fewer than two layers.

    A loose end is one that touches no pad, via, other track of its net or (for a poured
    net) its zone. Removal repeats until nothing is left dangling. Nothing of a net goes if its
    pads would be less connected afterwards (a track crossing a pad's centre carries it).
    """
    copper = _copper(project.pcb, filled_zones=filled_zones)

    def connected(p: tracks.Point, s: tracks.Seg) -> bool:
        return copper.attached(p, s) or copper.in_zone(p, s.net, s.layer)

    trimmed = _trim_stubs_past_pads(copper, connected)
    removed_segs = [copper.segs[i] for i in tracks.dangling(copper.segs, connected)]
    # islands: tracks/vias joined to no pad of their net (and, on a poured net, not to its pour)
    island_vias: set[int] = set()
    for net, segs in copper.segs_by_net.items():
        net_vias = [v for v in copper.vias if v[3] == net]
        vias_i, segs_i = tracks.unreached(copper.pads_by_net.get(net, []), [(c, r) for _, c, r, _ in net_vias], segs)
        if any(copper.zones.get((net, layer)) for layer in COPPER_LAYERS):
            continue  # a poured net's islands may hang on the pour: dangling() and KiCad judge those
        removed_segs += [s for k, s in enumerate(segs) if k in segs_i and all(s is not r for r in removed_segs)]
        island_vias |= {id(net_vias[v][0]) for v in vias_i}
    alive = [s for s in copper.segs if all(s is not r for r in removed_segs)]
    via_out: set[int] = set()
    # a via on one layer only may still be what joins that layer's tracks ending inside it: they get a short piece
    # to its centre (inside its copper) when it goes
    bridges: dict[int, list[tuple[tracks.Seg, tracks.Seg]]] = {}  # via id -> (bridge, the track it extends)
    for node, centre, radius, net in copper.vias:
        if id(node) in island_vias:
            via_out.add(id(node))
            continue
        layers = {
            layer
            for layer in COPPER_LAYERS
            if copper.in_zone(centre, net, layer)
            or any(s.net == net and s.layer == layer and tracks.via_takes(centre, radius, s) for s in alive)
            or any(o.net == net and layer in o.layers and o.distance(centre, centre) <= radius for o in copper.pads)
        }
        if len(layers) < 2:
            via_out.add(id(node))
            bridges[id(node)] = [
                (tracks.Seg(end, centre, s.width, s.layer, net), s)
                for s in alive
                if s.net == net and s.layer in layers
                for end in (s.a, s.b)
                if 1e-6 < math.dist(end, centre) <= radius - tracks.EPS
            ]
    # a "loose" end may still carry a pad: KiCad also connects a pad whose centre lies on the track's body (a track
    # crossing the pad, both ends beyond it). Per net, nothing goes if the net's pads would be less connected.
    candidates_segs, candidates_vias = {id(s) for s in removed_segs}, set(via_out)
    gone_ids: set[int] = set()
    via_out = set()
    for net in {s.net for s in removed_segs} | {v[3] for v in copper.vias if id(v[0]) in candidates_vias}:
        net_pads = copper.pads_by_net.get(net, [])
        net_segs = copper.segs_by_net.get(net, [])
        net_via_list = [v for v in copper.vias if v[3] == net]

        def groups(
            seg_out: set[int],
            v_out: set[int],
            pads: list[tracks.Obstacle] = net_pads,
            segs: list[tracks.Seg] = net_segs,
            vias: list[tuple[list[Node], tracks.Point, float, str]] = net_via_list,
        ) -> list[set[int]]:
            kept = [s for s in segs if id(s) not in seg_out]
            kept += [b for v in v_out for b, s in bridges.get(v, []) if id(s) not in seg_out]
            return tracks.pad_groups(pads, [(c, r) for node, c, r, _ in vias if id(node) not in v_out], kept)

        before = groups(set(), set())
        cand_s = {id(s) for s in net_segs} & candidates_segs
        cand_v = {id(v[0]) for v in net_via_list} & candidates_vias
        if tracks.still_connected(before, groups(cand_s, cand_v)):
            gone_ids |= cand_s
            via_out |= cand_v
            continue
        # one at a time: what can go without splitting the pads goes, the rest stays
        seg_out: set[int] = set()
        v_out: set[int] = set()
        # in board order (ids differ run to run; the result must not)
        order = [("s", id(s)) for s in net_segs if id(s) in cand_s] + [
            ("v", id(v[0])) for v in net_via_list if id(v[0]) in cand_v
        ]
        for kind, item in order:
            trial = (seg_out | {item}, v_out) if kind == "s" else (seg_out, v_out | {item})
            if tracks.still_connected(before, groups(*trial)):
                seg_out, v_out = trial
        gone_ids |= seg_out
        via_out |= v_out
    removed_segs = [s for s in removed_segs if id(s) in gone_ids]
    for s in removed_segs:
        _drop(project.pcb, _source(s))
    removed_vias = 0
    gone_now = {id(s) for s in removed_segs}
    for node, _, _, _ in copper.vias:
        if id(node) in via_out:
            for bridge, s in bridges.get(id(node), []):
                if id(s) not in gone_now:
                    index = project.pcb.index(_source(s))
                    project.pcb.insert(index + 1, _segment_node(_source(s), bridge.a, bridge.b, bridge.width))
            project.pcb.remove(node)
            removed_vias += 1
    changes = []
    if trimmed:
        changes.append(f"antennas: cut {trimmed} track end(s) running past their pad back to the pad centre")
    if removed_segs:
        changes.append(f"antennas: removed {len(removed_segs)} dangling track segment(s)")
    if removed_vias:
        changes.append(f"antennas: removed {removed_vias} via(s) connecting fewer than two layers")
    return changes
