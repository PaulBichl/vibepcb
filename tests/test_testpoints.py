from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibepcb import bringup, config, fixes, progress, sexpr, testpoints

if TYPE_CHECKING:
    from vibepcb.sexpr import Node

DATA = Path(__file__).parent / "data"


def _pad(name: str, net: str) -> str:
    return f'(pad "{name}" smd rect (at 0 0) (size 1 1) (layers F.Cu) (net "{net}"))'


def _fp(ref: str, footprint: str, value: str, pads: dict[str, str]) -> str:
    body = " ".join(_pad(n, net) for n, net in pads.items())
    props = f'(property "Reference" "{ref}") (property "Value" "{value}")'
    return f'(footprint "lib:{footprint}" (layer "F.Cu") {props} {body})'


def _pcb(*footprints: str) -> list[Node]:
    return sexpr.loads("(kicad_pcb " + " ".join(footprints) + ")")


def _board(*, with_tp: bool = True) -> list[Node]:
    parts = [
        _fp("J1", "pinheader", "hdr", {"1": "VBUS", "2": "GND"}),
        _fp("U1", "sot223", "AMS1117-3.3", {"1": "GND", "2": "+3V3", "3": "VBUS"}),
        _fp("R1", "0603", "10k", {"1": "+3V3", "2": "LED_A"}),
        _fp("C1", "0603", "1u", {"1": "+3V3", "2": "GND"}),
    ]
    if with_tp:
        parts.append(_fp("TP1", "test_point_smtpad_circle_d1", "tp", {"1": "+3V3"}))
        parts.append(_fp("TP5", "test_point_smtpad_circle_d1", "tp", {"1": "LED_A"}))
    return _pcb(*parts)


@pytest.mark.parametrize(
    ("name", "power", "ground"),
    [
        ("+3V3", True, False), ("V3V3", True, False), ("3V3_A", True, False), ("5V", True, False),
        ("VBUS", True, False), ("VIN", True, False), ("VBAT", True, False), ("VDDA", True, False),
        ("12V", True, False), ("GND", False, True), ("AGND", False, True), ("VSS", False, True),
        ("LED_A", False, False), ("SPI_MOSI", False, False), ("VIN_SENSE_EN", False, False),
    ],
)  # fmt: skip
def test_rail_names(name: str, power: bool, ground: bool) -> None:
    assert testpoints.is_power(name) is power
    assert testpoints.is_ground(name) is ground


def test_missing_rails_and_connector_pins_count() -> None:
    parts = testpoints.parts(_board(with_tp=False))
    nets, severity = testpoints.missing(parts)
    assert nets == ["+3V3"]  # VBUS and GND are reached through the header J1
    assert severity == "warning"
    assert testpoints.missing(testpoints.parts(_board()))[0] == []


def test_config_required_patterns_ignore_severity() -> None:
    parts = testpoints.parts(_board(with_tp=False))
    nets, severity = testpoints.missing(parts, None, {"required": ["gnd", "LED_*"], "severity": "error"})
    assert (nets, severity) == (["LED_A"], "error")
    assert testpoints.missing(parts, None, {"required": ["power"], "ignore": ["+3V*"]})[0] == []
    assert testpoints.missing(parts, None, {"required": []})[0] == []


def test_power_symbol_nets_are_rails() -> None:
    sch = sexpr.loads('(kicad_sch (symbol (lib_id "x") (property "Reference" "#PWR01") (property "Value" "SUPPLY_X")))')
    parts = testpoints.parts(_pcb(_fp("R1", "0603", "1k", {"1": "SUPPLY_X", "2": "GND"})))
    assert testpoints.rail_nets(parts, sch) == (["SUPPLY_X"], ["GND"])


def test_findings_lines() -> None:
    lines, severity = testpoints.findings(testpoints.parts(_board(with_tp=False)))
    assert severity == "warning"
    assert len(lines) == 1
    assert lines[0].startswith("  [warning] missing_testpoint: +3V3")


def test_config_validation() -> None:
    config.validate({"testpoints": {"required": ["power"], "severity": "error"}})
    with pytest.raises(config.ConfigError, match="severity"):
        config.validate({"testpoints": {"severity": "fatal"}})
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.validate({"testpoints": {"nets": []}})


def test_led_indicator_fixture_passes() -> None:
    pcb = sexpr.load(DATA / "led-indicator.kicad_pcb")
    sch = sexpr.load(DATA / "led-indicator.kicad_sch")
    assert testpoints.missing(testpoints.parts(pcb), sch)[0] == []  # VCC and GND sit on header J1


