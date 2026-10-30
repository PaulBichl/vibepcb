from __future__ import annotations

import pytest

from vibepcb.parts import ipc7351
from vibepcb.parts.spec import Dim, ExposedPad, Package


def d(lo: float, hi: float) -> Dim:
    return Dim(lo, (lo + hi) / 2, hi)


SOIC8 = Package(
    family="SOIC",
    pins=8,
    pitch=1.27,
    body_l=d(4.8, 5.0),
    body_w=d(3.8, 4.0),
    height=1.75,
    span=d(5.8, 6.2),
    lead_l=d(0.4, 1.27),
    lead_w=d(0.31, 0.51),
    page=1,
)
R0603 = Package(
    family="chip", pins=2, pitch=0, body_l=d(1.45, 1.75), body_w=d(0.65, 0.95), height=0.55, lead_l=d(0.15, 0.4), page=1
)
QFN16 = Package(
    family="QFN",
    pins=16,
    pitch=0.5,
    body_l=d(2.9, 3.1),
    body_w=d(2.9, 3.1),
    height=1.0,
    span=d(2.9, 3.1),
    lead_l=d(0.3, 0.5),
    lead_w=d(0.18, 0.3),
    exposed_pad=ExposedPad(d(1.6, 1.8), d(1.6, 1.8), "17"),
    page=1,
)


def test_soic8_matches_kicad_ipc_nominal() -> None:
    # KiCad library SOIC-8_3.9x4.9mm_P1.27mm (JEDEC MS-012AA, IPC-7351B nominal): 1.95 x 0.6 at x = +-2.475
    pattern = ipc7351.calculate(SOIC8)
    row = pattern.rows[0]
    assert (row.z, row.g, row.x) == (6.9, 3.0, 0.6)
    assert pattern.pads[0] == ipc7351.Pad("1", -2.475, -1.905, 1.95, 0.6)
    assert pattern.pads[4] == ipc7351.Pad("5", 2.475, 1.905, 1.95, 0.6)  # pin 5 bottom right, counting up
    assert pattern.name == "SOIC127P600X175-8N"
    assert pattern.fillets.source.startswith("IPC-7351B table 3-2")


def test_0603_matches_kicad_outer_span_and_width() -> None:
    # KiCad R_0603_1608Metric (IPC-7351 nominal): Z = 2 x 0.825 + 0.8 = 2.45, X = 0.95
    pattern = ipc7351.calculate(R0603, kind="resistor")
    row = pattern.rows[0]
    assert (row.z, row.x) == (2.45, 0.95)
    assert [p.number for p in pattern.pads] == ["1", "2"]
    assert pattern.pads[0].x < 0 < pattern.pads[1].x
    assert pattern.name == "RESC1608X55N"


def test_small_chip_uses_its_table() -> None:
    small = Package(
        family="chip",
        pins=2,
        pitch=0,
        body_l=d(0.95, 1.05),
        body_w=d(0.45, 0.55),
        height=0.4,
        lead_l=d(0.1, 0.3),
        page=1,
    )
    f = ipc7351.fillets(small, "N")
    assert (f.toe, f.round_off) == (0.2, 0.02)


def test_qfn_pin_order_and_exposed_pad() -> None:
    pattern = ipc7351.calculate(QFN16)
    pads = {p.number: p for p in pattern.pads}
    assert len(pads) == 17
    assert pads["1"].x < 0
    assert pads["1"].y < 0
    assert pads["4"].x < 0
    assert pads["4"].y > 0
    assert pads["5"].y > 0
    assert pads["5"].x < 0
    assert pads["9"].x > 0
    assert pads["9"].y > 0
    assert pads["13"].y < 0
    assert pads["13"].x > 0
    assert pads["5"].w > pads["5"].l  # bottom pads stand upright
    assert (pads["17"].x, pads["17"].y, pads["17"].l) == (0.0, 0.0, 1.7)
    assert pattern.name == "QFN50P300X300X100-17N"
    assert pattern.fillets.toe == 0.3
    # terminal pads keep 0.2 mm from the exposed pad (inner end trimmed, outer end kept)
    ep = pads["17"]
    for n in ("1", "5", "9", "13"):
        b = pads[n]
        gap = max(abs(b.x) - b.l / 2 - ep.l / 2, abs(b.y) - b.w / 2 - ep.w / 2)
        assert gap >= ipc7351.EP_CLEARANCE - 1e-9
    assert pads["1"].x - pads["1"].l / 2 == pytest.approx(-pattern.rows[0].z / 2)
    # pads stay apart at the corner
    assert pads["1"].x + pads["1"].l / 2 < pads["16"].x - pads["16"].l / 2


def test_density_levels_differ() -> None:
    z = {density: ipc7351.calculate(SOIC8, density).rows[0].z for density in ("L", "N", "M")}
    assert z["L"] < z["N"] < z["M"]


def test_custom_family_has_no_calculation() -> None:
    custom = Package(family="custom", pins=1, pitch=0, body_l=d(1, 1), body_w=d(1, 1), height=1, page=1)
    with pytest.raises(ValueError, match="explicit"):
        ipc7351.calculate(custom)


@pytest.mark.parametrize(
    ("pins", "expected"),
    [
        (3, {"1": (-1, -0.95), "2": (-1, 0.95), "3": (1, 0.0)}),
        (5, {"1": (-1, -0.95), "2": (-1, 0.0), "3": (-1, 0.95), "4": (1, 0.95), "5": (1, -0.95)}),
        (6, {"1": (-1, -0.95), "3": (-1, 0.95), "4": (1, 0.95), "6": (1, -0.95)}),
    ],
)
def test_sot23_pin_slots(pins: int, expected: dict[str, tuple[int, float]]) -> None:
    sot = Package(
        family="SOT",
        pins=pins,
        pitch=0.95,
        body_l=d(2.8, 3.0),
        body_w=d(1.5, 1.7),
        height=1.45,
        span=d(2.6, 3.0),
        lead_l=d(0.3, 0.6),
        lead_w=d(0.3, 0.5),
        page=1,
    )
    pads = {p.number: p for p in ipc7351.calculate(sot).pads}
    for number, (side, y) in expected.items():
        assert (pads[number].x > 0) == (side > 0)
        assert pads[number].y == y
    assert ipc7351.calculate(sot).name == f"SOT95P280X145-{pins}N"
