from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from synth import seg, write

from vibepcb import board as board_mod
from vibepcb import drc

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def board(tmp_path: Path) -> board_mod.Board:
    return board_mod.Board.from_project(write(tmp_path, seg("12 5", "12 15", 2)))


def test_track_clear_of_other_net(board: board_mod.Board) -> None:
    assert drc.fits_track(board, "A", "F.Cu", (10, 11), (11, 11), 0.2)  # beside own pad
    # B's vertical track at x=12: width 0.2 + clearance 0.2 -> A's centre line stays 0.4 away
    assert drc.fits_track(board, "A", "F.Cu", (10, 14), (11.6, 14), 0.2)
    blocker = drc.blocking_track(board, "A", "F.Cu", (10, 14), (11.75, 14), 0.2)
    assert blocker is not None
    assert blocker.kind == "copper"
    assert blocker.net == "B"
    assert blocker.gap == pytest.approx(0.05, abs=1e-6)
    assert drc.fits_track(board, "A", "B.Cu", (10, 14), (13, 14), 0.2)  # other layer: free


def test_same_net_may_touch(board: board_mod.Board) -> None:
    assert drc.fits_track(board, "B", "F.Cu", (12, 6), (14, 6), 0.2)


def test_edge_clearance(board: board_mod.Board) -> None:
    assert drc.fits_track(board, "A", "F.Cu", (5, 0.8), (6, 0.8), 0.2)
    blocker = drc.blocking_track(board, "A", "F.Cu", (5, 0.5), (6, 0.5), 0.2)
    assert blocker is not None
    assert blocker.kind == "edge"


def test_via_checks(board: board_mod.Board) -> None:
    assert drc.fits_via(board, "A", (15, 15), 0.6, 0.3)
    assert not drc.fits_via(board, "A", (12.3, 15), 0.6, 0.3)  # copper of B's track
    assert drc.fits_via(board, "A", (1.0, 10), 0.6, 0.3)  # 0.7 from the edge line
    assert not drc.fits_via(board, "A", (0.7, 10), 0.6, 0.3)  # 0.4 < 0.5
    board.add_via("A", (15, 15), 0.6, 0.3)
    blocker = drc.blocking_via(board, "B", (15.5, 15), 0.6, 0.3)
    assert blocker is not None
    assert blocker.kind in ("copper", "hole")


def test_added_track_blocks_later_routes(board: board_mod.Board) -> None:
    assert drc.fits_track(board, "A", "F.Cu", (15, 5), (15, 9), 0.2)
    board.add_track("B", "F.Cu", (15, 6), (15, 8), 0.2)
    assert not drc.fits_track(board, "A", "F.Cu", (15.3, 5), (15.3, 9), 0.2)


def test_net_class_clearance(tmp_path: Path) -> None:
    project = write(tmp_path, seg("12 5", "12 15", 2))
    pro = project.pcb_path.with_suffix(".kicad_pro")
    pro.write_text(
        json.dumps(
            {
                "net_settings": {
                    "classes": [
                        {"name": "Default", "clearance": 0.2},
                        {"name": "HV", "clearance": 1.0, "track_width": 0.4},
                    ],
                    "netclass_patterns": [{"netclass": "HV", "pattern": "B*"}],
                }
            }
        ),
        encoding="utf-8",
    )
    b = board_mod.Board.from_project(project)
    assert b.rules.class_of("B").track_width == 0.4
    assert b.rules.clearance("A", "B") == 1.0
    assert not drc.fits_track(b, "A", "F.Cu", (13, 5), (13, 9), 0.2)  # 0.9 gap < 1.0
    assert drc.fits_track(b, "A", "F.Cu", (13.2, 5), (13.2, 9), 0.2)


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_agrees_with_kicad_drc(tmp_path: Path) -> None:
    """Deliberately bad tracks: ours and KiCad's verdicts agree on clearance and edge."""
    cases = {  # name -> (net id, a, b)
        "ok": (1, "10 12", "11 12"),
        "close": (1, "10.5 14", "11.75 14"),
        "edge": (1, "5 0.5", "6 0.5"),
    }
    extra = seg("12 5", "12 15", 2) + "".join(
        seg(a, b, net).replace('(uuid "1")', f'(uuid "{i:08d}-0000-0000-0000-000000000000")')
        for i, (net, a, b) in enumerate(cases.values(), 1)
    )
    project = write(tmp_path, extra)
    out = tmp_path / "drc.json"
    subprocess.run(
        ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "-o", str(out), str(project.pcb_path)],
        check=True,
        capture_output=True,
    )
    kinds = {v["type"] for v in json.loads(out.read_text())["violations"]}
    b = board_mod.Board.from_project(write(tmp_path / "x", seg("12 5", "12 15", 2)))
    assert drc.fits_track(b, "A", "F.Cu", (10, 12), (11, 12), 0.2)
    assert not drc.fits_track(b, "A", "F.Cu", (10.5, 14), (11.75, 14), 0.2)
    assert not drc.fits_track(b, "A", "F.Cu", (5, 0.5), (6, 0.5), 0.2)
    assert {"clearance", "copper_edge_clearance"} <= kinds
