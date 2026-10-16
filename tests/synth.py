"""Synthetic KiCad boards for the routing tests: a 30 x 20 mm outline, two SMD pads, extra items."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibepcb import fixes

if TYPE_CHECKING:
    from pathlib import Path

NETS = '(net 0 "")(net 1 "A")(net 2 "B")'
OUTLINE = "".join(
    f'(gr_line (start {a}) (end {b}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))'
    for a, b in [("0 0", "30 0"), ("30 0", "30 20"), ("30 20", "0 20"), ("0 20", "0 0")]
)
LAYERS = '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))'


def pad(ref: str, x: float, y: float, net: int, name: str) -> str:
    return (
        f'(footprint "t:p" (layer "F.Cu") (uuid "00000000-0000-0000-0000-0000000000{x:02.0f}") (at {x} {y} 0)'
        f'(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))'
        f'(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net {net} "{name}")))'
    )


def write(tmp_path: Path, extra: str = "") -> fixes.KicadProject:
    text = (
        f"(kicad_pcb (version 20241229) (generator t) (general (thickness 1.6)) (paper A4) {LAYERS} "
        f"{NETS} {OUTLINE} {pad('P1', 10, 10, 1, 'A')} {pad('P2', 20, 10, 2, 'B')} {pad('P3', 10, 17, 1, 'A')} {extra})"
    )
    directory = tmp_path / "kicad"
    directory.mkdir(parents=True)
    (directory / "b.kicad_pcb").write_text(text, encoding="utf-8")
    (directory / "b.kicad_sch").write_text("(kicad_sch (version 20250114))", encoding="utf-8")
    (directory / "b.kicad_pro").write_text(
        json.dumps({"board": {"design_settings": {"rules": {"min_copper_edge_clearance": 0.5}}}}), encoding="utf-8"
    )
    return fixes.KicadProject.load(directory, "b")


def seg(a: str, b: str, net: int, width: float = 0.2) -> str:
    return f'(segment (start {a}) (end {b}) (width {width}) (layer "F.Cu") (net {net}) (uuid "1"))'
