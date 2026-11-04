"""Deterministic placement check: the rules a script can judge from where the parts sit (circuit JSON, no routing).

Runs on the unrouted circuit JSON (``pcb place-check <board>``, right after the placement gate) and in ``check``
(its findings go into ``findings.toml`` with ``source = "placement"``; vibe mode: warnings, ``--strict``: errors).
Distances are pad centre to pad centre in mm, positions in the board frame (= a part's TSX ``pcbX`` / ``pcbY``
unless it sits in a moved ``<group>``). Rules, finding ids ``place_<rule>:<ref>``:

- ``decoupling``  a capacitor (at most ``bulk_cap_uf``; bigger ones are bulk) between a supply net (a schematic
                  rail, a voltage in its name, or a chip power pin on it; ``netcheck``'s pin names) and ground,
                  farther than ``decoupling_mm`` from the supply pin it serves: the chip pin on its net nearest to it
                  (the schematic owner's, ``schgen.passive_owners``, when that one is within the limit; BGA balls
                  have no names and all count). A chip's caps on one net share out its pins, closest pairs first,
                  one cap per pin; a cap nearest a connector pin is input filtering and not judged
- ``crystal``     a crystal / resonator (or an oscillator's output) farther than ``crystal_mm`` from the chip clock
                  pin on its net (the worst of its nets); ``load_cap``: a capacitor from a crystal net to ground
                  farther than ``load_cap_mm`` from the crystal's pad
- ``regulator``   for each ``netcheck.find_regulators`` part: its inductor farther than ``regulator_mm`` from the
                  SW / LX pin, and a cap (any value) whose nearest pin is its VIN / VOUT or the inductor's output
                  side farther than ``regulator_mm`` from it: the input and switching loops must stay compact
- ``connector``   a connector mated from the side whose mating side is more than ``connector_edge_mm`` from the
                  board edge. The side: the footprint's ``insertionDirection`` (``from_above`` / ``from_below``:
                  mated from the top, skipped), else where courtyard + silkscreen reach farthest past the pads
                  (USB-C, RJ45, terminal blocks: the pads sit at the back); footprints symmetric around their pads
                  (pin / box headers) are skipped, and so is an inferred side when another side already is at an
                  edge (the designer put it there; the guess is wrong for some jacks)

Every finding says where to put the part: the nearest spot next to the pin where its box (current orientation)
overlaps no other part on its layer and stays on the board (0.25 mm grid), or for a connector the position that
puts its mating side at the edge (and the rotation, when another side already is at an edge).

Limits: ``[placement]`` in ``rules/sim-defaults.toml`` (house), overridden per board by ``[placement]`` in
``boards/<board>/vibepcb.toml``; keys = the fields of :class:`Limits`.

For the placement suggester (issue #8): :func:`analyse` returns every judged relation (:class:`Link`: part, the
pin it serves, distance, limit - also the ones within their limit), the connector edges and the :class:`Layout`;
:func:`free_spot` finds a free position for a part near a point.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb.check import netcheck, sim
from vibepcb.check.findings import Finding
from vibepcb.kicad import schgen
from vibepcb.settings import config

if TYPE_CHECKING:
    from vibepcb.check.netcheck import Design, Pin

    Point = tuple[float, float]
    Box = tuple[float, float, float, float]  # x0, y0, x1, y1

SOURCE = "placement"
GRID = 0.25  # free-spot search grid, mm
SPOT_RADIUS = 8.0  # how far from the pin a free spot is looked for
CLEARANCE = 0.25  # part box to part box in a free spot
EDGE_MARGIN = 0.5  # part box to board edge in a free spot
ASYMMETRY = 1.0  # courtyard reaching this much farther past the pads on one side: mated from that side
CRYSTAL_TYPES = {"simple_crystal", "simple_resonator"}
CRYSTAL_REF = re.compile(r"^(X|Y|XTAL|Q|OSC)\d+$", re.IGNORECASE)
CLOCK_PIN = re.compile(r"OSC|XTAL|XT|^XI|^XO|XIN|XOUT|CLK|^RS$|^RTC$|^X\d$", re.IGNORECASE)
OSC_OUT = {"OUT", "CLK", "CLKOUT", "OUTPUT"}
CONNECTOR_REF = re.compile(r"^(J|CN|CON|USB|JACK)\d+$", re.IGNORECASE)
SIDE_ENTRY = re.compile(r"TYPE-?C|USB|RJ45|HR911|SJ-\d|PJ-\d|DC-?00\d|BARREL|DG30\d|KF30\d|TERMINAL", re.IGNORECASE)
SWITCH_PIN = re.compile(r"^(SW|LX|PH)\d*$", re.IGNORECASE)
REG_OUT = {"VOUT", "OUT", "VO"}
INSERTION = {"from_left": (-1.0, 0.0), "from_right": (1.0, 0.0), "from_top": (0.0, 1.0), "from_bottom": (0.0, -1.0)}
SIDE_NAMES = {(1.0, 0.0): "+x", (-1.0, 0.0): "-x", (0.0, 1.0): "+y", (0.0, -1.0): "-y"}


class PlacementError(config.ConfigError):
    """A [placement] table is malformed."""


@dataclass(frozen=True)
class Limits:
    decoupling_mm: float = 3.0
    bulk_cap_uf: float = 1.0
    crystal_mm: float = 5.0
    load_cap_mm: float = 3.0
    regulator_mm: float = 5.0
    connector_edge_mm: float = 1.5


def parse_limits(*tables: dict[str, Any], where: str = "[placement]") -> Limits:
    """:class:`Limits` from ``[placement]`` tables, later ones winning."""
    names = set(config.PLACEMENT_KEYS)
    values: dict[str, float] = {}
    for table in tables:
        if bad := set(table) - names:
            msg = f"{where}: unknown key(s) {sorted(bad)} (known: {', '.join(sorted(names))})"
            raise PlacementError(msg)
        for key, value in table.items():
            if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
                msg = f"{where}: {key} must be a number >= 0"
                raise PlacementError(msg)
            values[key] = float(value)
    return Limits(**values)


def load_limits(board: str) -> Limits:
    """House ``[placement]`` (``rules/sim-defaults.toml``) overridden by the board's ``vibepcb.toml``."""
    return parse_limits(sim.load_defaults().placement, config.load(board).get("placement", {}))


