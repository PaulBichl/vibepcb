"""After the zone fill: restoring nets, stitching unconnected pads and pour islands with vias."""

from __future__ import annotations

import copy
import itertools
import math
import uuid
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import (
    COPPER_LAYERS,
    REROUTE_GRID_MARGIN_MM,
    REROUTE_STEP_MM,
    TRACK_NODES,
    KicadProject,
    _bbox,
    _design_clearances,
    _design_min_track_width,
    _design_via,
    _footprints_by_ref,
    _fp_to_board,
    _layer,
    _mm,
    _net_names,
    _net_node,
    _net_of,
    _num,
)
from vibepcb.kicad.fixes.copper import _Copper, _copper, _zone_polygons
from vibepcb.kicad.fixes.push import remove_antennas
from vibepcb.kicad.fixes.references import _graphic_points
from vibepcb.kicad.fixes.vias import _min_hole_to_hole, merge_duplicate_vias
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from vibepcb.core.sexpr import Node


def restore_nets(project: KicadProject, nets: set[str]) -> list[str]:
    """Put the tracks and vias of ``nets`` back as they were before the track clean-up.

    The safety net for the geometry fixes: their connection model follows KiCad's rules
    as measured, but where KiCad still finds a net broken afterwards (``fix`` asks it), that
    net goes back to its routed state - one unclean net beats a disconnected one.
    """
    snapshot = project.before_track_fixes
    if snapshot is None or not nets:
        return []
    names_now, names_then = _net_names(project.pcb), _net_names(snapshot)
    project.pcb[:] = [
        n
        for n in project.pcb
        if not (sexpr.head(n) in TRACK_NODES and isinstance(n, list) and _net_of(n, names_now) in nets)
    ]
    restored = [
        n for n in snapshot if sexpr.head(n) in TRACK_NODES and isinstance(n, list) and _net_of(n, names_then) in nets
    ]
    project.pcb.extend(copy.deepcopy(restored))
    merge_duplicate_vias(project)  # the routed state has tscircuit's stacked / crowded vias back
    return [
        f"{net}: tracks/vias restored as routed (KiCad found it unconnected after the clean-up)" for net in sorted(nets)
    ]


STITCH_STEPS_MM = tuple(round(0.05 * k, 2) for k in range(1, 61))  # up to 3 mm from the pad


STITCH_DIRECTIONS = 36


def stitch_unconnected_pads(project: KicadProject, pads: list[tuple[str, tracks.Point]]) -> list[str]:
    """Join pads of a poured net that KiCad reports unconnected to the net's plane with a via.

    ``pads``: (net, pad position) from KiCad's unconnected-items report, after the zone fill.
    A pad boxed in by other nets' tracks (seen: the GND pin between the two USB lines of an
    ESD part) gets no copper from its own layer's pour, and tscircuit routes no track to a
    poured net. The via goes to the nearest spot (<= 3 mm) where it keeps the clearance on
    every layer and lands in the net's filled pour on another layer; a minimum-width track
    joins it to the pad.
    """
    copper = _copper(project.pcb, filled_zones=True)
    changes = []
    # KiCad lists a pad once per unconnected pair it is in: one stitching via per pad, not per item
    for net, pos in dict.fromkeys((net, (round(pos[0], 4), round(pos[1], 4))) for net, pos in pads):
        pad = next((o for o in copper.pads_by_net.get(net, []) if o.distance(pos, pos) <= tracks.ON_EDGE_MM), None)
        if pad is None or not any(n == net and lay not in pad.layers for n, lay in copper.zones):
            continue  # no plane of the net on another layer: nothing to stitch to
        others = [o for o in copper.obstacles() if o.net != net]
        nodes = _pad_via(project, copper, pad, net, others)
        if nodes is None:
            changes.append(f"{net} pad at ({_mm(pos[0])}, {_mm(pos[1])}): no room for a stitching via")
            continue
        project.pcb += nodes
        copper = _copper(project.pcb, filled_zones=True)  # the next via keeps its clearance to this one
        changes.append(f"{net} pad at ({_mm(pos[0])}, {_mm(pos[1])}): stitching via to its plane")
    return changes


