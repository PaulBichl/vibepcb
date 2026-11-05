"""Assembly documents for hand soldering: what goes where, in which order, which way round.

``collect`` reads the checked KiCad board (footprints, pads, courtyards via :mod:`vibepcb.core.sexpr`), the
schematic's pin names, tscircuit's circuit JSON (port hints: anode / cathode / +) and the fab BOM, and returns a
:class:`Assembly`: the parts with their polarity mark, the BOM lines split into assembly steps in hand-soldering
order (:func:`assembly_order`), test points / DNP parts apart. :mod:`vibepcb.output.assembly_draw` draws it
(``<board>-assembly.pdf`` and ``<board>-assembly.html``). Deterministic: sorted, no timestamps.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb.core import sexpr

if TYPE_CHECKING:
    from pathlib import Path

    from vibepcb.core.sexpr import Node

type Point = tuple[float, float]
type Box = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax (board mm, y down)

FINE_PITCH = 0.65  # mm: pad pitch at or below this is fine pitch (TSSOP, MSOP, QFN)
SMALL_AREA = 15.0  # mm2 courtyard: 0402 .. 1206, SOT-23, SOD-123
MODULE_AREA = 150.0  # mm2: bigger SMD parts with pads under the body are modules, soldered with the large parts
ELECTROMECH = re.compile(r"^(J|P|CN|CON|X|SW|S|BTN|ENC|K|RL|RLY|BT|BAT|BZ|LS|MK|F|FH)\d", re.IGNORECASE)
SWITCH = re.compile(r"^(SW|S|BTN|ENC|K)\d", re.IGNORECASE)
DIODE = re.compile(r"^(D|LED|ZD|TVS|CR|VD)\d", re.IGNORECASE)
LED = re.compile(r"^(LED)\d", re.IGNORECASE)
CAPACITOR = re.compile(r"^(C|CP|EC)\d", re.IGNORECASE)
TESTPOINT = re.compile(r"^TP\d", re.IGNORECASE)
CATHODE_NAMES = {"k", "c", "cathode", "kathode", "-", "neg"}
ANODE_NAMES = {"a", "anode", "+", "pos"}
PLUS_NAMES = {"+", "pos", "plus", "anode"}
MINUS_NAMES = {"-", "neg", "minus", "cathode"}
# tscircuit's own diode / LED footprints: pad 1 is the anode (see build.tscircuit.anode_first_refs)
BUILTIN_ANODE_FIRST = re.compile(r"^tscircuit:(led|diode)_", re.IGNORECASE)
REVISION = re.compile(r"\brev(?:ision)?\.?\s*([A-Za-z0-9][A-Za-z0-9.]*)", re.IGNORECASE)
SIDES = ("top", "bottom")

RANKS = (  # hand-soldering order within a side: lowest / smallest first, connectors last, THT after all SMD
    "fine-pitch / leadless ICs",
    "ICs",
    "small SMD parts",
    "large SMD parts",
    "SMD connectors / switches",
    "through-hole parts",
    "through-hole connectors / switches",
)
THT_RANK = 5


@dataclass(frozen=True)
class Pad:
    number: str
    x: float
    y: float
    corners: tuple[Point, ...]  # copper outline (rotated rectangle) in board mm
    round: bool  # circle / oval: drawn as an ellipse in the fallback base drawing
    tht: bool


@dataclass(frozen=True)
class Mark:
    """The orientation mark of a part: ``pin1`` (dot), ``cathode`` (bar) or ``plus`` (+) at ``pad``."""

    kind: str
    pad: str
    source: str  # where the polarity came from (schematic pin name, circuit JSON hint, footprint)


@dataclass(frozen=True)
class Part:
    ref: str
    value: str
    footprint: str  # library name without the "lib:" prefix
    side: str  # "top" | "bottom"
    x: float
    y: float
    rotation: float
    pads: tuple[Pad, ...]
    outline: tuple[Point, ...]  # courtyard polygon (board mm); the pad extent when there is none
    mpn: str = ""
    lcsc: str = ""
    in_bom: bool = True
    dnp: bool = False
    mark: Mark | None = None
    note: str = ""  # polarity / handling note for the step

    @property
    def box(self) -> Box:
        xs, ys = [p[0] for p in self.outline], [p[1] for p in self.outline]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def area(self) -> float:
        x0, y0, x1, y1 = self.box
        return (x1 - x0) * (y1 - y0)

    @property
    def tht(self) -> bool:
        numbered = [p for p in self.pads if p.number]
        return bool(numbered) and sum(p.tht for p in numbered) * 2 > len(numbered)

    @property
    def testpoint(self) -> bool:
        return bool(TESTPOINT.match(self.ref))

    def pad(self, number: str) -> Pad | None:
        return next((p for p in self.pads if p.number == number), None)


@dataclass(frozen=True)
class Step:
    number: int
    side: str
    rank: int
    value: str
    footprint: str
    mpn: str
    lcsc: str
    refs: tuple[str, ...]
    note: str

    @property
    def category(self) -> str:
        return RANKS[self.rank]


@dataclass(frozen=True)
class Hole:
    x: float
    y: float
    diameter: float


@dataclass
class Assembly:
    name: str
    revision: str
    date: str
    outline: Box  # board extent (Edge.Cuts)
    copper_layers: int
    thickness: float
    parts: list[Part]
    holes: list[Hole]
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def width(self) -> float:
        return self.outline[2] - self.outline[0]

    @property
    def height(self) -> float:
        return self.outline[3] - self.outline[1]

    @property
    def by_ref(self) -> dict[str, Part]:
        return {p.ref: p for p in self.parts}

    @property
    def testpoints(self) -> list[Part]:
        return sorted((p for p in self.parts if p.testpoint), key=lambda p: natural_key(p.ref))

    @property
    def not_fitted(self) -> list[Part]:
        """Parts on the board that are not soldered: DNP, or left out of the BOM (test points apart)."""
        return sorted(
            (p for p in self.parts if not p.testpoint and (p.dnp or not p.in_bom)), key=lambda p: natural_key(p.ref)
        )

    def side_parts(self, side: str) -> list[Part]:
        return [p for p in self.parts if p.side == side]


def natural_key(ref: str) -> tuple[str, int, str]:
    m = re.match(r"(\D*)(\d*)(.*)", ref)
    return (m[1], int(m[2] or 0), m[3]) if m else (ref, 0, "")


# --- geometry ----------------------------------------------------------------


def _num(node: list[Node] | None, i: int, default: float = 0.0) -> float:
    return float(str(node[i])) if node is not None and len(node) > i else default


def _rotate(x: float, y: float, degrees: float) -> Point:
    """KiCad's rotation (y down, positive = counter-clockwise on screen)."""
    a = math.radians(degrees)
    return x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)


