"""Post-processing fixes applied to the KiCad files tscircuit exports.

Each fix is a function ``(project: KicadProject) -> list[str]`` that edits the
parsed trees in place and returns a human-readable line per change (empty when
there was nothing to do). Register new fixes in ``FIXES``; they run in order.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import functools
import importlib
import itertools
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb import fanout, rules, schgen, sexpr, stackup, testpoints, tracks
from vibepcb.config import NETCLASS_KEYS
from vibepcb.sexpr import Sym

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibepcb.sexpr import Node

STANDARD_BOARD_THICKNESS_MM = 1.6
FOOTPRINT_LIB_NICKNAME = "tscircuit"  # what tscircuit writes into (footprint "tscircuit:<name>")


log = logging.getLogger(__name__)


@dataclass
class KicadProject:
    """The parsed .kicad_pcb / .kicad_sch of one project directory."""

    directory: Path
    pcb: list[Node]
    sch: list[Node]
    pcb_path: Path
    sch_path: Path
    before_track_fixes: list[Node] | None = None  # board copy taken by apply_all (see restore_nets)
    config: dict[str, Any] = field(default_factory=dict)  # boards/<board>/vibepcb.toml (see vibepcb.config)
    routes: dict[str, Any] = field(default_factory=dict)  # boards/<board>/routes.toml (see vibepcb.routes)
    # reroute_tracks: run key -> fingerprint of the copper around it when no route was found (see there)
    reroute_failed: dict[tuple[Any, ...], tuple[Any, ...]] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path, stem: str) -> KicadProject:
        pcb_path = directory / f"{stem}.kicad_pcb"
        sch_path = directory / f"{stem}.kicad_sch"
        return cls(directory, sexpr.load(pcb_path), sexpr.load(sch_path), pcb_path, sch_path)

    def save(self) -> None:
        sexpr.dump(self.pcb, self.pcb_path)
        sexpr.dump(self.sch, self.sch_path)


def _footprints_by_ref(pcb: list[Node]) -> dict[str, list[Node]]:
    refs = {}
    for fp in sexpr.children(pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference")
        if ref:
            refs[ref] = fp
    return refs


# --- fixes ----------------------------------------------------------------------

_AUTO_NET = re.compile(r"^\.|\s>\s|\sto\s")  # e.g. ".R1 > .pin2 to .D1 > .anode"


def _raw_circuit(project: KicadProject) -> Path:
    """tscircuit's circuit JSON of the board: ``out/<board>/.work/raw/`` next to ``kicad/``."""
    stem = project.pcb_path.stem
    work = project.directory.parent / ".work" / "raw" / f"{stem}.circuit.json"
    return work if work.exists() else project.directory.parent / "raw" / f"{stem}.circuit.json"


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


def _num(node: list[Node] | None, i: int, default: float = 0.0) -> float:
    return float(str(node[i])) if node is not None and len(node) > i else default


def _pad_box(pad: list[Node]) -> tuple[float, float, float, float]:
    """Pad copper extent ``(xmin, ymin, xmax, ymax)`` in the pad's own frame (custom shapes included)."""
    size = sexpr.child(pad, "size")
    hw, hh = _num(size, 1) / 2, _num(size, 2) / 2
    box = [-hw, -hh, hw, hh]
    primitives = sexpr.child(pad, "primitives")
    for xy in [] if primitives is None else (n for n in sexpr.walk(primitives) if sexpr.head(n) == "xy"):
        x, y = _num(xy, 1), _num(xy, 2)
        box = [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
    return box[0], box[1], box[2], box[3]


def _inside_pad(fp: list[Node], pad: list[Node], x: float, y: float) -> bool:
    """Whether board point ``(x, y)`` lies within ``pad``'s copper.

    Circle and oval pads are tested by their shape (a diagonal track leaving a round THT pad
    is still inside its bounding box), every other pad by its bounding box.
    """
    at, pad_at = sexpr.child(fp, "at"), sexpr.child(pad, "at")
    fp_angle = math.radians(_num(at, 3))
    dx, dy = x - _num(at, 1), y - _num(at, 2)
    # board -> footprint frame (inverse of KiCad's y-down, counter-clockwise rotation)
    lx = dx * math.cos(fp_angle) - dy * math.sin(fp_angle) - _num(pad_at, 1)
    ly = dx * math.sin(fp_angle) + dy * math.cos(fp_angle) - _num(pad_at, 2)
    # footprint -> pad frame; a pad's angle is stored absolute, footprint rotation included
    pad_angle = math.radians(_num(pad_at, 3)) - fp_angle
    u = lx * math.cos(pad_angle) - ly * math.sin(pad_angle)
    v = lx * math.sin(pad_angle) + ly * math.cos(pad_angle)
    if str(pad[3]) in ("circle", "oval") and sexpr.child(pad, "primitives") is None:
        size = sexpr.child(pad, "size")
        hw, hh = _num(size, 1) / 2, _num(size, 2) / 2
        r = min(hw, hh)  # oval = stadium: distance to its centre line (a circle's is a point)
        return math.hypot(max(abs(u) - (hw - r), 0.0), max(abs(v) - (hh - r), 0.0)) <= r
    xmin, ymin, xmax, ymax = _pad_box(pad)
    return xmin <= u <= xmax and ymin <= v <= ymax


def _pad_on_layer(pad: list[Node], layer: str) -> bool:
    layers = sexpr.child(pad, "layers")
    names = [] if layers is None else [str(x) for x in layers[1:]]
    return layer in names or (layer.endswith(".Cu") and "*.Cu" in names)


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


def fill_schematic_footprints(project: KicadProject) -> list[str]:
    """Copy each footprint's lib id from the PCB into the schematic symbol's empty Footprint field.

    Without it, KiCad's "Update PCB from Schematic" would delete every footprint.
    """
    footprints = _footprints_by_ref(project.pcb)
    changes = []
    for symbol in sexpr.children(project.sch, "symbol"):
        ref = sexpr.property_value(symbol, "Reference")
        prop = sexpr.property_node(symbol, "Footprint")
        fp = footprints.get(ref or "")
        if prop is None or fp is None or prop[2]:
            continue
        prop[2] = str(fp[1])
        changes.append(f"{ref}: Footprint = {fp[1]}")
    return changes


def fix_value_equals_reference(project: KicadProject) -> list[str]:
    """tscircuit sets Value = Reference for parts without a value (J1 -> "J1"); derive one from the footprint."""
    changes = []
    for fp in sexpr.children(project.pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference")
        prop = sexpr.property_node(fp, "Value")
        if prop is None or not ref or prop[2] != ref:
            continue
        value = str(fp[1]).split(":", 1)[-1]
        prop[2] = value
        for symbol in sexpr.children(project.sch, "symbol"):
            sym_value = sexpr.property_node(symbol, "Value")
            if sexpr.property_value(symbol, "Reference") == ref and sym_value is not None:
                sym_value[2] = value
        changes.append(f"{ref}: Value {ref!r} -> {value!r}")
    return changes


def link_footprint_library(project: KicadProject) -> list[str]:
    """Register the exported footprint library under the nickname the board uses.

    tscircuit names footprints ``tscircuit:<name>`` but exports the library as
    ``tscircuit_builtin``; a project-local fp-lib-table bridges the two.
    """
    pretty = next(project.directory.glob("lib/footprints/*.pretty"), None)
    table = project.directory / "fp-lib-table"
    if pretty is None or table.exists():
        return []
    uri = "${KIPRJMOD}/" + pretty.relative_to(project.directory).as_posix()
    tree: list[Node] = [
        Sym("fp_lib_table"),
        [Sym("version"), Sym("7")],
        [
            Sym("lib"),
            [Sym("name"), FOOTPRINT_LIB_NICKNAME],
            [Sym("type"), "KiCad"],
            [Sym("uri"), uri],
            [Sym("options"), ""],
            [Sym("descr"), "Footprints exported by tscircuit"],
        ],
    ]
    sexpr.dump(tree, table)
    return [f"fp-lib-table: {FOOTPRINT_LIB_NICKNAME} -> {uri}"]


LABEL_SNAP_MM = 3.0  # how far a net-name text may sit from the wire it names


def _wire_segments(sch: list[Node]) -> list[tuple[float, float, float, float]]:
    segments = []
    for wire in sexpr.children(sch, "wire"):
        pts = [(_num(xy, 1), _num(xy, 2)) for xy in sexpr.walk(wire) if sexpr.head(xy) == "xy"]
        segments += [(*a, *b) for a, b in itertools.pairwise(pts)]
    return segments


def _mm(value: float) -> str:
    """Coordinate with KiCad's 0.1 um file precision (``:g`` would round 116.0675 to 116.067)."""
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _closest_on_segment(x: float, y: float, seg: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = seg
    length2 = (x2 - x1) ** 2 + (y2 - y1) ** 2
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - x1) * (x2 - x1) + (y - y1) * (y2 - y1)) / length2))
    return x1 + t * (x2 - x1), y1 + t * (y2 - y1)


# label angle -> justification KiCad itself uses for that orientation (text reads away from the anchor)
_LABEL_JUSTIFY = {0: "left", 90: "left", 180: "right", 270: "right"}


def _label_anchor(
    x: float, y: float, segments: list[tuple[float, float, float, float]], pins: set[tuple[float, float]]
) -> tuple[float, float, int] | None:
    """Anchor and angle for a net label whose text sits at ``(x, y)``: the free end of the nearest wire.

    tscircuit's net-label stubs run from a pin outwards; the label goes on the end away from
    the pin, reading away from it, so it doesn't cover pin numbers or names.
    """
    seg = min(segments, key=lambda s: math.dist((x, y), _closest_on_segment(x, y, s)))
    if math.dist((x, y), _closest_on_segment(x, y, seg)) > LABEL_SNAP_MM:
        return None
    a, b = (seg[0], seg[1]), (seg[2], seg[3])
    at_pin = [(round(p[0], 4), round(p[1], 4)) in pins for p in (a, b)]
    if at_pin[0] == at_pin[1]:  # wire between two pins (or two wires): keep the text's place
        px, py = _closest_on_segment(x, y, seg)
        return px, py, 0
    free, fixed = (b, a) if at_pin[0] else (a, b)
    dx, dy = free[0] - fixed[0], free[1] - fixed[1]
    # sheet y grows downwards: a stub going up gets 90 deg
    angle = (0 if dx >= 0 else 180) if abs(dx) >= abs(dy) else (90 if dy < 0 else 270)
    return free[0], free[1], angle


def schematic_net_labels(project: KicadProject) -> list[str]:
    """Turn net-name texts next to wires into real net labels.

    tscircuit draws a net-label stub as a wire plus a plain ``text`` with the net name, so
    in KiCad nothing connects through it and ERC reports the pins as unconnected. Each text
    naming a board net is replaced by a ``label`` on the free end of its stub (see
    :func:`_label_anchor`).
    """
    nets = {str(n[2]) for n in sexpr.children(project.pcb, "net") if len(n) > 2 and n[2]}
    segments = _wire_segments(project.sch)
    pins = set(_pin_positions(project.sch).values())
    changes = []
    for i, node in enumerate(project.sch):
        if sexpr.head(node) != "text" or not isinstance(node, list) or str(node[1]) not in nets or not segments:
            continue
        at = sexpr.child(node, "at")
        anchor = _label_anchor(_num(at, 1), _num(at, 2), segments, pins)
        if anchor is None:
            continue
        px, py, angle = anchor
        label = sexpr.loads(
            f'(label "" (at {_mm(px)} {_mm(py)} {angle}) (effects (font (size 1.27 1.27))'
            f" (justify {_LABEL_JUSTIFY[angle]} bottom)) (uuid {uuid.uuid4()}))"
        )
        label[1] = node[1]
        project.sch[i] = label
        changes.append(f"label {node[1]!r} at ({_mm(px)}, {_mm(py)})")
    return changes


def drop_loose_power_symbols(project: KicadProject) -> list[str]:
    """Remove power symbols whose pin touches no wire, label or other pin.

    tscircuit's schematic layout can leave a GND/supply rail symbol standing alone (seen after
    a part's custom symbol was dropped); it joins nothing, and KiCad's ERC reports its hidden
    power pin as unconnected.
    """
    pins = _pin_positions(project.sch)
    points: dict[tuple[float, float], int] = {}
    for x1, y1, x2, y2 in _wire_segments(project.sch):
        for q in ((round(x1, 4), round(y1, 4)), (round(x2, 4), round(y2, 4))):
            points[q] = points.get(q, 0) + 1
    for kind in ("label", "global_label", "hierarchical_label"):
        for node in sexpr.children(project.sch, kind):
            at = sexpr.child(node, "at")
            if at is not None:
                q = (round(_num(at, 1), 4), round(_num(at, 2), 4))
                points[q] = points.get(q, 0) + 1
    pin_count: dict[tuple[float, float], int] = {}
    for q in pins.values():
        pin_count[q] = pin_count.get(q, 0) + 1
    changes = []
    for symbol in list(sexpr.children(project.sch, "symbol")):
        ref = sexpr.property_value(symbol, "Reference") or ""
        if not ref.startswith("#PWR"):
            continue
        spots = [q for (r, _), q in pins.items() if r == ref]
        if spots and all(q not in points and pin_count[q] == 1 for q in spots):
            project.sch.remove(symbol)
            changes.append(f"{ref} ({sexpr.property_value(symbol, 'Value')}): loose power symbol removed")
    return changes


def _symbol_unit(name: str) -> int:
    """Unit of a library sub-symbol ``<name>_<unit>_<style>`` (0: drawn in every unit)."""
    m = re.search(r"_(\d+)_(\d+)$", name)
    return int(m[1]) if m else 0


def _pin_positions(sch: list[Node]) -> dict[tuple[str, str], tuple[float, float]]:
    """``(reference, pin number) -> (x, y)`` connection point of every placed symbol pin."""
    lib = sexpr.child(sch, "lib_symbols")
    lib_pins: dict[str, list[tuple[str, float, float, int]]] = {}  # unit 0: common to all units
    for sym in [] if lib is None else sexpr.children(lib, "symbol"):
        pins = lib_pins.setdefault(str(sym[1]), [])
        for sub in [sym, *sexpr.children(sym, "symbol")]:
            unit = _symbol_unit(str(sub[1])) if sub is not sym else 0
            pins += [
                (str(num[1]), _num(pin_at, 1), _num(pin_at, 2), unit)
                for pin in sexpr.children(sub, "pin")
                if (pin_at := sexpr.child(pin, "at")) is not None and (num := sexpr.child(pin, "number")) is not None
            ]
    positions: dict[tuple[str, str], tuple[float, float]] = {}
    for sym in sexpr.children(sch, "symbol"):
        ref, lib_id, at = sexpr.property_value(sym, "Reference"), sexpr.child(sym, "lib_id"), sexpr.child(sym, "at")
        if not ref or lib_id is None or at is None or sexpr.child(sym, "mirror") is not None:
            continue
        angle = math.radians(_num(at, 3))
        unit_node = sexpr.child(sym, "unit")
        unit = int(_num(unit_node, 1, 1)) if unit_node is not None else 1
        for number, px, py, pin_unit in lib_pins.get(str(lib_id[1]), []):
            if pin_unit not in {0, unit}:
                continue
            # library coordinates are y-up, the sheet is y-down
            x = _num(at, 1) + px * math.cos(angle) - py * math.sin(angle)
            y = _num(at, 2) - (px * math.sin(angle) + py * math.cos(angle))
            positions[ref, number] = (round(x, 4), round(y, 4))
    return positions


def schematic_no_connects(project: KicadProject) -> list[str]:
    """Put a no-connect flag on every schematic pin whose PCB pad has no net.

    tscircuit already warns about pins left without a trace, and marks NC pins in its own
    schematic, but exports no ``no_connect`` items, so KiCad ERC fails on every unused pin.
    """
    unused: set[tuple[str, str]] = set()
    for ref, fp in _footprints_by_ref(project.pcb).items():
        pads: dict[str, bool] = {}
        for pad in sexpr.children(fp, "pad"):
            if str(pad[1]):
                pads[str(pad[1])] = pads.get(str(pad[1]), False) or sexpr.child(pad, "net") is not None
        unused |= {(ref, number) for number, has_net in pads.items() if not has_net}

    taken = {
        (round(_num(at, 1), 4), round(_num(at, 2), 4))
        for nc in sexpr.children(project.sch, "no_connect")
        if (at := sexpr.child(nc, "at"))
    }
    for x1, y1, x2, y2 in _wire_segments(project.sch):
        taken |= {(round(x1, 4), round(y1, 4)), (round(x2, 4), round(y2, 4))}

    changes = []
    for (ref, number), (x, y) in sorted(_pin_positions(project.sch).items()):
        if (ref, number) not in unused or (x, y) in taken:
            continue
        project.sch.append(
            [Sym("no_connect"), [Sym("at"), Sym(_mm(x)), Sym(_mm(y))], [Sym("uuid"), Sym(str(uuid.uuid4()))]]
        )
        taken.add((x, y))
        changes.append(f"{ref} pin {number}: no-connect")
    return changes


