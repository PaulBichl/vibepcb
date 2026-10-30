from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from vibepcb import paths
from vibepcb.parts import altium_script, build

DATA = Path(__file__).parent / "data" / "parts"


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> build.Built:
    (tmp_path / "parts").mkdir()
    shutil.copy(DATA / "LM358DR.toml", tmp_path / "parts")
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "OUT_DIR", tmp_path / "out")
    return build.build("LM358DR")


def test_script_is_written(built: build.Built) -> None:
    assert built.script_file is not None
    text = built.script_file.read_text(encoding="utf-8")
    assert "Procedure CreateFootprint;" in text
    assert "Procedure CreateSymbol;" in text
    # every Begin has its End (DelphiScript blocks; Case-free script)
    assert len(re.findall(r"\bBegin\b", text)) == len(re.findall(r"\bEnd;", text))


def test_footprint_calls(built: build.Built) -> None:
    calls = altium_script.footprint_calls(built)
    pads = [c for c in calls if c.startswith("AddPad(Fp, '")]
    assert len(pads) == 9  # 8 copper pads + the PIN1 copy
    # pad 1 at KiCad (-2.475, -1.905) -> Altium y up
    assert "AddPad(Fp, '1', -2.475, 1.905, 1.95, 0.6, 0, 0, eRoundedRectangular, 50, eTopLayer);" in calls
    assert "AddPad(Fp, 'PIN1', -2.475, 1.905, 1.95, 0.6, 0, 0, eRectangular, 0, Mech(15));" in calls
    texts = [c for c in calls if c.startswith("AddText")]
    assert any("'.Designator'" in c and "Mech(15)" in c for c in texts)
    assert not any("'PIN1'" in c for c in texts)  # the KiCad stand-in text is replaced by the pad
    assert any("eTopOverlay" in c for c in calls)
    assert any("Mech(13)" in c for c in calls)


def test_symbol_calls(built: build.Built) -> None:
    calls = altium_script.symbol_calls(built)
    pins = [c for c in calls if c.startswith("SchPin")]
    assert len(pins) == 8
    # VCC (pin 8) on top: the body end lies one pin length below the KiCad connection point, pointing up
    vcc = next(c for c in pins if "'8'" in c)
    assert "eRotate90" in vcc
    assert "eElectricPower" in vcc
    rect = next(c for c in calls if c.startswith("SchRect"))
    assert rect.endswith(", 16711680);")  # blue (BGR) border, §1.4
    script = altium_script.script(built)
    assert "Comp.Designator.Text := 'U?';" in script
    assert "Comp.Comment.IsHidden := True;" in script
    assert "Impl.ModelName := 'SOIC127P600X175-8N';" in script


def test_quotes_are_escaped() -> None:
    assert altium_script._q("it's") == "'it''s'"