# --- layout ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Body:
    ref: str
    ftype: str
    centre: Point
    box: Box  # what the part occupies (circuit JSON width / height, board frame)
    courtyard: Box  # courtyard + body drawing (silkscreen / fab paths)
    layer: str
    tht: bool
    insertion: str | None
    pads: tuple[Point, ...]


@dataclass
class Layout:
    bodies: dict[str, Body] = field(default_factory=dict)
    pads: dict[tuple[str, str], Point] = field(default_factory=dict)  # (ref, pin number) -> pad centre
    outline: list[Point] = field(default_factory=list)

    def pad(self, pin: Pin) -> Point | None:
        return self.pads.get((pin.ref, pin.number))


def _box_of(points: list[Point]) -> Box:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _courtyard_points(e: dict[str, Any]) -> list[Point]:
    if e.get("outline"):
        return [(float(p["x"]), float(p["y"])) for p in e["outline"]]
    centre = e.get("center")
    if not centre:
        return []
    cx, cy = float(centre["x"]), float(centre["y"])
    if "radius" in e:
        r = float(e["radius"])
        return [(cx - r, cy - r), (cx + r, cy + r)]
    w, h = float(e.get("width", 0)) / 2, float(e.get("height", 0)) / 2
    a = math.radians(float(e.get("ccw_rotation", 0)))
    ca, sa = math.cos(a), math.sin(a)
    return [(cx + dx * ca - dy * sa, cy + dx * sa + dy * ca) for dx, dy in ((-w, -h), (w, -h), (w, h), (-w, h))]


