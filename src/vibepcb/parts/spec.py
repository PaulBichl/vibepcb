"""Part spec: ``parts/<mpn>.toml``, what the agent read from the datasheet (pins, package drawing).

Everything after the spec is deterministic. Every dimension is given as the drawing gives it, ``[min, max]`` or
``[min, nom, max]`` (a single number = exact), and every table carries the datasheet ``page`` it came from::

    [part]
    mpn = "LM358DR"
    manufacturer = "Texas Instruments"
    description = "Dual op-amp"
    datasheet = "https://www.ti.com/lit/ds/symlink/lm358.pdf"
    kind = "ic"                          # ic | opamp | resistor | capacitor | polar_capacitor | inductor
                                         #   | diode | led | connector | transistor | crystal
    [package]
    family = "SOIC"                      # see FAMILIES
    pins = 8
    pitch = 1.27
    span = [5.8, 6.2]                    # lead tip to lead tip (gull-wing, SOT); chips / QFN: the body
    body = { l = [4.8, 5.0], w = [3.8, 4.0], h = [1.75] }
    lead = { l = [0.4, 1.27], w = [0.31, 0.51] }      # foot length (T), width (b)
    exposed_pad = { l = [..], w = [..], number = "9" } # optional
    page = 30

    [land]                               # only families in the standard's manual list (custom, tht, connector)
    page = 31
    [[land.pad]]
    number = "1"; x = -2.7; y = -1.905; l = 1.55; w = 0.6   # mm, l along x; optional shape, drill, rotation

    [[pin]]
    number = "1"; name = "OUT1"; type = "output"; page = 3
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

KINDS = (
    "ic",
    "opamp",
    "resistor",
    "capacitor",
    "polar_capacitor",
    "inductor",
    "diode",
    "led",
    "connector",
    "transistor",
    "crystal",
)
# family -> (lead style, sides): gullwing = leads out of the body sides, nolead = pads under the body edge,
# chip = two end terminations; None = no IPC calculation (explicit [land])
FAMILIES: dict[str, tuple[str, int] | None] = {
    "chip": ("chip", 2),
    "SOD": ("gullwing", 2),
    "SOT": ("gullwing", 2),
    "SOIC": ("gullwing", 2),
    "SSOP": ("gullwing", 2),
    "TSSOP": ("gullwing", 2),
    "MSOP": ("gullwing", 2),
    "QFP": ("gullwing", 4),
    "DFN": ("nolead", 2),
    "QFN": ("nolead", 4),
    "custom": None,
    "connector": None,
    "tht": None,
}
PIN_TYPES = (
    "input",
    "output",
    "bidirectional",
    "tri_state",
    "passive",
    "power_in",
    "power_out",
    "open_collector",
    "open_emitter",
    "no_connect",
    "unspecified",
)
PAD_SHAPES = ("roundrect", "rect", "circle", "oval")
LIFECYCLES = ("active", "nrnd", "obsolete", "unknown")


class SpecError(ValueError):
    """A part spec that can't be used; the message names the field (and pin)."""


@dataclass(frozen=True)
class Dim:
    """A drawing dimension: min / nominal / max in mm."""

    min: float
    nom: float
    max: float

    @property
    def tol(self) -> float:
        return self.max - self.min


@dataclass(frozen=True)
class ExposedPad:
    l: Dim  # noqa: E741 - the drawing's name
    w: Dim
    number: str


@dataclass(frozen=True)
class Package:
    family: str
    pins: int
    pitch: float
    body_l: Dim  # along the pin rows (y) for two-sided parts; chips: terminal to terminal
    body_w: Dim
    height: float  # max
    span: Dim | None = None  # lead tip to tip across the rows (x)
    span_y: Dim | None = None  # four-sided parts: the other direction (default = span)
    lead_l: Dim | None = None  # foot / terminal length (T)
    lead_w: Dim | None = None  # lead / terminal width (b); chips: the body width
    pins_x: int | None = None  # four-sided parts: pins on the top / bottom sides (default pins / 4)
    exposed_pad: ExposedPad | None = None
    standoff: float = 0.0
    page: int = 0

    @property
    def style(self) -> str | None:
        found = FAMILIES[self.family]
        return found[0] if found else None

    @property
    def sides(self) -> int:
        found = FAMILIES[self.family]
        return found[1] if found else 0


@dataclass(frozen=True)
class LandPad:
    number: str
    x: float
    y: float
    l: float  # noqa: E741 - size along x
    w: float
    shape: str = "roundrect"
    rotation: float = 0.0
    drill: float = 0.0  # > 0: through hole


