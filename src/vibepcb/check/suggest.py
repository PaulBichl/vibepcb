"""Placement suggestions: where the passives go that serve one chip pin (``pcb place-suggest <board>``).

Works on the unrouted circuit JSON (the placement gate's), like ``check.placement``, and writes nothing: it prints
TSX edits for the designer (no automatic rewrite of the board source). The parts it moves:

- every relation :func:`placement.analyse` judges (decoupling caps, crystals, load caps, regulator caps and
  inductor) beyond its limit;
- every other resistor / capacitor / inductor whose schematic owner (``schgen.passive_owners``; a chip U / IC or a
  connector) has a pin on one of its signal nets, farther than ``SIGNAL_MM`` from that pin (``signal``; not a
  thermistor RT / TH / NTC: it sits where it measures);
- with ``everything``: every such part that can get at least ``MIN_GAIN`` closer, within its limit or not.

In that order (one spot per part; a moved part's new place is taken for the next ones, and its old place is free),
each goes to :func:`placement.free_spot` next to the pin it serves: turned in 90 deg steps, the pad on the pin's net
first and nearest the pin, on the side of the chip the pin faces, box + courtyard clear of the other parts,
keepouts and holes, on the board. Its other nets pull too (``NET_WEIGHT`` x the distance of each other pad to the
nearest pad of its net). A move that does not bring the pad ``MIN_GAIN`` closer, or makes the ratsnest longer by
more than ``MAX_LONGER``, is not suggested.

The ratsnest is ``place-score``'s metric (``ratsnest.pad_links``: per net a minimum spanning tree over the pads,
without plane nets of ``[planes]`` and nets with a copper pour), computed on the circuit JSON before and after.

The edit is written against the TSX: the part's element (``name="C5"``) in ``boards/<board>/*.tsx`` with its
current ``pcbX`` / ``pcbY`` / ``pcbRotation`` expressions, shifted by the move (``pcbY={YL + 4}`` ->
``pcbY={YL + 1.5}``). A part inside a ``<group>`` / subcircuit, or one written inside a sub-component (above the
board's ``export default``) or generated (no literal name), gets a note instead of a guessed expression: its TSX
coordinates are not board coordinates.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from vibepcb.check import netcheck, placement
from vibepcb.kicad import schgen
from vibepcb.routing import ratsnest, routes

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibepcb.check.placement import Box, Layout, Link, Point, Spot

SIGNAL_MM = 5.0  # an owned passive farther than this from its owner's pin gets a suggestion
MIN_GAIN = 0.5  # a suggestion brings the serving pad at least this much closer to the pin
MAX_LONGER = 1.0  # ... and makes the ratsnest at most this much longer (mm)
NET_WEIGHT = 0.3  # cost per mm of a moved part's other pads to their nets' nearest pads
PASSIVES = {"resistor", "capacitor", "inductor"}
CHIP_REF = re.compile(r"^(U|IC)\d+$", re.IGNORECASE)  # owners of ``signal`` parts: chips (and connectors)
SENSOR_REF = re.compile(r"^(RT|TH|NTC|PTC)\d+$", re.IGNORECASE)  # thermistors sit where they measure
ORDER = {"crystal": 0, "regulator": 1, "decoupling": 2, "load_cap": 3, "signal": 4}


@dataclass(frozen=True)
class Source:
    """Where a part is written in the TSX: file, line, the expressions of pcbX / pcbY / pcbRotation (``None``:
    not set), and a note when they are not board coordinates."""

    file: str = ""
    line: int = 0
    attrs: dict[str, str | None] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class Suggestion:
    link: Link
    spot: Spot
    before: float  # serving pad to pin, mm
    after: float
    centre: Point  # where the part is now
    rotation: float  # its rotation now (deg ccw, TSX form)
    ratsnest: float  # change of the ratsnest length of the part's nets by this move, mm
    source: Source = field(default_factory=Source)

    @property
    def new_rotation(self) -> float:
        return placement.tsx_rotation(self.rotation + self.spot.turn)


@dataclass
class Result:
    suggestions: list[Suggestion] = field(default_factory=list)
    stuck: list[tuple[Link, str]] = field(default_factory=list)  # candidates without a useful spot, why
    before: tuple[float, int] = (0.0, 0)  # ratsnest length (mm), crossings
    after: tuple[float, int] = (0.0, 0)
    candidates: int = 0


# --- relations ------------------------------------------------------------------------------------------------


def signal_links(circuit: list[dict[str, Any]], analysis: placement.Analysis) -> list[Link]:
    """For each resistor / capacitor / inductor not judged by :func:`placement.analyse`: the pin of its schematic
    owner on one of its signal nets nearest to it (``rule = "signal"``, limit ``SIGNAL_MM``)."""
    design = netcheck.load_design(circuit)
    layout, judged = analysis.layout, {k.ref for k in analysis.links}
    rails = schgen.rails(circuit)
    nets = design.nets()
    out = []
    connectors = set(placement.find_connectors(design))
    for ref, owner in sorted(analysis.owners.items()):
        part = design.parts.get(ref)
        if part is None or part.kind not in PASSIVES or ref in judged or owner not in design.parts:
            continue
        if (not CHIP_REF.match(owner) and owner not in connectors) or SENSOR_REF.match(ref):
            continue  # an antenna terminal, jack, crystal ... / a thermistor: placed where it is for its own reasons
        pairs = [
            (pin, q)
            for pin in design.pins_of(ref)
            if pin.net and pin.net not in rails and not netcheck.is_ground(pin.net)
            for q in nets[pin.net]
            if q.ref == owner
        ]
        links = []
        for pin, q in pairs:
            a, b = layout.pad(pin), layout.pad(q)
            if a is not None and b is not None:
                d = round(math.dist(a, b), 2)
                links.append(
                    placement.Link("signal", ref, pin.number, owner, q.number, q.name, pin.net or "", d, SIGNAL_MM, b)
                )
        if links:
            out.append(min(links, key=lambda k: (k.distance, k.target_pin)))
    return out


# --- ratsnest -------------------------------------------------------------------------------------------------


def _pads(layout: Layout, net_of: dict[tuple[str, str], str]) -> list[routes.Pad]:
    return [
        routes.Pad(ref, num, net_of[(ref, num)], p, frozenset())
        for (ref, num), p in sorted(layout.pads.items())
        if (ref, num) in net_of
    ]


def score(layout: Layout, net_of: dict[tuple[str, str], str], skip: set[str]) -> tuple[float, int]:
    """``place-score``'s numbers on a layout: ratsnest length (mm) and crossings."""
    links = ratsnest.pad_links(_pads(layout, net_of), skip)
    return sum(k.length for k in links), ratsnest.crossings(links)


