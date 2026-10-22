"""Regression table between ``check`` runs: key metrics are stored per run, the new run is compared with the last.

An agent iterating on a board can oscillate between two variants, each fixing what the other broke. The
table after every ``check`` shows what got worse (DRC errors per type, ERC errors, unrouted connections,
sharp bends, vias, track length, warnings) next to the previous run. ``reports/history.json`` keeps the
last :data:`KEEP` runs.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vibepcb import ratsnest, sexpr

if TYPE_CHECKING:
    from pathlib import Path

    from vibepcb.fixes import KicadProject

KEEP = 30
LENGTH_TOLERANCE = 0.01  # a length counts as worse only when it grew by more than 1 %

# metric -> label; every metric is "lower is better"
FIXED_METRICS = {
    "erc_errors": "ERC errors",
    "drc_errors": "DRC errors",
    "unrouted": "unrouted connections",
    "ratsnest_mm": "ratsnest length (mm)",
    "sharp_corners": "sharp bends (<135 deg)",
    "vias": "vias",
    "track_mm": "track length (mm)",
    "warnings": "warnings",
}
LENGTH_METRICS = frozenset({"ratsnest_mm", "track_mm"})
DRC_PREFIX = "drc:"


def _point(node: list[Any] | None) -> tuple[float, float]:
    return (float(str(node[1])), float(str(node[2]))) if node else (0.0, 0.0)


def board_metrics(project: KicadProject) -> dict[str, float]:
    """Vias, track length (mm) and ratsnest length / link count of the board as it is now."""
    pcb = project.pcb
    track = 0.0
    for kind in ("segment", "arc"):  # an arc counts by its chord (close enough for a trend)
        for item in sexpr.children(pcb, kind):
            track += math.dist(_point(sexpr.child(item, "start")), _point(sexpr.child(item, "end")))
    links = ratsnest.unrouted(project)
    return {
        "vias": float(sum(1 for _ in sexpr.children(pcb, "via"))),
        "track_mm": round(track, 2),
        "ratsnest_mm": round(sum(k.length for k in links), 2),
        "ratsnest_links": float(len(links)),
    }


def snapshot(
    *,
    erc: list[dict[str, Any]],
    drc: list[dict[str, Any]],
    unrouted: int,
    sharp_corners: int,
    warnings: int,
    board: dict[str, float],
    passed: bool,
) -> dict[str, Any]:
    """One run's metrics: ERC / DRC violations as KiCad reports them (``severity``, ``type``), the rest as counts."""
    drc_errors = Counter(str(v.get("type")) for v in drc if v.get("severity") == "error")
    warned = sum(v.get("severity") == "warning" for v in [*erc, *drc])
    metrics: dict[str, float] = {
        "erc_errors": float(sum(v.get("severity") == "error" for v in erc)),
        "drc_errors": float(sum(drc_errors.values())),
        "unrouted": float(unrouted),
        "ratsnest_mm": board["ratsnest_mm"],
        "sharp_corners": float(sharp_corners),
        "vias": board["vias"],
        "track_mm": board["track_mm"],
        "warnings": float(warnings + warned),
    }
    metrics |= {f"{DRC_PREFIX}{t}": float(n) for t, n in drc_errors.items()}
    return {"time": int(time.time()), "passed": passed, "metrics": metrics}


def load(path: Path) -> list[dict[str, Any]]:
    """The stored runs (oldest first); empty if there is no readable history."""
    try:
        runs = json.loads(path.read_text(encoding="utf-8")).get("runs", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [r for r in runs if isinstance(r, dict) and isinstance(r.get("metrics"), dict)]


def save(path: Path, runs: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runs": runs[-KEEP:]}, indent=1), encoding="utf-8")


@dataclass(frozen=True)
class Row:
    metric: str
    before: float | None
    after: float
    verdict: str  # "worse" | "better" | "same" | "new"

    @property
    def label(self) -> str:
        if self.metric.startswith(DRC_PREFIX):
            return f"  DRC {self.metric.removeprefix(DRC_PREFIX)}"
        return FIXED_METRICS.get(self.metric, self.metric)


def _verdict(metric: str, before: float, after: float) -> str:
    if metric in LENGTH_METRICS:
        slack = max(abs(before) * LENGTH_TOLERANCE, 0.01)
        if after > before + slack:
            return "worse"
        return "better" if after < before - slack else "same"
    if after > before:
        return "worse"
    return "better" if after < before else "same"


def compare(previous: dict[str, float] | None, current: dict[str, float]) -> list[Row]:
    """Rows for the fixed metrics and every DRC error type seen in either run; ``previous`` None: first run."""
    names = list(FIXED_METRICS)
    seen = {m for m in [*(previous or {}), *current] if m.startswith(DRC_PREFIX)}
    names += sorted(seen)
    rows = []
    for name in names:
        after = current.get(name, 0.0)
        if previous is None:
            rows.append(Row(name, None, after, "new"))
        else:
            before = previous.get(name, 0.0)
            rows.append(Row(name, before, after, _verdict(name, before, after)))
    return rows


def _num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g}" if float(value).is_integer() else f"{value:.1f}"


def table(rows: list[Row]) -> list[str]:
    """Lines of the comparison; a per-type DRC row is shown only when it is non-zero in one of the runs."""
    shown = [r for r in rows if not r.metric.startswith(DRC_PREFIX) or r.after or r.before]
    first = all(r.verdict == "new" for r in rows)
    worse = [r for r in shown if r.verdict == "worse"]
    width = max(len(r.label) for r in shown)
    head = "Regression table: first recorded check run" if first else "Regression table vs previous check run"
    lines = [head, f"  {'':<{width}}  {'before':>8}  {'now':>8}"]
    for r in shown:
        flag = {"worse": "  WORSE", "better": "  better"}.get(r.verdict, "")
        lines.append(f"  {r.label:<{width}}  {_num(r.before):>8}  {_num(r.after):>8}{flag}")
    if not first:
        lines.append(
            "  WORSE: " + ", ".join(r.label.strip() for r in worse)
            if worse
            else "  nothing got worse since the last run"
        )
    return lines


def record(path: Path, new: dict[str, Any]) -> list[str]:
    """Compare ``new`` with the last run in ``path``, store it, return the table lines."""
    runs = load(path)
    previous = runs[-1]["metrics"] if runs else None
    lines = table(compare(previous, new["metrics"]))
    save(path, [*runs, new])
    return lines
