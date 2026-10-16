from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest

from vibepcb import fanout, fixes, sexpr

if TYPE_CHECKING:
    from pathlib import Path

    from vibepcb.sexpr import Node

PITCH = 0.8


def _grid(cols: list[int], rows: int, net: str = "sig") -> list[fanout.Ball]:
    """Balls on the given columns (0-based) and rows, centred; every ball its own net (``<net><n>``)."""
    width = max(cols)
    balls: list[fanout.Ball] = []
    for c in cols:
        for r in range(rows):
            x, y = (c - width / 2) * PITCH, (r - (rows - 1) / 2) * PITCH
            balls.append(fanout.Ball(f"pin{len(balls) + 1}", round(x, 6), round(y, 6), 0.18, f"{net}{len(balls)}"))
    return balls


def test_sdram_54_ball_grid_escapes_cleanly() -> None:
    """TFBGA-54 layout: 3 + 3 columns with three empty ones in between, 9 rows (IS42S16160J / W9825G6JB)."""
    balls = _grid([0, 1, 2, 6, 7, 8], 9)
    plan = fanout.plan("U2", balls)
    assert fanout.violations(plan) == []
    assert {e.pin for e in plan.escapes} == {b.pin for b in balls}
    # outer balls straight out on top; only balls two deep need a dogbone via
    assert all(len(e.vias) <= 1 for e in plan.escapes)
    assert any(e.vias for e in plan.escapes)
    assert all(not e.vias for e in plan.escapes if e.end_layer == "top")


def test_escape_ends_lie_outside_the_keepout() -> None:
    plan = fanout.plan("U2", _grid([0, 1, 2, 6, 7, 8], 9))
    x0, y0, x1, y1 = plan.keepout
    half = fanout.STANDIN_PAD_MM / 2
    for e in plan.escapes:
        x, y = e.end
        assert not (x0 < x - half and x + half < x1 and y0 < y - half and y + half < y1), e
        # the escape itself stays inside the keepout up to its end (tscircuit can't see it)
        for _, pts in e.tracks:
            for px, py in pts[:-1]:
                assert x0 <= px <= x1
                assert y0 <= py <= y1


def test_unconnected_balls_are_not_escaped() -> None:
    balls = [
        fanout.Ball(b.pin, b.x, b.y, b.radius, None if i % 3 == 0 else b.net) for i, b in enumerate(_grid([0, 1, 2], 4))
    ]
    plan = fanout.plan("U1", balls)
    assert {e.pin for e in plan.escapes} == {b.pin for b in balls if b.net is not None}
    assert fanout.violations(plan) == []


def test_side_follows_the_rest_of_the_net() -> None:
    """A ball as deep from the west as from the east leaves towards its net's other pads."""
    balls = _grid([0, 1, 2], 3)
    middle = next(b for b in balls if b.x == 0 and b.y == 0)
    east = fanout.Ball(middle.pin, 0.0, 0.0, 0.18, middle.net, towards=(20.0, 0.0))
    plan = fanout.plan("U1", [east if b is middle else b for b in balls])
    assert next(e for e in plan.escapes if e.pin == middle.pin).side == "E"


def test_off_grid_ball_is_rejected() -> None:
    balls = [*_grid([0, 1], 2), fanout.Ball("pin9", 0.33, 0.0, 0.18, "x")]
    with pytest.raises(fanout.EscapeError, match="off the"):
        fanout.plan("U1", balls)


def test_plan_round_trips_through_json() -> None:
    plan = fanout.plan("U2", _grid([0, 1, 2, 6, 7, 8], 9))
    again = fanout.Plan.from_json(plan.to_json())
    assert again == plan


def test_inject_stand_in_and_keepout() -> None:
    plan = fanout.plan("U2", _grid([0, 1], 2))
    tsx = '<board>\n    <IS42 name="U2" pcbX={24} pcbRotation={90} />\n    <R name="U22" />\n</board>'
    out = fanout.inject(tsx, "U2", fanout.standin_tsx(plan), fanout.keepout_tsx(plan, 24, 0, 90, ["top", "bottom"]))
    assert '<IS42 name="U2" pcbX={24} pcbRotation={90} footprint={<footprint><smtpad' in out
    assert '<keepout shape="rect" pcbX={24}' in out
    assert '<R name="U22" />' in out  # only the named part
    with pytest.raises(fanout.EscapeError):
        fanout.inject(tsx, "U9", "", "")