def load_layout(circuit: list[dict[str, Any]]) -> Layout:
    """Part bodies, pad positions and the board outline from a circuit JSON (routed or not)."""
    sources = {e["source_component_id"]: e for e in circuit if e.get("type") == "source_component"}
    pin_of = {
        e["source_port_id"]: (str(sources[e["source_component_id"]]["name"]), str(e.get("pin_number") or e["name"]))
        for e in circuit
        if e.get("type") == "source_port"
        and schgen.INTERNAL_PORT not in str(e.get("name"))
        and e.get("source_component_id") in sources
    }
    layout = Layout()
    pads_of: dict[str, list[Point]] = {}
    courtyards: dict[str, list[Point]] = {}
    tht: set[str] = set()
    for e in circuit:
        kind = e.get("type")
        if kind == "pcb_port":
            point = (float(e["x"]), float(e["y"]))
            pads_of.setdefault(e.get("pcb_component_id", ""), []).append(point)
            if (key := pin_of.get(e.get("source_port_id", ""))) is not None:
                layout.pads[key] = point
        elif str(kind).startswith("pcb_courtyard"):
            courtyards.setdefault(e.get("pcb_component_id", ""), []).extend(_courtyard_points(e))
        elif kind in {"pcb_silkscreen_path", "pcb_fabrication_note_path"}:  # the body drawing can outgrow the courtyard
            route = [(float(p["x"]), float(p["y"])) for p in e.get("route", [])]
            courtyards.setdefault(e.get("pcb_component_id", ""), []).extend(route)
        elif kind == "pcb_plated_hole":
            tht.add(e.get("pcb_component_id", ""))
        elif kind == "pcb_board":
            if e.get("outline"):
                layout.outline = [(float(p["x"]), float(p["y"])) for p in e["outline"]]
            else:
                cx, cy = float(e["center"]["x"]), float(e["center"]["y"])
                w, h = float(e["width"]) / 2, float(e["height"]) / 2
                layout.outline = [(cx - w, cy - h), (cx + w, cy - h), (cx + w, cy + h), (cx - w, cy + h)]
    for e in circuit:
        if e.get("type") != "pcb_component" or e.get("source_component_id") not in sources:
            continue
        src, pid = sources[e["source_component_id"]], e["pcb_component_id"]
        cx, cy = float(e["center"]["x"]), float(e["center"]["y"])
        w, h = float(e.get("width") or 0) / 2, float(e.get("height") or 0) / 2
        box = (cx - w, cy - h, cx + w, cy + h)
        ref = str(src["name"])
        layout.bodies[ref] = Body(
            ref,
            str(src.get("ftype", "")),
            (cx, cy),
            box,
            _box_of(courtyards[pid]) if courtyards.get(pid) else box,
            str(e.get("layer", "top")),
            pid in tht,
            e.get("insertion_direction"),
            tuple(pads_of.get(pid, [])),
        )
    return layout


# --- geometry -------------------------------------------------------------------------------------------------


def _inside(outline: list[Point], p: Point) -> bool:
    x, y, inside = p[0], p[1], False
    for (x0, y0), (x1, y1) in zip(outline, outline[1:] + outline[:1], strict=True):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _edge_distance(outline: list[Point], p: Point) -> float:
    return min(_segment_distance(p, a, b) for a, b in zip(outline, outline[1:] + outline[:1], strict=True))


def ray_to_edge(outline: list[Point], p: Point, d: Point) -> float:
    """Distance from ``p`` along unit vector ``d`` to the board edge; negative (the overhang) when ``p`` is off
    the board."""
    if not outline:
        return 0.0
    if not _inside(outline, p):
        return -_edge_distance(outline, p)
    best = math.inf
    for a, b in zip(outline, outline[1:] + outline[:1], strict=True):
        ex, ey = b[0] - a[0], b[1] - a[1]
        den = d[0] * ey - d[1] * ex
        if abs(den) < 1e-12:
            continue
        t = ((a[0] - p[0]) * ey - (a[1] - p[1]) * ex) / den
        u = ((a[0] - p[0]) * d[1] - (a[1] - p[1]) * d[0]) / den
        if t >= 0 and -1e-9 <= u <= 1 + 1e-9:
            best = min(best, t)
    return best if best < math.inf else 0.0


