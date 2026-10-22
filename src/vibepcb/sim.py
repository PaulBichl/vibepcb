"""SPICE decks in ``check``: ``boards/<board>/sim/*.cir``, run by ``ngspice -b``, judged against pass ranges.

A deck proves something with ``.measure`` lines; what counts as a pass is declared in comment lines of
the deck (``*`` is a SPICE comment)::

    * range: vout_max 3.2 3.4 source=datasheet:TLV75533 p.5     # measure, min, max ("-" = open), where from
    * covers: powerup U1                                         # which required sim this deck is (kind, parts)
    R5 vin fb 100k                                               # passives with a board reference are compared
    .measure tran vout_max MAX v(out)

Where a measure's pass range comes from (first match wins; the agent must not set its own pass mark):

1. ``* range:`` with ``source=datasheet...``: the datasheet of the receiving part (kind ``datasheet``)
2. the board's ``intent.toml``: measures ``vmax_<rail>``, ``vmin_<rail>``, ``imax_<rail>`` (kind ``intent``)
3. ``house`` defaults, ``rules/sim-defaults.toml`` ``[limits]`` (measure name, or a name ending ``_<key>``)
4. a ``* range:`` line without ``source=`` is ``assumed``; a measure without any range is ``assumed`` too
   (its value is shown, nothing to fail)

Any other ``source=`` text (``source=app note``) is a sourced range (kind ``sourced``).

Deck vs board: every resistor / capacitor / inductor of the deck whose name is also a reference on the
board (``R5``, ``C3``) must have the same value (SPICE suffixes: ``k meg u n p m f g t``), and a deck
passive named like a reference (``R9``) that is not on the board is stale. Either invalidates the deck
(it is not run; ``stale``). ``covers`` lines bind a deck to the required sims of ``sim-defaults.toml``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb import paths, proc, rules
from vibepcb.config import ConfigError
from vibepcb.intent import Intent, measure_key

if TYPE_CHECKING:
    from pathlib import Path

DEFAULTS_NAME = "sim-defaults.toml"
SIM_TIMEOUT_S = 120
VALUE_TOLERANCE = 1e-6  # relative: 4.7k vs 4700
SPICE_SCALE = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}
NUMBER = re.compile(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)([a-z]*)$", re.IGNORECASE)
PASSIVE_NAME = re.compile(r"^([RCL])\d+$", re.IGNORECASE)
MEASURE_LINE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*=\s*([-+]?[\d.]+(?:e[-+]?\d+)?)", re.IGNORECASE)
MEASURE_FAILED = re.compile(r"^\s*\.meas\w*\s+\w+\s+(\w+)\b.*\bfailed!?", re.IGNORECASE)
MEASURE_FAILED_VALUE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*=\s*failed", re.IGNORECASE)
KIND_BY_LETTER = {"R": "resistor", "C": "capacitor", "L": "inductor"}
SOURCE_KINDS = ("datasheet", "house", "intent")


class SimError(ConfigError):
    """A deck or the sim defaults are malformed, or ngspice failed."""


def parse_value(token: str) -> float:
    """A SPICE number: ``4.7k`` -> 4700, ``100nF`` -> 1e-7 (unit letters after the suffix are ignored),
    ``1meg`` -> 1e6. Raises :class:`ValueError` for anything else (``{rload}``, ``PULSE(...)``)."""
    m = NUMBER.match(token.strip())
    if not m:
        msg = f"not a SPICE number: {token!r}"
        raise ValueError(msg)
    number, tail = float(m.group(1)), m.group(2).lower()
    if tail.startswith("meg"):
        return number * 1e6
    if tail.startswith("mil"):
        return number * 25.4e-6
    return number * SPICE_SCALE.get(tail[:1], 1.0) if tail else number


@dataclass(frozen=True)
class Range:
    """Pass range of a measure and where it comes from."""

    lo: float | None
    hi: float | None
    source: str | None
    kind: str  # datasheet | house | intent | sourced | assumed

    def contains(self, value: float) -> bool:
        return (self.lo is None or value >= self.lo) and (self.hi is None or value <= self.hi)

    def text(self) -> str:
        lo = "-inf" if self.lo is None else f"{self.lo:g}"
        hi = "inf" if self.hi is None else f"{self.hi:g}"
        return f"[{lo}, {hi}] ({self.kind}: {self.source})" if self.source else f"[{lo}, {hi}] ({self.kind})"


@dataclass(frozen=True)
class Element:
    name: str
    value: float


@dataclass
class Deck:
    path: Path
    ranges: dict[str, Range] = field(default_factory=dict)
    covers: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)  # (kind, refs)
    elements: list[Element] = field(default_factory=list)
    measures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Defaults:
    """``rules/sim-defaults.toml``: the house policy."""

    limits: dict[str, tuple[float | None, float | None]]
    required: dict[str, tuple[str, ...]]  # circuit type -> deck kinds every part of it needs
    netlist: dict[str, float]
    source: str = DEFAULTS_NAME


@dataclass
class Measured:
    name: str
    value: float | None
    range: Range | None
    status: str  # ok | fail | assumed | error

    def line(self) -> str:
        shown = "failed to measure" if self.value is None else f"{self.value:.6g}"
        rng = f" range {self.range.text()}" if self.range else " (no pass range)"
        return f"{self.name} = {shown}{rng}"


@dataclass
class DeckResult:
    deck: Deck
    status: str  # ok | fail | stale | error | skipped
    measures: list[Measured] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # why stale / error / skipped

    @property
    def name(self) -> str:
        return self.deck.path.name


def _bound(token: str, where: str) -> float | None:
    if token in {"-", "inf", "-inf"}:
        return None
    try:
        return parse_value(token)
    except ValueError as exc:
        msg = f"{where}: bad range bound {token!r}"
        raise SimError(msg) from exc


def classify(source: str | None) -> str:
    if not source:
        return "assumed"
    head = source.strip().lower()
    return next((k for k in SOURCE_KINDS if head.startswith(k)), "sourced")


def parse_deck(path: Path, text: str | None = None) -> Deck:
    """Ranges, covers, passives and measures of a deck (the first line is SPICE's title and ignored)."""
    deck = Deck(path)
    params: dict[str, float] = {}
    lines = (path.read_text(encoding="utf-8") if text is None else text).splitlines()
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        where = f"{path.name}:{number}"
        if number == 1 or not line:
            continue
        if line.startswith("*"):
            body = line.lstrip("*").strip()
            key, _, rest = body.partition(":")
            if key.strip().lower() == "range":
                rest, _, source = rest.partition("source=")
                tokens = rest.split()
                if len(tokens) != 3:
                    msg = f"{where}: `* range:` needs <measure> <min> <max> [source=...], got {body!r}"
                    raise SimError(msg)
                src = source.strip() or None
                deck.ranges[tokens[0].lower()] = Range(
                    _bound(tokens[1], where), _bound(tokens[2], where), src, classify(src)
                )
            elif key.strip().lower() == "covers":
                tokens = rest.split()
                if not tokens:
                    msg = f"{where}: `* covers:` needs a kind (and parts), e.g. `* covers: powerup U1`"
                    raise SimError(msg)
                deck.covers.append((tokens[0].lower(), tuple(tokens[1:])))
            continue
        tokens = line.split()
        head = tokens[0].lower()
        if head == ".param":
            for assignment in tokens[1:]:
                name, _, value = assignment.partition("=")
                try:
                    params[name.lower()] = parse_value(value)
                except ValueError:
                    continue
        elif head in {".meas", ".measure"} and len(tokens) >= 3:
            deck.measures.append(tokens[2].lower())
        elif PASSIVE_NAME.match(tokens[0]) and len(tokens) >= 4:
            value = tokens[3].strip("{}").lower()
            try:
                deck.elements.append(
                    Element(tokens[0].upper(), params[value] if value in params else parse_value(value))
                )
            except ValueError:
                continue  # a model name / expression: nothing to compare
    return deck


def compare_values(deck: Deck, board_values: dict[str, float], board_kinds: dict[str, str]) -> list[str]:
    """Reasons the deck no longer describes the board (empty = it does).

    ``board_values`` maps references to R/C/L values, ``board_kinds`` every reference to its kind
    (``resistor`` ...). A deck passive named like a board reference must be the same kind and value; one that
    looks like a reference but is not on the board is stale."""
    out = []
    for element in deck.elements:
        kind = KIND_BY_LETTER[element.name[0]]
        if element.name not in board_kinds:
            out.append(f"{element.name} is in the deck but not on the board")
        elif board_kinds[element.name] != kind:
            out.append(f"{element.name} is a {kind} in the deck but a {board_kinds[element.name]} on the board")
        elif element.name in board_values:
            board = board_values[element.name]
            if abs(board - element.value) > VALUE_TOLERANCE * max(abs(board), abs(element.value)):
                out.append(f"{element.name}: deck {element.value:g}, board {board:g}")
    return out


def parse_measures(output: str) -> dict[str, float | None]:
    """name -> value from ngspice's batch output (``None``: ngspice could not measure it)."""
    out: dict[str, float | None] = {}
    for line in output.splitlines():
        if m := MEASURE_LINE.match(line):
            out[m.group(1).lower()] = float(m.group(2))
        elif m := MEASURE_FAILED.match(line) or MEASURE_FAILED_VALUE.match(line):
            out[m.group(1).lower()] = None
    return out


def ngspice() -> str | None:
    return shutil.which("ngspice")


def run_deck(deck_path: Path, binary: str) -> dict[str, float | None]:
    """Run ``ngspice -b`` on a deck (in its folder, so includes work) and parse the measures."""
    try:
        done = proc.run([binary, "-b", deck_path.name], cwd=deck_path.parent, timeout=SIM_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        msg = f"{deck_path.name}: ngspice timed out after {SIM_TIMEOUT_S} s"
        raise SimError(msg) from exc
    measures = parse_measures(done.stdout)
    if done.returncode != 0 and not measures:
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        msg = f"{deck_path.name}: ngspice exited {done.returncode}: {tail[0]}"
        raise SimError(msg)
    return measures


# --- defaults and range resolution -------------------------------------------------------------------


def defaults_path() -> Path | None:
    return next((d / DEFAULTS_NAME for d in rules.profile_dirs() if (d / DEFAULTS_NAME).is_file()), None)


def parse_defaults(data: dict[str, Any], source: str = DEFAULTS_NAME) -> Defaults:
    limits = {}
    for key, bounds in data.get("limits", {}).items():
        if not isinstance(bounds, list) or len(bounds) != 2:
            msg = f'{source}: limits.{key} must be [min, max] (use a string "-" for an open end)'
            raise SimError(msg)
        limits[key.lower()] = tuple(None if b == "-" else float(b) for b in bounds)
    required = {kind: tuple(str(d) for d in table.get("decks", [])) for kind, table in data.get("required", {}).items()}
    netlist = {k: float(v) for k, v in data.get("netlist", {}).items()}
    return Defaults(limits, required, netlist, source)  # type: ignore[arg-type]


def load_defaults() -> Defaults:
    path = defaults_path()
    if path is None:
        return Defaults({}, {}, {})
    try:
        return parse_defaults(tomllib.loads(path.read_text(encoding="utf-8")), path.name)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path.name}: {exc}"
        raise SimError(msg) from exc


def resolve_range(name: str, deck: Deck, intent: Intent, defaults: Defaults) -> Range | None:
    """The pass range of measure ``name`` (``None``: no source gives one); see the module docstring."""
    name = name.lower()
    explicit = deck.ranges.get(name)
    if explicit and explicit.kind != "assumed":
        return explicit
    if m := re.match(r"^(vmax|vmin|imax)_(.+)$", name):
        rail = intent.rail_by_key(measure_key(m.group(2)))
        if rail:
            src = f"rail {rail.net}"
            if m.group(1) == "imax" and rail.current is not None:
                return Range(None, rail.current, src, "intent")
            if rail.voltage is not None:
                tol = rail.voltage * rail.tolerance_pct / 100
                if m.group(1) == "vmax":
                    return Range(
                        None, rail.voltage + tol, f"{src} {rail.voltage:g} V +{rail.tolerance_pct:g} %", "intent"
                    )
                if m.group(1) == "vmin":
                    return Range(
                        rail.voltage - tol, None, f"{src} {rail.voltage:g} V -{rail.tolerance_pct:g} %", "intent"
                    )
    for key, (lo, hi) in defaults.limits.items():
        if name == key or name.endswith("_" + key):
            return Range(lo, hi, f"{defaults.source} limits.{key}", "house")
    return explicit  # may be None


def evaluate(deck: Deck, values: dict[str, float | None], intent: Intent, defaults: Defaults) -> list[Measured]:
    """One :class:`Measured` per ``.measure`` of the deck, judged against its resolved range."""
    out = []
    for name in deck.measures:
        value = values.get(name)
        rng = resolve_range(name, deck, intent, defaults)
        if value is None:
            status = "error"
        elif rng is None or rng.kind == "assumed":
            status = "fail" if rng and not rng.contains(value) else "assumed"
        else:
            status = "ok" if rng.contains(value) else "fail"
        out.append(Measured(name, value, rng, status))
    return out


def deck_paths(board: str) -> list[Path]:
    return sorted((paths.board_source(board).parent / "sim").glob("*.cir"))


def check_deck(
    deck: Deck,
    board_values: dict[str, float],
    board_kinds: dict[str, str],
    intent: Intent,
    defaults: Defaults,
    binary: str | None,
) -> DeckResult:
    """Validate a deck against the board, run it and judge its measures."""
    if stale := compare_values(deck, board_values, board_kinds):
        return DeckResult(deck, "stale", problems=stale)
    if binary is None:
        return DeckResult(deck, "skipped", problems=["ngspice is not installed (pacman -S ngspice)"])
    if not deck.measures:
        return DeckResult(deck, "error", problems=["no .measure line: the deck proves nothing"])
    try:
        values = run_deck(deck.path, binary)
    except SimError as exc:
        return DeckResult(deck, "error", problems=[str(exc)])
    measures = evaluate(deck, values, intent, defaults)
    status = "fail" if any(m.status in {"fail", "error"} for m in measures) else "ok"
    return DeckResult(deck, status, measures)