def _to_board(fp: list[Node]) -> tuple[float, float, float]:
    at = sexpr.child(fp, "at")
    return _num(at, 1), _num(at, 2), _num(at, 3)


def _pad(fp: list[Node], pad: list[Node]) -> Pad:
    fx, fy, fa = _to_board(fp)
    at, size = sexpr.child(pad, "at"), sexpr.child(pad, "size")
    px, py, pa = _num(at, 1), _num(at, 2), _num(at, 3)  # a pad's angle is absolute (footprint rotation included)
    hw, hh = _num(size, 1) / 2, _num(size, 2) / 2
    box = [-hw, -hh, hw, hh]
    primitives = sexpr.child(pad, "primitives")
    for xy in [] if primitives is None else (n for n in sexpr.walk(primitives) if sexpr.head(n) == "xy"):
        box = [min(box[0], _num(xy, 1)), min(box[1], _num(xy, 2)), max(box[2], _num(xy, 1)), max(box[3], _num(xy, 2))]
    corners = []
    for u, v in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])):
        lx, ly = _rotate(u, v, pa - fa)
        bx, by = _rotate(px + lx, py + ly, fa)
        corners.append((round(fx + bx, 4), round(fy + by, 4)))
    cx, cy = _rotate(px, py, fa)
    kind = str(pad[2])
    return Pad(
        number=str(pad[1]),
        x=round(fx + cx, 4),
        y=round(fy + cy, 4),
        corners=tuple(corners),
        round=str(pad[3]) in ("circle", "oval") and primitives is None,
        tht=kind == "thru_hole",
    )


