"""Generate a readable KiCad schematic from tscircuit's circuit JSON.

tscircuit's schematic layout does not scale (a ~250-part board does not finish in 20 minutes),
so builds always disable it and the schematic comes from here, in well under a second:

- one framed block per TSX section (``{/* ===== title ===== */}`` headers, parts in source order);
- resistors, capacitors, inductors, diodes, crystals and 2-pin buttons get real symbols, every
  other part is a box with its pins (left / right half by pin number);
- the two-terminal parts hang off the chip pins they serve as wired trees (pin - R - node - C - GND),
  the chip's pin pitch stretched to make room; parts no chip reaches start from a labelled net;
- GND / supply rails end in rail symbols (``fixes.schematic_power_symbols`` makes them power
  symbols); a net is wired once per block and labelled there, every further appearance is a label
  (double arrow when the net leaves the block); labels always, so KiCad keeps the PCB's net names;
- capacitors between two rails (decoupling) stand at the supply pin of the chip they serve
  (a wire out of the pin, the caps standing on it with GND below); ones no chip takes stand in a
  row at the block end; two parts in series between two rails (LED + resistor) stand as one column;
- rail symbols point GND down / supplies up wherever that room is free (``Sheet.finish``).
"""

from __future__ import annotations

import copy
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb import easyeda, fixes, sexpr, symlib  # fixes uses schgen at call time only

if TYPE_CHECKING:
    from pathlib import Path

GRID = 2.54  # everything sits on the 1.27 mm connection grid
HALF = 1.27
PIN_LENGTH = 2.54
FONT = 1.27
SMALL = 1.0  # reference / value of the two-terminal parts
CHAR_W = 1.0  # width per character at 1.27 mm (generous)
SMALL_CHAR_W = 1.0  # width per character at SMALL (KiCad font, measured)
STUB = 2.54  # wire from a node to a part / label
TWO_LEN = 7.62  # pin end to pin end of a two-terminal symbol
TWO_TEXT = 2.286  # reference above / value below a horizontal two-terminal symbol
BRANCH_EXTENT = 3.81  # room above / below the row of a two-terminal part
MAX_JOG = 2 * GRID  # a library pin's tree may sit this far off its pin, else it moves next to the chip
LONG_PIN_ROW = 12  # on longer pin rows (MCUs) a tree must not push its neighbours at all
CELL_GAP = 7.62
BLOCK_PAD = 7.62
TITLE = 2.54  # block title font
SHEET_MARGIN = 15.24
A0_USABLE = (1189.0 - 2 * 12.0, 841.0 - 2 * 12.0 - 40.0)  # fit_schematic_sheet's frame + title block
INTERNAL_PORT = "_internal_"
NAMESPACE = uuid.UUID("5b0e6c3e-6f43-4a51-9f7b-2c3f6d1e8a10")
VALUE_KEYS = ("display_resistance", "display_capacitance", "display_inductance", "manufacturer_part_number")
RAIL_DOWN, RAIL_UP, RAIL_NEG = "Custom:rail_down", "Custom:rail_up", "Custom:rail_neg"  # fixes.RAIL_LIB_PREFIX
PAPERS = ((297.0, 210.0), (420.0, 297.0), (594.0, 420.0), (841.0, 594.0), (1189.0, 841.0))  # A4 .. A0

_SECTION = re.compile(r"\{/\*\s*=+\s*(.*?)\s*=+\s*\*/\}")
_NAME = re.compile(r"""\bname\s*=\s*["']([^"']+)["']""")
_GROUND = re.compile(r"GND|VSS", re.IGNORECASE)
_SUPPLY = re.compile(r"^[+-]?(V|VCC|VDD|VBUS|VIN|VBAT|\d+V)", re.IGNORECASE)
# chip pins that take a supply (VDD, AVDD, VIN, VPOS, VS_NEG, V+, ...): a net on one is a rail, not a signal
_POWER_PIN = re.compile(
    r"^(A|D|P)?V(CC|DD|EE|IN|BUS|BAT|POS|NEG|S_?POS|S_?NEG|_POS|_NEG|BIAS)?\d*$|^VS?[+-]$"
    r"|^\+?\d+V\d*$|^(A|D)?V(CC|DD)[A-Z0-9_]*$",  # 3V3 / 5V pins (modules), VDDA, VDD_SPI, VCCIO
    re.I,
)
_NEGATIVE_PIN = re.compile(r"NEG|^VEE|^VS?-$", re.IGNORECASE)  # a rail on such a pin is a negative supply
_GENERIC_PIN = re.compile(r"^(pin)?\d+$", re.IGNORECASE)
_ANODE = {"a", "anode", "pos"}
_CATHODE = {"c", "k", "cathode", "neg"}
TWO_KINDS = {
    "simple_resistor": "R",
    "simple_capacitor": "C",
    "simple_inductor": "L",
    "simple_diode": "D",
    "simple_led": "LED",
    "simple_crystal": "Y",
    "simple_push_button": "SW",
}


def _uid(*parts: object) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(str(p) for p in parts)))


def _snap(v: float, grid: float = GRID) -> float:
    return round(math.ceil(v / grid - 1e-9) * grid, 4)


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)  # KiCad strings: double quotes, backslash escapes


def _fmt(v: float) -> str:
    return f"{round(v, 4):g}"


def _text_w(text: str) -> float:
    return len(text) * CHAR_W


def _font(size: float = FONT) -> str:
    return f"(font (size {_fmt(size)} {_fmt(size)}))"


# --- netlist -----------------------------------------------------------------------


@dataclass
class Pin:
    number: str
    name: str
    net: str | None


@dataclass
class Part:
    name: str
    value: str
    kind: str  # "chip" or a two-terminal kind (TWO_KINDS values)
    pins: list[Pin]
    lcsc: str = ""
    pos: tuple[float, float] | None = None  # PCB placement centre: which chip a passive serves

    @property
    def two(self) -> bool:
        return self.kind != "chip"


def _nets(circuit: list[dict[str, Any]]) -> dict[str, str]:
    """source_port_id -> net name.

    An unnamed net takes the name of a named trace on it (``<trace name="SD_DQ0">``: rules,
    net classes and diff pairs can then refer to it), else the first chip pin on it with a real
    pin name (``U1_VREG_1V2``), else KiCad-style after its first pin (``Net-(R5-Pad2)``).
    ``fixes.rename_auto_nets`` gives the PCB nets the same names (via :func:`pin_net_names`), so
    schematic and board agree.
    """
    named = {e["subcircuit_connectivity_map_key"]: e["name"] for e in circuit if e.get("type") == "source_net"}
    traces = sorted(
        (e for e in circuit if e.get("type") == "source_trace" and _user_trace_name(e)),
        key=lambda e: str(e["name"]),
    )
    for e in traces:  # a net (no <net> name) joined by a named trace: the trace's name
        key = e.get("subcircuit_connectivity_map_key")
        if key and key not in named and not e.get("connected_source_net_ids"):
            named[key] = str(e["name"])
    comps = {e["source_component_id"]: e for e in circuit if e.get("type") == "source_component"}
    traced = {p for e in circuit if e.get("type") == "source_trace" for p in e.get("connected_source_port_ids", [])}
    by_key: dict[str, list[dict[str, Any]]] = {}
    n_ports: dict[str, int] = {}
    for e in circuit:
        if e.get("type") == "source_port" and INTERNAL_PORT not in str(e.get("name")):
            n_ports[e["source_component_id"]] = n_ports.get(e["source_component_id"], 0) + 1
        if e.get("type") == "source_port" and e.get("subcircuit_connectivity_map_key"):
            by_key.setdefault(e["subcircuit_connectivity_map_key"], []).append(e)

    def chip_pin(p: dict[str, Any]) -> bool:
        comp = comps.get(p["source_component_id"], {})
        name = str(p["name"])
        return (
            comp.get("ftype") == "simple_chip"
            and n_ports.get(p["source_component_id"], 0) > 2
            and INTERNAL_PORT not in name
            and not _GENERIC_PIN.match(name)
        )

    taken = set(named.values())
    out = {}
    for key, ports in by_key.items():
        if key not in named and not any(p["source_port_id"] in traced for p in ports):
            continue  # a pin nothing connects to
        first = ports[0]
        name = named.get(key)
        if name is None:
            pin = next((p for p in ports if chip_pin(p)), None)
            ref = comps.get(first["source_component_id"], {}).get("name", "?")
            name = f"Net-({ref}-Pad{first.get('pin_number', '?')})"
            if pin is not None:
                candidate = f"{comps[pin['source_component_id']]['name']}_{pin['name']}"
                if candidate not in taken:
                    name = candidate
            taken.add(name)
        for p in ports:
            out[p["source_port_id"]] = name
    return out


def _user_trace_name(trace: dict[str, Any]) -> bool:
    """Whether a source_trace has a name the board author gave it (not tscircuit's id)."""
    name = trace.get("name")
    return bool(name) and not str(name).startswith("source_trace")