def _net_length(layout: Layout, net_of: dict[tuple[str, str], str], skip: set[str], nets: set[str]) -> float:
    pads = [p for p in _pads(layout, net_of) if p.net in nets]
    return sum(k.length for k in ratsnest.pad_links(pads, skip))


def skip_nets(circuit: list[dict[str, Any]], planes: list[str], net_of: dict[tuple[str, str], str]) -> set[str]:
    """The nets ``place-score`` leaves out: ``[planes] nets`` and the nets of copper pours (by the circuit JSON's
    net, named as :func:`netcheck.load_design` names it)."""
    poured = {e.get("source_net_id") for e in circuit if e.get("type") == "pcb_copper_pour"}
    keys = {e.get("subcircuit_connectivity_map_key") for e in circuit if e.get("source_net_id") in poured}
    keys.discard(None)
    sources = {e["source_component_id"]: str(e["name"]) for e in circuit if e.get("type") == "source_component"}
    out = set(planes)
    for e in circuit:
        if e.get("type") == "source_port" and e.get("subcircuit_connectivity_map_key") in keys:
            key = (sources.get(e.get("source_component_id", ""), ""), str(e.get("pin_number") or e.get("name")))
            if key in net_of:
                out.add(net_of[key])
    return out


# --- moves ----------------------------------------------------------------------------------------------------


def _moved(layout: Layout, spot: Spot) -> None:
    """Put the part where ``spot`` says (in place)."""
    body = layout.bodies[spot.ref]
    (cx, cy), (nx, ny) = body.centre, spot.centre

    def shifted_box(box: Box) -> Box:
        x0, y0, x1, y1 = placement.turned_box(box, (cx, cy), spot.turn)
        return nx + x0, ny + y0, nx + x1, ny + y1

    pads = tuple((nx + q[0], ny + q[1]) for p in body.pads for q in [placement.turned(p[0] - cx, p[1] - cy, spot.turn)])
    layout.bodies[spot.ref] = replace(
        body,
        centre=spot.centre,
        box=shifted_box(body.box),
        courtyard=shifted_box(body.courtyard),
        pads=pads,
        rotation=(body.rotation + spot.turn) % 360,
    )
    for num, p in spot.pads.items():
        layout.pads[(spot.ref, num)] = p


def _pull(layout: Layout, net_of: dict[tuple[str, str], str], skip: set[str], link: Link) -> Callable[[Spot], float]:
    """``extra`` cost for :func:`placement.free_spot`: the part's other pads to their nets' nearest pads."""
    others: dict[str, list[Point]] = {}
    for (ref, num), p in layout.pads.items():
        net = net_of.get((ref, num))
        if ref != link.ref and net and net not in skip:
            others.setdefault(net, []).append(p)

    def extra(spot: Spot) -> float:
        total = 0.0
        for num, p in spot.pads.items():
            net = net_of.get((link.ref, num))
            if num != link.pin and net in others:
                total += min(math.dist(p, q) for q in others[net or ""])
        return NET_WEIGHT * total

    return extra


