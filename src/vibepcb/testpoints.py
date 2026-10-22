"""Test points: which nets of a board have one, and the house rule that every rail and GND needs one.

Configured per board in ``vibepcb.toml``::

    [testpoints]
    required = ["power", "gnd", "RESET", "UART_*"]  # "power" / "gnd" = the rail / ground nets found by name
                                                    #   (and the nets of power symbols); anything else is a
                                                    #   net-name pattern (fnmatch: * ?). Default: power + gnd.
    ignore = ["VBUS_RAW"]                           # nets that need none (patterns)
    severity = "warning"                            # "warning" (default) or "error" (fails `check`)

``required = []`` switches the rule off. A net has a test point when a test-point footprint
(``<testpoint>``, reference TP...) or a connector pad (reference J.../P.../CN...: a probe or a
header pin reaches the net there) sits on it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vibepcb import sexpr

if TYPE_CHECKING:
    from vibepcb.sexpr import Node

SEVERITIES = ("warning", "error")
GROUPS = ("power", "gnd")
DEFAULT_REQUIRED = list(GROUPS)

GROUND = re.compile(r"^(?:[ADPS]?GND[A-Z0-9_]*|VSS[A-Z0-9_]*|GROUND|0V)$", re.IGNORECASE)
# +3V3, V3V3, 3V3_A, 5V, 12V, +5.0V, VCC, VDD, VDDA, AVDD, DVCC, VBUS, VIN, VBAT, VSYS, VOUT, V_IN, +BATT ...
POWER = re.compile(
    r"^[+-]?(?:\d+(?:\.\d+)?V\d*|V\d+V\d*"
    r"|V_?(?:CC|DD|BUS|IN|BATT?|SYS|OUT|EE|REG|DC|USB)[A-Z]{0,2}\d*|[AD]V(?:CC|DD)[A-Z]{0,2}\d*|BATT?)"
    r"(?:_[A-Z0-9]{1,3})?$",
    re.IGNORECASE,
)
TEST_REF = re.compile(r"^TP\d*")
CONNECTOR_REF = re.compile(r"^(?:J|P|CN|X)\d")


@dataclass(frozen=True)
class Part:
    """A footprint as the checks see it: reference, footprint name, value and (pad name, net) pairs."""

    ref: str
    footprint: str
    value: str
    pads: tuple[tuple[str, str], ...]


def is_ground(net: str) -> bool:
    return bool(GROUND.match(net))


def is_power(net: str) -> bool:
    return bool(POWER.match(net)) and not is_ground(net)


def pad_net(pad: list[Node]) -> str:
    """The net name of a pad, whether written ``(net 3 "GND")`` or ``(net "GND")``."""
    net = sexpr.child(pad, "net")
    if net is None or len(net) < 2 or not isinstance(net[-1], str):
        return ""
    return str(net[-1])


def _ref_key(ref: str) -> tuple[str, int, str]:
    m = re.match(r"^([A-Za-z#]+)(\d*)(.*)$", ref)
    return (m[1], int(m[2] or 0), m[3]) if m else (ref, 0, "")


def parts(pcb: list[Node]) -> list[Part]:
    """Every footprint of the board with a reference, sorted by reference."""
    out = []
    for fp in sexpr.children(pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference")
        if not ref:
            continue
        pads = tuple((str(p[1]), pad_net(p)) for p in sexpr.children(fp, "pad") if len(p) > 1)
        out.append(Part(str(ref), str(fp[1]) if len(fp) > 1 else "", sexpr.property_value(fp, "Value") or "", pads))
    return sorted(out, key=lambda p: _ref_key(p.ref))


def power_symbol_nets(sch: list[Node]) -> set[str]:
    """Net names of the power symbols (``#PWR..``) of the schematic."""
    nets = set()
    for symbol in sexpr.children(sch, "symbol"):
        ref = sexpr.property_value(symbol, "Reference") or ""
        value = sexpr.property_value(symbol, "Value")
        if ref.startswith("#PWR") and value:
            nets.add(str(value))
    return nets


def is_test_point(part: Part) -> bool:
    name = part.footprint.lower()
    return bool(TEST_REF.match(part.ref)) or "test_point" in name or "testpoint" in name


def is_connector(part: Part) -> bool:
    return bool(CONNECTOR_REF.match(part.ref))


def access_points(board_parts: list[Part]) -> dict[str, list[str]]:
    """net -> where a probe reaches it: test-point refs and connector pads (``J1.2``), in reference order."""
    access: dict[str, list[str]] = {}
    for part in board_parts:
        if is_test_point(part):
            for _, net in part.pads:
                if net:
                    access.setdefault(net, []).append(part.ref)
        elif is_connector(part):
            for pad, net in part.pads:
                if net:
                    access.setdefault(net, []).append(f"{part.ref}.{pad}")
    return access


def rail_nets(board_parts: list[Part], sch: list[Node] | None = None) -> tuple[list[str], list[str]]:
    """(supply nets, ground nets) of the board: by name, plus the nets of power symbols."""
    nets = {net for part in board_parts for _, net in part.pads if net}
    symbol_nets = power_symbol_nets(sch) if sch is not None else set()
    grounds = sorted(n for n in nets if is_ground(n))
    supplies = sorted(n for n in nets if (is_power(n) or n in symbol_nets) and n not in grounds)
    return supplies, grounds


def settings(section: dict[str, Any] | None) -> tuple[list[str], list[str], str]:
    """(required, ignore, severity) of the ``[testpoints]`` config section, defaults filled in."""
    section = section or {}
    return (
        list(section.get("required", DEFAULT_REQUIRED)),
        list(section.get("ignore", [])),
        str(section.get("severity", "warning")),
    )


def validate(section: dict[str, Any], where: str) -> None:
    """Raise ``ValueError`` (the config module wraps it) when ``[testpoints]`` is malformed."""
    if bad := set(section) - {"required", "ignore", "severity"}:
        msg = f"{where}: [testpoints]: unknown key(s) {sorted(bad)} (known: ignore, required, severity)"
        raise ValueError(msg)
    for key in ("required", "ignore"):
        value = section.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            msg = f'{where}: [testpoints] {key} must be a list of strings (e.g. ["power", "gnd", "RESET"])'
            raise ValueError(msg)
    if section.get("severity", "warning") not in SEVERITIES:
        msg = f'{where}: [testpoints] severity must be "warning" or "error"'
        raise ValueError(msg)


def missing(
    board_parts: list[Part], sch: list[Node] | None = None, section: dict[str, Any] | None = None
) -> tuple[list[str], str]:
    """(nets that need a test point and have none, in name order; the configured severity)."""
    required, ignore, severity = settings(section)
    supplies, grounds = rail_nets(board_parts, sch)
    nets = {net for part in board_parts for _, net in part.pads if net}
    wanted: set[str] = set()
    if "power" in required:
        wanted |= set(supplies)
    if "gnd" in required:
        wanted |= set(grounds)
    for pattern in required:
        if pattern not in GROUPS:
            wanted |= {n for n in nets if fnmatch.fnmatchcase(n, pattern)}
    wanted = {n for n in wanted if not any(fnmatch.fnmatchcase(n, p) for p in ignore)}
    access = access_points(board_parts)
    return sorted(n for n in wanted if n not in access), severity


def findings(
    board_parts: list[Part], sch: list[Node] | None = None, section: dict[str, Any] | None = None
) -> tuple[list[str], str]:
    """(``check`` summary lines for the missing test points, the configured severity)."""
    nets, severity = missing(board_parts, sch, section)
    hint = "add a <testpoint> on it (or a connector pin), labelled on the silkscreen"
    return [f"  [{severity}] missing_testpoint: {net} - {hint}" for net in nets], severity
