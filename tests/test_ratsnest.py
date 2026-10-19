from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from synth import pad, seg, write

from vibepcb import ratsnest

if TYPE_CHECKING:
    from pathlib import Path


def test_crossing_links_of_two_nets(tmp_path: Path) -> None:
    parts = pad("P1", 5, 5, 1, "A") + pad("P2", 15, 15, 1, "A") + pad("P3", 6, 17, 2, "B") + pad("P4", 14, 7, 2, "B")
    links = ratsnest.unrouted(write(tmp_path, parts=parts))
    assert sorted((k.net, k.a, k.b) for k in links) == [("A", "P1.1", "P2.1"), ("B", "P3.1", "P4.1")]
    assert ratsnest.crossings(links) == 1
    assert ratsnest.cell_crossings(links) == {(1, 1): 1}  # the crossing at about (10.9, 10.9)


def test_routed_net_has_no_link(tmp_path: Path) -> None:
    project = write(tmp_path, seg("10 10", "10 17", 1))  # P1 - P3 (net A) routed, B has one pad
    assert ratsnest.unrouted(project) == []


def test_spanning_tree_takes_nearest_pads(tmp_path: Path) -> None:
    parts = pad("P1", 2, 5, 1, "A") + pad("P2", 12, 5, 1, "A") + pad("P3", 28, 5, 1, "A")
    links = ratsnest.unrouted(write(tmp_path, parts=parts))
    assert {(k.a, k.b) for k in links} == {("P1.1", "P2.1"), ("P2.1", "P3.1")}
    assert sum(k.length for k in links) == pytest.approx(26)


def test_group_joins_from_its_nearest_pad(tmp_path: Path) -> None:
    parts = pad("P1", 5, 5, 1, "A") + pad("P2", 6, 15, 1, "A") + pad("P3", 20, 14, 1, "A")
    links = ratsnest.unrouted(write(tmp_path, seg("5 5", "6 15", 1), parts=parts))
    assert [(k.a, k.b) for k in links] == [("P2.1", "P3.1")]


def test_planes_skipped(tmp_path: Path) -> None:
    parts = pad("P1", 5, 5, 1, "A") + pad("P2", 15, 15, 1, "A") + pad("P3", 6, 15, 2, "B") + pad("P4", 14, 5, 2, "B")
    project = write(tmp_path, parts=parts)
    project.config = {"planes": {"nets": ["A"]}}
    links = ratsnest.unrouted(project)
    assert [k.net for k in links] == ["B"]
    score = ratsnest.score(project)
    assert score["links"] == 1
    assert score["crossings"] == 0
    assert score["length"] == pytest.approx(links[0].length)