@dataclass(frozen=True)
class Land:
    pads: list[LandPad]
    page: int
    name: str = ""  # footprint name (the package's, e.g. HC49-US_P4.88); default <family>_<mpn>


@dataclass(frozen=True)
class Pin:
    number: str
    name: str
    type: str
    page: int
    side: str | None = None  # left | right | top | bottom: overrides the standard's placement
    hidden: bool = False


@dataclass
class Spec:
    mpn: str
    manufacturer: str
    description: str
    datasheet: str
    kind: str
    package: Package
    pins: list[Pin]
    land: Land | None = None
    source: str = ""
    lifecycle: str = "unknown"
    symbol_shape: str = "box"  # box | opamp

    @property
    def pad_numbers(self) -> list[str]:
        """The pads the package has: 1..pins, plus the exposed pad."""
        if self.land:
            return list(dict.fromkeys(p.number for p in self.land.pads))
        numbers = [str(i) for i in range(1, self.package.pins + 1)]
        if self.package.exposed_pad and self.package.exposed_pad.number not in numbers:
            numbers.append(self.package.exposed_pad.number)
        return numbers


def file_name(mpn: str) -> str:
    """A file-system safe name for an MPN (``/`` and other odd characters become ``_``)."""
    return re.sub(r"[^A-Za-z0-9.+-]", "_", mpn)


def dim(value: Any, where: str) -> Dim:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return Dim(float(value), float(value), float(value))
    if isinstance(value, list) and all(isinstance(v, int | float) and not isinstance(v, bool) for v in value):
        if len(value) == 2:
            lo, hi = map(float, value)
            d = Dim(lo, round((lo + hi) / 2, 4), hi)
        elif len(value) == 3:
            d = Dim(*map(float, value))
        elif len(value) == 1:
            d = Dim(*[float(value[0])] * 3)
        else:
            d = None
        if d is not None:
            if not d.min <= d.nom <= d.max:
                msg = f"{where}: needs min <= nom <= max, got {value}"
                raise SpecError(msg)
            return d
    msg = f"{where}: a dimension is a number, [min, max] or [min, nom, max], got {value!r}"
    raise SpecError(msg)


def _req(table: dict[str, Any], key: str, where: str, kind: type | tuple[type, ...]) -> Any:
    if key not in table:
        msg = f"{where}: missing {key!r}"
        raise SpecError(msg)
    value = table[key]
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        msg = f"{where}.{key}: wrong type {type(value).__name__} ({value!r})"
        raise SpecError(msg)
    return value


def _known(table: dict[str, Any], keys: set[str], where: str) -> None:
    if unknown := set(table) - keys:
        msg = f"{where}: unknown key(s) {sorted(unknown)} (known: {sorted(keys)})"
        raise SpecError(msg)


def _page(table: dict[str, Any], where: str) -> int:
    page = table.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        msg = f"{where}: 'page' (the datasheet page the numbers come from) is required"
        raise SpecError(msg)
    return page


def _package(raw: dict[str, Any]) -> Package:
    where = "[package]"
    _known(
        raw,
        {"family", "pins", "pitch", "body", "span", "span_y", "lead", "pins_x", "exposed_pad", "standoff", "page"},
        where,
    )
    family = _req(raw, "family", where, str)
    if family not in FAMILIES:
        msg = f"{where}.family: {family!r} is not one of {list(FAMILIES)}"
        raise SpecError(msg)
    body = _req(raw, "body", where, dict)
    _known(body, {"l", "w", "h"}, f"{where}.body")
    lead = raw.get("lead", {})
    _known(lead, {"l", "w"}, f"{where}.lead")
    ep = raw.get("exposed_pad")
    exposed = None
    if ep is not None:
        _known(ep, {"l", "w", "number"}, f"{where}.exposed_pad")
        exposed = ExposedPad(
            dim(_req(ep, "l", f"{where}.exposed_pad", (int, float, list)), f"{where}.exposed_pad.l"),
            dim(_req(ep, "w", f"{where}.exposed_pad", (int, float, list)), f"{where}.exposed_pad.w"),
            str(_req(ep, "number", f"{where}.exposed_pad", (str, int))),
        )
    package = Package(
        family=family,
        pins=_req(raw, "pins", where, int),
        pitch=float(_req(raw, "pitch", where, (int, float))),
        body_l=dim(_req(body, "l", f"{where}.body", (int, float, list)), f"{where}.body.l"),
        body_w=dim(_req(body, "w", f"{where}.body", (int, float, list)), f"{where}.body.w"),
        height=dim(_req(body, "h", f"{where}.body", (int, float, list)), f"{where}.body.h").max,
        span=dim(raw["span"], f"{where}.span") if "span" in raw else None,
        span_y=dim(raw["span_y"], f"{where}.span_y") if "span_y" in raw else None,
        lead_l=dim(lead["l"], f"{where}.lead.l") if "l" in lead else None,
        lead_w=dim(lead["w"], f"{where}.lead.w") if "w" in lead else None,
        pins_x=raw.get("pins_x"),
        exposed_pad=exposed,
        standoff=float(raw.get("standoff", 0.0)),
        page=_page(raw, where),
    )
    if package.pins < 1:
        msg = f"{where}.pins: must be >= 1"
        raise SpecError(msg)
    if package.style in ("gullwing", "nolead"):
        for key, value in (("span", package.span), ("lead.l", package.lead_l), ("lead.w", package.lead_w)):
            if value is None:
                msg = f"{where}: family {family} needs {key} for the IPC-7351 land pattern"
                raise SpecError(msg)
    if package.style == "chip" and package.lead_l is None:
        msg = f"{where}: family chip needs lead.l (the terminal length)"
        raise SpecError(msg)
    if package.sides == 4 and package.pins % 2:
        msg = f"{where}: a four-sided package needs an even pin count"
        raise SpecError(msg)
    if package.sides == 2 and package.family not in ("chip", "SOT") and package.pins % 2:
        msg = f"{where}: a two-row package needs an even pin count (use family 'custom' with [land] otherwise)"
        raise SpecError(msg)
    return package


