"""Bring-up sheet and firmware pin header, generated from the finished board.

``collect`` reads the KiCad project (pad nets, references, power symbols) and, for the MCU pin
names, tscircuit's circuit JSON. ``render_sheet`` writes ``<board>-bringup.md`` (rails with their
expected voltage, the test points of each, a power-on checklist, the regulators);
``render_header`` writes ``<board>-pins.h`` (net name -> MCU pin of the largest IC with GPIO-like
pin names). Both are deterministic: sorted, no timestamps.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb import testpoints

if TYPE_CHECKING:
    from pathlib import Path

    from vibepcb.fixes import KicadProject

VOLT_TOLERANCE = "5 %"
# "3V3", "+3V3", "V3V3", "1V8_A", "12V", "5V" -> volts; "+5.0V"
_VOLTS = re.compile(r"^([+-]?)V?(\d+)V(\d*)(?:_[A-Z0-9]{1,3})?$", re.IGNORECASE)
_VOLTS_DECIMAL = re.compile(r"^([+-]?)(\d+\.\d+)V$", re.IGNORECASE)
NAMED_VOLTS = {"VBUS": 5.0, "VUSB": 5.0}
REGULATOR_WORDS = re.compile(
    r"ldo|regulator|buck|boost|ams1117|lm1117|lm2596|lm317|ap2112|ap7\d|xc6206|mic5\d|tps[67]\d|mcp17|rt9\d|ld1117|l78|78\d\d|"
    r"tlv7\d|ncp\d|sy8\d|mp1\d|ht7",
    re.IGNORECASE,
)
MAX_REGULATOR_PADS = 8
GPIO_NAME = re.compile(
    r"^(P[A-K]\d{1,2}|GPIO\d+|IO\d+|GP\d+|P\d+[._]\d+|PIO\d+|RA\d|RB\d|RC\d|D\d+|A\d+)(?![A-Za-z0-9])"
)
MIN_GPIO_PINS = 4


@dataclass(frozen=True)
class Rail:
    net: str
    volts: float | None
    access: tuple[str, ...]  # test points / connector pads that reach it


@dataclass(frozen=True)
class Regulator:
    ref: str
    value: str
    rails: tuple[str, ...]  # power nets on its pads, input (highest voltage) first


@dataclass(frozen=True)
class McuPin:
    net: str
    pin: str  # the pin name as the datasheet writes it (PA5, GPIO4)
    pad: str


@dataclass
class BringUp:
    rails: list[Rail] = field(default_factory=list)
    grounds: list[Rail] = field(default_factory=list)
    regulators: list[Regulator] = field(default_factory=list)
    signal_points: dict[str, list[str]] = field(default_factory=dict)  # other nets with a test point
    mcu_ref: str = ""
    mcu_value: str = ""
    mcu_pins: list[McuPin] = field(default_factory=list)


def expected_volts(net: str) -> float | None:
    """Voltage a rail name promises (``+3V3`` -> 3.3, ``VBUS`` -> 5.0), None when the name doesn't say."""
    name = net.strip()
    if (m := _VOLTS.match(name)) and m[2]:
        volts = float(f"{m[2]}.{m[3]}") if m[3] else float(m[2])
    elif m := _VOLTS_DECIMAL.match(name):
        volts = float(m[2])
    elif name.upper() in NAMED_VOLTS:
        return NAMED_VOLTS[name.upper()]
    else:
        return None
    return -volts if m[1] == "-" else volts


def _rail_order(rail: Rail) -> tuple[int, float, str]:
    """Highest known voltage first (the input of a chain is checked before its outputs), unknown last."""
    return (rail.volts is None, -(rail.volts or 0.0), rail.net)


