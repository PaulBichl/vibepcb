from __future__ import annotations

import json
import shutil
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from vibepcb import cli, findings, intent, netcheck, paths, sim, verify

if TYPE_CHECKING:
    from pathlib import Path

HAVE_NGSPICE = shutil.which("ngspice") is not None
DEFAULTS = sim.Defaults({"overshoot_pct": (0.0, 5.0)}, {"regulator": ("powerup",)}, {"cap_derating": 0.8})
NO_INTENT = intent.Intent()


def circuit(parts: list[tuple[str, str, dict[str, Any], list[tuple[str, str | None]]]]) -> list[dict[str, Any]]:
    """Circuit JSON elements: (ref, ftype, props, [(pin name, net)])."""
    out: list[dict[str, Any]] = []
    nets: set[str] = set()
    for i, (ref, ftype, props, pins) in enumerate(parts):
        out.append({"type": "source_component", "source_component_id": f"c{i}", "name": ref, "ftype": ftype, **props})
        for n, (pin, net) in enumerate(pins, 1):
            port: dict[str, Any] = {
                "type": "source_port",
                "source_port_id": f"p{i}_{n}",
                "source_component_id": f"c{i}",
                "name": pin,
                "pin_number": n,
            }
            if net:
                port["subcircuit_connectivity_map_key"] = f"key_{net}"
                nets.add(net)
            out.append(port)
    out += [
        {"type": "source_net", "name": n, "source_net_id": f"n_{n}", "subcircuit_connectivity_map_key": f"key_{n}"}
        for n in sorted(nets)
    ]
    return out


def r(
    ref: str, ohm: float, a: str | None, b: str | None
) -> tuple[str, str, dict[str, Any], list[tuple[str, str | None]]]:
    return (ref, "simple_resistor", {"resistance": ohm}, [("pin1", a), ("pin2", b)])


def c(
    ref: str, farad: float, a: str | None, b: str | None, **props: Any
) -> tuple[str, str, dict[str, Any], list[tuple[str, str | None]]]:
    return (ref, "simple_capacitor", {"capacitance": farad, **props}, [("pin1", a), ("pin2", b)])


def chip(
    ref: str, pins: list[tuple[str, str | None]], **props: Any
) -> tuple[str, str, dict[str, Any], list[tuple[str, str | None]]]:
    return (ref, "simple_chip", props, pins)


def ids(found: list[findings.Finding]) -> set[str]:
    return {f.id for f in found}


# --- SPICE values, decks -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("4.7k", 4700),
        ("1meg", 1e6),
        ("100nF", 1e-7),
        ("10u", 1e-5),
        ("2.2p", 2.2e-12),
        ("5m", 5e-3),
        ("3", 3),
        ("1e-3", 1e-3),
        ("1MEG", 1e6),
    ],
)
def test_parse_value(token: str, value: float) -> None:
    assert sim.parse_value(token) == pytest.approx(value)


def test_parse_value_rejects_expressions() -> None:
    with pytest.raises(ValueError, match="not a SPICE number"):
        sim.parse_value("{rload}")


DECK = """power-up of U1
* range: vout_max 3.2 3.4 source=datasheet:TLV75533 p.5
* range: ripple_pct - 5
* covers: powerup U1
.param rtop=100k
V1 vin 0 5
R1 vin fb {rtop}
R2 fb 0 33k
Rload out 0 10
C1 out 0 4.7u
CLOAD out 0 1u
.tran 1u 1m
.measure tran vout_max MAX v(out)
.meas tran ripple_pct PP v(out)
.end
"""


def test_parse_deck(tmp_path: Path) -> None:
    deck = sim.parse_deck(tmp_path / "powerup.cir", DECK)
    assert deck.ranges["vout_max"] == sim.Range(3.2, 3.4, "datasheet:TLV75533 p.5", "datasheet")
    assert deck.ranges["ripple_pct"] == sim.Range(None, 5.0, None, "assumed")
    assert deck.covers == [("powerup", ("U1",))]
    assert deck.measures == ["vout_max", "ripple_pct"]
    # only R/C/L with a reference-like name; .param substituted; Rload / CLOAD are not references
    assert {(e.name, e.value) for e in deck.elements} == {("R1", 1e5), ("R2", 33e3), ("C1", 4.7e-6)}


