from __future__ import annotations

from typing import TYPE_CHECKING, Any

from synth import seg, write

from vibepcb import history

if TYPE_CHECKING:
    from pathlib import Path


def run(
    *, drc: list[dict[str, str]] | None = None, vias: float = 2, track: float = 50, unrouted: int = 0
) -> dict[str, Any]:
    return history.snapshot(
        erc=[],
        drc=drc or [],
        unrouted=unrouted,
        sharp_corners=0,
        warnings=1,
        board={"vias": vias, "track_mm": track, "ratsnest_mm": 0.0, "ratsnest_links": 0.0},
        passed=not drc,
    )


def err(kind: str) -> dict[str, str]:
    return {"severity": "error", "type": kind}


def test_snapshot_counts_errors_by_type_and_warnings() -> None:
    drc = [err("clearance"), err("clearance"), err("shorting_items"), {"severity": "warning", "type": "x"}]
    m = run(drc=drc)["metrics"]
    assert m["drc_errors"] == 3
    assert m["drc:clearance"] == 2
    assert m["drc:shorting_items"] == 1
    assert m["warnings"] == 2  # one passed in, one DRC warning


def test_first_run_has_no_verdicts(tmp_path: Path) -> None:
    lines = history.record(tmp_path / "reports" / "history.json", run())
    assert "first recorded" in lines[0]
    assert not any("WORSE" in line for line in lines)


def test_worse_and_better_are_flagged(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    history.record(path, run(drc=[err("clearance")], vias=2, unrouted=1))
    lines = history.record(
        path, run(drc=[err("clearance"), err("clearance"), err("shorting_items")], vias=2, unrouted=0)
    )
    text = "\n".join(lines)
    assert "WORSE: DRC errors, DRC clearance, DRC shorting_items" in text
    assert any("unrouted connections" in line and "better" in line for line in lines)


def test_nothing_worse_and_a_fixed_type_still_listed(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    history.record(path, run(drc=[err("clearance")]))
    lines = history.record(path, run())
    assert "nothing got worse" in lines[-1]
    assert any("DRC clearance" in line and "better" in line for line in lines)


def test_length_changes_below_one_percent_are_same() -> None:
    rows = history.compare({"track_mm": 100.0}, {"track_mm": 100.5})
    assert next(r for r in rows if r.metric == "track_mm").verdict == "same"
    rows = history.compare({"track_mm": 100.0}, {"track_mm": 103.0})
    assert next(r for r in rows if r.metric == "track_mm").verdict == "worse"


def test_history_is_trimmed_and_survives_garbage(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    path.write_text("{not json", encoding="utf-8")
    assert history.load(path) == []
    for _ in range(history.KEEP + 5):
        history.record(path, run())
    assert len(history.load(path)) == history.KEEP


def test_board_metrics_counts_vias_and_track_length(tmp_path: Path) -> None:
    via = '(via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))'
    m = history.board_metrics(write(tmp_path, seg("0 0", "3 4", 1) + via))
    assert m["vias"] == 1
    assert m["track_mm"] == 5.0
    assert m["ratsnest_links"] == 1  # P1 and P3 of net A are not joined by this track
