"""Circuit verification in ``check``: simulations, required sims, netlist checks and the findings file.

Two modes decide what an issue is: in *vibe* mode (default) failed / stale / skipped sims, missing required
sims, ranges that are only assumed and open findings are warnings and the loop goes on; in *strict* mode
(``check --strict``, and ``fab`` needs a strict pass) each of them is an error. :func:`run` returns the
summary lines and the number of issues; ``cmd_check`` appends the lines and fails on ``issues`` in strict mode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb import findings, intent, netcheck, paths, sim

if TYPE_CHECKING:
    from vibepcb.sim import DeckResult, Defaults


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    issues: int = 0


def _norm(kind: str) -> str:
    return kind.lower().replace("-", "").replace("_", "")


def missing_sims(regulators: list[str], results: list[DeckResult], defaults: Defaults) -> list[str]:
    """Required decks (``[required.regulator]``) no passing deck covers, one line per part and kind."""
    done: set[tuple[str, str | None]] = set()
    for result in results:
        if result.status != "ok":
            continue
        for kind, refs in result.deck.covers:
            done |= {(_norm(kind), ref) for ref in refs} or {(_norm(kind), None)}
    out = []
    for ref in regulators:
        for kind in defaults.required.get("regulator", ()):
            if (_norm(kind), ref) not in done and (_norm(kind), None) not in done:
                out.append(
                    f"regulator {ref} needs a passing '{kind}' deck (boards/<board>/sim/*.cir with `* covers: {kind} {ref}`)"
                )
    return out


def _sim_lines(results: list[DeckResult], missing: list[str], binary: str | None, level: str) -> tuple[list[str], int]:
    lines, issues = [], 0
    if results and binary is None:
        lines.append(
            f"  [{level}] sim_no_ngspice: ngspice not found, {len(results)} deck(s) not run (install it, e.g. `sudo pacman -S ngspice`)"
        )
        issues += 1
    for r in results:
        for problem in r.problems:
            if r.status == "skipped":
                continue
            lines.append(
                f"  [{level}] sim_{r.status}: {r.name}: {problem}"
                + (" - the sim no longer describes the board" if r.status == "stale" else "")
            )
            issues += 1
        for m in r.measures:
            if m.status in {"fail", "error"}:
                lines.append(f"  [{level}] sim_fail: {r.name}: {m.line()}")
                issues += 1
            elif m.status == "assumed":
                lines.append(
                    f"  [{level}] sim_assumed: {r.name}: {m.line()}; assumed, give it a `* range:` with source="
                )
                issues += 1
            elif m.range and m.range.kind == "assumed":
                lines.append(f"  [{level}] sim_assumed: {r.name}: {m.line()}")
                issues += 1
    for text in missing:
        lines.append(f"  [{level}] sim_missing: {text}")
        issues += 1
    return lines, issues


def run(board: str, *, strict: bool = False) -> Report:
    """Verify board ``board`` from its circuit JSON; writes ``findings.toml`` when there is something to say."""
    level = "error" if strict else "warning"
    report = Report()
    circuit_json = paths.raw_dir(board) / f"{board}.circuit.json"
    if not circuit_json.exists():
        report.lines.append(f"Verification: skipped, {circuit_json.name} not found (run `pcb build {board}`)")
        return report
    design = netcheck.load_design(json.loads(circuit_json.read_text(encoding="utf-8")))
    spec, defaults = intent.load(board), sim.load_defaults()

    decks = [sim.parse_deck(p) for p in sim.deck_paths(board)]
    regulators = netcheck.find_regulators(design)
    binary = sim.ngspice() if decks else None
    results = [sim.check_deck(d, design.values(), design.kinds(), spec, defaults, binary) for d in decks]
    missing = missing_sims(regulators, results, defaults)
    sim_lines, sim_issues = _sim_lines(results, missing, binary, level)
    ok = sum(r.status == "ok" for r in results)
    report.lines.append(f"Simulation: {len(results)} deck(s), {ok} passed; {len(regulators)} regulator(s) found")
    report.lines += sim_lines
    report.issues += sim_issues

    merged = findings.merge(findings.load(board), netcheck.run(design, spec, defaults))
    findings.save(board, merged)
    open_ = [f for f in merged if f.blocking]
    report.lines.append(f"Findings: {len(open_)} open, {len(merged) - len(open_)} accepted ({findings.FINDINGS_NAME})")
    report.lines += [
        f"  [{level}] finding: {f.line()}" + ("" if f.status == "open" else " (accepted without a reason)")
        for f in open_
    ]
    report.issues += len(open_)
    report.lines.append(f"Mode: {'strict' if strict else 'vibe'}; {report.issues} verification issue(s)")
    return report