def pin_net_names(circuit: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(reference, pin number) -> net name, as the schematic names the nets."""
    nets = _nets(circuit)
    comps = {e["source_component_id"]: str(e["name"]) for e in circuit if e.get("type") == "source_component"}
    return {
        (comps[e["source_component_id"]], str(e["pin_number"])): nets[e["source_port_id"]]
        for e in circuit
        if e.get("type") == "source_port"
        and e.get("pin_number") is not None
        and e["source_port_id"] in nets
        and e["source_component_id"] in comps
    }


def _value(comp: dict[str, Any]) -> str:
    for key in VALUE_KEYS:
        if comp.get(key):
            return str(comp[key])
    return str(comp["name"])


def _kind(comp: dict[str, Any], pins: list[Pin]) -> str:
    if len(pins) != 2:
        return "chip"
    names = {p.name.lower() for p in pins}
    if comp.get("ftype") == "simple_chip" and names & _ANODE and names & _CATHODE:
        return "D"  # imported diodes (TVS, zener) come as chips with pins A / C
    return TWO_KINDS.get(str(comp.get("ftype")), "chip")


def _parts(circuit: list[dict[str, Any]]) -> list[Part]:
    nets = _nets(circuit)
    ports: dict[str, list[dict[str, Any]]] = {}
    for e in circuit:
        # "<pin>_internal_<n>": tscircuit's copies of a pin for extra pads of it (split / exposed
        # pads, 4-pad switches); the pin itself carries the net
        if e.get("type") == "source_port" and INTERNAL_PORT not in str(e.get("name")):
            ports.setdefault(e["source_component_id"], []).append(e)
    centers = {e.get("source_component_id"): e.get("center") for e in circuit if e.get("type") == "pcb_component"}
    parts = []
    for comp in circuit:
        if comp.get("type") != "source_component" or not ports.get(comp["source_component_id"]):
            continue
        raw = sorted(ports[comp["source_component_id"]], key=lambda p: (p.get("pin_number") or 0, p["name"]))
        pins = [Pin(str(p.get("pin_number") or p["name"]), str(p["name"]), nets.get(p["source_port_id"])) for p in raw]
        kind = _kind(comp, pins)
        if kind in {"D", "LED"} and pins[0].name.lower() in _CATHODE:
            pins.reverse()  # symbol: anode left, cathode right
        lcsc = ((comp.get("supplier_part_numbers") or {}).get("jlcpcb") or [""])[0]
        center = centers.get(comp["source_component_id"])
        pos = (float(center["x"]), float(center["y"])) if center else None
        parts.append(Part(str(comp["name"]), _value(comp), kind, pins, str(lcsc), pos))
    return parts


def sections(source: Path | None) -> dict[str, str]:
    """Part name -> TSX section title (the ``{/* ===== title ===== */}`` header above it)."""
    if source is None or not source.exists():
        return {}
    out: dict[str, str] = {}
    title = ""
    text = source.read_text(encoding="utf-8")
    events = sorted(
        [(m.start(), "s", m[1]) for m in _SECTION.finditer(text)]
        + [(m.start(), "n", m[1]) for m in _NAME.finditer(text)]
    )
    for _, kind, value in events:
        if kind == "s":
            title = value
        else:
            out.setdefault(value, title)
    return out


def is_ground(net: str) -> bool:
    return bool(_GROUND.search(net))


def _negative(rails: set[str], parts: list[Part]) -> set[str]:
    """Rails that feed a chip's negative supply pin (VNEG, V_NEG, VEE, V-) or are named ``-...``."""
    fed = {p.net for part in parts if not part.two for p in part.pins if p.net and _NEGATIVE_PIN.search(p.name)}
    return {r for r in rails if not is_ground(r) and (r in fed or r.startswith("-"))}


def _rails(circuit: list[dict[str, Any]], parts: list[Part]) -> set[str]:
    """Named nets drawn as rail symbols: grounds, and supply-named nets with at least three pins
    that feed a chip's power pin (``VSET`` or ``VI_MON`` are signals, however they are named)."""
    named = {str(e["name"]) for e in circuit if e.get("type") == "source_net"}
    count: dict[str, int] = {}
    powered: set[str] = set()
    for part in parts:
        for pin in part.pins:
            if pin.net:
                count[pin.net] = count.get(pin.net, 0) + 1
                if not part.two and _POWER_PIN.match(pin.name):
                    powered.add(pin.net)
    return {n for n in named if is_ground(n) or (_SUPPLY.match(n) and n in powered and count[n] >= 3)}


# --- layout tree -------------------------------------------------------------------


@dataclass
class Label:
    net: str


@dataclass
class Rail:
    net: str


@dataclass
class Node:
    """A net drawn as wire from an attach point: its entries stack downwards on a trunk."""

    net: str
    entries: list[Label | Rail | Branch] = field(default_factory=list)
    offset: float = 0.0  # a comb (see _combs): how far out its first cap stands, past the trees below it
    comb: bool = True  # False: drawn as a stack (the comb would have to stand too far from its pin)


@dataclass
class Branch:
    """A two-terminal part from a node outwards, and what hangs on its far pin."""

    part: Part
    near: Pin
    far: Pin
    end: Node | Rail | Label | None


End = Node | Rail | Label | None


@dataclass(frozen=True)
class Frame:
    """Drawing frame of a tree: ``u`` grows outwards from a pin (``side``), ``v`` along the pin row
    (down for left / right pins, rightwards for top / bottom pins); (ox, oy) is u = v = 0."""

    ox: float
    oy: float
    side: str  # "right", "left", "up", "down": the outward direction

    @property
    def vertical(self) -> bool:
        return self.side in {"up", "down"}

    def pt(self, u: float, v: float) -> tuple[float, float]:
        if self.side == "right":
            return self.ox + u, self.oy + v
        if self.side == "left":
            return self.ox - u, self.oy + v
        if self.side == "down":
            return self.ox + v, self.oy + u
        return self.ox + v, self.oy - u


Box = tuple[float, float, float, float]  # x1, y1, x2, y2 (x1 <= x2, y1 <= y2)
RAIL_TEXT = 2 * GRID  # a rail symbol's name: this far from its pin, past the symbol


def _overlap(a: Box, b: Box, *, touch: bool = False) -> bool:
    """Whether two boxes share area (``touch``: or meet, for zero-width wires)."""
    if touch:
        return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _rail_box(net: str, x: float, y: float, direction: str) -> Box:
    """Room a rail symbol on (x, y) pointing ``direction`` takes, its name included."""
    half = len(net) * 1.2 / 2  # KiCad's font at 1.27 mm, measured
    if direction in {"up", "down"}:
        w, far = max(HALF, half), RAIL_TEXT + HALF
        return (x - w, y - far, x + w, y) if direction == "up" else (x - w, y, x + w, y + far)
    far = GRID + HALF + 2 * half
    return (x, y - HALF, x + far, y + HALF) if direction == "right" else (x - far, y - HALF, x, y + HALF)


def _body_box(sym: symlib.LibSymbol, unit: int) -> Box:
    """(xmin, ymin, xmax, ymax) of a library symbol's graphics (pins left out), library frame."""
    xs, ys = [], []
    for sub in sexpr.children(sym.node, "symbol"):
        m = re.search(r"_(\d+)_(\d+)$", str(sub[1]))
        if m and int(m[1]) not in {0, unit}:
            continue
        for item in sexpr.walk(sub):
            if sexpr.head(item) in {"xy", "start", "end", "center", "mid"} and len(item) > 2:
                xs.append(float(str(item[1])))
                ys.append(float(str(item[2])))
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)


def _part_text(part: Part) -> float:
    return _text_w(max(part.name, part.value, key=len))


def _span(part: Part, vertical: bool) -> float:
    """Length along the chain of a two-terminal part: the symbol, plus lead wires when its reference /
    value (below / above a horizontal symbol) is wider than it, so neighbouring texts do not overlap."""
    if vertical:
        return TWO_LEN
    return max(TWO_LEN, _snap(len(max(part.name, part.value, key=len)) * SMALL_CHAR_W + GRID))


def _extent(item: End | Branch, vertical: bool = False) -> tuple[float, float]:
    """(before, after) the item's row along v, in mm, on the 1.27 mm grid (rows stay on it)."""
    before, after = _raw_extent(item, vertical)
    return _snap(before, HALF), _snap(after, HALF)


def _raw_extent(item: End | Branch, vertical: bool) -> tuple[float, float]:
    if isinstance(item, Branch):
        before, after = _extent(item.end, vertical)
        if vertical:  # standing part, reference / value to its right
            return max(GRID, before), max(GRID + HALF + _part_text(item.part), after)
        return max(BRANCH_EXTENT, before), max(BRANCH_EXTENT, after)
    if isinstance(item, Node) and item.entries:
        rows = _rows(item, vertical)
        return _extent(item.entries[0], vertical)[0], rows[-1] + _extent(item.entries[-1], vertical)[1]
    if isinstance(item, Label):
        return HALF, HALF  # global label: its box is centred on the wire
    if isinstance(item, Rail) and vertical:
        half = max(HALF, _text_w(item.net) / 2 + HALF)
        return half, half
    return HALF, HALF


def _hangs(item: End, side: str) -> bool:
    """A supply pin on top of a chip with decoupling caps: drawn as a bus above it, caps hanging
    down to GND (a tree growing upwards would stand the GND symbols on their heads)."""
    return (
        side == "up"
        and isinstance(item, Node)
        and len(item.entries) > 1
        and isinstance(item.entries[0], Rail)
        and all(isinstance(e, Branch) for e in item.entries[1:])
    )


def _decoupling(item: End) -> bool:
    """A supply pin's node: its rail symbol, then the decoupling caps (see Block._attach)."""
    return (
        isinstance(item, Node)
        and len(item.entries) > 1
        and isinstance(item.entries[0], Rail)
        and all(isinstance(e, Branch) for e in item.entries[1:])
    )


def _combs(item: End, side: str) -> bool:
    """A supply pin on the left / right of a chip with decoupling caps: a wire out from the pin, the
    caps standing on it side by side down to GND (GND below, as drawn by hand), the rail at its end.
    The comb stands out past the trees of the pins below it (``Node.offset``), so it does not push
    them off their pins."""
    return side in {"left", "right"} and _decoupling(item) and isinstance(item, Node) and item.comb


COMB_MAX_OFFSET = 3 * GRID  # library symbols (fixed pin pitch): a comb standing further out is a stack
COMB_DEPTH = TWO_LEN + 2 * GRID + 2 * HALF  # below the pin row: a standing cap and its GND symbol + name


def _comb_offsets(node: Node, side: str) -> tuple[list[float], float]:
    """u of each cap of a comb from its start (offset included), and the u of the rail at its end.
    A standing cap's reference / value sit to its right: towards the chip on the left side."""
    x = node.offset + STUB
    offsets = []
    for entry in node.entries[1:]:
        assert isinstance(entry, Branch)
        text = GRID + HALF + _part_text(entry.part)
        if side == "left":
            x = _snap(x + text, HALF)
            offsets.append(x)
            x += GRID
        else:
            offsets.append(_snap(x, HALF))
            x = offsets[-1] + text + GRID
    return offsets, _snap(x, HALF)


def _comb_offset(
    start: float, v: float, below: list[tuple[Any, End, float, float]], side: str, u_tree: float, gn: set[str]
) -> float:
    """``Node.offset`` of a comb starting at ``start`` on row ``v``: its caps stand past the trees of
    the slots ``below`` it (``(pins, attachment, pin row, drawn row)``, trees starting at ``u_tree``)
    that reach into the rows the caps hang over."""
    clear = max(
        (
            (0.0 if isinstance(a, Label | Rail) else u_tree) + _slot_width(a, side, gn)
            for _, a, _, v_s in below
            if a is not None and v_s - _slot_extent(a, side)[0] < v + COMB_DEPTH
        ),
        default=0.0,
    )
    return max(0.0, _snap(clear + GRID - start - STUB, HALF))


def _hang_offsets(node: Node) -> tuple[list[float], float, float]:
    """v offsets of the hanging caps right of the pin, and (before, after) of the whole drawing."""
    before, x = _extent(node.entries[0], True)
    offsets = []
    for entry in node.entries[1:]:
        assert isinstance(entry, Branch)
        x = _snap(x + GRID, HALF)
        offsets.append(x)
        x += GRID + HALF + _part_text(entry.part)
    return offsets, before, _snap(x, HALF)


def _slot_extent(item: End, side: str) -> tuple[float, float]:
    if _combs(item, side):
        return HALF, HALF  # the caps hang outside the trees below (Node.offset)
    if isinstance(item, Node) and _hangs(item, side):
        _, before, after = _hang_offsets(item)
        return before, after
    return _extent(item, side in {"up", "down"})


def _slot_width(item: End, side: str, global_nets: set[str]) -> float:
    """Reach of a pin's attachment outwards from the pin."""
    if isinstance(item, Node) and _hangs(item, side):
        return STUB + TWO_LEN + 2 * GRID + HALF + GRID
    if isinstance(item, Node) and _combs(item, side):
        return _comb_offsets(item, side)[1] + _width(item.entries[0], global_nets)
    return _width(item, global_nets, side in {"up", "down"})


def _rows(node: Node, vertical: bool = False) -> list[float]:
    """Row offset of each entry of ``node`` after its first one, along v."""
    rows, y = [], 0.0
    for i, entry in enumerate(node.entries):
        if i:
            y += _extent(node.entries[i - 1], vertical)[1] + _extent(entry, vertical)[0]
        rows.append(y)
    return rows