RAIL_LIB_PREFIX = "Custom:rail_"  # tscircuit's GND (rail_down) / supply (rail_up) symbols
PWR_FLAG_LIB = """(symbol "power:PWR_FLAG" (power) (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes))
  (exclude_from_sim no) (in_bom yes) (on_board yes)
  (property "Reference" "#FLG" (at 0 1.905 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Value" "PWR_FLAG" (at 0 3.81 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Description" "Special symbol for telling ERC where power comes from" (at 0 0 0)
    (effects (font (size 1.27 1.27)) (hide yes)))
  (symbol "PWR_FLAG_0_0" (pin power_out line (at 0 0 90) (length 0)
    (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27))))))
  (symbol "PWR_FLAG_0_1" (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27))
    (stroke (width 0) (type default)) (fill (type none))))
  (embedded_fonts no))"""


def _hide(prop: list[Node], *, hidden: bool) -> None:
    effects = sexpr.child(prop, "effects")
    if effects is None:
        return
    effects[:] = [x for x in effects if sexpr.head(x) != "hide"]
    if hidden:
        effects.append([Sym("hide"), Sym("yes")])


def schematic_power_symbols(project: KicadProject) -> list[str]:
    """Make tscircuit's GND / supply rail symbols real KiCad power symbols, plus one PWR_FLAG per rail.

    They are exported as ordinary parts (Reference = net name, in BOM, on board) with a
    passive pin, so every rail symbol is a net of its own: the schematic has a dozen GND
    nets and "Update PCB from Schematic" would add footprint-less parts. As power symbols
    (hidden power-input pin, ``#PWR`` reference, Value = net) they join globally by name.
    Every pin in tscircuit's export is passive, so nothing drives the rails; a PWR_FLAG on
    each tells ERC the rail is supplied (by the USB connector / regulator).
    """
    lib = sexpr.child(project.sch, "lib_symbols")
    rails = [s for s in ([] if lib is None else sexpr.children(lib, "symbol")) if str(s[1]).startswith(RAIL_LIB_PREFIX)]
    if lib is None or not rails or all(sexpr.child(s, "power") is not None for s in rails):
        return []
    for rail in rails:
        if sexpr.child(rail, "power") is None:
            rail.insert(2, [Sym("power")])
        for pin in (n for n in sexpr.walk(rail) if sexpr.head(n) == "pin"):
            pin[1] = Sym("power_in")
            if sexpr.child(pin, "hide") is None:
                pin.append([Sym("hide"), Sym("yes")])

    changes = []
    instances = [
        s
        for s in sexpr.children(project.sch, "symbol")
        if (lib_id := sexpr.child(s, "lib_id")) is not None and str(lib_id[1]).startswith(RAIL_LIB_PREFIX)
    ]
    for n, symbol in enumerate(instances, start=1):
        ref, net = f"#PWR{n:02d}", sexpr.property_value(symbol, "Value") or "?"
        ref_prop, value_prop = sexpr.property_node(symbol, "Reference"), sexpr.property_node(symbol, "Value")
        if ref_prop is None or value_prop is None:
            continue
        ref_prop[2] = ref
        _hide(ref_prop, hidden=True)
        _hide(value_prop, hidden=False)
        for item in ("in_bom", "on_board"):
            if (flag := sexpr.child(symbol, item)) is not None:
                flag[1] = Sym("no")
        for reference in (x for x in sexpr.walk(symbol) if sexpr.head(x) == "reference"):
            reference[1] = ref
        changes.append(f"{ref}: power symbol {net!r}")

    lib.append(sexpr.loads(PWR_FLAG_LIB))
    pins = _pin_positions(project.sch)
    flagged: set[str] = set()
    for symbol in instances:
        rail_net, rail_ref = sexpr.property_value(symbol, "Value"), sexpr.property_value(symbol, "Reference")
        path = next((x for x in sexpr.walk(symbol) if sexpr.head(x) == "path"), None)
        if not rail_net or not rail_ref or rail_net in flagged or (rail_ref, "1") not in pins or path is None:
            continue
        flagged.add(rail_net)
        x, y = (_mm(v) for v in pins[rail_ref, "1"])
        flag_ref = f"#FLG{len(flagged):02d}"
        project.sch.append(
            sexpr.loads(
                f'(symbol (lib_id "power:PWR_FLAG") (at {x} {y} 0) (unit 1) (exclude_from_sim no) (in_bom yes)'
                f" (on_board yes) (dnp no) (uuid {uuid.uuid4()})"
                f' (property "Reference" "{flag_ref}" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))'
                f' (property "Value" "PWR_FLAG" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))'
                f' (property "Footprint" "" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))'
                f' (property "Datasheet" "~" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))'
                f' (pin "1" (uuid {uuid.uuid4()}))'
                f' (instances (project "" (path "{path[1]}" (reference "{flag_ref}") (unit 1)))))'
            )
        )
        changes.append(f"{flag_ref}: PWR_FLAG on {rail_net!r}")
    return changes


def add_part_numbers(project: KicadProject) -> list[str]:
    """Copy ``manufacturerPartNumber`` / JLCPCB part numbers from the circuit JSON onto the symbols.

    tscircuit's KiCad export drops them, so the KiCad BOM could not say what to buy.
    Adds hidden ``MPN`` and ``LCSC`` fields (read from ``../raw/<stem>.circuit.json``) and
    uses the MPN as Value where the export left it empty.
    """
    circuit = _raw_circuit(project)
    if not circuit.exists():
        return []
    parts: dict[str, dict[str, str]] = {}
    for element in json.loads(circuit.read_text(encoding="utf-8")):
        if element.get("type") != "source_component":
            continue
        fields = {}
        if mpn := element.get("manufacturer_part_number"):
            fields["MPN"] = str(mpn)
        if lcsc := (element.get("supplier_part_numbers") or {}).get("jlcpcb"):
            fields["LCSC"] = str(lcsc[0])
        if fields:
            parts[str(element.get("name"))] = fields

    changes = []
    for symbol in sexpr.children(project.sch, "symbol"):
        ref = sexpr.property_value(symbol, "Reference") or ""
        template = sexpr.property_node(symbol, "Datasheet")
        if ref not in parts or template is None:
            continue
        for name, value in parts[ref].items():
            if sexpr.property_node(symbol, name) is not None:
                continue
            prop = [item for item in copy.deepcopy(template) if sexpr.head(item) != "id"]
            prop[1], prop[2] = name, value
            effects = sexpr.child(prop, "effects")
            if effects is not None and sexpr.child(effects, "hide") is None:
                effects.append([Sym("hide"), Sym("yes")])
            symbol.insert(symbol.index(template) + 1, prop)
            changes.append(f"{ref}: {name} = {value}")
        value_prop = sexpr.property_node(symbol, "Value")
        if value_prop is not None and not value_prop[2] and "MPN" in parts[ref]:  # e.g. LEDs export no value
            value_prop[2] = parts[ref]["MPN"]
            changes.append(f"{ref}: Value = {value_prop[2]}")
    return changes


def mark_dnp_parts(project: KicadProject) -> list[str]:
    """Mark tscircuit's ``doNotPlace`` parts DNP on the symbol and the footprint.

    Neither the KiCad export nor ``schgen`` carries the flag, so unpopulated pads (ISP headers,
    antenna / touch pads) landed in the fab BOM and CPL. ``fab`` exports both with ``--exclude-dnp``
    (BOM from the symbols, CPL from the footprints), so both get it.
    """
    circuit = _raw_circuit(project)
    if not circuit.exists():
        return []
    elements = json.loads(circuit.read_text(encoding="utf-8"))
    names = {e["source_component_id"]: str(e.get("name")) for e in elements if e.get("type") == "source_component"}
    dnp = {
        names[e["source_component_id"]]
        for e in elements
        if e.get("type") == "pcb_component" and e.get("do_not_place") and e.get("source_component_id") in names
    }
    changes = []
    for symbol in sexpr.children(project.sch, "symbol"):
        ref = sexpr.property_value(symbol, "Reference") or ""
        flag = sexpr.child(symbol, "dnp")
        if ref in dnp and (flag is None or flag[1] != Sym("yes")):
            if flag is None:
                symbol.append([Sym("dnp"), Sym("yes")])
            else:
                flag[1] = Sym("yes")
            changes.append(f"{ref}: symbol DNP")
    for fp in sexpr.children(project.pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or ""
        attr = sexpr.child(fp, "attr")
        if ref not in dnp or (attr is not None and Sym("dnp") in attr):
            continue
        if attr is None:
            props = [i for i, item in enumerate(fp) if sexpr.head(item) == "property"]
            fp.insert(props[-1] + 1 if props else len(fp), [Sym("attr"), Sym("dnp")])
        else:
            attr.append(Sym("dnp"))
        changes.append(f"{ref}: footprint DNP")
    return changes


def exclude_testpoints_from_bom(project: KicadProject) -> list[str]:
    """Keep ``<testpoint>`` pads out of the fab BOM and CPL: a bare pad is nothing to buy or place.

    The export gives the test point a symbol "in BOM" and a footprint without attributes, so JLCPCB
    would list it as a part. The symbol goes ``in_bom no``; the footprint gets
    ``(attr board_only exclude_from_pos_files exclude_from_bom)`` (not DNP: the pad is meant to be on the board).
    """
    refs = {p.ref for p in testpoints.parts(project.pcb) if testpoints.is_test_point(p)}
    changes = []
    for symbol in sexpr.children(project.sch, "symbol"):
        flag = sexpr.child(symbol, "in_bom")
        if (sexpr.property_value(symbol, "Reference") or "") in refs and flag is not None and flag[1] != Sym("no"):
            flag[1] = Sym("no")
            changes.append(f"{sexpr.property_value(symbol, 'Reference')}: symbol out of the BOM")
    wanted = [Sym("board_only"), Sym("exclude_from_pos_files"), Sym("exclude_from_bom")]
    for fp in sexpr.children(project.pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or ""
        if ref not in refs:
            continue
        attr = sexpr.child(fp, "attr")
        if attr is None:
            props = [i for i, item in enumerate(fp) if sexpr.head(item) == "property"]
            fp.insert(props[-1] + 1 if props else len(fp), [Sym("attr"), *wanted])
        elif missing := [w for w in wanted if w not in attr]:
            attr.extend(missing)
        else:
            continue
        changes.append(f"{ref}: footprint board_only, out of BOM and CPL")
    return changes


FOOTPRINT_INTERNAL_HOLE_CLEARANCE_MM = 0.15  # still comfortably within common 2-layer fab limits


def footprint_internal_clearance_rule(project: KicadProject) -> list[str]:
    """Add a custom DRC rule: hole clearance *within* one footprint follows its land pattern.

    Imported manufacturer land patterns (e.g. a USB-C receptacle's GND pads 0.17 mm from
    its locating-peg holes) can sit below the board-wide hole clearance; that geometry is
    the part's, not something the layout can fix.
    """
    dru = project.pcb_path.with_suffix(".kicad_dru")
    if dru.exists():
        return []
    dru.write_text(
        "(version 1)\n"
        '(rule "footprint-internal hole clearance"\n'
        '  (condition "A.Parent == B.Parent")\n'
        f"  (constraint hole_clearance (min {FOOTPRINT_INTERNAL_HOLE_CLEARANCE_MM}mm)))\n",
        encoding="utf-8",
    )
    return [f"{dru.name}: footprint-internal hole clearance >= {FOOTPRINT_INTERNAL_HOLE_CLEARANCE_MM} mm"]


THERMAL_GAP_MM = 0.3
THERMAL_SPOKE_MM = 0.3


def zone_thermal_reliefs(project: KicadProject) -> list[str]:
    """Connect pads to copper zones through thermal-relief spokes instead of solid copper.

    tscircuit's pours connect pads solid (``connect_pads yes``): a 0402 with one pad in a
    plane and the other on a thin track heats unevenly in reflow (tombstoning) and the
    plane-side pad is hard to rework. Spokes are narrowed from KiCad's 0.5 mm, which is as
    wide as a 0402 pad and so barely relieves anything.
    """
    changes = []
    for zone in sexpr.children(project.pcb, "zone"):
        connect, fill = sexpr.child(zone, "connect_pads"), sexpr.child(zone, "fill")
        if sexpr.child(zone, "keepout") is not None or connect is None or fill is None:
            continue
        changed = False
        if len(connect) > 1 and connect[1] == Sym("yes"):
            del connect[1]
            changed = True
        for key, value in (("thermal_gap", THERMAL_GAP_MM), ("thermal_bridge_width", THERMAL_SPOKE_MM)):
            node = sexpr.child(fill, key)
            if node is not None and float(str(node[1])) != value:
                node[1] = Sym(_mm(value))
                changed = True
        if changed:
            net = sexpr.child(zone, "net_name")
            layer = sexpr.child(zone, "layer")
            changes.append(
                f"zone {net[1] if net is not None else '?'} on {layer[1] if layer is not None else '?'}: thermal reliefs"
            )
    return changes


THERMAL_SPOKE_RULE = "zone pads: one thermal spoke is enough"


def thermal_spoke_rule(project: KicadProject) -> list[str]:
    """Accept a single thermal spoke per pad (KiCad wants 2 and reports ``starved_thermal`` errors).

    With thermal reliefs on, a small pad hemmed in by its own tracks may only get one spoke
    from the pour; it is still connected (often by a track as well), so where the router put
    its tracks shouldn't fail DRC.
    """
    dru = project.pcb_path.with_suffix(".kicad_dru")
    text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
    if THERMAL_SPOKE_RULE in text:
        return []
    text += f'(rule "{THERMAL_SPOKE_RULE}"\n  (constraint min_resolved_spokes 1))\n'
    dru.write_text(text, encoding="utf-8")
    return [f"{dru.name}: min. 1 thermal spoke per pad"]


ROUTER_CLEARANCE_RULE = "router clearance: float tolerance"
ROUTER_CLEARANCE_SLACK_MM = 0.005  # far below any fab tolerance (~0.025 mm)


def router_clearance_tolerance(project: KicadProject) -> list[str]:
    """Accept copper gaps a few microns short of the net-class clearance.

    The autorouter targets exactly the board's clearance, which becomes the KiCad net-class
    clearance, and floating-point error leaves some via/track gaps 1-2 um short (seen:
    0.1986 mm for 0.2 mm) - errors already in the raw export, no layout change fixes them.
    """
    dru = project.pcb_path.with_suffix(".kicad_dru")
    text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
    if ROUTER_CLEARANCE_RULE in text:
        return []
    clearance = _design_clearances(project)[0] - ROUTER_CLEARANCE_SLACK_MM
    text += (
        f'(rule "{ROUTER_CLEARANCE_RULE}"\n'
        "  (condition \"A.NetClass == 'Default' && B.NetClass == 'Default'\")\n"  # other classes keep theirs
        f"  (constraint clearance (min {_mm(clearance)}mm)))\n"
    )
    dru.write_text(text, encoding="utf-8")
    return [f"{dru.name}: copper clearance >= {_mm(clearance)} mm"]


REF_TEXT_SIZE_MM = 0.8  # common fab minimum for legible silkscreen
REF_TEXT_THICKNESS_MM = 0.15
REF_CLEARANCE_MM = 0.15  # text box to pads / silk / other text
REF_EDGE_CLEARANCE_MM = 0.3
CHAR_ADVANCE = 0.8  # KiCad stroke font: average glyph advance per text size (rough)

type Box = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax (board, y down)


def _fp_to_board(fp: list[Node]) -> Callable[[float, float], tuple[float, float]]:
    at = sexpr.child(fp, "at")
    fx, fy, angle = _num(at, 1), _num(at, 2), math.radians(_num(at, 3))
    cos, sin = math.cos(angle), math.sin(angle)
    return lambda px, py: (fx + px * cos + py * sin, fy - px * sin + py * cos)


def _bbox(points: list[tuple[float, float]], grow: float = 0.0) -> Box:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _text_box(x: float, y: float, text: str, size: float, angle: float, effects: list[Node] | None = None) -> Box:
    """Approximate extent of a stroke-font text (``justify`` honoured, angle 0 or 90)."""
    w, h = len(text) * size * CHAR_ADVANCE + REF_TEXT_THICKNESS_MM, size + REF_TEXT_THICKNESS_MM
    justify = sexpr.child(effects, "justify") if effects is not None else None
    flags = {str(f) for f in justify[1:]} if justify is not None else set()
    dx = w / 2 if "left" in flags else -w / 2 if "right" in flags else 0.0
    dy = -h / 2 if "bottom" in flags else h / 2 if "top" in flags else 0.0
    if round(angle) % 180 == 90:
        return x + dy - h / 2, y - dx - w / 2, x + dy + h / 2, y - dx + w / 2
    return x + dx - w / 2, y + dy - h / 2, x + dx + w / 2, y + dy + h / 2


def _graphic_points(node: list[Node]) -> list[tuple[float, float]]:
    if sexpr.head(node) == "fp_circle":
        center, end = sexpr.child(node, "center"), sexpr.child(node, "end")
        cx, cy = _num(center, 1), _num(center, 2)
        r = math.hypot(_num(end, 1) - cx, _num(end, 2) - cy)
        return [(cx - r, cy - r), (cx + r, cy + r)]
    if sexpr.head(node) == "fp_rect":
        (x1, y1), (x2, y2) = ((_num(p, 1), _num(p, 2)) for p in (sexpr.child(node, "start"), sexpr.child(node, "end")))
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
    keys = ("start", "mid", "end")
    pts = [(_num(p, 1), _num(p, 2)) for k in keys if (p := sexpr.child(node, k)) is not None]
    return pts + [(_num(xy, 1), _num(xy, 2)) for xy in sexpr.walk(node) if sexpr.head(xy) == "xy"]


def _segment_boxes(points: list[tuple[float, float]], grow: float) -> list[Box]:
    if len(points) == 1:
        return [_bbox(points, grow)]
    return [_bbox([a, b], grow) for a, b in itertools.pairwise(points)]


def _layer(node: list[Node]) -> str:
    layer = sexpr.child(node, "layer")
    return "" if layer is None else str(layer[1])


@dataclass
class _Footprint:
    ref: str
    node: list[Node]
    prop: list[Node]
    body: Box  # courtyard (or pads) extent
    keepouts: list[Box]  # own pads + silk


def _pad_corners(pad: list[Node], to_board: Callable[[float, float], tuple[float, float]]) -> list[tuple[float, float]]:
    """Board coordinates of the pad's copper bounding box corners (pad rotation included)."""
    pad_at = sexpr.child(pad, "at")
    angle = math.radians(_num(pad_at, 3))
    px, py = to_board(_num(pad_at, 1), _num(pad_at, 2))
    xmin, ymin, xmax, ymax = _pad_box(pad)
    # pad frame -> board: rotate by the pad's absolute angle around its board position
    return [
        (px + u * math.cos(angle) + v * math.sin(angle), py - u * math.sin(angle) + v * math.cos(angle))
        for u, v in ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax))
    ]


