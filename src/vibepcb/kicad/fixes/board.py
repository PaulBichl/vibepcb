"""Board set-up fixes: escapes, net names, pads, holes, keepout attributes, board thickness, footprint library."""

from __future__ import annotations

import copy
import itertools
import json
import math
import uuid
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad import schgen
from vibepcb.kicad.fixes.base import (
    _AUTO_NET,
    STANDARD_BOARD_THICKNESS_MM,
    Box,
    KicadProject,
    _bbox,
    _footprints_by_ref,
    _fp_to_board,
    _inside_pad,
    _layer,
    _mm,
    _net_names,
    _net_node,
    _net_of,
    _num,
    _overlaps,
    _pad_on_layer,
    _raw_circuit,
    _segment_node,
)
from vibepcb.routing import fanout, tracks
from vibepcb.settings import rules

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibepcb.core.sexpr import Node


def mask_covered_pads(project: KicadProject) -> list[str]:
    """Pads the TSX covers with soldermask (``<smtpad coveredWithSolderMask />``) lose their mask opening.

    tscircuit's KiCad export writes every pad with its mask layer, so a capacitive touch electrode came out as bare
    copper (a finger on it needs a series resistor against ESD; under mask the sensor works through it).
    """
    circuit = _raw_circuit(project)
    if not circuit.exists():
        return []
    elements = json.loads(circuit.read_text(encoding="utf-8"))
    by_id = {e.get(f"{e['type']}_id"): e for e in elements if isinstance(e, dict) and "type" in e}
    covered: set[tuple[str, str]] = set()
    for e in elements:
        if e.get("type") != "pcb_smtpad" or not e.get("is_covered_with_solder_mask"):
            continue
        port = by_id.get(e.get("pcb_port_id"), {})
        source = by_id.get(port.get("source_port_id"), {})
        comp = by_id.get(source.get("source_component_id"), {})
        if comp.get("name") and source.get("pin_number") is not None:
            covered.add((str(comp["name"]), str(source["pin_number"])))
    changes = []
    for ref, fp in _footprints_by_ref(project.pcb).items():
        for pad in sexpr.children(fp, "pad"):
            if (ref, str(pad[1])) not in covered or (layers := sexpr.child(pad, "layers")) is None:
                continue
            masks = [x for x in layers[1:] if str(x).endswith(".Mask")]
            for m in masks:
                layers.remove(m)
            if masks:
                changes.append(f"{ref} pad {pad[1]}: covered with soldermask (no mask opening)")
    return changes


def _escape_plans(project: KicadProject) -> list[fanout.Plan]:
    """The escape plans ``build`` saved next to the circuit JSON (``vibepcb.fanout``)."""
    name = f"{project.pcb_path.stem}.escapes.json"
    for raw in (project.directory.parent / ".work" / "raw", project.directory.parent / "raw"):
        if (raw / name).exists():
            return fanout.load(raw / name)
    return []


def _kicad_layer(layer: str) -> str:
    """tscircuit layer name -> KiCad copper layer."""
    if layer.startswith("inner"):
        return f"In{layer.removeprefix('inner')}.Cu"
    return {"top": "F.Cu", "bottom": "B.Cu"}[layer]


def _escape_boxes(project: KicadProject) -> list[Box]:
    """Board-frame boxes of the escaped ball fields, out to the escape ends (``vibepcb.fanout``)."""
    boxes = []
    refs = _footprints_by_ref(project.pcb)
    for plan in _escape_plans(project):
        fp = refs.get(plan.ref)
        if fp is None:
            continue
        to_board = _fp_to_board(fp)
        dx, dy = escape_offset(fp, plan)
        ends = [e.end for e in plan.escapes] + [(b.x, b.y) for b in plan.balls]
        boxes.append(_bbox([to_board(x + dx, -y + dy) for x, y in ends], grow=plan.rules.via_diameter))
    return boxes


def _box_has(box: Box, p: tuple[float, float]) -> bool:
    return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]