def _overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def free_spot(
    layout: Layout, ref: str, anchor: Point, radius: float = SPOT_RADIUS, taken: list[Box] | None = None
) -> Point | None:
    """The centre nearest ``anchor`` (``GRID`` steps, within ``radius``) where part ``ref``'s box, as it is
    turned now, keeps ``CLEARANCE`` to every other part on its layer (through-hole parts: on any layer), to the
    boxes in ``taken`` (spots already handed out; the chosen box is appended) and ``EDGE_MARGIN`` to the board
    edge. ``None``: no such spot."""
    body = layout.bodies[ref]
    hw, hh = (body.box[2] - body.box[0]) / 2 + CLEARANCE, (body.box[3] - body.box[1]) / 2 + CLEARANCE
    reach = radius + max(hw, hh) + CLEARANCE
    obstacles = [
        b.box
        for b in layout.bodies.values()
        if b.ref != ref
        and (b.layer == body.layer or b.tht or body.tht)
        and b.box[0] - reach < anchor[0] < b.box[2] + reach
        and b.box[1] - reach < anchor[1] < b.box[3] + reach
    ]
    obstacles += taken or []
    steps = int(radius / GRID)
    candidates = sorted(
        ((i * GRID, j * GRID) for i in range(-steps, steps + 1) for j in range(-steps, steps + 1)),
        key=lambda o: (math.hypot(*o), o),
    )
    for dx, dy in candidates:
        if math.hypot(dx, dy) > radius:
            break
        cx, cy = round((anchor[0] + dx) / GRID) * GRID, round((anchor[1] + dy) / GRID) * GRID
        box = (cx - hw, cy - hh, cx + hw, cy + hh)
        if any(_overlap(box, o) for o in obstacles):
            continue
        corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
        if layout.outline and not all(
            _inside(layout.outline, c) and _edge_distance(layout.outline, c) >= EDGE_MARGIN - CLEARANCE for c in corners
        ):
            continue
        if taken is not None:
            taken.append((box[0] + CLEARANCE, box[1] + CLEARANCE, box[2] - CLEARANCE, box[3] - CLEARANCE))
        return cx, cy
    return None


# --- relations ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    """A part that belongs next to a pin: ``ref``'s pad ``pin`` on ``net`` is ``distance`` mm from
    ``target``'s pin ``target_pin`` (named ``target_name``)."""

    rule: str  # decoupling | crystal | load_cap | regulator
    ref: str
    pin: str
    target: str
    target_pin: str
    target_name: str
    net: str
    distance: float
    limit: float
    anchor: Point  # the target pad

    @property
    def far(self) -> bool:
        return self.distance > self.limit + 1e-6


@dataclass(frozen=True)
class Edge:
    """A side-mated connector: its mating side ``side`` (unit vector) is ``distance`` mm from the board edge
    (negative: past it)."""

    ref: str
    side: Point
    distance: float
    limit: float
    sides: dict[Point, float]  # every side's distance to the edge

    @property
    def far(self) -> bool:
        return self.distance > self.limit + 1e-6


@dataclass
class Analysis:
    layout: Layout
    links: list[Link] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    owners: dict[str, str] = field(default_factory=dict)  # passive -> chip (schgen.passive_owners)


def _is_power_pin(name: str) -> bool:
    return bool(netcheck.POWER_PIN.match(name) or schgen._POWER_PIN.match(name))


def _ground_caps(design: Design) -> list[tuple[str, Pin]]:
    """(capacitor, its pin on the non-ground net) of every capacitor between a net and ground."""
    out = []
    for ref, part in sorted(design.parts.items()):
        pins = design.pins_of(ref)
        if part.kind != "capacitor" or len(pins) != 2:
            continue
        grounded = [p for p in pins if netcheck.is_ground(p.net)]
        live = [p for p in pins if p.net and not netcheck.is_ground(p.net)]
        if len(grounded) == 1 and len(live) == 1:
            out.append((ref, live[0]))
    return out


def _link(layout: Layout, rule: str, part_pin: Pin, target: Pin, limit: float) -> Link | None:
    a, b = layout.pad(part_pin), layout.pad(target)
    if a is None or b is None:
        return None
    return Link(
        rule, part_pin.ref, part_pin.number, target.ref, target.number, target.name, part_pin.net or "",
        round(math.dist(a, b), 2), limit, b,
    )  # fmt: skip