def _footprint_geometry(fp: list[Node], side: str = "F") -> _Footprint | None:
    """Reference, body and keepouts of a part on ``side`` (``F`` top, ``B`` bottom); None for the other side's
    parts and for hidden references. Bottom footprints carry their geometry already flipped, on ``B.*`` layers."""
    # tscircuit writes a visible ``(fp_text reference ...)`` next to a hidden Reference property
    # (KiCad merges the two on save); both keep the text at index 2
    prop = next((t for t in sexpr.children(fp, "fp_text") if str(t[1]) == "reference"), None)
    prop = prop if prop is not None else sexpr.property_node(fp, "Reference")
    if prop is None or not prop[2] or _layer(prop) != f"{side}.SilkS" or _layer(fp) != f"{side}.Cu":
        return None
    effects = sexpr.child(prop, "effects")
    for parent in (prop, effects):
        if parent is not None and (hide := sexpr.child(parent, "hide")) is not None and str(hide[-1]) != "no":
            return None
    to_board = _fp_to_board(fp)
    keepouts, body_points = [], []
    for pad in sexpr.children(fp, "pad"):
        if not (_pad_on_layer(pad, f"{side}.Cu") or str(pad[2]) == "np_thru_hole"):
            continue
        corners = _pad_corners(pad, to_board)
        keepouts.append(_bbox(corners, REF_CLEARANCE_MM))
        body_points += corners
    courtyard = []
    for item in fp:
        if (
            not isinstance(item, list)
            or not str(sexpr.head(item) or "").startswith("fp_")
            or sexpr.head(item) == "fp_text"
        ):
            continue
        points = [to_board(x, y) for x, y in _graphic_points(item)]
        if not points:
            continue
        if _layer(item) == f"{side}.SilkS":
            keepouts += _segment_boxes(points, REF_CLEARANCE_MM)
        elif _layer(item) == f"{side}.CrtYd":
            courtyard += points
    body_points = courtyard or body_points
    if not body_points:
        return None
    return _Footprint(str(prop[2]), fp, prop, _bbox(body_points), keepouts)


def _ref_candidates(body: Box, w: float, h: float) -> list[tuple[float, float, float]]:
    """``(x, y, angle)`` text centres around ``body``, preferred first."""
    xmin, ymin, xmax, ymax = body
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    out = []
    for gap in (0.05, 0.35, 0.7):
        for shift in (0.0, -0.5, 0.5, -1.0, 1.0, -1.5, 1.5):
            out += [
                (cx + shift, ymin - gap - h / 2, 0.0),  # above
                (cx + shift, ymax + gap + h / 2, 0.0),  # below
                (xmin - gap - h / 2, cy + shift, 90.0),  # left, reading upwards
                (xmax + gap + h / 2, cy + shift, 90.0),  # right
                (xmin - gap - w / 2, cy + shift, 0.0),
                (xmax + gap + w / 2, cy + shift, 0.0),
            ]
    return out


def _grow(box: Box, d: float) -> Box:
    return box[0] - d, box[1] - d, box[2] + d, box[3] + d


def _fixed_obstacles(pcb: list[Node], footprints: list[_Footprint], side: str = "F") -> list[Box]:
    """Board-level things a reference on ``side`` must not cover: edges, that side's silkscreen, vias, and the
    pads of every other part on that side (reference-less parts, the other side's through-hole pads)."""
    obstacles: list[Box] = []
    for item in pcb:
        if not isinstance(item, list):
            continue
        kind, layer = sexpr.head(item), _layer(item)
        if kind in ("gr_line", "gr_rect", "gr_arc", "gr_poly") and layer in ("Edge.Cuts", f"{side}.SilkS"):
            grow = REF_EDGE_CLEARANCE_MM if layer == "Edge.Cuts" else REF_CLEARANCE_MM
            obstacles += _segment_boxes(_graphic_points(item), grow)
        elif kind == "gr_text" and layer == f"{side}.SilkS":
            at, effects = sexpr.child(item, "at"), sexpr.child(item, "effects")
            font = None if effects is None else sexpr.child(effects, "font")
            size = _num(None if font is None else sexpr.child(font, "size"), 1, 1.0)
            box = _text_box(_num(at, 1), _num(at, 2), str(item[1]), size, _num(at, 3), effects)
            obstacles.append(_grow(box, REF_CLEARANCE_MM))
        elif kind == "via":
            at, r = sexpr.child(item, "at"), _num(sexpr.child(item, "size"), 1) / 2
            obstacles.append(_grow((_num(at, 1), _num(at, 2), _num(at, 1), _num(at, 2)), r + REF_CLEARANCE_MM))
    with_ref = {id(g.node) for g in footprints}
    for fp in sexpr.children(pcb, "footprint"):  # mounting holes etc.
        if id(fp) in with_ref:
            continue
        to_board = _fp_to_board(fp)
        for pad in sexpr.children(fp, "pad"):
            if not (_pad_on_layer(pad, f"{side}.Cu") or "thru_hole" in str(pad[2])):
                continue  # an SMD pad on the other side
            pad_size, pad_at = sexpr.child(pad, "size"), sexpr.child(pad, "at")
            x, y = to_board(_num(pad_at, 1), _num(pad_at, 2))
            r = max(_num(pad_size, 1), _num(pad_size, 2)) / 2
            obstacles.append(_grow((x, y, x, y), r + REF_CLEARANCE_MM))
    return obstacles


def _set_ref_font(text: list[Node]) -> bool:
    """Reference text to REF_TEXT_SIZE_MM, centred (bottom text stays mirrored); returns whether the size changed."""
    effects = sexpr.child(text, "effects")
    font = None if effects is None else sexpr.child(effects, "font")
    if effects is None or font is None:
        return False
    mirrored = any(sexpr.head(e) == "justify" and Sym("mirror") in e for e in effects)
    effects[:] = [e for e in effects if sexpr.head(e) != "justify"]
    if mirrored:
        effects.append([Sym("justify"), Sym("mirror")])
    changed = False
    size, thickness = Sym(_mm(REF_TEXT_SIZE_MM)), Sym(_mm(REF_TEXT_THICKNESS_MM))
    for key, value in (("size", [size, size]), ("thickness", [thickness])):
        node = sexpr.child(font, key)
        if node is None:
            font.append([Sym(key), *value])
        elif [str(v) for v in node[1:]] != [str(v) for v in value]:
            node[1:] = value
        else:
            continue
        changed = True
    return changed


def place_reference_designators(project: KicadProject) -> list[str]:
    """Size reference designators legibly and move each to a free spot next to its part.

    tscircuit writes 0.4 mm references (below every fab's minimum) at a fixed offset, so
    they land on pads, on neighbours' silkscreen, on each other or beyond the board edge.
    Each reference gets 0.8 mm text, placed at the first candidate around its courtyard
    that stays inside the board and clear of pads, vias, holes, silkscreen, board-level text
    and references already placed; parts' bodies (courtyards) are avoided when possible.
    Top and bottom parts are placed separately, each against its own side's silkscreen and pads.
    """
    pcb = project.pcb
    edge_points = [
        p for item in pcb if isinstance(item, list) and sexpr.head(item) in ("gr_line", "gr_rect", "gr_arc", "gr_poly")
        and _layer(item) == "Edge.Cuts" for p in _graphic_points(item)
    ]  # fmt: skip
    if not edge_points:
        return []
    board = _bbox(edge_points, -REF_EDGE_CLEARANCE_MM)
    changes = []
    for side in ("F", "B"):
        changes += _place_side_references(pcb, board, side)
    return changes


def _place_side_references(pcb: list[Node], board: Box, side: str) -> list[str]:
    footprints = [geo for fp in sexpr.children(pcb, "footprint") if (geo := _footprint_geometry(fp, side)) is not None]
    hard = _fixed_obstacles(pcb, footprints, side) + [box for geo in footprints for box in geo.keepouts]
    changes = []
    placed: list[Box] = []
    h = REF_TEXT_SIZE_MM + REF_TEXT_THICKNESS_MM
    for geo in sorted(footprints, key=lambda geo: _natural_key(geo.ref)):
        resized = _set_ref_font(geo.prop)
        w = len(geo.ref) * REF_TEXT_SIZE_MM * CHAR_ADVANCE + REF_TEXT_THICKNESS_MM
        bodies = [other.body for other in footprints if other is not geo]
        choice = None
        for obstacles in (hard + placed + bodies, hard + placed):  # avoid other parts' bodies if possible
            for x, y, angle in _ref_candidates(geo.body, w, h):
                box = _text_box(x, y, geo.ref, REF_TEXT_SIZE_MM, angle)
                inside = board[0] <= box[0] and board[1] <= box[1] and box[2] <= board[2] and box[3] <= board[3]
                if inside and not any(_overlaps(box, o) for o in obstacles):
                    choice = (x, y, angle, box)
                    break
            if choice is not None:
                break
        if choice is None:
            if resized:
                changes.append(f"{geo.ref}: reference resized, no free spot found - check by eye")
            continue
        x, y, angle, box = choice
        placed.append(_grow(box, REF_CLEARANCE_MM))
        at, fp_at = sexpr.child(geo.prop, "at"), sexpr.child(geo.node, "at")
        a = math.radians(_num(fp_at, 3))
        dx, dy = x - _num(fp_at, 1), y - _num(fp_at, 2)
        local = [
            Sym("at"),
            Sym(_mm(dx * math.cos(a) - dy * math.sin(a))),
            Sym(_mm(dx * math.sin(a) + dy * math.cos(a))),
            Sym(_mm(angle)),
        ]
        if at is not None and [str(v) for v in at] != [str(v) for v in local]:
            at[:] = local
            changes.append(f"{geo.ref}: reference at ({_mm(x)}, {_mm(y)}) {angle:g} deg")
        elif resized:
            changes.append(f"{geo.ref}: reference resized")
    return changes


def _natural_key(ref: str) -> tuple[str, int]:
    m = re.match(r"(\D*)(\d*)", ref)
    return (m[1], int(m[2] or 0)) if m else (ref, 0)


PAPER_SIZES_MM = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}
SHEET_FRAME_MM = 12.0  # border + margin on each side
TITLE_BLOCK_MM = (115.0, 40.0)  # KiCad's title block in the bottom-right corner (+ margin)
SCHEMATIC_GRID_MM = 1.27
_SHEET_META = {"version", "generator", "generator_version", "uuid", "paper", "title_block", "lib_symbols", "sheet_instances", "symbol_instances", "embedded_fonts"}  # fmt: skip
_COORDS = {"at", "xy", "start", "end", "center", "mid"}


def _sheet_placement(points: list[tuple[float, float]]) -> tuple[str, float, float] | None:
    """Smallest paper + grid-aligned shift that puts ``points`` inside the frame, clear of the title block.

    Tries the drawing centred on the sheet, then pushed to the top, then to the top-left.
    """
    xmin, ymin, xmax, ymax = _bbox(points, 2 * SCHEMATIC_GRID_MM)  # room for texts sticking out
    grid, frame = SCHEMATIC_GRID_MM, SHEET_FRAME_MM
    for paper, (w, h) in PAPER_SIZES_MM.items():
        if xmax - xmin > w - 2 * frame or ymax - ymin > h - 2 * frame:
            continue
        block_x, block_y = w - frame - TITLE_BLOCK_MM[0], h - frame - TITLE_BLOCK_MM[1]
        centred_x, centred_y = (w - xmax - xmin) / 2, (h - ymax - ymin) / 2
        for dx, dy in ((centred_x, centred_y), (centred_x, frame - ymin), (frame - xmin, frame - ymin)):
            dx, dy = round(dx / grid) * grid, round(dy / grid) * grid
            if not any(x + dx > block_x and y + dy > block_y for x, y in points):
                return paper, dx, dy
    return None


def fit_schematic_sheet(project: KicadProject) -> list[str]:
    """Put the schematic on the smallest landscape ISO sheet that holds it, centred, with a title block.

    tscircuit picks a huge sheet (A0/A1) and draws in one corner of it, so the PDF is
    mostly empty paper with unreadably small parts, and the title block stays empty.
    """
    sch = project.sch
    items = [n for n in sch if isinstance(n, list) and sexpr.head(n) not in _SHEET_META]
    points = [(_num(c, 1), _num(c, 2)) for item in items for c in sexpr.walk(item) if sexpr.head(c) in _COORDS]
    points += list(_pin_positions(sch).values())
    if not points:
        return []
    placement = _sheet_placement(points)
    if placement is None:
        return []
    paper, dx, dy = placement

    changes = []
    paper_node = sexpr.child(sch, "paper")
    if paper_node is None:
        paper_node = [Sym("paper"), paper]
        sch.insert(next(i for i, n in enumerate(sch) if sexpr.head(n) == "lib_symbols"), paper_node)
    if str(paper_node[1]) != paper:
        changes.append(f"paper {paper_node[1]} -> {paper}")
        paper_node[1:] = [paper]
    if abs(dx) >= SCHEMATIC_GRID_MM / 2 or abs(dy) >= SCHEMATIC_GRID_MM / 2:
        for item in items:
            for c in sexpr.walk(item):
                if sexpr.head(c) in _COORDS and len(c) > 2:
                    c[1], c[2] = Sym(_mm(_num(c, 1) + dx)), Sym(_mm(_num(c, 2) + dy))
        changes.append(f"drawing moved by ({_mm(dx)}, {_mm(dy)}) mm onto the sheet")
    if sexpr.child(sch, "title_block") is None:
        stem = project.sch_path.stem
        today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
        block: list[Node] = [
            Sym("title_block"),
            [Sym("title"), stem],
            [Sym("date"), today],
            [Sym("comment"), Sym("1"), f"VibePCB: generated from boards/{stem}/index.circuit.tsx"],
        ]
        sch.insert(sch.index(paper_node) + 1, block)
        changes.append(f"title block: {stem}")
    return changes


