from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

import pytest

from vibepcb import cli
from vibepcb.check import findings, placement
from vibepcb.core import paths
from vibepcb.settings import config

if TYPE_CHECKING:
    from pathlib import Path

LIMITS = placement.Limits()
# (ref, ftype, props, centre, (w, h), [(pin name, net, (x, y) pad)])
Spec = tuple[Any, ...]


def board(
    parts: list[Spec], size: tuple[float, float] = (60, 40), extra: dict[str, dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Circuit JSON with source + pcb elements; ``extra``: ref -> fields for its pcb_component (insertion_direction)
    or ``courtyard`` -> [x0, y0, x1, y1]."""
    out: list[dict[str, Any]] = [
        {"type": "pcb_board", "center": {"x": 0, "y": 0}, "width": size[0], "height": size[1]},
    ]
    nets: set[str] = set()
    for i, (ref, ftype, props, centre, (w, h), pins) in enumerate(parts):
        more = dict((extra or {}).get(ref, {}))
        courtyard = more.pop("courtyard", None)
        out.append({"type": "source_component", "source_component_id": f"c{i}", "name": ref, "ftype": ftype, **props})
        out.append(
            {
                "type": "pcb_component",
                "pcb_component_id": f"pc{i}",
                "source_component_id": f"c{i}",
                "center": {"x": centre[0], "y": centre[1]},
                "width": w,
                "height": h,
                "layer": "top",
                **more,
            }
        )
        if courtyard:
            x0, y0, x1, y1 = courtyard
            out.append(
                {
                    "type": "pcb_courtyard_rect",
                    "pcb_component_id": f"pc{i}",
                    "center": {"x": (x0 + x1) / 2, "y": (y0 + y1) / 2},
                    "width": x1 - x0,
                    "height": y1 - y0,
                }
            )
        for n, (pin, net, (x, y)) in enumerate(pins, 1):
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
            out.append(
                {
                    "type": "pcb_port",
                    "pcb_port_id": f"pp{i}_{n}",
                    "pcb_component_id": f"pc{i}",
                    "source_port_id": f"p{i}_{n}",
                    "x": x,
                    "y": y,
                }
            )
    out += [
        {"type": "source_net", "name": n, "source_net_id": f"n_{n}", "subcircuit_connectivity_map_key": f"key_{n}"}
        for n in sorted(nets)
    ]
    return out


def cap(ref: str, x: float, y: float, net: str, farad: float = 1e-7, gnd: str = "GND") -> Spec:
    """A horizontal 0603 capacitor: pin 1 (``net``) left, pin 2 (``gnd``) right."""
    return (
        ref,
        "simple_capacitor",
        {"capacitance": farad},
        (x, y),
        (1.6, 0.8),
        [("pin1", net, (x - 0.8, y)), ("pin2", gnd, (x + 0.8, y))],
    )


def mcu(x: float = 0, y: float = 0, vdd: str = "+3V3") -> Spec:
    """A 4 x 4 mm chip: VDD1 left, VDD2 right, GND bottom, two clock pins top."""
    pins = [
        ("VDD1", vdd, (x - 2, y)),
        ("VDD2", vdd, (x + 2, y)),
        ("GND", "GND", (x, y - 2)),
        ("OSC_IN", "XIN", (x - 0.5, y + 2)),
        ("OSC_OUT", "XOUT", (x + 0.5, y + 2)),
    ]
    return ("U1", "simple_chip", {}, (x, y), (4, 4), pins)


def ids(found: list[findings.Finding]) -> set[str]:
    return {f.id for f in found}


def run(parts: list[Spec], **kw: Any) -> list[findings.Finding]:
    return placement.run(board(parts, **kw), LIMITS)[1]


# --- decoupling ----------------------------------------------------------------------------------------------


def test_decoupling_near_and_far() -> None:
    near = cap("C1", -3.5, 0, "+3V3")  # pin 1 at -4.3: 2.3 mm from VDD1
    far = cap("C2", 8.0, 0, "+3V3")  # pin 1 at 7.2: 5.2 mm from VDD2
    found = run([mcu(), near, far])
    assert ids(found) == {"place_decoupling:C2"}
    f = found[0]
    assert f.ref == "C2"
    assert f.pin == "1"
    assert f.source == placement.SOURCE
    assert "5.2 mm from the power pin it decouples U1.VDD2 (pin 2) on +3V3 (limit 3 mm)" in f.text
    assert "free spot next to it: pcbX=" in f.text


def test_caps_share_out_the_pins() -> None:
    """Two caps by VDD1: one serves it, the other is judged against VDD2 (each pin wants its own cap)."""
    a, b = cap("C1", -3.5, 0.5, "+3V3"), cap("C2", -3.5, -0.5, "+3V3")
    analysis = placement.analyse(board([mcu(), a, b]), LIMITS)
    served = {k.ref: k.target_name for k in analysis.links if k.rule == "decoupling"}
    assert sorted(served.values()) == ["VDD1", "VDD2"]
    assert len(placement.findings(analysis)) == 1  # the one sent to VDD2 is 5+ mm from it


def test_bulk_and_signal_caps_are_not_decoupling() -> None:
    bulk = cap("C1", 15, 0, "+3V3", farad=1e-5)
    signal = cap("C2", 15, 5, "SENSE")
    chip: Spec = (
        "U2",
        "simple_chip",
        {},
        (20, 5),
        (2, 2),
        [("IN", "SENSE", (19, 5)), ("VDD", "+3V3", (21, 5)), ("GND", "GND", (20, 4))],
    )
    found = run([mcu(), bulk, signal, chip])
    assert not {"place_decoupling:C1", "place_decoupling:C2"} & ids(found)


def test_free_spot_avoids_parts_and_other_suggestions() -> None:
    analysis = placement.analyse(board([mcu(), cap("C1", 10, 10, "+3V3"), cap("C2", 12, 10, "+3V3")]), LIMITS)
    taken: list[placement.Box] = []
    spots = [placement.free_spot(analysis.layout, ref, (2, 0), taken=taken) for ref in ("C1", "C2")]
    assert spots[0] is not None
    assert spots[1] is not None
    assert spots[0] != spots[1]
    u1 = analysis.layout.bodies["U1"].box
    for x, y in spots:  # type: ignore[misc]
        assert not (u1[0] - 0.8 < x < u1[2] + 0.8 and u1[1] - 0.4 < y < u1[3] + 0.4)  # clear of the chip
    assert placement.free_spot(analysis.layout, "C1", (2, 0), radius=0.5) is None  # only the chip within reach


# --- crystals ------------------------------------------------------------------------------------------------


def test_crystal_and_load_caps() -> None:
    xtal: Spec = ("Y1", "simple_crystal", {}, (0, 9), (3, 1.2), [("pin1", "XIN", (-1, 9)), ("pin2", "XOUT", (1, 9))])
    found = run([mcu(), xtal, cap("C5", -1.8, 11, "XIN"), cap("C6", 6, 9, "XOUT")])
    assert ids(found) == {"place_crystal:Y1", "place_load_cap:C6"}
    text = next(f.text for f in found if f.id == "place_crystal:Y1")
    assert "from the clock pin it drives U1.OSC_" in text
    assert "(limit 5 mm)" in text


def test_oscillator_output_only() -> None:
    osc: Spec = (
        "X1",
        "simple_chip",
        {},
        (0, 12),
        (3, 2),
        [("EN", "+3V3", (-1, 11)), ("GND", "GND", (1, 11)), ("OUT", "XIN", (1, 13)), ("VDD", "+3V3", (-1, 13))],
    )
    analysis = placement.analyse(board([mcu(), osc]), LIMITS)
    crystal = [k for k in analysis.links if k.rule == "crystal"]
    assert [(k.ref, k.pin, k.target_name) for k in crystal] == [("X1", "3", "OSC_IN")]


# --- regulators ----------------------------------------------------------------------------------------------


def buck(l_x: float) -> list[Spec]:
    reg: Spec = (
        "U3",
        "simple_chip",
        {},
        (0, 0),
        (3, 3),
        [
            ("VIN", "VIN", (-1.5, 0.5)),
            ("SW", "SW", (1.5, 0.5)),
            ("GND", "GND", (-1.5, -0.5)),
            ("FB", "FB", (1.5, -0.5)),
        ],
    )
    ind: Spec = (
        "L1",
        "simple_inductor",
        {"inductance": 1e-5},
        (l_x, 0.5),
        (4, 4),
        [("pin1", "SW", (l_x - 1.5, 0.5)), ("pin2", "+5V", (l_x + 1.5, 0.5))],
    )
    load: Spec = (
        "U9",
        "simple_chip",
        {},
        (25, 0),
        (2, 2),
        [("VDD", "+5V", (24, 0)), ("GND", "GND", (26, 0)), ("X", None, (25, 1))],
    )
    return [reg, ind, load, cap("C1", -3.5, 0.5, "VIN", farad=1e-5), cap("C2", l_x + 4, 3, "+5V", farad=2.2e-5)]


def test_regulator_loop_compact() -> None:
    assert run(buck(4.0)) == []


def test_regulator_inductor_far() -> None:
    found = run(buck(14.0))  # SW pad 11 mm from the SW pin; the output cap follows the inductor
    assert ids(found) == {"place_regulator:L1"}
    assert "U3.SW" in found[0].text


# --- connectors ----------------------------------------------------------------------------------------------


def usb(x: float, y: float, **more: Any) -> tuple[Spec, dict[str, Any]]:
    """A USB-C-like part mated from +y: pads at the back (y - 2), the body reaching 5 mm forward."""
    pins = [("VBUS", "VBUS", (x - 1, y - 2)), ("GND", "GND", (x + 1, y - 2)), ("DP", "DP", (x, y - 2))]
    spec: Spec = ("J1", "simple_chip", {}, (x, y), (9, 7), pins)
    return spec, {"courtyard": [x - 4.5, y - 3, x + 4.5, y + 4], **more}


def test_connector_at_edge_passes() -> None:
    spec, extra = usb(0, 16)  # body front at y 20 = board edge
    assert run([spec], extra={"J1": extra}) == []


def test_connector_inside_board() -> None:
    spec, extra = usb(0, 0)
    found = run([spec], extra={"J1": extra})
    assert ids(found) == {"place_connector:J1"}
    assert "mating side (+y) is 16.0 mm from the board edge" in found[0].text
    assert "move it to pcbX=0 pcbY=16" in found[0].text


def test_connector_with_inferred_side_at_another_edge_is_trusted() -> None:
    spec, extra = usb(0, -16.5)  # back at the bottom edge: the guess (+y) may be wrong, the designer put it there
    assert run([spec], extra={"J1": extra}) == []


def test_connector_explicit_direction_suggests_turn() -> None:
    spec, extra = usb(0, -16.5, insertion_direction="from_top")
    found = run([spec], extra={"J1": extra})
    assert ids(found) == {"place_connector:J1"}
    assert "turn it by 180 deg (pcbRotation) to face -y" in found[0].text


def test_top_entry_and_symmetric_headers_skipped() -> None:
    header: Spec = ("J2", "simple_pin_header", {}, (0, 0), (5, 2.5), [("1", "A", (-1.27, 0)), ("2", "B", (1.27, 0))])
    assert run([header]) == []
    assert run([header], extra={"J2": {"insertion_direction": "from_above"}}) == []


def test_ray_to_edge() -> None:
    square = [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]
    assert placement.ray_to_edge(square, (0, 0), (1, 0)) == pytest.approx(10)
    assert placement.ray_to_edge(square, (12, 0), (1, 0)) == pytest.approx(-2)  # past the edge: the overhang


# --- limits, config, cli -------------------------------------------------------------------------------------


def test_limits_keys_match_config() -> None:
    assert {f.name for f in dataclasses.fields(placement.Limits)} == set(config.PLACEMENT_KEYS)


def test_parse_limits() -> None:
    limits = placement.parse_limits({"decoupling_mm": 2, "crystal_mm": 4}, {"decoupling_mm": 5})
    assert limits.decoupling_mm == 5
    assert limits.crystal_mm == 4
    assert limits.load_cap_mm == 3
    with pytest.raises(placement.PlacementError, match="unknown key"):
        placement.parse_limits({"decouple": 1})
    with pytest.raises(placement.PlacementError, match="number"):
        placement.parse_limits({"crystal_mm": "near"})
    with pytest.raises(config.ConfigError, match=r"\[placement\]"):
        config.validate({"placement": {"crystal": 1}})


def test_limits_change_the_verdict() -> None:
    parts = [mcu(), cap("C2", 8.0, 0, "+3V3")]
    loose = dataclasses.replace(LIMITS, decoupling_mm=6)
    assert placement.run(board(parts), loose)[1] == []


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "BOARDS_DIR", tmp_path / "boards")
    monkeypatch.setattr(paths, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    (tmp_path / "boards" / "b").mkdir(parents=True)
    (tmp_path / "boards" / "b" / "index.circuit.tsx").write_text("", encoding="utf-8")
    raw = tmp_path / "out" / "b" / ".work" / "raw"
    raw.mkdir(parents=True)
    (raw / "b.unrouted.circuit.json").write_text(
        json.dumps(board([mcu(), cap("C2", 8.0, 0, "+3V3")])), encoding="utf-8"
    )
    return tmp_path / "boards" / "b"


def test_place_check_command(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["place-check", "b"]) == 0
    out = capsys.readouterr().out
    assert "[warning] place_decoupling:C2" in out
    assert "1 placement finding(s), 0 accepted" in out
    (project / "vibepcb.toml").write_text("[placement]\ndecoupling_mm = 6\n", encoding="utf-8")
    assert cli.main(["place-check", "b"]) == 0
    assert "0 placement finding(s)" in capsys.readouterr().out


def test_merge_keeps_netcheck_findings() -> None:
    net = findings.Finding("floating:U1:3", "U1", "EN", "floating")
    placed = findings.Finding("place_decoupling:C2", "C2", "1", "far", source=placement.SOURCE)
    merged = findings.merge(findings.merge([], [net]), [placed], source=placement.SOURCE)
    assert ids(merged) == {"floating:U1:3", "place_decoupling:C2"}
    assert ids(findings.merge(merged, [], source=placement.SOURCE)) == {"floating:U1:3"}  # fixed: dropped
