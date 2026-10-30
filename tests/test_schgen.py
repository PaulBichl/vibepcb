from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibepcb import fixes, schgen, sexpr, symlib

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vibepcb.sexpr import Node

KEY = "connectivity_net"
# part -> ftype, [(pin name, net)]; nets: 0 VCC, 1 OUT, 2 FB, 3 GND, 4 unnamed (U2 SENSE - R3 - D1)
PARTS: dict[str, tuple[str, list[tuple[str, int | None]]]] = {
    "U1": ("simple_chip", [("VCC", 0), ("OUT", 1), ("FB", 2), ("GND", 3), ("NC", None), ("VCC2", 0), ("GND2", 3)]),
    "R1": ("simple_resistor", [("pin1", 1), ("pin2", 2)]),
    "R2": ("simple_resistor", [("pin1", 2), ("pin2", 3)]),
    "C1": ("simple_capacitor", [("pin1", 0), ("pin2", 3)]),
    "C2": ("simple_capacitor", [("pin1", 0), ("pin2", 3)]),
    "J1": ("simple_chip", [("SIG", 1), ("GND", 3)]),
    "U2": ("simple_chip", [("SENSE", 4), ("GND", 3), ("NC", None)]),
    "R3": ("simple_resistor", [("pin1", 1), ("pin2", 4)]),
    "D1": ("simple_diode", [("cathode", 3), ("anode", 4)]),
}
NAMED = {0: "VCC", 1: "OUT", 2: "FB", 3: "GND"}
TSX = """
    {/* ===== power ===== */}
    <chip name="U1" />
    <R name="R1" v="10k" /> <R name="R2" v="10k" />
    <C name="C1" v="100nF" /> <C name="C2" v="1uF" />
    {/* ===== io ===== */}
    <chip name="J1" /> <chip name="U2" /> <R name="R3" v="1k" /> <led name="D1" />
"""


def _circuit(
    tmp_path: Path,
    parts: dict[str, tuple[str, list[tuple[str, int | None]]]] | None = None,
    named: dict[int, str] | None = None,
) -> Path:
    elements: list[dict[str, object]] = []
    port = 0
    for comp, (name, (ftype, pins)) in enumerate((PARTS if parts is None else parts).items()):
        elements.append({"type": "source_component", "source_component_id": f"c{comp}", "name": name, "ftype": ftype})
        for number, (pin, net) in enumerate(pins, start=1):
            p: dict[str, object] = {
                "type": "source_port",
                "source_port_id": f"p{port}",
                "name": pin,
                "pin_number": number,
                "source_component_id": f"c{comp}",
            }
            if net is not None:
                p["subcircuit_connectivity_map_key"] = f"{KEY}{net}"
            elements.append(p)
            port += 1
    # tscircuit's copy of a pin for an extra pad: no symbol pin
    elements.append(
        {"type": "source_port", "source_port_id": "px", "name": "pin1_internal_1", "source_component_id": "c0"}
    )
    elements += [
        {"type": "source_net", "name": n, "subcircuit_connectivity_map_key": f"{KEY}{k}"}
        for k, n in (NAMED if named is None else named).items()
    ]
    elements.append({"type": "source_trace", "connected_source_port_ids": ["p0"]})  # the unnamed net is traced
    elements.append({"type": "source_trace", "connected_source_port_ids": [f"p{port - 1}"]})
    path = tmp_path / "board.circuit.json"
    path.write_text(json.dumps(elements), encoding="utf-8")
    return path


def _no_library(tmp_path: Path) -> symlib.Library:
    (tmp_path / "nolib").mkdir(exist_ok=True)
    return symlib.Library(tmp_path / "nolib")  # tests do not depend on the installed KiCad library


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "index.circuit.tsx"
    path.write_text(TSX, encoding="utf-8")
    return path


def _at(node: list[Node]) -> tuple[float, float]:
    at = sexpr.child(node, "at")
    assert at is not None
    return round(float(str(at[1])), 4), round(float(str(at[2])), 4)


