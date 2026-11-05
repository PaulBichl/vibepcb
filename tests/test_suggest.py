from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING

import pytest
from test_placement import LIMITS, Spec, board, cap, mcu

from vibepcb import cli
from vibepcb.check import placement, suggest
from vibepcb.core import paths
from vibepcb.routing import ratsnest, routes

if TYPE_CHECKING:
    from pathlib import Path


def flipped_cap(ref: str, x: float, y: float, net: str) -> Spec:
    """A horizontal 0603 capacitor with pin 1 (``net``) on the right, pin 2 (GND) on the left."""
    return ref, "simple_capacitor", {"capacitance": 1e-7}, (x, y), (1.6, 0.8), [
        ("pin1", net, (x + 0.8, y)),
        ("pin2", "GND", (x - 0.8, y)),
    ]  # fmt: skip


def resistor(ref: str, x: float, y: float, net1: str, net2: str) -> Spec:
    return ref, "simple_resistor", {"resistance": 1e4}, (x, y), (1.6, 0.8), [
        ("pin1", net1, (x - 0.8, y)),
        ("pin2", net2, (x + 0.8, y)),
    ]  # fmt: skip


def chip(ref: str, x: float, y: float, pins: list[tuple[str, str, tuple[float, float]]]) -> Spec:
    return ref, "simple_chip", {}, (x, y), (4, 4), pins


# --- moves ----------------------------------------------------------------------------------------------------


def test_far_decoupling_cap_goes_next_to_its_pin() -> None:
    result = suggest.suggest(board([mcu(), cap("C2", 15.0, 10, "+3V3")]), LIMITS, ["GND"])
    assert [s.link.ref for s in result.suggestions] == ["C2"]
    s = result.suggestions[0]
    assert s.link.rule == "decoupling"
    assert s.link.target_pin == "2"  # VDD2 at (2, 0)
    assert s.after <= LIMITS.decoupling_mm < s.before
    assert s.spot.centre[0] > 2  # on the side the pin faces (+x), clear of the chip
    assert s.spot.pads["1"][0] < s.spot.pads["2"][0]  # the +3V3 pad first, GND pad away from the chip
    assert s.ratsnest < 0
    assert result.after[0] < result.before[0]


def test_cap_is_turned_to_put_its_pad_first() -> None:
    result = suggest.suggest(board([mcu(), flipped_cap("C2", 15.0, 10, "+3V3")]), LIMITS, ["GND"])
    s = result.suggestions[0]
    assert s.spot.turn == 180
    assert s.new_rotation == 180
    assert s.spot.pads["1"][0] < s.spot.pads["2"][0]


def test_parts_within_their_limit_only_with_everything() -> None:
    circuit = board([mcu(), cap("C1", 4.5, 0, "+3V3")])  # pad 1.7 mm from VDD2: within 3 mm
    assert suggest.suggest(circuit, LIMITS, ["GND"]).suggestions == []
    assert suggest.suggest(circuit, LIMITS, ["GND"], everything=True).candidates == 1


def test_signal_part_next_to_its_chip_pin_but_not_a_thermistor() -> None:
    u2 = chip("U2", 0, 0, [("IN", "SENSE", (2, 0)), ("A", "X1", (-2, 0)), ("B", "X2", (0, -2))])
    parts = [u2, resistor("R1", 15, 10, "SENSE", "GND"), resistor("RT1", -15, 10, "SENSE", "GND")]
    result = suggest.suggest(board(parts), LIMITS, ["GND"])
    assert [(s.link.ref, s.link.rule, s.link.target_pin) for s in result.suggestions] == [("R1", "signal", "1")]


def test_no_suggestion_when_the_spot_is_not_closer() -> None:
    result = suggest.suggest(board([mcu(), cap("C2", 3.05, 0, "+3V3")]), LIMITS, ["GND"], everything=True)
    assert result.suggestions == []  # its pad is 0.25 mm from VDD2 already
    assert [(k.ref, why.split(" (")[0]) for k, why in result.stuck] == [("C2", "no spot closer than now")]


def test_free_spot_turns_a_tight_part_in_place() -> None:
    # an inductor whose far pad is on the SW net, boxed in by its neighbours: only turning it where it is helps
    sw: Spec = (
        "U3",
        "simple_chip",
        {},
        (0, 0),
        (4, 4),
        [("SW", "SW", (2, 0)), ("VIN", "VIN", (-2, 0)), ("GND", "GND", (0, -2))],
    )
    ind: Spec = (
        "L1",
        "simple_inductor",
        {"inductance": 1e-5},
        (5.1, 0),
        (6, 3),
        [("pin1", "VOUT", (3.1, 0)), ("pin2", "SW", (7.1, 0))],
    )
    walls = [chip(f"U{i}", 5.1, y, []) for i, y in ((4, 3.9), (5, -3.9))]  # and U3 0.1 mm to its left
    layout = placement.analyse(board([sw, ind, *walls]), LIMITS).layout
    spot = placement.free_spot(layout, "L1", (2, 0), 6, pin="2", facing=(1.0, 0.0))
    assert spot is not None
    assert spot.centre == (5.1, 0)  # off the grid: where it is
    assert spot.turn == 180
    assert math.dist(spot.pads["2"], (2, 0)) == pytest.approx(1.1)