def _width(item: End | Branch, global_nets: set[str], vertical: bool = False) -> float:
    """Reach of the item along u."""
    if isinstance(item, Branch):
        return STUB + _span(item.part, vertical) + _width(item.end, global_nets, vertical)
    if isinstance(item, Node):
        return max((_width(e, global_nets, vertical) for e in item.entries), default=0.0)
    if isinstance(item, Label):
        return STUB + _text_w(item.net) + (4 * HALF if item.net in global_nets else 3 * HALF)
    if isinstance(item, Rail):
        return 2 * GRID + HALF if vertical else 2 * GRID + _text_w(item.net)
    return 0.0


# --- drawing -----------------------------------------------------------------------

_LABEL_ANGLE = {"right": (0, "left"), "left": (180, "right"), "up": (90, "left"), "down": (270, "right")}
_INWARD = {"right": "left", "left": "right", "up": "down", "down": "up"}
_TWO_ANGLE = {"right": 0, "left": 180, "down": 270, "up": 90}  # the symbol's pin 1 nearest the start


class Sheet:
    """Collects the schematic items (as S-expression text) with deterministic UUIDs."""

    def __init__(self, stem: str, global_nets: set[str], negative: set[str]) -> None:
        self.stem, self.global_nets, self.negative = stem, global_nets, negative
        self.root = _uid(stem, "root")
        self.lib: list[str] = []
        self.items: list[str] = []
        self.points: list[tuple[float, float]] = []  # extent of the drawing
        self.boxes: list[Box] = []  # what is drawn (bodies, texts, labels): rails turn only into free room
        self.segments: list[Box] = []  # wires and frames
        self.pending: list[tuple[str, float, float, str]] = []  # rails, placed by finish()
        self.n = 0
        self.power = 0

    def uid(self) -> str:
        self.n += 1
        return _uid(self.stem, self.n)

    def wire(self, x1: float, y1: float, x2: float, y2: float) -> None:
        if (round(x1, 4), round(y1, 4)) == (round(x2, 4), round(y2, 4)):
            return
        self.items.append(
            f"(wire (pts (xy {_fmt(x1)} {_fmt(y1)}) (xy {_fmt(x2)} {_fmt(y2)}))"
            f" (stroke (width 0) (type default)) (uuid {self.uid()}))"
        )
        self.points += [(x1, y1), (x2, y2)]
        self.segments.append((x1, y1, x2, y2))

    def junction(self, x: float, y: float) -> None:
        self.items.append(f"(junction (at {_fmt(x)} {_fmt(y)}) (diameter 0) (color 0 0 0 0) (uuid {self.uid()}))")

    def label(self, net: str, x: float, y: float, side: str) -> None:
        angle, justify = _LABEL_ANGLE[side]
        # always global: a local label on the root sheet names the net "/NAME", which no longer matches
        # the PCB; the shape tells nets that leave the block (double arrow) from ones that stay in it
        shape = "bidirectional" if net in self.global_nets else "passive"
        self.items.append(
            f"(global_label {_q(net)} (shape {shape}) (at {_fmt(x)} {_fmt(y)} {angle})"
            f" (effects {_font()} (justify {justify})) (uuid {self.uid()}) (fields_autoplaced no))"
        )
        reach = _width(Label(net), self.global_nets) - STUB
        self.points.append(Frame(x, y, side).pt(reach, 0))
        self.box(*Frame(x, y, side).pt(0, -HALF), *Frame(x, y, side).pt(reach, HALF))

    def text(self, text: str, x: float, y: float, size: float) -> None:
        self.items.append(
            f"(text {_q(text)} (exclude_from_sim no) (at {_fmt(x)} {_fmt(y)} 0)"
            f" (effects (font (size {_fmt(size)} {_fmt(size)}) (thickness 0.4) bold) (justify left bottom))"
            f" (uuid {self.uid()}))"
        )
        self.box(x, y - size, x + len(text) * size * 0.9, y)

    def frame(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.items.append(
            f"(rectangle (start {_fmt(x1)} {_fmt(y1)}) (end {_fmt(x2)} {_fmt(y2)})"
            f" (stroke (width 0.254) (type dash)) (fill (type none)) (uuid {self.uid()}))"
        )
        self.points += [(x1, y1), (x2, y2)]
        self.segments += [(x1, y1, x2, y1), (x2, y1, x2, y2), (x1, y2, x2, y2), (x1, y1, x1, y2)]

    def box(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.boxes.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))

    def symbol(
        self,
        lib_id: str,
        ref: str,
        value: str,
        pins: list[str],
        x: float,
        y: float,
        angle: int,
        fields: tuple[tuple[float, float, str], tuple[float, float, str]],
        *,
        size: float = FONT,
        power: bool = False,
        unit: int = 1,
    ) -> None:
        """Place a symbol; ``fields`` = (x, y, justify) of Reference and Value."""
        (rx, ry, rj), (vx, vy, vj) = fields

        text_angle = angle if angle in {90, 270} else 0  # KiCad turns field texts with the symbol

        def prop(name: str, text: str, px: float, py: float, justify: str, *, hide: bool = False) -> str:
            j = f" (justify {justify})" if justify else ""
            h = " (hide yes)" if hide else ""
            return (
                f"(property {_q(name)} {_q(text)} (at {_fmt(px)} {_fmt(py)} {text_angle})"
                f" (effects (font (size {_fmt(size)} {_fmt(size)})){j}){h})"
            )

        flag = "no" if power else "yes"
        self.items.append(
            f"(symbol (lib_id {_q(lib_id)}) (at {_fmt(x)} {_fmt(y)} {angle}) (unit {unit}) (exclude_from_sim no)"
            f" (in_bom {flag}) (on_board {flag}) (dnp no)"
            f" (uuid {_uid(self.stem, 'sym', ref) if unit == 1 else _uid(self.stem, 'sym', ref, unit)})"
            " (fields_autoplaced no)"
            f" {prop('Reference', ref, rx, ry, rj, hide=power)} {prop('Value', value, vx, vy, vj, hide=value == ref)}"
            f" {prop('Footprint', '', x, y, '', hide=True)} {prop('Datasheet', '~', x, y, '', hide=True)}"
            f" {' '.join(f'(pin {_q(n)} (uuid {_uid(self.stem, ref, "pin", n)}))' for n in pins)}"
            f' (instances (project {_q(self.stem)} (path "/{self.root}" (reference {_q(ref)}) (unit {unit})))))'
        )

    def rail(self, net: str, x: float, y: float, direction: str) -> None:
        """Rail symbol on (x, y) pointing ``direction`` (right / left / up / down) away from it;
        :meth:`finish` may turn it (GND down, supplies up) where that room is free."""
        self.pending.append((net, x, y, direction))
        box = _rail_box(net, x, y, direction)
        self.points += [box[:2], box[2:]]

    def finish(self) -> None:
        """Place the rails. One whose wire arrives sideways (or from below / above) points its
        conventional way instead - GND and negative supplies down, positive supplies up - where
        nothing is drawn in that room; an upside-down one at least turns sideways."""
        for net, x, y, direction in self.pending:
            down = is_ground(net) or net in self.negative
            want, back = ("down" if down else "up"), _INWARD[direction]
            tries = [want] if want != back else ["right", "left"]
            pick = next((d for d in tries if d != direction and self.free(_rail_box(net, x, y, d), x, y, d)), direction)
            self.place_rail(net, x, y, pick)
            self.box(*_rail_box(net, x, y, pick))
            box = _rail_box(net, x, y, pick)
            self.points += [box[:2], box[2:]]
        self.pending = []

    def free(self, box: Box, x: float, y: float, direction: str) -> bool:
        """Nothing drawn in ``box`` (a rail on (x, y) pointing ``direction``), but the wires ending on
        (x, y) from other directions."""
        grown = (box[0] - 0.3, box[1] - 0.3, box[2] + 0.3, box[3] + 0.3)
        if any(_overlap(grown, b) for b in self.boxes):
            return False
        for x1, y1, x2, y2 in self.segments:
            seg = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            ends = {(round(x1, 3), round(y1, 3)), (round(x2, 3), round(y2, 3))}
            if (round(x, 3), round(y, 3)) in ends:
                ox, oy = (x2, y2) if (round(x1, 3), round(y1, 3)) == (round(x, 3), round(y, 3)) else (x1, y1)
                along = {"right": ox > x, "left": ox < x, "down": oy > y, "up": oy < y}[direction]
                if not along:
                    continue  # its own wire, arriving from another side
            if _overlap(grown, seg, touch=True):
                return False
        return True

    def place_rail(self, net: str, x: float, y: float, direction: str) -> None:
        down = is_ground(net) or net in self.negative
        lib_id = RAIL_UP if not down else RAIL_DOWN if is_ground(net) else RAIL_NEG
        # rail_down / rail_neg draw downwards, rail_up upwards at angle 0 (angles counter-clockwise)
        angles = (
            {"right": 90, "left": 270, "up": 180, "down": 0}
            if down
            else {"right": 270, "left": 90, "up": 0, "down": 180}
        )
        far, side = RAIL_TEXT, GRID + HALF + _text_w(net) / 2  # value centred: KiCad turns justify too
        vx, vy, vj = {
            "right": (x + side, y, ""),
            "left": (x - side, y, ""),
            "up": (x, y - far, ""),
            "down": (x, y + far, ""),
        }[direction]
        self.power += 1
        ref = f"#PWR{self.power:03d}"
        self.symbol(lib_id, ref, net, ["1"], x, y, angles[direction], ((x, y, ""), (vx, vy, vj)), power=True)

    def two(self, part: Part, near: Pin, f: Frame, u: float, v: float) -> None:
        """Two-terminal part from (u, v) outwards along u, its ``near`` pin at (u, v)."""
        self.lib.append(_two_lib(part))
        span = _span(part, f.vertical)
        if span > TWO_LEN:  # lead wires either side, the symbol centred
            lead = _snap((span - TWO_LEN) / 2, HALF)
            self.wire(*f.pt(u, v), *f.pt(u + lead, v))
            self.wire(*f.pt(u + lead + TWO_LEN, v), *f.pt(u + span, v))
            u += lead
        cx, cy = f.pt(u + TWO_LEN / 2, v)
        angle = (_TWO_ANGLE[f.side] + (0 if near is part.pins[0] else 180)) % 360
        if f.vertical:  # reference / value to the right, centred (KiCad turns justify with the symbol)
            tx = cx + GRID + _part_text(part) / 2
            fields = ((tx, cy - HALF, ""), (tx, cy + HALF, ""))
            self.points += [(cx - GRID, cy), (tx + _part_text(part) / 2, cy)]
            self.box(cx - 1.6, cy - TWO_LEN / 2, tx + _part_text(part) / 2, cy + TWO_LEN / 2)
        else:
            fields = ((cx, cy - TWO_TEXT, ""), (cx, cy + TWO_TEXT, ""))
            half = max(TWO_LEN, len(max(part.name, part.value, key=len)) * SMALL_CHAR_W) / 2
            self.points += [(cx - half, cy - TWO_TEXT - HALF), (cx + half, cy + TWO_TEXT + HALF)]
            self.box(cx - half, cy - TWO_TEXT - 0.6, cx + half, cy + TWO_TEXT + 0.6)
        self.symbol(f"vibepcb:{part.name}", part.name, part.value, [p.number for p in part.pins], cx, cy, angle,
                    fields, size=SMALL)  # fmt: skip

    def standing(self, part: Part, x: float, y: float) -> None:
        """Vertical two-terminal part centred on (x, y), rails above / below."""
        self.lib.append(_two_lib(part))
        top, bottom = part.pins
        if self.rank(top.net) < self.rank(bottom.net):
            top, bottom = bottom, top
        angle = 270 if top is part.pins[0] else 90  # 90: the symbol's left pin at the bottom
        name_x = x + GRID + _text_w(max(part.name, part.value, key=len)) / 2  # centred: KiCad turns justify too
        fields = ((name_x, y - HALF, ""), (name_x, y + HALF, ""))
        self.symbol(f"vibepcb:{part.name}", part.name, part.value, [p.number for p in part.pins], x, y, angle,
                    fields, size=SMALL)  # fmt: skip
        for pin, py, direction in ((top, y - TWO_LEN / 2, "up"), (bottom, y + TWO_LEN / 2, "down")):
            if pin.net:
                self.rail(pin.net, x, py, direction)
        self.points.append((name_x + _text_w(max(part.name, part.value, key=len)) / 2, y))
        self.box(x - 1.6, y - TWO_LEN / 2, name_x + _text_w(max(part.name, part.value, key=len)) / 2, y + TWO_LEN / 2)

    def rank(self, net: str | None) -> int:
        """Vertical order of rails: negative supplies below ground below positive ones."""
        if net is None or is_ground(net):
            return 1
        return 0 if net in self.negative else 2

    # trees

    def hanging(self, node: Node, f: Frame, u: float, v: float) -> None:
        """See :func:`_hangs`: pin up to a bus carrying the rail symbol, caps hanging from the bus."""
        top = u + STUB + TWO_LEN + 2 * GRID + HALF
        assert isinstance(node.entries[0], Rail)
        self.wire(*f.pt(u, v), *f.pt(top, v))
        self.rail(node.entries[0].net, *f.pt(top, v), "up")
        offsets, _, _ = _hang_offsets(node)
        down = Frame(*f.pt(top, 0), "down")
        prev = v
        for entry, offset in zip(node.entries[1:], offsets, strict=True):
            assert isinstance(entry, Branch)
            self.junction(*f.pt(top, prev))
            self.wire(*f.pt(top, prev), *f.pt(top, v + offset))
            prev = v + offset
            self.two(entry.part, entry.near, down, 0, v + offset)
            self.end(entry.end, down, TWO_LEN, v + offset)

    def comb(self, node: Node, f: Frame, u: float, v: float) -> None:
        """See :func:`_combs`."""
        offsets, end = _comb_offsets(node, f.side)
        prev = u
        for entry, offset in zip(node.entries[1:], offsets, strict=True):
            assert isinstance(entry, Branch)
            self.wire(*f.pt(prev, v), *f.pt(u + offset, v))
            self.junction(*f.pt(u + offset, v))
            prev = u + offset
            down = Frame(*f.pt(u + offset, v), "down")
            self.two(entry.part, entry.near, down, 0, 0)
            self.end(entry.end, down, TWO_LEN, 0)
        self.wire(*f.pt(prev, v), *f.pt(u + end, v))
        assert isinstance(node.entries[0], Rail)
        self.rail(node.entries[0].net, *f.pt(u + end, v), f.side)

    def node(self, node: Node, f: Frame, u: float = 0.0, v: float = 0.0) -> None:
        if _combs(node, f.side):
            self.comb(node, f, u, v)
            return
        if _hangs(node, f.side):
            self.hanging(node, f, u, v)
            return
        rows = _rows(node, f.vertical)
        for i, (entry, row) in enumerate(zip(node.entries, rows, strict=True)):
            vi = v + row
            if i:
                self.wire(*f.pt(u, v + rows[i - 1]), *f.pt(u, vi))
            if i < len(rows) - 1:  # trunk on + entry (+ trunk back or the incoming wire)
                self.junction(*f.pt(u, vi))
            self.wire(*f.pt(u, vi), *f.pt(u + STUB, vi))
            if isinstance(entry, Label):
                self.label(entry.net, *f.pt(u + STUB, vi), f.side)
            elif isinstance(entry, Rail):
                self.rail(entry.net, *f.pt(u + STUB, vi), f.side)
            else:
                self.two(entry.part, entry.near, f, u + STUB, vi)
                self.end(entry.end, f, u + STUB + _span(entry.part, f.vertical), vi)

    def end(self, end: End, f: Frame, u: float = 0.0, v: float = 0.0) -> None:
        if isinstance(end, Node):
            self.node(end, f, u, v)
        elif isinstance(end, Rail):
            self.rail(end.net, *f.pt(u, v), f.side)
        elif isinstance(end, Label):
            self.label(end.net, *f.pt(u, v), f.side)