def _in_escape(node: list[Node], boxes: list[Box]) -> bool:
    """A track or via lying wholly in an escaped ball field."""
    keys = ("start", "end") if sexpr.head(node) == "segment" else ("at",)
    pts = [(_num(c, 1), _num(c, 2)) for k in keys if (c := sexpr.child(node, k)) is not None]
    return bool(pts) and any(all(_box_has(b, q) for q in pts) for b in boxes)


def _declared_net(pcb: list[Node], name: str) -> list[Node] | None:
    """A pad's ``(net <id> "<name>")`` node of a net the board declares, or None."""
    for n in sexpr.children(pcb, "net"):
        if len(n) > 2 and str(n[2]) == name:
            return [Sym("net"), n[1], name]
        if len(n) == 2 and str(n[1]) == name:
            return [Sym("net"), name]
    return None


def escape_offset(fp: list[Node], plan: fanout.Plan) -> tuple[float, float]:
    """Where the plan's part frame sits in the footprint's own frame (KiCad units, y down).

    The plan is relative to the part centre; tscircuit puts a footprint's origin at the middle of its
    pads, which for the stand-in (escape ends, asymmetric when the escapes are) is not the part centre.
    Measured from the pads that are known: square stand-in pads (an escape end) or real balls.
    """
    standin = round(fanout.STANDIN_PAD_MM, 4)
    ends = {e.pin.removeprefix("pin"): e.end for e in plan.escapes}
    balls = {b.pin.removeprefix("pin"): (b.x, b.y) for b in plan.balls}
    deltas = []
    for pad in sexpr.children(fp, "pad"):
        at, size = sexpr.child(pad, "at"), sexpr.child(pad, "size")
        number = str(pad[1])
        px, py = _num(at, 1), _num(at, 2)
        w, h = _num(size, 1), _num(size, 2)
        if abs(w - standin) <= 1e-4 and abs(h - standin) <= 1e-4 and number in ends:
            ex, ey = ends[number]
        elif str(pad[3]) == "circle" and number in balls:
            ex, ey = balls[number]
        else:
            continue
        deltas.append((px - ex, py + ey))
    if not deltas:
        return 0.0, 0.0
    return sum(d[0] for d in deltas) / len(deltas), sum(d[1] for d in deltas) / len(deltas)


