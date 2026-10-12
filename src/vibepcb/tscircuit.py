"""Thin wrapper around the tscircuit CLI (``tsci``) installed in node_modules."""

from __future__ import annotations

import json
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
    cmd = [*_tsci(), "export", str(source), "-f", fmt, "-o", str(output.resolve())]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not output.exists():
        msg = f"tsci export -f {fmt} failed:\n{result.stdout}{result.stderr}"
        raise TscircuitError(msg)


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


def circuit_errors(circuit_json: Path) -> tuple[int, str]:
    """Blocking errors in a circuit JSON (``*_error`` elements) and a short report of them."""
    elements = json.loads(circuit_json.read_text(encoding="utf-8"))
    errors = [
        e
        for e in elements
        if str(e.get("type", "")).endswith("_error")
        and not any(p in str(e.get("message", "")) for p in NON_BLOCKING_ERRORS)
    ]
    counts = Counter(str(e["type"]) for e in errors)
    lines = [f"Errors: {len(errors)}", *(f"  {n} x {t}" for t, n in counts.most_common())]
    lines += [f"- {e['type']}: {e.get('message', '')}" for e in errors[:REPORT_LINES]]
    return len(errors), "\n".join(lines)
