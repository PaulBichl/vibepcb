from __future__ import annotations

from vibepcb import hotspots, routes


def _violation(kind: str, a: tuple[float, float], b: tuple[float, float], nets: tuple[str, str]) -> dict[str, object]:
    return {
        "type": kind,
        "severity": "error",
        "items": [
            {"description": f"Track [{nets[0]}] on F.Cu", "pos": {"x": a[0], "y": a[1]}},
            {"description": f"Via [{nets[1]}] on F.Cu - B.Cu", "pos": {"x": b[0], "y": b[1]}},
        ],
    }


def test_errors_group_into_spots_with_their_nets_and_parts() -> None:
    drc = {
        "violations": [
            _violation("clearance", (10, 10), (10.2, 10), ("GND", "MISO")),
            _violation("clearance", (10.8, 10.4), (11, 10.4), ("GND", "SCK")),
            _violation("track_angle", (40, 40), (40.1, 40), ("VCC", "VCC")),
            {"type": "silk_overlap", "severity": "warning", "items": [{"description": "x", "pos": {"x": 0, "y": 0}}]},
        ],
        "unconnected_items": [_violation("unconnected_items", (70, 70), (71, 70), ("GND", "GND"))],
    }
    errors = hotspots.drc_errors(drc)
    assert len(errors) == 4  # the warning is no error
    spots = hotspots.group(errors)
    assert [len(s.errors) for s in spots] == [2, 1, 1]  # the two clearance errors 1 mm apart are one spot
    pads = [
        routes.Pad("U9", "14", "MISO", (10.5, 10.8), frozenset({"F.Cu"})),
        routes.Pad("R1", "1", "X", (30, 30), frozenset({"F.Cu"})),
    ]
    lines = hotspots.describe(spots[0], pads, origin=(10, 10))
    assert lines[0].startswith("2 error(s) (10.1,10.0)-(10.9,10.4)  tsx (0.5, -0.2)")
    assert "GND x2" in lines[2]
    assert "U9 (1 pads)" in lines[3]
    assert "R1" not in lines[3]