def _nearest(layout: Layout, rule: str, part_pin: Pin, targets: list[Pin], limit: float) -> Link | None:
    links = [k for t in targets if (k := _link(layout, rule, part_pin, t, limit)) is not None]
    return min(links, key=lambda k: (k.distance, k.target, k.target_pin)) if links else None


def find_crystals(design: Design, ftypes: dict[str, str]) -> dict[str, list[Pin]]:
    """Crystal / resonator / oscillator reference -> its pins whose nets go to the chip it clocks: a crystal part,
    or a small part named X / Y / Q / OSC whose signal nets reach a chip clock pin (OSC*, XI / XO, CLK, ...);
    for an oscillator (an OUT pin and a supply pin) only its output."""
    chips = {r for r, p in design.parts.items() if p.kind == "chip" and p.ports >= 3}
    nets = design.nets()
    out: dict[str, list[Pin]] = {}
    for ref, part in sorted(design.parts.items()):
        pins = design.pins_of(ref)
        signal = [p for p in pins if p.net and not netcheck.is_ground(p.net)]
        if ftypes.get(ref) in CRYSTAL_TYPES:
            out[ref] = signal
            continue
        if not CRYSTAL_REF.match(ref) or not 2 <= len(pins) <= 6:
            continue
        outs = [p for p in signal if p.name.upper() in OSC_OUT]
        if outs and any(_is_power_pin(p.name) for p in pins):
            out[ref] = outs
        elif part.ports <= 4 and any(
            q.ref != ref and q.ref in chips and CLOCK_PIN.search(q.name) for p in signal for q in nets[p.net or ""]
        ):
            out[ref] = signal
    return out


def find_connectors(design: Design) -> list[str]:
    """Connector references: J / CN / USB ... designators, or a known side-entry part (USB, RJ45, jack, terminal
    block) under any designator."""
    return sorted(r for r, p in design.parts.items() if CONNECTOR_REF.match(r) or SIDE_ENTRY.search(p.text))


def _edge(layout: Layout, ref: str, limit: float) -> Edge | None:
    body = layout.bodies.get(ref)
    if body is None or not body.pads or body.insertion in {"from_above", "from_below"}:
        return None
    cb = body.courtyard
    px = sum(p[0] for p in body.pads) / len(body.pads)
    py = sum(p[1] for p in body.pads) / len(body.pads)
    reach = {(1.0, 0.0): cb[2] - px, (-1.0, 0.0): px - cb[0], (0.0, 1.0): cb[3] - py, (0.0, -1.0): py - cb[1]}
    explicit = body.insertion in INSERTION
    if explicit:
        side = INSERTION[str(body.insertion)]
    else:
        side = max(reach, key=lambda s: (reach[s], s))
        if reach[side] - reach[(-side[0], -side[1])] < ASYMMETRY:
            return None  # symmetric around its pads: mated from above (header) or unknown
    mid = {
        (1.0, 0.0): (cb[2], (cb[1] + cb[3]) / 2),
        (-1.0, 0.0): (cb[0], (cb[1] + cb[3]) / 2),
        (0.0, 1.0): ((cb[0] + cb[2]) / 2, cb[3]),
        (0.0, -1.0): ((cb[0] + cb[2]) / 2, cb[1]),
    }
    sides = {s: round(ray_to_edge(layout.outline, mid[s], s), 2) for s in mid}
    if not explicit and sides[side] > limit and min(sides.values()) <= limit:
        return None  # an inferred side is a guess; a connector already at an edge was put there on purpose
    return Edge(ref, side, sides[side], limit, sides)


