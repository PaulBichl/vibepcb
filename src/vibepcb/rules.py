"""Design / manufacturing rule profiles: ``rules/<name>.toml``, picked per board in ``vibepcb.toml``.

A profile is a flat TOML file of values in mm (keys: ``config.RULE_KEYS``) plus optional net classes::

    kind = "fab"                   # "fab": the fab's capability (its min_* values are floors)
    description = "JLCPCB 4 layer" #   "application": a layer on top (IPC class, high voltage, ...)
    min_clearance = 0.09
    board_thickness = 1.6
    [netclass.HV60]                # merged with the board's [netclass] (the board wins key by key)
    nets = ["HV_*"]
    clearance = 0.6

The board lists profiles in order (``[rules] profiles = [...]``) and sets single values in
``[rules.override]``. Merge: a later profile replaces a value, except that an application layer only
tightens a ``min_*`` value (the larger one stays); overrides replace anything. Each merged value
remembers its source. A value below the fab's capability (a ``min_*`` of a ``fab`` profile, or the
Default net class / a net class below the merged minimum) raises :class:`RulesError`.

Profiles are looked up in ``<project>/rules/`` first (own fabs), then in the tool's ``rules/``.
``fixes.apply_rule_profiles`` writes the result into the KiCad project (design-settings minimums,
Default net class, custom DRC rules), ``fixes.standard_board_thickness`` takes the board thickness.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibepcb import paths, stackup
from vibepcb.config import IMPEDANCE_KEYS, NETCLASS_KEYS, RULE_CLASS_KEYS, RULE_KEYS, RULE_MIN_KEYS, ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

TOOL_RULES_DIR = Path(__file__).resolve().parents[2] / "rules"
OVERRIDE = "override"
TOLERANCE_MM = 1e-9

# profile key -> KiCad .kicad_pro ``board.design_settings.rules`` key
PRO_RULE_KEYS = {
    "min_track_width": "min_track_width",
    "min_clearance": "min_clearance",
    "min_via_drill": "min_through_hole_diameter",
    "min_via_diameter": "min_via_diameter",
    "min_via_annular_ring": "min_via_annular_width",
    "min_hole_to_hole": "min_hole_to_hole",
    "min_hole_clearance": "min_hole_clearance",
    "min_copper_edge_clearance": "min_copper_edge_clearance",
    "min_silk_width": "min_text_thickness",
    "min_silk_clearance": "min_silk_clearance",
}
# KiCad has no design setting for these: custom rules in the .kicad_dru
DRU_RULES = {
    "min_pad_hole_to_hole": (
        "A.Pad_Type == 'Through-hole' && B.Pad_Type == 'Through-hole'",
        "hole_to_hole",
    ),
    "min_pth_annular_ring": ("A.Pad_Type == 'Through-hole'", "annular_width"),
}
DRU_RULE_PREFIX = "vibepcb rule profile"
# net-class value -> the minimum it must not go below
CLASS_FLOORS = {
    "track_width": "min_track_width",
    "clearance": "min_clearance",
    "via_diameter": "min_via_diameter",
    "via_drill": "min_via_drill",
    "diff_pair_width": "min_track_width",
    "diff_pair_gap": "min_clearance",
}


class RulesError(ConfigError):
    """A profile is missing / malformed, or a value goes below the fab's capability."""


@dataclass(frozen=True)
class Setting:
    value: float
    source: str  # profile name, "override" or "board" (a net class of vibepcb.toml)


@dataclass
class NetClassRule:
    nets: list[str]
    nets_source: str
    values: dict[str, Setting] = field(default_factory=dict)


@dataclass
class RuleSet:
    profiles: list[str]
    values: dict[str, Setting] = field(default_factory=dict)
    floors: dict[str, Setting] = field(default_factory=dict)  # min_* of the fab profile(s): capability
    netclasses: dict[str, NetClassRule] = field(default_factory=dict)

    def get(self, key: str) -> float | None:
        s = self.values.get(key)
        return None if s is None else s.value


# verification policy (vibepcb.sim) and reviewer checklists (vibepcb.review) live next to the profiles
NOT_PROFILES = {"sim-defaults", "review-checklists"}


