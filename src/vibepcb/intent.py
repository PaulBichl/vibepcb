"""The assumption list: ``boards/<board>/intent.toml``, written by the agent from the prompt.

It records what the board is supposed to do, so ``check`` can verify the design against it (and the human
only confirms or corrects it). Every key is optional; ``pcb new`` scaffolds an empty file::

    description = "USB-powered sensor board with an ESP32"
    assumptions = ["5 V in from USB, 500 mA", "3V3 rail for MCU and sensor"]   # free text, confirmed by the human

    [supply]                       # where the power comes in
    net = "VBUS"
    voltage = 5.0                  # V
    current = 0.5                  # A the source can give

    [rail."+3V3"]                  # one per power rail, keyed by net name (quote names with + or .)
    voltage = 3.3                  # V, nominal
    tolerance_pct = 5              # optional, default 5: the pass range of vmin_/vmax_ measures
    current = 0.5                  # A budget (peak); the pass limit of an imax_ measure
    regulator = "U1"               # optional: the reference that makes the rail
    vref = 0.8                     # optional: the regulator's feedback reference (datasheet), checks the divider

Sim pass ranges from here (see ``vibepcb.sim``): a ``.measure`` named ``vmax_3v3`` / ``vmin_3v3`` / ``imax_3v3``
(rail name lower-cased, runs of other characters replaced by ``_``) is limited by the rail's voltage
+/- tolerance / the current budget, with the source ``intent:rail +3V3``. Net voltages also feed the netlist
checks (``vibepcb.netcheck``) for nets whose name carries no voltage.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb import paths
from vibepcb.config import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

INTENT_NAME = "intent.toml"
DEFAULT_TOLERANCE_PCT = 5.0
RAIL_KEYS = {"voltage", "tolerance_pct", "current", "regulator", "vref"}
SUPPLY_KEYS = {"net", "voltage", "current"}
TOP_KEYS = {"description", "assumptions", "supply", "rail"}
TEMPLATE = (
    "# What the board is supposed to do (the agent derives it from the prompt, the human confirms it).\n"
    "# Format: src/vibepcb/intent.py\n"
    'description = ""\n'
    "assumptions = []\n"
)


class IntentError(ConfigError):
    """intent.toml is malformed."""


@dataclass(frozen=True)
class Rail:
    net: str
    voltage: float | None = None
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT
    current: float | None = None
    regulator: str | None = None
    vref: float | None = None

    @property
    def key(self) -> str:
        """The name measures use: ``+3V3`` -> ``3v3``."""
        return measure_key(self.net)


@dataclass(frozen=True)
class Intent:
    description: str = ""
    assumptions: list[str] = field(default_factory=list)
    supply: Rail | None = None
    rails: dict[str, Rail] = field(default_factory=dict)

    def voltage_of(self, net: str) -> float | None:
        rail = self.rails.get(net)
        if rail and rail.voltage is not None:
            return rail.voltage
        if self.supply and self.supply.net == net:
            return self.supply.voltage
        return None

    def rail_by_key(self, key: str) -> Rail | None:
        return next((r for r in self.rails.values() if r.key == key), None)


def measure_key(net: str) -> str:
    """``+3V3`` -> ``3v3``, ``VBUS_5V`` -> ``vbus_5v``: a net name as part of a measure name."""
    return re.sub(r"[^a-z0-9]+", "_", net.lower()).strip("_")


def intent_path(board: str) -> Path:
    return paths.board_source(board).parent / INTENT_NAME


def _number(table: dict[str, Any], key: str, where: str) -> float | None:
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{where}: {key} must be a number, got {value!r}"
        raise IntentError(msg)
    return float(value)


def _rail(net: str, table: Any, keys: set[str], where: str) -> Rail:
    if not isinstance(table, dict):
        msg = f"{where}: must be a table"
        raise IntentError(msg)
    if unknown := set(table) - keys:
        msg = f"{where}: unknown key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(keys))})"
        raise IntentError(msg)
    tolerance = _number(table, "tolerance_pct", where)
    return Rail(
        net=net,
        voltage=_number(table, "voltage", where),
        tolerance_pct=DEFAULT_TOLERANCE_PCT if tolerance is None else tolerance,
        current=_number(table, "current", where),
        regulator=table.get("regulator"),
        vref=_number(table, "vref", where),
    )


def parse(data: dict[str, Any], where: str = INTENT_NAME) -> Intent:
    """The intent from parsed TOML; unknown keys are errors (a typo must not silently drop a requirement)."""
    if unknown := set(data) - TOP_KEYS:
        msg = f"{where}: unknown key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(TOP_KEYS))})"
        raise IntentError(msg)
    assumptions = data.get("assumptions", [])
    if not isinstance(assumptions, list) or not all(isinstance(a, str) for a in assumptions):
        msg = f"{where}: assumptions must be a list of strings"
        raise IntentError(msg)
    supply = None
    if "supply" in data:
        table = data["supply"]
        net = str(table.get("net", "")) if isinstance(table, dict) else ""
        supply = _rail(net, table, SUPPLY_KEYS, f"{where} [supply]")
    rails = {net: _rail(net, table, RAIL_KEYS, f"{where} [rail.{net}]") for net, table in data.get("rail", {}).items()}
    return Intent(str(data.get("description", "")), list(assumptions), supply, rails)


def load(board: str) -> Intent:
    """The board's intent (empty without a file)."""
    path = intent_path(board)
    if not path.exists():
        return Intent()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path.name}: {exc}"
        raise IntentError(msg) from exc
    return parse(data, path.name)