ISLAND_VIA_STEP_MM = 0.25  # candidate grid for a stitching via inside a pour piece


def copper_vias(project: KicadProject) -> list[tuple[list[Node], tracks.Point, float, str]]:
    """The board's vias as (node, centre, radius, net)."""
    return _copper(project.pcb).vias


def drop_vias_at(project: KicadProject, spots: list[tracks.Point]) -> list[str]:
    """Remove the vias centred on ``spots`` (KiCad's ``via_dangling`` positions) and the tracks only they held.

    A stitching via can join two pour pieces that reach no pad (a top island inside a QFP and the cut-off
    bottom fill under it): the pair floats, KiCad reports the vias dangling. ``check`` drops them after the
    refill and keeps that only if nothing loses its connection.
    """
    names = _net_names(project.pcb)
    changes = []
    for node in [n for n in project.pcb if sexpr.head(n) == "via" and isinstance(n, list)]:
        at = sexpr.child(node, "at")
        if at is None:
            continue
        centre = (_num(at, 1), _num(at, 2))
        if any(math.dist(centre, q) <= tracks.ON_EDGE_MM for q in spots):
            project.pcb.remove(node)
            changes.append(f"{_net_of(node, names)} via at ({_mm(centre[0])}, {_mm(centre[1])}): dangling, removed")
    if changes:
        changes += remove_antennas(project, filled_zones=True)
    return changes


ISLANDS_REMOVED = 0  # KiCad's island_removal_mode: 0 always remove, 1 never


ISLANDS_KEPT = 1


def set_island_removal(project: KicadProject, mode: int) -> int:
    """Set ``island_removal_mode`` of every net zone (not keepouts); returns how many zones changed.

    ``check`` refills once with islands kept (:data:`ISLANDS_KEPT`) so :func:`stitch_pour_islands` sees the pour
    pieces KiCad would delete (a piece fenced in by tracks, no via yet), then restores the removal.
    """
    changed = 0
    for zone in sexpr.children(project.pcb, "zone"):
        if sexpr.child(zone, "keepout") is not None:
            continue
        for node in sexpr.walk(zone):
            if sexpr.head(node) == "island_removal_mode" and isinstance(node, list) and str(node[1]) != str(mode):
                node[1] = Sym(str(mode))
                changed += 1
    return changed


def stitch_pour_islands(project: KicadProject) -> list[str]:
    """A via joining every piece of a net's pour that floats to a piece on another layer that does not.

    Tracks cut an outer-layer GND pour into pieces; a piece reaching no other copper of its net floats (KiCad:
    unconnected zone items, or a deleted island) even though the plane is right below it. Standard practice is
    a stitching via. Pieces, pads, vias and the net's tracks form groups (:func:`_pour_groups`); the group with
    the most pads is the net. A piece outside it gets a via at the spot nearest its middle that lies fully in
    the fill, keeps the clearance on every layer and lands in a piece of that group on another layer (a via
    into a piece that floats too would only dangle); repeated while a stitch connects a further piece. Needs the
    filled zones (``check`` runs it after KiCad's refill, with islands kept).
    """
    copper = _copper(project.pcb, filled_zones=True)
    clearance, edge_clearance = _design_clearances(project)
    via_size, via_drill = _design_via(project)
    gap = _min_hole_to_hole(project)
    r = via_size / 2
    others_all = copper.obstacles()
    all_vias = [(c, rr) for _, c, rr, _ in copper.vias]
    changes = []
    for net in sorted({n for n, _ in copper.zones}):
        net_node = _net_node(project.pcb, net)
        if net_node is None:
            continue
        others = [o for o in others_all if o.net != net]
        added: list[tracks.Point] = []
        tried: set[tuple[str, int]] = set()
        while True:
            pieces, group_of, main = _pour_groups(copper, net, added)
            if main is None:
                break
            stitched = False
            for k, (layer, poly) in enumerate(pieces):
                if group_of[k] == main or (layer, k) in tried or len(poly) < 3:
                    continue
                tried.add((layer, k))
                targets = [pl for j, (lay, pl) in enumerate(pieces) if lay != layer and group_of[j] == main]
                spot = _island_spot(poly, targets, others, copper.edges, all_vias, r, clearance, edge_clearance, gap)
                if spot is None:
                    continue
                x, y = _mm(spot[0]), _mm(spot[1])
                uid = uuid.uuid5(uuid.NAMESPACE_OID, f"island-via:{net}:{x},{y}")
                via = sexpr.loads(
                    f'(via (at {x} {y}) (size {_mm(via_size)}) (drill {_mm(via_drill)}) (layers "F.Cu" "B.Cu") '
                    f'(uuid "{uid}"))'
                )
                via.insert(len(via) - 1, copy.deepcopy(net_node))
                project.pcb.append(via)
                added.append(spot)
                all_vias.append((spot, r))
                changes.append(f"{net} pour piece on {layer}: stitching via at ({x}, {y})")
                stitched = True
                break  # the groups changed: recompute
            if not stitched:
                break
    return changes


