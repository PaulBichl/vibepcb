from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from synth import seg, write

from vibepcb import router, routes, sexpr

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
    assert report.results[0].detail.startswith(("no path", "search limit"))
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