def _groups(sch: list[Node], *, labels: bool = True) -> dict[tuple[str, str], str]:
    """Connectivity as KiCad sees it: (reference, pin) -> group id (``labels=False``: by wire only)."""
    parent: dict[object, object] = {}

    def find(a: object) -> object:
        parent.setdefault(a, a)
        while parent[a] != a:
            a = parent[a]
        return a

    def union(a: object, b: object) -> None:
        parent[find(a)] = find(b)

    for wire in sexpr.children(sch, "wire"):
        a, b = (
            (round(float(str(xy[1])), 4), round(float(str(xy[2])), 4))
            for xy in sexpr.walk(wire)
            if sexpr.head(xy) == "xy"
        )
        union(a, b)
    for kind in ("label", "global_label") if labels else ():
        for label in sexpr.children(sch, kind):
            union(_at(label), ("net", str(label[1])))
    pins = fixes._pin_positions(sch)
    for (ref, number), point in pins.items():
        union((ref, number), point)
        if ref.startswith("#PWR"):
            symbol = next(s for s in sexpr.children(sch, "symbol") if sexpr.property_value(s, "Reference") == ref)
            union(point, ("net", sexpr.property_value(symbol, "Value")))
    return {pin: str(find(pin)) for pin in pins if not pin[0].startswith("#")}


def test_sections(tmp_path: Path) -> None:
    assert schgen.sections(_source(tmp_path)) == {
        "U1": "power", "R1": "power", "R2": "power", "C1": "power", "C2": "power",
        "J1": "io", "U2": "io", "R3": "io", "D1": "io",
    }  # fmt: skip
    assert schgen.sections(None) == {}


def test_generate(tmp_path: Path) -> None:
    sch_path = tmp_path / "board.kicad_sch"
    schgen.write(_circuit(tmp_path), sch_path, "board", _source(tmp_path), _no_library(tmp_path))
    sch = sexpr.load(sch_path)

    symbols = {sexpr.property_value(s, "Reference"): s for s in sexpr.children(sch, "symbol")}
    assert set(PARTS) < set(symbols)
    assert sexpr.property_value(symbols["U1"], "Value") == "U1"
    rails = sorted(sexpr.property_value(s, "Value") or "" for r, s in symbols.items() if r and r.startswith("#PWR"))
    assert set(rails) == {"VCC", "GND"}  # VCC: 3 pins -> rail; every GND end its own symbol
    # U1's two VCC pins share one rail symbol (joined by a wire); C1, C2 hang off that pin by wire
    assert rails.count("VCC") == 1
    positions = fixes._pin_positions(sch)
    wire_ends = {(round(float(str(xy[1])), 4), round(float(str(xy[2])), 4)) for w in sexpr.children(sch, "wire")
                 for xy in sexpr.walk(w) if sexpr.head(xy) == "xy"}  # fmt: skip
    assert positions["C1", "1"] in wire_ends
    assert positions["C2", "1"] in wire_ends
    assert len(list(sexpr.children(sch, "rectangle"))) == 2  # one frame per TSX section
    # global labels only (a local one would name the net "/FB"); OUT is in both blocks: double arrow
    shapes = {(str(n[1]), str(sexpr.child(n, "shape")[1])) for n in sexpr.children(sch, "global_label")}  # type: ignore[index]
    assert ("OUT", "bidirectional") in shapes
    assert ("FB", "passive") in shapes
    assert not list(sexpr.children(sch, "label"))

    groups = _groups(sch)
    nets: dict[int, set[str]] = {}
    for name, (_, pins) in PARTS.items():
        for number, (_, net) in enumerate(pins, start=1):
            if net is not None:
                nets.setdefault(net, set()).add(groups[name, str(number)])
    assert all(len(g) == 1 for g in nets.values())  # every net is one group
    assert len({next(iter(g)) for g in nets.values()}) == len(nets)  # and no two nets are joined
    assert schgen.pin_net_names(json.loads(_circuit(tmp_path).read_text()))["R3", "2"] == "U2_SENSE"
    assert sexpr.property_value(symbols["U2"], "Value") == "U2"  # no value: shown hidden
    value = sexpr.property_node(symbols["U2"], "Value")
    assert value is not None
    assert "hide" in str(value)
    assert ("U1", "5") in groups  # the NC pin has a symbol pin (fix adds the no-connect flag)
    assert len(groups) == sum(len(pins) for _, pins in PARTS.values())  # internal pad copies get no pin

    assert schgen.generate(_circuit(tmp_path), "board", _source(tmp_path), _no_library(tmp_path)) == schgen.generate(
        _circuit(tmp_path), "board", _source(tmp_path), _no_library(tmp_path)
    )  # deterministic