def test_control_block_result_declared_by_its_range(tmp_path: Path) -> None:
    text = (
        "sweep\n* range: pm_min_phase_margin_deg 45 - source=house\nR9 a 0 1k\n"
        '.control\n  meas ac fcx when vdb=0\n  echo "pm_min_phase_margin_deg = 52"\n.endc\n'
    )
    deck = sim.parse_deck(tmp_path / "loop.cir", text)
    assert deck.measures == ["pm_min_phase_margin_deg"]  # the control block's `meas fcx` is a scratch value
    values = sim.parse_measures("fcx = 1.2e+04\npm_min_phase_margin_deg = 52\n")
    [judged] = sim.evaluate(deck, values, NO_INTENT, DEFAULTS)
    assert (judged.name, judged.value, judged.status) == ("pm_min_phase_margin_deg", 52.0, "ok")


def test_bad_range_line(tmp_path: Path) -> None:
    with pytest.raises(sim.SimError, match="needs <measure> <min> <max>"):
        sim.parse_deck(tmp_path / "x.cir", "title\n* range: vout_max 3.2\n")


def test_compare_values(tmp_path: Path) -> None:
    deck = sim.parse_deck(tmp_path / "powerup.cir", DECK)
    kinds = {"R1": "resistor", "R2": "resistor", "C1": "capacitor"}
    assert sim.compare_values(deck, {"R1": 1e5, "R2": 33e3, "C1": 4.7e-6}, kinds) == []
    stale = sim.compare_values(deck, {"R1": 1e5, "R2": 47e3, "C1": 4.7e-6}, kinds)
    assert stale == ["R2: deck 33000, board 47000"]
    assert "C1 is in the deck but not on the board" in sim.compare_values(
        deck, {}, {"R1": "resistor", "R2": "resistor"}
    )
    assert "R2 is a resistor in the deck but a capacitor on the board" in sim.compare_values(
        deck, {}, {**kinds, "R2": "capacitor"}
    )


NGSPICE_OUT = """
vmax                =  3.29985e+00 at=  1.00000e-03
tr                  =  2.29252e-04 targ=  2.40289e-04 trig=  1.10375e-05
Error: measure  bad  find(AT) : out of interval
 .meas tran bad find v(out) at=5 failed!
other = failed
Total analysis time (seconds) = 0.00411208
"""


def test_parse_measures() -> None:
    values = sim.parse_measures(NGSPICE_OUT)
    assert values["vmax"] == pytest.approx(3.29985)
    assert values["tr"] == pytest.approx(2.29252e-4)
    assert values["bad"] is None
    assert values["other"] is None
    assert "total" not in values  # "Total analysis time (seconds) = " has a different shape


def test_range_sources(tmp_path: Path) -> None:
    deck = sim.parse_deck(tmp_path / "powerup.cir", DECK)
    spec = intent.parse(
        {
            "rail": {"+3V3": {"voltage": 3.3, "tolerance_pct": 5, "current": 0.5}},
            "supply": {"net": "VBUS", "voltage": 5},
        }
    )
    assert sim.resolve_range("vout_max", deck, spec, DEFAULTS).kind == "datasheet"  # type: ignore[union-attr]
    vmax = sim.resolve_range("vmax_3v3", deck, spec, DEFAULTS)
    assert vmax is not None
    assert vmax.kind == "intent"
    assert vmax.hi == pytest.approx(3.465)
    vmin = sim.resolve_range("vmin_3v3", deck, spec, DEFAULTS)
    assert vmin is not None
    assert vmin.lo == pytest.approx(3.135)
    imax = sim.resolve_range("imax_3v3", deck, spec, DEFAULTS)
    assert imax is not None
    assert (imax.hi, imax.kind) == (0.5, "intent")
    house = sim.resolve_range("vout_overshoot_pct", deck, spec, DEFAULTS)
    assert house is not None
    assert (house.lo, house.hi, house.kind) == (0.0, 5.0, "house")
    assert sim.resolve_range("ripple_pct", deck, spec, DEFAULTS).kind == "assumed"  # type: ignore[union-attr]
    assert sim.resolve_range("unknown", deck, spec, DEFAULTS) is None


