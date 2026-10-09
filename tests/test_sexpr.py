from __future__ import annotations

from pathlib import Path

import pytest

from vibepcb import sexpr
from vibepcb.sexpr import Sym

DATA = Path(__file__).parent / "data"


def test_parse_atoms_strings_and_escapes() -> None:
    tree = sexpr.loads(r'(net 3 "a \"b\" \\ c")')
    assert tree == [Sym("net"), Sym("3"), 'a "b" \\ c']
    assert isinstance(tree[1], Sym)
    assert not isinstance(tree[2], Sym)


def test_dumps_keeps_quoted_and_bare_distinct() -> None:
    tree: list[sexpr.Node] = [Sym("layer"), Sym("F.Cu"), "F.Cu", ""]
    assert sexpr.dumps(tree) == '(layer F.Cu "F.Cu" "")\n'


@pytest.mark.parametrize("name", ["led-indicator.kicad_pcb", "led-indicator.kicad_sch"])
def test_roundtrip_is_lossless(name: str) -> None:
    tree = sexpr.load(DATA / name)
    assert sexpr.loads(sexpr.dumps(tree)) == tree


@pytest.mark.parametrize("text", ["(a (b)", "(a))", "a", "(a) (b)"])
def test_rejects_malformed(text: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        sexpr.loads(text)


def test_navigation_helpers() -> None:
    pcb = sexpr.load(DATA / "led-indicator.kicad_pcb")
    refs = [sexpr.property_value(fp, "Reference") for fp in sexpr.children(pcb, "footprint")]
    assert refs == ["J1", "R1", "D1"]
    assert sexpr.head(sexpr.child(pcb, "general") or []) == "general"
