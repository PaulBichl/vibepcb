"""Review findings: ``boards/<board>/findings.toml``, one entry per finding, committed with the board.

::

    [[finding]]
    id = "i2c_pullup:I2C_SDA"      # unique; deterministic findings get "<rule>:<net or pin>"
    ref = "U2"                     # the part the finding cites
    pin = "SDA"                    # and its pin (may be empty for a part-level finding)
    text = "I2C_SDA has no pull-up resistor to a supply net"
    status = "open"                # open | accepted
    reason = ""                    # accepted needs a reason ("pull-ups are on the sensor board")
    source = "netcheck"            # netcheck (written by `check`) | review (`pcb review --merge`) | anything else (by hand)

``check`` merges the deterministic findings (:mod:`vibepcb.netcheck`) into the file: a finding it still finds
keeps its status and reason (unless its text changed: an acceptance answers what the finding said, so it is reopened
with the old reason kept after :data:`REOPENED`), a new one is added as ``open``, one it no longer finds is dropped. ``pcb review --merge``
does the same for ``source = "review"`` (the reviewer agent's issue / unknown answers, docs/REVIEW.md). Entries
with any other ``source`` (written by hand) are never touched. An
``accepted`` finding without a reason still counts as open. Open findings are warnings in vibe mode and fail
``check --strict``.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from vibepcb import paths
from vibepcb.config import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

FINDINGS_NAME = "findings.toml"
AUTO = "netcheck"
STATUSES = ("open", "accepted")
REOPENED = "previously accepted, before the finding text changed: "
KEYS = ("id", "ref", "pin", "text", "status", "reason", "source")


class FindingsError(ConfigError):
    """findings.toml is malformed."""


@dataclass(frozen=True)
class Finding:
    id: str
    ref: str
    pin: str
    text: str
    status: str = "open"
    reason: str = ""
    source: str = AUTO

    @property
    def blocking(self) -> bool:
        """Open, or accepted without saying why."""
        return self.status == "open" or not self.reason.strip()

    def line(self) -> str:
        where = f"{self.ref}.{self.pin}" if self.pin else self.ref
        return f"{self.id} [{where}]: {self.text}" if where else f"{self.id}: {self.text}"


def findings_path(board: str) -> Path:
    return paths.board_source(board).parent / FINDINGS_NAME


def parse(data: dict[str, object], where: str = FINDINGS_NAME) -> list[Finding]:
    out: list[Finding] = []
    entries = data.get("finding", [])
    if not isinstance(entries, list):
        msg = f"{where}: use [[finding]] tables"
        raise FindingsError(msg)
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            msg = f"{where}: finding #{i} must be a table"
            raise FindingsError(msg)
        if unknown := set(entry) - set(KEYS):
            msg = f"{where}: finding #{i}: unknown key(s) {sorted(unknown)}"
            raise FindingsError(msg)
        if not entry.get("id") or not entry.get("text"):
            msg = f"{where}: finding #{i} needs an id and a text"
            raise FindingsError(msg)
        finding = Finding(**{"ref": "", "pin": "", **{k: str(v) for k, v in entry.items()}})
        if finding.status not in STATUSES:
            msg = f"{where}: {finding.id}: status must be one of {', '.join(STATUSES)}, got {finding.status!r}"
            raise FindingsError(msg)
        out.append(finding)
    if len({f.id for f in out}) != len(out):
        msg = f"{where}: duplicate finding ids"
        raise FindingsError(msg)
    return out


def load(board: str) -> list[Finding]:
    path = findings_path(board)
    if not path.exists():
        return []
    try:
        return parse(tomllib.loads(path.read_text(encoding="utf-8")), path.name)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path.name}: {exc}"
        raise FindingsError(msg) from exc


def merge(existing: list[Finding], found: list[Finding], source: str = AUTO) -> list[Finding]:
    """Entries of other sources as they are; those of ``source`` (``netcheck``, or ``review`` from
    :mod:`vibepcb.review`) as ``found`` now, with the status and reason the file already holds for the same id.
    An accepted finding whose text changed is reopened (a new review may raise a different objection under the
    same id); its old reason stays, after :data:`REOPENED`, for the designer to update."""
    kept = {f.id: f for f in existing if f.source == source}
    merged = [f for f in existing if f.source != source]
    for finding in sorted(found, key=lambda f: f.id):
        old = kept.get(finding.id)
        if old is None:
            merged.append(finding)
        elif old.text != finding.text and old.status == "accepted":
            reason = old.reason.removeprefix(REOPENED)
            merged.append(replace(finding, status="open", reason=f"{REOPENED}{reason}" if reason else ""))
        else:
            merged.append(replace(finding, status=old.status, reason=old.reason))
    return merged


def dumps(findings: list[Finding]) -> str:
    """TOML text (JSON strings are valid TOML basic strings)."""
    blocks = []
    for f in findings:
        blocks.append("[[finding]]\n" + "".join(f"{k} = {json.dumps(getattr(f, k))}\n" for k in KEYS))
    header = (
        '# Review findings (format: src/vibepcb/findings.py). Set status = "accepted" with a reason to close one.\n'
    )
    return header + "\n" + "\n".join(blocks) if blocks else header


def save(board: str, findings: list[Finding]) -> None:
    """Write the file; no file is created for a board without findings."""
    path = findings_path(board)
    if findings or path.exists():
        text = dumps(findings)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