def _courtyard(fp: list[Node]) -> list[Point]:
    """The courtyard of a footprint in board mm: its polygon / rectangle, else the extent of its lines."""
    fx, fy, fa = _to_board(fp)
    shapes: list[list[Point]] = []
    for node in fp:
        if not isinstance(node, list) or sexpr.head(node) not in ("fp_rect", "fp_poly", "fp_line", "fp_circle"):
            continue
        layer = sexpr.child(node, "layer")
        if layer is None or not str(layer[1]).endswith("CrtYd"):
            continue
        head = sexpr.head(node)
        if head == "fp_rect":
            s, e = sexpr.child(node, "start"), sexpr.child(node, "end")
            x0, y0, x1, y1 = _num(s, 1), _num(s, 2), _num(e, 1), _num(e, 2)
            shapes.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        elif head == "fp_poly":
            pts = sexpr.child(node, "pts")
            shapes.append([(_num(xy, 1), _num(xy, 2)) for xy in sexpr.children(pts or [], "xy")])
        elif head == "fp_circle":
            c, e = sexpr.child(node, "center"), sexpr.child(node, "end")
            r = math.hypot(_num(e, 1) - _num(c, 1), _num(e, 2) - _num(c, 2))
            cx, cy = _num(c, 1), _num(c, 2)
            shapes.append([(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)])
        else:
            s, e = sexpr.child(node, "start"), sexpr.child(node, "end")
            shapes.append([(_num(s, 1), _num(s, 2)), (_num(e, 1), _num(e, 2))])
    if not shapes:
        return []
    if len(shapes) == 1 and len(shapes[0]) >= 3:
        local = shapes[0]
    else:  # loose lines / several shapes: their extent
        xs, ys = [p[0] for s in shapes for p in s], [p[1] for s in shapes for p in s]
        local = [(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys))]
    out = []
    for x, y in local:
        bx, by = _rotate(x, y, fa)
        out.append((round(fx + bx, 4), round(fy + by, 4)))
    return out


def _pad_extent(pads: list[Pad], grow: float = 0.25) -> list[Point]:
    xs = [c[0] for p in pads for c in p.corners]
    ys = [c[1] for p in pads for c in p.corners]
    x0, y0, x1, y1 = min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def board_outline(pcb: list[Node]) -> Box:
    """Extent of the Edge.Cuts drawing (board-level shapes; arcs and circles by their points / radius)."""
    xs: list[float] = []
    ys: list[float] = []
    for node in pcb:
        if not isinstance(node, list) or sexpr.head(node) not in (
            "gr_line",
            "gr_rect",
            "gr_arc",
            "gr_circle",
            "gr_poly",
        ):
            continue
        layer = sexpr.child(node, "layer")
        if layer is None or str(layer[1]) != "Edge.Cuts":
            continue
        if sexpr.head(node) == "gr_circle":
            c, e = sexpr.child(node, "center"), sexpr.child(node, "end")
            r = math.hypot(_num(e, 1) - _num(c, 1), _num(e, 2) - _num(c, 2))
            xs += [_num(c, 1) - r, _num(c, 1) + r]
            ys += [_num(c, 2) - r, _num(c, 2) + r]
            continue
        for sub in sexpr.walk(node):
            if sexpr.head(sub) in ("start", "end", "mid", "xy"):
                xs.append(_num(sub, 1))
                ys.append(_num(sub, 2))
    if not xs:
        msg = "the board has no Edge.Cuts outline"
        raise ValueError(msg)
    return min(xs), min(ys), max(xs), max(ys)


# --- polarity ----------------------------------------------------------------


