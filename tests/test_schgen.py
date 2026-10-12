from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibepcb import fixes, schgen, sexpr

if TYPE_CHECKING:
    from pathlib import Path

KEY = "connectivity_net"


def _circuit(tmp_path: Path) -> Path:
    def port(i: int, comp: int, name: str, net: int | None) -> dict[str, object]:
        p: dict[str, object] = {
            "type": "source_port",
            "source_port_id": f"p{i}",
            "name": name,
            "pin_number": int(name[-1]) if name.startswith("pin") else i,
            "source_component_id": f"c{comp}",
        }
        if net is not None:
            p["subcircuit_connectivity_map_key"] = f"{KEY}{net}"
        return p

    elements = [
        {"type": "source_component", "source_component_id": "c0", "name": "R1", "display_resistance": "10k"},
        {"type": "source_component", "source_component_id": "c1", "name": "U1", "manufacturer_part_number": "XYZ"},
        port(0, 0, "pin1", 0),
        port(1, 0, "pin2", 1),
        port(2, 1, "VCC", 0),
        port(3, 1, "OUT", 1),
        port(4, 1, "NC", 2),  # only on a net key of its own: not connected
        {"type": "source_net", "name": "VCC", "subcircuit_connectivity_map_key": f"{KEY}0"},
        {"type": "source_trace", "connected_source_port_ids": ["p1", "p3"]},
    ]
    path = tmp_path / "board.circuit.json"
    path.write_text(json.dumps(elements), encoding="utf-8")
    return path


def test_generate(tmp_path: Path) -> None:
    sch_path = tmp_path / "board.kicad_sch"
    sch_path.write_text("(kicad_sch (version 20250114) (lib_symbols))", encoding="utf-8")
    assert not schgen.has_symbols(sch_path)
    schgen.write(_circuit(tmp_path), sch_path, "board")
    assert schgen.has_symbols(sch_path)
    sch = sexpr.load(sch_path)

    symbols = {sexpr.property_value(s, "Reference"): s for s in sexpr.children(sch, "symbol")}
    assert set(symbols) == {"R1", "U1"}
    assert sexpr.property_value(symbols["R1"], "Value") == "10k"
    assert sexpr.property_value(symbols["U1"], "Value") == "XYZ"

    labels = sorted(str(n[1]) for n in sexpr.children(sch, "global_label"))
    assert labels == ["Net-(R1-Pad2)", "Net-(R1-Pad2)", "VCC", "VCC"]  # the NC pin gets no label
    # every label sits on a pin end: same coordinates as fixes see the pins
    pins = fixes._pin_positions(sch)
    ends = {(round(x, 3), round(y, 3)) for x, y in pins.values()}
    for label in sexpr.children(sch, "global_label"):
        at = sexpr.child(label, "at")
        assert at is not None
        assert (round(float(str(at[1])), 3), round(float(str(at[2])), 3)) in ends
    assert schgen.generate(_circuit(tmp_path), "board") == schgen.generate(_circuit(tmp_path), "board")  # deterministic