def apply_escapes(project: KicadProject) -> list[str]:
    """Real balls and VibePCB's escapes for parts tscircuit routed through a stand-in (``vibepcb.fanout``).

    ``build`` gave each ``[escape.<ref>]`` part a stand-in footprint (a small pad per escape end)
    and a keepout over the package, so tscircuit's tracks end at the escape ends. Here the stand-in
    pads become the real balls again (their nets carried over), the planned escape tracks and vias
    are added, and the keepout goes (the escapes live inside it).
    """
    changes = []
    refs = _footprints_by_ref(project.pcb)
    names = _net_names(project.pcb)
    for plan in _escape_plans(project):
        fp = refs.get(plan.ref)
        if fp is None:
            changes.append(f"{plan.ref}: not on the board, escape plan skipped")
            continue
        pads = [p for p in fp if sexpr.head(p) == "pad" and isinstance(p, list)]
        standin = round(fanout.STANDIN_PAD_MM, 4)

        def standin_size(pad: list[Node], standin: float = standin) -> bool:  # square, or a staggered end strip
            size = sexpr.child(pad, "size")
            return abs(min(_num(size, 1), _num(size, 2)) - standin) <= 1e-4

        if not pads or not all(standin_size(p) for p in pads):
            continue  # real balls already (or not a stand-in)
        dx, dy = escape_offset(fp, plan)  # the part centre in the footprint frame (not its origin, see there)
        fp_to_board = _fp_to_board(fp)

        def to_board(
            x: float,
            y: float,
            dx: float = dx,
            dy: float = dy,
            fp_to_board: Callable[..., tuple[float, float]] = fp_to_board,
        ) -> tuple[float, float]:
            return fp_to_board(x + dx, y + dy)

        net_of_pin = {str(p[1]): sexpr.child(p, "net") for p in pads}
        template = copy.deepcopy(pads[0])
        for p in pads:
            fp.remove(p)
        added = 0
        for ball in plan.balls:
            number = ball.pin.removeprefix("pin")
            pad = copy.deepcopy(template)
            pad[1] = number
            pad[3] = Sym("circle")
            at = sexpr.child(pad, "at")
            if at is not None:
                at[1:3] = [Sym(_mm(ball.x + dx)), Sym(_mm(-ball.y + dy))]
            size = sexpr.child(pad, "size")
            if size is not None:
                size[1:] = [Sym(_mm(2 * ball.radius)), Sym(_mm(2 * ball.radius))]
            layers = sexpr.child(pad, "layers")
            if layers is not None:
                layers[1:] = [Sym("F.Cu"), Sym("F.Paste"), Sym("F.Mask")]
            old_net = sexpr.child(pad, "net")
            if old_net is not None:
                pad.remove(old_net)
            net = net_of_pin.get(number) or (_declared_net(project.pcb, ball.net_name) if ball.net_name else None)
            if net is not None:  # the stand-in pad's net, else (a via-only plane ball) the plane net
                pad.insert(len(pad) - 1, copy.deepcopy(net))
                net_of_pin[number] = net
            uid = sexpr.child(pad, "uuid")
            if uid is not None:
                new = str(uuid.uuid5(uuid.NAMESPACE_OID, f"ball:{plan.ref}:{number}"))
                uid[1] = Sym(new) if isinstance(uid[1], Sym) else new
            fp.append(pad)
        for g in plan.graphics:  # the courtyard the stand-in left out (tscircuit: courtyard over a keepout)
            if g["kind"] != "courtyardoutline":
                continue
            ring: list[tuple[float, float]] = []
            for q in g["route"]:  # tscircuit repeats corner points; KiCad wants a closed outline
                if not ring or math.dist(ring[-1], q) > 1e-6:
                    ring.append((q[0], q[1]))
            if len(ring) > 2 and math.dist(ring[0], ring[-1]) > 1e-6:
                ring.append(ring[0])
            for k, (a, b) in enumerate(itertools.pairwise(ring)):
                line_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"courtyard:{plan.ref}:{k}")
                fp.append(sexpr.loads(
                    f"(fp_line (start {_mm(a[0] + dx)} {_mm(-a[1] + dy)}) (end {_mm(b[0] + dx)} {_mm(-b[1] + dy)}) "
                    f'(stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "{line_uuid}"))'
                ))  # fmt: skip
        r = plan.rules
        for esc in plan.escapes:
            pad_net = net_of_pin.get(esc.pin.removeprefix("pin"))
            net_node = _net_node(project.pcb, _net_of([Sym("pad"), pad_net], names)) if pad_net else None
            nodes = []
            for layer, pts in esc.tracks:
                board = [to_board(x, -y) for x, y in pts]
                for a, b in itertools.pairwise(board):
                    key = f"escape:{plan.ref}:{esc.pin}:{_mm(a[0])},{_mm(a[1])}:{_mm(b[0])},{_mm(b[1])}"
                    nodes.append(sexpr.loads(
                        f"(segment (start {_mm(a[0])} {_mm(a[1])}) (end {_mm(b[0])} {_mm(b[1])}) "
                        f'(width {_mm(r.track_width)}) (layer "{_kicad_layer(layer)}") '
                        f'(uuid "{uuid.uuid5(uuid.NAMESPACE_OID, key)}"))'
                    ))  # fmt: skip
            for x, y in esc.vias:
                vx, vy = to_board(x, -y)
                key = f"escape-via:{plan.ref}:{esc.pin}:{_mm(vx)},{_mm(vy)}"
                nodes.append(sexpr.loads(
                    f"(via (at {_mm(vx)} {_mm(vy)}) (size {_mm(r.via_diameter)}) (drill {_mm(r.via_drill)}) "
                    f'(layers "F.Cu" "B.Cu") (uuid "{uuid.uuid5(uuid.NAMESPACE_OID, key)}"))'
                ))  # fmt: skip
            for node in nodes:
                if net_node is not None:
                    node.insert(len(node) - 1, copy.deepcopy(net_node))
                project.pcb.append(node)
            added += len(nodes)
        # the keepout tscircuit kept the router out with: the escapes are inside it
        x0, y0, x1, y1 = plan.keepout
        box = _bbox([to_board(x, -y) for x, y in ((x0, y0), (x1, y1))])
        dropped = 0
        for zone in [z for z in sexpr.children(project.pcb, "zone") if sexpr.child(z, "keepout") is not None]:
            pts = [(_num(q, 1), _num(q, 2)) for q in sexpr.walk(zone) if isinstance(q, list) and sexpr.head(q) == "xy"]
            if pts and all(abs(a - b) < 0.05 for a, b in zip(_bbox(pts), box, strict=True)):
                project.pcb.remove(zone)
                dropped += 1
        # tscircuit cut the keepout out of its pours too: a zone's extra polygons are holes (its voids,
        # merged into large shapes). Those over the package go; KiCad's refill keeps its own clearances.
        holes = 0
        opened: list[tuple[str, str, list[tuple[float, float]]]] = []  # (net, layer, hole outline)
        for zone in list(sexpr.children(project.pcb, "zone")):
            for poly in list(sexpr.children(zone, "polygon"))[1:]:
                pts = [
                    (_num(q, 1), _num(q, 2)) for q in sexpr.walk(poly) if isinstance(q, list) and sexpr.head(q) == "xy"
                ]
                if pts and _overlaps(_bbox(pts), box):
                    zone.remove(poly)
                    holes += 1
                    opened.append((_net_of(zone, names), _layer(zone), pts))
        # tscircuit's pour pieces inside an opened hole now overlap the zone around them (zones_intersect)
        for zone in list(sexpr.children(project.pcb, "zone")):
            outline = sexpr.child(zone, "polygon")
            pts = [
                (_num(q, 1), _num(q, 2))
                for q in sexpr.walk(outline or [])
                if isinstance(q, list) and sexpr.head(q) == "xy"
            ]
            zone_net, zone_layer = _net_of(zone, names), _layer(zone)
            if pts and any(n == zone_net and lay == zone_layer and all(tracks.point_in_polygon(q, hole) for q in pts)
                           for n, lay, hole in opened):  # fmt: skip
                project.pcb.remove(zone)
        # plane copper has to pass between the vias at ball pitch: zone clearance / minimum width to suit
        pitch = min((abs(a.x - b.x) for a in plan.balls for b in plan.balls if abs(a.x - b.x) > 0.01), default=1.0)
        gap = pitch - r.via_diameter - 2 * r.clearance
        for zone in sexpr.children(project.pcb, "zone"):
            if sexpr.child(zone, "keepout") is not None:
                continue
            clearance = sexpr.child(sexpr.child(zone, "connect_pads") or [], "clearance")
            if clearance is not None and _num(clearance, 1) > r.clearance:
                clearance[1] = Sym(_mm(r.clearance))
            thickness = sexpr.child(zone, "min_thickness")
            if thickness is not None and gap > 0 and _num(thickness, 1) > gap * 0.75:
                thickness[1] = Sym(_mm(round(gap * 0.75, 3)))
        changes.append(
            f"{plan.ref}: {len(plan.balls)} balls back in place of the stand-in, "
            f"{added} escape tracks/vias added, {dropped} keepout(s) and {holes} pour hole(s) removed"
        )
    return changes


