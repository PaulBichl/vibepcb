from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest

from vibepcb import config, fixes, stackup

DATA = Path(__file__).parent / "data"
JLC4 = {"preset": "jlc04161h-7628"}


def test_microstrip_matches_pozar_example() -> None:
    # Pozar, Microwave Engineering (4th ed.), Example 3.7: 50 ohm on d = 0.127 cm, er = 2.20,
    # zero thickness -> W = 0.391 cm (W/d = 3.081)
    w = stackup._bisect(lambda w: stackup.microstrip_z0(w, 1.27, 0, 2.2), 50, 0.01, 20, rising=False)
    assert w == pytest.approx(3.91, rel=0.01)
    # air line, w = h: Hammerstad's Z01(1) = 60 ln(6 + sqrt 5) = 126.5 ohm (Wadell eq. 3.3.4)
    assert stackup.microstrip_z0(1, 1, 0, 1) == pytest.approx(60 * math.log(6 + math.sqrt(5)), rel=1e-4)


def test_microstrip_thickness_lowers_impedance() -> None:
    assert stackup.microstrip_z0(0.3, 0.2, 0.035, 4.4) < stackup.microstrip_z0(0.3, 0.2, 0, 4.4)


def _cohn_exact(w: float, b: float, er: float) -> float:
    """Zero-thickness centred stripline, exact conformal map (Cohn 1954): 30 pi / sqrt(er) K(k)/K(k')."""
    k, kp = 1 / math.cosh(math.pi * w / (2 * b)), math.tanh(math.pi * w / (2 * b))

    def agm(a: float, g: float) -> float:
        while abs(a - g) > 1e-15:
            a, g = (a + g) / 2, math.sqrt(a * g)
        return a

    return 30 * math.pi / math.sqrt(er) * agm(1, k) / agm(1, kp)  # K(k)/K(k') = agm(1, k)/agm(1, k')


@pytest.mark.parametrize("w_over_b", [0.1, 0.3, 0.83, 2.0])
def test_stripline_matches_the_exact_zero_thickness_solution(w_over_b: float) -> None:
    assert stackup.stripline_z0(w_over_b, 0.5, 0.5, 0, 4.0) == pytest.approx(_cohn_exact(w_over_b, 1, 4.0), rel=0.005)


def test_stripline_matches_pozar_example() -> None:
    # Pozar Example 3.5: 50 ohm copper stripline, b = 0.32 cm, er = 2.20 -> W = 0.266 cm
    w = stackup._bisect(lambda w: stackup.stripline_z0(w, 1.6, 1.6, 0, 2.2), 50, 0.01, 20, rising=False)
    assert w == pytest.approx(2.66, rel=0.01)


def test_offset_stripline_is_between_the_centred_lines() -> None:
    z = stackup.stripline_z0(0.15, 0.1, 0.5, 0.0152, 4.5)
    assert stackup.stripline_z0(0.15, 0.1, 0.1, 0.0152, 4.5) < z < stackup.stripline_z0(0.15, 0.5, 0.5, 0.0152, 4.5)


def test_differential_coupling() -> None:
    z0 = stackup.microstrip_z0(0.2, 0.2, 0.035, 4.4)
    assert stackup.microstrip_zdiff(0.2, 0.2, 0.2, 0.035, 4.4) < 2 * z0
    assert stackup.microstrip_zdiff(0.2, 20, 0.2, 0.035, 4.4) == pytest.approx(2 * z0, rel=1e-6)
    # IPC-2141A: s = h -> 2 Z0 (1 - 0.48 e^-0.96)
    assert stackup.microstrip_zdiff(0.2, 0.2, 0.2, 0.035, 4.4) == pytest.approx(2 * z0 * (1 - 0.48 * math.exp(-0.96)))


def test_preset_geometry() -> None:
    stack = stackup.from_config(JLC4)
    assert stack.copper == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert stack.geometry("F.Cu") == stackup.Geometry("F.Cu", 0.035, None, 0.2104, 4.4)
    assert stack.geometry("B.Cu").kind == "microstrip"
    inner = stack.geometry("In1.Cu")
    assert (inner.kind, inner.h1, inner.h2) == ("stripline", 0.2104, 1.065)
    assert inner.er == pytest.approx((0.2104 * 4.4 + 1.065 * 4.6) / (0.2104 + 1.065))
    with pytest.raises(stackup.StackupError, match="no copper layer"):
        stack.geometry("In3.Cu")


def test_jlc_4_layer_widths() -> None:
    # Values recorded for comparison with JLCPCB's online calculator (bare trace, no mask; JLC's
    # calculator includes solder mask and gives a somewhat narrower width - not checked here)
    geo = stackup.from_config(JLC4).geometry("F.Cu")
    assert stackup.solve_width(geo, 50) == pytest.approx(0.387, abs=0.002)
    w, s = stackup.solve_pair(geo, 90)
    assert w == s == pytest.approx(0.406, abs=0.002)


