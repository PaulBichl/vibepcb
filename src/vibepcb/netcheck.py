"""Deterministic netlist checks (no agent): the rules a script can decide from the circuit JSON.

Nets whose name carries a voltage (``+3V3``, ``5V``, ``VBUS_5V``, ``3.3V``) or that ``intent.toml`` gives one
have a known voltage. Checks (each finding cites a reference and a pin; ids are ``<rule>:<what>``):

- ``cap_rating``      a capacitor with a known voltage rating (``maxVoltageRating`` or ``16V`` in its part text)
                      on a net above ``netlist.cap_derating`` (``rules/sim-defaults.toml``, default 0.8) of it
- ``i2c_pullup``      an I2C net (``SDA`` / ``SCL`` in the name) with no pull-up resistor to a supply net;
                      ``i2c_pullup_multi``: more than one
- ``decoupling``      an IC power pin (VDD*, VCC*, VBAT, VIN, VOUT, ...) whose net has no capacitor to a
                      ground net; ``power_unconnected``: a power pin on no net
- ``fb_divider``      a feedback divider (FB / ADJ pin, resistor to the output net and to ground) whose implied
                      reference voltage differs from the regulator's ``vref`` in ``intent.toml`` by more than 3 %,
                      or lies outside ``netlist.vref_min`` .. ``vref_max`` when no ``vref`` is given
- ``floating``        an EN / RESET / BOOT / SHDN ... pin on no net, or on a net with only that pin
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb import schgen
from vibepcb.findings import Finding
from vibepcb.sim import parse_value

if TYPE_CHECKING:
    from vibepcb.intent import Intent
    from vibepcb.sim import Defaults

KINDS = {
    "simple_resistor": "resistor",
    "simple_capacitor": "capacitor",
    "simple_inductor": "inductor",
    "simple_chip": "chip",
}
VALUE_FIELDS = {
    "resistor": ("resistance", "display_resistance"),
    "capacitor": ("capacitance", "display_capacitance"),
    "inductor": ("inductance", "display_inductance"),
}
TEXT_FIELDS = ("name", "manufacturer_part_number", "display_value", "mpn", "description")
GND_NET = re.compile(r"^(GND|AGND|DGND|PGND|GNDA|GNDD|VSS|VSSA|EARTH)\d*$", re.IGNORECASE)
I2C_NET = re.compile(r"(^|[^A-Z0-9])(SDA|SCL)\d*([^A-Z]|$)", re.IGNORECASE)
POWER_PIN = re.compile(r"^(V?DD\w*|VCC\w*|AVDD\w*|DVDD\w*|VBAT|VIN|VOUT|VBUS|VSYS)$", re.IGNORECASE)
FB_PIN = re.compile(r"^(V?FB|ADJ)\d*$", re.IGNORECASE)
CONTROL_PIN = re.compile(
    r"^(EN|ENABLE|CE|SHDN|NSHDN|RUN|RESET|RST|NRST|NRESET|RESET_?N|RST_?N|BOOT\d?|NBOOT\d?|PWRON)$", re.IGNORECASE
)
REGULATOR_IN = {"VIN", "IN", "VI", "PVIN"}
REGULATOR_OUT = {"VOUT", "OUT", "VO", "FB", "VFB", "SW", "LX", "ADJ"}
REGULATOR_NAME = re.compile(
    r"\b(LDO|REGULATOR|BUCK|BOOST|AMS1117|LM1117|LM2596|LM317|AP2112|TLV7\d+|MCP17\d+|XC62\d+|MIC5\d+|TPS6\d+|TPS7\d+"
    r"|AP7\d+|RT9\d+|LD1117|ME6\d+)",
    re.IGNORECASE,
)
RATING = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*V\b")
VREF_TOLERANCE = 0.03
DEFAULT_DERATING = 0.8
DEFAULT_VREF_RANGE = (0.4, 1.3)


@dataclass(frozen=True)
class Part:
    ref: str
    kind: str  # resistor | capacitor | inductor | chip | other
    value: float | None
    text: str
    max_voltage: float | None
    ports: int


@dataclass(frozen=True)
class Pin:
    ref: str
    number: str
    name: str
    net: str | None


@dataclass
class Design:
    parts: dict[str, Part] = field(default_factory=dict)
    pins: list[Pin] = field(default_factory=list)

    def nets(self) -> dict[str, list[Pin]]:
        out: dict[str, list[Pin]] = {}
        for pin in self.pins:
            if pin.net:
                out.setdefault(pin.net, []).append(pin)
        return out

    def pins_of(self, ref: str) -> list[Pin]:
        return [p for p in self.pins if p.ref == ref]

    def values(self) -> dict[str, float]:
        """Reference -> value of the resistors, capacitors and inductors (for the deck comparison)."""
        return {r: p.value for r, p in self.parts.items() if p.value is not None}

    def kinds(self) -> dict[str, str]:
        return {r: p.kind for r, p in self.parts.items()}


def _value(comp: dict[str, Any], kind: str) -> float | None:
    for key in VALUE_FIELDS.get(kind, ()):
        raw = comp.get(key)
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            return float(raw)
        if isinstance(raw, str):
            try:
                return parse_value(raw)
            except ValueError:
                continue
    return None


def _rating(comp: dict[str, Any], text: str) -> float | None:
    raw = comp.get("max_voltage_rating")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    m = RATING.search(str(raw)) if raw else RATING.search(text)
    return float(m.group(1)) if m else None


def load_design(circuit: list[dict[str, Any]]) -> Design:
    """Parts and pins (with net names as the schematic gives them) from the tscircuit circuit JSON."""
    nets = schgen._nets(circuit)
    comps = {e["source_component_id"]: e for e in circuit if e.get("type") == "source_component"}
    ports: dict[str, list[dict[str, Any]]] = {}
    for e in circuit:
        if e.get("type") == "source_port" and schgen.INTERNAL_PORT not in str(e.get("name")):
            ports.setdefault(e["source_component_id"], []).append(e)
    design = Design()
    for cid, comp in comps.items():
        if not comp.get("name") or not ports.get(cid):
            continue
        ref, kind = str(comp["name"]), KINDS.get(str(comp.get("ftype")), "other")
        text = " ".join(str(comp[k]) for k in TEXT_FIELDS if comp.get(k))
        design.parts[ref] = Part(ref, kind, _value(comp, kind), text, _rating(comp, text), len(ports[cid]))
        for p in sorted(ports[cid], key=lambda p: (p.get("pin_number") or 0, str(p["name"]))):
            number = str(p.get("pin_number") or p["name"])
            design.pins.append(Pin(ref, number, str(p["name"]), nets.get(p["source_port_id"])))
    return design


def name_voltage(net: str) -> float | None:
    """Voltage in a net name: ``+3V3`` / ``V3V3`` / ``3V3_ANA`` -> 3.3, ``5V`` -> 5, ``+3.3V`` -> 3.3."""
    up = re.sub(r"(^|[^A-Z0-9])V(?=\d+V)", r"\1", net.upper())
    if m := re.search(r"(?<![A-Z0-9.])(\d+)V(\d+)(?![A-Z0-9])", up):
        return float(f"{m.group(1)}.{m.group(2)}")
    if m := re.search(r"(?<![A-Z0-9.])(\d+(?:\.\d+)?)V(?![A-Z0-9])", up):
        return float(m.group(1))
    return None


def net_voltage(net: str | None, intent: Intent) -> float | None:
    if net is None:
        return None
    voltage = intent.voltage_of(net)
    return voltage if voltage is not None else name_voltage(net)


def is_ground(net: str | None) -> bool:
    return net is not None and bool(GND_NET.match(net))


def _chips(design: Design) -> list[Part]:
    return [p for p in design.parts.values() if p.kind == "chip" and p.ports >= 3]


def find_regulators(design: Design) -> list[str]:
    """References of the voltage regulators: a chip with an input and an output / feedback / switch pin,
    or one whose name / MPN is a known regulator family."""
    out = []
    for chip in _chips(design):
        names = {p.name.upper() for p in design.pins_of(chip.ref)}
        if (names & REGULATOR_IN and names & REGULATOR_OUT) or REGULATOR_NAME.search(chip.text):
            out.append(chip.ref)
    return sorted(out)


def _cap_to_ground(design: Design, net: str, nets: dict[str, list[Pin]]) -> bool:
    for pin in nets.get(net, []):
        if design.parts[pin.ref].kind != "capacitor":
            continue
        others = [p for p in design.pins_of(pin.ref) if p is not pin]
        if any(is_ground(o.net) for o in others):
            return True
    return False


def check_cap_ratings(design: Design, intent: Intent, derating: float) -> list[Finding]:
    out = []
    for part in design.parts.values():
        if part.kind != "capacitor" or part.max_voltage is None:
            continue
        for pin in design.pins_of(part.ref):
            voltage = net_voltage(pin.net, intent)
            if voltage is not None and voltage > derating * part.max_voltage:
                out.append(
                    Finding(
                        f"cap_rating:{part.ref}",
                        part.ref,
                        pin.number,
                        f"rated {part.max_voltage:g} V but net {pin.net} is {voltage:g} V "
                        f"(derating {derating:g}: at most {derating * part.max_voltage:g} V)",
                    )
                )
                break
    return out


def check_i2c(design: Design, intent: Intent) -> list[Finding]:
    out = []
    for net, pins in sorted(design.nets().items()):
        if not I2C_NET.search(net):
            continue
        pullups = []
        for pin in pins:
            if design.parts[pin.ref].kind != "resistor":
                continue
            others = [p for p in design.pins_of(pin.ref) if p is not pin]
            if len(others) == 1 and net_voltage(others[0].net, intent) is not None:
                pullups.append(pin)
        anchor = next((p for p in pins if design.parts[p.ref].kind == "chip"), pins[0])
        if not pullups:
            out.append(
                Finding(
                    f"i2c_pullup:{net}",
                    anchor.ref,
                    anchor.name,
                    f"I2C net {net} has no pull-up resistor to a supply net",
                )
            )
        elif len(pullups) > 1:
            refs = ", ".join(p.ref for p in pullups)
            out.append(
                Finding(
                    f"i2c_pullup_multi:{net}",
                    pullups[0].ref,
                    pullups[0].number,
                    f"I2C net {net} has {len(pullups)} pull-ups ({refs}); one per bus",
                )
            )
    return out


def check_decoupling(design: Design, intent: Intent) -> list[Finding]:
    del intent
    nets, out = design.nets(), []
    for chip in _chips(design):
        for pin in design.pins_of(chip.ref):
            if not POWER_PIN.match(pin.name) or is_ground(pin.net):
                continue
            if pin.net is None:
                out.append(
                    Finding(
                        f"power_unconnected:{pin.ref}:{pin.number}",
                        pin.ref,
                        pin.name,
                        f"power pin {pin.name} is on no net",
                    )
                )
            elif not _cap_to_ground(design, pin.net, nets):
                out.append(
                    Finding(
                        f"decoupling:{pin.ref}:{pin.number}",
                        pin.ref,
                        pin.name,
                        f"power pin {pin.name} (net {pin.net}) has no capacitor to ground on its net",
                    )
                )
    return out


def check_feedback(design: Design, intent: Intent, vref_range: tuple[float, float]) -> list[Finding]:
    nets, out = design.nets(), []
    for chip in _chips(design):
        for pin in design.pins_of(chip.ref):
            if not FB_PIN.match(pin.name) or pin.net is None:
                continue
            top, bottom = _divider(design, pin, nets)
            if top is None or bottom is None or top[1].value is None or bottom.value is None:
                continue
            vout = net_voltage(top[0], intent)
            if vout is None or bottom.value <= 0:
                continue
            implied = vout / (1 + top[1].value / bottom.value)
            rail = next((r for r in intent.rails.values() if r.regulator == chip.ref and r.vref), None)
            cite = f"divider {top[1].ref} {top[1].value:g} / {bottom.ref} {bottom.value:g} ohm to {top[0]} ({vout:g} V) implies Vref {implied:.3f} V"
            if rail and rail.vref and abs(implied - rail.vref) > VREF_TOLERANCE * rail.vref:
                out.append(
                    Finding(f"fb_divider:{chip.ref}", chip.ref, pin.name, f"{cite}; intent.toml says {rail.vref:g} V")
                )
            elif not rail and not vref_range[0] <= implied <= vref_range[1]:
                out.append(
                    Finding(
                        f"fb_divider:{chip.ref}",
                        chip.ref,
                        pin.name,
                        f"{cite}, outside the usual {vref_range[0]:g}..{vref_range[1]:g} V (set vref in intent.toml to check it)",
                    )
                )
    return out


def _divider(design: Design, fb: Pin, nets: dict[str, list[Pin]]) -> tuple[tuple[str, Part] | None, Part | None]:
    """((output net, top resistor), bottom resistor) of the divider on a feedback pin's net."""
    top: tuple[str, Part] | None = None
    bottom: Part | None = None
    for pin in nets.get(fb.net or "", []):
        part = design.parts[pin.ref]
        if part.kind != "resistor":
            continue
        others = [p for p in design.pins_of(pin.ref) if p is not pin]
        if len(others) != 1 or others[0].net is None:
            continue
        if is_ground(others[0].net):
            bottom = part
        elif top is None:
            top = (others[0].net, part)
    return top, bottom


def check_control_pins(design: Design) -> list[Finding]:
    nets, out = design.nets(), []
    for chip in _chips(design):
        for pin in design.pins_of(chip.ref):
            if CONTROL_PIN.match(pin.name) and (pin.net is None or len(nets[pin.net]) == 1):
                out.append(
                    Finding(
                        f"floating:{pin.ref}:{pin.number}",
                        pin.ref,
                        pin.name,
                        f"control pin {pin.name} is floating (nothing else on its net): tie it high / low or to a driver",
                    )
                )
    return out


def run(design: Design, intent: Intent, defaults: Defaults) -> list[Finding]:
    """All deterministic findings of a design."""
    derating = defaults.netlist.get("cap_derating", DEFAULT_DERATING)
    vref_range = (
        defaults.netlist.get("vref_min", DEFAULT_VREF_RANGE[0]),
        defaults.netlist.get("vref_max", DEFAULT_VREF_RANGE[1]),
    )
    return [
        *check_cap_ratings(design, intent, derating),
        *check_i2c(design, intent),
        *check_decoupling(design, intent),
        *check_feedback(design, intent, vref_range),
        *check_control_pins(design),
    ]
