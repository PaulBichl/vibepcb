"""Per-board settings beyond what the TSX carries: ``boards/<board>/vibepcb.toml``.

tscircuit has no net classes, impedance-driven widths or KiCad length rules; ``fix`` writes
these from the board's config file into the KiCad project (:func:`fixes.apply_board_config`)::

    [netclass.USB]                 # KiCad net class
    nets = ["USB_D*"]              # net-name patterns (KiCad wildcards: * ?)
    track_width = 0.2              # mm; optional: clearance, via_diameter, via_drill,
    diff_pair_width = 0.2          #     diff_pair_width, diff_pair_gap
    diff_pair_gap = 0.15
    impedance = 50                 # optional, ohm: track_width solved from the [stackup] (vibepcb.stackup)
    impedance_diff = 90            # optional, ohm: diff_pair_width / _gap solved (a given gap stays fixed)
    layer = "F.Cu"                 # optional: routing layer the impedance is solved for (default F.Cu)

    [stackup]                      # for impedance: a preset (vibepcb.stackup.PRESETS, JLCPCB's), or
    preset = "jlc04161h-7628"      #   layers = [{name = "F.Cu", copper = 0.035}, {dielectric = 0.2104, er = 4.4}, ...]
                                   #   closed-form, a few % off a field solver: the fab's calculator is final

    [length_group.SDRAM_DQ]        # nets routed to matching lengths
    nets = ["SD_DQ*"]
    max_skew = 2.0                 # mm, longest - shortest
    tune_to = 1.0                  # optional: meanders bring the group to this spread (vibepcb.tune);
                                   #   default 90 % of max_skew

    [diff_pair]                    # pairs = nets named <base>_P/_N or <base>+/-
    max_uncoupled = 5.0            # mm of the pair routed apart

    [planes]                       # nets with a plane: each SMD pad gets its own via to it,
    nets = ["GND", "V3V3"]         #   the router's tracks of the net go (fixes.plane_pad_vias)

    [pour.v3v3]                    # a pour the build lacks (manual routing: tscircuit emits pours with its routes):
    net = "V3V3"                   #   copy of the board's pour settings over the outline, inset by margin (default 0.5)
    layers = ["In2.Cu"]
    # area = [x0, y0, x1, y1]        #   optional: only this box (board mm) - a split plane / a patch under a part;
    # priority = 2                   #   it wins over a full pour of the layer (KiCad zone priority)

    [keepout.antenna]              # rule area (KiCad enforces it: pour fill + DRC; the router keeps out of tracks / vias)
    ref = "U3"                     #   over this part's pads + courtyard (or: area = [x0, y0, x1, y1] board mm)
    margin = 1.0                   #   mm around it (default 0)
    layers = ["F.Cu", "In1.Cu"]    #   default: every copper layer
    block = ["pour", "vias"]       #   any of pour, tracks, vias (default: ["pour"]) - e.g. no GND under an antenna

    [routing]                      # mode = "manual": tscircuit places only, routes.toml routes (vibepcb.routes);
    mode = "manual"                #   default "auto" = tscircuit's autorouter

    [rules]                        # design / manufacturing rule profiles (vibepcb.rules): rules/<name>.toml,
    profiles = ["jlcpcb-4l", "ipc-class2"]  # fab capability first, application layers after
    [rules.override]               # single values; later profiles and overrides win, an override
    min_clearance = 0.15           #   below the fab capability is an error (`pcb rules <board>`)
    track_width = 0.2              #   Default net class: track_width, clearance, via_diameter, via_drill

    [escape.U2]                    # ball grid escaped by VibePCB, not tscircuit's router (vibepcb.fanout);
    track_width = 0.1              #   all optional: track_width, clearance, via_diameter, via_drill,
    layers = ["bottom"]            #   layers (escape layers of balls that can't leave on top)

    [thermal_vias.U3]              # via grid in an exposed pad, into the inner / bottom planes (fixes.thermal_pad_vias)
    pad = "9"                      #   pad number (default: the part's largest SMD pad)
    pitch = 1.0                    #   all optional, mm: pitch 1.0, drill 0.3, size 0.5,
    drill = 0.3                    #   margin 0.2 (via edge to pad edge)

    [testpoints]                   # house rule in `check`: every rail and GND needs a test point (vibepcb.testpoints)
    required = ["power", "gnd", "RESET"]  # "power" / "gnd" = rails found by name + power symbols; others are
                                   #   net-name patterns (* ?). Default ["power", "gnd"]; [] = rule off
    ignore = ["VBUS_RAW"]          # nets exempt (patterns)
    severity = "warning"           # or "error": a missing test point then fails `check`

Net names: a net gets the name of its ``<net>``, else of a named ``<trace name="...">`` on it
(see ``schgen``), so name the traces a rule should catch.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any

from vibepcb import paths, stackup, testpoints

if TYPE_CHECKING:
    from pathlib import Path

CONFIG_NAME = "vibepcb.toml"
ESCAPE_KEYS = ("track_width", "clearance", "via_diameter", "via_drill", "layers")
KEEPOUT_KEYS = {"ref", "area", "margin", "layers", "block"}
THERMAL_VIA_KEYS = {"pad", "pitch", "drill", "size", "margin"}
# rule-profile values (mm; vibepcb.rules): fab / application minimums, the Default net class, the board
RULE_MIN_KEYS = (
    "min_track_width",
    "min_clearance",
    "min_via_drill",
    "min_via_diameter",
    "min_via_annular_ring",
    "min_hole_to_hole",
    "min_hole_clearance",
    "min_pad_hole_to_hole",
    "min_pth_annular_ring",
    "min_copper_edge_clearance",
    "min_silk_width",
    "min_silk_clearance",
)
RULE_CLASS_KEYS = ("track_width", "clearance", "via_diameter", "via_drill")
RULE_KEYS = (*RULE_MIN_KEYS, *RULE_CLASS_KEYS, "board_thickness")
NETCLASS_KEYS = ("track_width", "clearance", "via_diameter", "via_drill", "diff_pair_width", "diff_pair_gap")
IMPEDANCE_KEYS = ("impedance", "impedance_diff", "layer")  # solved into NETCLASS_KEYS (vibepcb.stackup)


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
    known = {
        "netclass",
        "length_group",
        "diff_pair",
        "planes",
        "escape",
        "routing",
        "keepout",
        "pour",
        "stackup",
        "rules",
        "testpoints",
        "thermal_vias",
    }
    if unknown := set(data) - known:
        msg = f"{where}: unknown section(s) {sorted(unknown)} (known: {sorted(known)})"
        raise ConfigError(msg)
    stack = None
    if "stackup" in data:
        try:
            stack = stackup.from_config(data["stackup"])
        except stackup.StackupError as e:
            msg = f"{where}: {e}"
            raise ConfigError(msg) from e
    for name, cls in data.get("netclass", {}).items():
        if not cls.get("nets"):
            msg = f"{where}: netclass {name!r} needs nets = [...]"
            raise ConfigError(msg)
        if bad := set(cls) - {"nets", *NETCLASS_KEYS, *IMPEDANCE_KEYS}:
            msg = f"{where}: netclass {name!r}: unknown key(s) {sorted(bad)}"
            raise ConfigError(msg)
        if not {"impedance", "impedance_diff"} & set(cls):
            if "layer" in cls:
                msg = f"{where}: netclass {name!r}: layer only goes with impedance / impedance_diff"
                raise ConfigError(msg)
            continue
        if stack is None:
            msg = f"{where}: netclass {name!r}: impedance needs a [stackup] (preset or layers)"
            raise ConfigError(msg)
        try:
            stackup.netclass_widths(stack, cls)
        except stackup.StackupError as e:
            msg = f"{where}: netclass {name!r}: {e}"
            raise ConfigError(msg) from e
    _validate_rules(data.get("rules", {}), where)
    try:
        testpoints.validate(data.get("testpoints", {}), where)
    except ValueError as e:
        raise ConfigError(str(e)) from e
    if data.get("routing", {}).get("mode", "auto") not in ("auto", "manual"):
        msg = f'{where}: [routing] mode must be "auto" (tscircuit routes) or "manual" (routes.toml)'
        raise ConfigError(msg)
    for name, k in data.get("pour", {}).items():
        if "net" not in k or not k.get("layers"):
            msg = f'{where}: pour {name!r} needs net = "NAME" and layers = ["In2.Cu", ...]'
            raise ConfigError(msg)
        if bad := set(k) - {"net", "layers", "margin", "area", "priority"}:
            msg = f"{where}: pour {name!r}: unknown key(s) {sorted(bad)} (known: area, layers, margin, net, priority)"
            raise ConfigError(msg)
        if "area" in k and len(k["area"]) != 4:
            msg = f"{where}: pour {name!r}: area needs 4 numbers (x0 y0 x1 y1, board mm)"
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
    for ref, spec in data.get("thermal_vias", {}).items():
        if not isinstance(spec, dict) or (bad := set(spec) - THERMAL_VIA_KEYS):
            msg = f"{where}: thermal_vias {ref!r}: a table with keys from {sorted(THERMAL_VIA_KEYS)}"
            raise ConfigError(msg)
        if any(not isinstance(v, int | float) or v <= 0 for k, v in spec.items() if k != "pad"):
            msg = f"{where}: thermal_vias {ref!r}: pitch / drill / size / margin must be positive numbers (mm)"
            raise ConfigError(msg)
    for name, group in data.get("length_group", {}).items():
        if not group.get("nets") or "max_skew" not in group:
            msg = f"{where}: length_group {name!r} needs nets = [...] and max_skew"
            raise ConfigError(msg)


def _validate_rules(rules: dict[str, Any], where: str) -> None:
    if bad := set(rules) - {"profiles", "override"}:
        msg = f"{where}: [rules]: unknown key(s) {sorted(bad)} (known: override, profiles)"
        raise ConfigError(msg)
    profiles = rules.get("profiles", [])
    if not isinstance(profiles, list) or not all(isinstance(p, str) for p in profiles):
        msg = f'{where}: [rules] profiles must be a list of names, e.g. ["jlcpcb-4l", "ipc-class2"]'
        raise ConfigError(msg)
    for key, value in rules.get("override", {}).items():
        if key not in RULE_KEYS:
            msg = f"{where}: [rules.override]: unknown key {key!r} (known: {', '.join(RULE_KEYS)})"
            raise ConfigError(msg)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            msg = f"{where}: [rules.override] {key} must be a positive number (mm), got {value!r}"
            raise ConfigError(msg)