def test_free_spot_keeps_clear_of_keepouts_and_holes() -> None:
    circuit = board([mcu(), cap("C2", 15.0, 10, "+3V3")])
    circuit.append(
        {"type": "pcb_keepout", "shape": "rect", "center": {"x": 5, "y": 0}, "width": 4, "height": 6, "layers": ["top"]}
    )
    circuit.append({"type": "pcb_hole", "x": 2, "y": 4, "hole_diameter": 2})
    layout = placement.analyse(circuit, LIMITS).layout
    spot = placement.free_spot(layout, "C2", (2, 0), 8, pin="1", facing=(1.0, 0.0))
    assert spot is not None
    for box, _ in layout.blocked:
        assert not placement._overlap(spot.box, box)


# --- ratsnest -------------------------------------------------------------------------------------------------


def test_pad_links_skip_plane_nets() -> None:
    pads = [
        routes.Pad("A", "1", "N", (0, 0), frozenset()),
        routes.Pad("B", "1", "N", (3, 4), frozenset()),
        routes.Pad("A", "2", "GND", (0, 1), frozenset()),
        routes.Pad("B", "2", "GND", (9, 9), frozenset()),
    ]
    assert [(k.net, k.length) for k in ratsnest.pad_links(pads, {"GND"})] == [("N", 5.0)]


# --- TSX ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "old", "new", "out"),
    [
        ("{3}", 3, 2.5, "{2.5}"),
        ("{YL + 4}", 54, 51.5, "{YL + 1.5}"),
        ("{XA - 5}", 13, 18, "{XA}"),
        ("{-XA - 4.5}", -22.5, -20, "{-XA - 2}"),
        ("{XA}", 18, 19.25, "{XA + 1.25}"),
        ('"3mm"', 3, 4, "{4}"),
        (None, 0, 1.5, "{1.5}"),
        ("{TOP - 30}", 37.5, 37.5, "{TOP - 30}"),
    ],
)
def test_shifted(expr: str | None, old: float, new: float, out: str) -> None:
    assert suggest.shifted(expr, old, new) == out


TSX = """const C = ({ name, ...p }) => <capacitor name={name} {...p} />
const Block = ({ x }) => <resistor name="R9" pcbX={x + 1} pcbY={2} />
export default () => (
  <board>
    <C name="C5" v="470pF" pcbX={XA + 4.5} pcbY={YL + 4}
       pcbRotation={90} />
    <C name="C7" {...bot(13, 3.0)} />
    {[1, 2].map((n) => <C name={`C${n}0`} />)}
  </board>
)
"""


def test_locate_and_edit() -> None:
    files = {"index.circuit.tsx": TSX}
    c5 = suggest.locate(files, "C5")
    assert (c5.file, c5.line, c5.note) == ("index.circuit.tsx", 5, "")
    assert c5.attrs == {"pcbX": "{XA + 4.5}", "pcbY": "{YL + 4}", "pcbRotation": "{90}"}
    c7 = suggest.locate(files, "C7")
    assert c7.attrs["spread"] == "bot"
    assert "sub-component" in suggest.locate(files, "R9").note
    assert "generated" in suggest.locate(files, "C10").note
    assert "group G" in suggest.locate(files, "C5", "G").note

    link = placement.Link("decoupling", "C5", "1", "U1", "2", "VDD", "V", 5, 3, (0, 0))
    spot = placement.Spot("C5", (22.5, 51.5), 90, (0, 0, 1, 1), {"1": (0, 0)})
    s = suggest.Suggestion(link, spot, 5, 1, (22.5, 54.0), 90, -1.0, c5)
    assert suggest.edit(s) == "pcbX={XA + 4.5} pcbY={YL + 1.5} pcbRotation={180}"
    assert suggest.move(s) == "move dx=+0 dy=-2.5, turn +90 deg"
    spread = suggest.Suggestion(link, spot, 5, 1, (13, 3), 90, -1.0, c7)
    assert suggest.edit(spread) == "{...bot(22.5, 51.5)} pcbRotation={180}"


# --- command --------------------------------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "BOARDS_DIR", tmp_path / "boards")
    monkeypatch.setattr(paths, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    src = tmp_path / "boards" / "b"
    src.mkdir(parents=True)
    (src / "index.circuit.tsx").write_text(
        'export default () => (\n  <board>\n    <capacitor name="C2" pcbX={15} pcbY={10} />\n  </board>\n)\n',
        encoding="utf-8",
    )
    raw = tmp_path / "out" / "b" / ".work" / "raw"
    raw.mkdir(parents=True)
    (raw / "b.unrouted.circuit.json").write_text(
        json.dumps(board([mcu(), cap("C2", 15.0, 10, "+3V3")])), encoding="utf-8"
    )
    return src


def test_place_suggest_command(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["place-suggest", "b"]) == 0
    out = capsys.readouterr().out
    assert "C2 -> U1.VDD2 (pin 2) on +3V3 [decoupling, limit 3 mm]" in out
    assert "index.circuit.tsx:3  now   pcbX={15} pcbY={10}" in out
    assert "edit  pcbX={" in out
    assert "1 part(s) to place, 1 suggestion(s), 0 without one; ratsnest" in out