# --- track geometry: copper extraction for vibepcb.tracks ---------------------------

# every copper layer KiCad knows: "*.Cu" pads, vias and the board edge reach the inner layers
# of 4+ layer boards too (a layer that does not exist simply carries no copper)
COPPER_LAYERS = ("F.Cu", *(f"In{i}.Cu" for i in range(1, 31)), "B.Cu")
DEFAULT_CLEARANCE_MM = 0.2
DEFAULT_EDGE_CLEARANCE_MM = 0.5


def _net_names(pcb: list[Node]) -> dict[str, str]:
    """Net number -> name (tscircuit's export references nets by number)."""
    return {str(n[1]): str(n[2]) for n in sexpr.children(pcb, "net") if len(n) > 2}


def _net_of(node: list[Node], names: dict[str, str]) -> str:
    """A node's net name, whether written as ``(net 3)``, ``(net 3 "GND")`` or ``(net "GND")``."""
    net = sexpr.child(node, "net")
    if net is None or len(net) < 2:
        return ""
    last = net[-1]
    return names.get(str(last), "") if isinstance(last, Sym) else str(last)


def _copper_layers(node: list[Node]) -> frozenset[str]:
    layers = sexpr.child(node, "layers")
    names = {str(x) for x in (layers or [])[1:]} | ({_layer(node)} if sexpr.child(node, "layer") is not None else set())
    if "*.Cu" in names or "F&B.Cu" in names:
        return frozenset(COPPER_LAYERS)
    return frozenset(layer for layer in COPPER_LAYERS if layer in names)


DEFAULT_MIN_TRACK_WIDTH_MM = 0.15


