from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from vibepcb import easyeda, schgen, sexpr, symlib

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

FONT = "(effects (font (size 1.27 1.27)))"


def _pin(kind: str, x: float, y: float, angle: int, name: str, number: str, *, hide: bool = False) -> str:
    return (
        f"(pin {kind} line (at {x} {y} {angle}) (length 2.54){' hide' if hide else ''}"
        f' (name "{name}" {FONT}) (number "{number}" {FONT}))'
    )


# KiCad library layout: top-level symbols one tab in (the index scans for them)
LIBRARY = "\n".join([
    '(kicad_symbol_lib (version 20241209) (generator "test")',
    f'\t(symbol "BASE" (pin_names (offset 1)) (property "Reference" "U" (at 0 6.35 0) {FONT})'
    f' (property "Value" "BASE" (at 0 -8.89 0) {FONT})'
    '  (symbol "BASE_0_1" (rectangle (start -5.08 3.81) (end 5.08 -3.81)'
    "    (stroke (width 0.254) (type default)) (fill (type background))))"
    f'  (symbol "BASE_1_1" {_pin("input", -7.62, 0, 0, "SENSE", "1")} {_pin("power_in", 0, -6.35, 90, "GND", "2")}'
    f' {_pin("no_connect", 7.62, 0, 180, "NC", "3", hide=True)}))',
    f'\t(symbol "TESTCHIPx" (extends "BASE") (property "Reference" "U" (at 0 6.35 0) {FONT})'
    f' (property "Value" "TESTCHIPx" (at 0 -8.89 0) {FONT}))',
    ")",
])  # fmt: skip


def _library(tmp_path: Path) -> symlib.Library:
    lib = tmp_path / "symbols"
    lib.mkdir()
    (lib / "Test.kicad_sym").write_text(LIBRARY, encoding="utf-8")
    return symlib.Library(lib)


def test_library_symbol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(symlib, "CACHE", tmp_path / "cache")
    library = _library(tmp_path)
    assert set(library.index) == {"BASE", "TESTCHIPx"}
    assert symlib.candidates("TESTCHIP1-SOT23", library.index) == ["TESTCHIPx"]  # lowercase x: any character
    assert symlib.candidates("OTHER", library.index) == []

    sym = library.symbol("TESTCHIPx")
    assert sym is not None
    assert [(p.number, p.name, p.angle) for p in sym.pins] == [("1", "SENSE", 0), ("2", "GND", 90), ("3", "NC", 180)]
    assert sym.units() == {1}
    assert sym.bbox() == (-7.62, -6.35, 7.62, 3.81)  # parent's graphics, resolved through ``extends``
    assert library.symbol("TESTCHIPx") is not None  # second time from the cache

    good = {"1": ("SENSE", "S"), "2": ("GND", "GND"), "3": ("NC", None)}
    assert symlib.fits(sym, good) is None
    assert symlib.fits(sym, {"1": ("SENSE", "S"), "2": ("GND", "GND")}) == "pin numbers differ (1)"
    wrong = {"1": ("FOO", "S"), "2": ("BAR", "GND"), "3": ("NC", None)}
    assert symlib.fits(sym, wrong) == "pin names differ (1/3 agree)"
    assert symlib.fits(sym, wrong, names=False) is None  # LCSC symbols: numbers are authoritative

    node = symlib.for_schematic(sym, "vibepcb:U2")
    assert node[1] == "vibepcb:U2"
    assert {str(s[1]) for s in sexpr.children(node, "symbol")} == {"U2_0_1", "U2_1_1"}
    pins = [p for p in sexpr.walk(node) if sexpr.head(p) == "pin"]
    assert {str(p[1]) for p in pins} == {"passive"}  # no pin types: ERC sees only the netlist
    assert not any(str(x) == "hide" or sexpr.head(x) == "hide" for p in pins for x in p)  # lone hidden pin shown