def test_rails_need_a_power_pin() -> None:
    def part(name: str, kind: str, *pins: tuple[str, str]) -> schgen.Part:
        return schgen.Part(name, name, kind, [schgen.Pin(str(i), p, n) for i, (p, n) in enumerate(pins, start=1)])

    parts = [
        part("U1", "chip", ("VDD", "V3V3"), ("IN", "VSET"), ("GND", "AGND")),
        part("U2", "chip", ("OUT", "VSET"), ("AVDD", "V3V3")),
        part("C1", "C", ("pin1", "V3V3"), ("pin2", "AGND")),
        part("R1", "R", ("pin1", "VSET"), ("pin2", "AGND")),
    ]
    circuit = [{"type": "source_net", "name": n} for n in ("V3V3", "VSET", "AGND")]
    assert schgen._rails(circuit, parts) == {"V3V3", "AGND"}  # VSET: 3 pins, V-name, but a signal


def test_negative_rails() -> None:
    part = schgen.Part("U1", "U1", "chip", [schgen.Pin("1", "VPOS", "VP"), schgen.Pin("2", "VNEG", "VN"),
                                            schgen.Pin("3", "GND", "GND")])  # fmt: skip
    assert schgen._negative({"VP", "VN", "GND", "-12V"}, [part]) == {"VN", "-12V"}


def test_connectivity_errors_see_pins_on_wires(tmp_path: Path) -> None:
    text = schgen.generate(_circuit(tmp_path), "board", _source(tmp_path), _no_library(tmp_path))
    parts = schgen._parts(json.loads(_circuit(tmp_path).read_text()))
    assert schgen.connectivity_errors(text, parts) == []
    x, y = fixes._pin_positions(sexpr.loads(text))["U1", "5"]  # the NC pin
    # a VCC-labelled wire passing straight over the pin end: KiCad connects it there
    short = (
        f"(wire (pts (xy {x - 5.08} {y}) (xy {x + 5.08} {y})) (stroke (width 0) (type default)) (uuid {'0' * 8}-0000-0000-0000-{'0' * 12}))"
        f' (global_label "VCC" (shape passive) (at {x - 5.08} {y} 180) (effects (font (size 1.27 1.27)) (justify right))'
        f" (uuid {'0' * 8}-0000-0000-0000-{'0' * 11}1) (fields_autoplaced no))"
    )
    tampered = text.replace("(sheet_instances", f"{short}\n(sheet_instances")
    assert schgen.connectivity_errors(tampered, parts) == ["U1 pin 5 on VCC"]


def test_named_trace_names_its_net() -> None:
    # an unnamed pad-to-pad net joined by <trace name="SD_DQ0"> is called SD_DQ0 (not U1_B16 / Net-(...)),
    # so net classes, length groups and diff pairs can refer to it; a trace to a named net keeps that name
    comp = [
        {"type": "source_component", "source_component_id": "c0", "name": "U1", "ftype": "simple_chip"},
        {"type": "source_component", "source_component_id": "c1", "name": "U2", "ftype": "simple_chip"},
    ]
    ports = [
        {
            "type": "source_port",
            "source_port_id": f"p{i}",
            "name": n,
            "pin_number": i % 3 + 1,
            "source_component_id": c,
            "subcircuit_connectivity_map_key": k,
        }
        for i, (n, c, k) in enumerate(
            [
                ("B16", "c0", "k1"),
                ("A1", "c0", "kg"),
                ("X", "c0", "k9"),
                ("DQ0", "c1", "k1"),
                ("VSS", "c1", "kg"),
                ("Y", "c1", "k9"),
            ]
        )
    ]
    traces = [
        {
            "type": "source_trace",
            "source_trace_id": "t0",
            "name": "SD_DQ0",
            "subcircuit_connectivity_map_key": "k1",
            "connected_source_port_ids": ["p0", "p3"],
            "connected_source_net_ids": [],
        },
        {
            "type": "source_trace",
            "source_trace_id": "t1",
            "name": "GND_A1",
            "subcircuit_connectivity_map_key": "kg",
            "connected_source_port_ids": ["p1"],
            "connected_source_net_ids": ["n0"],
        },
        {
            "type": "source_trace",
            "source_trace_id": "t2",
            "name": "source_trace_2",
            "subcircuit_connectivity_map_key": "k9",
            "connected_source_port_ids": ["p2", "p5"],
            "connected_source_net_ids": [],
        },
    ]
    nets = [{"type": "source_net", "source_net_id": "n0", "name": "GND", "subcircuit_connectivity_map_key": "kg"}]
    names = schgen.pin_net_names([*comp, *ports, *traces, *nets])
    assert names["U1", "1"] == "SD_DQ0"
    assert names["U2", "1"] == "SD_DQ0"
    assert names["U1", "2"] == "GND"
    assert names["U1", "3"] == "U1_X"  # tscircuit's own trace id is no name