def test_evaluate(tmp_path: Path) -> None:
    deck = sim.parse_deck(tmp_path / "powerup.cir", DECK)
    done = sim.evaluate(deck, {"vout_max": 3.5, "ripple_pct": 1.0}, NO_INTENT, DEFAULTS)
    assert [(m.name, m.status) for m in done] == [("vout_max", "fail"), ("ripple_pct", "assumed")]
    done = sim.evaluate(deck, {"vout_max": 3.3, "ripple_pct": 9.0}, NO_INTENT, DEFAULTS)
    assert [m.status for m in done] == ["ok", "fail"]  # an assumed range can still fail
    done = sim.evaluate(deck, {"vout_max": None}, NO_INTENT, DEFAULTS)
    assert done[0].status == "error"
    assert "failed to measure" in done[0].line()


def test_stale_deck_is_not_run(tmp_path: Path) -> None:
    deck = sim.parse_deck(tmp_path / "powerup.cir", DECK)
    result = sim.check_deck(deck, {"R1": 1.0}, {"R1": "resistor"}, NO_INTENT, DEFAULTS, "/nonexistent/ngspice")
    assert result.status == "stale"
    assert any("R1: deck 100000, board 1" in p for p in result.problems)


def test_missing_ngspice_is_skipped_not_a_crash(tmp_path: Path) -> None:
    deck = sim.parse_deck(tmp_path / "x.cir", "t\nR1 a 0 1k\n.meas tran m MAX v(a)\n")
    result = sim.check_deck(deck, {"R1": 1000.0}, {"R1": "resistor"}, NO_INTENT, DEFAULTS, None)
    assert result.status == "skipped"
    assert "not installed" in result.problems[0]


@pytest.mark.skipif(not HAVE_NGSPICE, reason="ngspice not installed")
def test_run_deck_with_ngspice(tmp_path: Path) -> None:
    path = tmp_path / "rc.cir"
    path.write_text(
        "rc\nV1 in 0 PULSE(0 3.3 0 1u 1u 1 2)\nR5 in out 1k\nC3 out 0 100n\n.tran 1u 1m\n"
        "* range: vmax 3 3.4 source=datasheet:test\n.meas tran vmax MAX v(out)\n"
        ".meas tran bad FIND v(out) AT=5\n.end\n",
        encoding="utf-8",
    )
    deck = sim.parse_deck(path)
    result = sim.check_deck(
        deck, {"R5": 1000.0, "C3": 1e-7}, {"R5": "resistor", "C3": "capacitor"}, NO_INTENT, DEFAULTS, sim.ngspice()
    )
    assert {m.name: m.status for m in result.measures} == {"vmax": "ok", "bad": "error"}
    assert result.status == "fail"
    stale = sim.check_deck(
        deck, {"R5": 2000.0, "C3": 1e-7}, {"R5": "resistor", "C3": "capacitor"}, NO_INTENT, DEFAULTS, sim.ngspice()
    )
    assert stale.status == "stale"


def test_shipped_defaults_load() -> None:
    defaults = sim.load_defaults()
    assert defaults.limits["overshoot_pct"] == (0.0, 5.0)
    assert defaults.limits["inrush_a"] == (None, 2.0)
    assert defaults.required["regulator"] == ("powerup",)
    assert defaults.netlist["cap_derating"] == 0.8


# --- intent --------------------------------------------------------------------------------------


def test_intent_parse() -> None:
    spec = intent.parse(
        {
            "description": "x",
            "assumptions": ["a"],
            "supply": {"net": "VBUS", "voltage": 5.0, "current": 0.5},
            "rail": {"+3V3": {"voltage": 3.3, "regulator": "U1", "vref": 0.8}},
        }
    )
    assert spec.voltage_of("+3V3") == 3.3
    assert spec.voltage_of("VBUS") == 5.0
    assert spec.voltage_of("X") is None
    assert spec.rails["+3V3"].tolerance_pct == 5.0
    assert spec.rail_by_key("3v3") is spec.rails["+3V3"]