def _land(raw: dict[str, Any]) -> Land:
    where = "[land]"
    _known(raw, {"pad", "page", "name"}, where)
    pads = []
    for i, p in enumerate(_req(raw, "pad", where, list)):
        w = f"[[land.pad]] #{i + 1}"
        _known(p, {"number", "x", "y", "l", "w", "shape", "rotation", "drill"}, w)
        shape = p.get("shape", "roundrect")
        if shape not in PAD_SHAPES:
            msg = f"{w}.shape: {shape!r} is not one of {PAD_SHAPES}"
            raise SpecError(msg)
        pad = LandPad(
            number=str(_req(p, "number", w, (str, int))),
            x=float(_req(p, "x", w, (int, float))),
            y=float(_req(p, "y", w, (int, float))),
            l=float(_req(p, "l", w, (int, float))),
            w=float(_req(p, "w", w, (int, float))),
            shape=shape,
            rotation=float(p.get("rotation", 0.0)),
            drill=float(p.get("drill", 0.0)),
        )
        if pad.l <= 0 or pad.w <= 0 or pad.drill < 0 or (pad.drill and pad.drill >= min(pad.l, pad.w)):
            msg = f"{w} (pad {pad.number}): sizes must be > 0 and the drill smaller than the pad"
            raise SpecError(msg)
        pads.append(pad)
    if not pads:
        msg = f"{where}: no pads"
        raise SpecError(msg)
    name = raw.get("name", "")
    if not isinstance(name, str) or (name and not re.fullmatch(r"[A-Za-z0-9._+-]+", name)):
        msg = f"{where}.name: letters, digits and . _ + - only, got {name!r}"
        raise SpecError(msg)
    return Land(pads, _page(raw, where), name)


def _pins(raw: list[dict[str, Any]]) -> list[Pin]:
    pins = []
    for i, p in enumerate(raw):
        where = f"[[pin]] #{i + 1}"
        _known(p, {"number", "name", "type", "page", "side", "hidden"}, where)
        number = str(_req(p, "number", where, (str, int)))
        where = f"[[pin]] {number}"
        pin_type = _req(p, "type", where, str)
        if pin_type not in PIN_TYPES:
            msg = f"{where}.type: {pin_type!r} is not one of {PIN_TYPES}"
            raise SpecError(msg)
        side = p.get("side")
        if side is not None and side not in ("left", "right", "top", "bottom"):
            msg = f"{where}.side: {side!r} is not left / right / top / bottom"
            raise SpecError(msg)
        pins.append(
            Pin(
                number,
                str(_req(p, "name", where, (str, int))),
                pin_type,
                _page(p, where),
                side,
                bool(p.get("hidden", False)),
            )
        )
    return pins


