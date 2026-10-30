from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import pytest

from vibepcb import sexpr
from vibepcb.parts import footprint, spec, standard

DATA = Path(__file__).parent / "data" / "parts"
STD = standard.load()


def build(name: str) -> footprint.Footprint:
    return footprint.build(spec.load(DATA / f"{name}.toml"), STD)


def _dist_to_box(x: float, y: float, box: footprint.Box) -> float:
    dx = max(box.x0 - x, 0, x - box.x1)
    dy = max(box.y0 - y, 0, y - box.y1)
    return math.hypot(dx, dy)


def _seg_dist(seg: footprint.Segment, box: footprint.Box) -> float:
    x0, y0, x1, y1 = seg
    steps = 50
    return min(_dist_to_box(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, box) for i in range(steps + 1))


@pytest.mark.parametrize("part", ["LM358DR", "RC0603FR-0710KL", "TPS62133RGTR"])
def test_footprint_follows_the_standard(part: str) -> None:
    fp = build(part)
    tree = fp.tree
    # origin in the middle of the pads
    boxes = [p.box for p in fp.pads]
    assert (min(b.x0 for b in boxes) + max(b.x1 for b in boxes)) / 2 == pytest.approx(0, abs=1e-6)
    assert (min(b.y0 for b in boxes) + max(b.y1 for b in boxes)) / 2 == pytest.approx(0, abs=1e-6)
    # silkscreen keeps 0.2 mm (+ half its width) from every pad
    keep = STD.num("footprint", "silk_pad_clearance") + STD.num("choices", "silk_width") / 2
    for seg in fp.silk:
        assert all(_seg_dist(seg, b) >= keep - 1e-6 for b in boxes), seg
    # placement outline: on its 0.1 mm grid, around body and pads with at least 0.1 mm
    grid = STD.num("footprint", "placement_grid")
    for v in (fp.placement.x0, fp.placement.y0, fp.placement.x1, fp.placement.y1):
        assert v / grid == pytest.approx(round(v / grid), abs=1e-6)
    for b in [*boxes, fp.body]:
        assert fp.placement.x0 <= b.x0 - 0.1 + 1e-6
        assert fp.placement.x1 >= b.x1 + 0.1 - 1e-6
    # pads: rounded rectangles, mask and paste expansion 0
    pads = list(sexpr.children(tree, "pad"))
    assert len(pads) == len(fp.pads)
    for pad in pads:
        assert pad[3] == "roundrect"
        assert sexpr.child(pad, "solder_mask_margin") == ["solder_mask_margin", "0"]
        assert sexpr.child(pad, "solder_paste_margin") == ["solder_paste_margin", "0"]
    # Mechanical 13 body, Mechanical 15 placement + reference
    layers = [sexpr.child(n, "layer") for n in sexpr.walk(tree) if sexpr.head(n) in ("fp_rect", "fp_line")]
    assert ["layer", "User.13"] in layers
    assert ["layer", "User.15"] in layers
    ref = sexpr.property_node(tree, "Reference")
    assert ref is not None
    assert sexpr.child(ref, "layer") == ["layer", "User.15"]
    assert fp.toe is not None
    assert fp.toe >= STD.num("footprint", "toe_min")


def test_ic_has_pin1_marks() -> None:
    fp = build("LM358DR")
    texts = [n[2] for n in sexpr.walk(fp.tree) if sexpr.head(n) == "fp_text"]
    assert "PIN1" in texts
    dots = [n for n in sexpr.walk(fp.tree) if sexpr.head(n) == "fp_circle"]
    assert {sexpr.child(d, "layer")[1] for d in dots} == {"User.15", "F.SilkS"}  # type: ignore[index]
    silk_dot = next(d for d in dots if sexpr.child(d, "layer") == ["layer", "F.SilkS"])
    centre = sexpr.child(silk_dot, "center")
    assert centre is not None
    assert float(str(centre[1])) < fp.pads[0].box.x0  # outside pad 1


def test_resistor_has_no_polarity() -> None:
    fp = build("RC0603FR-0710KL")
    assert not [n for n in sexpr.walk(fp.tree) if sexpr.head(n) == "fp_circle"]
    assert fp.name == "RESC1608X50N"


def _two_pin(kind: str, names: tuple[str, str]) -> spec.Spec:
    return spec.parse(
        {
            "part": {"mpn": "X", "manufacturer": "Y", "datasheet": "z", "kind": kind},
            "package": {
                "family": "chip",
                "pins": 2,
                "pitch": 0,
                "body": {"l": [1.5, 1.7], "w": [0.7, 0.9], "h": [0.8]},
                "lead": {"l": [0.2, 0.4]},
                "page": 1,
            },
            "pin": [
                {"number": "1", "name": names[0], "type": "passive", "page": 1},
                {"number": "2", "name": names[1], "type": "passive", "page": 1},
            ],
        }
    )


def test_diode_cathode_is_marked() -> None:
    fp = footprint.build(_two_pin("diode", ("A", "K")), STD)
    mark, how = footprint.polarity_pad(_two_pin("diode", ("A", "K")), fp.pads)
    assert how == "cathode"
    assert mark is not None
    assert mark.number == "2"
    assert fp.name.startswith("DIOC")
    lines = [n for n in sexpr.walk(fp.tree) if sexpr.head(n) == "fp_line"]
    assert any(sexpr.child(n, "layer") == ["layer", "F.SilkS"] for n in lines)


def test_connector_silk_stays_out_of_pin_fronts() -> None:
    pads = [{"number": str(i + 1), "x": -2.0, "y": i * 1.0 - 1.0, "l": 1.5, "w": 0.6} for i in range(3)]
    s = spec.parse(
        {
            "part": {"mpn": "C3", "manufacturer": "Y", "datasheet": "z", "kind": "connector"},
            "package": {
                "family": "connector",
                "pins": 3,
                "pitch": 1.0,
                "body": {"l": 4.0, "w": 3.0, "h": 2.0},
                "page": 1,
            },
            "land": {"page": 2, "pad": pads},
            "pin": [{"number": str(i + 1), "name": f"P{i + 1}", "type": "passive", "page": 1} for i in range(3)],
        }
    )
    fp = footprint.build(s, STD)
    for seg in fp.silk:
        x0, y0, x1, y1 = seg
        for pad in fp.pads:
            b = pad.box
            if abs(x0 - x1) < 1e-9 and x0 < b.x0:  # a vertical silk line left of (in front of) a pad
                assert max(y0, y1) <= b.y0 or min(y0, y1) >= b.y1


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_kicad_loads_the_footprint(tmp_path: Path) -> None:
    lib = tmp_path / "lib.pretty"
    lib.mkdir()
    fp = build("TPS62133RGTR")
    (lib / f"{fp.name}.kicad_mod").write_text(fp.text(), encoding="utf-8")
    run = subprocess.run(
        ["kicad-cli", "fp", "export", "svg", "-o", str(tmp_path), str(lib)], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr
    assert (tmp_path / f"{fp.name}.svg").is_file()