def _symbols(sch: list[Node]) -> dict[str, list[Node]]:
    return {sexpr.property_value(s, "Reference") or "": s for s in sexpr.children(sch, "symbol")}


def _power_at(sch: list[Node], point: tuple[float, float]) -> tuple[str, int]:
    """(rail name, angle) of the rail symbol whose pin sits on ``point``."""
    symbols = _symbols(sch)
    for (ref, _), at in fixes._pin_positions(sch).items():
        if ref.startswith("#PWR") and at == point:
            angle = sexpr.child(symbols[ref], "at")
            assert angle is not None
            return sexpr.property_value(symbols[ref], "Value") or "", round(float(str(angle[3])))
    msg = f"no rail symbol on {point}"
    raise AssertionError(msg)


def test_decoupling_caps_stand_at_their_supply_pin(tmp_path: Path) -> None:
    # a box chip: the VCC pin's caps stand side by side on a wire out of the pin, each with GND below it
    text = schgen.generate(_circuit(tmp_path), "board", _source(tmp_path), _no_library(tmp_path))
    sch = sexpr.loads(text)
    pins = fixes._pin_positions(sch)
    vcc = pins["U1", "6"]  # VCC and VCC2 are joined, the caps go on the run's last pin
    for cap in ("C1", "C2"):
        top, bottom = sorted([pins[cap, "1"], pins[cap, "2"]], key=lambda p: p[1])
        assert top[0] == bottom[0]  # standing
        assert top[1] == vcc[1]  # on the pin's row ...
        assert abs(top[0] - vcc[0]) <= 8 * schgen.GRID  # ... within 8 grid units of the pin
        assert _power_at(sch, bottom) == ("GND", 0)  # GND below the cap, pointing down
    assert schgen.connectivity_errors(text, schgen._parts(json.loads(_circuit(tmp_path).read_text()))) == []


def test_rail_to_rail_chain_is_one_column(tmp_path: Path) -> None:
    # LED + resistor between VCC and GND (no chip on their middle net): one standing column, VCC on top
    parts: dict[str, tuple[str, list[tuple[str, int | None]]]] = {
        "U1": ("simple_chip", [("VCC", 0), ("OUT", 2), ("GND", 1)]),
        "R1": ("simple_resistor", [("pin1", 0), ("pin2", 3)]),
        "D1": ("simple_led", [("anode", 3), ("cathode", 1)]),
        "J1": ("simple_chip", [("VCC", 0), ("SIG", 2), ("GND", 1)]),
    }
    circuit = _circuit(tmp_path, parts, {0: "VCC", 1: "GND", 2: "OUT", 3: "LED_A"})
    sch = sexpr.loads(schgen.generate(circuit, "board", None, _no_library(tmp_path)))
    pins = fixes._pin_positions(sch)
    xs = {pins[ref, n][0] for ref in ("R1", "D1") for n in ("1", "2")}
    assert len(xs) == 1  # one column
    top = min((pins[ref, n] for ref in ("R1", "D1") for n in ("1", "2")), key=lambda p: p[1])
    bottom = max((pins[ref, n] for ref in ("R1", "D1") for n in ("1", "2")), key=lambda p: p[1])
    assert _power_at(sch, top)[0] == "VCC"
    assert _power_at(sch, bottom) == ("GND", 0)


def test_one_pin_part_is_a_small_symbol(tmp_path: Path) -> None:
    parts: dict[str, tuple[str, list[tuple[str, int | None]]]] = {
        "U1": ("simple_chip", [("VCC", 0), ("OUT", 2), ("GND", 1)]),
        "J1": ("simple_chip", [("VCC", 0), ("SIG", 2), ("GND", 1)]),
        "TP1": ("simple_test_point", [("pin1", 0)]),
    }
    sch = sexpr.loads(schgen.generate(_circuit(tmp_path, parts, {0: "VCC", 1: "GND", 2: "OUT"}), "b", None,
                                      _no_library(tmp_path)))  # fmt: skip
    lib = sexpr.child(sch, "lib_symbols")
    assert lib is not None
    tp = next(s for s in sexpr.children(lib, "symbol") if str(s[1]) == "vibepcb:TP1")
    heads = {sexpr.head(x) for x in sexpr.walk(tp)}
    assert "circle" in heads
    assert "rectangle" not in heads  # no box