def _pour_groups(
    copper: _Copper, net: str, extra_vias: list[tracks.Point]
) -> tuple[list[tuple[str, list[tracks.Point]]], list[int], int | None]:
    """The net's filled pour pieces, the connectivity group of each and the main group (the most pads).

    Union-find over pieces, pads, vias (``extra_vias`` too) and the net's tracks: a via joins the pieces its
    centre lies in on every layer, a pad the pieces its centre lies in on its layers, a track the pieces its
    ends lie in (and the pads / vias at its ends).
    """
    pieces = [(layer, poly) for (n, layer), polys in sorted(copper.zones.items()) if n == net for poly in polys]
    pads = copper.pads_by_net.get(net, [])
    vias = [c for c, _ in copper.vias_by_net.get(net, [])] + extra_vias
    segs = copper.segs_by_net.get(net, [])
    n_pieces, n_pads = len(pieces), len(pads)
    parent = list(range(n_pieces + n_pads + len(vias) + len(segs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    def pieces_at(q: tracks.Point, layers: frozenset[str] | set[str]) -> list[int]:
        return [k for k, (lay, poly) in enumerate(pieces) if lay in layers and tracks.point_in_polygon(q, poly)]

    via_base, seg_base = n_pieces + n_pads, n_pieces + n_pads + len(vias)
    for i, pad in enumerate(pads):
        for k in pieces_at(pad.centre, pad.layers):
            union(n_pieces + i, k)
    for i, c in enumerate(vias):
        for k in pieces_at(c, set(COPPER_LAYERS)):
            union(via_base + i, k)
    for i, s in enumerate(segs):
        me = seg_base + i
        for end in (s.a, s.b):
            for k in pieces_at(end, {s.layer}):
                union(me, k)
            for j, pad in enumerate(pads):
                if s.layer in pad.layers and pad.distance(end, end) <= s.width / 2:
                    union(me, n_pieces + j)
            for j, c in enumerate(vias):
                if math.dist(end, c) <= s.width / 2 + tracks.EPS:
                    union(me, via_base + j)
        for j, t in enumerate(segs[:i]):
            if t.layer == s.layer and any(math.dist(e, f) <= tracks.EPS for e in (s.a, s.b) for f in (t.a, t.b)):
                union(me, seg_base + j)
    if not pads:
        return pieces, [find(k) for k in range(n_pieces)], None
    counts: dict[int, int] = {}
    for i in range(n_pads):
        counts[find(n_pieces + i)] = counts.get(find(n_pieces + i), 0) + 1
    main = max(counts, key=lambda g: (counts[g], -g))
    return pieces, [find(k) for k in range(n_pieces)], main


def _island_spot(
    poly: list[tracks.Point],
    targets: list[list[tracks.Point]],
    others: list[tracks.Obstacle],
    edges: list[tracks.Obstacle],
    vias: list[tuple[tracks.Point, float]],
    r: float,
    clearance: float,
    edge_clearance: float,
    gap: float,
) -> tracks.Point | None:
    """The spot nearest the middle of ``poly`` for a via of radius ``r``: fully in the fill, in one of ``targets``
    (pieces on another layer), clear of ``others`` / the edges / other vias' holes."""
    if not targets:
        return None
    ring = list(itertools.pairwise([*poly, poly[0]]))
    xs, ys = [q[0] for q in poly], [q[1] for q in poly]
    mid = (sum(xs) / len(xs), sum(ys) / len(ys))
    nx = int((max(xs) - min(xs)) / ISLAND_VIA_STEP_MM) + 1
    ny = int((max(ys) - min(ys)) / ISLAND_VIA_STEP_MM) + 1
    candidates = sorted(
        (
            (min(xs) + i * ISLAND_VIA_STEP_MM, min(ys) + j * ISLAND_VIA_STEP_MM)
            for i in range(nx + 1)
            for j in range(ny + 1)
        ),
        key=lambda q: (math.dist(q, mid), q),
    )
    for q in candidates:
        if (
            tracks.point_in_polygon(q, poly)
            and all(tracks.point_seg_distance(q, a, b) >= r for a, b in ring)  # the via's copper in the fill
            and any(tracks.point_in_polygon(q, pl) for pl in targets)
            and all(o.distance(q, q) >= r + clearance for o in others)  # every layer
            and all(e.distance(q, q) >= r + edge_clearance for e in edges)
            and all(math.dist(q, c) >= rr + r + gap for c, rr in vias)
        ):
            return q
    return None


def _pad_via(
    project: KicadProject, copper: _Copper, pad: tracks.Obstacle, net: str, others: list[tracks.Obstacle]
) -> list[list[Node]] | None:
    """[track, via] joining an SMD pad to its net's zone on another layer, or None without room.

    The via goes to the nearest spot (<= 3 mm) where it keeps the clearance to ``others`` on every
    layer and to the board edge, sits beside the pad (no via-in-pad: solder wicks away) and inside
    one of the net's zones on another layer; the track from the pad centre keeps its clearance too.
    """
    clearance, edge_clearance = _design_clearances(project)
    width = _design_min_track_width(project)
    via_size, via_drill = _design_via(project)
    r = via_size / 2
    layer = next((lay for lay in COPPER_LAYERS if lay in pad.layers), None)
    plane_layers = sorted({lay for n, lay in copper.zones if n == net and lay != layer})
    net_node = _net_node(project.pcb, net)
    if layer is None or not plane_layers or net_node is None:
        return None
    # only what a via within reach could touch (the whole board's copper made this the slowest fix)
    reach = max(STITCH_STEPS_MM) + r + clearance + width + 0.5
    cx, cy = pad.centre
    others = [
        o
        for o in others
        if o.box[0] <= cx + reach and o.box[2] >= cx - reach and o.box[1] <= cy + reach and o.box[3] >= cy - reach
    ]
    on_layer = [o for o in others if layer in o.layers]
    own_pads = [o for o in copper.pads_by_net.get(net, []) if math.dist(o.centre, pad.centre) < 4]

    def fits_via(q: tracks.Point) -> bool:
        return (
            all(o.distance(q, q) >= r + clearance for o in others)
            and all(e.distance(q, q) >= r + edge_clearance for e in copper.edges)
            and all(o.distance(q, q) >= r + clearance for o in own_pads)  # no via-in-pad, also not in a neighbour's
            and any(copper.poured(q, net, lay) for lay in plane_layers)  # not in a pour keepout (antenna, coil)
        )

    def fits(q: tracks.Point) -> bool:  # the via and a straight track to it
        return fits_via(q) and all(o.distance(pad.centre, q) >= clearance + width / 2 for o in on_layer)

    candidates = [
        (pad.centre[0] + step * math.cos(2 * math.pi * k / STITCH_DIRECTIONS),
         pad.centre[1] + step * math.sin(2 * math.pi * k / STITCH_DIRECTIONS))
        for step in STITCH_STEPS_MM
        for k in range(STITCH_DIRECTIONS)
    ]  # fmt: skip
    path: list[tracks.Point] | None = None
    spot = next((q for q in candidates if fits(q)), None)
    if spot is not None:
        path = [pad.centre, spot]
    else:  # no straight way out (a pin hemmed in by other tracks): a short dogleg to a free via spot
        via_ok = [q for q in candidates if fits_via(q)][:STITCH_DOGLEG_SPOTS]
        for q in via_ok:
            route = _dogleg(pad.centre, q, on_layer, copper.edges, clearance, edge_clearance, width, layer)
            if route is not None:
                spot, path = q, route
                break
    if spot is None or path is None:
        return None
    x, y = _mm(spot[0]), _mm(spot[1])
    via_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"stitch:{net}:{x},{y}")
    via = sexpr.loads(
        f'(via (at {x} {y}) (size {_mm(via_size)}) (drill {_mm(via_drill)}) (layers "F.Cu" "B.Cu") (uuid "{via_uuid}"))'
    )
    nodes = []
    for a, b in itertools.pairwise(path):
        seg_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"stitch-track:{net}:{_mm(a[0])},{_mm(a[1])}:{_mm(b[0])},{_mm(b[1])}")
        nodes.append(sexpr.loads(
            f'(segment (start {_mm(a[0])} {_mm(a[1])}) (end {_mm(b[0])} {_mm(b[1])}) (width {_mm(width)}) '
            f'(layer "{layer}") (uuid "{seg_uuid}"))'
        ))  # fmt: skip
    for node in (*nodes, via):
        node.insert(len(node) - 1, copy.deepcopy(net_node))  # before the uuid, as KiCad writes it
    return [*nodes, via]


STITCH_DOGLEG_SPOTS = 8  # free via spots (nearest first) a hemmed-in pad tries a maze route to


def _dogleg(
    start: tracks.Point,
    goal: tracks.Point,
    on_layer: list[tracks.Obstacle],
    edges: list[tracks.Obstacle],
    clearance: float,
    edge_clearance: float,
    width: float,
    layer: str,
) -> list[tracks.Point] | None:
    """A short 45-degree route start -> goal on one layer keeping the clearances (``tracks.grid_route``)."""
    half = width / 2
    near = [o for o in on_layer if o.box[0] < max(start[0], goal[0]) + 4 and o.box[2] > min(start[0], goal[0]) - 4
            and o.box[1] < max(start[1], goal[1]) + 4 and o.box[3] > min(start[1], goal[1]) - 4]  # fmt: skip
    outline = [e for e in edges if layer in e.layers]
    box = (
        min(start[0], goal[0]) - 2,
        min(start[1], goal[1]) - 2,
        max(start[0], goal[0]) + 2,
        max(start[1], goal[1]) + 2,
    )
    need = clearance + half + REROUTE_GRID_MARGIN_MM

    def free(p: tracks.Point) -> bool:
        if not (box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]):
            return False
        return all(o.distance(p, p) >= need for o in near) and all(
            e.distance(p, p) >= edge_clearance + half for e in outline
        )

    def fits(a: tracks.Point, b: tracks.Point) -> bool:
        return all(o.distance(a, b) >= clearance + half for o in near) and all(
            e.distance(a, b) >= edge_clearance + half for e in outline
        )

    route = tracks.grid_route(start, goal, free, REROUTE_STEP_MM, None, fits, max_states=60_000)
    if route is None or not all(fits(a, b) for a, b in itertools.pairwise(route)):
        return None
    return route