def profile_dirs() -> list[Path]:
    out = [paths.ROOT / "rules"]
    if out[0] != TOOL_RULES_DIR:
        out.append(TOOL_RULES_DIR)
    return out


def available() -> list[str]:
    return sorted({p.stem for d in profile_dirs() if d.is_dir() for p in d.glob("*.toml")} - NOT_PROFILES)


def load_profile(name: str) -> dict[str, Any]:
    """The profile ``name`` (``rules/<name>.toml``), validated."""
    path = next((d / f"{name}.toml" for d in profile_dirs() if (d / f"{name}.toml").is_file()), None)
    if path is None:
        msg = f"rule profile {name!r} not found (available: {', '.join(available()) or 'none'})"
        raise RulesError(msg)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    where = f"rules/{path.name}"
    if data.get("kind") not in ("fab", "application"):
        msg = f'{where}: kind must be "fab" or "application"'
        raise RulesError(msg)
    for key, value in data.items():
        if key in ("kind", "description", "netclass"):
            continue
        if key not in RULE_KEYS:
            msg = f"{where}: unknown key {key!r} (known: {', '.join(RULE_KEYS)})"
            raise RulesError(msg)
        _positive(value, f"{where}: {key}")
    for cls_name, cls in data.get("netclass", {}).items():
        if not cls.get("nets") or set(cls) - {"nets", *NETCLASS_KEYS}:
            msg = f"{where}: netclass {cls_name!r} needs nets = [...] and only {', '.join(NETCLASS_KEYS)}"
            raise RulesError(msg)
        for key in set(cls) - {"nets"}:
            _positive(cls[key], f"{where}: netclass {cls_name} {key}")
    return data