def _design_min_track_width(project: KicadProject) -> float:
    """The board's minimum track width (design rules), the floor for necking tracks down."""
    try:
        data = json.loads(project.pcb_path.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_MIN_TRACK_WIDTH_MM
    rules = data.get("board", {}).get("design_settings", {}).get("rules", {})
    return float(rules.get("min_track_width", DEFAULT_MIN_TRACK_WIDTH_MM))


def _design_clearances(project: KicadProject) -> tuple[float, float]:
    """(copper clearance, copper-to-edge clearance) from the project's default net class / rules."""
    pro = project.pcb_path.with_suffix(".kicad_pro")
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_CLEARANCE_MM, DEFAULT_EDGE_CLEARANCE_MM
    classes = data.get("net_settings", {}).get("classes", [])
    clearance = float(classes[0].get("clearance", DEFAULT_CLEARANCE_MM)) if classes else DEFAULT_CLEARANCE_MM
    rules = data.get("board", {}).get("design_settings", {}).get("rules", {})
    return clearance, float(rules.get("min_copper_edge_clearance", DEFAULT_EDGE_CLEARANCE_MM))


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
    def segs_by_net(self) -> dict[str, list[tracks.Seg]]:
        out: dict[str, list[tracks.Seg]] = {}
        for s in self.segs:
            out.setdefault(s.net, []).append(s)
        return out

    def on_pad_or_via(self, p: tracks.Point, net: str, layer: str) -> bool:
        """Whether a point (a polyline node) lies on a pad or via of its net (no width involved)."""
        if any(net == v_net and math.dist(p, c) <= r + tracks.EPS for _, c, r, v_net in self.vias):
            return True
        return any(layer in o.layers and o.distance(p, p) <= tracks.EPS for o in self.pads_by_net.get(net, []))

    def attached(self, p: tracks.Point, s: tracks.Seg) -> bool:
        """Whether track end p of s connects to a pad or via the way KiCad sees it.

        A pad takes any track end inside it; a via a track end inside it (see :func:`tracks.via_takes`).
        """
        if any(math.dist(p, c) <= r - tracks.EPS for c, r in self.vias_by_net.get(s.net, [])):
            return True
        index = self.pad_index_by_net.get(s.net)
        if index is None:
            return False
        pads = self.pads_by_net[s.net]
        # a point outside a pad's bounding box (plus the edge tolerance) is never on the pad
        e = tracks.ON_EDGE_MM
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
                pads.append(
                    tracks.Obstacle(_net_of(pad, names), layers, tuple(_pad_corners(pad, to_board)), polygon=True)
                )
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
    return _Copper(names, segs, vias, pads, rect_pads, edges, zones, keepouts)


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


def _segment_node(template: list[Node], a: tracks.Point, b: tracks.Point, width: float) -> list[Node]:
    node = copy.deepcopy(template)
    for key, values in (("start", a), ("end", b), ("width", (width,))):
        child = sexpr.child(node, key)
        if child is not None:
            child[1:] = [Sym(_mm(v)) for v in values]
    uid = sexpr.child(node, "uuid")
    if uid is not None:  # derived from the content: the same board always gets the same file
        new = str(
            uuid.uuid5(uuid.NAMESPACE_OID, f"{uid[1]}:{_mm(a[0])},{_mm(a[1])}:{_mm(b[0])},{_mm(b[1])}:{_mm(width)}")
        )
        uid[1] = Sym(new) if isinstance(uid[1], Sym) else new
    return node


CLEAN_ROUNDS = 3


def clean_tracks(project: KicadProject) -> list[str]:
    """Straighten tscircuit's tracks and take out every corner sharper than 135 degrees.

    The autorouter leaves overlapping duplicate routes of one net, polylines of many short
    pieces, zig-zags inside the pads it connects, spikes where a track runs out and back on
    itself (an antenna) and acute or 90 degree bends. Net by net (``vibepcb.tracks``):
    duplicates covered by the net's other copper go; then per polyline between pads, vias
    and junctions the part inside its end pads is trimmed, duplicate/spike/collinear points
    go, and sharp corners are cut across or chamfered - every new piece only where it keeps
    the net-class clearance to other nets' copper and the board-edge clearance. Each change
    is kept only if the net's pads stay as connected as before (checked per net, else per
    polyline).
    """
    clearance, edge_clearance = _design_clearances(project)
    min_width = _design_min_track_width(project)
    totals = {"duplicates": 0, "dropped": 0, "fixed": 0, "left": 0, "kept": 0}
    for _ in range(CLEAN_ROUNDS):  # a round's own edits can leave new duplicates/corners
        changed, last = _clean_round(project, clearance, edge_clearance, min_width)
        for key, value in last.items():
            totals[key] = totals[key] + value if key in ("duplicates", "dropped", "fixed") else value
        if not changed:
            break
    changes = []
    if totals["duplicates"]:
        changes.append(
            f"tracks: removed {totals['duplicates']} track segment(s) lying inside other copper of their net"
        )
    if totals["dropped"]:
        changes.append(
            f"tracks: dropped {totals['dropped']} redundant polyline point(s) (zig-zags in pads, spikes, straight runs)"
        )
    if totals["fixed"]:
        changes.append(f"tracks: removed {totals['fixed']} sharp corner(s) (cut across or chamfered to 45 deg)")
    if totals["left"]:
        changes.append(f"tracks: {totals['left']} sharp corner(s) left - no room to fix, check by eye")
    if totals["kept"]:
        changes.append(f"tracks: {totals['kept']} polyline(s) left as routed - cleaning would have cut a connection")
    return changes


def _split_crossings(project: KicadProject) -> int:
    """Cut tracks of one net that cross each other (an X, no shared vertex) at the crossing.

    The router sometimes runs a net across itself; KiCad joins the two there and measures
    the angle. With a vertex at the crossing the loop and junction clean-up can handle it.
    """
    copper = _copper(project.pcb)
    cuts: dict[int, list[tracks.Point]] = {}
    for _net, segs in copper.segs_by_net.items():
        for i, j in itertools.combinations(range(len(segs)), 2):
            s, t = segs[i], segs[j]
            if s.layer != t.layer:
                continue
            q = tracks.crossing(s.a, s.b, t.a, t.b)
            if q is None or min(math.dist(q, e) for e in (s.a, s.b, t.a, t.b)) <= tracks.EPS:
                continue
            cuts.setdefault(id(s), []).append(q)
            cuts.setdefault(id(t), []).append(q)
    by_id = {id(s): s for s in copper.segs}
    for sid, points in cuts.items():
        seg = by_id[sid]
        node = _source(seg)
        chain = [seg.a, *sorted(points, key=lambda p: math.dist(seg.a, p)), seg.b]
        index = project.pcb.index(node)
        project.pcb[index : index + 1] = [
            _segment_node(node, p, q, seg.width) for p, q in itertools.pairwise(chain) if math.dist(p, q) > tracks.EPS
        ]
    return len(cuts) // 2


def _split_tees(project: KicadProject, clearance: float, edge_clearance: float) -> int:
    """Give every T-junction a vertex in its through track and snap the ending track onto it.

    Track ends that already sit on a pad or via stay where they are (that is their connection).
    The snapped track pivots about its other end and must keep its clearance (seen: a 7 mm
    track swung 0.05 mm into a via); where it wouldn't, the T stays as it is (KiCad connects an
    end lying on another track's copper).
    """
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()

    def fits(seg: tracks.Seg, a: tracks.Point, b: tracks.Point) -> bool:
        half = seg.width / 2
        return all(
            o.distance(a, b) >= clearance + half for o in obstacles if o.net != seg.net and seg.layer in o.layers
        ) and all(e.distance(a, b) >= edge_clearance + half for e in copper.edges if seg.layer in e.layers)

    found = [
        (i, end, j, q)
        for i, end, j, q in tracks.tees(copper.segs)
        if not copper.attached(copper.segs[i].b if end else copper.segs[i].a, copper.segs[i])
        and fits(copper.segs[i], q, copper.segs[i].a if end else copper.segs[i].b)
    ]
    cuts: dict[int, list[tracks.Point]] = {}
    for i, end, j, q in found:
        node = sexpr.child(_source(copper.segs[i]), ("start", "end")[end])
        if node is not None:
            node[1:] = [Sym(_mm(q[0])), Sym(_mm(q[1]))]
        cuts.setdefault(j, []).append(q)
    for j, points in cuts.items():
        node = _source(copper.segs[j])
        a, b = ((_num(sexpr.child(node, k), 1), _num(sexpr.child(node, k), 2)) for k in ("start", "end"))
        chain = [a, *sorted(points, key=lambda p: math.dist(a, p)), b]
        index = project.pcb.index(node)
        width = copper.segs[j].width
        project.pcb[index : index + 1] = [
            _segment_node(node, p, q, width) for p, q in itertools.pairwise(chain) if math.dist(p, q) > tracks.EPS
        ]
    return len(found)


def _snap_via_ends(project: KicadProject, clearance: float, edge_clearance: float) -> int:
    """Move track ends lying inside a via of their net onto the via's centre.

    KiCad connects a track to a via through the via centre anyway; with every end on the
    centre, two tracks meeting at a via form one polyline whose bend the corner clean-up
    can work on, instead of two overlapping ends at some angle. The moved track pivots
    about its other end: it must keep its clearance (seen: a 1.7 mm track swung into a pad).
    """
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()

    def fits(s: tracks.Seg, a: tracks.Point, b: tracks.Point) -> bool:
        half = s.width / 2
        return all(
            o.distance(a, b) >= clearance + half for o in obstacles if o.net != s.net and s.layer in o.layers
        ) and all(e.distance(a, b) >= edge_clearance + half for e in copper.edges if s.layer in e.layers)

    moved = 0
    for s in copper.segs:
        for key, p in (("start", s.a), ("end", s.b)):
            other = s.b if key == "start" else s.a
            for _, centre, radius, net in copper.vias:
                if net == s.net and 0 < math.dist(p, centre) <= min(radius, s.width / 2) and fits(s, centre, other):
                    node = sexpr.child(_source(s), key)
                    if node is not None:
                        node[1:] = [Sym(_mm(centre[0])), Sym(_mm(centre[1]))]
                        moved += 1
                    break
    return moved


COLLAPSE_BRANCH_MM = 1.0  # a junction may move to the far end of a branch this short


def _open_junctions(project: KicadProject, clearance: float, edge_clearance: float) -> int:
    """Move junctions (3+ tracks meeting, not on a pad/via) whose branches meet at an acute angle.

    All tracks meeting there move their common end together, so nothing disconnects;
    each moved track must keep its clearances (``tracks.move_junction``).
    """
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()
    moved = 0
    touched: set[int] = set()
    for (net, layer, _), users in tracks.junctions(copper.segs).items():
        j = copper.segs[users[0][0]].b if users[0][1] else copper.segs[users[0][0]].a
        others = [copper.segs[i].a if end else copper.segs[i].b for i, end in users]
        if copper.on_pad_or_via(j, net, layer) or tracks.min_branch_angle(j, others) >= tracks.MIN_JUNCTION_DEG:
            continue
        relevant = [o for o in obstacles if o.net != net and layer in o.layers]
        edges = [e for e in copper.edges if layer in e.layers]
        # each moved piece keeps clearance at its own width (a 0.15 mm branch off a 0.4 mm track)
        width_to = {other: copper.segs[i].width for other, (i, _) in zip(others, users, strict=True)}
        widest = max(width_to.values())

        def fits(
            a: tracks.Point,
            b: tracks.Point,
            relevant: list[tracks.Obstacle] = relevant,
            edges: list[tracks.Obstacle] = edges,
            width_to: dict[tracks.Point, float] = width_to,
            widest: float = widest,
        ) -> bool:
            width = width_to.get(b, widest)
            return all(o.distance(a, b) >= clearance + width / 2 for o in relevant) and all(
                e.distance(a, b) >= edge_clearance + width / 2 for e in edges
            )

        if any(i in touched for i, _ in users):
            continue  # a track an earlier junction of this pass changed: next round
        q = tracks.move_junction(j, others, fits)
        collapsed: int | None = None
        if q is None or tracks.min_branch_angle(q, others) < tracks.MIN_JUNCTION_DEG:
            # a short branch can disappear: the junction moves to its far end, the rest follow
            for (i, _), far in sorted(zip(users, others, strict=True), key=lambda u: math.dist(j, u[1])):
                if math.dist(j, far) > COLLAPSE_BRANCH_MM:
                    break
                rest = [o for o in others if o is not far]
                if tracks.min_branch_angle(far, rest) >= tracks.MIN_JUNCTION_DEG and all(fits(far, o) for o in rest):
                    q, collapsed = far, i
                    break
        if q is None:
            continue
        touched.update(i for i, _ in users)
        for i, end in users:
            if i == collapsed:
                _drop(project.pcb, _source(copper.segs[i]))
                continue
            node = sexpr.child(_source(copper.segs[i]), ("start", "end")[end])
            if node is not None:
                node[1:] = [Sym(_mm(q[0])), Sym(_mm(q[1]))]
        moved += 1
    return moved


def _clean_round(
    project: KicadProject, clearance: float, edge_clearance: float, min_width: float
) -> tuple[bool, dict[str, int]]:
    """One pass of :func:`clean_tracks` over all nets; returns (anything changed, stats)."""
    totals = {"duplicates": 0, "dropped": 0, "fixed": 0, "left": 0, "kept": 0}
    changed = (
        _split_crossings(project)
        + _split_tees(project, clearance, edge_clearance)
        + _snap_via_ends(project, clearance, edge_clearance)
        + _open_junctions(project, clearance, edge_clearance)
        > 0
    )
    stale = False
    copper = _copper(project.pcb)
    for net in sorted({s.net for s in copper.segs}):
        if stale:  # earlier nets' segments were replaced (only segments: see _with_new_segments)
            copper, stale = _with_new_segments(copper, project.pcb), False
        segs = copper.segs_by_net.get(net, [])
        pads = copper.pads_by_net.get(net, [])
        vias = copper.vias_by_net.get(net, [])
        # vias into a zone of the net reach its plane: a pad's track to one is its connection
        planes = [poly for (n, _), polys in copper.zones.items() if n == net for poly in polys]
        anchors = [i for i, (c, _) in enumerate(vias) if any(tracks.point_in_polygon(c, poly) for poly in planes)]
        before = tracks.pad_groups(pads, vias, segs, anchors)

        def connected(
            pieces: list[_Piece],
            pads: list[tracks.Obstacle] = pads,
            vias: list[tuple[tracks.Point, float]] = vias,
            before: list[set[int]] = before,
            anchors: list[int] = anchors,
        ) -> bool:
            rebuilt = [tracks.Seg(a, b, w, layer, "") for _, a, b, w, layer in pieces]
            return tracks.still_connected(before, tracks.pad_groups(pads, vias, rebuilt, anchors))

        drop: set[int] = set()  # duplicates, each dropped only if the net stays connected without it
        for i in tracks.redundant(segs):
            remaining = [(_source(s), s.a, s.b, s.width, s.layer) for j, s in enumerate(segs) if j not in drop | {i}]
            if connected(remaining):
                drop.add(i)
        drop |= _open_loops(copper, segs, drop, connected)
        result = _clean_net(copper, segs, drop, clearance, edge_clearance, min_width)
        if not connected(result.pieces()):
            result.choose(connected)
        if not result.changed():
            continue
        index = min(project.pcb.index(_source(s)) for s in segs)
        for s in segs:
            _drop(project.pcb, _source(s))
        project.pcb[index:index] = [_segment_node(template, a, b, w) for template, a, b, w, _ in result.pieces()]
        changed = stale = True
        for key, value in result.stats().items():
            totals[key] += value
    return changed, totals


def _open_loops(
    copper: _Copper, segs: list[tracks.Seg], drop: set[int], connected: Callable[[list[_Piece]], bool]
) -> set[int]:
    """Tracks to remove where a net meets itself at a sharp angle (junction or T) on a loop.

    Routing each connection separately leaves loops and lattices of one net; their
    junctions have sharp angles that no chamfer fixes. A track at such a junction goes,
    together with whatever is left dangling without it, if the net's pads stay connected.
    Shortest first; each removal is checked against what is left.
    """
    removed: set[int] = set()
    base = [j for j in range(len(segs)) if j not in drop]
    for target in [base[k] for k in tracks.sharp_junctions([segs[j] for j in base])]:
        if target in removed:
            continue
        rest_idx = [j for j in base if j not in removed and j != target]
        rest = [segs[j] for j in rest_idx]
        gone = {target} | {rest_idx[k] for k in tracks.dangling(rest, copper.attached)}
        remaining = [
            (_source(segs[j]), segs[j].a, segs[j].b, segs[j].width, segs[j].layer)
            for j in base
            if j not in removed | gone
        ]
        if connected(remaining):
            removed |= gone
    return removed


type _Piece = tuple[list[Node], tracks.Point, tracks.Point, float, str]  # template node, a, b, width, layer


@dataclass
class _ChainEdit:
    old: list[_Piece]
    new: list[_Piece]
    dropped: int
    fixed: int
    left: int
    use_new: bool = True


@dataclass
class _NetClean:
    fixed_pieces: list[_Piece] = field(default_factory=list)  # duplicates already dropped, untouched loops
    edits: list[_ChainEdit] = field(default_factory=list)
    duplicates: int = 0

    def pieces(self) -> list[_Piece]:
        return self.fixed_pieces + [p for e in self.edits for p in (e.new if e.use_new else e.old)]

    def changed(self) -> bool:
        return bool(self.duplicates) or any(e.use_new and e.new != e.old for e in self.edits)

    def choose(self, connected: Callable[[list[_Piece]], bool]) -> None:
        """Apply the polyline edits one by one, keeping each only while the net stays connected."""
        for edit in self.edits:
            edit.use_new = False
        for edit in self.edits:
            if edit.new != edit.old:
                edit.use_new = True
                if not connected(self.pieces()):
                    edit.use_new = False

    def stats(self) -> dict[str, int]:
        used = [e for e in self.edits if e.use_new]
        return {
            "duplicates": self.duplicates,
            "dropped": sum(e.dropped for e in used),
            "fixed": sum(e.fixed for e in used),
            "left": sum(e.left for e in used) + sum(e.fixed + e.left for e in self.edits if not e.use_new),
            "kept": sum(1 for e in self.edits if not e.use_new and e.new != e.old),
        }


def _clean_net(
    copper: _Copper,
    segs: list[tracks.Seg],
    duplicates: set[int],
    clearance: float,
    edge_clearance: float,
    min_width: float,
) -> _NetClean:
    """The cleaned-up tracks of one net without ``duplicates`` (see :func:`clean_tracks`), not yet written to the board."""
    out = _NetClean()
    out.duplicates = len(duplicates)
    kept = [s for i, s in enumerate(segs) if i not in duplicates]
    obstacles = copper.obstacles()

    def region_of(p: tracks.Point, net: str, layer: str) -> int | None:
        """Index of the same-net pad/via the point lies in (pads first)."""
        for i, o in enumerate(copper.pads):
            if o.net == net and layer in o.layers and o.distance(p, p) <= tracks.EPS:
                return i
        for j, (_, centre, radius, via_net) in enumerate(copper.vias):
            if via_net == net and math.dist(p, centre) <= radius:
                return len(copper.pads) + j
        return None

    def pad_region_of(p: tracks.Point, net: str, layer: str) -> int | None:
        """Like region_of, but rectangular pads only and strictly inside: a track must keep
        running to a via's centre, and round/oval pads are smaller than the box the pad
        test uses."""
        r = region_of(p, net, layer)
        return r if r is not None and r in copper.rect_pads and copper.pads[r].distance(p, p) <= 0 else None

    def touches(region: int, a: tracks.Point, b: tracks.Point, half_width: float) -> bool:
        """Whether a piece a-b passing a pad/via still connects to it the way KiCad sees it: over its centre."""
        centre = copper.pads[region].centre if region < len(copper.pads) else copper.vias[region - len(copper.pads)][1]
        return tracks.on_track_body(centre, a, b, half_width - tracks.EPS)

    in_chain: set[int] = set()
    for chain in tracks.chains(kept, lambda p, net, layer: pad_region_of(p, net, layer) is not None):
        in_chain |= {id(s) for s in chain.segs}
        fits = tracks.clearance_check(chain, obstacles, copper.edges, clearance, edge_clearance, min_width)
        region = functools.partial(region_of, net=chain.net, layer=chain.layer)

        def keeps(
            old: tracks.Point,
            a: tracks.Point,
            b: tracks.Point,
            region: Callable[[tracks.Point], int | None] = region,
            chain: tracks.Chain = chain,
        ) -> bool:
            r = region(old)
            return r is None or touches(r, a, b, chain.width_at(a, b) / 2)

        original = chain.points
        in_pad = functools.partial(pad_region_of, net=chain.net, layer=chain.layer)
        end_vias = [
            (c, r)
            for _, c, r, net in copper.vias
            if net == chain.net and min(math.dist(c, original[0]), math.dist(c, original[-1])) <= tracks.EPS
        ]

        def in_end_via(p: tracks.Point, end_vias: list[tuple[tracks.Point, float]] = end_vias) -> bool:
            return any(math.dist(p, c) <= r for c, r in end_vias)

        points = tracks.simplify(tracks.drop_inside_start(tracks.trim_ends(original, in_pad), in_end_via), keeps)
        old_pieces = set(itertools.pairwise(original))
        if not all(fits(a, b) for a, b in itertools.pairwise(points) if (a, b) not in old_pieces):
            points = tracks.simplify(original, keeps)  # straightening would get too close to something
            if not all(fits(a, b) for a, b in itertools.pairwise(points) if (a, b) not in old_pieces):
                points = original
        sharp = len(tracks.sharp_corners(points))
        dropped = len(original) - len(points)
        points = tracks.round_off_corners(points, fits, keeps)
        remaining = len(tracks.sharp_corners(points))
        old: list[_Piece] = [(_source(s), s.a, s.b, s.width, s.layer) for s in chain.segs]
        if points == original:
            out.edits.append(_ChainEdit(old, old, 0, 0, remaining))
            continue
        template = _source(chain.segs[0])
        new: list[_Piece] = [(template, a, b, chain.width_at(a, b), chain.layer) for a, b in itertools.pairwise(points)]
        out.edits.append(_ChainEdit(old, new, dropped, sharp - remaining, remaining))
    out.fixed_pieces = [(_source(s), s.a, s.b, s.width, s.layer) for s in kept if id(s) not in in_chain]  # closed loops
    return out


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
    spots = [q for q in spots if all(o.distance(q, q) >= radius + clearance for o in others_all)][:VIA_REROUTE_SPOTS]
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
            near = [o for o in others_all if r.layer in o.layers]
            edges = [e for e in copper.edges if r.layer in e.layers]
            index = tracks.BoxIndex([o.box for o in near])
            need = clearance + half + REROUTE_GRID_MARGIN_MM
            box = (min(q[0], far[0]) - 3, min(q[1], far[1]) - 3, max(q[0], far[0]) + 3, max(q[1], far[1]) + 3)

            def free(
                pt: tracks.Point,
                near: list[tracks.Obstacle] = near,
                edges: list[tracks.Obstacle] = edges,
                index: tracks.BoxIndex = index,
                need: float = need,
                half: float = half,
                box: tuple[float, float, float, float] = box,
            ) -> bool:
                x, y = pt
                if not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
                    return False
                hits = index.overlapping(x - need, y - need, x + need, y + need)
                return all(near[i].distance(pt, pt) >= need for i in hits) and all(
                    e.distance(pt, pt) >= edge_clearance + half for e in edges
                )

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


def _snap_one_end(project: KicadProject, clearance: float, edge_clearance: float) -> str | None:
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()
    for s in copper.segs:
        for k, (end, far) in enumerate(((s.a, s.b), (s.b, s.a))):
            if copper.on_pad_or_via(end, s.net, s.layer):
                continue
            same = [t for t in copper.segs_by_net.get(s.net, []) if t is not s and t.layer == s.layer]
            if any(min(math.dist(end, t.a), math.dist(end, t.b)) <= 1e-6 for t in same):
                continue  # already meets a track end exactly
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
            node[1:3] = [Sym(_mm(foot[0])), Sym(_mm(foot[1]))]
            changes.append(f"{s.net} at ({_mm(end[0])}, {_mm(end[1])}): T-junction squared ({angle:.0f} -> 90 deg)")
            touched |= {id(s), id(body)}
            break
    return changes


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


REROUTE_STUB_MM = (0.15, 0.3)  # off-grid first piece of a reroute where no grid heading may leave its start
REROUTE_STUB_ANGLE_STEP_DEG = 5
REROUTE_STUB_SAMPLE_DEG = 0.1
REROUTE_STUB_TRIES = 12


REROUTE_STEP_MM = 0.05  # grid of the local maze router
REROUTE_WINDOWS_MM = (2.0, 4.0, 6.0)  # it may leave the old route's bounding box by this much (tried in turn)
REROUTE_MAX_PATH_MM = 25.0  # longest stretch one reroute replaces
REROUTE_GROW_PIECES = (1, 3, 6, 1000)  # pieces kept around the bad stretch, then the whole run
REROUTE_GRID_MARGIN_MM = 0.01  # grid nodes keep this extra distance: pieces between them stay clear


def reroute_tracks(project: KicadProject) -> list[str]:
    """Rip up and reroute the track runs that still break a rule, with a local 45-degree maze router.

    A run (polyline between pads, vias, junctions and loose ends) is a problem if a piece is
    closer than the clearance to another net's copper or the board edge, a bend is sharper
    than 135 deg, or it meets other tracks at a junction at an acute angle. The new route
    keeps the run's ends and width, turns at most 45 deg at a time, leaves the junctions at
    90 deg or more and keeps every clearance; the old one stays if no such route exists.
    """
    clearance, edge_clearance = _design_clearances(project)
    changes: list[str] = []
    tried: set[tuple[str, str, tuple[int, int], tuple[int, int]]] = set()
    made: set[frozenset[tuple[int, int]]] = set()  # pieces this call routed
    for _ in range(40):  # one run per pass: the copper changes with every reroute
        copper = _copper(project.pcb)
        obstacles = copper.obstacles()
        at_end: dict[tuple[str, str, tuple[int, int]], list[tuple[tracks.Seg, tracks.Point]]] = {}
        for s in copper.segs:
            for e, f in ((s.a, s.b), (s.b, s.a)):
                at_end.setdefault((s.net, s.layer, tracks._key(e)), []).append((s, f))
        runs = tracks.chains(copper.segs, copper.on_pad_or_via)
        done = False
        for run in runs:
            pts = run.points
            key = (run.net, run.layer, tracks._key(pts[0]), tracks._key(pts[-1]))
            length = sum(math.dist(a, b) for a, b in itertools.pairwise(pts))
            if len(pts) < 2 or key in tried:
                continue
            if all(_piece_key(a, b) in made for a, b in itertools.pairwise(pts)):
                continue  # this call's own route: rerouting it again only creeps
            if copper.same_anchor(pts[0], pts[-1], run.net, run.layer):
                continue  # inside one via or pad: no copper of its own to reroute
            width = max(
                {s.width for s in run.segs}, key=lambda w: sum(math.dist(s.a, s.b) for s in run.segs if s.width == w)
            )
            half = width / 2
            widest = REROUTE_WINDOWS_MM[-1]
            x0, y0 = min(p[0] for p in pts) - widest, min(p[1] for p in pts) - widest
            x1, y1 = max(p[0] for p in pts) + widest, max(p[1] for p in pts) + widest
            far = clearance + 1.0
            others = [
                o
                for o in obstacles
                if o.net != run.net
                and run.layer in o.layers
                and o.box[0] < x1 + far
                and o.box[2] > x0 - far
                and o.box[1] < y1 + far
                and o.box[3] > y0 - far
            ]
            edges = [e for e in copper.edges if run.layer in e.layers]
            # the search depends only on the run, its width and the copper (any net) within reach: a run that
            # found no route with exactly this surrounding finds none again - skip it (later post-fill passes
            # retried every hopeless run with dozens of exhaustive grid searches)
            fingerprint = (
                tuple(tracks._key(p) for p in pts),
                round(width * 1000),
                tuple(
                    sorted(
                        (
                            o.net,
                            tuple(sorted(o.layers)),
                            tuple(tracks._key(q) for q in o.points),
                            round(o.radius * 1000),
                        )
                        for o in obstacles
                        if o.box[0] < x1 + far and o.box[2] > x0 - far and o.box[1] < y1 + far and o.box[3] > y0 - far
                    )
                ),
            )
            if project.reroute_failed.get(key) == fingerprint:
                continue
            mine = {id(s) for s in run.segs}

            def fits(
                a: tracks.Point,
                b: tracks.Point,
                others: list[tracks.Obstacle] = others,
                edges: list = edges,
                half: float = half,
            ) -> bool:
                return all(o.distance(a, b) >= clearance + half for o in others) and all(
                    e.distance(a, b) >= edge_clearance + half for e in edges
                )

            def branches(
                p: tracks.Point,
                run: tracks.Chain = run,
                mine: set[int] = mine,
                copper: _Copper = copper,
                at_end: dict[tuple[str, str, tuple[int, int]], list[tuple[tracks.Seg, tracks.Point]]] = at_end,
            ) -> list[tracks.Point]:
                """Far ends of the other tracks meeting at p.

                On a pad or via, tracks merely ending there make no junction angle; a track whose
                side p lies on (it passes through, its copper overlapping the run's end) does.
                """
                if copper.on_pad_or_via(p, run.net, run.layer):
                    return [
                        e
                        for t in copper.segs_by_net.get(run.net, [])
                        if t.layer == run.layer
                        and id(t) not in mine
                        and min(math.dist(p, t.a), math.dist(p, t.b)) > tracks.EPS
                        and tracks.point_seg_distance(p, t.a, t.b) <= t.width / 2
                        for e in (t.a, t.b)
                    ]
                return [f for s, f in at_end.get((run.net, run.layer, tracks._key(p)), []) if id(s) not in mine]

            def junction_ok(
                p: tracks.Point,
                towards: tracks.Point,
                branches: Callable[[tracks.Point], list[tracks.Point]] = branches,
            ) -> bool:
                return all(tracks.interior_angle(towards, p, f) >= tracks.MIN_JUNCTION_DEG - 0.5 for f in branches(p))

            clear_at = clearance + half + REROUTE_GRID_MARGIN_MM
            edge_at = edge_clearance + half + REROUTE_GRID_MARGIN_MM

            def free_within(
                window: float,
                others: list[tracks.Obstacle] = others,
                edges: list[tracks.Obstacle] = edges,
                box: tuple[float, float, float, float] = (x0 + widest, y0 + widest, x1 - widest, y1 - widest),
                clear_at: float = clear_at,
                edge_at: float = edge_at,
            ) -> Callable[[tracks.Point], bool]:
                """``free(pt)`` for routes leaving the old route's box by at most ``window``."""

                index = tracks.BoxIndex([o.box for o in others])
                memo: dict[tuple[int, int], bool] = {}
                reach = clear_at + tracks.EPS

                def free(pt: tracks.Point) -> bool:
                    x, y = pt
                    if not (box[0] - window <= x <= box[2] + window and box[1] - window <= y <= box[3] + window):
                        return False
                    key = (round(x * 1000), round(y * 1000))
                    if key not in memo:  # every grid node is asked once per heading: remember
                        near = index.overlapping(x - reach, y - reach, x + reach, y + reach)
                        memo[key] = all(others[i].distance(pt, pt) >= clear_at for i in near) and all(
                            e.distance(pt, pt) >= edge_at for e in edges
                        )
                    return memo[key]

                return free

            n = len(pts)
            bad = {i for i in range(n - 1) if not fits(pts[i], pts[i + 1])}
            for k in range(1, n - 1):
                if (
                    not copper.on_pad_or_via(pts[k], run.net, run.layer)
                    and tracks.interior_angle(pts[k - 1], pts[k], pts[k + 1]) < tracks.MIN_CORNER_DEG - 1
                ):
                    bad |= {k - 1, k}
            if not junction_ok(pts[0], pts[1]):
                bad.add(0)
            if not junction_ok(pts[-1], pts[-2]):
                bad.add(n - 2)
            if not bad:
                continue
            tried.add(key)
            anchored = _anchored_pieces(copper, run, mine)
            new_points = None
            # reroute the bad stretch only - its legal surroundings stay; widen it while nothing fits
            for grow in REROUTE_GROW_PIECES:
                lo, hi = max(0, min(bad) - grow), min(n - 2, max(bad) + grow)
                stretch = sum(math.dist(pts[i], pts[i + 1]) for i in range(lo, hi + 1))
                if stretch > REROUTE_MAX_PATH_MM:  # a long run: only its bad part is rerouted
                    break
                # something joins the run on an anchored piece: it must stay - start / end the
                # stretch next to it, give up if it lies inside
                inside = [i for i in anchored if lo <= i <= hi]
                lo = max([i + 1 for i in inside if i <= min(bad)], default=lo)
                hi = min([i - 1 for i in inside if i >= max(bad)], default=hi)
                if lo > hi or any(lo <= i <= hi for i in anchored):
                    log.debug("reroute %s %s: stretch carries a T-junction, via or pad", run.net, run.layer)
                    break
                start, goal = pts[lo], pts[hi + 1]
                before = pts[lo - 1] if lo > 0 else None
                after = pts[hi + 2] if hi + 2 < n else None

                def leaves_ok(
                    p: tracks.Point,
                    towards: tracks.Point,
                    neighbour: tracks.Point | None,
                    junction_ok: Callable[[tracks.Point, tracks.Point], bool] = junction_ok,
                ) -> bool:
                    """At a kept neighbour: a bend of 135 deg or more; at the run's end: the junction rule."""
                    if neighbour is None:
                        return junction_ok(p, towards)
                    return tracks.interior_angle(neighbour, p, towards) >= tracks.MIN_CORNER_DEG - 0.5

                route = None
                for window in REROUTE_WINDOWS_MM:
                    free_here = free_within(window)
                    route = _route_with_stub(start, goal, before, after, free_here, fits, leaves_ok)
                    if route is None:  # the far end may be the one no grid heading can leave
                        back = _route_with_stub(goal, start, after, before, free_here, fits, leaves_ok)
                        route = None if back is None else back[::-1]
                    if route is not None:
                        break
                if route is None or len(route) < 2:
                    log.debug("reroute %s %s %s-%s: no route", run.net, run.layer, start, goal)
                    continue
                candidate = [*pts[:lo], *route, *pts[hi + 2 :]]
                bends = [
                    tracks.interior_angle(candidate[i - 1], candidate[i], candidate[i + 1])
                    for i in range(1, len(candidate) - 1)
                    if not copper.on_pad_or_via(candidate[i], run.net, run.layer)
                ]
                new_length = sum(math.dist(p, q) for p, q in itertools.pairwise(candidate))
                if (
                    all(fits(p, q) for p, q in itertools.pairwise(candidate))
                    and all(angle >= tracks.MIN_CORNER_DEG - 0.5 for angle in bends)
                    and junction_ok(candidate[0], candidate[1])
                    and junction_ok(candidate[-1], candidate[-2])
                    and new_length <= 3 * length + 5
                ):
                    new_points = candidate
                    break
                log.debug("reroute %s %s %s-%s: route rejected", run.net, run.layer, start, goal)
            if new_points is None:
                project.reroute_failed[key] = fingerprint
                continue
            template = _source(run.segs[0])
            index = min(project.pcb.index(_source(s)) for s in run.segs)
            for s in run.segs:
                _drop(project.pcb, _source(s))
            for k, (p, q) in enumerate(itertools.pairwise(new_points)):
                project.pcb.insert(index + k, _segment_node(template, p, q, width))
                made.add(_piece_key(p, q))
            changes.append(
                f"{run.net} on {run.layer}: run ({_mm(pts[0][0])}, {_mm(pts[0][1])}) - "
                f"({_mm(pts[-1][0])}, {_mm(pts[-1][1])}) rerouted"
            )
            done = True
            break
        if not done:
            break
    return changes


PUSH_STEPS_MM = tuple(round(0.02 * k, 2) for k in range(1, 16))  # up to 0.3 mm


def push_tracks_clear(project: KicadProject) -> list[str]:
    """Slide track pieces sideways that sit closer than the clearance to another net's copper.

    The router occasionally squeezes a track past a via or between two others a few
    hundredths of a mm too close. A piece whose ends are free corners (two tracks meet
    there, no pad or via) or lie on a pad of its net moves perpendicular to itself, its
    neighbours following, in small steps up to 0.3 mm, either side; an end on a pad must stay
    inside it (KiCad still connects it), every changed piece must keep the clearance and no
    corner may get sharper than 135 deg (or than it was).
    """
    clearance, edge_clearance = _design_clearances(project)
    changes = []
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
            for step in PUSH_STEPS_MM:
                done = False
                for sign in (1, -1):
                    off = (normal[0] * step * sign, normal[1] * step * sign)
                    na = (s.a[0] + off[0], s.a[1] + off[1])
                    nb = (s.b[0] + off[0], s.b[1] + off[1])
                    if not ok(na, nb, s.width):
                        continue
                    fits_ends, new_corners, moves = True, [], [(s, s.a, na), (s, s.b, nb)]
                    for end, old_p, new_p, other in ((ends[0], s.a, na, nb), (ends[1], s.b, nb, na)):
                        if isinstance(end, tracks.Obstacle):  # stays inside its pad, clear of the edge
                            m = PAD_EDGE_MARGIN_MM
                            if any(
                                end.distance(q, q) > 0
                                for q in (
                                    (new_p[0] + m, new_p[1]),
                                    (new_p[0] - m, new_p[1]),
                                    (new_p[0], new_p[1] + m),
                                    (new_p[0], new_p[1] - m),
                                )
                            ):
                                fits_ends = False
                            continue
                        t, far = end
                        if not ok(far, new_p, t.width):
                            fits_ends = False
                        new_corners.append(tracks.interior_angle(far, new_p, other))
                        moves.append((t, old_p, new_p))
                    # no corner sharper than 135 deg, or than it already was
                    floor = min(tracks.MIN_CORNER_DEG, *corners)
                    if not fits_ends or (new_corners and min(new_corners) < floor - 1e-6):
                        continue
                    for seg, old, new_point in moves:
                        key = "start" if math.dist(seg.a, old) <= tracks.EPS else "end"
                        node = sexpr.child(_source(seg), key)
                        assert node is not None
                        node[1:3] = [Sym(_mm(new_point[0])), Sym(_mm(new_point[1]))]
                    changes.append(f"{s.net} track at ({_mm(s.a[0])}, {_mm(s.a[1])}) pushed {step} mm clear")
                    done = pushed = True
                    break
                if done:
                    break
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
    net) its zone. Removal repeats until nothing is left dangling.
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
    for s in removed_segs:
        _drop(project.pcb, _source(s))
    alive = [s for s in copper.segs if all(s is not r for r in removed_segs)]

    removed_vias = 0
    for node, centre, radius, net in copper.vias:
        if id(node) in island_vias:
            project.pcb.remove(node)
            removed_vias += 1
            continue
        layers = {
            layer
            for layer in COPPER_LAYERS
            if copper.in_zone(centre, net, layer)
            or any(s.net == net and s.layer == layer and tracks.via_takes(centre, radius, s) for s in alive)
            or any(o.net == net and layer in o.layers and o.distance(centre, centre) <= radius for o in copper.pads)
        }
        if len(layers) < 2:
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