CONNECTOR_LABEL_REACH_MM = 6.0  # board text this close to a connector's pads counts as its labelling


FULL_POUR_SHARE = 0.8  # a zone covering this share of the board is a full plane, not a region of a split


def split_crossings(project: KicadProject) -> list[str]:
    """Tracks that cross the edge of a region pour (a split plane, a patch) on the copper layer next to them.

    Over the edge the track's return current has to change planes: a detour and a noise source, so common
    practice keeps signals off split edges (or stitches a capacitor across). One line per track and split.
    """
    layer_names = {
        str(n[1]) for n in (sexpr.child(project.pcb, "layers") or [])[1:] if isinstance(n, list) and len(n) > 1
    }
    stack = [lay for lay in COPPER_LAYERS if lay in layer_names]
    edge_points = [
        pt
        for g in project.pcb
        if isinstance(g, list)
        and sexpr.head(g) in ("gr_line", "gr_rect", "gr_arc", "gr_poly")
        and _layer(g) == "Edge.Cuts"
        for pt in _graphic_points(g)
    ]
    if not edge_points:
        return []
    bx0, by0, bx1, by1 = _bbox(edge_points)
    board_area = (bx1 - bx0) * (by1 - by0)
    names = _net_names(project.pcb)
    regions: dict[str, list[tuple[str, list[tracks.Point]]]] = {}  # layer -> (net, outline) of region pours
    nets_on: dict[str, set[str]] = {}
    for zone in sexpr.children(project.pcb, "zone"):
        if sexpr.child(zone, "keepout") is not None:
            continue
        net = _net_of(zone, names)
        for layer, poly in _zone_polygons(zone, filled=False):
            nets_on.setdefault(layer, set()).add(net)
            x0, y0, x1, y1 = _bbox(poly)
            if (x1 - x0) * (y1 - y0) < FULL_POUR_SHARE * board_area:
                regions.setdefault(layer, []).append((net, poly))
    out = []
    copper = _copper(project.pcb)
    for i, layer in enumerate(stack):
        if len(nets_on.get(layer, set())) < 2 or not regions.get(layer):
            continue  # one net on the layer: no split
        neighbours = {stack[j] for j in (i - 1, i + 1) if 0 <= j < len(stack)}
        for s in copper.segs:
            if s.layer not in neighbours:
                continue
            for net, poly in regions[layer]:
                if s.net == net:
                    continue
                ring = itertools.pairwise([*poly, poly[0]])
                hit = next((p for a, b in ring if (p := tracks.crossing(s.a, s.b, a, b)) is not None), None)
                if hit is not None:
                    out.append(
                        f"{s.net} on {s.layer} crosses the {net} region edge on {layer} at ({_mm(hit[0])}, {_mm(hit[1])})"
                    )
                    break
    return out


