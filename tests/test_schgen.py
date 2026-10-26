from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibepcb import fixes, schgen, sexpr, symlib

if TYPE_CHECKING:
    from pathlib import Path

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


def _groups(sch: list[Node]) -> dict[tuple[str, str], str]:
    """Connectivity as KiCad sees it: (reference, pin) -> group id."""
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
    for kind in ("label", "global_label"):
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