def test_intent_errors() -> None:
    with pytest.raises(intent.IntentError, match="unknown key"):
        intent.parse({"rail": {"X": {"volts": 3}}})
    with pytest.raises(intent.IntentError, match="must be a number"):
        intent.parse({"rail": {"X": {"voltage": "3V"}}})
    with pytest.raises(intent.IntentError, match="unknown key"):
        intent.parse({"suply": {}})


# --- findings ------------------------------------------------------------------------------------


def test_findings_merge_and_roundtrip(tmp_path: Path) -> None:
    old = [
        findings.Finding("a", "U1", "VDD", "old text", "accepted", "on the sensor board"),
        findings.Finding("gone", "U2", "", "fixed since"),
        findings.Finding("review:1", "U3", "EN", "checklist item", "open", "", "review"),
    ]
    new = [findings.Finding("a", "U1", "VDD", "old text"), findings.Finding("b", "R1", "1", "fresh")]
    merged = findings.merge(old, new)
    assert [f.id for f in merged] == ["review:1", "a", "b"]  # hand-written first, "gone" dropped
    assert merged[1].status == "accepted"
    assert merged[1].reason == "on the sensor board"
    assert merged[2].status == "open"
    parsed = findings.parse(__import__("tomllib").loads(findings.dumps(merged)))
    assert parsed == merged


def test_findings_merge_reopens_changed_text() -> None:
    old = [findings.Finding("a", "U1", "VDD", "old text", "accepted", "on the sensor board")]
    (merged,) = findings.merge(old, [findings.Finding("a", "U1", "VDD", "new text")])
    assert (merged.text, merged.status) == ("new text", "open")
    assert merged.reason == findings.REOPENED + "on the sensor board"
    assert merged.blocking
    (again,) = findings.merge([replace(merged, status="accepted")], [findings.Finding("a", "U1", "VDD", "newer")])
    assert again.reason == findings.REOPENED + "on the sensor board"  # the prefix is not stacked


def test_blocking_needs_a_reason() -> None:
    assert findings.Finding("a", "U1", "", "t").blocking
    assert findings.Finding("a", "U1", "", "t", "accepted", "").blocking
    assert not findings.Finding("a", "U1", "", "t", "accepted", "why").blocking


def test_findings_errors() -> None:
    with pytest.raises(findings.FindingsError, match="status must be one of"):
        findings.parse({"finding": [{"id": "a", "text": "t", "status": "done"}]})
    with pytest.raises(findings.FindingsError, match="duplicate"):
        findings.parse({"finding": [{"id": "a", "text": "t"}, {"id": "a", "text": "u"}]})


# --- netlist checks ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("net", "volts"),
    [
        ("+3V3", 3.3),
        ("3V3", 3.3),
        ("V3V3", 3.3),
        ("5V", 5.0),
        ("VBUS_5V", 5.0),
        ("+3.3V", 3.3),
        ("12V", 12.0),
        ("1V8_ANA", 1.8),
    ],
)
def test_name_voltage(net: str, volts: float) -> None:
    assert netcheck.name_voltage(net) == volts


@pytest.mark.parametrize("net", ["GND", "VBAT", "SDA", "NET1", "HV3"])
def test_name_voltage_none(net: str) -> None:
    assert netcheck.name_voltage(net) is None