# --- library symbols ---------------------------------------------------------------


def _lib_head(lib_id: str, *, hide_pins: bool) -> str:
    pins = " (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes))" if hide_pins else " (pin_names (offset 0.508))"
    return (
        f"(symbol {_q(lib_id)}{pins} (exclude_from_sim no) (in_bom yes) (on_board yes)"
        f' (property "Reference" "U" (at 0 0 0) (effects {_font()}))'
        f' (property "Value" "" (at 0 0 0) (effects {_font()}))'
        f' (property "Footprint" "" (at 0 0 0) (effects {_font()} (hide yes)))'
        f' (property "Datasheet" "~" (at 0 0 0) (effects {_font()} (hide yes)))'
    )


def _poly(*pts: tuple[float, float], width: float = 0.254, fill: str = "none") -> str:
    xy = " ".join(f"(xy {_fmt(x)} {_fmt(y)})" for x, y in pts)
    return f"(polyline (pts {xy}) (stroke (width {_fmt(width)}) (type default)) (fill (type {fill})))"


_TWO_BODY = {
    # graphics (horizontal, pins at x = -3.81 / +3.81) and the pins' length
    "R": ("(rectangle (start -2.54 1.016) (end 2.54 -1.016) (stroke (width 0.254) (type default)) (fill (type none)))", 1.27),
    "C": (_poly((-0.508, 1.524), (-0.508, -1.524), width=0.381) + _poly((0.508, 1.524), (0.508, -1.524), width=0.381), 3.302),
    "L": (
        " ".join(
            f"(arc (start {_fmt(x)} 0) (mid {_fmt(x + 0.635)} 0.635) (end {_fmt(x + 1.27)} 0)"
            " (stroke (width 0.254) (type default)) (fill (type none)))"
            for x in (-2.54, -1.27, 0, 1.27)
        ),
        1.27,
    ),
    "D": (_poly((-1.27, 1.27), (-1.27, -1.27), (1.27, 0), (-1.27, 1.27)) + _poly((1.27, 1.27), (1.27, -1.27)), 2.54),
    "LED": (
        _poly((-1.27, 1.27), (-1.27, -1.27), (1.27, 0), (-1.27, 1.27)) + _poly((1.27, 1.27), (1.27, -1.27))
        + _poly((-0.508, 1.778), (0.508, 2.794)) + _poly((0.508, 1.778), (1.524, 2.794)),
        2.54,
    ),
    "Y": (
        "(rectangle (start -0.762 1.778) (end 0.762 -1.778) (stroke (width 0.254) (type default)) (fill (type none)))"
        + _poly((-1.524, 1.27), (-1.524, -1.27)) + _poly((1.524, 1.27), (1.524, -1.27)),
        2.286,
    ),
    "SW": (
        "(circle (center -2.032 0) (radius 0.508) (stroke (width 0.254) (type default)) (fill (type none)))"
        " (circle (center 2.032 0) (radius 0.508) (stroke (width 0.254) (type default)) (fill (type none)))"
        + _poly((-1.524, 0.508), (2.286, 1.778)),
        1.27,
    ),
}  # fmt: skip


def _two_lib(part: Part) -> str:
    lib_id = f"vibepcb:{part.name}"
    body, length = _TWO_BODY[part.kind]
    pins = " ".join(
        f"(pin passive line (at {_fmt(x)} 0 {angle}) (length {_fmt(length)})"
        f" (name {_q(p.name)} (effects {_font()})) (number {_q(p.number)} (effects {_font()})))"
        for p, x, angle in ((part.pins[0], -TWO_LEN / 2, 0), (part.pins[1], TWO_LEN / 2, 180))
    )
    return (
        f"{_lib_head(lib_id, hide_pins=True)} (symbol {_q(part.name + '_0_1')} {body})"
        f" (symbol {_q(part.name + '_1_1')} {pins}))"
    )


RAIL_LIBS = (
    f'{_lib_head(RAIL_DOWN, hide_pins=True)} (symbol "rail_down_0_1" '
    + _poly((0, 0), (0, -1.27))
    + _poly((-1.27, -1.27), (1.27, -1.27), (0, -2.54), (-1.27, -1.27))
    + ') (symbol "rail_down_1_1" (pin passive line (at 0 0 270) (length 0)'
    f' (name "~" (effects {_font()})) (number "1" (effects {_font()})))))',
    f'{_lib_head(RAIL_UP, hide_pins=True)} (symbol "rail_up_0_1" '
    + _poly((0, 0), (0, 1.27))
    + _poly((-1.27, 1.27), (1.27, 1.27))
    + _poly((-0.762, 1.778), (0, 2.54), (0.762, 1.778))
    + ') (symbol "rail_up_1_1" (pin passive line (at 0 0 90) (length 0)'
    f' (name "~" (effects {_font()})) (number "1" (effects {_font()})))))',
    f'{_lib_head(RAIL_NEG, hide_pins=True)} (symbol "rail_neg_0_1" '
    + _poly((0, 0), (0, -1.27))
    + _poly((-1.27, -1.27), (1.27, -1.27))
    + _poly((-0.762, -1.778), (0, -2.54), (0.762, -1.778))
    + ') (symbol "rail_neg_1_1" (pin passive line (at 0 0 270) (length 0)'
    f' (name "~" (effects {_font()})) (number "1" (effects {_font()})))))',
)


# --- blocks ------------------------------------------------------------------------


@dataclass
class Cell:
    """A placeable piece of a block: ``draw(sheet, x, y)`` with (x, y) = its top-left corner."""

    width: float
    height: float
    draw: Any


_OUTWARD = {0: "left", 180: "right", 90: "down", 270: "up"}  # library pin angle -> side it leaves on
_STRENGTH = {Node: 3, Rail: 2, Label: 1}


def _strongest(attachments: list[End]) -> End:
    return max(attachments, key=lambda a: _STRENGTH.get(type(a), 0))