SHARP_CORNER_RULE = "no acute track angles"
STRICT_SEVERITIES = {"track_dangling": "error", "via_dangling": "error", "track_angle": "error"}


BOARD_CONFIG_RULE = "board config"  # prefix of the .kicad_dru rules apply_board_config writes


def apply_board_config(project: KicadProject) -> list[str]:
    """Net classes, length groups and diff-pair rules from ``boards/<board>/vibepcb.toml``.

    tscircuit has no net classes or KiCad length rules: classes (widths, clearance, diff-pair
    width / gap) and their net-name patterns go into the .kicad_pro; each length group becomes a
    ``skew`` rule and the diff-pair settings a ``diff_pair_uncoupled`` rule in the .kicad_dru, so
    KiCad's DRC (and ``check``) fails on them. Rewritten from the config on every run. A class with
    ``impedance`` / ``impedance_diff`` gets its track_width / diff_pair_width / diff_pair_gap solved
    from the ``[stackup]`` (:mod:`vibepcb.stackup`, closed-form: confirm with the fab's calculator),
    replacing any width given in the class; a change line names the layer and stack-up.
    """
    cfg = project.config
    if not cfg:
        return []
    changes = []
    pro = project.pcb_path.with_suffix(".kicad_pro")
    netclasses = rules.netclasses(cfg)  # the board's [netclass] plus those of its rule profiles
    if pro.exists() and netclasses:
        data = json.loads(pro.read_text(encoding="utf-8"))
        settings = data.setdefault("net_settings", {})
        classes = [c for c in settings.get("classes", []) if c.get("name") == "Default"]
        default = classes[0] if classes else {}
        patterns, notes = [], []
        stack = stackup.from_config(cfg["stackup"]) if "stackup" in cfg else None
        for name, cls in netclasses.items():
            entry = {**default, "name": name}
            entry.update({k: float(v) for k, v in cls.items() if k in NETCLASS_KEYS})
            if stack is not None and (solved := stackup.netclass_widths(stack, cls))[0]:
                entry.update(solved[0])
                values = ", ".join(f"{k} {v:g}" for k, v in solved[0].items())
                notes.append(f"{pro.name}: net class {name}: {values} mm ({solved[1]})")
            classes.append(entry)
            patterns += [{"netclass": name, "pattern": str(p)} for p in cls["nets"]]
        if settings.get("classes") != classes or settings.get("netclass_patterns") != patterns:
            settings["classes"] = classes
            settings["netclass_patterns"] = patterns
            pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            changes.append(f"{pro.name}: net classes {', '.join(netclasses)}")
            changes += notes
    dru_forms = []
    for name, group in cfg.get("length_group", {}).items():
        condition = " || ".join(f"A.NetName == '{p}'" for p in group["nets"])
        dru_forms.append(
            f'(rule "{BOARD_CONFIG_RULE}: length group {name}"\n  (condition "{condition}")\n'
            f"  (constraint skew (max {_mm(float(group['max_skew']))}mm)))\n"
        )
    if (uncoupled := cfg.get("diff_pair", {}).get("max_uncoupled")) is not None:
        dru_forms.append(
            f'(rule "{BOARD_CONFIG_RULE}: diff pairs"\n  (condition "A.inDiffPair(\'*\')")\n'
            f"  (constraint diff_pair_uncoupled (max {_mm(float(uncoupled))}mm)))\n"
        )
    if dru_forms:
        dru = project.pcb_path.with_suffix(".kicad_dru")
        text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
        kept = [r for r in _dru_rules(text) if f'(rule "{BOARD_CONFIG_RULE}' not in r]
        new_text = "".join(kept) + "".join(dru_forms)
        if new_text != text:
            dru.write_text(new_text, encoding="utf-8")
            changes.append(f"{dru.name}: {len(dru_forms)} rule(s) from the board config")
    return changes


def apply_rule_profiles(project: KicadProject) -> list[str]:
    """Design / manufacturing rule profiles (``[rules]`` in ``vibepcb.toml``, see ``vibepcb.rules``).

    The merged minimums go into the .kicad_pro design settings (KiCad's DRC and the router read
    them there), the Default net class takes the profile values (else its own, raised to the
    minimums), and pad-hole rules KiCad has no setting for become .kicad_dru rules. Runs before the
    routing fixes so ``Board.from_project`` sees the profile.
    """
    rs = rules.merge(project.config)
    if rs is None:
        return []
    changes = []
    pro = project.pcb_path.with_suffix(".kicad_pro")
    if pro.exists():
        data = json.loads(pro.read_text(encoding="utf-8"))
        design = data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
        new_rules = {k: v for k, v in rules.pro_rules(rs).items() if design.get(k) != v}
        design.update(new_rules)
        classes = data.setdefault("net_settings", {}).setdefault("classes", [])
        index = next((i for i, c in enumerate(classes) if c.get("name") == "Default"), None)
        if index is None:
            classes.insert(0, {"name": "Default"})
            index = 0
        default = rules.default_class(rs, classes[index])
        new_default = {k: v for k, v in default.items() if classes[index].get(k) != v}
        classes[index] = default
        if new_rules or new_default:
            pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            changes += [f"{pro.name}: {k} = {v:g} mm" for k, v in {**new_rules, **new_default}.items()]
    dru = project.pcb_path.with_suffix(".kicad_dru")
    text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
    added = rules.dru_rules(rs)
    forms = _dru_rules(text)
    kept = [r for r in forms if f'(rule "{rules.DRU_RULE_PREFIX}' not in r]
    new_text = "".join(kept) + "".join(added)
    if new_text != "".join(forms):
        dru.write_text(new_text, encoding="utf-8")
        changes.append(f"{dru.name}: {len(added)} rule(s) from the rule profiles")
    return changes