def schematic_pin_names(sch: list[Node]) -> dict[str, dict[str, str]]:
    """``ref -> pad number -> pin name`` from the schematic's per-part library symbols (``vibepcb:<ref>``),
    mapped through the placed symbols' references."""
    lib = sexpr.child(sch, "lib_symbols")
    pins_of: dict[str, dict[str, str]] = {}
    for symbol in [] if lib is None else sexpr.children(lib, "symbol"):
        pins: dict[str, str] = {}
        for node in sexpr.walk(symbol):
            if sexpr.head(node) == "pin":
                number, name = sexpr.child(node, "number"), sexpr.child(node, "name")
                if number is not None and name is not None:
                    pins[str(number[1])] = str(name[1])
        pins_of[str(symbol[1])] = pins
    out: dict[str, dict[str, str]] = {}
    for placed in sexpr.children(sch, "symbol"):
        lib_id, ref = sexpr.child(placed, "lib_id"), sexpr.property_value(placed, "Reference")
        if lib_id is not None and ref and str(lib_id[1]) in pins_of:
            out.setdefault(ref, {}).update(pins_of[str(lib_id[1])])
    return out


def circuit_hints(circuit_json: Path | None) -> dict[str, dict[str, set[str]]]:
    """``ref -> pin number -> port hints`` (lower case) of the polarised parts in tscircuit's circuit JSON:
    diodes and LEDs (an LED's pins also get the hint ``led``), capacitors with ``are_pins_interchangeable: false``
    (electrolytics)."""
    if circuit_json is None or not circuit_json.exists():
        return {}
    elements = json.loads(circuit_json.read_text(encoding="utf-8"))
    names = {
        e["source_component_id"]: (e.get("name", ""), e.get("ftype"))
        for e in elements
        if e.get("type") == "source_component"
        and (
            e.get("ftype") in ("simple_diode", "simple_led")
            or (e.get("ftype") == "simple_capacitor" and e.get("are_pins_interchangeable") is False)
        )
    }
    out: dict[str, dict[str, set[str]]] = {}
    for e in elements:
        if e.get("type") == "source_port" and e.get("source_component_id") in names:
            number = e.get("pin_number")
            if number is None:
                continue
            name, ftype = names[e["source_component_id"]]
            hints = {str(h).lower() for h in e.get("port_hints") or []} | {str(e.get("name", "")).lower()}
            out.setdefault(name, {})[str(number)] = hints | ({"led"} if ftype == "simple_led" else set())
    return out


def _mark(
    ref: str, footprint: str, pads: list[Pad], pins: dict[str, str], hints: dict[str, set[str]]
) -> tuple[Mark | None, str]:
    """The orientation mark of a part and the note for its step."""
    numbers = {p.number for p in pads if p.number}
    two_pin = len(numbers) == 2
    if DIODE.match(ref) and two_pin:
        led = LED.match(ref) or "led" in footprint.lower() or any("led" in h for h in hints.values())
        what = "LED" if led else "diode"
        for number, name in sorted(pins.items()):
            if name.lower() in CATHODE_NAMES and number in numbers:
                return Mark("cathode", number, f"schematic pin {name}"), _cathode_note(what)
        for number, name in sorted(pins.items()):
            if name.lower() in ANODE_NAMES and number in numbers:
                other = next(iter(sorted(numbers - {number})))
                return Mark("cathode", other, f"schematic pin {name} on pad {number}"), _cathode_note(what)
        for number, hint in sorted(hints.items()):
            if "cathode" in hint and number in numbers:
                return Mark("cathode", number, "circuit JSON port hint"), _cathode_note(what)
        if BUILTIN_ANODE_FIRST.match(footprint) and "2" in numbers:
            return Mark("cathode", "2", "tscircuit footprint (pad 1 = anode)"), _cathode_note(what)
        return Mark("pin1", "1", "pad 1 only") if "1" in numbers else None, (
            f"{what}: polarity NOT found in the design data - check the datasheet and the silkscreen before soldering"
        )
    if CAPACITOR.match(ref) and two_pin:
        plus = next((n for n, name in sorted(pins.items()) if name.lower() in ("+", "pos", "plus")), None)
        source = "schematic pin name"
        if plus is None:
            plus = next((n for n, h in sorted(hints.items()) if h & {"pos", "+"}), None)
            source = "circuit JSON (polarised capacitor)"
        if plus is None:
            minus = next((n for n, name in sorted(pins.items()) if name.lower() in ("-", "neg", "minus")), None)
            plus = next(iter(sorted(numbers - {minus}))) if minus else None
            source = "schematic pin name"
        if plus is not None and plus in numbers:
            return Mark("plus", plus, source), "polarised: + to the + mark (the stripe on the can is -)"
        return None, ""
    if SWITCH.match(ref):
        return None, ""
    pad_ones = [p for p in pads if p.number in ("1", "A1")]
    if len(numbers) >= 3 and len(pad_ones) == 1:
        kind = "connector" if ELECTROMECH.match(ref) else "part"
        note = (
            "pin 1 to the red dot (check the plug's orientation against the silkscreen)"
            if kind == "connector"
            else "pin 1 (dot / notch / bevel on the part) to the red dot"
        )
        return Mark("pin1", pad_ones[0].number, "pad 1"), note
    return None, ""