@pytest.mark.parametrize("layer", ["F.Cu", "In1.Cu", "In2.Cu"])
def test_solver_round_trip(layer: str) -> None:
    geo = stackup.from_config({"preset": "jlc06161h-3313"}).geometry(layer)
    for w in (0.1, 0.2, 0.5):
        assert stackup.solve_width(geo, geo.z0(w)) == pytest.approx(w, rel=1e-6)
    w, s = stackup.solve_pair(geo, geo.zdiff(0.15, 0.2), gap=0.2)
    assert w == pytest.approx(0.15, rel=1e-6)
    w, s = stackup.solve_pair(geo, geo.zdiff(0.15, 0.2), width=0.15)
    assert s == pytest.approx(0.2, rel=1e-6)
    w, s = stackup.solve_pair(geo, 100)
    assert w == s
    assert geo.zdiff(w, s) == pytest.approx(100, rel=1e-6)


def test_unreachable_impedance() -> None:
    geo = stackup.from_config(JLC4).geometry("F.Cu")
    with pytest.raises(stackup.StackupError, match="out of reach"):
        stackup.solve_pair(geo, 130, width=stackup.solve_width(geo, 50))  # > 2 Z0: no gap gets there


def test_explicit_layers_and_validation() -> None:
    stack = stackup.from_config(
        {
            "layers": [
                {"name": "F.Cu", "copper": 0.035},
                {"dielectric": 1.5, "er": 4.5},
                {"name": "B.Cu", "copper": 0.035},
            ]
        }
    )
    assert stack.geometry("F.Cu").h2 == 1.5
    for bad in (
        {},
        {"preset": "nope"},
        {"preset": "jlc04161h-7628", "layers": []},
        {"layers": [{"dielectric": 1, "er": 4}]},
        {"layers": [{"name": "F.Cu", "copper": 0.035}, {"name": "B.Cu", "copper": 0.035}]},
        {
            "layers": [
                {"name": "F.Cu", "copper": 0.035},
                {"dielectric": 1, "er": 0.5},
                {"name": "B.Cu", "copper": 0.035},
            ]
        },
    ):
        with pytest.raises(stackup.StackupError):
            stackup.from_config(bad)


def test_config_validation() -> None:
    usb = {"nets": ["USB*"], "impedance": 50, "impedance_diff": 90}
    config.validate({"stackup": JLC4, "netclass": {"USB": usb}})
    config.validate({"stackup": JLC4, "netclass": {"USB": {**usb, "layer": "B.Cu"}}})
    with pytest.raises(config.ConfigError, match="needs a"):
        config.validate({"netclass": {"USB": usb}})
    with pytest.raises(config.ConfigError, match="no copper layer"):
        config.validate({"stackup": JLC4, "netclass": {"USB": {**usb, "layer": "In5.Cu"}}})
    with pytest.raises(config.ConfigError, match="only goes with"):
        config.validate({"stackup": JLC4, "netclass": {"USB": {"nets": ["U*"], "layer": "F.Cu"}}})
    with pytest.raises(config.ConfigError, match="unknown preset"):
        config.validate({"stackup": {"preset": "x"}})
    with pytest.raises(config.ConfigError, match="out of reach"):
        config.validate({"stackup": JLC4, "netclass": {"X": {"nets": ["X"], "impedance": 50, "impedance_diff": 130}}})


def test_apply_board_config_writes_solved_widths(tmp_path: Path) -> None:
    directory = tmp_path / "kicad"
    directory.mkdir()
    for suffix in (".kicad_pcb", ".kicad_sch"):
        shutil.copy(DATA / f"led-indicator{suffix}", directory / f"board{suffix}")
    project = fixes.KicadProject.load(directory, "board")
    pro = project.pcb_path.with_suffix(".kicad_pro")
    pro.write_text(json.dumps({"net_settings": {"classes": [{"name": "Default", "clearance": 0.2}]}}), encoding="utf-8")
    project.config = {
        "stackup": JLC4,
        "netclass": {
            "RF": {"nets": ["RF*"], "impedance": 50, "track_width": 0.2},
            "USB": {"nets": ["USB_D*"], "impedance_diff": 90, "diff_pair_gap": 0.2, "layer": "B.Cu"},
        },
    }
    changes = fixes.apply_board_config(project)
    assert len(changes) == 3
    assert "net class RF: track_width 0.387 mm (50 ohm microstrip on F.Cu, stack-up jlc04161h-7628)" in changes[1]
    assert "90 ohm diff microstrip on B.Cu" in changes[2]
    classes = {c["name"]: c for c in json.loads(pro.read_text(encoding="utf-8"))["net_settings"]["classes"]}
    assert classes["RF"] == {"name": "RF", "clearance": 0.2, "track_width": 0.387}  # replaces the given 0.2
    usb = classes["USB"]
    assert usb["diff_pair_gap"] == 0.2
    geo = stackup.from_config(JLC4).geometry("B.Cu")
    assert geo.zdiff(usb["diff_pair_width"], 0.2) == pytest.approx(90, rel=0.005)
    assert "impedance_diff" not in usb
    assert "layer" not in usb
    assert fixes.apply_board_config(project) == []  # idempotent