def port_names(circuit: Path | None) -> dict[tuple[str, str], str]:
    """(reference, pad) -> the port (pin) name tscircuit gave it, from the circuit JSON."""
    if circuit is None:
        return {}
    elements = json.loads(circuit.read_text(encoding="utf-8"))
    refs = {e["source_component_id"]: str(e.get("name")) for e in elements if e.get("type") == "source_component"}
    names: dict[tuple[str, str], str] = {}
    for e in elements:
        if e.get("type") != "source_port" or e.get("source_component_id") not in refs or not e.get("name"):
            continue
        ref = refs[e["source_component_id"]]
        keys = {str(e["pin_number"])} if e.get("pin_number") is not None else set()
        keys |= {str(h) for h in e.get("port_hints", []) if str(h).isalnum() and not str(h).startswith("pin")}
        for key in keys:
            names.setdefault((ref, key), str(e["name"]))
    return names


def _regulators(board_parts: list[testpoints.Part], supplies: set[str]) -> list[Regulator]:
    out = []
    for part in board_parts:
        if not part.ref.startswith("U") or len(part.pads) > MAX_REGULATOR_PADS:
            continue
        rails = sorted(
            {net for _, net in part.pads if net in supplies}, key=lambda n: _rail_order(Rail(n, expected_volts(n), ()))
        )
        if len(rails) >= 2 or (rails and REGULATOR_WORDS.search(part.value)):
            out.append(Regulator(part.ref, part.value, tuple(rails)))
    return out


def _mcu(board_parts: list[testpoints.Part], names: dict[tuple[str, str], str]) -> tuple[str, str, list[McuPin]]:
    best: tuple[int, str, str, list[McuPin]] | None = None
    for part in board_parts:
        if not part.ref.startswith("U"):
            continue
        pins = []
        for pad, net in part.pads:
            if (name := names.get((part.ref, pad))) and (m := GPIO_NAME.match(name)):
                pins.append(McuPin(net, m[1], pad))
        if len(pins) >= MIN_GPIO_PINS and (best is None or len(part.pads) > best[0]):
            best = (len(part.pads), part.ref, part.value, pins)
    return (best[1], best[2], best[3]) if best else ("", "", [])


def collect(project: KicadProject, circuit: Path | None = None) -> BringUp:
    board_parts = testpoints.parts(project.pcb)
    access = testpoints.access_points(board_parts)
    supplies, grounds = testpoints.rail_nets(board_parts, project.sch)
    data = BringUp()
    data.rails = sorted((Rail(n, expected_volts(n), tuple(access.get(n, ()))) for n in supplies), key=_rail_order)
    data.grounds = [Rail(n, 0.0, tuple(access.get(n, ()))) for n in grounds]
    data.regulators = _regulators(board_parts, set(supplies))
    special = set(supplies) | set(grounds)
    data.signal_points = {
        n: refs for n, refs in sorted(access.items()) if n not in special and any(r.startswith("TP") for r in refs)
    }
    data.mcu_ref, data.mcu_value, data.mcu_pins = _mcu(board_parts, port_names(circuit))
    return data


def _volts(volts: float | None) -> str:
    return "see schematic" if volts is None else f"{volts:g} V"


