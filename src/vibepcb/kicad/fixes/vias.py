"""Via fixes: duplicates, thin vias, vias crowding other nets."""

from __future__ import annotations

import itertools
import json
import math
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import (
    COPPER_LAYERS,
    REROUTE_GRID_MARGIN_MM,
    REROUTE_STEP_MM,
    KicadProject,
    _design_clearances,
    _design_via,
    _mm,
    _num,
    _segment_node,
    log,
)
from vibepcb.kicad.fixes.copper import _Copper, _copper, _drop, _source
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from vibepcb.core.sexpr import Node


def merge_duplicate_vias(project: KicadProject) -> list[str]:
    """Keep one via where tscircuit exported several of one net on one spot or holes too close.

    Seen: a through via plus a blind F.Cu-In2 via at the same centre; moving or judging one
    of them alone (relocate_vias, remove_antennas) leaves the other in the way. The via
    spanning the most layers stays (a through via covers every layer the others joined).
    Same-net vias whose holes are closer than the board's hole-to-hole minimum (seen: 0.44 mm
    apart, overlapping copper; a drill hazard) merge too: the tracks ending in the dropped
    via are pulled to its centre and bridged to the kept one.
    """
    changes = []
    copper = _copper(project.pcb)
    spots: dict[tuple[str, tuple[int, int]], list[list[Node]]] = {}
    for node, centre, _, net in copper.vias:
        spots.setdefault((net, tracks._key(centre)), []).append(node)
    for (net, _), nodes in spots.items():
        if len(nodes) < 2:
            continue
        keep = max(nodes, key=_via_span)
        for node in nodes:
            if node is not keep:
                project.pcb.remove(node)
        at = sexpr.child(keep, "at")
        changes.append(f"via {net} at ({_num(at, 1)}, {_num(at, 2)}): {len(nodes) - 1} duplicate(s) merged")
    gap = _min_hole_to_hole(project)
    while True:  # one merge at a time: the copper changes with each
        copper = _copper(project.pcb)
        pair = next(
            (
                (v, w)
                for i, v in enumerate(copper.vias)
                for w in copper.vias[i + 1 :]
                if v[3] == w[3] and math.dist(v[1], w[1]) < (_via_drill(v[0]) + _via_drill(w[0])) / 2 + gap - tracks.EPS
            ),
            None,
        )
        if pair is None:
            break
        kept, dropped = sorted(pair, key=lambda v: -_via_span(v[0]))
        kept_at, via_net = kept[1], kept[3]
        dropped_node, dropped_at, radius = dropped[0], dropped[1], dropped[2]
        bridges: dict[str, tuple[float, list[Node]]] = {}  # layer -> (width, template) of the bridge
        for seg in copper.segs_by_net.get(via_net, []):
            for k, end in enumerate((seg.a, seg.b)):
                if math.dist(end, dropped_at) <= radius and math.dist(end, kept_at) > tracks.EPS:
                    point = sexpr.child(_source(seg), ("start", "end")[k])
                    assert point is not None
                    point[1:3] = [Sym(_mm(dropped_at[0])), Sym(_mm(dropped_at[1]))]
                    if seg.layer not in bridges or seg.width < bridges[seg.layer][0]:
                        bridges[seg.layer] = (seg.width, _source(seg))
        index = project.pcb.index(dropped_node)
        project.pcb.remove(dropped_node)
        for _, (width, template) in sorted(bridges.items()):
            project.pcb.insert(index, _segment_node(template, dropped_at, kept_at, width))
        changes.append(
            f"via {via_net} at ({_mm(dropped_at[0])}, {_mm(dropped_at[1])}): merged into the one at"
            f" ({_mm(kept_at[0])}, {_mm(kept_at[1])}) (holes closer than {gap} mm)"
        )
    return changes


