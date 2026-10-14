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


def _circuit(tmp_path: Path) -> Path:
    elements: list[dict[str, object]] = []
    port = 0
    for comp, (name, (ftype, pins)) in enumerate(PARTS.items()):
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
        {"type": "source_net", "name": n, "subcircuit_connectivity_map_key": f"{KEY}{k}"} for k, n in NAMED.items()
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