STANDIN_PCB = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (net 0 "")
  (net 1 "SD_DQ0")
  (net 2 "SD_DQ1")
  (footprint "tscircuit:U2" (layer "F.Cu") (at 100 50 0)
    (property "Reference" "U2" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at {x0} {y0} 0) (size 0.2 0.2) (layers F.Cu F.Paste F.Mask) (net 1 "SD_DQ0") (uuid a1))
    (pad "2" smd rect (at {x1} {y1} 0) (size 0.2 0.2) (layers B.Cu B.Paste B.Mask) (net 2 "SD_DQ1") (uuid a2))
  )
  (zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (uuid z1) (hatch edge 0.5)
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
    (polygon (pts (xy {kx0} {ky0}) (xy {kx1} {ky0}) (xy {kx1} {ky1}) (xy {kx0} {ky1})))
  )
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid z2) (hatch edge 0.5)
    (polygon (pts (xy 80 30) (xy 120 30) (xy 120 70) (xy 80 70)))
    (polygon (pts (xy {kx0} {ky0}) (xy {kx1} {ky0}) (xy {kx1} {ky1}) (xy {kx0} {ky1})))
    (polygon (pts (xy 85 35) (xy 86 35) (xy 86 36) (xy 85 36)))
  )
)
"""


def _child(node: Node, key: str) -> list[Node]:
    assert isinstance(node, list)
    found = sexpr.child(node, key)
    assert found is not None
    return found


def test_apply_escapes_restores_balls_and_adds_escapes(tmp_path: Path) -> None:
    balls = [
        fanout.Ball("pin1", -0.4, 0.4, 0.18, "a"),
        fanout.Ball("pin2", 0.4, 0.4, 0.18, None),
        fanout.Ball("pin3", -0.4, -0.4, 0.18, None),
        fanout.Ball("pin4", 0.4, -0.4, 0.18, None),
    ]
    plan = fanout.plan("U2", balls)
    (esc,) = plan.escapes
    x0, y0, x1, y1 = plan.keepout
    text = STANDIN_PCB.format(
        x0=esc.end[0], y0=-esc.end[1], x1=5, y1=5,  # pad 2: a stand-in of no escape (unconnected here)
        kx0=100 + x0, ky0=50 - y1, kx1=100 + x1, ky1=50 - y0,
    )  # fmt: skip
    (tmp_path / "kicad").mkdir()
    (tmp_path / ".work" / "raw").mkdir(parents=True)
    (tmp_path / "kicad" / "board.kicad_pcb").write_text(text)
    (tmp_path / "kicad" / "board.kicad_sch").write_text("(kicad_sch)")
    fanout.save([plan], tmp_path / ".work" / "raw" / "board.escapes.json")
    project = fixes.KicadProject.load(tmp_path / "kicad", "board")

    changes = fixes.apply_escapes(project)

    assert changes == [
        "U2: 4 balls back in place of the stand-in, 1 escape tracks/vias added, 1 keepout(s) and 1 pour hole(s) removed"
    ]
    fp = next(sexpr.children(project.pcb, "footprint"))
    pads = {str(p[1]): p for p in fp if isinstance(p, list) and sexpr.head(p) == "pad"}
    assert sorted(pads) == ["1", "2", "3", "4"]
    assert str(pads["1"][3]) == "circle"
    assert [str(v) for v in _child(pads["1"], "at")[1:3]] == ["-0.4", "-0.4"]  # KiCad y down
    assert _child(pads["1"], "net") == [sexpr.Sym("net"), sexpr.Sym("1"), "SD_DQ0"]
    (seg,) = sexpr.children(project.pcb, "segment")
    assert [str(v) for v in _child(seg, "start")[1:]] == ["99.6", "49.6"]
    assert _child(seg, "net")[1] == sexpr.Sym("1")
    (pour,) = sexpr.children(project.pcb, "zone")  # the keepout went, the pour lost its hole under U2 only
    assert len(list(sexpr.children(pour, "polygon"))) == 2
    assert fixes.apply_escapes(project) == []  # idempotent


def test_plane_balls_get_their_own_via() -> None:
    balls = _grid([0, 1, 2, 6, 7, 8], 9)
    gnd = {b.pin for b in balls[::7]}
    balls = [
        fanout.Ball(b.pin, b.x, b.y, b.radius, "GND" if b.pin in gnd else b.net, plane=b.pin in gnd) for b in balls
    ]
    plan = fanout.plan("U2", balls)
    assert fanout.violations(plan) == []
    for e in plan.escapes:
        if e.pin in gnd:  # a via near the ball (the plane), besides the outward stub tscircuit routes to
            ball = next(b for b in balls if b.pin == e.pin)
            assert any(math.dist(v, (ball.x, ball.y)) < 1.2 for v in e.vias)
