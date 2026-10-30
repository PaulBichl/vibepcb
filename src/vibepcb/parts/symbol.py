"""Schematic symbol: spec + standard -> ``(kicad_symbol_lib ...)`` in KiCad 9 format.

Layout by the standard (§1.2-1.4, rules in ``rules/parts/<standard>.toml``): pins on a 2 mm grid and 2 mm long,
inputs left, outputs right, supply pins on top, ground pins at the bottom, repeated names numbered (GND1, GND2),
the exposed pad as a pin of its own, the designator visible at the top left, the comment (Value) empty and hidden,
no further parameters. The function is drawn where the part kind says what it is: a lone op-amp is a triangle
(Figure 3), several op-amps in one package are triangles inside the box (Figure 2).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from vibepcb import sexpr
from vibepcb.schgen import _text_w
from vibepcb.sexpr import Sym

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibepcb.parts.spec import Pin, Spec
    from vibepcb.parts.standard import Standard
    from vibepcb.sexpr import Node

VERSION = "20241209"  # KiCad 9 symbol library format (Altium 26 imports KiCad up to 9)
GENERATOR = "vibepcb"
FACE = "Arial"  # §1.3 designator Arial 8
LIBRARY = "vibepcb_parts"  # footprint library nickname in the Footprint field
_GROUND = re.compile(r"^(A|D|P|S)?(GND|VSS|VEE|V-|EP_?GND|PAD_?GND)\d*$|GND", re.IGNORECASE)
_SUPPLY = re.compile(r"^(A|D|P)?(V|VCC|VDD|VIN|VBAT|VBUS|VS|V\+|AVIN|PVIN|AVDD|DVDD|IOVDD|VIO)", re.IGNORECASE)
_AMP_IN = re.compile(r"^(?:([+-])IN([A-Z0-9]*)|IN([A-Z0-9]*)([+-]))$", re.IGNORECASE)
_AMP_OUT = re.compile(r"^V?OUT([A-Z0-9]*)$", re.IGNORECASE)
TWO_TERMINAL = ("resistor", "capacitor", "polar_capacitor", "inductor", "diode", "led", "crystal")
_CATHODE = ("K", "C", "CATHODE", "KA", "-")
_ANODE = ("A", "ANODE", "+", "AK")
_OUT_TYPES = {"output", "power_out", "tri_state", "open_collector", "open_emitter", "no_connect"}


@dataclass(frozen=True)
class SymPin:
    number: str
    name: str
    type: str
    side: str
    hidden: bool = False


def numbered_names(pins: list[Pin]) -> dict[str, str]:
    """Pin number -> name, repeated names numbered in pin order (GND, GND -> GND1, GND2; §1.2)."""
    count: dict[str, int] = {}
    for p in pins:
        count[p.name] = count.get(p.name, 0) + 1
    seen: dict[str, int] = {}
    out: dict[str, str] = {}
    for p in sorted(pins, key=lambda p: _num_key(p.number)):
        if count[p.name] > 1:
            seen[p.name] = seen.get(p.name, 0) + 1
            out[p.number] = f"{p.name}{seen[p.name]}"
        else:
            out[p.number] = p.name
    return out


def _num_key(number: str) -> tuple[int, str]:
    m = re.match(r"\d+", number)
    return (int(m.group()) if m else 10**6, number)


def is_ground(pin: Pin) -> bool:
    return pin.type in ("power_in", "power_out", "passive") and bool(_GROUND.search(pin.name))


def side_of(pin: Pin, std: Standard) -> str:
    """Where the standard puts a pin: supply top, ground bottom, inputs left, outputs right."""
    if pin.side:
        return pin.side
    if is_ground(pin):
        return std.text("symbol", "ground")
    if pin.type == "power_in" or (pin.type == "passive" and _SUPPLY.match(pin.name)):
        return std.text("symbol", "supply")
    if pin.type in _OUT_TYPES:
        return std.text("symbol", "outputs")
    return std.text("symbol", "inputs")


def place(spec: Spec, std: Standard) -> list[SymPin]:
    names = numbered_names(spec.pins) if std.flag("symbol", "number_repeated_names") else {}
    ep = spec.package.exposed_pad.number if spec.package.exposed_pad else None

    def side(p: Pin) -> str:
        if p.number == ep and not p.side and p.type in ("power_in", "passive", "unspecified"):
            return std.text("symbol", "ground")  # the exposed pad is the ground / thermal pad unless told
        return side_of(p, std)

    pins = [
        SymPin(p.number, names.get(p.number, p.name), p.type, side(p), p.hidden)
        for p in sorted(spec.pins, key=lambda p: _num_key(p.number))
    ]
    # bidirectional / passive signals: balance them onto the emptier of left / right
    count = {s: sum(1 for p in pins if p.side == s) for s in ("left", "right")}
    out = []
    for p, src in zip(pins, sorted(spec.pins, key=lambda p: _num_key(p.number)), strict=True):
        balance = src.side is None and p.side == "left" and p.type in ("bidirectional", "passive", "unspecified")
        if balance and count["right"] < count["left"] - 1:
            count["left"] -= 1
            count["right"] += 1
            p = replace(p, side="right")
        out.append(p)
    return out


@dataclass(frozen=True)
class Amp:
    plus: SymPin
    minus: SymPin
    out: SymPin


def amplifiers(pins: list[SymPin]) -> list[Amp]:
    """Op-amp sections found by pin names (IN1+ / -INA / OUTA ...), in output pin order."""
    ins: dict[str, dict[str, SymPin]] = {}
    outs: dict[str, SymPin] = {}
    for p in pins:
        if m := _AMP_IN.match(p.name.replace(" ", "")):
            sign = m.group(1) or m.group(4)
            key = (m.group(2) or m.group(3) or "").upper()
            ins.setdefault(key, {})[sign] = p
        elif m := _AMP_OUT.match(p.name.replace(" ", "")):
            outs[m.group(1).upper()] = p
    amps = [Amp(ins[k]["+"], ins[k]["-"], o) for k, o in outs.items() if k in ins and "+" in ins[k] and "-" in ins[k]]
    return sorted(amps, key=lambda a: _num_key(a.out.number))


def _r(v: float) -> Sym:
    v = round(v, 4)
    return Sym(f"{v:.4f}".rstrip("0").rstrip(".") if v != 0 else "0")


def _effects(size: float, justify: str = "", face: str = "") -> list[Node]:
    font: list[Node] = [Sym("font")]
    if face:
        font.append([Sym("face"), face])
    font.append([Sym("size"), _r(size), _r(size)])
    node: list[Node] = [Sym("effects"), font]
    if justify:
        node.append([Sym("justify"), *[Sym(j) for j in justify.split()]])
    return node


def _property(
    key: str, value: str, x: float, y: float, size: float, *, hide: bool, justify: str = "", face: str = ""
) -> list[Node]:
    node: list[Node] = [Sym("property"), key, value, [Sym("at"), _r(x), _r(y), Sym("0")]]
    if hide:
        node.append([Sym("hide"), Sym("yes")])
    node.append(_effects(size, justify, face))
    return node


def _stroke(width: float) -> list[Node]:
    return [Sym("stroke"), [Sym("width"), _r(width)], [Sym("type"), Sym("default")]]


def _poly(points: list[tuple[float, float]], width: float) -> list[Node]:
    pts: list[Node] = [Sym("pts")]
    pts += [[Sym("xy"), _r(x), _r(y)] for x, y in points]
    return [
        Sym("polyline"),
        pts,
        _stroke(width),
        [Sym("fill"), [Sym("type"), Sym("none")]],
    ]


def _pin(p: SymPin, x: float, y: float, angle: int, length: float, size: float) -> list[Node]:
    node: list[Node] = [
        Sym("pin"),
        Sym(p.type),
        Sym("line"),
        [Sym("at"), _r(x), _r(y), Sym(str(angle))],
        [Sym("length"), _r(length)],
    ]
    if p.hidden:
        node.append([Sym("hide"), Sym("yes")])
    node.append([Sym("name"), p.name, _effects(size)])
    node.append([Sym("number"), p.number, _effects(size)])
    return node


@dataclass
class Symbol:
    name: str
    pins: list[SymPin]
    box: tuple[float, float, float, float]  # x0, y0 (bottom), x1, y1 (top); KiCad symbol y points up
    pin_at: dict[str, tuple[float, float, int]]  # number -> connection point and angle
    tree: list[Node]

    def text(self) -> str:
        lib: list[Node] = [
            Sym("kicad_symbol_lib"),
            [Sym("version"), Sym(VERSION)],
            [Sym("generator"), GENERATOR],
            [Sym("generator_version"), "1"],
            self.tree,
        ]
        return sexpr.dumps(lib) + "\n"


def _snap_up(v: float, grid: float) -> float:
    return round(math.ceil(v / grid - 1e-9) * grid, 4)


def build(spec: Spec, std: Standard, footprint_name: str) -> Symbol:
    pitch = std.num("symbol", "pin_pitch")
    length = std.num("symbol", "pin_length")
    size = std.font_mm("symbol", "pin_font_pt")
    offset = std.num("symbol", "pin_number_offset")
    ref_size = std.font_mm("symbol", "designator_font_pt")
    body_w = std.num("choices", "symbol_body_width")
    pins = place(spec, std)
    amps = amplifiers(pins) if spec.symbol_shape == "opamp" and std.flag("symbol", "function_drawing") else []

    def tw(text: str) -> float:
        return _text_w(text) * size / 1.27

    graphics: list[Node] = []
    pin_at: dict[str, tuple[float, float, int]] = {}
    lone = len(amps) == 1 and all(
        p in (amps[0].plus, amps[0].minus, amps[0].out) or p.side in ("top", "bottom") for p in pins
    )
    hide_names = False
    if spec.kind in TWO_TERMINAL and len(pins) == 2 and std.flag("symbol", "function_drawing"):
        box = _two_terminal(spec, pins, pitch, length, body_w, graphics, pin_at)
        hide_names = True
    elif lone and sum(1 for p in pins if p.side in ("top", "bottom")) <= 2:
        box = _lone_opamp(amps[0], pins, pitch, length, body_w, graphics, pin_at)
        hide_names = True  # the + / - marks say it; names would cross the triangle
    else:
        box = _box(pins, amps, pitch, length, size, offset, body_w, tw, graphics, pin_at)
    x0, _y0, _x1, y1 = box
    by_number = {p.number: p for p in pins}
    pin_nodes = [_pin(by_number[n], x, y, a, length, size) for n, (x, y, a) in pin_at.items()]

    pin_names: list[Node] = [Sym("pin_names"), [Sym("offset"), _r(offset)]]
    if hide_names:
        pin_names.append([Sym("hide"), Sym("yes")])
    name = spec.mpn
    prefix = std.value("symbol", "reference_prefix").get(spec.kind, "U")
    hide_value = std.flag("symbol", "comment_empty_hidden")
    top_pin = any(p.side == "top" for p in pins)
    ref_y = y1 + (length + 0.5 if top_pin else 0.5)
    tree: list[Node] = [
        Sym("symbol"),
        name,
        pin_names,
        [Sym("exclude_from_sim"), Sym("no")],
        [Sym("in_bom"), Sym("yes")],
        [Sym("on_board"), Sym("yes")],
        _property(
            "Reference",
            prefix,
            x0,
            ref_y,
            ref_size,
            hide=not std.flag("symbol", "designator_visible"),
            justify="left bottom",
            face=FACE,
        ),
        _property("Value", "" if hide_value else spec.mpn, 0, 0, size, hide=hide_value),
        _property("Footprint", f"{LIBRARY}:{footprint_name}", 0, 0, size, hide=True),
        # §1.3 no parameters: KiCad's own fields stay, empty (the datasheet / description live in the part spec)
        _property("Datasheet", "", 0, 0, size, hide=True),
        _property("Description", "", 0, 0, size, hide=True),
        [Sym("symbol"), f"{name}_0_1", *graphics],
        [Sym("symbol"), f"{name}_1_1", *pin_nodes],
        [Sym("embedded_fonts"), Sym("no")],
    ]
    return Symbol(name, pins, box, pin_at, tree)


def _lone_opamp(
    amp: Amp,
    pins: list[SymPin],
    pitch: float,
    length: float,
    width: float,
    graphics: list[Node],
    pin_at: dict[str, tuple[float, float, int]],
) -> tuple[float, float, float, float]:
    """One op-amp: a triangle (Figure 3), + input above -, supplies on the slanted edges."""
    half = 3 * pitch  # triangle half height
    left, apex = -3 * pitch, 3 * pitch
    graphics.append(_poly([(left, half), (apex, 0), (left, -half), (left, half)], width))
    pin_at[amp.plus.number] = (left - length, pitch, 0)
    pin_at[amp.minus.number] = (left - length, -pitch, 0)
    pin_at[amp.out.number] = (apex + length, 0, 180)
    # a supply pin at x = -pitch meets the edge at y = half * (apex + pitch) / (apex - left)
    x = -pitch
    edge = half * (apex - x) / (apex - left)
    for p in pins:
        if p.side == "top":
            pin_at[p.number] = (x, edge + length, 270)
        elif p.side == "bottom":
            pin_at[p.number] = (x, -edge - length, 90)
    graphics.append(_poly([(left + 0.6, pitch), (left + 1.6, pitch)], width))  # "+"
    graphics.append(_poly([(left + 1.1, pitch + 0.5), (left + 1.1, pitch - 0.5)], width))
    graphics.append(_poly([(left + 0.6, -pitch), (left + 1.6, -pitch)], width))  # "-"
    return (left, -half, apex, half)


def _box(
    pins: list[SymPin],
    amps: list[Amp],
    pitch: float,
    length: float,
    size: float,
    offset: float,
    width: float,
    tw: Callable[[str], float],
    graphics: list[Node],
    pin_at: dict[str, tuple[float, float, int]],
) -> tuple[float, float, float, float]:
    """A box: left / right pins in rows from the top, top / bottom pins in columns from the left."""
    left: list[SymPin | None] = []
    right: list[SymPin | None] = []
    in_amp = {p.number for a in amps for p in (a.plus, a.minus, a.out)}
    for a in amps:  # each op-amp: + / out / - on three rows, a free row between sections
        if left:
            left.append(None)
            right.append(None)
        left += [a.plus, None, a.minus]
        right += [None, a.out, None]
    rest_l = [p for p in pins if p.side == "left" and p.number not in in_amp]
    rest_r = [p for p in pins if p.side == "right" and p.number not in in_amp]
    if amps and (rest_l or rest_r):
        left.append(None)
        right.append(None)
    left += rest_l
    right += rest_r
    rows = max(len(left), len(right), 1)
    top = [p for p in pins if p.side == "top"]
    bottom = [p for p in pins if p.side == "bottom"]

    def longest(group: list[SymPin | None]) -> float:
        return max((tw(p.name) for p in group if p is not None), default=0.0)

    triangle = 3 * pitch if amps else 0.0
    inner_w = longest(left) + longest(right) + 2 * offset + triangle + pitch
    cols = max(len(top), len(bottom))
    w = max(_snap_up(inner_w, pitch), (cols + 1) * pitch, 4 * pitch)
    v_top = longest(list(top)) + offset if top else 0.0
    v_bottom = longest(list(bottom)) + offset if bottom else 0.0
    y1 = _snap_up(max(pitch, v_top + pitch / 2), pitch)
    y0 = -(rows - 1) * pitch - _snap_up(max(pitch, v_bottom + pitch / 2), pitch)
    x0, x1 = 0.0, w
    graphics.append(
        [
            Sym("rectangle"),
            [Sym("start"), _r(x0), _r(y1)],
            [Sym("end"), _r(x1), _r(y0)],
            _stroke(width),
            [Sym("fill"), [Sym("type"), Sym("none")]],
        ]
    )
    for i, p in enumerate(left):
        if p is not None:
            pin_at[p.number] = (x0 - length, -i * pitch, 0)
    for i, p in enumerate(right):
        if p is not None:
            pin_at[p.number] = (x1 + length, -i * pitch, 180)
    for group, y, angle in ((top, y1 + length, 270), (bottom, y0 - length, 90)):
        start = x0 + _snap_up((w - (len(group) - 1) * pitch) / 2 - pitch / 2, pitch)
        start = max(start, x0 + pitch)
        for i, p in enumerate(group):
            pin_at[p.number] = (start + i * pitch, y, angle)
    # function drawing: a triangle per op-amp section between the names
    tl = x0 + offset + longest(left) + pitch / 2
    for a in amps:
        yp = pin_at[a.plus.number][1]
        ym = pin_at[a.minus.number][1]
        yo = pin_at[a.out.number][1]
        graphics.append(
            _poly([(tl, yp + pitch / 2), (tl + triangle, yo), (tl, ym - pitch / 2), (tl, yp + pitch / 2)], width)
        )
        graphics.append(_poly([(tl + 0.3, yp), (tl + 1.0, yp)], width))  # "+"
        graphics.append(_poly([(tl + 0.65, yp + 0.35), (tl + 0.65, yp - 0.35)], width))
        graphics.append(_poly([(tl + 0.3, ym), (tl + 1.0, ym)], width))  # "-"
    return (x0, y0, x1, y1)


def _arc(start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float], width: float) -> list[Node]:
    return [
        Sym("arc"),
        [Sym("start"), _r(start[0]), _r(start[1])],
        [Sym("mid"), _r(mid[0]), _r(mid[1])],
        [Sym("end"), _r(end[0]), _r(end[1])],
        _stroke(width),
        [Sym("fill"), [Sym("type"), Sym("none")]],
    ]


def _two_terminal(
    spec: Spec,
    pins: list[SymPin],
    pitch: float,
    length: float,
    width: float,
    graphics: list[Node],
    pin_at: dict[str, tuple[float, float, int]],
) -> tuple[float, float, float, float]:
    """Resistor, capacitor, inductor, diode, LED, crystal: the usual drawing between a left and a right pin.

    Signal flow left to right: pin 1 left for unpolarised parts, the anode / + left and the cathode right.
    """
    first, second = sorted(pins, key=lambda p: _num_key(p.number))
    polar = spec.kind in ("diode", "led", "polar_capacitor")
    if polar and (first.name.upper() in _CATHODE or second.name.upper() in _ANODE):
        first, second = second, first
    half = pitch  # body from -half to +half (resistor, diode), pins from there outwards
    pin_at[first.number] = (-half - length, 0, 0)
    pin_at[second.number] = (half + length, 0, 180)
    h = 0.8
    if spec.kind == "resistor":
        graphics.append(_poly([(-half, h), (half, h), (half, -h), (-half, -h), (-half, h)], width))
    elif spec.kind in ("capacitor", "polar_capacitor"):
        gap = 0.4
        graphics.append(_poly([(-half, 0), (-gap, 0)], width))
        graphics.append(_poly([(gap, 0), (half, 0)], width))
        graphics.append(_poly([(-gap, 1.5), (-gap, -1.5)], width * 2))
        if spec.kind == "polar_capacitor":
            graphics.append(_arc((gap + 0.6, 1.5), (gap, 0), (gap + 0.6, -1.5), width * 2))
            graphics.append(_poly([(-gap - 1.2, 1.4), (-gap - 0.6, 1.4)], width))
            graphics.append(_poly([(-gap - 0.9, 1.7), (-gap - 0.9, 1.1)], width))
        else:
            graphics.append(_poly([(gap, 1.5), (gap, -1.5)], width * 2))
    elif spec.kind == "inductor":
        step = 2 * half / 4
        for i in range(4):
            x = -half + i * step
            graphics.append(_arc((x, 0), (x + step / 2, step / 2), (x + step, 0), width))
    elif spec.kind in ("diode", "led"):
        graphics.append(_poly([(-half, 0), (half, 0)], width))
        graphics.append(_poly([(-0.8, 1.0), (0.8, 0), (-0.8, -1.0), (-0.8, 1.0)], width))
        graphics.append(_poly([(0.8, 1.0), (0.8, -1.0)], width))
        if spec.kind == "led":
            for dx in (0.0, 0.7):
                graphics.append(_poly([(-0.2 + dx, 1.2), (0.5 + dx, 1.9)], width))
                graphics.append(_poly([(0.1 + dx, 1.9), (0.5 + dx, 1.9), (0.5 + dx, 1.5)], width))
    else:  # crystal
        graphics.append(_poly([(-half, 0), (-0.9, 0)], width))
        graphics.append(_poly([(0.9, 0), (half, 0)], width))
        graphics.append(_poly([(-0.9, 1.2), (-0.9, -1.2)], width))
        graphics.append(_poly([(0.9, 1.2), (0.9, -1.2)], width))
        graphics.append(_poly([(-0.5, 1.4), (0.5, 1.4), (0.5, -1.4), (-0.5, -1.4), (-0.5, 1.4)], width))
    return (-half, -1.5, half, 1.5)