def _easyeda_unit(name: str, number_in: str, number_out: str) -> dict[str, object]:
    return {
        "head": {"x": 400, "y": 300},
        "shape": [
            "PL~380 320 420 300 380 280 380 320~#880000~1~0~none~gge1~0",
            f"P~show~0~{number_in}~360~290~180~gge2~0^^360~290^^M 360 290 h 20~#880000"
            f"^^0~383~293~0~{name}~start~~~#0000FF^^1~374~289~0~{number_in}~end~~~#0000FF^^0~377~290^^0~M 0 0",
            f"P~show~0~{number_out}~440~300~0~gge3~0^^440~300^^M 440 300 h -20~#880000"
            f"^^0~417~303~0~OUT~end~~~#0000FF^^1~426~299~0~{number_out}~start~~~#0000FF^^0~423~300^^0~M 0 0",
            "P~show~4~8~400~260~90~gge4~0^^400~260^^M 400 260 v 20~#FF0000^^0~403~283~270~VDD~end^^1~0~0~0~8~start",
        ]
        if number_in == "2"
        else [
            "PL~380 320 420 300 380 280 380 320~#880000~1~0~none~gge1~0",
            f"P~show~0~{number_in}~360~290~180~gge2~0^^360~290^^M 360 290 h 20~#880000"
            f"^^0~383~293~0~{name}~start~~~#0000FF^^1~374~289~0~{number_in}~end",
            f"P~show~0~{number_out}~440~300~0~gge3~0^^440~300^^M 440 300 h -20~#880000"
            f"^^0~417~303~0~OUT~end~~~#0000FF^^1~426~299~0~{number_out}~start",
        ],
    }


def test_easyeda_symbol() -> None:
    data = {
        "result": {
            "subparts": [{"dataStr": _easyeda_unit("IN-", "2", "1")}, {"dataStr": _easyeda_unit("IN+", "6", "7")}]
        }
    }
    sym = easyeda.to_symbol(data, "C1")
    assert sym is not None
    assert sym.units() == {1, 2}  # a dual op-amp: one unit per sub-part
    pins = {p.number: p for p in sym.pins}
    assert set(pins) == {"1", "2", "6", "7", "8"}
    assert (pins["2"].unit, pins["2"].name, pins["2"].angle) == (1, "IN-", 0)  # from the left, into the body
    assert pins["1"].angle == 180
    assert pins["8"].angle == 270  # from the top (EasyEDA y grows downwards)
    assert (pins["2"].x, pins["2"].y) == (-10.16, 2.54)
    for p in sym.pins:  # every pin on KiCad's 1.27 mm connection grid
        assert abs(p.x / 1.27 - round(p.x / 1.27)) < 1e-6
        assert abs(p.y / 1.27 - round(p.y / 1.27)) < 1e-6
    assert sexpr.child(sym.node, "symbol") is not None
    assert easyeda.to_symbol({"result": {"dataStr": {"head": {}, "shape": []}}}, "C2") is None


def test_generate_with_library_symbol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(symlib, "CACHE", tmp_path / "cache")
    circuit: list[dict[str, Any]] = [
        {"type": "source_component", "source_component_id": "c0", "name": "U2", "ftype": "simple_chip",
         "manufacturer_part_number": "TESTCHIP1"},
        {"type": "source_component", "source_component_id": "c1", "name": "R1", "ftype": "simple_resistor",
         "display_resistance": "1k"},
        {"type": "source_port", "source_port_id": "a", "source_component_id": "c0", "name": "SENSE", "pin_number": 1,
         "subcircuit_connectivity_map_key": "k1"},
        {"type": "source_port", "source_port_id": "b", "source_component_id": "c0", "name": "GND", "pin_number": 2,
         "subcircuit_connectivity_map_key": "k2"},
        {"type": "source_port", "source_port_id": "c", "source_component_id": "c0", "name": "NC", "pin_number": 3},
        {"type": "source_port", "source_port_id": "d", "source_component_id": "c1", "name": "pin1", "pin_number": 1,
         "subcircuit_connectivity_map_key": "k1"},
        {"type": "source_port", "source_port_id": "e", "source_component_id": "c1", "name": "pin2", "pin_number": 2,
         "subcircuit_connectivity_map_key": "k2"},
        {"type": "source_net", "name": "GND", "subcircuit_connectivity_map_key": "k2"},
        {"type": "source_trace", "connected_source_port_ids": ["a", "d"]},
    ]  # fmt: skip
    path = tmp_path / "board.circuit.json"
    path.write_text(json.dumps(circuit), encoding="utf-8")
    text, notes = schgen.generate_with_notes(path, "board", None, _library(tmp_path))
    assert notes == []
    sch = sexpr.loads(text)
    lib = sexpr.child(sch, "lib_symbols")
    assert lib is not None
    u2 = next(s for s in sexpr.children(lib, "symbol") if str(s[1]) == "vibepcb:U2")
    assert any(sexpr.head(x) == "rectangle" for x in sexpr.walk(u2))  # the library's body, not a generated box
    assert schgen.connectivity_errors(text, schgen._parts(circuit)) == []
    # R1 hangs off SENSE by wire, the GND pin at the bottom gets its rail symbol
    rails = [
        s for s in sexpr.children(sch, "symbol") if (sexpr.property_value(s, "Reference") or "").startswith("#PWR")
    ]
    assert {sexpr.property_value(s, "Value") for s in rails} == {"GND"}
