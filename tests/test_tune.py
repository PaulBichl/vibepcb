from __future__ import annotations

import itertools
import math
from typing import TYPE_CHECKING

import pytest
from synth import write

from vibepcb import board as board_mod
from vibepcb import drc, fixes, tracks, tune

if TYPE_CHECKING:
    from pathlib import Path


def test_bump_height_inverts_bump_extra() -> None:
    for h in (0.5, 1.0, 1.7):
        assert tune.bump_height(tune.bump_extra(h)) == pytest.approx(h)


def test_meander_adds_the_planned_length_with_45_degree_corners() -> None:
    bumps = tune.Bumps(3, 0.8, 1, 0.4)
    pts = tune.meander((0.0, 0.0), (10.0, 0.0), bumps)
    length = sum(math.dist(p, q) for p, q in itertools.pairwise(pts))
    assert length == pytest.approx(10.0 + bumps.extra())
    assert tracks.sharp_corners(tracks.simplify(pts)) == []  # no bend sharper than 135 degrees
    assert pts[0] == (0.0, 0.0)
    assert pts[-1] == (10.0, 0.0)


def test_tune_brings_a_short_net_into_the_group(tmp_path: Path) -> None:
    # A: P1 (10, 10) -> P3 (10, 17), 7 mm; B: 9 mm away from it; max skew 1 mm -> A gets about +1.1 mm
    extra = (
        '(segment (start 10 10) (end 10 17) (width 0.2) (layer "F.Cu") (net 1) (uuid "a1"))'
        '(segment (start 20 10) (end 20 19) (width 0.2) (layer "F.Cu") (net 2) (uuid "b1"))'
    )
    project = write(tmp_path, extra)
    project.config = {"length_group": {"G": {"nets": ["A", "B"], "max_skew": 1.0}}}
    changes = fixes.tune_lengths(project)
    assert len(changes) == 1
    assert changes[0].startswith("G: A +")
    copper = fixes._copper(project.pcb)
    lengths = tune.net_lengths(copper)
    assert lengths["B"] - lengths["A"] == pytest.approx(0.9, abs=0.02)  # 90 % of max_skew
    model = board_mod.Board.from_project(project)
    for s in copper.segs_by_net["A"]:
        assert drc.blocking_track(model, "A", s.layer, s.a, s.b, s.width) is None
    assert fixes.tune_lengths(project) == []  # inside the rule now