def rename_auto_nets(project: KicadProject) -> list[str]:
    """Replace tscircuit's selector-style net names with the schematic's names for them.

    ``schgen`` names an unnamed net after its chip pin (``U1_VREG_1V2``), else KiCad-style
    ``Net-(R1-Pad2)``; read from ``../raw/<stem>.circuit.json``, without it KiCad-style always.
    """
    pcb = project.pcb
    circuit = _raw_circuit(project)
    pin_names = schgen.pin_net_names(json.loads(circuit.read_text(encoding="utf-8"))) if circuit.exists() else {}
    first_pad: dict[str, str] = {}  # net name -> "Net-(R1-Pad2)" after its first pad
    schematic: dict[str, str] = {}  # net name -> the schematic's name for it
    for fp in sexpr.children(pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or "?"
        for pad in sexpr.children(fp, "pad"):
            net = sexpr.child(pad, "net")
            if net is not None and len(net) > 2:
                first_pad.setdefault(str(net[2]), f"Net-({ref}-Pad{pad[1]})")
                if (ref, str(pad[1])) in pin_names:
                    schematic.setdefault(str(net[2]), pin_names[ref, str(pad[1])])

    renames = {}
    taken = {str(n[2]) for n in sexpr.children(pcb, "net") if len(n) > 2}
    for name in sorted(taken):
        if _AUTO_NET.search(name) and name in first_pad:
            new = schematic.get(name, first_pad[name])
            if new not in taken:
                renames[name] = new
    if not renames:
        return []

    for node in sexpr.walk(pcb):
        if sexpr.head(node) in ("net", "net_name"):
            for i, item in enumerate(node):
                if isinstance(item, str) and not isinstance(item, Sym) and item in renames:
                    node[i] = renames[item]
    return [f"net {old!r} -> {new!r}" for old, new in renames.items()]


def propagate_split_pad_nets(project: KicadProject) -> list[str]:
    """Give every pad of a split pad (several pads sharing one number) the net of its siblings.

    tscircuit exports e.g. a module's 3x3 grid of thermal pads "49" with the net on the
    first pad only; KiCad then reports the rest as unconnected copper shorted to it.
    The footprint is also flagged ``duplicate_pad_numbers_are_jumpers`` (KiCad >= 9), so
    pads joined inside the part (a USB-C shell's front and rear tabs) count as connected.
    """
    changes = []
    for fp in sexpr.children(project.pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or "?"
        pads = [pad for pad in sexpr.children(fp, "pad") if str(pad[1])]  # "" = mechanical NPTH
        numbers = [str(pad[1]) for pad in pads]
        if len(set(numbers)) == len(numbers):
            continue
        nets = {str(pad[1]): net for pad in pads if (net := sexpr.child(pad, "net")) is not None}
        for pad in pads:
            net = nets.get(str(pad[1]))
            if net is None or sexpr.child(pad, "net") is not None:
                continue
            layers = sexpr.child(pad, "layers")
            pad.insert(pad.index(layers) + 1 if layers is not None else len(pad), list(net))
            changes.append(f"{ref} pad {pad[1]}: net {net[2]!r}")
        if sexpr.child(fp, "duplicate_pad_numbers_are_jumpers") is None:
            fp.insert(fp.index(pads[0]), [Sym("duplicate_pad_numbers_are_jumpers"), Sym("yes")])
            changes.append(f"{ref}: duplicate pad numbers are jumpers")
    return changes


FALLBACK_TRACK_WIDTH_MM = 0.15


def repair_zero_width_tracks(project: KicadProject) -> list[str]:
    """Give zero-width track segments the width of the tracks they join.

    The autorouter occasionally emits a route point with width 0 (seen once on a GND route
    of a ~250-part board); KiCad then has a copper-less segment, i.e. a broken connection.
    """
    segs = list(sexpr.children(project.pcb, "segment"))

    def ends(seg: list[Node]) -> list[tuple[float, float]]:
        return [(_num(sexpr.child(seg, k), 1), _num(sexpr.child(seg, k), 2)) for k in ("start", "end")]

    changes = []
    for seg in segs:
        width = sexpr.child(seg, "width")
        if width is None or _num(width, 1) > 0:
            continue
        net, mine = sexpr.child(seg, "net"), ends(seg)
        neighbours = [
            _num(sexpr.child(o, "width"), 1)
            for o in segs
            if o is not seg
            and sexpr.child(o, "net") == net
            and _num(sexpr.child(o, "width"), 1) > 0
            and any(math.dist(p, q) < 1e-3 for p in mine for q in ends(o))
        ]
        new = max(neighbours, default=FALLBACK_TRACK_WIDTH_MM)
        width[1] = Sym(_mm(new))
        changes.append(f"track at ({_mm(mine[0][0])}, {_mm(mine[0][1])}): width 0 -> {_mm(new)} mm")
    return changes


PAD_EDGE_MARGIN_MM = 0.05


def drop_tracks_inside_pads(project: KicadProject) -> list[str]:
    """Remove track segments lying entirely within a pad of their own net.

    tscircuit can emit a stub (seen: 0.05 mm wide) inside a custom-shaped pad when it
    routes to it; it adds no connectivity but fails KiCad's minimum track width check.
    A run of in-pad segments that a track outside the pad continues from at (nearly) the pad
    edge stays: dropping it would leave that track ending on the edge, which KiCad may or may
    not connect; :func:`clean_tracks` trims such runs to end properly inside the pad.
    """
    pads_by_net: dict[str, list[tuple[str, list[Node], list[Node]]]] = {}
    for fp in sexpr.children(project.pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or "?"
        for pad in sexpr.children(fp, "pad"):
            net = sexpr.child(pad, "net")
            if net is not None:
                pads_by_net.setdefault(str(net[1]), []).append((ref, fp, pad))

    def ends(seg: list[Node]) -> list[tuple[str, str, float, float]]:
        net, layer = sexpr.child(seg, "net"), sexpr.child(seg, "layer")
        key = (str(net[1]) if net else "", str(layer[1]) if layer else "")
        return [
            (*key, round(_num(sexpr.child(seg, k), 1), 4), round(_num(sexpr.child(seg, k), 2), 4))
            for k in ("start", "end")
        ]

    segs = list(sexpr.children(project.pcb, "segment"))
    inside: dict[int, tuple[str, int, list[Node], list[Node]]] = {}  # id(seg) -> (ref, id(pad), fp, pad)
    for seg in segs:
        net, start, end = sexpr.child(seg, "net"), sexpr.child(seg, "start"), sexpr.child(seg, "end")
        layer = sexpr.child(seg, "layer")
        if net is None or start is None or end is None or layer is None:
            continue
        for ref, fp, pad in pads_by_net.get(str(net[1]), []):
            if not _pad_on_layer(pad, str(layer[1])):
                continue  # a track merely passing under/over an SMD pad on the other side
            if all(_inside_pad(fp, pad, _num(p, 1), _num(p, 2)) for p in (start, end)):
                inside[id(seg)] = (ref, id(pad), fp, pad)
                break

    at_end: dict[tuple[str, str, float, float], list[list[Node]]] = {}
    for seg in segs:
        for e in ends(seg):
            at_end.setdefault(e, []).append(seg)
    # runs of in-pad segments of one pad, joined end to end; a run touching any other track stays
    run_of = {id(seg): id(seg) for seg in segs if id(seg) in inside}

    def find(k: int) -> int:
        while run_of[k] != k:
            run_of[k] = run_of[run_of[k]]
            k = run_of[k]
        return k

    for group in at_end.values():
        members = [s for s in group if id(s) in inside]
        for a, b in itertools.pairwise(members):
            if inside[id(a)][1] == inside[id(b)][1]:
                run_of[find(id(a))] = find(id(b))

    def near_edge(point: tuple[str, str, float, float], fp: list[Node], pad: list[Node]) -> bool:
        x, y, m = point[2], point[3], PAD_EDGE_MARGIN_MM
        return not all(_inside_pad(fp, pad, x + dx, y + dy) for dx, dy in ((m, 0), (-m, 0), (0, m), (0, -m)))

    anchored = {
        find(id(s))
        for point, group in at_end.items()
        for s in group
        if id(s) in inside
        and any(id(o) not in inside or inside[id(o)][1] != inside[id(s)][1] for o in group)
        and near_edge(point, inside[id(s)][2], inside[id(s)][3])
    }

    dropped: dict[str, int] = {}
    for seg in segs:
        if id(seg) in inside and find(id(seg)) not in anchored:
            project.pcb.remove(seg)
            ref = inside[id(seg)][0]
            dropped[ref] = dropped.get(ref, 0) + 1
    changes = [f"{ref}: dropped {n} track stub(s) inside its own pads" for ref, n in sorted(dropped.items())]
    return changes + _straighten_pad_entries(project, segs, inside, find, at_end, anchored, near_edge)


def _straighten_pad_entries(
    project: KicadProject,
    segs: list[list[Node]],
    inside: dict[int, tuple[str, int, list[Node], list[Node]]],
    find: Callable[[int], int],
    at_end: dict[tuple[str, str, float, float], list[list[Node]]],
    anchored: set[int],
    near_edge: Callable[[tuple[str, str, float, float], list[Node], list[Node]], bool],
) -> list[str]:
    """Replace an in-pad run that one outside track enters at the pad edge by one straight piece
    from that edge point to the pad centre.

    The router's in-pad wiggles (seen: 0.05 mm zig-zags) make acute angles with the entering
    track that KiCad's ``track_angle`` rule reports; the straight piece keeps the connection
    (KiCad connects a track through a pad's centre) without them. Skipped where it would bend
    sharper than 135 deg against the entering track.
    """
    runs: dict[int, list[list[Node]]] = {}
    for seg in segs:
        if id(seg) in inside and find(id(seg)) in anchored:
            runs.setdefault(find(id(seg)), []).append(seg)
    changes = []
    for root, run in runs.items():
        members = {id(r) for r in run}
        ref, _, fp, pad = inside[id(run[0])]
        entries = [
            (point, o)
            for point, group in at_end.items()
            if any(id(r) in members for r in group) and near_edge(point, fp, pad)
            for o in group
            if id(o) not in members
        ]
        if len(entries) != 1 or root not in anchored:
            continue
        point, outer = entries[0]
        edge = (point[2], point[3])
        o_start, o_end = sexpr.child(outer, "start"), sexpr.child(outer, "end")
        a, b = (_num(o_start, 1), _num(o_start, 2)), (_num(o_end, 1), _num(o_end, 2))
        far = b if math.dist(a, edge) <= math.dist(b, edge) else a
        pad_at = sexpr.child(pad, "at")
        centre = _fp_to_board(fp)(_num(pad_at, 1), _num(pad_at, 2))
        if math.dist(edge, centre) <= tracks.EPS or tracks.interior_angle(far, edge, centre) < tracks.MIN_CORNER_DEG:
            continue
        if len(run) == 1:
            r_start, r_end = sexpr.child(run[0], "start"), sexpr.child(run[0], "end")
            ends_ = {
                (round(_num(r_start, 1), 4), round(_num(r_start, 2), 4)),
                (round(_num(r_end, 1), 4), round(_num(r_end, 2), 4)),
            }
            if ends_ == {(round(edge[0], 4), round(edge[1], 4)), (round(centre[0], 4), round(centre[1], 4))}:
                continue  # already straight
        index = min(project.pcb.index(r) for r in run)
        for r in run:
            project.pcb.remove(r)
        project.pcb.insert(index, _segment_node(outer, edge, centre, _num(sexpr.child(outer, "width"), 1)))
        changes.append(f"{ref}: {len(run)} in-pad track piece(s) replaced by a straight entry to the pad centre")
    return changes


def _npth_holes(pcb: list[Node]) -> list[tuple[str, float, float, float]]:
    """``(ref, x, y, radius)`` of every round NPTH pad, in board coordinates."""
    holes = []
    for fp in sexpr.children(pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or "?"
        at = sexpr.child(fp, "at")
        fx, fy = (float(str(at[1])), float(str(at[2]))) if at is not None else (0.0, 0.0)
        angle = math.radians(float(str(at[3]))) if at is not None and len(at) > 3 else 0.0
        for pad in sexpr.children(fp, "pad"):
            drill = sexpr.child(pad, "drill")
            pad_at = sexpr.child(pad, "at")
            if str(pad[2]) != "np_thru_hole" or drill is None or pad_at is None or len(drill) != 2:
                continue
            px, py = float(str(pad_at[1])), float(str(pad_at[2]))
            x = fx + px * math.cos(angle) + py * math.sin(angle)
            y = fy - px * math.sin(angle) + py * math.cos(angle)
            holes.append((ref, x, y, float(str(drill[1])) / 2))
    return holes


def drop_npth_edge_cuts(project: KicadProject) -> list[str]:
    """Remove board-outline copies of footprint NPTH holes.

    tscircuit exports a footprint's non-plated holes (e.g. a USB-C receptacle's locating
    pegs) both as NPTH pads *and* as polygonal Edge.Cuts cut-outs, so DRC measures every
    nearby pad/track against a 0.5 mm board-edge clearance instead of hole clearance.
    """
    holes = _npth_holes(project.pcb)
    tolerance = 0.02  # mm; tscircuit's polygon vertices sit exactly on the drill circle

    def hole_of(point: list[Node] | None) -> str | None:
        if point is None:
            return None
        x, y = float(str(point[1])), float(str(point[2]))
        for ref, hx, hy, r in holes:
            if abs(math.hypot(x - hx, y - hy) - r) <= tolerance:
                return ref
        return None

    dropped: dict[str, int] = {}
    for line in list(sexpr.children(project.pcb, "gr_line")):
        layer = sexpr.child(line, "layer")
        if layer is None or str(layer[1]) != "Edge.Cuts":
            continue
        ref = hole_of(sexpr.child(line, "start"))
        if ref is not None and ref == hole_of(sexpr.child(line, "end")):
            project.pcb.remove(line)
            dropped[ref] = dropped.get(ref, 0) + 1
    return [f"{ref}: dropped {n} Edge.Cuts segments duplicating its NPTH holes" for ref, n in dropped.items()]


def allow_footprints_in_keepouts(project: KicadProject) -> list[str]:
    """Let footprints (and their pads) overlap keepout zones; keep the track/via/pour restrictions.

    A tscircuit ``<keepout>`` blocks copper; placement against it is already checked by
    tscircuit (honouring ``excludeRefs``). The export still writes ``footprints not_allowed``,
    so e.g. an antenna keepout makes KiCad flag the very module that owns the antenna.
    """
    changes = []
    for zone in sexpr.children(project.pcb, "zone"):
        keepout = sexpr.child(zone, "keepout")
        rules = [] if keepout is None else [sexpr.child(keepout, k) for k in ("footprints", "pads")]
        changed = [r for r in rules if r is not None and str(r[1]) != "allowed"]
        if not changed:
            continue
        for rule in changed:
            rule[1] = Sym("allowed")
        net = sexpr.child(zone, "net_name")
        kinds = " and ".join(str(r[0]) for r in changed)
        changes.append(f"keepout zone{f' ({net[1]})' if net is not None and net[1] else ''}: {kinds} allowed")
    return changes


def standard_board_thickness(project: KicadProject) -> list[str]:
    """tscircuit exports 1.4 mm; most fabs default to 1.6 mm (or the rule profiles' ``board_thickness``)."""
    target = rules.board_thickness(project.config) or STANDARD_BOARD_THICKNESS_MM
    general = sexpr.child(project.pcb, "general")
    thickness = None if general is None else sexpr.child(general, "thickness")
    if thickness is None or float(str(thickness[1])) == target:
        return []
    old = thickness[1]
    thickness[1] = Sym(f"{target:g}")
    return [f"board thickness {old} mm -> {target:g} mm"]