def _dru_rules(text: str) -> list[str]:
    """A .kicad_dru split into its top-level forms (version line, rules), each with its newline."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                out.append(text[start : i + 1] + "\n")
    return out


def strict_track_rules(project: KicadProject) -> list[str]:
    """Make KiCad DRC fail on sharp track corners and on antennas (dangling tracks/vias).

    KiCad 10's ``track_angle`` (interior angle, 180 = straight) applies one minimum to
    plain bends and to the branches of junctions alike; three branches can't all be 135
    degrees apart, so KiCad gets "no acute angles" and the 135 degree bend minimum is
    checked by :func:`sharp_corners` (``check``). KiCad only warns about dangling tracks
    and vias by default.
    """
    changes = []
    dru = project.pcb_path.with_suffix(".kicad_dru")
    text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
    if SHARP_CORNER_RULE not in text:
        minimum = tracks.MIN_JUNCTION_DEG - 1
        text += f'(rule "{SHARP_CORNER_RULE}"\n  (constraint track_angle (min {minimum:g})))\n'
        dru.write_text(text, encoding="utf-8")
        changes.append(f"{dru.name}: track angles >= {minimum:g} deg")
    pro = project.pcb_path.with_suffix(".kicad_pro")
    if pro.exists():
        data = json.loads(pro.read_text(encoding="utf-8"))
        severities = data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rule_severities", {})
        missing = {k: v for k, v in STRICT_SEVERITIES.items() if severities.get(k) != v}
        if missing:
            severities.update(missing)
            pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            changes.append(f"{pro.name}: {', '.join(sorted(missing))} -> error")
    return changes


POUR_SAMPLE_MM = 0.05


KEEP_RADIUS_MM = 1.5
ONLY_RADIUS_MM = 0.1  # targeted pruning: tracks at a reported spot only
PIECE_MATCH_MM = 0.25  # the refill after pruning moves a fill piece's corners a little


SPOKE_REACH_MM = 0.05  # fill outline within this of a pad: a spoke reaches it


def prune_pour_tracks(
    project: KicadProject, keep_near: list[tracks.Point] | None = None, only_near: list[tracks.Point] | None = None
) -> list[str]:
    """Remove tracks of a poured net that the (KiCad-filled) pour already provides.

    tscircuit routes every GND connection as a track before it pours; on a board with GND
    pours those tracks are redundant, take routing room and short-circuit the thermal
    reliefs. A track goes when every point along it lies in its net's fill on that layer,
    in one of its net's pads or within the thermal gap around such a pad (so the pad then
    joins the pour through its spokes), all in one piece of the fill. Tracks through gaps the
    pour cannot reach, or bridging two separate fill pieces, stay.
    With ``only_near``, only tracks at those points are candidates (``check``'s fallback: the
    poured-net tracks at acute angles). Tracks within ``KEEP_RADIUS_MM`` of a ``keep_near`` point stay (``check`` passes the
    spots KiCad reported unconnected after an earlier attempt, e.g. where a pour island only
    held on through the track). Needs zone fills, i.e. runs after KiCad's refill (``check``).
    """
    copper = _copper(project.pcb, filled_zones=True)
    gap = THERMAL_GAP_MM + 0.05

    contacts: dict[tuple[int, str], list[tuple[int, tracks.Point]]] = {}  # (pad, layer) -> (piece, outline point)

    def pieces_at_pad(k: int, s: tracks.Seg) -> set[int]:
        """Fill pieces reaching pad k other than along track s: where a track enters a pad the fill
        hugs it up to the pad - that contact goes with the track, it is no thermal spoke."""
        if (k, s.layer) not in contacts:
            pad = copper.pads[k]
            x0, y0, x1, y1 = pad.box
            r = SPOKE_REACH_MM
            contacts[k, s.layer] = [
                (i, v)
                for i, poly in enumerate(copper.zones.get((pad.net, s.layer), []))
                for v in poly
                if x0 - r <= v[0] <= x1 + r and y0 - r <= v[1] <= y1 + r and pad.distance(v, v) <= r
            ]
        clear = s.width / 2 + SPOKE_REACH_MM
        return {i for i, v in contacts[k, s.layer] if tracks.point_seg_distance(v, s.a, s.b) > clear}

    def piece(p: tracks.Point, s: tracks.Seg) -> int | tuple[int, ...] | None:
        """Fill piece (index) of the track's net and layer that p lies in; at a pad the pad (as a
        1-tuple) - it must reach the same piece through its spokes; None: neither."""
        for i, poly in enumerate(copper.zones.get((s.net, s.layer), [])):
            if tracks.point_in_polygon(p, poly):
                return i
        for k, o in enumerate(copper.pads):
            if o.net == s.net and s.layer in o.layers and o.distance(p, p) <= gap:
                return (k,)
        return None

    # KiCad reports a pour fragment that lost its link by a corner of that fill piece: keep
    # every track touching such a piece
    kept_pieces: dict[tuple[str, str], set[int]] = {}
    for key, polys in copper.zones.items():
        main = max(range(len(polys)), key=lambda i: len(polys[i]), default=-1)  # reported by its corner: no clue
        for i, poly in enumerate(polys):
            if i != main and any(math.dist(k, v) <= PIECE_MATCH_MM for k in keep_near or [] for v in poly):
                kept_pieces.setdefault(key, set()).add(i)

    def fill_pieces_touched(s: tracks.Seg) -> set[int]:
        polys = copper.zones.get((s.net, s.layer), [])
        ends = (s.a, s.b, ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2))
        return {i for i, poly in enumerate(polys) if any(tracks.point_in_polygon(p, poly) for p in ends)}

    pruned = 0
    for s in copper.segs:
        if not copper.zones.get((s.net, s.layer)):
            continue
        if any(tracks.point_seg_distance(k, s.a, s.b) <= KEEP_RADIUS_MM for k in keep_near or []):
            continue
        if only_near is not None and not any(
            tracks.point_seg_distance(k, s.a, s.b) <= ONLY_RADIUS_MM for k in only_near
        ):
            continue
        if any(i in kept_pieces.get((s.net, s.layer), set()) for i in fill_pieces_touched(s)):
            continue
        n = max(2, math.ceil(math.dist(s.a, s.b) / POUR_SAMPLE_MM) + 1)
        samples = [
            (s.a[0] + (s.b[0] - s.a[0]) * k / (n - 1), s.a[1] + (s.b[1] - s.a[1]) * k / (n - 1)) for k in range(n)
        ]
        hits = [piece(p, s) for p in samples]
        fills = {h for h in hits if isinstance(h, int)}
        pads = {h[0] for h in hits if isinstance(h, tuple)}
        # one fill piece must hold the whole track and reach every pad it touches; a track over two
        # pieces, or to a pad whose spokes reach another piece, may be all that joins them: it stays
        if None in hits or len(fills) > 1:
            continue
        if not fills and pads:  # the track lies in pad gaps only: the pads must share a piece
            fills = set.intersection(*(pieces_at_pad(k, s) for k in pads))
        if fills and all(fills & pieces_at_pad(k, s) for k in pads):
            _drop(project.pcb, _source(s))
            pruned += 1
    changes = [f"pours: removed {pruned} track segment(s) the pour already connects"] if pruned else []
    return changes + remove_antennas(project, filled_zones=True)


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
            if angle < tracks.MIN_CORNER_DEG - 1 and not copper.on_pad_or_via(p, net, layer):
                out.append((net, p, angle))
    return sorted(out)


def apply_post_fill(
    project: KicadProject, keep_near: list[tracks.Point] | None = None, only_near: list[tracks.Point] | None = None
) -> list[str]:
    """Fixes that need KiCad's zone fill (run by ``check`` after the refill); saves when anything changed."""
    changes = prune_pour_tracks(project, keep_near, only_near)
    if changes:  # the pruned GND tracks leave room for corners that had none before
        for _ in range(POST_FILL_PASSES):  # each step can leave work for an earlier one: repeat until quiet
            second = (
                snap_near_ends(project)
                + clean_tracks(project)
                + separate_ends_on_pads(project)
                + square_tees(project)
                + push_tracks_clear(project)
                + relocate_vias(project)
                + reroute_tracks(project)
                + remove_antennas(project, filled_zones=True)
            )
            changes += [f"second pass: {c}" for c in second]
            if not second:
                break
        sexpr.dump(project.pcb, project.pcb_path)
    return changes


POST_FILL_PASSES = 3


type Fix = Callable[[KicadProject], list[str]]


THERMAL_VIA_DEFAULTS = {"pitch": 1.0, "drill": 0.3, "size": 0.5, "margin": 0.2}  # mm; JLCPCB 4-layer capable


def thermal_pad_vias(project: KicadProject) -> list[str]:
    """A grid of vias in a part's exposed pad (``[thermal_vias.<ref>]``): heat into the inner / bottom planes.

    tscircuit has no via-in-pad; without them a power IC's exposed pad only spreads heat on its own layer
    (LMR33630 at 3 A: Tj about 110 C instead of 75 C). The grid fills the pad's copper box minus ``margin``
    (via edge to pad edge) at ``pitch``, on the pad's net, through all layers. Small drills (0.3 mm) wick
    little solder; the fab tents / plugs them on request. The pad connects solid to the pours
    (``zone_connect 2``, overriding ``zone_thermal_reliefs``' spokes). Runs before the track clean-up,
    so later fixes treat the vias as obstacles and the connectivity roll-back keeps them.
    """
    specs = project.config.get("thermal_vias", {})
    if not specs:
        return []
    names = _net_names(project.pcb)
    existing = {
        (_mm(_num(sexpr.child(v, "at"), 1)), _mm(_num(sexpr.child(v, "at"), 2)))
        for v in sexpr.children(project.pcb, "via")
    }
    changes = []
    for ref, spec in sorted(specs.items()):
        cfg = {**THERMAL_VIA_DEFAULTS, **{k: float(v) for k, v in spec.items() if k != "pad"}}
        fp = next(
            (f for f in sexpr.children(project.pcb, "footprint") if _footprint_ref(f) == ref),
            None,
        )
        if fp is None:
            changes.append(f"{ref}: no such footprint, no thermal vias")
            continue
        pad_name = str(spec.get("pad", ""))
        pads = [pd for pd in sexpr.children(fp, "pad") if str(pd[2]) == "smd"]
        pad = (
            next((pd for pd in pads if str(pd[1]) == pad_name), None)
            if pad_name
            else max(pads, key=_pad_area, default=None)  # the exposed pad is the largest
        )
        net = None if pad is None else _net_of(pad, names)
        net_node = None if net is None else _net_node(project.pcb, net)
        if pad is None or net is None or net_node is None:
            changes.append(f"{ref}: pad {pad_name or '(largest)'} not found or on no net, no thermal vias")
            continue
        xmin, ymin, xmax, ymax = _bbox(_pad_corners(pad, _fp_to_board(fp)))
        inset = cfg["margin"] + cfg["size"] / 2
        nx = max(1, int((xmax - xmin - 2 * inset) / cfg["pitch"] + 1e-6) + 1)
        ny = max(1, int((ymax - ymin - 2 * inset) / cfg["pitch"] + 1e-6) + 1)
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        added = 0
        for i in range(nx):
            for j in range(ny):
                x = _mm(cx + (i - (nx - 1) / 2) * cfg["pitch"])
                y = _mm(cy + (j - (ny - 1) / 2) * cfg["pitch"])
                if (x, y) in existing:
                    continue
                uid = uuid.uuid5(uuid.NAMESPACE_OID, f"thermal-via:{ref}:{x},{y}")
                via = sexpr.loads(
                    f'(via (at {x} {y}) (size {_mm(cfg["size"])}) (drill {_mm(cfg["drill"])}) (layers "F.Cu" "B.Cu") '
                    f'(uuid "{uid}"))'
                )
                via.insert(len(via) - 1, copy.deepcopy(net_node))
                project.pcb.append(via)
                existing.add((x, y))
                added += 1
        if added:
            changes.append(f"{ref}: {added} thermal via(s) ({nx} x {ny}, {net}) in pad {pad[1]}")
        if _solid_zone_connect(pad):
            changes.append(f"{ref}: pad {pad[1]} connects solid to the pours (no thermal spokes on a heat path)")
    return changes


def _solid_zone_connect(pad: list[Node]) -> bool:
    """``(zone_connect 2)`` on the pad (KiCad: solid, overriding the zones' thermal reliefs); whether it changed."""
    node = sexpr.child(pad, "zone_connect")
    if node is not None:
        if str(node[1]) == "2":
            return False
        node[1] = Sym("2")
        return True
    uid = next((i for i, item in enumerate(pad) if sexpr.head(item) == "uuid"), len(pad))
    pad.insert(uid, [Sym("zone_connect"), Sym("2")])
    return True


def _footprint_ref(fp: list[Node]) -> str:
    text = next((t for t in sexpr.children(fp, "fp_text") if str(t[1]) == "reference"), None)
    return str(text[2]) if text is not None else (sexpr.property_value(fp, "Reference") or "")


def _pad_area(pad: list[Node]) -> float:
    xmin, ymin, xmax, ymax = _pad_box(pad)
    return (xmax - xmin) * (ymax - ymin)


def plane_pad_vias(project: KicadProject) -> list[str]:
    """Connect the SMD pads of plane nets to their plane through their own via, not by tracks.

    For the nets listed in the board config (``[planes] nets = [...]``, see ``vibepcb.config``):
    tscircuit routes a plane net's pads to each other with tracks, which knots up in fine-pitch
    pin fields (0.5 mm QFP GND / VCC pins). The router's tracks and vias of the net go; each SMD
    pad gets a minimum-width track to a via beside it (<= 3 mm, clearance on every layer, inside
    the net's zone on another layer) - common practice; a pad without room joins a neighbouring
    pad of the net that has one (adjacent GND pins). Per net all or nothing: if a pad finds neither,
    that net keeps its routing.
    """
    nets = list(project.config.get("planes", {}).get("nets", []))
    if not nets:
        return []
    changes = []
    escapes = _escape_boxes(project)  # escaped ball fields (vibepcb.fanout) keep their own escapes
    for net in nets:
        copper = _copper(project.pcb)
        if not any(n == net for n, _ in copper.zones):
            changes.append(f"{net}: no zone on the board, left as routed")
            continue
        names = _net_names(project.pcb)
        routed = [
            n
            for n in project.pcb
            if sexpr.head(n) in ("segment", "via")
            and isinstance(n, list)
            and _net_of(n, names) == net
            and not _in_escape(n, escapes)
        ]
        for n in routed:
            project.pcb.remove(n)
        copper = _copper(project.pcb)
        others = [o for o in copper.obstacles() if o.net != net]
        added: list[list[Node]] = []
        smd = [
            o
            for o in copper.pads_by_net.get(net, [])
            if len(o.layers & frozenset(COPPER_LAYERS)) == 1 and not any(_box_has(b, o.centre) for b in escapes)
        ]
        with_via: list[tracks.Obstacle] = []
        no_room = []
        for pad in smd:
            nodes = _pad_via(project, copper, pad, net, others)
            if nodes is None:
                no_room.append(pad)
                continue
            added += nodes
            with_via.append(pad)
            via_at = sexpr.child(nodes[1], "at")
            centre = (_num(via_at, 1), _num(via_at, 2))
            r = _num(sexpr.child(nodes[1], "size"), 1) / 2
            others.append(tracks.Obstacle("<new via>", frozenset(COPPER_LAYERS), (centre,), r))  # spacing to the next
        # a pad without room joins a neighbouring pad of the net that has its via (adjacent GND pins)
        stuck = None
        for pad in no_room:
            link = _pad_link(project, pad, with_via, others, net)
            if link is None:
                stuck = pad
                break
            added.append(link)
        if stuck is None:
            project.pcb += added
            changes.append(
                f"{net}: {len(routed)} routed tracks/vias replaced by {len(with_via)} pad vias to the plane"
                + (f" ({len(no_room)} pad(s) joined to a neighbour)" if no_room else "")
            )
            continue
        project.pcb += routed  # a pad without room: the net keeps its routing
        changes.append(
            f"{net}: pad at ({_mm(stuck.centre[0])}, {_mm(stuck.centre[1])}) has no room for a via, left as routed"
        )
    return changes


PAD_LINK_MAX_MM = 1.6  # a pad without via room may join a same-net pad this close (centre to centre)


def _pad_link(
    project: KicadProject, pad: tracks.Obstacle, targets: list[tracks.Obstacle], others: list[tracks.Obstacle], net: str
) -> list[Node] | None:
    """A straight minimum-width track from ``pad`` to the nearest of ``targets`` on its layer, if it keeps
    the clearance to ``others`` (other nets' copper); None if there is no such neighbour."""
    clearance, _ = _design_clearances(project)
    width = _design_min_track_width(project)
    layer = next((lay for lay in COPPER_LAYERS if lay in pad.layers), None)
    net_node = _net_node(project.pcb, net)
    if layer is None or net_node is None:
        return None
    on_layer = [o for o in others if layer in o.layers]
    for target in sorted(targets, key=lambda t: math.dist(t.centre, pad.centre)):
        if math.dist(target.centre, pad.centre) > PAD_LINK_MAX_MM:
            break
        if layer not in target.layers:
            continue
        a, b = pad.centre, target.centre
        if all(o.distance(a, b) >= clearance + width / 2 for o in on_layer):
            key = f"{_mm(a[0])},{_mm(a[1])}:{_mm(b[0])},{_mm(b[1])}"
            seg = sexpr.loads(
                f"(segment (start {_mm(a[0])} {_mm(a[1])}) (end {_mm(b[0])} {_mm(b[1])}) (width {_mm(width)}) "
                f'(layer "{layer}") (uuid "{uuid.uuid5(uuid.NAMESPACE_OID, "pad-link:" + net + ":" + key)}"))'
            )
            seg.insert(len(seg) - 1, copy.deepcopy(net_node))
            return seg
    return None


KEEPOUT_ZONE_PREFIX = "vibepcb:keepout:"


def _part_area(project: KicadProject, ref: str) -> Box | None:
    """Board-frame bounding box of a part: its pads and its courtyard (F/B.CrtYd)."""
    fp = _footprints_by_ref(project.pcb).get(ref)
    if fp is None:
        return None
    to_board = _fp_to_board(fp)
    points = []
    for pad in sexpr.children(fp, "pad"):
        points += _pad_corners(pad, to_board)
    for g in fp:
        if isinstance(g, list) and _layer(g) in ("F.CrtYd", "B.CrtYd"):
            points += [to_board(x, y) for x, y in _graphic_points(g)]
    return _bbox(points) if points else None


