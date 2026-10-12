"""Thin wrapper around the tscircuit CLI (``tsci``) installed in node_modules."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from typing import TYPE_CHECKING

from vibepcb.paths import ROOT

if TYPE_CHECKING:
    from pathlib import Path


class TscircuitError(RuntimeError):
    pass


def _tsci() -> list[str]:
    tsci = ROOT / "node_modules" / ".bin" / "tsci"
    if not tsci.exists() or shutil.which("node") is None:
        msg = "tscircuit is not installed - run `hatch run setup` first"
        raise TscircuitError(msg)
    # tsci's shebang is `env bun`; bun comes from node_modules/.bin as well
    return ["npx", "--no-install", "tsci"]


def export(source: Path, fmt: str, output: Path) -> None:
    """``tsci export <source> -f <fmt> -o <output>``; raises with tsci's output on failure."""
    output.parent.mkdir(parents=True, exist_ok=True)
    # the parts engine looks up every part on the network on each run; `fab` checks availability once
    cmd = [*_tsci(), "export", str(source), "-f", fmt, "-o", str(output.resolve()), "--disable-parts-engine"]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not output.exists():
        msg = f"tsci export -f {fmt} failed:\n{result.stdout}{result.stderr}"
        raise TscircuitError(msg)


FAST_EFFORT = "2x"
_RELATIVE_IMPORT = re.compile(r"""(\bfrom\s*|\bimport\s*\(?\s*)(["'])(\.{1,2}/[^"']*|\.{1,2})\2""")
_EFFORT_PROP = re.compile(r"autorouterEffortLevel\s*=\s*(?:\"[^\"]*\"|'[^']*'|\{[^}]*\})")
_BOARD_TAG = re.compile(r"<board(?=[\s>/])")


def fast_source(source: Path, target_dir: Path) -> Path:
    """A copy of ``source`` in ``target_dir`` with the board's ``autorouterEffortLevel`` forced to ``"2x"``.

    Relative imports are made absolute so the copy evaluates from its new place. For quick
    placement experiments only: the router result differs from the final (full effort) one.
    """
    text = source.read_text(encoding="utf-8")
    here = source.resolve().parent
    text = _RELATIVE_IMPORT.sub(lambda m: f"{m[1]}{m[2]}{(here / m[3]).resolve()}{m[2]}", text)
    prop = f'autorouterEffortLevel="{FAST_EFFORT}"'
    if _EFFORT_PROP.search(text):
        text = _EFFORT_PROP.sub(prop, text, count=1)
    elif _BOARD_TAG.search(text):
        text = _BOARD_TAG.sub(f"<board {prop}", text, count=1)
    else:
        msg = f"no <board> tag found in {source}"
        raise TscircuitError(msg)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    target.write_text(text, encoding="utf-8")
    return target


def evaluate_unrouted(source: Path, *, parts_engine: bool = False) -> Path:
    """``tsci build --routing-disabled``: evaluate placement only (no autorouter); returns the circuit JSON.

    With ``parts_engine`` the supplier lookup runs too, which adds ``*_availability_warning`` elements.
    """
    cmd = [*_tsci(), "build", str(source), "--routing-disabled", "--ignore-errors"]
    if not parts_engine:
        cmd.append("--disable-parts-engine")
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    relative = source.resolve().relative_to(ROOT)
    circuit = (
        ROOT / "dist" / relative.parent / relative.name.removesuffix(".tsx").removesuffix(".circuit") / "circuit.json"
    )
    if result.returncode != 0 or not circuit.exists():
        msg = f"tsci build --routing-disabled failed:\n{result.stdout}{result.stderr}"
        raise TscircuitError(msg)
    return circuit


def availability_warnings(source: Path) -> list[str]:
    """Supplier availability warnings (one line per part) from a parts-engine evaluation of ``source``."""
    elements = json.loads(evaluate_unrouted(source, parts_engine=True).read_text(encoding="utf-8"))
    return [str(e.get("message", "")) for e in elements if str(e.get("type", "")).endswith("availability_warning")]


def build(source: Path, out_dir: Path, stem: str, formats: dict[str, str]) -> list[Path]:
    """Export ``source`` to every ``{tsci format: file suffix}`` in ``formats``; returns the written files."""
    written = []
    for fmt, suffix in formats.items():
        output = out_dir / f"{stem}{suffix}"
        export(source, fmt, output)
        written.append(output)
    return written


# tscircuit's DRC giving up (e.g. on a zero-width route point) is no finding of its own:
# KiCad's DRC in `check` covers the board, and fixes.repair_zero_width_tracks the cause
NON_BLOCKING_ERRORS = ("DRC could not complete",)
REPORT_LINES = 40


# what a placement-only evaluation (before routing) can already tell
PLACEMENT_ERRORS = (
    "pcb_placement_error",
    "pcb_component_outside_board_error",
    "pcb_courtyard_overlap_error",
    "pcb_footprint_overlap_error",
    "pcb_pad_pad_clearance_error",
)


def circuit_errors(circuit_json: Path, only: tuple[str, ...] | None = None) -> tuple[int, str]:
    """Blocking errors in a circuit JSON (``*_error`` elements; just the types in ``only``) and a short report."""
    elements = json.loads(circuit_json.read_text(encoding="utf-8"))
    errors = [
        e
        for e in elements
        if str(e.get("type", "")).endswith("_error")
        and (only is None or e["type"] in only)
        and not any(p in str(e.get("message", "")) for p in NON_BLOCKING_ERRORS)
    ]
    counts = Counter(str(e["type"]) for e in errors)
    lines = [f"Errors: {len(errors)}", *(f"  {n} x {t}" for t, n in counts.most_common())]
    lines += [f"- {e['type']}: {e.get('message', '')}" for e in errors[:REPORT_LINES]]
    return len(errors), "\n".join(lines)
