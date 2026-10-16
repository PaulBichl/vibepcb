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

    [planes]                       # nets with a plane: each SMD pad gets its own via to it,
    nets = ["GND", "V3V3"]         #   the router's tracks of the net go (fixes.plane_pad_vias)

    [keepout.antenna]              # rule area (KiCad enforces it: pour fill + DRC; the router keeps out of tracks / vias)
    ref = "U3"                     #   over this part's pads + courtyard (or: area = [x0, y0, x1, y1] board mm)
    margin = 1.0                   #   mm around it (default 0)
    layers = ["F.Cu", "In1.Cu"]    #   default: every copper layer
    block = ["pour", "vias"]       #   any of pour, tracks, vias (default: ["pour"]) - e.g. no GND under an antenna

    [routing]                      # mode = "manual": tscircuit places only, routes.toml routes (vibepcb.routes);
    mode = "manual"                #   default "auto" = tscircuit's autorouter

    [escape.U2]                    # ball grid escaped by VibePCB, not tscircuit's router (vibepcb.fanout);
    track_width = 0.1              #   all optional: track_width, clearance, via_diameter, via_drill,
    layers = ["bottom"]            #   layers (escape layers of balls that can't leave on top)

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
ESCAPE_KEYS = ("track_width", "clearance", "via_diameter", "via_drill", "layers")
KEEPOUT_KEYS = {"ref", "area", "margin", "layers", "block"}
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
    known = {"netclass", "length_group", "diff_pair", "planes", "escape", "routing", "keepout"}
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
    if data.get("routing", {}).get("mode", "auto") not in ("auto", "manual"):
        msg = f'{where}: [routing] mode must be "auto" (tscircuit routes) or "manual" (routes.toml)'
        raise ConfigError(msg)
    for name, k in data.get("keepout", {}).items():
        if ("ref" in k) == ("area" in k):
            msg = f'{where}: keepout {name!r} needs either ref = "U1" (the part) or area = [x0, y0, x1, y1]'
            raise ConfigError(msg)
        if bad := set(k) - KEEPOUT_KEYS:
            msg = f"{where}: keepout {name!r}: unknown key(s) {sorted(bad)} (known: {sorted(KEEPOUT_KEYS)})"
            raise ConfigError(msg)
        if bad := set(k.get("block", [])) - {"pour", "tracks", "vias"}:
            msg = f'{where}: keepout {name!r}: block takes "pour", "tracks", "vias" (got {sorted(bad)})'
            raise ConfigError(msg)
        if "area" in k and len(k["area"]) != 4:
            msg = f"{where}: keepout {name!r}: area needs 4 numbers (x0 y0 x1 y1)"
            raise ConfigError(msg)
    if "planes" in data and not isinstance(data["planes"].get("nets"), list):
        msg = f"{where}: [planes] needs nets = [...]"
        raise ConfigError(msg)
    for ref, part in data.get("escape", {}).items():
        if bad := set(part) - set(ESCAPE_KEYS):
            msg = f"{where}: escape {ref!r}: unknown key(s) {sorted(bad)} (known: {list(ESCAPE_KEYS)})"
            raise ConfigError(msg)
    for name, group in data.get("length_group", {}).items():
        if not group.get("nets") or "max_skew" not in group:
            msg = f"{where}: length_group {name!r} needs nets = [...] and max_skew"
            raise ConfigError(msg)