def render_sheet(board: str, data: BringUp) -> str:
    lines = [
        f"# {board} bring-up sheet",
        "",
        "Generated by `pcb bringup` from the finished board (nets, references, power symbols) - rebuild, don't edit.",
        "Expected voltages come from the rail names; check them against the regulator datasheets.",
        "",
        "## Rails",
        "",
    ]
    if data.rails:
        lines += ["| Rail | Expected | Test points | Fed by |", "|---|---|---|---|"]
        for rail in data.rails:
            feeds = [r.ref for r in data.regulators if rail.net in r.rails[1:]]
            where = ", ".join(rail.access) or "none - probe a pad"
            lines.append(f"| {rail.net} | {_volts(rail.volts)} | {where} | {', '.join(feeds) or '-'} |")
    else:
        lines.append("No supply rails found by name (VCC, VDD, +3V3, 5V, VBUS, VIN, VBAT, ...) or power symbol.")
    lines += [
        "",
        "Ground reference: "
        + (", ".join(f"{g.net} at {', '.join(g.access) or 'no test point'}" for g in data.grounds) or "none found"),
    ]
    lines += ["", "## Regulators", ""]
    if data.regulators:
        for reg in data.regulators:
            lines.append(f"- {reg.ref} {reg.value}: input {reg.rails[0]}, output {', '.join(reg.rails[1:]) or '?'}")
    else:
        lines.append("None found (a part with 2+ supply nets on at most 8 pads, or a regulator-like value).")
    lines += ["", "## Power-on checklist", ""]
    lines += [
        "1. Visual check: orientation of ICs, diodes and polarised caps, no solder bridges, no missing parts.",
        "2. Continuity, supply off: each rail to GND must not beep (a few ohms or lower is a short).",
        "3. Bench supply current-limited (start at 20-50 mA, input voltage per the schematic), then switch on.",
        "4. Watch the supply current: it should settle quickly; any limit hit means switch off and look for heat.",
    ]
    step = 5
    gnd = ", ".join(data.grounds[0].access) if data.grounds and data.grounds[0].access else "any GND pad"
    for rail in data.rails:
        where = " or ".join(rail.access) or "a pad of the net"
        lines.append(
            f"{step}. Measure {rail.net} at {where} against GND ({gnd}): expect {_volts(rail.volts)}"
            + (f" +/- {VOLT_TOLERANCE}." if rail.volts is not None else ".")
        )
        step += 1
    lines += [
        f"{step}. Raise the current limit to the expected consumption and recheck the rails under load.",
        f"{step + 1}. Then signals: clocks, reset, programming interface, indicator LEDs - in that order.",
    ]
    if data.signal_points:
        lines += ["", "## Signal test points", ""]
        lines += [f"- {net}: {', '.join(refs)}" for net, refs in data.signal_points.items()]
    if data.mcu_ref:
        lines += ["", f"MCU {data.mcu_ref} ({data.mcu_value}): pin assignments in `{board}-pins.h`."]
    return "\n".join(lines) + "\n"


def _identifier(net: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9]+", "_", net).strip("_").upper()
    return f"N_{ident}" if ident[:1].isdigit() else ident


def _pin_token(pin: str) -> str:
    """GPIO4 / IO4 / GP4 -> 4 (a number the SDK takes), anything else (PA5, P0.13) as the identifier."""
    if m := re.fullmatch(r"(?:GPIO|IO|GP)(\d+)", pin):
        return m[1]
    return re.sub(r"[^A-Za-z0-9]+", "_", pin)


def render_header(board: str, data: BringUp) -> str:
    """C header with one ``#define <NET>_PIN <pin>`` per named, connected MCU pin; empty when no MCU was found."""
    if not data.mcu_ref:
        return ""
    special = {r.net for r in data.rails} | {g.net for g in data.grounds}
    pins = [p for p in data.mcu_pins if p.net and p.net not in special and not p.net.startswith("Net-")]
    guard = f"{_identifier(board)}_PINS_H"
    lines = [
        f"/* {board}: MCU pin assignments of {data.mcu_ref} ({data.mcu_value}), generated by `pcb bringup`. */",
        f"#ifndef {guard}",
        f"#define {guard}",
        "",
    ]
    for p in sorted(pins, key=lambda p: (p.net, p.pin)):
        base = f"{_identifier(p.net)}_PIN"
        name = base if sum(1 for q in pins if q.net == p.net) == 1 else f"{base}_{_identifier(p.pin)}"
        lines.append(f"#define {name:<24} {_pin_token(p.pin):<8} /* {data.mcu_ref} pin {p.pad} ({p.pin}) */")
    if not pins:
        lines.append("/* no GPIO pin is connected to a named net */")
    lines += ["", f"#endif /* {guard} */"]
    return "\n".join(lines) + "\n"