def apply_keepouts(project: KicadProject) -> list[str]:
    """Keepout areas from the board config (``[keepout.<name>]``, see ``vibepcb.config``).

    A global pour (GND) under an antenna, a crystal or a switching node is wrong; here each keepout is a
    KiCad rule area over a part's pads / courtyard (or a box) that forbids a pour, tracks and / or vias
    on the chosen layers. KiCad's zone fill and DRC enforce it, and the router stays out of the track and
    via areas. Rewritten from the config on every run.
    """
    cfg = project.config.get("keepout", {})
    for z in [z for z in sexpr.children(project.pcb, "zone") if _is_keepout_zone(z)]:
        project.pcb.remove(z)
    layer_names = {
        str(n[1]) for n in (sexpr.child(project.pcb, "layers") or [])[1:] if isinstance(n, list) and len(n) > 1
    }
    board_layers = [lay for lay in COPPER_LAYERS if lay in layer_names]
    changes = []
    for name, entry in cfg.items():
        if "area" in entry:
            x0, y0, x1, y1 = (float(v) for v in entry["area"])
        else:
            part = _part_area(project, entry["ref"])
            if part is None:
                changes.append(f"{name}: part {entry['ref']} not on the board, keepout skipped")
                continue
            x0, y0, x1, y1 = part
        grow = float(entry.get("margin", 0.0))
        x0, y0, x1, y1 = x0 - grow, y0 - grow, x1 + grow, y1 + grow
        layers = [lay for lay in entry.get("layers", board_layers) if lay in board_layers]
        block = set(entry.get("block", ["pour"]))
        rules = (
            "".join(
                f"({rule} {'not_allowed' if key in block else 'allowed'})"
                for key, rule in (("tracks", "tracks"), ("vias", "vias"), ("pour", "copperpour"))
            )
            + "(pads allowed)(footprints allowed)"
        )
        uid = uuid.uuid5(uuid.NAMESPACE_OID, f"keepout:{name}")
        layer_list = " ".join(f'"{lay}"' for lay in layers)
        project.pcb.append(sexpr.loads(
            f'(zone (net 0) (net_name "") (layers {layer_list}) (uuid "{uid}") (name "{KEEPOUT_ZONE_PREFIX}{name}") '
            f"(hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25) (keepout {rules}) "
            f'(placement (enabled no) (sheetname "")) (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) '
            f"(polygon (pts (xy {_mm(x0)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y1)}) (xy {_mm(x0)} {_mm(y1)}))))"
        ))  # fmt: skip
        changes.append(
            f"{name}: no {', '.join(sorted(block))} on {', '.join(layers)} in ({_mm(x0)}, {_mm(y0)})-({_mm(x1)}, {_mm(y1)})"
        )
    return changes


POUR_ZONE_PREFIX = "vibepcb:pour:"


def apply_pours(project: KicadProject) -> list[str]:
    """Copper pours from the board config (``[pour.<name>]``, see ``vibepcb.config``).

    tscircuit only emits a pour once routing happened, so the placement-only build of a board with
    ``[routing] mode = "manual"`` has none on a plane layer of a supply (a ``<copperpour>`` on an inner
    layer). A pour for a net and layer the board has no zone for yet is added here, as a copy of the
    board's existing pour settings (else KiCad's usual ones), over the board outline inset by ``margin``.
    With ``area`` (x0, y0, x1, y1, board mm) the pour covers only that box - a split plane or a patch on a
    signal layer (e.g. a BGA's core supply under it) - and ``priority`` lets it win over a full pour of the
    same layer; such pours are rewritten from the config on every run.
    """
    cfg = project.config.get("pour", {})
    zones = [z for z in sexpr.children(project.pcb, "zone") if sexpr.child(z, "keepout") is None]
    have = {(_zone_net(project.pcb, z), lay) for z in zones for lay in _copper_layers(z)}
    template = next((z for z in zones if sexpr.child(z, "fill") is not None), None) or sexpr.loads(
        '(zone (net 0) (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000000") (hatch edge 0.5) '
        "(connect_pads (clearance 0.2)) (min_thickness 0.25) "
        "(fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3) (island_removal_mode 0)))"
    )
    edge_points = [
        pt
        for g in project.pcb
        if isinstance(g, list)
        and sexpr.head(g) in ("gr_line", "gr_rect", "gr_arc", "gr_poly")
        and _layer(g) == "Edge.Cuts"
        for pt in _graphic_points(g)
    ]
    changes = []
    for z in [z for z in sexpr.children(project.pcb, "zone") if _is_pour_zone(z)]:
        project.pcb.remove(z)  # area pours are rewritten from the config on every run
    for name, entry in cfg.items():
        net, margin = str(entry["net"]), float(entry.get("margin", 0.5))
        declared = _declared_net(project.pcb, net)
        if declared is None:
            changes.append(f"{name}: net {net} not on the board, pour skipped")
            continue
        if not edge_points:
            changes.append(f"{name}: no board outline, pour skipped")
            continue
        x0, y0, x1, y1 = _bbox(edge_points, -margin)
        area = entry.get("area")
        if area is not None:  # a region of the layer (a split plane, a patch under a part), inside the outline
            ax0, ay0, ax1, ay1 = (float(v) for v in area)
            x0, y0, x1, y1 = max(x0, ax0), max(y0, ay0), min(x1, ax1), min(y1, ay1)
        for layer in entry["layers"]:
            if area is None and (net, layer) in have:
                continue
            zone = copy.deepcopy(template)
            for key in ("net", "net_name", "layer", "layers", "filled_polygon", "polygon"):
                for old in list(sexpr.children(zone, key)):
                    zone.remove(old)
            uid = uuid.uuid5(uuid.NAMESPACE_OID, f"pour:{name}:{layer}")
            head = [copy.deepcopy(declared), [Sym("layer"), layer]]
            if len(declared) > 2:  # (net <id> "<name>") on a pad: a zone writes (net <id>) (net_name "<name>")
                head = [[Sym("net"), declared[1]], [Sym("net_name"), net], [Sym("layer"), layer]]
            outline = sexpr.loads(
                f"(polygon (pts (xy {_mm(x0)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y1)}) (xy {_mm(x0)} {_mm(y1)})))"
            )
            uid_node = sexpr.child(zone, "uuid")
            if uid_node is not None:
                uid_node[1] = str(uid)
            zone[1:1] = head
            if area is not None:
                zone.insert(len(head) + 1, [Sym("name"), f"{POUR_ZONE_PREFIX}{name}"])
                for old in list(sexpr.children(zone, "priority")):
                    zone.remove(old)
                zone.insert(len(head) + 2, [Sym("priority"), Sym(str(int(entry.get("priority", 1))))])
            zone.append(outline)
            project.pcb.append(zone)
            where = f" in ({_mm(x0)}, {_mm(y0)})-({_mm(x1)}, {_mm(y1)})" if area is not None else ""
            changes.append(f"{name}: {net} pour on {layer}{where}")
    return changes


def _is_pour_zone(zone: list[Node]) -> bool:
    name = sexpr.child(zone, "name")
    return name is not None and len(name) > 1 and str(name[1]).startswith(POUR_ZONE_PREFIX)


def _zone_net(pcb: list[Node], zone: list[Node]) -> str:
    return _net_of(zone, _net_names(pcb))


def _is_keepout_zone(zone: list[Node]) -> bool:
    name = sexpr.child(zone, "name")
    return name is not None and len(name) > 1 and str(name[1]).startswith(KEEPOUT_ZONE_PREFIX)


def tune_lengths(project: KicadProject) -> list[str]:
    """Meanders for the length groups of the board config (``vibepcb.tune``): KiCad checks the skew, this makes it."""
    tune = importlib.import_module("vibepcb.tune")  # tune imports this module: no import at the top
    return list(tune.tune(project))


UNROUTED_NAME = "unrouted.kicad_pcb"


def apply_routes(project: KicadProject) -> list[str]:
    """Manual routing (``[routing] mode = "manual"``): ``routes.toml`` becomes tracks and vias (``vibepcb.routes``).

    The board as it is before the routes goes to ``.work/unrouted.kicad_pcb``: ``pcb route`` redoes the
    routes from it in seconds. Entries that fail are listed; the board keeps what did route.
    """
    if project.config.get("routing", {}).get("mode") != "manual":
        return []
    routes = importlib.import_module("vibepcb.routes")  # routes imports this module: no import at the top

    snapshot = project.directory.parent / ".work" / UNROUTED_NAME
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    sexpr.dump(project.pcb, snapshot)
    report = routes.apply(project, project.routes)
    return [f"{r.entry}: {r.detail}" if r.ok else f"FAILED {r.entry}: {r.detail}" for r in report.results]


FIXES: dict[str, Fix] = {
    "rename_auto_nets": rename_auto_nets,
    "propagate_split_pad_nets": propagate_split_pad_nets,
    "apply_rule_profiles": apply_rule_profiles,
    "apply_board_config": apply_board_config,  # before the routing: the router reads the net classes
    "apply_escapes": apply_escapes,
    "apply_pours": apply_pours,
    "apply_keepouts": apply_keepouts,
    "apply_routes": apply_routes,
    "thermal_pad_vias": thermal_pad_vias,  # before the track fixes: obstacles for them, kept by restore_nets
    "repair_zero_width_tracks": repair_zero_width_tracks,
    "drop_tracks_inside_pads": drop_tracks_inside_pads,
    "merge_duplicate_vias": merge_duplicate_vias,
    "plane_pad_vias": plane_pad_vias,
    "relocate_vias": relocate_vias,
    "snap_near_ends": snap_near_ends,
    "clean_tracks": clean_tracks,
    "separate_ends_on_pads": separate_ends_on_pads,
    "square_tees": square_tees,
    "push_tracks_clear": push_tracks_clear,
    "reroute_tracks": reroute_tracks,
    "remove_antennas": remove_antennas,
    "tune_lengths": tune_lengths,
    "drop_npth_edge_cuts": drop_npth_edge_cuts,
    "allow_footprints_in_keepouts": allow_footprints_in_keepouts,
    "standard_board_thickness": standard_board_thickness,
    "fix_value_equals_reference": fix_value_equals_reference,
    "fill_schematic_footprints": fill_schematic_footprints,
    "schematic_net_labels": schematic_net_labels,
    "schematic_no_connects": schematic_no_connects,
    "schematic_power_symbols": schematic_power_symbols,
    "drop_loose_power_symbols": drop_loose_power_symbols,
    "add_part_numbers": add_part_numbers,
    "mark_dnp_parts": mark_dnp_parts,
    "exclude_testpoints_from_bom": exclude_testpoints_from_bom,
    "link_footprint_library": link_footprint_library,
    "footprint_internal_clearance_rule": footprint_internal_clearance_rule,
    "zone_thermal_reliefs": zone_thermal_reliefs,
    "thermal_spoke_rule": thermal_spoke_rule,
    "router_clearance_tolerance": router_clearance_tolerance,
    "strict_track_rules": strict_track_rules,
    "place_reference_designators": place_reference_designators,
    "fit_schematic_sheet": fit_schematic_sheet,
}


TRACK_FIXES_START = "repair_zero_width_tracks"  # first geometry fix: restore_nets rolls back to before it


SLOW_FIX_S = 5.0  # fixes slower than this get a timing line in the report


def apply_all(project: KicadProject, skip: set[str] | None = None) -> dict[str, list[str]]:
    """Run every registered fix (except ``skip``), save, and return the changes per fix (slow ones with their time)."""
    report = {}
    for name, fix in FIXES.items():
        if name == TRACK_FIXES_START:
            project.before_track_fixes = copy.deepcopy(project.pcb)
        if name not in (skip or set()):
            started = time.monotonic()
            report[name] = fix(project)
            if (spent := time.monotonic() - started) > SLOW_FIX_S:
                report[name].append(f"took {spent:.0f} s")
    project.save()
    return report


TRACK_NODES = ("segment", "arc", "via")


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


def stitch_pour_islands(project: KicadProject) -> list[str]:
    """A via into every piece of a net's pour that has no connection to the net's pour on another layer.

    Tracks cut an outer-layer GND pour into pieces; a piece without a via or a through-hole pad of its
    net floats (KiCad: unconnected zone items) even though the plane is right below it. Standard
    practice is a stitching via; it goes to the spot of the piece nearest its middle that lies fully in
    the fill, keeps the clearance on every layer and lands in the net's pour on another layer. Needs
    the filled zones (``check`` runs it after KiCad's refill).
    """
    copper = _copper(project.pcb, filled_zones=True)
    clearance, edge_clearance = _design_clearances(project)
    via_size, via_drill = _design_via(project)
    gap = _min_hole_to_hole(project)
    r = via_size / 2
    others_all = copper.obstacles()
    vias_by_net: dict[str, list[tracks.Point]] = {}
    for _, c, _, n in copper.vias:
        vias_by_net.setdefault(n, []).append(c)
    tht = [o for o in copper.pads if len(o.layers) > 1]
    all_vias = [(c, rr) for _, c, rr, _ in copper.vias]
    changes = []
    for (net, layer), polys in sorted(copper.zones.items()):
        planes = [poly for (n, lay), ps in copper.zones.items() if n == net and lay != layer for poly in ps]
        net_node = _net_node(project.pcb, net)
        if not planes or net_node is None:
            continue
        others = [o for o in others_all if o.net != net]
        for poly in polys:
            if len(poly) < 3 or any(tracks.point_in_polygon(v, poly) for v in vias_by_net.get(net, [])):
                continue
            if any(o.net == net and tracks.point_in_polygon(o.centre, poly) for o in tht):
                continue
            ring = list(itertools.pairwise([*poly, poly[0]]))
            xs, ys = [q[0] for q in poly], [q[1] for q in poly]
            mid = (sum(xs) / len(xs), sum(ys) / len(ys))

            def fits(
                q: tracks.Point,
                ring: list[tuple[tracks.Point, tracks.Point]] = ring,
                poly: list[tracks.Point] = poly,
                planes: list[list[tracks.Point]] = planes,
                others: list[tracks.Obstacle] = others,
            ) -> bool:
                return (
                    tracks.point_in_polygon(q, poly)
                    and all(tracks.point_seg_distance(q, a, b) >= r for a, b in ring)  # the via's copper in the fill
                    and any(tracks.point_in_polygon(q, pl) for pl in planes)
                    and all(o.distance(q, q) >= r + clearance for o in others)  # every layer
                    and all(e.distance(q, q) >= r + edge_clearance for e in copper.edges)
                    and all(math.dist(q, c) >= rr + r + gap for c, rr in all_vias)
                )

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
            spot = next((q for q in candidates if fits(q)), None)
            if spot is None:
                continue
            x, y = _mm(spot[0]), _mm(spot[1])
            uid = uuid.uuid5(uuid.NAMESPACE_OID, f"island-via:{net}:{x},{y}")
            via = sexpr.loads(
                f'(via (at {x} {y}) (size {_mm(via_size)}) (drill {_mm(via_drill)}) (layers "F.Cu" "B.Cu") (uuid "{uid}"))'
            )
            via.insert(len(via) - 1, copy.deepcopy(net_node))
            project.pcb.append(via)
            vias_by_net.setdefault(net, []).append(spot)
            all_vias.append((spot, r))
            changes.append(f"{net} pour piece on {layer}: stitching via at ({x}, {y})")
    return changes


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
    planes = [poly for (n, lay), polys in copper.zones.items() if n == net and lay != layer for poly in polys]
    net_node = _net_node(project.pcb, net)
    if layer is None or not planes or net_node is None:
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
            and any(tracks.point_in_polygon(q, poly) for poly in planes)
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


def _net_node(pcb: list[Node], net: str) -> list[Node] | None:
    """A ``(net ...)`` node of ``net`` as the board writes it on tracks and vias.

    KiCad 9 files give tracks and vias ``(net <id>)`` and pads ``(net <id> "<name>")``; a pad's
    form on a via crashes kicad-cli's DRC (seen with KiCad 10). KiCad 10 saves ``(net "<name>")``
    everywhere. A track's or via's node is taken as it is, a pad's is cut down to its id.
    """
    names = _net_names(pcb)
    pad_node = None
    for node in sexpr.walk(pcb):
        if sexpr.head(node) in ("pad", "segment", "via") and isinstance(node, list):
            child = sexpr.child(node, "net")
            if child is None or _net_of(node, names) != net:
                continue
            if sexpr.head(node) != "pad":
                return child
            pad_node = pad_node or child
    if pad_node is not None and len(pad_node) > 2 and isinstance(pad_node[1], Sym):
        return pad_node[:2]  # (net <id> "<name>") -> (net <id>)
    return pad_node


def _design_via(project: KicadProject) -> tuple[float, float]:
    """(via diameter, drill) of the Default net class."""
    try:
        data = json.loads(project.pcb_path.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
        default = next(c for c in data["net_settings"]["classes"] if c.get("name") == "Default")
        return float(default["via_diameter"]), float(default["via_drill"])
    except (OSError, ValueError, KeyError, StopIteration):
        return DEFAULT_VIA_MM


DEFAULT_VIA_MM = (0.6, 0.3)


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
