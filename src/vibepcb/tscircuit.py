"""Thin wrapper around the tscircuit CLI (``tsci``) installed in node_modules."""

from __future__ import annotations

import re
import shutil
import subprocess
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


def check(source: Path) -> tuple[int, str]:
    """``tsci check``: tscircuit's own validation (netlist, pin polarity vs. supplier part, ...).

    Returns the number of errors and the report (autorouter chatter stripped).
    """
    cmd = [*_tsci(), "check", str(source)]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    lines = [ln for ln in (result.stdout + result.stderr).splitlines() if "autorouting" not in ln]
    match = re.search(r"^Errors: (\d+)", "\n".join(lines), re.MULTILINE)
    errors = int(match[1]) if match else int(result.returncode != 0)
    return errors, "\n".join(lines).strip()
