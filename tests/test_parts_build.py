from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from vibepcb import paths, sexpr
from vibepcb.parts import altium, build, check, model3d

DATA = Path(__file__).parent / "data" / "parts"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "parts").mkdir()
    for f in DATA.glob("*.toml"):
        shutil.copy(f, tmp_path / "parts" / f.name)
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "OUT_DIR", tmp_path / "out")
    return tmp_path


@pytest.mark.parametrize("mpn", ["LM358DR", "RC0603FR-0710KL", "TPS62133RGTR"])
def test_build_and_check_pass(root: Path, mpn: str) -> None:
    built = build.build(mpn)
    assert built.symbol_file.is_file()
    assert built.footprint_file.parent.name == "vibepcb_parts.pretty"
    report = check.run(built)
    assert report.ok, report.text()
    assert any("land pattern calculated" in w for w in report.warnings)
    assert any(m.startswith("§1.4") for m in report.manual)


def test_missing_spec(root: Path) -> None:
    with pytest.raises(FileNotFoundError, match="pcb part fetch"):
        build.build("NOPE123")


def test_check_catches_a_broken_footprint(root: Path) -> None:
    built = build.build("LM358DR")
    pad = next(p for p in built.footprint.pads if p.number == "8")
    built.footprint.pads.remove(pad)
    for node in list(built.footprint.tree):
        if isinstance(node, list) and node[:2] == ["pad", "8"]:
            node[node.index(next(n for n in node if isinstance(n, list) and n[0] == "solder_mask_margin"))] = [
                "solder_mask_margin",
                "0.05",
            ]
    value = sexpr.property_node(built.symbol.tree, "Value")
    assert value is not None
    value[2] = "LM358"  # no longer empty
    report = check.run(built)
    text = report.text()
    assert not report.ok
    assert "symbol pins without a footprint pad: ['8']" in text
    assert "solder mask expansion" in text
    assert "comment (Value) not empty and hidden (§1.3" in text


def test_lifecycle_warning(root: Path) -> None:
    spec_file = root / "parts" / "LM358DR.toml"
    spec_file.write_text(spec_file.read_text().replace('lifecycle = "active"', 'lifecycle = "nrnd"'))
    report = check.run(build.build("LM358DR"))
    assert any(w.startswith("lifecycle: nrnd") for w in report.warnings)


def test_altium_export(root: Path, tmp_path: Path) -> None:
    built = build.build("TPS62133RGTR")
    files = altium.export(built, tmp_path / "altium")
    names = {f.name for f in files}
    assert {"TPS62133RGTR.kicad_sym", "QFN50P300X300X100-17N.kicad_mod", "IMPORT-TPS62133RGTR.md"} <= names
    side = json.loads((tmp_path / "altium" / "TPS62133RGTR.altium.json").read_text())
    assert side["layers"] == {"mech13": "User.13", "mech15": "User.15"}
    assert side["fields"]["Reference"]["visible"] is True
    assert side["fields"]["Value"] == {"value": "", "visible": False}
    assert 0.15 in side["line_widths"]["F.SilkS"]


@pytest.mark.skipif(model3d.cadquery() is None, reason="needs CadQuery (hatch run parts:test)")
def test_step_bounding_box(root: Path) -> None:
    cq = model3d.cadquery()
    assert cq is not None
    built = build.build("LM358DR")
    assert built.step_file is not None
    bb = cq.importers.importStep(str(built.step_file)).val().BoundingBox()
    assert (round(bb.xmin, 3), round(bb.xmax, 3)) == (-3.1, 3.1)  # leads out to the max span
    assert (round(bb.ymin, 3), round(bb.ymax, 3)) == (-2.5, 2.5)  # max body length
    assert (round(bb.zmin, 3), round(bb.zmax, 3)) == (0.0, 1.75)