def test_cap_rating() -> None:
    design = netcheck.load_design(
        circuit(
            [
                c("C1", 1e-5, "+5V", "GND", max_voltage_rating=5),
                c("C2", 1e-5, "+5V", "GND", max_voltage_rating=16),
                c("C3", 1e-5, "+3V3", "GND", manufacturer_part_number="CL10A106 6.3V"),
                c("C4", 1e-5, "+5V", "GND", manufacturer_part_number="CL21 6V"),
                c("C5", 1e-5, "VBAT", "GND", max_voltage_rating=5),
            ]
        )
    )
    spec = intent.parse({"rail": {"VBAT": {"voltage": 4.2}}})
    found = netcheck.check_cap_ratings(design, spec, 0.8)
    assert ids(found) == {"cap_rating:C1", "cap_rating:C4", "cap_rating:C5"}  # C3: 3.3 <= 0.8*6.3
    c1 = next(f for f in found if f.ref == "C1")
    assert c1.pin == "1"
    assert "5 V" in c1.text


def test_i2c_pullups() -> None:
    parts = [
        chip("U1", [("SDA", "I2C_SDA"), ("SCL", "I2C_SCL"), ("VDD", "+3V3"), ("GND", "GND")]),
        chip("U2", [("SDA", "I2C_SDA"), ("SCL", "I2C_SCL"), ("VDD", "+3V3"), ("GND", "GND")]),
        r("R1", 4700, "I2C_SDA", "+3V3"),
        r("R2", 4700, "I2C_SDA", "+3V3"),
        r("R3", 4700, "I2C_SCL", "GND"),  # to ground is not a pull-up
    ]
    found = netcheck.check_i2c(netcheck.load_design(circuit(parts)), NO_INTENT)
    assert ids(found) == {"i2c_pullup:I2C_SCL", "i2c_pullup_multi:I2C_SDA"}
    missing = next(f for f in found if f.id == "i2c_pullup:I2C_SCL")
    assert (missing.ref, missing.pin) == ("U1", "SCL")


def test_decoupling() -> None:
    parts = [
        chip("U1", [("VDD", "+3V3"), ("VDDA", "+3V3A"), ("VCC", None), ("GND", "GND"), ("PA0", "SIG")]),
        c("C1", 1e-7, "+3V3", "GND"),
        c("C2", 1e-7, "+3V3A", "SIG"),  # not to ground
    ]
    found = netcheck.check_decoupling(netcheck.load_design(circuit(parts)), NO_INTENT)
    assert ids(found) == {"decoupling:U1:2", "power_unconnected:U1:3"}


def test_feedback_divider() -> None:
    parts = [
        chip("U1", [("VIN", "+5V"), ("VOUT", "+3V3"), ("FB", "FBN"), ("GND", "GND")]),
        r("R1", 100e3, "+3V3", "FBN"),
        r("R2", 33e3, "FBN", "GND"),
    ]
    design = netcheck.load_design(circuit(parts))
    # 3.3 / (1 + 100/33) = 0.82 V: plausible without intent, wrong against vref 1.2
    assert netcheck.check_feedback(design, NO_INTENT, (0.4, 1.3)) == []
    assert (
        netcheck.check_feedback(design, intent.parse({"rail": {"+3V3": {"regulator": "U1", "vref": 0.8}}}), (0.4, 1.3))
        == []
    )
    bad = netcheck.check_feedback(
        design, intent.parse({"rail": {"+3V3": {"regulator": "U1", "vref": 1.2}}}), (0.4, 1.3)
    )
    assert ids(bad) == {"fb_divider:U1"}
    assert "R1" in bad[0].text
    assert ids(netcheck.check_feedback(design, NO_INTENT, (0.9, 1.3))) == {"fb_divider:U1"}


def test_control_pins() -> None:
    parts = [
        chip("U1", [("VIN", "+5V"), ("VOUT", "+3V3"), ("EN", None), ("GND", "GND")]),
        chip("U2", [("VDD", "+3V3"), ("NRST", "RST_NET"), ("BOOT0", "BOOT0_NET"), ("GND", "GND")]),
        r("R1", 10e3, "RST_NET", "+3V3"),
    ]
    found = netcheck.check_control_pins(netcheck.load_design(circuit(parts)))
    assert ids(found) == {"floating:U1:3", "floating:U2:3"}  # NRST has the pull-up; BOOT0 is alone on its net