def unlabelled_connectors(project: KicadProject) -> list[str]:
    """Connectors (reference J..., 3+ pads) without any board silkscreen text near their pads.

    A header nobody can read is useless on the bench: pin labels (``showSilkscreenPinLabels`` or
    ``<silkscreentext>`` per pin) or at least a caption belong next to every connector.
    A warning only (``check`` reports it, the exit code doesn't change).
    """
    texts = []
    for node in sexpr.children(project.pcb, "gr_text"):
        if _layer(node) in ("F.SilkS", "B.SilkS"):
            at = sexpr.child(node, "at")
            texts.append((_num(at, 1), _num(at, 2)))
    out = []
    for ref, fp in sorted(_footprints_by_ref(project.pcb).items()):
        pads = list(sexpr.children(fp, "pad"))
        if not ref.startswith("J") or len(pads) < 3:
            continue
        to_board = _fp_to_board(fp)
        centres = [to_board(_num(sexpr.child(p, "at"), 1), _num(sexpr.child(p, "at"), 2)) for p in pads]
        x0, y0 = min(x for x, _ in centres), min(y for _, y in centres)
        x1, y1 = max(x for x, _ in centres), max(y for _, y in centres)
        r = CONNECTOR_LABEL_REACH_MM
        if not any(x0 - r <= x <= x1 + r and y0 - r <= y <= y1 + r for x, y in texts):
            out.append(f"{ref} ({len(pads)} pads): no silkscreen text next to it - label its pins")
    return out


def unpoured_copper_layers(project: KicadProject) -> list[str]:
    """Copper layers without any pour (common practice: every layer carries a plane - GND or a supply).

    Without one, every pad of the plane's net on that layer needs its own track, which the
    router lays as chains of pad-to-pad links meeting at acute angles.
    """
    layers_node = sexpr.child(project.pcb, "layers")
    copper = [
        str(layer[1])
        for layer in ([] if layers_node is None else layers_node[1:])
        if isinstance(layer, list) and len(layer) > 1 and str(layer[1]).endswith(".Cu")
    ]
    poured: set[str] = set()
    for zone in sexpr.children(project.pcb, "zone"):
        if sexpr.child(zone, "keepout") is not None:
            continue
        for key in ("layer", "layers"):
            node = sexpr.child(zone, key)
            if node is not None:
                poured |= {str(x) for x in node[1:]}
    return [layer for layer in copper if layer not in poured and "*.Cu" not in poured]