def test_rails_turn_only_into_free_room() -> None:
    sheet = schgen.Sheet("t", set(), set())
    sheet.wire(0, 0, 2.54, 0)  # a pin's wire to the right, its GND rail at the end
    sheet.rail("GND", 2.54, 0, "right")
    sheet.wire(0, 10.16, 2.54, 10.16)
    sheet.rail("GND", 2.54, 10.16, "right")
    sheet.wire(0, 12.7, 10.16, 12.7)  # the next pin's wire right under the second one
    sheet.rail("VCC", 0, 30, "down")  # a supply arriving from above: upside down, turns sideways
    sheet.finish()
    angles = {
        sexpr.property_value(n, "Value") + str(round(float(str(sexpr.child(n, "at")[2])))): sexpr.child(n, "at")[3]  # type: ignore[index, operator]
        for n in (sexpr.loads(item) for item in sheet.items)
        if sexpr.head(n) == "symbol"
    }
    assert str(angles["GND0"]) == "0"  # free below: points down
    assert str(angles["GND10"]) == "90"  # a wire below: stays pointing right
    assert str(angles["VCC30"]) in {"90", "270"}  # sideways, not upside down


def test_unsectioned_decoupling_joins_its_chip() -> None:
    def part(name: str, kind: str, pos: tuple[float, float], *pins: tuple[str, str]) -> schgen.Part:
        pin_list = [schgen.Pin(str(i), p, n) for i, (p, n) in enumerate(pins, start=1)]
        return schgen.Part(name, name, kind, pin_list, pos=pos)

    parts = [
        part("U1", "chip", (0, 0), ("VDD", "V3V3"), ("GND", "GND")),
        part("U2", "chip", (20, 0), ("VDD", "V3V3"), ("GND", "GND")),
        part("C1", "C", (19, 1), ("pin1", "V3V3"), ("pin2", "GND")),
        part("R1", "R", (19, 1), ("pin1", "V3V3"), ("pin2", "SIG")),
    ]
    section_of = {"U1": "mcu", "U2": "sensor"}
    schgen._adopt_decoupling(parts, section_of, {"V3V3", "GND"})
    assert section_of["C1"] == "sensor"  # the nearest chip its supply powers
    assert "R1" not in section_of  # not a rail-to-rail part


def _lib_symbol(name: str, left: list[str], right: list[str]) -> symlib.LibSymbol:
    """A DIP-like library symbol: ``left`` pins top-down on the left, ``right`` bottom-up on the right."""
    font = "(effects (font (size 1.27 1.27)))"
    n = max(len(left), len(right))
    top = (n - 1) * schgen.GRID / 2
    pins = [
        f'(pin passive line (at -10.16 {top - i * schgen.GRID:g} 0) (length 2.54) (name "{p}" {font})'
        f' (number "{i + 1}" {font}))'
        for i, p in enumerate(left)
    ] + [
        f'(pin passive line (at 10.16 {top - (n - 1 - i) * schgen.GRID:g} 180) (length 2.54) (name "{p}" {font})'
        f' (number "{len(left) + i + 1}" {font}))'
        for i, p in enumerate(right)
    ]
    node = sexpr.loads(
        f'(symbol "{name}" (property "Reference" "U" (at 0 {top + 2.54:g} 0) {font})'
        f' (property "Value" "{name}" (at 0 {-top - 2.54:g} 0) {font})'
        f' (symbol "{name}_0_1" (rectangle (start -7.62 {top + 1.27:g}) (end 7.62 {-top - 1.27:g})'
        " (stroke (width 0.254) (type default)) (fill (type background))))"
        f' (symbol "{name}_1_1" {" ".join(pins)}))'
    )
    return symlib.LibSymbol(name, node, symlib.pins_of(node))