class Block:
    def __init__(
        self,
        title: str,
        parts: list[Part],
        rails: set[str],
        global_nets: set[str],
        symbols: dict[str, symlib.LibSymbol] | None = None,
    ) -> None:
        self.title, self.parts, self.rails, self.global_nets = title, parts, rails, global_nets
        self.symbols = symbols or {}
        self.ctx: str | None = None  # chip whose trees are being built
        self.wired: set[str] = set()
        self.owner: dict[str, str] = {}
        self.placed: set[str] = set()
        self.on_net: dict[str, list[tuple[Part, Pin]]] = {}
        for part in parts:
            if part.two:
                for pin in part.pins:
                    if pin.net:
                        self.on_net.setdefault(pin.net, []).append((part, pin))
        self.cells = self._cells()

    def _owners(self, chips: list[Part]) -> dict[str, str]:
        """Two-terminal part -> the chip it serves: of the chips it connects to (directly, else
        through one more passive, else by supply rail), the one it sits next to on the PCB."""
        twos = [p for p in self.parts if p.two]
        signal = {c.name: {p.net for p in c.pins if p.net and p.net not in self.rails} for c in chips}
        supply = {c.name: {p.net for p in c.pins if p.net in self.rails and not is_ground(p.net)} for c in chips}
        nets_of = {p.name: {q.net for q in p.pins if q.net and q.net not in self.rails} for p in twos}
        owners = {}
        for part in twos:
            nets = nets_of[part.name]
            cands = [c for c in chips if signal[c.name] & nets]
            if not cands and nets:
                hop = set(nets).union(*(nets_of[q.name] for q in twos if nets_of[q.name] & nets))
                cands = [c for c in chips if signal[c.name] & hop]
            if not cands:
                rails = {q.net for q in part.pins if q.net in self.rails and not is_ground(q.net)}
                cands = [c for c in chips if supply[c.name] & rails]
            if not cands:
                continue
            if part.pos is not None and all(c.pos is not None for c in cands):
                px, py = part.pos
                owners[part.name] = min(cands, key=lambda c: math.dist((px, py), c.pos or (0, 0))).name
            else:
                owners[part.name] = cands[0].name
        # passives joined by a net no chip pin is on (LED + resistor, RC between rails) are drawn as one
        # piece: they all take the owner most of them have, else one owner would cut the chain into labels
        chip_nets = set().union(*signal.values()) if signal else set()
        group = {p.name: p.name for p in twos}

        def find(a: str) -> str:
            while group[a] != a:
                a = group[a]
            return a

        by_net: dict[str, str] = {}
        for part in twos:
            for net in sorted(nets_of[part.name] - chip_nets):
                if net in by_net:
                    group[find(part.name)] = find(by_net[net])
                else:
                    by_net[net] = part.name
        members: dict[str, list[str]] = {}
        for part in twos:
            members.setdefault(find(part.name), []).append(part.name)
        for names in members.values():
            votes = [owners[n] for n in names if n in owners]
            if len(names) > 1 and votes:
                pick = max(dict.fromkeys(votes), key=votes.count)
                owners.update(dict.fromkeys(names, pick))
        return owners

    def _build(self, net: str) -> Node:
        node = Node(net)
        self.wired.add(net)
        for part, pin in self.on_net.get(net, []):
            if part.name in self.placed or self.owner.get(part.name, self.ctx) != self.ctx:
                continue  # another chip's part: that chip draws it
            self.placed.add(part.name)
            far = part.pins[1] if pin is part.pins[0] else part.pins[0]
            end: End
            if far.net is None:
                end = None
            elif far.net in self.rails:
                end = Rail(far.net)
            elif far.net in self.wired:
                end = Label(far.net)
            else:
                end = self._build(far.net)
            node.entries.append(Branch(part, pin, far, end))
        return node

    def _attach(self, chip: Part, pin: Pin, decouple: dict[tuple[str, str], list[Part]]) -> End:
        if pin.net is None:
            return None
        if pin.net in self.rails:
            caps = decouple.pop((chip.name, pin.net), [])
            if not caps:
                return Rail(pin.net)
            # supply pin with its decoupling caps: pin -+- rail symbol, each cap below it to its other rail
            node = Node(pin.net, [Rail(pin.net)])
            for cap in caps:
                near, far = cap.pins if cap.pins[0].net == pin.net else cap.pins[::-1]
                node.entries.append(Branch(cap, near, far, Rail(far.net) if far.net else None))
            return node
        if pin.net in self.wired:
            return Label(pin.net)
        return self._build(pin.net)

    def _cells(self) -> list[Cell]:
        chips = [p for p in self.parts if not p.two]
        self.owner = self._owners(chips)
        by_name = {c.name: c for c in chips}
        # rail-to-rail parts (decoupling caps) go on a supply pin of the chip they serve
        decouple: dict[tuple[str, str], list[Part]] = {}
        for part in self.parts:
            if not part.two or not all(p.net in self.rails for p in part.pins):
                continue
            nets = [p.net for p in part.pins if p.net and not is_ground(p.net)] or [p.net for p in part.pins if p.net]
            owner = by_name.get(self.owner.get(part.name, ""))
            order = [owner, *chips] if owner else chips
            pick = next(((c, p.net) for c in order for p in c.pins if p.net in nets and _POWER_PIN.match(p.name)), None)
            pick = pick or next(((c, p.net) for c in order for p in c.pins if p.net in nets), None)
            if pick is not None and pick[1] is not None:
                decouple.setdefault((pick[0].name, pick[1]), []).append(part)
                self.placed.add(part.name)
        attached = []
        for chip in chips:
            self.ctx = chip.name
            attached.append([self._attach(chip, pin, decouple) for pin in chip.pins])
        roots: list[tuple[str | None, Node]] = []
        while True:  # two-terminal parts no chip reached: trees from a labelled net, kept by their chip
            left = [p for p in self.parts if p.two and p.name not in self.placed]
            seeds = [p for p in left if any(q.net and q.net not in self.rails for q in p.pins)]
            if not seeds:
                break
            self.ctx = self.owner.get(seeds[0].name)
            counts: dict[str, int] = {}
            for part in seeds:
                if self.owner.get(part.name) == self.ctx:
                    for pin in part.pins:
                        if pin.net and pin.net not in self.rails:
                            counts[pin.net] = counts.get(pin.net, 0) + 1
            net = max(counts, key=lambda n: (counts[n], n not in self.wired))
            root = self._build(net)
            root.entries.insert(0, Label(net))
            roots.append((self.ctx, root))
        bank = [p for p in self.parts if p.two and p.name not in self.placed]
        self._label_nodes([*(a for row in attached for a in row), *(r for _, r in roots)])
        cells = []
        for chip, row in zip(chips, attached, strict=True):
            own: list[Cell] = []
            satellites: list[Node] = [r for owner, r in roots if owner == chip.name]
            if chip.name in self.symbols:
                sym = self.symbols[chip.name]
                for unit in sorted(sym.units()) or [1]:  # each unit of a dual op-amp its own triangle
                    own.append(self._lib_cell(chip, row, sym, satellites, unit))
            elif len(chip.pins) == 1:
                own.append(self._point_cell(chip, row[0]))
            else:
                own.append(self._chip_cell(chip, row))
            extra = [self._root_cell(r) for r in satellites]
            extra += [self._bank_cell(p) for p in bank if self.owner.get(p.name) == chip.name]
            cells.append(_group(own, extra))  # the chip with everything it keeps, as one piece
        cells += [self._root_cell(r) for owner, r in roots if owner not in {c.name for c in chips}]
        cells += [self._bank_cell(p) for p in bank if self.owner.get(p.name) not in {c.name for c in chips}]
        return cells

    def _label_nodes(self, tops: list[End]) -> None:
        """Label every wired net: KiCad names an unlabelled one after a pin of its own choice,
        which no longer matches the PCB's net name (parity, "Update PCB from Schematic").
        The label goes below the node's parts, so a chain pin - R - node - C runs straight on."""

        def visit(item: End | Branch) -> None:
            if isinstance(item, Branch):
                visit(item.end)
            elif isinstance(item, Node):
                if item.net not in self.rails and not any(isinstance(e, Label) for e in item.entries):
                    item.entries.append(Label(item.net))
                for e in item.entries:
                    visit(e)

        for top in tops:
            visit(top)

    def _pin_rank(self, pin: Pin) -> tuple[int, str]:
        """Side order: supplies on top, then signals (pin order), unconnected pins, grounds last (room
        below them for the GND symbol pointing down)."""
        if pin.net is None:
            return 2, ""
        if pin.net in self.rails:
            return (3 if is_ground(pin.net) else 0), pin.net
        return 1, ""

    def _point_cell(self, chip: Part, attached: End) -> Cell:
        """A one-pin part (test point, antenna, touch pad): a small circle on its pin, not a box."""
        lib_id = f"vibepcb:{chip.name}"
        pin = chip.pins[0]
        r = 0.762
        lib = (
            f"{_lib_head(lib_id, hide_pins=True)} (symbol {_q(chip.name + '_0_1')} (circle (center {_fmt(r)} 0)"
            f" (radius {_fmt(r)}) (stroke (width 0.254) (type default)) (fill (type none))))"
            f" (symbol {_q(chip.name + '_1_1')} (pin passive line (at {_fmt(-PIN_LENGTH)} 0 0)"
            f" (length {_fmt(PIN_LENGTH)}) (name {_q(pin.name)} (effects {_font()}))"
            f" (number {_q(pin.number)} (effects {_font()})))))"
        )
        above, below = _extent(attached) if attached is not None else (0.0, 0.0)
        above, below = max(above, 2 * GRID), max(below, GRID)
        left = _snap(_width(attached, self.global_nets))
        text = max(_text_w(chip.name), _text_w(chip.value if chip.value != chip.name else ""))
        width = left + PIN_LENGTH + 2 * r + GRID + text

        def draw(sheet: Sheet, x: float, y: float) -> None:
            px, py = x + left, y + _snap(above, HALF)
            sheet.lib.append(lib)
            tx = px + PIN_LENGTH + 2 * r + HALF
            sheet.symbol(lib_id, chip.name, chip.value, [pin.number], px + PIN_LENGTH, py, 0,
                         ((tx, py - HALF, "left"), (tx, py + HALF, "left")))  # fmt: skip
            sheet.points += [(x, py - above), (tx + text, py + below)]
            sheet.box(px + PIN_LENGTH, py - HALF, tx + text, py + HALF)
            sheet.segments.append((px, py, px + PIN_LENGTH, py))
            sheet.end(attached, Frame(px, py, "left"))

        return Cell(_snap(width), _snap(above + below), draw)

    def _chip_cell(self, chip: Part, attached: list[End]) -> Cell:
        pairs = sorted(zip(chip.pins, attached, strict=True), key=lambda pa: self._pin_rank(pa[0]))
        # runs of pins on one net (S1-S3, AVDD1..6, GND pins): joined by a wire, attached once at the last
        slots: list[list[tuple[Pin, End]]] = []
        for pin, a in pairs:
            if slots and pin.net is not None and slots[-1][-1][0].net == pin.net:
                slots[-1].append((pin, a))
            else:
                slots.append([(pin, a)])
        strength = {Node: 3, Rail: 2, Label: 1}

        def attach(slot: list[tuple[Pin, End]]) -> End:
            return max((a for _, a in slot), key=lambda a: strength.get(type(a), 0))

        def extent(slot: list[tuple[Pin, End]]) -> tuple[float, float]:
            above, below = _slot_extent(attach(slot), "left")
            if _combs(attach(slot), "left"):  # box pins are spaced freely: the next pin goes below the caps
                below = COMB_DEPTH + HALF
            return max(above, (len(slot) - 1) * GRID + HALF), below

        heights = [sum(extent(slot)) for slot in slots]
        total, acc, split = sum(heights), 0.0, len(slots)
        for i, h in enumerate(heights):
            if acc and acc + h / 2 > total / 2:
                split = i
                break
            acc += h
        sides = [slots[:split], slots[split:]]
        longest = max(len(p.name) for p in chip.pins)
        half_w = _snap(max(2 * GRID, longest * CHAR_W + GRID))
        pin_y: dict[str, float] = {}
        side_h = []
        for side in sides:
            y = GRID
            for i, slot in enumerate(side):
                y += extent(slot)[0] if i == 0 else extent(side[i - 1])[1] + extent(slot)[0]
                for k, (pin, _) in enumerate(slot):
                    pin_y[pin.number] = y - (len(slot) - 1 - k) * GRID
            side_h.append((y + extent(side[-1])[1] if side else 0) + GRID)
        height = _snap(max(side_h))
        for s, side in enumerate(sides):
            name = "left" if s == 0 else "right"
            rows: list[tuple[Any, End, float, float]] = [
                ([], attach(slot), 0.0, pin_y[slot[-1][0].number]) for slot in side
            ]
            for i, (_, a, _, v) in enumerate(rows):
                if isinstance(a, Node) and _combs(a, name):
                    a.offset = _comb_offset(0.0, v, rows[i + 1 :], name, 0.0, self.global_nets)
        widths = [
            max(
                (_slot_width(attach(slot), "left" if s == 0 else "right", self.global_nets) for slot in side),
                default=0.0,
            )
            for s, side in enumerate(sides)
        ]
        lib_id = f"vibepcb:{chip.name}"
        pin_defs = []
        for s, side in enumerate(sides):
            px = -(half_w + PIN_LENGTH) if s == 0 else half_w + PIN_LENGTH
            for slot in side:
                for pin, _ in slot:
                    pin_defs.append(
                        f"(pin passive line (at {_fmt(px)} {_fmt(-pin_y[pin.number])} {0 if s == 0 else 180})"
                        f" (length {_fmt(PIN_LENGTH)}) (name {_q(pin.name)} (effects {_font(SMALL)}))"
                        f" (number {_q(pin.number)} (effects {_font(SMALL)})))"
                    )
        lib = (
            f"{_lib_head(lib_id, hide_pins=False)} (symbol {_q(chip.name + '_0_1')} (rectangle (start {_fmt(-half_w)} 0)"
            f" (end {_fmt(half_w)} {_fmt(-height)}) (stroke (width 0.254) (type default)) (fill (type background))))"
            f" (symbol {_q(chip.name + '_1_1')} {' '.join(pin_defs)}))"
        )
        left_w = _snap(widths[0])
        cell_w = left_w + 2 * (half_w + PIN_LENGTH) + _snap(widths[1])

        def draw(sheet: Sheet, x: float, y: float) -> None:
            cx, top = x + left_w + PIN_LENGTH + half_w, y + GRID
            sheet.lib.append(lib)
            fields = ((cx, top - HALF, ""), (cx, top + height + HALF, ""))
            sheet.symbol(lib_id, chip.name, chip.value, [p.number for p in chip.pins], cx, top, 0, fields)
            sheet.points += [(x, y), (x + cell_w, top + height + GRID)]
            sheet.box(cx - half_w, top - HALF - 0.6, cx + half_w, top + height + HALF + 0.6)
            for s, side in enumerate(sides):
                for slot in side:
                    for pin, _ in slot:
                        sx = cx + (half_w if s else -half_w)
                        sheet.segments.append((sx, top + pin_y[pin.number], sx + (PIN_LENGTH if s else -PIN_LENGTH),
                                               top + pin_y[pin.number]))  # fmt: skip
            for s, side in enumerate(sides):
                d = -1 if s == 0 else 1
                px = cx + d * (half_w + PIN_LENGTH)
                for slot in side:
                    ys = [top + pin_y[pin.number] for pin, _ in slot]
                    for k in range(1, len(ys)):
                        sheet.wire(px, ys[k - 1], px, ys[k])
                        sheet.junction(px, ys[k])  # pin + wire up + (wire down or the attachment)
                    sheet.end(attach(slot), Frame(px, ys[-1], "left" if d < 0 else "right"))

        return Cell(cell_w, height + 2 * GRID, draw)

    def _lib_cell(
        self, chip: Part, attached: list[End], sym: symlib.LibSymbol, moved: list[Node], unit: int = 1
    ) -> Cell:
        """A chip drawn with its library symbol: pins keep their pitch on every side, so a pin whose
        tree needs more room than the pitch jogs out to a free row (lanes stepping inwards row by row,
        so no jog crosses another). A tree that would have to jog further than MAX_JOG goes to
        a column beside the chip on the pin's side, level with the pin (from a label facing the pin's
        label; top / bottom pins: to ``moved``, packed under the chip), and the pin gets the net label
        instead, as on any MCU schematic; a supply pin keeps its decoupling caps (see _combs)."""
        lib_id = f"vibepcb:{chip.name}"
        lib_text = sexpr.dumps(symlib.for_schematic(sym, lib_id))
        net_of = {p.number: p.net for p in chip.pins}
        attach_of = dict(zip((p.number for p in chip.pins), attached, strict=True))
        sides: dict[str, list[symlib.LibPin]] = {"left": [], "right": [], "up": [], "down": []}
        first = min(sym.units() or {1})
        unit_pins = [lp for lp in sym.pins if lp.unit == unit or (lp.unit == 0 and unit == first)]
        for lp in unit_pins:
            sides[_OUTWARD.get(lp.angle, "left")].append(lp)

        # per side: base line (outermost pin end), runs of same-net pins, slots, lanes
        plans = []
        for side, pins in sides.items():
            if not pins:
                continue
            vertical = side in {"up", "down"}

            def v_of(lp: symlib.LibPin, vertical: bool = vertical) -> float:
                return lp.x if vertical else -lp.y

            def n_of(lp: symlib.LibPin, vertical: bool = vertical) -> float:
                return -lp.y if vertical else lp.x

            pins = sorted(pins, key=lambda lp: (v_of(lp), lp.number))
            base = {"left": min, "right": max, "up": min, "down": max}[side](n_of(lp) for lp in pins)
            runs: list[list[symlib.LibPin]] = []
            for lp in pins:
                if runs and net_of[lp.number] is not None and net_of[runs[-1][-1].number] == net_of[lp.number]:
                    runs[-1].append(lp)
                else:
                    runs.append([lp])
            while True:  # a comb that would stand too far out goes back to a stack of caps (Node.comb)
                slots = []
                outer: list[tuple[float, Node]] = []  # trees moved off their pin: (pin row, tree)
                prev_end = -math.inf
                long_row = len(pins) > LONG_PIN_ROW
                connected = [v_of(run[-1]) for run in runs if any(attach_of[lp.number] is not None for lp in run)]
                for run in runs:
                    a = _strongest([attach_of[lp.number] for lp in run])
                    before, after = _slot_extent(a, side) if a is not None else (0.0, 0.0)
                    v_p = v_of(run[-1])
                    v_s = v_p if a is None or prev_end == -math.inf else max(v_p, _snap(prev_end + before, HALF))
                    next_v = min((v for v in connected if v > v_p + 1e-6), default=math.inf)
                    pushes = v_s > v_p + 1e-6 or v_p + after + HALF > next_v + 1e-6
                    if (
                        isinstance(a, Node)
                        and a.net not in self.rails
                        and (v_s - v_p > MAX_JOG or (long_row and pushes))
                    ):
                        rest = [e for e in a.entries if not (isinstance(e, Label) and e.net == a.net)]
                        if rest:
                            outer.append((v_p, Node(a.net, rest)))
                        a = Label(a.net)
                        before, after = _slot_extent(a, side)
                        v_s = v_p if prev_end == -math.inf else max(v_p, _snap(prev_end + before, HALF))
                    if a is not None:
                        prev_end = v_s + after
                    slots.append((run, a, v_p, v_s))
                lanes: dict[int, float] = {}
                group: list[int] = []
                widest = 0
                for i, (run, a, v_p, v_s) in enumerate([*slots, ([], None, 0.0, 0.0)]):
                    if a is None and run:
                        continue  # an unconnected pin draws nothing: it does not end a group of jogs
                    if a is not None and v_s > v_p + 1e-6:
                        group.append(i)
                        continue
                    for k, j in enumerate(group):
                        lanes[j] = (len(group) - k) * HALF
                    widest = max(widest, len(group))
                    group = []
                u0 = (widest + 1) * HALF if widest else 0.0
                for i, (_, a, _, v_s) in enumerate(slots):
                    if isinstance(a, Node) and _combs(a, side):
                        start = u0 if i in lanes else max(u0, STUB)
                        a.offset = _comb_offset(start, v_s, slots[i + 1 :], side, max(u0, STUB), self.global_nets)
                far = [a for _, a, _, _ in slots if isinstance(a, Node) and a.comb and _combs(a, side)
                       and a.offset > COMB_MAX_OFFSET]  # fmt: skip
                if not far:
                    if vertical:  # above / below the chip: packed under it, from a label
                        moved += [Node(tree.net, [Label(tree.net), *tree.entries]) for _, tree in outer]
                        outer = []
                    break
                for a in far:
                    a.comb, a.offset = False, 0.0
            # the moved trees: one column outside the pin attachments, each as level with its pin as the
            # trees above it allow, its trunk starting at a label that faces the pin's own label
            column: list[tuple[float, float, Node]] = []
            spans = []  # rows and reach of each pin attachment
            for _, a, _, v_s in slots:
                if a is not None:
                    before, after = _slot_extent(a, side)
                    after = COMB_DEPTH if _combs(a, side) else after
                    spans.append((v_s - before, v_s + after, u0 + _slot_width(a, side, self.global_nets)))
            end = -math.inf
            for v_p, tree in outer:
                before, after = _extent(tree, vertical)
                v = v_p if end == -math.inf else max(v_p, _snap(end + max(before, HALF) + GRID, HALF))
                lo, hi = v - max(before, HALF) - GRID, v + max(after, HALF) + GRID
                clear = max((w for a, b, w in spans if a < hi and b > lo), default=0.0)
                reach = _width(Label(tree.net), self.global_nets) - STUB
                column.append((_snap(clear + 2 * GRID + reach), v, tree))
                end = v + max(after, HALF)
            plans.append((side, vertical, base, n_of, slots, lanes, u0, column))

        def draw_at(sheet: Sheet, cx: float, cy: float) -> None:
            sheet.lib.append(lib_text)
            ref = sexpr.property_node(sym.node, "Reference")
            val = sexpr.property_node(sym.node, "Value")

            def field(prop: list[sexpr.Node] | None, dy: float) -> tuple[float, float, str]:
                at = sexpr.child(prop, "at") if prop is not None else None
                effects = sexpr.child(prop, "effects") if prop is not None else None
                justify = sexpr.child(effects, "justify") if effects is not None else None
                j = " ".join(str(x) for x in justify[1:]) if justify is not None else ""
                if at is None:
                    return cx, cy + dy, ""
                return cx + float(str(at[1])), cy - float(str(at[2])), j

            xmin, ymin, xmax, ymax = sym.bbox(unit)
            fields = (field(ref, -ymax - HALF), field(val, -ymin + HALF))
            numbers = [lp.number for lp in unit_pins]
            sheet.symbol(lib_id, chip.name, chip.value, numbers, cx, cy, 0, fields, unit=unit)
            sheet.points += [(cx + xmin, cy - ymax), (cx + xmax, cy - ymin)]
            for (fx, fy, fj), text in zip(fields, (chip.name, chip.value), strict=True):
                w = _text_w(text)  # the cell holds its reference / value (long MPNs reach past the body)
                x0 = fx if "left" in fj else fx - w if "right" in fj else fx - w / 2
                sheet.points += [(x0, fy - HALF), (x0 + w, fy + HALF)]
                sheet.box(x0, fy - HALF, x0 + w, fy + HALF)
            bx0, by0, bx1, by1 = _body_box(sym, unit)
            sheet.box(cx + bx0, cy - by1, cx + bx1, cy - by0)
            for lp in unit_pins:  # pin lines: from the end into the body
                px, py = cx + lp.x, cy - lp.y
                ix = cx + bx0 if lp.angle == 0 else cx + bx1 if lp.angle == 180 else px
                iy = cy - by0 if lp.angle == 90 else cy - by1 if lp.angle == 270 else py
                sheet.segments.append((px, py, ix, iy))
            for side, vertical, base, n_of, slots, lanes, u0, column in plans:
                f = Frame(cx, cy + base, side) if vertical else Frame(cx + base, cy, side)
                sign = -1 if side in {"left", "up"} else 1
                for i, (run, a, v_p, v_s) in enumerate(slots):
                    if a is None:
                        continue  # unconnected pin: no wire (fixes.schematic_no_connects flags it)
                    for k, lp in enumerate(run):
                        u_pin = sign * (n_of(lp) - base)  # <= 0: pin end inside the base line
                        v = lp.x if vertical else -lp.y
                        sheet.wire(*f.pt(u_pin, v), *f.pt(0, v))
                        if k:
                            prev = run[k - 1]
                            sheet.wire(*f.pt(0, prev.x if vertical else -prev.y), *f.pt(0, v))
                            sheet.junction(*f.pt(0, v))
                    if i in lanes:
                        lane = lanes[i]
                        sheet.wire(*f.pt(0, v_p), *f.pt(lane, v_p))
                        sheet.wire(*f.pt(lane, v_p), *f.pt(lane, v_s))
                        sheet.wire(*f.pt(lane, v_s), *f.pt(u0, v_s))
                        sheet.end(a, f, u0, v_s)
                    elif isinstance(a, Node):
                        # the tree's trunk runs along the pin row: keep it off the pin ends it passes
                        start = max(u0, STUB)
                        sheet.wire(*f.pt(0, v_p), *f.pt(start, v_p))
                        sheet.end(a, f, start, v_p)
                    else:
                        sheet.end(a, f, 0, v_p)
                for u_col, v, tree in column:
                    sheet.label(tree.net, *f.pt(u_col, v), _INWARD[side])
                    sheet.node(tree, f, u_col, v)

        # dry run for the cell's extent, then place it by its top-left corner
        probe = Sheet("probe", self.global_nets, set())
        draw_at(probe, 0.0, 0.0)
        x0 = _snap(min(p[0] for p in probe.points) - HALF) - GRID
        y0 = _snap(min(p[1] for p in probe.points) - HALF) - GRID
        x1 = max(p[0] for p in probe.points) + HALF
        y1 = max(p[1] for p in probe.points) + HALF

        def draw(sheet: Sheet, x: float, y: float) -> None:
            draw_at(sheet, x - x0, y - y0)

        return Cell(_snap(x1 - x0), _snap(y1 - y0), draw)

    def _root_cell(self, root: Node) -> Cell:
        rungs = [e for e in root.entries if not isinstance(e, Label)]
        ends = [e.end.net for e in rungs if isinstance(e, Branch) and isinstance(e.end, Rail)]
        if len(rungs) == 2 and len(ends) == 2 and is_ground(ends[0]) != is_ground(ends[1]):
            return self._ladder_cell(root.net, rungs)  # type: ignore[arg-type]
        above, below = _extent(root)

        def draw(sheet: Sheet, x: float, y: float) -> None:
            sheet.node(root, Frame(x, y + _snap(above, HALF), "right"))

        return Cell(_snap(_width(root, self.global_nets)), _snap(above + below, HALF), draw)

    def _ladder_cell(self, net: str, rungs: list[Branch]) -> Cell:
        """Two parts in series between two rails (LED + resistor, divider): one standing column,
        the higher rail on top, the middle node labelled to the right."""
        sheet_rank = Sheet("rank", set(), set())
        top, bottom = sorted(rungs, key=lambda b: -sheet_rank.rank(b.end.net if isinstance(b.end, Rail) else None))
        head = 3 * GRID
        text = max(_part_text(b.part) for b in rungs) + GRID + HALF
        reach = _width(Label(net), self.global_nets)
        width = _snap(GRID + max(text, reach, _text_w(top.far.net or "") / 2 + GRID))
        height = _snap(2 * head + 2 * TWO_LEN)

        def draw(sheet: Sheet, x: float, y: float) -> None:
            cx, y0 = x + GRID, y + head
            f = Frame(cx, y0, "down")
            assert isinstance(top.end, Rail)
            assert isinstance(bottom.end, Rail)
            sheet.rail(top.end.net, cx, y0, "up")
            sheet.two(top.part, top.far, f, 0, 0)
            sheet.two(bottom.part, bottom.near, f, TWO_LEN, 0)
            sheet.rail(bottom.end.net, *f.pt(2 * TWO_LEN, 0), "down")
            sheet.junction(cx, y0 + TWO_LEN)
            sheet.wire(cx, y0 + TWO_LEN, cx + STUB, y0 + TWO_LEN)
            sheet.label(net, cx + STUB, y0 + TWO_LEN, "right")

        return Cell(width, height, draw)

    def _bank_cell(self, part: Part) -> Cell:
        text = max(_text_w(part.name), _text_w(part.value), *(_text_w(p.net or "") / 2 for p in part.pins))
        height = TWO_LEN + 2 * (2 * GRID + HALF + GRID)

        def draw(sheet: Sheet, x: float, y: float) -> None:
            sheet.standing(part, x + GRID, y + height / 2)

        return Cell(_snap(2 * GRID + text), _snap(height), draw)