def widen_thin_vias(project: KicadProject) -> list[str]:
    """Bring vias below the Default net class size (drill) or the board's minimum annular ring up to the class via.

    A trace's manual ``pcbPath`` via gets tscircuit's 0.3 mm pad on a 0.2 mm drill (the path syntax has no size): a
    ring below the minimum, and a drill that puts the whole board into the fab's smaller-via price class (JLCPCB:
    0.2 mm vs. the standard 0.3 mm). Such a via gets the class drill and the class diameter, else drill + 2 x the
    minimum ring, as far as its clearance to other nets' copper and the hole-to-hole minimum allow; one that can't
    grow is reported.
    """
    try:
        data = json.loads(project.pcb_path.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
        min_ring = float(data["board"]["design_settings"]["rules"]["min_via_annular_width"])
    except (OSError, ValueError, KeyError):
        min_ring = DEFAULT_MIN_ANNULAR_MM
    clearance, _ = _design_clearances(project)
    design_diameter, design_drill = _design_via(project)
    hole_gap = _min_hole_to_hole(project)
    changes = []
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()
    for node, centre, radius, net in copper.vias:
        drill = _via_drill(node)
        if radius - drill / 2 >= min_ring - 1e-6 and drill >= design_drill - 1e-6:
            continue
        new_drill = max(drill, design_drill)
        others = [o for o in obstacles if o.net != net]
        holes_ok = all(
            math.dist(centre, c) >= (new_drill + _via_drill(n)) / 2 + hole_gap - 1e-6
            for n, c, _, _ in copper.vias
            if n is not node and math.dist(centre, c) > tracks.EPS
        )
        where = f"via {net} at ({_mm(centre[0])}, {_mm(centre[1])})"
        for diameter in sorted(
            {max(design_diameter, new_drill + 2 * min_ring), new_drill + 2 * min_ring}, reverse=True
        ):
            if holes_ok and all(o.distance(centre, centre) >= clearance + diameter / 2 - 1e-6 for o in others):
                size, hole = sexpr.child(node, "size"), sexpr.child(node, "drill")
                assert size is not None
                assert hole is not None
                size[1:] = [Sym(_mm(diameter))]
                hole[1:] = [Sym(_mm(new_drill))]
                changes.append(
                    f"{where}: {_mm(2 * radius)} / {_mm(drill)} -> {_mm(diameter)} / {_mm(new_drill)} mm (diameter / drill)"
                )
                break
        else:
            changes.append(f"{where}: {_mm(2 * radius)} / {_mm(drill)} mm, no room for the class via - check")
    return changes


DEFAULT_MIN_ANNULAR_MM = 0.1


def _via_span(node: list[Node]) -> int:
    layers = sexpr.child(node, "layers")
    names = [] if layers is None else [str(x) for x in layers[1:]]
    order = list(COPPER_LAYERS)
    idx = [order.index(n) for n in names if n in order]
    return max(idx) - min(idx) if idx else 0


def _via_drill(node: list[Node]) -> float:
    drill = sexpr.child(node, "drill")
    return 0.0 if drill is None else _num(drill, 1)


DEFAULT_HOLE_TO_HOLE_MM = 0.25  # KiCad's default minimum hole-to-hole gap


def _min_hole_to_hole(project: KicadProject) -> float:
    pro = project.pcb_path.with_suffix(".kicad_pro")
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_HOLE_TO_HOLE_MM
    rules = data.get("board", {}).get("design_settings", {}).get("rules", {})
    return float(rules.get("min_hole_to_hole", DEFAULT_HOLE_TO_HOLE_MM))


VIA_MOVE_STEPS_MM = tuple(round(0.05 * k, 2) for k in range(1, 61))  # up to 3 mm


VIA_MOVE_DIRECTIONS = 48


def relocate_vias(project: KicadProject) -> list[str]:
    """Move vias that sit too close to other nets' copper to the nearest spot that keeps clearance.

    In crowded fan-outs (an ESP32 module's 0.85 mm pitch pad rows) the autorouter places
    vias against or onto neighbouring pads: shorts, hole-clearance and mask-bridge errors.
    The via moves (nearest first, 48 directions, up to 3 mm) together with the ends of its
    net's tracks that meet it; the via and every moved piece must keep the board clearance
    to other nets and the edge clearance to the outline, else the via stays.
    """
    clearance, edge_clearance = _design_clearances(project)
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()

    def crowded(centre: tracks.Point, radius: float, net: str) -> bool:
        return any(
            o.net != net and o.distance(centre, centre) < radius + clearance - tracks.EPS / 10 for o in obstacles
        )

    todo = [node for node, centre, radius, net in copper.vias if crowded(centre, radius, net)]
    changes = []
    dirty = False
    for via in todo:
        if dirty:  # an earlier move changed the copper
            copper, dirty = _copper(project.pcb), False
            obstacles = copper.obstacles()
        node, centre, radius, net = next(v for v in copper.vias if v[0] is via)
        ends = [  # (segment, index of its end at the via, far end)
            (s, k, (s.b if k == 0 else s.a))
            for s in copper.segs_by_net.get(net, [])
            for k, e in enumerate((s.a, s.b))
            if math.dist(e, centre) <= radius
        ]
        # only copper the moved via or its moved tracks could reach (the whole board made this a slow fix)
        reach = max(VIA_MOVE_STEPS_MM) + radius + clearance + 0.5
        xs = [centre[0] - reach, centre[0] + reach, *(far[0] for _, _, far in ends)]
        ys = [centre[1] - reach, centre[1] + reach, *(far[1] for _, _, far in ends)]
        bx0, by0, bx1, by1 = min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1
        others = [
            o
            for o in obstacles
            if o.net != net and o.box[0] <= bx1 and o.box[2] >= bx0 and o.box[1] <= by1 and o.box[3] >= by0
        ]

        def fits(
            q: tracks.Point,
            others: list[tracks.Obstacle] = others,
            radius: float = radius,
            ends: list[tuple[tracks.Seg, int, tracks.Point]] = ends,
            edges: list[tracks.Obstacle] = copper.edges,
        ) -> bool:
            if any(o.distance(q, q) < radius + clearance for o in others):
                return False
            if any(e.distance(q, q) < radius + edge_clearance for e in edges):
                return False
            for s, _, far in ends:
                half = s.width / 2
                near = [o for o in others if s.layer in o.layers]
                if any(o.distance(q, far) < half + clearance for o in near):
                    return False
                if any(e.distance(q, far) < half + edge_clearance for e in edges if s.layer in e.layers):
                    return False
            return True

        # back along its own tracks first (keeps that track straight), then all round
        directions = [
            ((far[0] - centre[0]) / n, (far[1] - centre[1]) / n)
            for _, _, far in ends
            if (n := math.dist(far, centre)) > tracks.EPS
        ] + [
            (math.cos(2 * math.pi * k / VIA_MOVE_DIRECTIONS), math.sin(2 * math.pi * k / VIA_MOVE_DIRECTIONS))
            for k in range(VIA_MOVE_DIRECTIONS)
        ]
        target = next(
            (
                q
                for step in VIA_MOVE_STEPS_MM
                for dx, dy in directions
                if fits(q := (centre[0] + step * dx, centre[1] + step * dy))
            ),
            None,
        )
        if target is None:
            moved_to = _relocate_via_rerouting(project, copper, node, centre, radius, net, clearance, edge_clearance)
            if moved_to is not None:
                dirty = True
                changes.append(
                    f"via {net} at ({_mm(centre[0])}, {_mm(centre[1])}) -> ({_mm(moved_to[0])}, {_mm(moved_to[1])}):"
                    " clearance, its tracks rerouted"
                )
                continue
            changes.append(
                f"via {net} at ({_mm(centre[0])}, {_mm(centre[1])}): too close to other copper, no room to move"
            )
            continue
        at = sexpr.child(node, "at")
        assert at is not None
        at[1:3] = [Sym(_mm(target[0])), Sym(_mm(target[1]))]
        dirty = True
        for s, k, _ in ends:
            end = sexpr.child(_source(s), "start" if k == 0 else "end")
            assert end is not None
            end[1:3] = [Sym(_mm(target[0])), Sym(_mm(target[1]))]
        changes.append(
            f"via {net} at ({_mm(centre[0])}, {_mm(centre[1])}) -> ({_mm(target[0])}, {_mm(target[1])}): clearance"
        )
    return changes


def _relocate_via_rerouting(
    project: KicadProject,
    copper: _Copper,
    node: list[Node],
    centre: tracks.Point,
    radius: float,
    net: str,
    clearance: float,
    edge_clearance: float,
) -> tracks.Point | None:
    """Move a crowded via to the nearest spot where it fits and reroute its tracks from there.

    The tracks leaving the via (runs up to their next pad, via or junction) get new 45-degree
    routes from the new spot on their own layers (see :func:`reroute_tracks`) over their first
    ``VIA_SPLICE_MM``, joining the rest of the run there (a run to a far via stays a small
    search); the move happens only if every one of them routes. Returns the new centre, or None.
    """
    obstacles = copper.obstacles()
    others_all = [o for o in obstacles if o.net != net]
    attached = [
        r
        for r in tracks.chains(copper.segs, copper.on_pad_or_via)
        if r.net == net and (math.dist(r.points[0], centre) <= radius or math.dist(r.points[-1], centre) <= radius)
    ]
    # stubs inside the via's copper go with the old position; the rest are rerouted from the new one
    stubs = [r for r in attached if all(math.dist(p, centre) <= radius for p in (r.points[0], r.points[-1]))]
    runs = [r for r in attached if not any(r is t for t in stubs)]
    if not runs:
        return None
    spots = [
        (
            centre[0] + step * math.cos(2 * math.pi * k / VIA_MOVE_DIRECTIONS),
            centre[1] + step * math.sin(2 * math.pi * k / VIA_MOVE_DIRECTIONS),
        )
        for step in VIA_MOVE_STEPS_MM
        for k in range(VIA_MOVE_DIRECTIONS)
    ]
    # only obstacles whose box comes within reach of a spot can be too close to it (testing every obstacle of
    # the board for every spot dominated `check` on a crowded board)
    all_index = tracks.BoxIndex([o.box for o in others_all])
    reach = radius + clearance

    def spot_free(q: tracks.Point) -> bool:
        hits = all_index.overlapping(q[0] - reach, q[1] - reach, q[0] + reach, q[1] + reach)
        return all(others_all[i].distance(q, q) >= reach for i in hits)

    spots = [q for q in spots if spot_free(q)][:VIA_REROUTE_SPOTS]
    by_layer: dict[str, tuple[list[tracks.Obstacle], list[tracks.Obstacle], tracks.BoxIndex]] = {}
    for layer in {r.layer for r in runs}:  # per layer once, not per spot and run
        near_l = [o for o in others_all if layer in o.layers]
        by_layer[layer] = (
            near_l,
            [e for e in copper.edges if layer in e.layers],
            tracks.BoxIndex([o.box for o in near_l]),
        )
    for q in spots:
        new_runs = []
        for r in runs:
            pts = r.points if math.dist(r.points[0], centre) <= radius else r.points[::-1]
            # reroute only the first stretch (up to VIA_SPLICE_MM along the run), keep the rest
            k, walked = 1, math.dist(pts[0], pts[1])
            while k < len(pts) - 1 and walked < VIA_SPLICE_MM:
                walked += math.dist(pts[k], pts[k + 1])
                k += 1
            far, rest = pts[k], pts[k + 1 :]
            width = max(s.width for s in r.segs)
            half = width / 2
            near, edges, index = by_layer[r.layer]
            need = clearance + half + REROUTE_GRID_MARGIN_MM
            memo: dict[tuple[int, int], bool] = {}
            box = (min(q[0], far[0]) - 3, min(q[1], far[1]) - 3, max(q[0], far[0]) + 3, max(q[1], far[1]) + 3)

            def free(
                pt: tracks.Point,
                near: list[tracks.Obstacle] = near,
                edges: list[tracks.Obstacle] = edges,
                index: tracks.BoxIndex = index,
                need: float = need,
                half: float = half,
                box: tuple[float, float, float, float] = box,
                memo: dict[tuple[int, int], bool] = memo,
            ) -> bool:
                x, y = pt
                if not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
                    return False
                key = (round(x * 1000), round(y * 1000))
                if key not in memo:  # every grid node is asked once per heading: remember
                    hits = index.overlapping(x - need, y - need, x + need, y + need)
                    memo[key] = all(near[i].distance(pt, pt) >= need for i in hits) and all(
                        e.distance(pt, pt) >= edge_clearance + half for e in edges
                    )
                return memo[key]

            def fits(
                a: tracks.Point,
                b: tracks.Point,
                near: list[tracks.Obstacle] = near,
                edges: list[tracks.Obstacle] = edges,
                half: float = half,
            ) -> bool:
                return all(o.distance(a, b) >= clearance + half for o in near) and all(
                    e.distance(a, b) >= edge_clearance + half for e in edges
                )

            route = tracks.grid_route(q, far, free, REROUTE_STEP_MM, None, fits)
            if route is None or not all(fits(a, b) for a, b in itertools.pairwise(route)):
                log.debug("via %s -> %s: no route on %s to %s", net, q, r.layer, far)
                break
            line = [*route, *rest]
            bends = [tracks.interior_angle(line[i - 1], line[i], line[i + 1]) for i in range(1, len(line) - 1)]
            if any(b < tracks.MIN_CORNER_DEG - 0.5 for b in bends):
                log.debug("via %s -> %s: route on %s bends sharper than 135 deg", net, q, r.layer)
                break
            new_runs.append((r, route, width, pts[: k + 1]))
        else:
            at = sexpr.child(node, "at")
            assert at is not None
            at[1:3] = [Sym(_mm(q[0])), Sym(_mm(q[1]))]
            for r in stubs:
                for seg in r.segs:
                    _drop(project.pcb, _source(seg))
            for r, route, width, replaced in new_runs:
                template = _source(r.segs[0])

                def on_replaced(p: tracks.Point, replaced: list[tracks.Point] = replaced) -> bool:
                    return any(
                        tracks.point_seg_distance(p, a, b) <= tracks.EPS for a, b in itertools.pairwise(replaced)
                    )

                old_segs = [s for s in r.segs if on_replaced(s.a) and on_replaced(s.b)]
                index_ = min(project.pcb.index(_source(s)) for s in old_segs)
                for s in old_segs:
                    _drop(project.pcb, _source(s))
                for k, (a, b) in enumerate(itertools.pairwise(route)):
                    project.pcb.insert(index_ + k, _segment_node(template, a, b, width))
            return q
    return None


VIA_REROUTE_SPOTS = 12  # spots tried by _relocate_via_rerouting, nearest first


VIA_SPLICE_MM = 2.0  # it reroutes this much of each run, the rest stays