def analyse(circuit: list[dict[str, Any]], limits: Limits) -> Analysis:
    """Every judged relation of the board (within the limits or not)."""
    design = netcheck.load_design(circuit)
    layout = load_layout(circuit)
    ftypes = {str(e["name"]): str(e.get("ftype", "")) for e in circuit if e.get("type") == "source_component"}
    owners = schgen.board_owners(circuit)
    schgen_rails = schgen.rails(circuit)
    result = Analysis(layout, owners=owners)
    nets = design.nets()
    crystals = find_crystals(design, ftypes)
    connectors = find_connectors(design)
    chips = {r for r, p in design.parts.items() if p.kind == "chip" and p.ports >= 3 and r not in crystals} - set(
        connectors
    )
    caps = _ground_caps(design)
    claimed: set[str] = set()

    # crystals and their load caps
    crystal_nets: dict[str, Pin] = {}
    for ref, pins in crystals.items():
        worst: Link | None = None
        for pin in pins:
            crystal_nets.setdefault(pin.net or "", pin)
            targets = [q for q in nets.get(pin.net or "", []) if q.ref in chips]
            link = _nearest(layout, "crystal", pin, targets, limits.crystal_mm)
            if link is not None and (worst is None or link.distance > worst.distance):
                worst = link
        if worst is not None:
            result.links.append(worst)
    for ref, pin in caps:
        if pin.net in crystal_nets and ref not in crystals:
            claimed.add(ref)
            if link := _link(layout, "load_cap", pin, crystal_nets[pin.net], limits.load_cap_mm):
                result.links.append(link)

    # regulators: the inductor at SW / LX; its output side serves the output caps
    regulators = set(netcheck.find_regulators(design))
    far_ends: dict[str, list[Pin]] = {}  # buck output net -> the inductor pads on it
    for reg in sorted(regulators):
        for sw in (p for p in design.pins_of(reg) if SWITCH_PIN.match(p.name) and p.net):
            for q in nets.get(sw.net or "", []):
                if design.parts[q.ref].kind != "inductor":
                    continue
                if link := _link(layout, "regulator", q, sw, limits.regulator_mm):
                    result.links.append(link)
                for other in design.pins_of(q.ref):
                    if other is not q and other.net and not netcheck.is_ground(other.net):
                        far_ends.setdefault(other.net, []).append(other)
    inductors = {p.ref for pins in far_ends.values() for p in pins}

    def supply_pins(chip: str, net: str) -> list[Pin]:
        on_net = [q for q in nets[net] if q.ref == chip]
        return [q for q in on_net if _is_power_pin(q.name)] or on_net  # BGA balls carry no pin names

    # every other cap to ground serves the chip pin on its net nearest to it (its schematic owner's when that one
    # is within the limit): a regulator's VIN / VOUT or its inductor's output side -> regulator rule; a connector
    # pin -> input filtering, not judged; else decoupling, against that chip's supply pins on the net
    groups: dict[tuple[str, str], list[Pin]] = {}
    supplies = {
        net
        for net, pins in nets.items()
        if net in schgen_rails or netcheck.name_voltage(net) is not None or any(_is_power_pin(q.name) for q in pins)
    }
    for ref, pin in caps:
        if ref in claimed or pin.net not in supplies:
            continue  # a filter / timing cap on a signal net
        net = pin.net or ""
        nearby = [q for c in sorted({q.ref for q in nets[net] if q.ref in chips}) for q in supply_pins(c, net)]
        nearby += [q for q in nets[net] if q.ref in connectors] + far_ends.get(net, [])
        nearby += [q for q in nets[net] if q.ref in regulators and q.name.upper() in netcheck.REGULATOR_IN | REG_OUT]
        links = sorted(
            (k for t in nearby if (k := _link(layout, "decoupling", pin, t, limits.decoupling_mm))),
            key=lambda k: (k.distance, k.target, k.target_pin),
        )
        if not links:
            continue
        own = [k for k in links if k.target == owners.get(ref) and k.distance <= limits.decoupling_mm]
        best = own[0] if own else links[0]
        if best.target in connectors:
            continue
        reg_pins = [
            q
            for q in nets[net]
            if best.target in regulators and q.ref == best.target and q.name.upper() in netcheck.REGULATOR_IN | REG_OUT
        ]
        if best.target in inductors or reg_pins:
            targets = reg_pins or [q for q in far_ends[net] if q.ref == best.target]
            if k := _nearest(layout, "regulator", pin, targets, limits.regulator_mm):
                result.links.append(k)
            continue
        part = design.parts[ref]
        if part.value is None or part.value <= limits.bulk_cap_uf * 1e-6:
            groups.setdefault((best.target, net), []).append(pin)
    for (chip, net), cap_pins in sorted(groups.items()):
        targets = supply_pins(chip, net)
        pairs = sorted(
            (k for c in cap_pins for t in targets if (k := _link(layout, "decoupling", c, t, limits.decoupling_mm))),
            key=lambda k: (k.distance, k.ref, k.target_pin),
        )
        used_caps: set[str] = set()
        used_pins: set[str] = set()
        for k in pairs:
            if k.ref not in used_caps and k.target_pin not in used_pins:
                result.links.append(k)
                used_caps.add(k.ref)
                used_pins.add(k.target_pin)
        for c in cap_pins:  # more caps than pins: each to its nearest pin
            if c.ref not in used_caps and (k := _nearest(layout, "decoupling", c, targets, limits.decoupling_mm)):
                result.links.append(k)

    for ref in connectors:
        if (edge := _edge(layout, ref, limits.connector_edge_mm)) is not None:
            result.edges.append(edge)
    result.links.sort(key=lambda k: (k.rule, k.ref))
    return result