def _group(main: list[Cell], extra: list[Cell]) -> Cell:
    """``main`` side by side, ``extra`` packed underneath, as one cell."""
    top_spots, top_w, top_h = _pack(main, math.inf)
    if not extra:
        if len(main) == 1:
            return main[0]
        bottom_spots: list[tuple[float, float]] = []
        bottom_w = bottom_h = 0.0
    else:
        bottom_spots, bottom_w, bottom_h = _pack(extra, max(top_w, _target_width(extra, 1.0)))

    def draw(sheet: Sheet, x: float, y: float) -> None:
        for cell, (cx, cy) in zip(main, top_spots, strict=True):
            cell.draw(sheet, x + cx, y + cy)
        for cell, (cx, cy) in zip(extra, bottom_spots, strict=True):
            cell.draw(sheet, x + cx, y + top_h + CELL_GAP + cy)

    return Cell(max(top_w, bottom_w), top_h + (CELL_GAP + bottom_h if extra else 0.0), draw)


def _paper(width: float, height: float) -> int:
    """Index into PAPERS of the smallest sheet holding a drawing of this size (len(PAPERS): none)."""
    for i, (w, h) in enumerate(PAPERS):
        if width + 2 * SHEET_MARGIN <= w - 24.0 and height + 2 * SHEET_MARGIN <= h - 24.0 - 40.0:
            return i
    return len(PAPERS)