def suggest(
    circuit: list[dict[str, Any]],
    limits: placement.Limits,
    planes: list[str] | None = None,
    *,
    everything: bool = False,
) -> Result:
    """The moves (see the module doc), in order, each on the layout the earlier ones left."""
    analysis = placement.analyse(circuit, limits)
    layout = analysis.layout
    design = netcheck.load_design(circuit)
    net_of = {(p.ref, p.number): p.net for p in design.pins if p.net}
    skip = skip_nets(circuit, planes or [], net_of)
    result = Result(before=score(layout, net_of, skip))
    links = sorted(
        analysis.links + signal_links(circuit, analysis), key=lambda k: (ORDER.get(k.rule, 9), -k.distance, k.ref)
    )
    seen: set[str] = set()
    for link in links:
        if link.ref in seen or link.ref not in layout.bodies or not (link.far or everything):
            continue
        seen.add(link.ref)
        result.candidates += 1
        anchor = layout.pads.get((link.target, link.target_pin), link.anchor)  # its chip may have moved (crystal)
        before = math.dist(layout.pads[(link.ref, link.pin)], anchor)
        spot = placement.free_spot(
            layout,
            link.ref,
            anchor,
            placement.spot_radius(layout, link.ref),
            pin=link.pin,
            facing=placement.outward(layout, link.target, anchor),
            extra=_pull(layout, net_of, skip, link),
        )
        if spot is None:
            result.stuck.append((link, f"no free spot within {placement.spot_radius(layout, link.ref):g} mm"))
            continue
        after = math.dist(spot.pads[link.pin], anchor)
        if after > before - MIN_GAIN:
            result.stuck.append((link, f"no spot closer than now ({before:.1f} mm; best {after:.1f} mm)"))
            continue
        nets = {net_of[(link.ref, n)] for n in spot.pads if (link.ref, n) in net_of} - skip
        old = _net_length(layout, net_of, skip, nets)
        trial = placement.Layout(dict(layout.bodies), dict(layout.pads), layout.outline, layout.blocked)
        _moved(trial, spot)
        delta = _net_length(trial, net_of, skip, nets) - old
        if delta > MAX_LONGER:
            result.stuck.append(
                (link, f"the spot at {before:.1f} -> {after:.1f} mm makes the ratsnest {delta:+.1f} mm")
            )
            continue
        body = layout.bodies[link.ref]
        _moved(layout, spot)
        result.suggestions.append(
            Suggestion(
                link, spot, round(before, 2), round(after, 2), body.centre, placement.tsx_rotation(body.rotation),
                round(delta, 2),
            )
        )  # fmt: skip
    result.after = score(layout, net_of, skip)
    return result


# --- the TSX --------------------------------------------------------------------------------------------------

_ATTR = r"\b{}=(\{{(?:[^{{}}]|\{{[^{{}}]*\}})*\}}|\"[^\"]*\"|'[^']*')"
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_TAIL = re.compile(r"(.*\S)\s*([+-])\s*(\d+(?:\.\d+)?)")
_SPREAD = re.compile(r"\{\s*\.\.\.(\w+)\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)\s*\}")  # {...at(x, y)}: a position helper


def _element(text: str, at: int) -> tuple[int, int]:
    """Start and end of the JSX element whose attribute is at ``at``."""
    start = text.rfind("<", 0, at)
    depth, quote, i = 0, "", at
    while i < len(text):
        ch = text[i]
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ">" and depth == 0:
            return start, i + 1
        i += 1
    return start, len(text)


def groups_of(circuit: list[dict[str, Any]]) -> dict[str, str]:
    """Part -> the name of the ``<group>`` / subcircuit it sits in (only parts not directly on the board)."""
    groups = {e["source_group_id"]: e for e in circuit if e.get("type") == "source_group"}
    out = {}
    for e in circuit:
        g = groups.get(e.get("source_group_id", "")) if e.get("type") == "source_component" else None
        if g is not None and g.get("parent_source_group_id"):
            out[str(e["name"])] = str(g.get("name") or g["source_group_id"])
    return out