def parse(data: dict[str, Any]) -> Spec:
    _known(data, {"part", "package", "land", "pin", "symbol"}, "spec")
    part = _req(data, "part", "spec", dict)
    _known(part, {"mpn", "manufacturer", "description", "datasheet", "kind", "source", "lifecycle"}, "[part]")
    kind = _req(part, "kind", "[part]", str)
    if kind not in KINDS:
        msg = f"[part].kind: {kind!r} is not one of {KINDS}"
        raise SpecError(msg)
    lifecycle = part.get("lifecycle", "unknown")
    if lifecycle not in LIFECYCLES:
        msg = f"[part].lifecycle: {lifecycle!r} is not one of {LIFECYCLES}"
        raise SpecError(msg)
    symbol = data.get("symbol", {})
    _known(symbol, {"shape"}, "[symbol]")
    shape = symbol.get("shape", "opamp" if kind == "opamp" else "box")
    if shape not in ("box", "opamp"):
        msg = f"[symbol].shape: {shape!r} is not box / opamp"
        raise SpecError(msg)
    spec = Spec(
        mpn=_req(part, "mpn", "[part]", str),
        manufacturer=_req(part, "manufacturer", "[part]", str),
        description=str(part.get("description", "")),
        datasheet=_req(part, "datasheet", "[part]", str),
        kind=kind,
        package=_package(_req(data, "package", "spec", dict)),
        pins=_pins(data.get("pin", [])),
        land=_land(data["land"]) if "land" in data else None,
        source=str(part.get("source", "")),
        lifecycle=lifecycle,
        symbol_shape=shape,
    )
    validate(spec)
    return spec


def validate(spec: Spec) -> None:
    """Cross-checks: pins complete and unique, land present where the family needs one."""
    pkg = spec.package
    if pkg.style is None and spec.land is None:
        msg = f"[land]: family {pkg.family} has no IPC calculation; give the datasheet land pattern as [[land.pad]]"
        raise SpecError(msg)
    numbers = [p.number for p in spec.pins]
    if dupes := sorted({n for n in numbers if numbers.count(n) > 1}):
        msg = f"[[pin]]: duplicate pin number(s) {dupes}"
        raise SpecError(msg)
    pads = spec.pad_numbers
    if missing := [n for n in pads if n not in numbers]:
        msg = f"[[pin]]: no pin for pad(s) {missing}"
        raise SpecError(msg)
    if extra := [n for n in numbers if n not in pads]:
        msg = f"[[pin]]: pin(s) {extra} have no pad in the package"
        raise SpecError(msg)
    if spec.land is None and pkg.style == "chip" and pkg.pins != 2:
        msg = "[package]: family chip has 2 pins"
        raise SpecError(msg)
    if pkg.span and pkg.span.min <= 0:
        msg = "[package].span: must be > 0"
        raise SpecError(msg)
    if pkg.span and pkg.lead_l and pkg.span.min - 2 * pkg.lead_l.max <= 0:
        msg = "[package]: span.min - 2 x lead.l.max <= 0 (span is tip to tip across the package, lead.l one foot)"
        raise SpecError(msg)


def load(path: Path) -> Spec:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        msg = f"{path}: {e}"
        raise SpecError(msg) from e
    try:
        return parse(data)
    except SpecError as e:
        msg = f"{path}: {e}"
        raise SpecError(msg) from e


def _toml_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def skeleton(
    *,
    mpn: str,
    manufacturer: str,
    description: str = "",
    datasheet: str = "",
    source: str = "",
    lifecycle: str = "unknown",
    package_hint: str = "",
) -> str:
    """A spec to fill in: the distributor data set, package and pins as commented placeholders."""
    hint = f"  # distributor: {package_hint}" if package_hint else ""
    return f"""# Part spec for {mpn}: fill in [package] and [[pin]] from the datasheet (format: vibepcb/parts/spec.py).
# Every table needs the datasheet page its numbers come from. Dimensions: [min, max] or [min, nom, max] in mm.

[part]
mpn = {_toml_str(mpn)}
manufacturer = {_toml_str(manufacturer)}
description = {_toml_str(description)}
datasheet = {_toml_str(datasheet)}
source = {_toml_str(source)}
lifecycle = {_toml_str(lifecycle)}
kind = "ic"  # {" | ".join(KINDS)}

[package]{hint}
family = ""  # {" | ".join(FAMILIES)}
pins = 0
pitch = 0.0
span = [0.0, 0.0]  # lead tip to lead tip across the rows
body = {{ l = [0.0, 0.0], w = [0.0, 0.0], h = [0.0] }}
lead = {{ l = [0.0, 0.0], w = [0.0, 0.0] }}  # foot length, lead width
# exposed_pad = {{ l = [0.0, 0.0], w = [0.0, 0.0], number = "" }}
page = 0

# [[pin]] per pin; type: {" | ".join(PIN_TYPES)}
# [[pin]]
# number = "1"
# name = ""
# type = "passive"
# page = 0
"""