def test_find_regulators() -> None:
    parts = [
        chip("U1", [("VIN", "+5V"), ("VOUT", "+3V3"), ("GND", "GND")]),
        chip("U2", [("A", "X"), ("B", "Y"), ("C", "Z")], manufacturer_part_number="AMS1117-3.3"),
        chip("U3", [("VDD", "+3V3"), ("PA0", "X"), ("GND", "GND")]),
    ]
    assert netcheck.find_regulators(netcheck.load_design(circuit(parts))) == ["U1", "U2"]


# --- required sims ---------------------------------------------------------------------------------


def test_missing_sims(tmp_path: Path) -> None:
    deck = sim.parse_deck(tmp_path / "powerup.cir", DECK)
    assert verify.missing_sims(["U1"], [sim.DeckResult(deck, "ok")], DEFAULTS) == []
    assert len(verify.missing_sims(["U1", "U2"], [sim.DeckResult(deck, "ok")], DEFAULTS)) == 1  # U2 not covered
    assert (
        len(verify.missing_sims(["U1"], [sim.DeckResult(deck, "stale")], DEFAULTS)) == 1
    )  # a stale deck covers nothing
    assert "'powerup'" in verify.missing_sims(["U1"], [], DEFAULTS)[0]


# --- the whole step, both modes ----------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "BOARDS_DIR", tmp_path / "boards")
    monkeypatch.setattr(paths, "OUT_DIR", tmp_path / "out")
    (tmp_path / "boards" / "b" / "sim").mkdir(parents=True)
    raw = tmp_path / "out" / "b" / ".work" / "raw"
    raw.mkdir(parents=True)
    parts = [
        chip("U1", [("VIN", "+5V"), ("VOUT", "+3V3"), ("EN", None), ("GND", "GND")]),
        c("C1", 1e-5, "+5V", "GND"),
        c("C2", 1e-5, "+3V3", "GND"),
    ]
    (raw / "b.circuit.json").write_text(json.dumps(circuit(parts)), encoding="utf-8")
    return tmp_path / "boards" / "b"


def test_run_vibe_and_strict(project: Path) -> None:
    vibe = verify.run("b")
    text = "\n".join(vibe.lines)
    assert "[warning] sim_missing: regulator U1" in text
    assert "[warning] finding: floating:U1:3 [U1.EN]" in text
    assert "Mode: vibe" in text
    assert vibe.issues == 3  # + review_missing
    assert (project / "findings.toml").exists()
    strict = verify.run("b", strict=True)
    assert "[error] finding: floating:U1:3" in "\n".join(strict.lines)
    # accepting the finding with a reason closes it; the missing sim stays
    path = project / "findings.toml"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace('status = "open"', 'status = "accepted"')
        .replace('reason = ""', 'reason = "tied by the module"'),
        encoding="utf-8",
    )
    again = verify.run("b", strict=True)
    assert again.issues == 2  # missing sim + review_missing
    assert "1 accepted" in "\n".join(again.lines)


def test_run_with_deck(project: Path) -> None:
    (project / "sim" / "powerup.cir").write_text(
        "powerup\n* covers: powerup U1\n* range: vout_max 3.2 3.4 source=datasheet:X\nR9 a 0 1k\n.meas tran vout_max MAX v(a)\n",
        encoding="utf-8",
    )
    report = verify.run("b")  # R9 is not on the board: stale, whatever ngspice says
    text = "\n".join(report.lines)
    assert "sim_stale: powerup.cir: R9 is in the deck but not on the board" in text
    assert "sim_missing" in text  # a stale deck covers nothing


def test_no_circuit_json_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "OUT_DIR", tmp_path / "out")
    report = verify.run("nope")
    assert report.issues == 0
    assert "skipped" in report.lines[0]


def test_check_status_for_fab(project: Path, tmp_path: Path) -> None:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    (tmp_path / "out" / "b" / ".work" / "reports").mkdir(parents=True)
    cli._write_check_status("b", pcb, True, strict_passed=False)
    assert cli._check_passed("b", pcb)
    assert not cli._check_passed("b", pcb, strict=True)
    cli._write_check_status("b", pcb, True, strict_passed=True)
    assert cli._check_passed("b", pcb, strict=True)