def _cathode_note(what: str) -> str:
    if what == "LED":
        return "LED: cathode to the K bar (the LED's cathode mark: see its datasheet, often a green dot / T)"
    return "polarised: cathode band to the K bar"


# --- order -------------------------------------------------------------------


def min_pitch(part: Part) -> float:
    """Smallest centre distance between two differently numbered SMD pads (inf when fewer than two)."""
    pads = [p for p in part.pads if p.number and not p.tht]
    best = math.inf
    for i, a in enumerate(pads):
        for b in pads[i + 1 :]:
            if a.number != b.number:
                best = min(best, math.hypot(a.x - b.x, a.y - b.y))
    return best


def pads_under_body(part: Part) -> bool:
    """Whether a pad sits inside the pad field (exposed pad, QFN / LGA module ground pads, BGA balls)."""
    pads = [p for p in part.pads if p.number and not p.tht]
    if len(pads) < 5:
        return False
    xs, ys = [p.x for p in pads], [p.y for p in pads]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    mx, my = (x1 - x0) * 0.25, (y1 - y0) * 0.25
    return any(x0 + mx < p.x < x1 - mx and y0 + my < p.y < y1 - my for p in pads)


def rank(part: Part) -> int:
    """Index into :data:`RANKS`."""
    electromech = bool(ELECTROMECH.match(part.ref))
    if part.tht:
        return THT_RANK + 1 if electromech else THT_RANK
    if electromech:
        return 4
    numbers = {p.number for p in part.pads if p.number}
    if len(numbers) >= 5:
        if part.area > MODULE_AREA:
            return 3
        return 0 if min_pitch(part) <= FINE_PITCH + 1e-6 or pads_under_body(part) else 1
    return 2 if part.area <= SMALL_AREA else 3


def _handling_note(part: Part) -> str:
    if part.tht:
        return f"through-hole: insert from the {part.side} side, solder on the other side"
    if pads_under_body(part):
        return "pads under the body: flux + hot air or hotplate (an iron cannot reach them)"
    if min_pitch(part) <= FINE_PITCH + 1e-6 and len({p.number for p in part.pads}) >= 5:
        return "fine pitch: flux, drag-solder, check for bridges with a loupe"
    return ""


def _value_key(value: str) -> float:
    m = re.match(r"([\d.]+)\s*([pnuµmkKMG]?)", value)
    if not m:
        return math.inf
    scale = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
    try:
        return float(m[1]) * scale.get(m[2], 1.0)
    except ValueError:
        return math.inf