# --- findings -------------------------------------------------------------------------------------------------

_WHAT = {
    "decoupling": "the power pin it decouples",
    "crystal": "the clock pin it drives",
    "load_cap": "its crystal's pad",
    "regulator": "the regulator pin it serves",
}


def _where(p: Point) -> str:
    return f"pcbX={p[0]:g} pcbY={p[1]:g}"


def _rotation(side: Point, to: Point) -> int:
    """Degrees counter-clockwise (TSX ``pcbRotation`` delta) that turn ``side`` into ``to``."""
    return round(math.degrees(math.atan2(to[1], to[0]) - math.atan2(side[1], side[0]))) % 360


def link_finding(analysis: Analysis, link: Link, taken: list[Box] | None = None) -> Finding:
    named = link.target_name not in {link.target_pin, f"pin{link.target_pin}"}
    target = f"{link.target}.{link.target_name}" + (f" (pin {link.target_pin})" if named else "")
    body = analysis.layout.bodies.get(link.target)
    what = "the regulator's inductor output" if body and body.ftype == "simple_inductor" else _WHAT[link.rule]
    text = f"{link.distance:.1f} mm from {what} {target} on {link.net} (limit {link.limit:g} mm); "
    spot = (
        free_spot(analysis.layout, link.ref, link.anchor, taken=taken) if link.ref in analysis.layout.bodies else None
    )
    text += (
        f"free spot next to it: {_where(spot)}"
        if spot
        else f"no free spot within {SPOT_RADIUS:g} mm: make room next to the pin"
    )
    return Finding(f"place_{link.rule}:{link.ref}", link.ref, link.pin, text, source=SOURCE)


def edge_finding(analysis: Analysis, edge: Edge) -> Finding:
    body = analysis.layout.bodies[edge.ref]
    side = SIDE_NAMES[edge.side]
    moved = (
        round(body.centre[0] + edge.side[0] * edge.distance, 2),
        round(body.centre[1] + edge.side[1] * edge.distance, 2),
    )
    text = f"mating side ({side}) is {edge.distance:.1f} mm from the board edge (limit {edge.limit:g} mm); move it to {_where(moved)}"
    best = min(edge.sides, key=lambda s: (edge.sides[s], s))
    if best != edge.side and edge.sides[best] <= edge.limit:
        text += f", or turn it by {_rotation(edge.side, best)} deg (pcbRotation) to face {SIDE_NAMES[best]}, {edge.sides[best]:.1f} mm from the edge"
    return Finding(f"place_connector:{edge.ref}", edge.ref, "", text, source=SOURCE)


def findings(analysis: Analysis) -> list[Finding]:
    """One finding per part beyond its limit."""
    taken: list[Box] = []  # one spot per part: two caps are not sent to the same place
    out = [link_finding(analysis, k, taken) for k in analysis.links if k.far]
    out += [edge_finding(analysis, e) for e in analysis.edges if e.far]
    seen: set[str] = set()
    unique = []
    for f in out:  # a part judged twice (a cap on two rules) keeps its first finding
        if f.id not in seen:
            seen.add(f.id)
            unique.append(f)
    return sorted(unique, key=lambda f: f.id)


def run(circuit: list[dict[str, Any]], limits: Limits) -> tuple[Analysis, list[Finding]]:
    analysis = analyse(circuit, limits)
    return analysis, findings(analysis)