def locate(files: dict[str, str], ref: str, group: str | None = None) -> Source:
    """Where ``ref`` is written in the board's TSX files (name -> text)."""
    if group:
        return Source(note=f"inside group {group}: its TSX pcbX / pcbY are relative to the group")
    pattern = re.compile(rf"\bname=(?:\"{re.escape(ref)}\"|'{re.escape(ref)}'|\{{\"{re.escape(ref)}\"\}})")
    for name, text in files.items():
        if (m := pattern.search(text)) is None:
            continue
        start, end = _element(text, m.start())
        element = text[start:end]
        attrs: dict[str, str | None] = {}
        for attr in ("pcbX", "pcbY", "pcbRotation"):
            found = re.search(_ATTR.format(attr), element)
            attrs[attr] = found.group(1) if found else None
        if attrs["pcbX"] is None and attrs["pcbY"] is None and (spread := _SPREAD.search(element)):
            attrs.update(spread=spread.group(1), pcbX="{" + spread.group(2) + "}", pcbY="{" + spread.group(3) + "}")
        line = text.count("\n", 0, start) + 1
        body = text.find("export default")
        if body < 0 or start < body:
            return Source(name, line, attrs, "written inside a sub-component: its pcbX / pcbY are relative to it")
        return Source(name, line, attrs)
    return Source(note="not written literally in the TSX (generated): set the position where it is made")


def _fmt(v: float) -> str:
    return f"{round(v, 3):g}"


def shifted(expr: str | None, old: float, new: float) -> str:
    """A TSX attribute value (``{YL + 4}``, ``{3}``, ``"3mm"``, ``None`` = unset) moved from ``old`` to ``new``."""
    delta = round(new - old, 6)
    if expr is None:
        return "{" + _fmt(new) + "}"
    inner = expr[1:-1].strip()
    if expr[0] in "\"'":
        number = inner.removesuffix("mm")
        return "{" + _fmt(new) + "}" if _NUMBER.fullmatch(number) else expr
    if abs(delta) < 1e-9:
        return expr
    if _NUMBER.fullmatch(inner):
        return "{" + _fmt(new) + "}"
    if m := _TAIL.fullmatch(inner):
        c = (float(m.group(3)) if m.group(2) == "+" else -float(m.group(3))) + delta
        tail = "" if abs(c) < 1e-9 else f" {'+' if c > 0 else '-'} {_fmt(abs(c))}"
        return "{" + m.group(1) + tail + "}"
    return "{" + f"{inner} {'+' if delta > 0 else '-'} {_fmt(abs(delta))}" + "}"


def with_sources(result: Result, board_dir: Path, circuit: list[dict[str, Any]]) -> Result:
    """``result`` with each suggestion's place in ``board_dir/*.tsx`` (index.circuit.tsx first)."""
    paths = sorted(board_dir.glob("*.tsx"), key=lambda p: (p.name != "index.circuit.tsx", p.name))
    files = {p.name: p.read_text(encoding="utf-8") for p in paths}
    groups = groups_of(circuit)
    result.suggestions = [
        replace(s, source=locate(files, s.link.ref, groups.get(s.link.ref))) for s in result.suggestions
    ]
    return result


def edit(s: Suggestion) -> str:
    """The TSX attributes to write: ``pcbX={..} pcbY={..}`` (+ ``pcbRotation={..}`` when it turns), the current
    expressions shifted when the part is written as board coordinates, else absolute values."""
    attrs = s.source.attrs if not s.source.note else {}
    x = shifted(attrs.get("pcbX"), s.centre[0], s.spot.centre[0])
    y = shifted(attrs.get("pcbY"), s.centre[1], s.spot.centre[1])
    parts = [f"{{...{attrs['spread']}({x[1:-1]}, {y[1:-1]})}}" if attrs.get("spread") else f"pcbX={x} pcbY={y}"]
    if s.spot.turn:
        parts.append(f"pcbRotation={{{_fmt(s.new_rotation)}}}")
    return " ".join(parts)


def current(s: Suggestion) -> str:
    """The part's position attributes as the TSX writes them now."""
    attrs = s.source.attrs
    if attrs.get("spread"):
        rotation = f" pcbRotation={attrs['pcbRotation']}" if attrs.get("pcbRotation") else ""
        return f"{{...{attrs['spread']}({str(attrs['pcbX'])[1:-1]}, {str(attrs['pcbY'])[1:-1]})}}{rotation}"
    return " ".join(f"{k}={v}" for k, v in attrs.items() if v is not None) or "(no pcbX / pcbY)"


def move(s: Suggestion) -> str:
    """The move as a delta (for a part whose TSX position is not board coordinates)."""
    dx, dy = s.spot.centre[0] - s.centre[0], s.spot.centre[1] - s.centre[1]
    text = f"move dx={'+' if dx >= 0 else ''}{_fmt(dx)} dy={'+' if dy >= 0 else ''}{_fmt(dy)}"
    turn = placement.tsx_rotation(s.spot.turn)
    return text + (f", turn {'+' if turn > 0 else ''}{_fmt(turn)} deg" if turn else "")
