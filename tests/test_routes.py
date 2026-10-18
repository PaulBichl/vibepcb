from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from synth import seg, write

from vibepcb import fixes, router, routes, sexpr

if TYPE_CHECKING:
    from pathlib import Path


def count(project: object) -> int:
    return len(list(sexpr.children(project.pcb, "segment")))  # type: ignore[attr-defined]


def test_validate_names_the_entry() -> None:
    with pytest.raises(routes.RoutesError, match=r"\[\[route\]\] #2.*unknown key"):
        routes.validate({"route": [{"net": "A"}, {"net": "A", "colour": 1}]})
    with pytest.raises(routes.RoutesError, match="either net"):
        routes.validate({"route": [{"layers": ["F.Cu"]}]})
    with pytest.raises(routes.RoutesError, match="from and to"):
        routes.validate({"route": [{"from": "P1.1"}]})
    with pytest.raises(routes.RoutesError, match="not implemented"):
        routes.validate({"bus": []})
    routes.validate({"defaults": {"layers": ["F.Cu"]}, "route": [{"from": "P1.1", "to": "P3.1", "via_max": 0}]})


def test_pad_to_pad_route_is_written(tmp_path: Path) -> None:
    project = write(tmp_path, seg("12 7", "12 13", 2))
    before = count(project)
    report = routes.apply(project, {"route": [{"from": "P1.1", "to": "P3.1", "layers": ["F.Cu"]}]})
    assert [r.ok for r in report.results] == [True]
    assert count(project) > before
    new = [s for s in sexpr.children(project.pcb, "segment") if sexpr.child(s, "net") is not None][-1]
    assert sexpr.child(new, "net") is not None


def test_net_entry_chains_the_pads(tmp_path: Path) -> None:
    project = write(tmp_path)
    report = routes.apply(project, {"route": [{"net": "A"}]})
    assert [r.entry for r in report.results] == ["P1.1 -> P3.1 (A)"]
    assert report.failed == []


def test_failure_is_reported_and_leaves_no_copper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "MAX_STATES", 5_000)
    project = write(tmp_path, seg("-5 14", "35 14", 2))  # B wall across the whole board, between P1 and P3
    before = count(project)
    report = routes.apply(project, {"route": [{"from": "P1.1", "to": "P3.1", "layers": ["F.Cu"]}]})
    assert [r.ok for r in report.results] == [False]
    assert "no path" in report.results[0].detail or "search limit" in report.results[0].detail
    assert count(project) == before


def test_bad_pad_and_deterministic(tmp_path: Path) -> None:
    project = write(tmp_path)
    with pytest.raises(routes.RoutesError, match="not on the board"):
        routes.apply(project, {"route": [{"from": "P1.1", "to": "X9.1"}]})
    other = write(tmp_path / "again")
    data = {"route": [{"net": "A"}]}
    routes.apply(project, data)
    routes.apply(other, data)
    assert sexpr.dumps(project.pcb) == sexpr.dumps(other.pcb)


def test_apply_twice_adds_nothing(tmp_path: Path) -> None:
    project = write(tmp_path)
    data = {"route": [{"from": "P1.1", "to": "P3.1"}, {"net": "A"}]}
    first = routes.apply(project, data)
    assert [r.ok for r in first.results] == [True]
    n = count(project)
    assert routes.apply(project, data).results == []
    assert count(project) == n


def _pad(ref: str, x: float, y: float) -> routes.Pad:
    return routes.Pad(ref, "1", "N", (x, y), frozenset({"F.Cu"}))


def test_by_angle_orders_a_bus_outermost_first() -> None:
    goals = [_pad("G", 20, 10 + k * 0.5) for k in range(5)]
    starts = [_pad("S", 0, 7 + 3 * k) for k in range(5)]  # y = 7, 10, 13, 16, 19: the middle one is on the axis
    conns = [("N", s, g, f"c{k}") for k, (s, g) in enumerate(zip(starts, goals, strict=True))]
    ordered = [c[3] for c in routes._by_angle(conns)]
    assert ordered[0] in ("c0", "c4")  # an outer arc first
    assert ordered[-1] == "c2"  # the one on the axis last
    assert routes._by_angle(conns[:3]) == conns[:3]  # a few connections keep their order


def test_parts_filter_keeps_connections_touching_the_part(tmp_path: Path) -> None:
    project = write(tmp_path)
    assert routes.apply(project, {"route": [{"net": "A", "parts": ["P9"]}]}).results == []
    other = write(tmp_path / "b")
    assert [r.entry for r in routes.apply(other, {"route": [{"net": "A", "parts": ["P3"]}]}).results] == [
        "P1.1 -> P3.1 (A)"
    ]


def test_remove_antennas_cuts_a_stub_back_to_the_pad_it_runs_over(tmp_path: Path) -> None:
    # A: from (8.5, 10) over P1's centre (10, 10) to (12, 10), then down to P3 (10, 17): the first piece sticks out
    extra = (
        '(segment (start 8.5 10) (end 12 10) (width 0.2) (layer "F.Cu") (net 1) (uuid "a1"))'
        '(segment (start 12 10) (end 12 17) (width 0.2) (layer "F.Cu") (net 1) (uuid "a2"))'
        '(segment (start 12 17) (end 10 17) (width 0.2) (layer "F.Cu") (net 1) (uuid "a3"))'
    )
    project = write(tmp_path, extra)
    changes = fixes.remove_antennas(project)
    assert changes == ["antennas: cut 1 track end(s) running past their pad back to the pad centre"]
    starts = [sexpr.child(s, "start") for s in sexpr.children(project.pcb, "segment")]
    assert ["start", sexpr.Sym("10"), sexpr.Sym("10")] in starts
    assert fixes.remove_antennas(project) == []
