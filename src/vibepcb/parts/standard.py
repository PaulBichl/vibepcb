"""The house component standard as machine rules: ``rules/parts/<name>.toml``.

The standard itself is prose (``rules/parts/<name>.md``); the TOML says it rule by rule, each with the section it
comes from, so a reviewer can trace every value::

    [symbol]
    pin_pitch = { value = 2.0, source = "§1.2 distance between pins at least 2 mm" }
    [choices]                       # what the standard leaves open: a reason instead of a section
    pad_corner_ratio = { value = 0.25, reason = "IPC-7351C" }
    [unexpressed]
    items = ["§1.4 colours are Altium preferences"]

Unknown sections or keys fail (a typo in a rule file must not silently drop a rule); a rule without its
source / reason fails; a key the file leaves out takes the built-in default, marked ``default`` as its source.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vibepcb import paths

TOOL_DIR = Path(__file__).resolve().parents[3] / "rules" / "parts"
DEFAULT = "altium-students-2022"

# section -> key -> built-in default (also the list of known keys)
DEFAULTS: dict[str, dict[str, Any]] = {
    "symbol": {
        "pin_pitch": 2.54,
        "pin_length": 2.54,
        "pin_font_pt": 6,
        "pin_number_offset": 0.5,
        "default_pin_type": "passive",
        "inputs": "left",
        "outputs": "right",
        "supply": "top",
        "ground": "bottom",
        "number_repeated_names": False,
        "exposed_pad_pin": True,
        "function_drawing": False,
        "designator_visible": True,
        "designator_position": "top_left",
        "designator_font_pt": 8,
        "comment_empty_hidden": False,
        "parameters": [],
        "body_fill": False,
        "reference_prefix": {"ic": "U"},
    },
    "footprint": {
        "land_pattern": "datasheet_first",
        "density": "N",
        "manual_families": ["tht", "connector", "custom"],
        "origin": "pads_centre",
        "toe_min": 0.0,
        "pad_shape": "roundrect",
        "mask_expansion": 0.0,
        "paste_expansion": 0.0,
        "component_outline": "body",
        "placement_grid": 0.05,
        "placement_margin": [0.25, 0.25],
        "centre_cross": False,
        "assembly_designator": True,
        "assembly_polarity": True,
        "pin1_text": "",
        "silk_pad_clearance": 0.2,
        "silk_min_width": 0.1,
        "silk_min_text": 1.0,
        "silk_polarity": True,
        "silk_connector_front": True,
    },
    "layers": {
        "mech13": "F.Fab",
        "mech15": "F.Fab",
    },
    "choices": {
        "pad_corner_ratio": 0.25,
        "pad_corner_max": 0.25,
        "silk_width": 0.15,
        "mech_width": 0.1,
        "courtyard_width": 0.05,
        "placement_margin": 0.25,
        "assembly_text": 0.8,
        "symbol_body_width": 0.254,
        "pt_to_mm": 0.254,
    },
}
SECTIONS = {"meta", "unexpressed", *DEFAULTS}
DENSITIES = ("L", "N", "M")


class StandardError(ValueError):
    """A rule file that can't be used."""


@dataclass(frozen=True)
class Rule:
    value: Any
    source: str  # "§1.2 ..." / the reason of a choice / "default"


@dataclass
class Standard:
    name: str
    path: Path | None
    rules: dict[str, dict[str, Rule]] = field(default_factory=dict)
    unexpressed: list[str] = field(default_factory=list)

    def rule(self, section: str, key: str) -> Rule:
        return self.rules[section][key]

    def value(self, section: str, key: str) -> Any:
        return self.rules[section][key].value

    def num(self, section: str, key: str) -> float:
        return float(self.value(section, key))

    def flag(self, section: str, key: str) -> bool:
        return bool(self.value(section, key))

    def text(self, section: str, key: str) -> str:
        return str(self.value(section, key))

    def font_mm(self, section: str, key: str) -> float:
        """A font size given in points (Altium) as a KiCad text size in mm."""
        return round(self.num(section, key) * self.num("choices", "pt_to_mm"), 3)


def dirs() -> list[Path]:
    """Where rule files are looked up: the project's ``rules/parts`` first, then the tool's."""
    return list(dict.fromkeys([paths.ROOT / "rules" / "parts", TOOL_DIR]))


def find(name: str) -> Path:
    for d in dirs():
        if (path := d / f"{name}.toml").is_file():
            return path
    msg = f"component standard {name!r} not found in {', '.join(str(d) for d in dirs())}"
    raise StandardError(msg)


def load(name: str = DEFAULT) -> Standard:
    path = find(name)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        msg = f"{path}: {e}"
        raise StandardError(msg) from e
    return parse(data, name=name, path=path)


def _check_type(where: str, value: Any, default: Any) -> None:
    numeric = (int, float)
    if isinstance(default, bool) or isinstance(value, bool):
        ok = isinstance(value, bool) and isinstance(default, bool)
    elif isinstance(default, numeric):
        ok = isinstance(value, numeric)
    else:
        ok = isinstance(value, type(default))
    if not ok:
        msg = f"{where}: expected {type(default).__name__}, got {type(value).__name__} ({value!r})"
        raise StandardError(msg)


def parse(data: dict[str, Any], *, name: str, path: Path | None = None) -> Standard:
    where = str(path or name)
    if unknown := set(data) - SECTIONS:
        msg = f"{where}: unknown section(s) {sorted(unknown)} (known: {sorted(SECTIONS)})"
        raise StandardError(msg)
    std = Standard(name=str(data.get("meta", {}).get("name", name)), path=path)
    for section, defaults in DEFAULTS.items():
        given = data.get(section, {})
        if unknown := set(given) - set(defaults):
            msg = f"{where}: [{section}]: unknown key(s) {sorted(unknown)} (known: {sorted(defaults)})"
            raise StandardError(msg)
        rules: dict[str, Rule] = {}
        for key, default in defaults.items():
            if key not in given:
                rules[key] = Rule(default, "default")
                continue
            entry = given[key]
            label = "reason" if section == "choices" else "source"
            if not isinstance(entry, dict) or "value" not in entry or not entry.get(label):
                msg = f'{where}: [{section}] {key}: write it as {{ value = ..., {label} = "..." }}'
                raise StandardError(msg)
            if extra := set(entry) - {"value", label}:
                msg = f"{where}: [{section}] {key}: unknown field(s) {sorted(extra)}"
                raise StandardError(msg)
            _check_type(f"{where}: [{section}] {key}", entry["value"], default)
            rules[key] = Rule(entry["value"], str(entry[label]))
        std.rules[section] = rules
    if std.text("footprint", "density") not in DENSITIES:
        msg = f"{where}: [footprint] density must be one of {DENSITIES}"
        raise StandardError(msg)
    items = data.get("unexpressed", {}).get("items", [])
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        msg = f"{where}: [unexpressed] items must be a list of strings"
        raise StandardError(msg)
    std.unexpressed = list(items)
    return std
