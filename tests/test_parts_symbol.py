from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from vibepcb import sexpr
from vibepcb.parts import spec, standard, symbol

DATA = Path(__file__).parent / "data" / "parts"
STD = standard.load()


def build(name: str) -> symbol.Symbol:
    return symbol.build(spec.load(DATA / f"{name}.toml"), STD, "FP")


def _on_grid(v: float, grid: float = 2.0) -> bool:
    return abs(v / grid - round(v / grid)) < 1e-6


@pytest.mark.parametrize("part", ["LM358DR", "TPS62133RGTR", "RC0603FR-0710KL"])
def test_symbol_follows_the_standard(part: str) -> None:
    sym = build(part)
    s = spec.load(DATA / f"{part}.toml")
    # every pin present once, connection points on the 2 mm grid and distinct
    assert sorted(sym.pin_at) == sorted(p.number for p in s.pins)
    points = [(x, y) for x, y, _ in sym.pin_at.values()]
    assert len(set(points)) == len(points)
    assert all(_on_grid(x) and _on_grid(y) for x, y in points)
    pins = [n for n in sexpr.walk(sym.tree) if sexpr.head(n) == "pin"]
    assert len(pins) == len(s.pins)
    assert all(sexpr.child(pin, "length") == ["length", "2"] for pin in pins)
    # designator visible top left; comment empty and hidden; no parameters beyond KiCad's own fields
    ref = sexpr.property_node(sym.tree, "Reference")
    value = sexpr.property_node(sym.tree, "Value")
    assert ref is not None
    assert value is not None
    assert sexpr.child(ref, "hide") is None
    assert float(str(ref[3][1])) == sym.box[0]
    assert value[2] == ""
    assert sexpr.child(value, "hide") == ["hide", "yes"]
    keys = [n[1] for n in sym.tree if isinstance(n, list) and sexpr.head(n) == "property"]
    assert keys == ["Reference", "Value", "Footprint", "Datasheet", "Description"]
    assert sexpr.property_value(sym.tree, "Footprint") == f"{symbol.LIBRARY}:FP"
    assert sexpr.property_value(sym.tree, "Datasheet") == ""  # no parameters (§1.3)
    assert sexpr.property_value(sym.tree, "Description") == ""
    font = sexpr.child(sexpr.child(ref, "effects") or [], "font") or []
    assert sexpr.child(font, "face") == ["face", "Arial"]  # designator Arial (§1.3)


def test_pin_sides() -> None:
    sym = build("TPS62133RGTR")
    side = {p.number: p.side for p in sym.pins}
    names = {p.number: p.name for p in sym.pins}
    assert side["10"] == "top"  # AVIN: supply on top
    assert side["6"] == side["15"] == side["17"] == "bottom"  # AGND, PGND, exposed pad
    assert side["5"] == "left"  # FB: input
    assert side["1"] == side["4"] == "right"  # SW (power out), PG (open drain)
    assert (names["1"], names["2"], names["3"]) == ("SW1", "SW2", "SW3")  # repeated names numbered
    assert (names["15"], names["16"]) == ("PGND1", "PGND2")


def test_dual_opamp_draws_two_triangles() -> None:
    sym = build("LM358DR")
    assert len(symbol.amplifiers(sym.pins)) == 2
    _x, y_plus, _ = sym.pin_at["3"]
    _, y_minus, _ = sym.pin_at["2"]
    _, y_out, angle = sym.pin_at["1"]
    assert y_plus > y_out > y_minus  # IN1+ above OUT1 above IN1-
    assert angle == 180


def _variant(kind: str, names: tuple[str, str]) -> spec.Spec:
    d: dict[str, Any] = tomllib.loads((DATA / "RC0603FR-0710KL.toml").read_text(encoding="utf-8"))
    d["part"]["kind"] = kind
    d["pin"][0]["name"], d["pin"][1]["name"] = names
    return spec.parse(d)


@pytest.mark.parametrize(
    ("kind", "names", "left", "prefix"),
    [("resistor", ("1", "2"), "1", "R"), ("led", ("K", "A"), "2", "D"), ("polar_capacitor", ("+", "-"), "1", "C")],
)
def test_two_terminal_parts(kind: str, names: tuple[str, str], left: str, prefix: str) -> None:
    sym = symbol.build(_variant(kind, names), STD, "FP")
    assert sym.pin_at[left][0] < 0  # anode / + / pin 1 on the left
    assert sexpr.property_value(sym.tree, "Reference") == prefix
    pin_names = sexpr.child(sym.tree, "pin_names")
    assert pin_names is not None
    assert sexpr.child(pin_names, "hide") == ["hide", "yes"]


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_kicad_loads_the_symbol(tmp_path: Path) -> None:
    path = tmp_path / "x.kicad_sym"
    path.write_text(build("TPS62133RGTR").text(), encoding="utf-8")
    run = subprocess.run(
        ["kicad-cli", "sym", "export", "svg", "-o", str(tmp_path), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert list(tmp_path.glob("*.svg"))