def assembly_order(parts: list[Part], groups: list[tuple[str, str, str, str, list[str]]]) -> list[Step]:
    """BOM lines (``value, footprint, mpn, lcsc, refs``) as steps in hand-soldering order.

    SMD first, side by side: the side with the fine-pitch / leadless ICs first (else the one with more SMD
    parts); within a side by :data:`RANKS` (fine-pitch ICs, ICs, small parts, large parts, connectors), then by
    size. Through-hole parts after all SMD. A BOM line with parts on both sides becomes one step per side.
    """
    by_ref = {p.ref: p for p in parts}
    smd = [p for p in parts if not p.tht]

    def side_score(side: str) -> tuple[int, int, int]:
        mine = [p for p in smd if p.side == side]
        return (-sum(rank(p) == 0 for p in mine), -sum(rank(p) <= 1 for p in mine), -len(mine))

    side_order = sorted(SIDES, key=lambda s: (side_score(s), SIDES.index(s)))
    rows = []
    for value, footprint, mpn, lcsc, refs in groups:
        for side in SIDES:
            mine = [by_ref[r] for r in refs if r in by_ref and by_ref[r].side == side]
            if not mine:
                continue
            ranks = {rank(p) for p in mine}
            step_rank = min(ranks)
            tht = step_rank >= THT_RANK
            notes = list(dict.fromkeys(n for p in mine for n in (p.note, _handling_note(p)) if n))
            key = (
                (1 if tht else 0),
                0 if tht else side_order.index(side),
                step_rank,
                round(max(p.area for p in mine), 0),
                natural_key(mine[0].ref)[0],
                _value_key(value),
                value,
                side_order.index(side),
            )
            rows.append((key, side, step_rank, value, footprint, mpn, lcsc, mine, "; ".join(notes)))
    rows.sort(key=lambda r: r[0])
    return [
        Step(
            i, side, step_rank, value, footprint, mpn, lcsc, tuple(sorted((p.ref for p in mine), key=natural_key)), note
        )
        for i, (_, side, step_rank, value, footprint, mpn, lcsc, mine, note) in enumerate(rows, start=1)
    ]


# --- collect -----------------------------------------------------------------


def _bom_rows(bom_csv: Path | None) -> list[dict[str, str]]:
    if bom_csv is None or not bom_csv.exists():
        return []
    with bom_csv.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _meaningful(value: str, ref: str) -> bool:
    return len(value) > 1 and value != ref