def test_library_pin_trees_stay_at_their_pins(tmp_path: Path) -> None:
    # a USB-serial chip (CH340C): series resistors on TXD / RXD, a cap on V3, labels on D+ / D-: every part
    # wired to its own pin (rows fanned out), not moved off into a column of labels; V3's cap stands at the pin
    parts: dict[str, tuple[str, list[tuple[str, int | None]]]] = {
        "U1": (
            "simple_chip",
            [
                ("GND", 0),
                ("TXD", 1),
                ("RXD", 2),
                ("V3", 3),
                ("DP", 4),
                ("DN", 5),
                ("NC", None),
                ("OUT", None),
                ("CTS", None),
                ("DSR", None),
                ("RI", None),
                ("DCD", None),
                ("DTR", None),
                ("RTS", None),
                ("R232", None),
                ("VCC", 6),
            ],
        ),
        "R9": ("simple_resistor", [("pin1", 1), ("pin2", 7)]),
        "R10": ("simple_resistor", [("pin1", 2), ("pin2", 8)]),
        "C30": ("simple_capacitor", [("pin1", 3), ("pin2", 0)]),
        "J1": ("simple_chip", [("TX", 7), ("RX", 8), ("DP", 4), ("DN", 5), ("VCC", 6), ("GND", 0)]),
    }
    named = {0: "GND", 1: "CH340_TXD", 2: "CH340_RXD", 3: "CH340_V3", 4: "USB_P", 5: "USB_N", 6: "VCC", 7: "TXD",
             8: "RXD"}  # fmt: skip
    path = _circuit(tmp_path, parts, named)
    circuit = json.loads(path.read_text())
    chip = _lib_symbol("CH340C", ["GND", "TXD", "RXD", "V3", "DP", "DN", "NC", "OUT"],
                       ["CTS", "DSR", "RI", "DCD", "DTR", "RTS", "R232", "VCC"])  # fmt: skip
    all_parts = schgen._parts(circuit)
    text = schgen._render(circuit, all_parts, {"U1": chip}, "board", None)
    assert schgen.connectivity_errors(text, all_parts) == []
    sch = sexpr.loads(text)
    pins, wired = fixes._pin_positions(sch), _groups(sch, labels=False)
    for ref, pin in (("R9", "2"), ("R10", "3"), ("C30", "4")):
        near = next((n for n in ("1", "2") if wired[ref, n] == wired["U1", pin]), None)
        assert near is not None, f"{ref} is not wired to U1 pin {pin}"
        # beside its pin: left of the chip, within a fan-out of its pin row
        assert pins[ref, near][0] < pins["U1", pin][0]
        assert abs(pins[ref, near][1] - pins["U1", pin][1]) <= 6 * schgen.GRID + 1e-6
    c_top, c_bottom = sorted([pins["C30", "1"], pins["C30", "2"]], key=lambda p: p[1])
    assert c_top[0] == c_bottom[0]  # the V3 cap stands ...
    assert _power_at(sch, c_bottom) == ("GND", 0)  # ... with GND below it
    # one wire path from the pin to the resistor: no label of CH340_TXD between them breaks it
    assert sum(1 for g in sexpr.children(sch, "global_label") if str(g[1]) == "CH340_TXD") == 1


def test_first_pin_of_a_net_gets_no_stub(tmp_path: Path) -> None:
    # a net on two neighbouring pins with nothing else on it in the block: both labels in line
    parts: dict[str, tuple[str, list[tuple[str, int | None]]]] = {
        "J1": ("simple_chip", [("DN1", 0), ("DP1", 1), ("DN2", 0), ("DP2", 1)]),
        "U1": ("simple_chip", [("DN", 0), ("DP", 1)]),
    }
    path = _circuit(tmp_path, parts, {0: "USB_N", 1: "USB_P"})
    tsx = tmp_path / "split.circuit.tsx"
    tsx.write_text('{/* ===== a ===== */}\n<chip name="J1" />\n{/* ===== b ===== */}\n<chip name="U1" />\n')
    sch = sexpr.loads(schgen.generate(path, "board", tsx, _no_library(tmp_path)))
    pins = fixes._pin_positions(sch)
    labels = [_at(g) for g in sexpr.children(sch, "global_label")]
    reach = {
        min(abs(x - pins["J1", n][0]) for x, y in labels if y == pins["J1", n][1]) for n in "1234"
    }  # each pin's label: how far from the pin end
    assert reach == {0.0}


