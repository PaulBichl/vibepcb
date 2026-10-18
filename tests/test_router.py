from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest
from synth import seg, write

from vibepcb import board as board_mod
from vibepcb import drc, router

if TYPE_CHECKING:
    from pathlib import Path

VIA = (0.6, 0.3)


def spec(layers: list[str] | None = None, via: tuple[float, float] | None = VIA, **kw: object) -> router.Spec:
    return router.Spec(layers or ["F.Cu"], 0.2, via if (layers and len(layers) > 1) else None, **kw)  # type: ignore[arg-type]


def test_straight_connection(tmp_path: Path) -> None:
    board = board_mod.Board.from_project(write(tmp_path))
    r = router.route(board, "A", (10, 10), (14, 10), spec())
    assert r.pieces == [("F.Cu", (10, 10), (14, 10))]
    assert r.vias == []


def test_goes_around_an_obstacle(tmp_path: Path) -> None:
    board = board_mod.Board.from_project(write(tmp_path, seg("12 7", "12 13", 2)))  # B wall between
    r = router.route(board, "A", (10, 10), (14, 10), spec())
    assert len(r.pieces) > 1
    assert r.pieces[0][1] == (10, 10)
    assert r.pieces[-1][2] == (14, 10)
    for layer, a, b in r.pieces:
        assert drc.fits_track(board, "A", layer, a, b, 0.2)
    pts = [r.pieces[0][1], *(p[2] for p in r.pieces)]
    for p, q, s in zip(pts, pts[1:], pts[2:], strict=False):
        assert _interior(p, q, s) >= 135 - 1e-6


def _interior(p: tuple[float, float], q: tuple[float, float], s: tuple[float, float]) -> float:
    a = math.atan2(p[1] - q[1], p[0] - q[0])
    b = math.atan2(s[1] - q[1], s[0] - q[0])
    d = abs(math.degrees(a - b)) % 360
    return 360 - d if d > 180 else d


def test_needs_a_via_when_the_layer_is_walled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wall = seg("12 -5", "12 25", 2)  # on F.Cu, outside the outline too: no way around
    board = board_mod.Board.from_project(write(tmp_path, wall))
    with monkeypatch.context() as m:
        m.setattr(router, "MAX_STATES", 20_000)  # the impossible F.Cu-only search floods the corridor
        with pytest.raises(router.RouteFailure):
            router.route(board, "A", (10, 10), (14, 10), spec(["F.Cu"]))
    r = router.route(
        board, "A", (10, 10), (14, 10), spec(["F.Cu", "B.Cu"]), start_layers=["F.Cu"], goal_layers=["F.Cu"]
    )
    assert len(r.vias) == 2
    assert {layer for layer, _, _ in r.pieces} == {"F.Cu", "B.Cu"}
    r.commit(board)
    assert not drc.fits_track(board, "B", "B.Cu", (11.5, 9), (11.5, 11), 0.2)  # the B.Cu piece is now an obstacle


def test_corridor_makes_it_fail_with_a_reason(tmp_path: Path) -> None:
    board = board_mod.Board.from_project(write(tmp_path, seg("12 7", "12 13", 2)))
    with pytest.raises(router.RouteFailure) as e:
        router.route(board, "A", (10, 10), (14, 10), spec(corridor=(9, 9, 15, 11)))
    assert "no path" in e.value.reason


def test_waypoint_is_passed(tmp_path: Path) -> None:
    board = board_mod.Board.from_project(write(tmp_path))
    r = router.route(board, "A", (10, 10), (14, 10), spec(), waypoints=[(12, 12)])
    assert any(b == (12, 12) for _, _, b in r.pieces)


def test_committed_route_blocks_the_next(tmp_path: Path) -> None:
    board = board_mod.Board.from_project(write(tmp_path))
    router.route(board, "A", (10, 10), (14, 10), spec()).commit(board)
    r = router.route(board, "B", (20, 10), (20, 14), spec())
    r2 = router.route(board, "B", (10, 8), (14, 8), spec())
    assert r.length == pytest.approx(4)
    assert r2.length >= 4  # fine: parallel above the A route at >= 0.4 mm


def test_deterministic(tmp_path: Path) -> None:
    board = board_mod.Board.from_project(write(tmp_path, seg("12 7", "12 13", 2)))
    a = router.route(board, "A", (10, 10), (14, 10), spec())
    b = router.route(board, "A", (10, 10), (14, 10), spec())
    assert a.pieces == b.pieces


def test_start_and_goal_directions_continue_an_escape(tmp_path: Path) -> None:
    board = board_mod.Board.from_project(write(tmp_path))
    # a track already leaves the start going +y and arrives at the goal going -y (it continues to a ball below)
    r = router.route(board, "A", (10, 10), (14, 10), spec(), start_dir=(0.0, 1.0), goal_dir=(0.0, 1.0))
    (_, a0, b0), (_, a1, b1) = r.pieces[0], r.pieces[-1]
    first = ((b0[0] - a0[0]) / math.dist(a0, b0), (b0[1] - a0[1]) / math.dist(a0, b0))
    last = ((b1[0] - a1[0]) / math.dist(a1, b1), (b1[1] - a1[1]) / math.dist(a1, b1))
    assert first[1] >= math.cos(math.radians(45)) - 1e-6  # within 45 deg of +y
    assert last[1] <= -math.cos(math.radians(45)) + 1e-6  # runs into the goal against +y: within 45 deg of -y