def collect(
    pcb_path: Path, sch_path: Path | None = None, *, circuit_json: Path | None = None, bom_csv: Path | None = None
) -> Assembly:
    """The assembly data of a checked board (``pcb_path``); the schematic, circuit JSON and the fab BOM
    (``<board>-bom.csv``, JLCPCB format) are optional sources for pin names, polarity hints and BOM lines."""
    pcb = sexpr.load(pcb_path)
    sch = sexpr.load(sch_path) if sch_path is not None and sch_path.exists() else []
    pin_names = schematic_pin_names(sch) if sch else {}
    hints = circuit_hints(circuit_json)
    bom = _bom_rows(bom_csv)
    bom_of: dict[str, dict[str, str]] = {}
    for row in bom:
        for ref in re.split(r"[,\s]+", row.get("Designator", "")):
            if ref:
                bom_of[ref] = row
    parts: list[Part] = []
    holes: list[Hole] = []
    warnings: list[str] = []
    for fp in sexpr.children(pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or ""
        raw_pads = list(sexpr.children(fp, "pad"))
        if not ref:
            for pad in raw_pads:
                drill = sexpr.child(pad, "drill")
                if str(pad[2]) in ("np_thru_hole", "thru_hole") and drill is not None:
                    p = _pad(fp, pad)
                    holes.append(Hole(p.x, p.y, _num(drill, 1)))
            continue
        layer = sexpr.child(fp, "layer")
        side = "bottom" if layer is not None and str(layer[1]) == "B.Cu" else "top"
        pads = [_pad(fp, pad) for pad in raw_pads if str(pad[2]) != "np_thru_hole"]
        outline = _courtyard(fp) or (_pad_extent(pads) if pads else [])
        fx, fy, fa = _to_board(fp)
        if not outline:
            outline = [(fx - 0.5, fy - 0.5), (fx + 0.5, fy - 0.5), (fx + 0.5, fy + 0.5), (fx - 0.5, fy + 0.5)]
        attr = sexpr.child(fp, "attr")
        flags = {str(a) for a in attr[1:]} if attr is not None else set()
        dnp = "dnp" in flags or sexpr.child(fp, "dnp") is not None
        bom_row = bom_of.get(ref)
        footprint = str(fp[1]).split(":")[-1]
        pcb_value = sexpr.property_value(fp, "Value") or ""
        mpn = (bom_row or {}).get("MPN") or sexpr.property_value(fp, "MPN") or ""
        lcsc = (bom_row or {}).get("LCSC Part #") or sexpr.property_value(fp, "Supplier Part Number") or ""
        value = next(
            (v for v in ((bom_row or {}).get("Comment", ""), pcb_value, mpn) if _meaningful(v, ref)), pcb_value or ref
        )
        mark, note = _mark(ref, str(fp[1]), pads, pin_names.get(ref, {}), hints.get(ref, {}))
        in_bom = "exclude_from_bom" not in flags and (not bom or bom_row is not None)
        if mark is not None and "NOT found" in note:
            warnings.append(f"{ref}: {note}")
        parts.append(
            Part(
                ref=ref,
                value=value,
                footprint=(bom_row or {}).get("Footprint") or footprint,
                side=side,
                x=fx,
                y=fy,
                rotation=fa,
                pads=tuple(pads),
                outline=tuple(outline),
                mpn=mpn,
                lcsc=lcsc,
                in_bom=in_bom,
                dnp=dnp,
                mark=mark,
                note=note,
            )
        )
    parts.sort(key=lambda p: natural_key(p.ref))
    fitted = [p for p in parts if p.in_bom and not p.dnp and not p.testpoint]
    groups = _groups(fitted, bom)
    title_block = sexpr.child(sch, "title_block") if sch else None
    date = sexpr.child(title_block, "date") if title_block is not None else None
    layers = sexpr.child(pcb, "layers")
    copper = [n for n in (layers or [])[1:] if isinstance(n, list) and len(n) > 1 and str(n[1]).endswith(".Cu")]
    general = sexpr.child(pcb, "general")
    assembly = Assembly(
        name=pcb_path.stem,
        revision=_revision(pcb),
        date=str(date[1]) if date is not None else "",
        outline=board_outline(pcb),
        copper_layers=len(copper),
        thickness=_num(sexpr.child(general or [], "thickness"), 1, 1.6),
        parts=parts,
        holes=sorted(holes, key=lambda h: (h.y, h.x)),
        warnings=warnings,
    )
    assembly.steps = assembly_order(fitted, groups)
    return assembly


def _groups(fitted: list[Part], bom: list[dict[str, str]]) -> list[tuple[str, str, str, str, list[str]]]:
    """BOM lines: the fab BOM's rows when there is one (what was ordered), else value + footprint + MPN + LCSC."""
    by_ref = {p.ref: p for p in fitted}
    groups: list[tuple[str, str, str, str, list[str]]] = []
    seen: set[str] = set()
    for row in bom:
        refs = [r for r in re.split(r"[,\s]+", row.get("Designator", "")) if r in by_ref]
        if refs:
            first = by_ref[refs[0]]
            groups.append((first.value, first.footprint, first.mpn, first.lcsc, refs))
            seen.update(refs)
    rest: dict[tuple[str, str, str, str], list[str]] = {}
    for p in fitted:
        if p.ref not in seen:
            rest.setdefault((p.value, p.footprint, p.mpn, p.lcsc), []).append(p.ref)
    groups += [(*key, refs) for key, refs in rest.items()]
    return groups


def _revision(pcb: list[Node]) -> str:
    """The revision from the board's silkscreen text (``rev C``), else from the title block."""
    for node in pcb:
        if isinstance(node, list) and sexpr.head(node) == "gr_text" and len(node) > 1:
            m = REVISION.search(str(node[1]))
            if m:
                return m[1]
    title_block = sexpr.child(pcb, "title_block")
    rev = sexpr.child(title_block, "rev") if title_block is not None else None
    return str(rev[1]) if rev is not None else ""