def test_text_width_matches_kicad_font() -> None:
    # KiCad draws "TYPE-C-31-M-12" 18.4 mm wide at 1.27 mm; lower case is narrower
    assert abs(schgen._text_w("TYPE-C-31-M-12") - 18.4) < 1.5
    assert schgen._text_w("100nF") < schgen._text_w("100NF")


def test_part_stands_at_the_pin_of_the_chip_it_sits_next_to(tmp_path: Path) -> None:
    # R1 joins U1 OUT to FILT (U1 draws FILT first); C1 (FILT to GND) sits next to U2 on the PCB: it stands
    # at U2's FILT pin, not as a loose piece behind a label
    parts: dict[str, tuple[str, list[tuple[str, int | None]]]] = {
        "U1": ("simple_chip", [("VCC", 0), ("OUT", 2), ("GND", 1)]),
        "R1": ("simple_resistor", [("pin1", 2), ("pin2", 3)]),
        "U2": ("simple_chip", [("VCC", 0), ("IN", 3), ("GND", 1)]),
        "C1": ("simple_capacitor", [("pin1", 3), ("pin2", 1)]),
    }
    circuit = _circuit(tmp_path, parts, {0: "VCC", 1: "GND", 2: "OUT", 3: "FILT"})
    elements = json.loads(circuit.read_text())
    centres = {"U1": (0, 0), "R1": (3, 0), "U2": (20, 0), "C1": (18, 0)}
    ids = {e["name"]: e["source_component_id"] for e in elements if e["type"] == "source_component"}
    elements += [
        {"type": "pcb_component", "source_component_id": ids[n], "center": {"x": x, "y": y}}
        for n, (x, y) in centres.items()
    ]
    circuit.write_text(json.dumps(elements))
    text = schgen.generate(circuit, "board", None, _no_library(tmp_path))
    sch = sexpr.loads(text)
    by_wire = _groups(sch, labels=False)
    assert by_wire["C1", "1"] == by_wire["U2", "2"]  # wired to U2's IN pin
    assert by_wire["C1", "1"] != by_wire["R1", "2"]  # R1's end is joined by the label only
    assert schgen.connectivity_errors(text, schgen._parts(elements)) == []


def test_blocks_take_their_own_shapes_for_the_smallest_sheet() -> None:
    # A's least-area shape (200 x 110) leaves no room for B on A4 (stacked 175 mm high, side by side 330 mm
    # wide); A's wide shape (250 x 95) does: B fits underneath, clear of the title block
    shapes: list[list[schgen.Shape]] = [[([], 200.0, 110.0), ([], 250.0, 95.0)], [([], 125.0, 60.0)]]
    choice, spots = schgen._arrange(shapes)
    assert choice == [1, 0]
    sizes = [(250.0, 95.0), (125.0, 60.0)]
    w = max(x + bw for (bw, _), (x, _) in zip(sizes, spots, strict=True))
    h = max(y + bh for (_, bh), (_, y) in zip(sizes, spots, strict=True))
    assert schgen._misfit(sizes, spots, w, h, schgen._room(schgen.PAPERS[0])) == 0  # A4
    (ax, ay), (bx, by) = spots
    assert not schgen._overlap((ax, ay, ax + 250, ay + 95), (bx, by, bx + 125, by + 60))


def test_block_shapes_drop_dominated_ones() -> None:
    options: dict[tuple[float, float], schgen.Spots] = {(100.0, 50.0): [], (120.0, 60.0): [], (60.0, 90.0): []}
    assert [(w, h) for _, w, h in schgen._shape_choices(options)] == [(100.0, 50.0), (60.0, 90.0)]