def _positive(value: object, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        msg = f"{what} must be a positive number (mm), got {value!r}"
        raise RulesError(msg)


def merge(cfg: Mapping[str, Any]) -> RuleSet | None:
    """The board's merged rule set (``None`` without a ``[rules]`` section), checked against the fab."""
    section = cfg.get("rules")
    if section is None:
        return None
    names = list(section.get("profiles", []))
    rs = RuleSet(names)
    for name in names:
        profile = load_profile(name)
        fab = profile["kind"] == "fab"
        for key, value in profile.items():
            if key not in RULE_KEYS:
                continue
            old = rs.values.get(key)
            # an application layer only tightens a minimum
            if fab or key not in RULE_MIN_KEYS or old is None or float(value) > old.value:
                rs.values[key] = Setting(float(value), name)
            if fab and key in RULE_MIN_KEYS:
                rs.floors[key] = Setting(float(value), name)
        for cls_name, cls in profile.get("netclass", {}).items():
            rule = rs.netclasses.setdefault(cls_name, NetClassRule([], name))
            rule.nets, rule.nets_source = [str(p) for p in cls["nets"]], name
            rule.values.update({k: Setting(float(v), name) for k, v in cls.items() if k != "nets"})
    for key, value in section.get(OVERRIDE, {}).items():
        rs.values[key] = Setting(float(value), OVERRIDE)
    for cls_name, cls in cfg.get("netclass", {}).items():
        rule = rs.netclasses.setdefault(cls_name, NetClassRule([], "board"))
        if cls.get("nets"):
            rule.nets, rule.nets_source = [str(p) for p in cls["nets"]], "board"
        values = {k: float(v) for k, v in cls.items() if k not in ("nets", *IMPEDANCE_KEYS)}
        if {"impedance", "impedance_diff"} & set(cls):  # widths solved from the stack-up (config checked it has one)
            values.update(stackup.netclass_widths(stackup.from_config(cfg["stackup"]), cls)[0])
        rule.values.update({k: Setting(v, "board") for k, v in values.items()})
    _check(rs)
    return rs


def _check(rs: RuleSet) -> None:
    def below(s: Setting, floor: Setting, what: str, floor_what: str) -> None:
        if s.value < floor.value - TOLERANCE_MM:
            msg = f"{what} = {s.value:g} mm ({s.source}) is below {floor_what} = {floor.value:g} mm ({floor.source})"
            raise RulesError(msg)

    for key, floor in rs.floors.items():
        if key in rs.values:
            below(rs.values[key], floor, key, "the fab capability")
    for key in RULE_CLASS_KEYS:
        if key in rs.values and CLASS_FLOORS[key] in rs.values:
            below(rs.values[key], rs.values[CLASS_FLOORS[key]], f"Default net class {key}", CLASS_FLOORS[key])
    for name, rule in rs.netclasses.items():
        for key, s in rule.values.items():
            if CLASS_FLOORS[key] in rs.values:
                below(s, rs.values[CLASS_FLOORS[key]], f"net class {name} {key}", CLASS_FLOORS[key])
    diameter, drill, ring = (rs.values.get(k) for k in ("via_diameter", "via_drill", "min_via_annular_ring"))
    if diameter and drill and ring and (diameter.value - drill.value) / 2 < ring.value - TOLERANCE_MM:
        msg = (
            f"Default via {diameter.value:g}/{drill.value:g} mm ({diameter.source}/{drill.source}) has an annular ring "
            f"of {(diameter.value - drill.value) / 2:g} mm, below min_via_annular_ring = {ring.value:g} mm ({ring.source})"
        )
        raise RulesError(msg)


def board_thickness(cfg: Mapping[str, Any]) -> float | None:
    rs = merge(cfg)
    return None if rs is None else rs.get("board_thickness")


def pro_rules(rs: RuleSet) -> dict[str, float]:
    """The .kicad_pro ``board.design_settings.rules`` entries the rule set defines."""
    return {pro_key: rs.values[key].value for key, pro_key in PRO_RULE_KEYS.items() if key in rs.values}


def default_class(rs: RuleSet, current: Mapping[str, Any]) -> dict[str, Any]:
    """The Default net class: values the rule set sets, else the current ones raised to the minimums."""
    out = dict(current)
    for key in RULE_CLASS_KEYS:
        if key in rs.values:
            out[key] = rs.values[key].value
        elif key in out and CLASS_FLOORS[key] in rs.values:
            out[key] = max(float(out[key]), rs.values[CLASS_FLOORS[key]].value)
    ring = rs.get("min_via_annular_ring")
    if ring is not None and "via_diameter" not in rs.values and "via_drill" in out and "via_diameter" in out:
        out["via_diameter"] = round(max(float(out["via_diameter"]), float(out["via_drill"]) + 2 * ring), 6)
    return out


def dru_rules(rs: RuleSet) -> list[str]:
    """Custom DRC rules (.kicad_dru forms) for the values KiCad has no design setting for."""
    out = []
    for key, (condition, constraint) in DRU_RULES.items():
        if (s := rs.values.get(key)) is not None:
            out.append(
                f'(rule "{DRU_RULE_PREFIX}: {key} ({s.source})"\n  (condition "{condition}")\n'
                f"  (constraint {constraint} (min {s.value:g}mm)))\n"
            )
    return out


def netclasses(cfg: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The net classes of the profiles and the board (``nets`` + values), as the board's [netclass] reads."""
    rs = merge(cfg)
    if rs is None:
        return dict(cfg.get("netclass", {}))
    return {
        name: {"nets": rule.nets, **{k: s.value for k, s in rule.values.items()}}
        for name, rule in rs.netclasses.items()
    }


def table(rs: RuleSet) -> list[str]:
    """The merged rule set, one line per value with its source (``pcb rules``)."""
    lines = [f"profiles: {', '.join(rs.profiles) or '(none)'}"]
    width = max(len(k) for k in RULE_KEYS)
    for key in RULE_KEYS:
        if (s := rs.values.get(key)) is None:
            continue
        floor = rs.floors.get(key)
        note = f"   (fab capability {floor.value:g}, {floor.source})" if floor and floor.source != s.source else ""
        lines.append(f"  {key:<{width}}  {s.value:>6g} mm   {s.source}{note}")
    for name, rule in rs.netclasses.items():
        lines.append(f"netclass {name}: nets {', '.join(rule.nets) or '(none)'} ({rule.nets_source})")
        lines += [f"  {k:<{width}}  {s.value:>6g} mm   {s.source}" for k, s in rule.values.items()]
    return lines