def test_exclude_testpoints_from_bom(tmp_path: Path) -> None:
    directory = tmp_path / "kicad"
    directory.mkdir()
    for suffix in (".kicad_pcb", ".kicad_sch"):
        shutil.copy(DATA / f"led-indicator{suffix}", directory / f"board{suffix}")
    project = fixes.KicadProject.load(directory, "board")
    project.pcb.append(sexpr.loads(_fp("TP1", "test_point_smtpad_circle_d1", "tp", {"1": "VCC"})))
    project.sch.append(
        sexpr.loads('(symbol (lib_id "v:TP1") (in_bom yes) (on_board yes) (property "Reference" "TP1"))')
    )
    changes = fixes.exclude_testpoints_from_bom(project)
    assert changes == ["TP1: symbol out of the BOM", "TP1: footprint board_only, out of BOM and CPL"]
    footprints = fixes._footprints_by_ref(project.pcb)
    attr = sexpr.child(footprints["TP1"], "attr")
    assert attr is not None
    assert [str(a) for a in attr[1:]] == ["board_only", "exclude_from_pos_files", "exclude_from_bom"]
    assert fixes.exclude_testpoints_from_bom(project) == []  # idempotent
    assert sexpr.child(footprints["R1"], "attr") is None  # real parts untouched


# --- bring-up sheet and firmware header ---------------------------------------------------------


def _mcu_board() -> list[Node]:
    pads = {
        "1": "VDD", "2": "GND", "3": "LED_STATUS", "4": "SPI_MOSI", "5": "UART TX", "6": "Net-(R1-Pad2)",
        "7": "LED_STATUS",
    }  # fmt: skip
    return _pcb(
        _fp("U2", "lqfp", "STM32F103", pads),
        _fp("J1", "pinheader", "hdr", {"1": "VIN", "2": "GND"}),
        _fp("U1", "sot223", "AMS1117-3.3", {"1": "GND", "2": "VDD", "3": "VIN"}),
        _fp("TP1", "test_point_smtpad_circle_d1", "tp", {"1": "VDD"}),
        _fp("TP2", "test_point_smtpad_circle_d1", "tp", {"1": "SPI_MOSI"}),
    )


def _circuit(tmp_path: Path) -> Path:
    elements: list[dict[str, object]] = [{"type": "source_component", "source_component_id": "c", "name": "U2"}]
    names = {"1": "VDD", "2": "VSS", "3": "PA5", "4": "PB15", "5": "GPIO4", "6": "PC13", "7": "PA6-ALT"}
    elements += [
        {"type": "source_port", "source_component_id": "c", "name": name, "pin_number": int(pad)}
        for pad, name in names.items()
    ]
    path = tmp_path / "board.circuit.json"
    path.write_text(json.dumps(elements), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("net", "volts"),
    [("+3V3", 3.3), ("V3V3", 3.3), ("5V", 5.0), ("1V8", 1.8), ("12V", 12.0), ("VBUS", 5.0), ("+5.0V", 5.0),
     ("VCC", None), ("LED", None)],
)  # fmt: skip
def test_expected_volts(net: str, volts: float | None) -> None:
    assert bringup.expected_volts(net) == volts


def _project(tmp_path: Path, pcb: list[Node]) -> fixes.KicadProject:
    sch = sexpr.loads("(kicad_sch)")
    return fixes.KicadProject(tmp_path, pcb, sch, tmp_path / "a.kicad_pcb", tmp_path / "a.kicad_sch")


def test_bringup_sheet(tmp_path: Path) -> None:
    data = bringup.collect(_project(tmp_path, _mcu_board()), _circuit(tmp_path))
    assert [r.net for r in data.rails] == ["VIN", "VDD"] or [r.net for r in data.rails] == ["VDD", "VIN"]
    assert [r.ref for r in data.regulators] == ["U1"]
    sheet = bringup.render_sheet("demo", data)
    assert sheet == bringup.render_sheet("demo", data)  # deterministic
    assert "| VDD |" in sheet
    assert "Measure VDD at TP1" in sheet
    assert "- SPI_MOSI: TP2" in sheet
    assert "demo-pins.h" in sheet


def test_pin_header(tmp_path: Path) -> None:
    data = bringup.collect(_project(tmp_path, _mcu_board()), _circuit(tmp_path))
    assert data.mcu_ref == "U2"
    header = bringup.render_header("demo", data)
    assert "#define SPI_MOSI_PIN" in header
    assert "PB15" in header
    assert "#define UART_TX_PIN" in header
    assert "LED_STATUS_PIN_PA5" in header
    assert "LED_STATUS_PIN_PA6" in header  # one net on two pins gets the pin name appended
    assert "Net-" not in header
    assert header.startswith("/* demo")
    assert header.rstrip().endswith("#endif /* DEMO_PINS_H */")


def test_no_mcu_no_header(tmp_path: Path) -> None:
    data = bringup.collect(_project(tmp_path, _board()), None)
    assert bringup.render_header("demo", data) == ""


def test_stages_summary() -> None:
    now = [0.0]
    lines: list[str] = []
    stages = progress.Stages(lines.append, clock=lambda: now[0])
    with stages.stage("routing"):
        now[0] = 125.0
    with stages.stage("export"):
        now[0] = 130.0
    assert lines[0].endswith("routing ...")
    assert lines[1].startswith("[2 min 05 s]")
    summary = stages.summary()
    assert "routing" in summary[0]
    assert "2 min 05 s" in summary[0]
    assert "5 s" in summary[1]
    assert "total" in summary[2]
    assert progress.Stages().summary() == []