def test_moved_tree_level_with_its_pin_is_wired_to_it(tmp_path: Path) -> None:
    # an MCU-like pin row (14 pins): the LED + resistor trees on A5 / A6 would push their neighbours, so they
    # move beside the chip; A5's stands level with its pin and nothing crosses the row: one wire from the
    # pin, the net's one label on the tree. A6's tree had to move down: it keeps a label pair.
    named = {0: "GND", 1: "LED_A", 2: "LED_B", 3: "VCC", 4: "N_A", 5: "N_B"}
    left = []
    for i in range(1, 15):
        net = {5: 1, 6: 2}.get(i, 10 + i)
        left.append((f"A{i}", net))
        named.setdefault(net, f"SIG{i}")
    parts: dict[str, tuple[str, list[tuple[str, int | None]]]] = {
        "U1": ("simple_chip", [*left, ("VCC", 3), ("GND", 0)]),
        "J1": ("simple_chip", [(f"P{i}", n) for i, (_, n) in enumerate(left, 1) if n >= 10]),
        "R1": ("simple_resistor", [("pin1", 1), ("pin2", 4)]),
        "D1": ("simple_diode", [("anode", 4), ("cathode", 0)]),
        "R2": ("simple_resistor", [("pin1", 2), ("pin2", 5)]),
        "D2": ("simple_diode", [("anode", 5), ("cathode", 0)]),
    }
    path = _circuit(tmp_path, parts, named)
    circuit = json.loads(path.read_text())
    chip = _lib_symbol("MCU", [p for p, _ in left], ["GND", "VCC"])
    all_parts = schgen._parts(circuit)
    text = schgen._render(circuit, all_parts, {"U1": chip}, "board", None)
    assert schgen.connectivity_errors(text, all_parts) == []
    sch = sexpr.loads(text)
    pins, wired = fixes._pin_positions(sch), _groups(sch, labels=False)
    labels = [str(g[1]) for g in sexpr.children(sch, "global_label")]
    assert wired["R1", "1"] == wired["U1", "5"]
    assert pins["R1", "1"][0] < pins["U1", "5"][0]  # beside the chip, on the pin's side
    assert labels.count("LED_A") == 1
    assert wired["R2", "1"] != wired["U1", "6"]
    assert labels.count("LED_B") == 2


def _top_symbol(name: str, top: list[str]) -> symlib.LibSymbol:
    """A display-like library symbol: ``top`` pins leaving upwards."""
    font = "(effects (font (size 1.27 1.27)))"
    half = len(top) * schgen.GRID / 2
    pins = [
        f'(pin passive line (at {-half + (i + 0.5) * schgen.GRID:g} 7.62 270) (length 2.54) (name "{p}" {font})'
        f' (number "{i + 1}" {font}))'
        for i, p in enumerate(top)
    ]
    node = sexpr.loads(
        f'(symbol "{name}" (property "Reference" "U" (at {half + 1.27:g} 6.35 0) {font})'
        f' (property "Value" "{name}" (at 0 -6.35 0) {font})'
        f' (symbol "{name}_0_1" (rectangle (start {-half:g} 5.08) (end {half:g} -5.08)'
        " (stroke (width 0.254) (type default)) (fill (type background))))"
        f' (symbol "{name}_1_1" {" ".join(pins)}))'
    )
    return symlib.LibSymbol(name, node, symlib.pins_of(node))


def test_unframed_drawing_stays_inside_the_sheet_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # no TSX sections: no frames, whose corners would tell fit_schematic_sheet the drawing's extent; it
    # places the drawing by its anchor points, so the long labels and texts of three displays stick out of
    # the sheet frame at first: the drawing is laid out again in a smaller room until they do not
    parts: dict[str, tuple[str, list[tuple[str, int | None]]]] = {}
    named: dict[int, str] = {}
    for d in range(1, 4):
        parts[f"U{d}"] = ("simple_chip", [(f"S{k}", k + 10 * (d - 1)) for k in range(1, 9)])
        named |= {k + 10 * (d - 1): f"DISPLAY_SEGMENT_{k}_{d}" for k in range(1, 9)}
    for d in range(1, 4):
        parts[f"J{d}"] = ("simple_chip", [(f"P{k}", k + 10 * (d - 1)) for k in range(1, 9)])
    path = _circuit(tmp_path, parts, named)
    circuit = json.loads(path.read_text())
    symbols = {f"U{d}": _top_symbol(f"DISP{d}", [f"S{k}" for k in range(1, 9)]) for d in range(1, 4)}
    overflow = schgen._sheet_overflow
    tries: list[tuple[float, float]] = []

    def spy(text: str, sheet: schgen.Sheet) -> tuple[float, float]:
        tries.append(overflow(text, sheet))
        return tries[-1]

    monkeypatch.setattr(schgen, "_sheet_overflow", spy)
    all_parts = schgen._parts(circuit)
    text = schgen._render(circuit, all_parts, symbols, "board", None)
    assert schgen.connectivity_errors(text, all_parts) == []
    assert len(tries) > 1  # the first layout stuck out
    assert tries[-1] == (0.0, 0.0)