def _scale(width: float, height: float) -> float:
    """How much the drawing could grow on its paper (the PDF prints it that much larger)."""
    paper = PAPERS[min(_paper(width, height), len(PAPERS) - 1)]
    return min((paper[0] - 24.0) / max(width, 1.0), (paper[1] - 64.0) / max(height, 1.0))


def _pack(cells: list[Cell], width: float) -> tuple[list[tuple[float, float]], float, float]:
    """Skyline packing in order: each cell goes to the lowest free spot (then the leftmost) within
    ``width``; returns the top-left corner of each cell and the total width / height."""
    skyline: list[tuple[float, float, float]] = [(0.0, math.inf, 0.0)]  # (x from, x to, free from y)
    spots: list[tuple[float, float]] = []
    used_w = used_h = 0.0
    for cell in cells:
        span = cell.width + CELL_GAP
        best: tuple[float, float] | None = None
        for x0, _, _ in skyline:
            if x0 and x0 + cell.width > width:
                continue
            y = max((top for a, b, top in skyline if a < x0 + span and b > x0), default=0.0)
            if best is None or (y, x0) < best:
                best = (y, x0)
        assert best is not None
        y, x = best
        x, y = _snap(x), _snap(y)
        spots.append((x, y))
        bottom = _snap(y + cell.height + CELL_GAP)
        new: list[tuple[float, float, float]] = []
        for a, b, top in skyline:
            if b <= x or a >= x + span:
                new.append((a, b, top))
                continue
            if a < x:
                new.append((a, x, top))
            if b > x + span:
                new.append((x + span, b, top))
        new.append((x, x + span, bottom))
        skyline = sorted(new)
        used_w, used_h = max(used_w, x + cell.width), max(used_h, y + cell.height)
    return spots, used_w, used_h


def _pack_columns(cells: list[Cell], aspect: float) -> tuple[list[tuple[float, float]], float, float]:
    """Like :func:`_pack`, but filling columns top to bottom (tall blocks waste less room)."""
    flipped = [Cell(c.height, c.width, None) for c in cells]
    spots, w, h = _pack(flipped, _target_width(flipped, 1 / aspect))
    return [(y, x) for x, y in spots], h, w


def _target_width(cells: list[Cell], aspect: float) -> float:
    area = sum((c.width + CELL_GAP) * (c.height + CELL_GAP) for c in cells)
    return max(max((c.width for c in cells), default=0.0), math.sqrt(area * aspect))


_FET_PIN = re.compile(r"^([GSD])\d*$", re.IGNORECASE)


def _transistor_symbol(part: Part, library: symlib.Library, rails: set[str]) -> symlib.LibSymbol | None:
    """A MOSFET whose pins are only G / S / D (SO-8: S1-S3, G, D1-D4) as KiCad's generic MOSFET,
    the repeated pins stacked; P-channel when its source sits on a positive supply."""
    roles: dict[str, list[Pin]] = {}
    for pin in part.pins:
        m = _FET_PIN.match(pin.name)
        if m is None:
            return None
        roles.setdefault(m[1].upper(), []).append(pin)
    if set(roles) != {"G", "S", "D"} or any(len({p.net for p in pins}) > 1 for pins in roles.values()):
        return None
    source = roles["S"][0].net
    channel = "P" if source in rails and not is_ground(source) else "N"
    base = library.symbol(f"Q_{channel}MOS_GSD")
    if base is None:
        return None
    node = copy.deepcopy(base.node)
    for sub in sexpr.children(node, "symbol"):
        for lib_pin in list(sexpr.children(sub, "pin")):
            name = sexpr.child(lib_pin, "name")
            role = str(name[1]).upper() if name is not None else ""
            if role not in roles:
                continue
            at = sub.index(lib_pin)
            copies: list[sexpr.Node] = []
            for k, ours in enumerate(roles[role]):
                twin = copy.deepcopy(lib_pin)
                number = sexpr.child(twin, "number")
                if number is not None:
                    number[1] = ours.number
                if k:  # stacked under the first one
                    twin.insert(3, [sexpr.Sym("hide"), sexpr.Sym("yes")])
                copies.append(twin)
            sub[at : at + 1] = copies
    return symlib.LibSymbol(f"Q_{channel}MOS_GSD", node, symlib.pins_of(node))


