"""Per-board settings beyond what the TSX carries: ``boards/<board>/vibepcb.toml``.

tscircuit has no net classes, impedance-driven widths or KiCad length rules; ``fix`` writes
these from the board's config file into the KiCad project (:func:`fixes.apply_board_config`)::

    [netclass.USB]                 # KiCad net class
    nets = ["USB_D*"]              # net-name patterns (KiCad wildcards: * ?)
    track_width = 0.2              # mm; optional: clearance, via_diameter, via_drill,
    diff_pair_width = 0.2          #     diff_pair_width, diff_pair_gap
    diff_pair_gap = 0.15

    [length_group.SDRAM_DQ]        # nets routed to matching lengths
    nets = ["SD_DQ*"]
    max_skew = 2.0                 # mm, longest - shortest

    [diff_pair]                    # pairs = nets named <base>_P/_N or <base>+/-
    max_uncoupled = 5.0            # mm of the pair routed apart

Net names: a net gets the name of its ``<net>``, else of a named ``<trace name="...">`` on it
(see ``schgen``), so name the traces a rule should catch.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any

from vibepcb import paths

if TYPE_CHECKING:
    from pathlib import Path

CONFIG_NAME = "vibepcb.toml"
NETCLASS_KEYS = ("track_width", "clearance", "via_diameter", "via_drill", "diff_pair_width", "diff_pair_gap")


class ConfigError(ValueError):
    """The board config is malformed."""


def config_path(board: str) -> Path:
    return paths.board_source(board).parent / CONFIG_NAME


def load(board: str) -> dict[str, Any]:
    """The board's config (empty without a file), validated."""
    path = config_path(board)
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    validate(data, path.name)
    return data


def validate(data: dict[str, Any], where: str = CONFIG_NAME) -> None:
    known = {"netclass", "length_group", "diff_pair"}
    if unknown := set(data) - known:
        msg = f"{where}: unknown section(s) {sorted(unknown)} (known: {sorted(known)})"
        raise ConfigError(msg)
    for name, cls in data.get("netclass", {}).items():
        if not cls.get("nets"):
            msg = f"{where}: netclass {name!r} needs nets = [...]"
            raise ConfigError(msg)
        if bad := set(cls) - {"nets", *NETCLASS_KEYS}:
            msg = f"{where}: netclass {name!r}: unknown key(s) {sorted(bad)}"
            raise ConfigError(msg)
    for name, group in data.get("length_group", {}).items():
        if not group.get("nets") or "max_skew" not in group:
            msg = f"{where}: length_group {name!r} needs nets = [...] and max_skew"
            raise ConfigError(msg)
