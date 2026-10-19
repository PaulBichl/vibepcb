from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

import pytest
from synth import pad, write

from vibepcb import board as board_mod
from vibepcb import drc, fixes, pairs, routes, tracks

if TYPE_CHECKING:
    from pathlib import Path

PAIR_NETS = '(net 0 "")(net 1 "D_P")(net 2 "D_N")(net 3 "X")'


def test_offset_keeps_the_distance_round_a_45_degree_corner() -> None:
    line = [(0.0, 0.0), (5.0, 0.0), (8.0, 3.0)]
    left = pairs.offset(line, 0.5)
    for (a, b), (c, d) in zip(itertools.pairwise(line), itertools.pairwise(left), strict=True):
        # each offset piece runs parallel to its centre piece, 0.5 mm away
        assert tracks.point_seg_distance(((c[0] + d[0]) / 2, (c[1] + d[1]) / 2), a, b) == pytest.approx(0.5, abs=1e-6)


def test_pair_routes_coupled_and_reports_uncoupled_and_skew(tmp_path: Path) -> None:
    parts = " ".join([
        pad("A1", 8, 8.5, 1, "D_P"), pad("A2", 8, 11.5, 2, "D_N"),
        pad("B1", 22, 8.5, 1, "D_P"), pad("B2", 22, 11.5, 2, "D_N"),
        pad("Q1", 15, 6, 3, "X"),
    ])  # fmt: skip
    project = write(tmp_path, nets=PAIR_NETS, parts=parts)
    report = routes.apply(project, {"pair": [{"nets": ["D_P", "D_N"]}]})
    assert [r.ok for r in report.results] == [True], report.results[0].detail
    assert "coupled" in report.results[0].detail
    copper = fixes._copper(project.pcb)
    model = board_mod.Board.from_project(project)
    for net in ("D_P", "D_N"):
        for s in copper.segs_by_net[net]:
            assert drc.blocking_track(model, net, s.layer, s.a, s.b, s.width) is None
    # both nets connect their two pads
    for net in ("D_P", "D_N"):
        groups = tracks.pad_groups(copper.pads_by_net[net], [], copper.segs_by_net[net])
        assert len(groups) == 1


def test_pair_entry_is_validated() -> None:
    with pytest.raises(routes.RoutesError, match="two net names"):
        routes.validate({"pair": [{"nets": ["D_P"]}]})