def _library_symbols(
    parts: list[Part], library: symlib.Library, rails: set[str] | None = None, *, lcsc: bool = True
) -> dict[str, symlib.LibSymbol]:
    """Part name -> the symbol that fits it: KiCad's library by MPN first, then KiCad's generic
    MOSFET for G/S/D-only parts, else LCSC's (EasyEDA) by LCSC number; see :func:`symlib.fits`.
    Parts without any keep the box."""
    chosen: dict[str, symlib.LibSymbol] = {}
    chips = [p for p in parts if not p.two]
    for part in chips:
        pins = {p.number: (p.name, p.net) for p in part.pins}
        for name in symlib.candidates(part.value, library.index)[:5]:
            sym = library.symbol(name)
            if sym is not None and symlib.fits(sym, pins) is None:
                chosen[part.name] = sym
                break
        else:
            fet = _transistor_symbol(part, library, rails or set())
            if fet is not None and symlib.fits(fet, pins) is None:
                chosen[part.name] = fet
    missing = [p for p in chips if p.name not in chosen and p.lcsc]
    data = easyeda.fetch_all(p.lcsc for p in missing) if lcsc and missing else {}
    for part in missing:
        sym = easyeda.to_symbol(data[part.lcsc], part.lcsc) if part.lcsc in data else None
        if sym is not None and symlib.fits(sym, {p.number: (p.name, p.net) for p in part.pins}, names=False) is None:
            chosen[part.name] = sym
    return chosen


def connectivity_errors(text: str, parts: list[Part]) -> list[str]:
    """Nets the drawn schematic splits or joins, compared with the netlist (empty: it matches)."""
    sch = sexpr.loads(text)
    parent: dict[object, object] = {}

    def find(a: object) -> object:
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: object, b: object) -> None:
        parent[find(a)] = find(b)

    def at(node: list[sexpr.Node]) -> tuple[float, float]:
        a = sexpr.child(node, "at")
        return (round(float(str(a[1])), 3), round(float(str(a[2])), 3)) if a is not None else (0.0, 0.0)

    wires = []
    for wire in sexpr.children(sch, "wire"):
        ends = [
            (round(float(str(xy[1])), 3), round(float(str(xy[2])), 3))
            for xy in sexpr.walk(wire)
            if sexpr.head(xy) == "xy"
        ]
        union(ends[0], ends[-1])
        wires.append((ends[0], ends[-1]))
    points = [p for w in wires for p in w]
    for label in sexpr.children(sch, "global_label"):
        union(at(label), ("net", str(label[1])))
        points.append(at(label))
    values = {
        sexpr.property_value(s, "Reference"): sexpr.property_value(s, "Value") for s in sexpr.children(sch, "symbol")
    }
    pins: dict[tuple[str, str], object] = {}
    for (ref, number), (x, y) in fixes._pin_positions(sch).items():
        point = (round(x, 3), round(y, 3))
        points.append(point)
        if ref.startswith("#PWR"):
            union(point, ("net", values.get(ref)))
        else:
            union((ref, number), point)
            pins[ref, number] = None
    # as in KiCad, a pin end, label or wire end anywhere along a wire connects to it
    vertical: dict[float, list[tuple[float, float]]] = {}
    horizontal: dict[float, list[tuple[float, float]]] = {}
    for (x1, y1), (x2, y2) in wires:
        if x1 == x2:
            vertical.setdefault(x1, []).append((min(y1, y2), max(y1, y2)))
        elif y1 == y2:
            horizontal.setdefault(y1, []).append((min(x1, x2), max(x1, x2)))
    for px, py in set(points):
        for lo, hi in vertical.get(px, []):
            if lo < py < hi:
                union((px, py), (px, lo))
        for lo, hi in horizontal.get(py, []):
            if lo < px < hi:
                union((px, py), (lo, py))
    group_of: dict[str, set[object]] = {}
    for part in parts:
        for pin in part.pins:
            if pin.net and (part.name, pin.number) in pins:
                group_of.setdefault(pin.net, set()).add(find((part.name, pin.number)))
    errors = [f"{net} split" for net, groups in group_of.items() if len(groups) > 1]
    owner: dict[object, str] = {}
    for net, groups in group_of.items():
        for g in groups:
            if g in owner:
                errors.append(f"{owner[g]} joined to {net}")
            owner[g] = net
    for part in parts:  # an unconnected pin touched by a net
        for pin in part.pins:
            if not pin.net and (part.name, pin.number) in pins and find((part.name, pin.number)) in owner:
                errors.append(f"{part.name} pin {pin.number} on {owner[find((part.name, pin.number))]}")
    return errors


def generate(circuit_json: Path, stem: str, source: Path | None = None, library: symlib.Library | None = None) -> str:
    """The ``.kicad_sch`` text for the circuit; ``source`` (the board TSX) gives the blocks,
    ``library`` the chip symbols (default: KiCad's installed library, then LCSC)."""
    return generate_with_notes(circuit_json, stem, source, library)[0]


def generate_with_notes(
    circuit_json: Path, stem: str, source: Path | None = None, library: symlib.Library | None = None
) -> tuple[str, list[str]]:
    """:func:`generate`, plus notes on what fell back. The drawing is checked against the netlist;
    should real symbols ever break a net, the schematic is drawn again with boxes only."""
    circuit = json.loads(circuit_json.read_text(encoding="utf-8"))
    parts = _parts(circuit)
    symbols = _library_symbols(parts, library if library is not None else symlib.Library(), _rails(circuit, parts))
    text = _render(circuit, parts, symbols, stem, source)
    errors = connectivity_errors(text, parts)
    if not errors:
        return text, []
    text = _render(circuit, parts, {}, stem, source)
    still = connectivity_errors(text, parts)
    notes = [f"symbols dropped, the drawing broke nets: {', '.join(errors[:5])}"]
    if still:
        msg = f"generated schematic does not match the netlist: {', '.join(still[:5])}"
        raise ValueError(msg)
    return text, notes


def _adopt_decoupling(parts: list[Part], section_of: dict[str, str], rails: set[str]) -> None:
    """A decoupling cap outside every TSX section joins the section of the chip it decouples (the
    nearest one on the PCB among the chips its supply powers), so it can stand at that chip's pin."""
    chips = [p for p in parts if not p.two]
    for part in parts:
        supply = {q.net for q in part.pins if q.net in rails and not is_ground(q.net)}
        if not part.two or section_of.get(part.name, "") or not supply or any(q.net not in rails for q in part.pins):
            continue
        powered = [c for c in chips if any(q.net in supply and _POWER_PIN.match(q.name) for q in c.pins)]
        if not powered or any(not section_of.get(c.name, "") for c in powered):
            continue  # a chip of its own (unsectioned) block takes it there
        pos = part.pos
        chip = min(powered, key=lambda c: math.dist(pos, c.pos) if pos and c.pos else math.inf)
        section_of[part.name] = section_of[chip.name]


def _render(
    circuit: list[dict[str, Any]],
    parts: list[Part],
    symbols: dict[str, symlib.LibSymbol],
    stem: str,
    source: Path | None,
) -> str:
    rails = _rails(circuit, parts)
    section_of = sections(source)
    _adopt_decoupling(parts, section_of, rails)
    titles: list[str] = []
    for part in parts:
        section_of.setdefault(part.name, "")
        if section_of[part.name] not in titles:
            titles.append(section_of[part.name])
    net_blocks: dict[str, set[str]] = {}
    for part in parts:
        for pin in part.pins:
            if pin.net:
                net_blocks.setdefault(pin.net, set()).add(section_of[part.name])
    global_nets = {n for n, b in net_blocks.items() if len(b) > 1 and n not in rails}

    sheet = Sheet(stem, global_nets, _negative(rails, parts))
    blocks = [Block(t, [p for p in parts if section_of[p.name] == t], rails, global_nets, symbols) for t in titles]
    framed = len(blocks) > 1 or bool(titles and titles[0])
    pad = BLOCK_PAD if framed else 0.0
    head = 2 * TITLE if framed else 0.0
    # the block shapes and arrangement that fit the smallest paper, as large as possible on it
    best: tuple[tuple[int, float], list[Any], list[tuple[float, float]]] | None = None
    for inner in (0.7, 1.0, 1.3, 1.6, 2.0, 2.5, 3.2):
        layouts = []
        for block in blocks:
            spots, w, h = _pack(block.cells, _target_width(block.cells, inner))
            title_w = len(block.title or "other") * TITLE * 0.9 if framed else 0.0  # the frame holds its title
            layouts.append((block, spots, _snap(max(w, title_w) + 2 * pad), _snap(h + 2 * pad + head)))
        outer = [Cell(w, h, None) for _, _, w, h in layouts]
        for aspect in (0.7, 0.85, 1.0, 1.2, math.sqrt(2), 1.55, 1.7, 2.0, 2.5):
            for origins, w, h in (_pack(outer, _target_width(outer, aspect)), _pack_columns(outer, aspect)):
                score = (_paper(w, h), -_scale(w, h))
                if best is None or score < best[0]:
                    best = (score, layouts, origins)
    assert best is not None
    _, layouts, origins = best
    for (block, spots, w, h), (bx, by) in zip(layouts, origins, strict=True):
        bx, by = bx + SHEET_MARGIN, by + SHEET_MARGIN
        if framed:
            sheet.frame(bx, by, bx + w, by + h)
            sheet.text(block.title or "other", bx + pad, by + pad + TITLE, TITLE)
        for cell, (cx, cy) in zip(block.cells, spots, strict=True):
            cell.draw(sheet, bx + pad + cx, by + pad + head + cy)

    sheet.finish()
    xs, ys = [p[0] for p in sheet.points] or [0.0], [p[1] for p in sheet.points] or [0.0]
    width, height = max(xs) + SHEET_MARGIN, max(ys) + SHEET_MARGIN
    # A0 or smaller: fit_schematic_sheet picks the paper; beyond A0 a user sheet that holds it
    fits = width - 2 * SHEET_MARGIN <= A0_USABLE[0] and height - 2 * SHEET_MARGIN <= A0_USABLE[1]
    paper = '(paper "A0")' if fits else f'(paper "User" {_fmt(_snap(width))} {_fmt(_snap(height))})'
    lib = list(dict.fromkeys([*RAIL_LIBS, *sheet.lib]))
    return (
        f'(kicad_sch (version 20250114) (generator "vibepcb") (generator_version "1") (uuid {sheet.root})'
        f" {paper}\n(lib_symbols\n{chr(10).join(lib)}\n)\n{chr(10).join(sheet.items)}\n"
        '(sheet_instances (path "/" (page "1"))) (embedded_fonts no))\n'
    )


def write(
    circuit_json: Path, sch_path: Path, stem: str, source: Path | None = None, library: symlib.Library | None = None
) -> list[str]:
    """Replace ``sch_path`` with the generated schematic (pretty-printed); returns notes on fallbacks."""
    text, notes = generate_with_notes(circuit_json, stem, source, library)
    sch_path.write_text(text, encoding="utf-8")
    sexpr.dump(sexpr.load(sch_path), sch_path)
    return notes
