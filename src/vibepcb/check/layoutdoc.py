"""``layout.md`` for the reviewer agent: where every part and pin sits, and which part serves which pin.

The reviewer judges placement against the datasheets' layout guidelines, so the review package needs the layout
as text (``review.write_package``). Built on :func:`placement.analyse` (positions, owners, judged links);
coordinates in the board frame (mm, +x right, +y up = the board image's top, seen from the top side).

Sections:

- **Bottom side** first: every chip (a part with 3+ pins) on the bottom, mirrored in x. Its pin rows as they land on
  the board (which pins face +x / -x / +y / -y), and per pin the parts serving it with distance and side.
  ``WRONG SIDE``: the part's pad on the pin's net sits on another side of the chip than the pin and farther than
  its placement limit; ``far``: on the pin's side but beyond the limit (within ``NEAR_MM`` any side is fine).
  Mismatches are listed first.
  (bench-psu: every LMR33630 buck had its inductor and caps placed as for the top side, on the far side.)
- **Top side**: the same for the top-side chips.
- **Regulators**: the loops a switcher layout guide asks about: input cap to VIN / ground, inductor to SW, BOOT
  cap, FB divider to FB and its distance to the switch node, the SW net's routed track length (KiCad board).
- **Parts**: every part's layer, centre and rotation.

A part *serves* a chip pin when it is a two-terminal part on the pin's (non-ground) net and it is the chip's
(``schgen`` owner, or a ``placement`` link to it), or it has no owner and the net is local to the chip (no other
chip on it). Each pad is listed once, at the nearest pin of the chip on its net.

:func:`layout_hash` is what the review's layout items saw (part layers, positions, rotations).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vibepcb.check import netcheck, placement
from vibepcb.core import sexpr

if TYPE_CHECKING:
    from pathlib import Path

    from vibepcb.check.netcheck import Design, Pin
    from vibepcb.check.placement import Analysis, Body, Point

NEAR_MM = 2.5  # a serving part this close to its pin is fine on any side
EDGE_TOL = 0.3  # a pad within this of the chip's pad-box edge sits on that edge
BOOT_PIN = re.compile(r"^(BOOT|BST|CB|CBOOT)\d*$", re.IGNORECASE)
FB_PIN = re.compile(r"^(FB|ADJ|VFB)\d*$", re.IGNORECASE)
EXPOSED_PAD = re.compile(r"^(EP|E?PAD|THERMAL|TAB)\d*$", re.IGNORECASE)
SIDES = ("+x", "-x", "+y", "-y")


def layout_hash(circuit: list[dict[str, Any]]) -> str:
    """Every part's layer, centre (0.01 mm) and rotation: a placement change changes it, routing does not."""
    names = {e["source_component_id"]: str(e["name"]) for e in circuit if e.get("type") == "source_component"}
    parts = sorted(
        (
            names[e["source_component_id"]],
            str(e.get("layer", "top")),
            round(float(e["center"]["x"]), 2),
            round(float(e["center"]["y"]), 2),
            round(float(e.get("rotation") or 0), 1),
        )
        for e in circuit
        if e.get("type") == "pcb_component" and e.get("source_component_id") in names
    )
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


# --- geometry -------------------------------------------------------------------------------------------------


def _pad_box(body: Body) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in body.pads], [p[1] for p in body.pads]
    return min(xs), min(ys), max(xs), max(ys)


def pin_sides(body: Body, pads: dict[str, Point]) -> dict[str, str]:
    """Pin number -> the side of the chip it sits on (``+x`` ...), ``centre`` (exposed pad) or ``-`` (a single
    row: no side). A pad on two edges (a SOIC corner pin) takes the edge with more pads."""
    if not pads:
        return {}
    x0, y0, x1, y1 = _pad_box(body) if body.pads else _box(list(pads.values()))
    flat_x, flat_y = x1 - x0 < EDGE_TOL, y1 - y0 < EDGE_TOL
    on: dict[str, list[str]] = {}
    for num, (x, y) in pads.items():
        edges = []
        if not flat_x:
            edges += ["+x"] * (abs(x - x1) <= EDGE_TOL) + ["-x"] * (abs(x - x0) <= EDGE_TOL)
        if not flat_y:
            edges += ["+y"] * (abs(y - y1) <= EDGE_TOL) + ["-y"] * (abs(y - y0) <= EDGE_TOL)
        on[num] = edges
    count = {s: sum(s in e for e in on.values()) for s in SIDES}

    def pitch(num: str, side: str) -> float:  # a row's pads sit closer together than a corner's two edges
        return min((math.dist(pads[num], pads[n]) for n, e in on.items() if n != num and side in e), default=math.inf)

    out = {}
    for num, edges in on.items():
        if edges:
            out[num] = max(edges, key=lambda s: (count[s], -pitch(num, s), s))
        else:
            out[num] = "-" if flat_x or flat_y else "centre"
    return out


def _box(points: list[Point]) -> tuple[float, float, float, float]:
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def side_of(body: Body, point: Point) -> str:
    """Where ``point`` lies relative to the chip's pads: the side it is farthest beyond, ``under`` inside."""
    x0, y0, x1, y1 = _pad_box(body)
    dx = point[0] - x1 if point[0] > x1 else point[0] - x0 if point[0] < x0 else 0.0
    dy = point[1] - y1 if point[1] > y1 else point[1] - y0 if point[1] < y0 else 0.0
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return "under"
    if abs(dx) >= abs(dy):
        return "+x" if dx > 0 else "-x"
    return "+y" if dy > 0 else "-y"


def _xy(p: Point) -> str:
    return f"{p[0]:.2f}, {p[1]:.2f}"


# --- who serves which pin -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Serving:
    """``ref``'s pad ``pad`` (on the pin's net) is ``distance`` mm from chip pin ``pin``, on side ``side``."""

    ref: str
    pad: str
    pin: str
    distance: float
    side: str
    limit: float
    verdict: str  # ok | far | WRONG SIDE


def _two_terminal(design: Design, ref: str) -> bool:
    return design.parts[ref].kind != "chip" and len(design.pins_of(ref)) == 2


def chips(design: Design, analysis: Analysis) -> list[str]:
    """Parts with 3+ pins that have a place on the board."""
    return sorted(
        r
        for r in design.parts
        if len(design.pins_of(r)) >= 3 and analysis.layout.bodies.get(r) and analysis.layout.bodies[r].pads
    )


def serving(design: Design, analysis: Analysis, chip: str, limits: placement.Limits) -> dict[str, list[Serving]]:
    """Chip pin number -> the parts serving it (module doc)."""
    layout = analysis.layout
    body = layout.bodies[chip]
    nets = design.nets()
    many = {r for r in design.parts if len(design.pins_of(r)) >= 3}
    linked = {(k.ref, k.target) for k in analysis.links}
    regulator = chip in set(netcheck.find_regulators(design))
    sides = pin_sides(body, {p.number: pt for p in design.pins_of(chip) if (pt := layout.pad(p)) is not None})
    best: dict[str, Serving] = {}  # one entry per serving part: at its nearest pin
    for pin in design.pins_of(chip):
        net = pin.net
        anchor = layout.pad(pin)
        if not net or netcheck.is_ground(net) or anchor is None:
            continue
        local = not any(q.ref != chip and q.ref in many for q in nets[net])
        for q in nets[net]:
            if q.ref == chip or not _two_terminal(design, q.ref):
                continue
            owner = analysis.owners.get(q.ref)
            if not (owner == chip or (q.ref, chip) in linked or (local and owner is None)):
                continue
            pad = layout.pad(q)
            if pad is None:
                continue
            dist = round(math.dist(pad, anchor), 2)
            kind = design.parts[q.ref].kind
            limit = limits.decoupling_mm if kind == "capacitor" and not regulator else limits.regulator_mm
            side, pin_side = side_of(body, pad), sides.get(pin.number, "-")
            other = pin_side in SIDES and side not in {pin_side, "under"}
            if dist <= NEAR_MM:
                verdict = "ok"
            elif dist <= limit:
                verdict = "ok, other side" if other else "ok"
            else:
                verdict = "WRONG SIDE" if other else "far"
            entry = Serving(q.ref, q.number, pin.number, dist, side, limit, verdict)
            key = f"{q.ref}.{q.number}"
            if key not in best or dist < best[key].distance:
                best[key] = entry
    out: dict[str, list[Serving]] = {}
    for entry in sorted(best.values(), key=lambda s: (s.distance, s.ref)):
        out.setdefault(entry.pin, []).append(entry)
    return out


# --- sections -------------------------------------------------------------------------------------------------


def _pin_label(pin: Pin) -> str:
    return pin.number if pin.name in {pin.number, f"pin{pin.number}"} else f"{pin.number} {pin.name}"


def _part_label(design: Design, ref: str) -> str:
    text = design.parts[ref].text.removeprefix(ref).strip()
    return f"{ref} {text}".strip()


def chip_section(
    design: Design, analysis: Analysis, chip: str, rotations: dict[str, float], limits: placement.Limits, kind: str
) -> list[str]:
    layout = analysis.layout
    body = layout.bodies[chip]
    pins = design.pins_of(chip)
    pads = {p.number: pt for p in pins if (pt := layout.pad(p)) is not None}
    sides = pin_sides(body, pads)
    served = serving(design, analysis, chip, limits)
    by_num = {p.number: p for p in pins}
    bottom = body.layer == "bottom"
    head = f"### {_part_label(design, chip)}" + (f" ({kind})" if kind else "")
    out = [
        head,
        "",
        f"{'bottom: mirrored in x' if bottom else 'top'}; centre {_xy(body.centre)}, rotation {rotations.get(chip, 0):g} deg",
    ]
    rows = []
    for side in (*SIDES, "centre", "-"):
        on = [_pin_label(by_num[n]) for n in sorted(pads, key=_pin_order) if sides.get(n) == side]
        if on:
            rows.append(f"{side}: {', '.join(on)}" if side != "-" else f"single row: {', '.join(on)}")
    if rows:
        out.append(
            ("Pin rows on the board (after mirroring): " if bottom else "Pin rows on the board: ") + " | ".join(rows)
        )
    bad = [s for entries in served.values() for s in entries if not s.verdict.startswith("ok")]
    if bad:
        out.append("")
        out.append("Mismatches:")
        for s in sorted(bad, key=lambda s: (s.verdict != "WRONG SIDE", -s.distance, s.ref)):
            pin = by_num[s.pin]
            where = f"serves {_pin_label(pin)} ({sides.get(s.pin, '-')} side)"
            if s.verdict == "WRONG SIDE":
                out.append(
                    f"- **WRONG SIDE**: {s.ref} {where} but sits at {s.side}, pad {s.pad} {s.distance:.2f} mm from the pin"
                )
            else:
                out.append(
                    f"- far: {s.ref} {where}, pad {s.pad} {s.distance:.2f} mm from the pin (limit {s.limit:g} mm)"
                )
    elif served:
        out.append("Every serving part on its pin's side, within its limit.")
    out += ["", "| Pin | Side | x, y | Net | Serving parts (pad: distance, side, verdict) |", "|---|---|---|---|---|"]
    for num in sorted(pads, key=_pin_order):
        pin = by_num[num]
        parts = "; ".join(f"{s.ref}.{s.pad}: {s.distance:.2f} mm, {s.side}, {s.verdict}" for s in served.get(num, []))
        out.append(
            f"| {_pin_label(pin)} | {sides.get(num, '-')} | {_xy(pads[num])} | {pin.net or '(no net)'} | {parts} |"
        )
    return [*out, ""]


def _pin_order(number: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", number)
    return (int(digits) if digits else 10**6, number)


def track_lengths(pcb: Path) -> dict[str, tuple[float, set[str]]]:
    """Net name -> (routed track length in mm, copper layers) from a KiCad board."""
    tree = sexpr.load(pcb)
    root = tree[0] if tree and isinstance(tree[0], list) else tree
    names: dict[str, str] = {}
    for node in sexpr.children(root, "net"):
        if len(node) >= 3:
            names[str(node[1])] = str(node[2])
    out: dict[str, tuple[float, set[str]]] = {}
    for kind in ("segment", "arc"):
        for node in sexpr.children(root, kind):
            net_node, layer_node = sexpr.child(node, "net"), sexpr.child(node, "layer")
            if not net_node or len(net_node) < 2:
                continue
            raw = net_node[1]
            net = str(raw) if not isinstance(raw, sexpr.Sym) else names.get(str(raw), str(raw))
            points = [
                (float(str(p[1])), float(str(p[2])))
                for key in ("start", "mid", "end")
                if (p := sexpr.child(node, key)) is not None
            ]
            length = sum(math.dist(a, b) for a, b in itertools.pairwise(points))
            total, layers = out.get(net, (0.0, set()))
            out[net] = (total + length, layers | ({str(layer_node[1])} if layer_node else set()))
    return out


def regulator_section(
    design: Design, analysis: Analysis, reg: str, tracks: dict[str, tuple[float, set[str]]] | None
) -> list[str]:
    layout = analysis.layout
    nets = design.nets()
    pins = design.pins_of(reg)

    def dist(a: Pin, b: Pin) -> float | None:
        pa, pb = layout.pad(a), layout.pad(b)
        return round(math.dist(pa, pb), 2) if pa and pb else None

    def nearest(pin: Pin, targets: list[Pin]) -> tuple[Pin, float] | None:
        found = [(t, d) for t in targets if (d := dist(pin, t)) is not None]
        return min(found, key=lambda td: td[1]) if found else None

    def fmt(found: tuple[Pin, float] | None) -> str:
        return f"{found[1]:.2f} mm to {_pin_label(found[0])}" if found else "no position"

    vin = [p for p in pins if p.name.upper() in netcheck.REGULATOR_IN and p.net]
    grounds = [p for p in pins if netcheck.is_ground(p.net)]
    ground = [p for p in grounds if not EXPOSED_PAD.match(p.name)] or grounds  # the PGND pin, not the pad
    sw = [p for p in pins if placement.SWITCH_PIN.match(p.name) and p.net]
    boot = [p for p in pins if BOOT_PIN.match(p.name) and p.net]
    fb = [p for p in pins if FB_PIN.match(p.name) and p.net]
    owned = {r for r, o in analysis.owners.items() if o == reg}
    lines = [f"### {_part_label(design, reg)}", ""]
    for v in vin:
        caps = [
            q
            for q in nets[v.net or ""]
            if design.parts[q.ref].kind == "capacitor"
            and _two_terminal(design, q.ref)
            and any(netcheck.is_ground(o.net) for o in design.pins_of(q.ref))
        ]
        caps = sorted(caps, key=lambda q: d if (d := dist(q, v)) is not None else math.inf)
        caps = [q for q in caps if q.ref in owned] or caps[:2]
        for q in caps:
            gnd = next(o for o in design.pins_of(q.ref) if netcheck.is_ground(o.net))
            lines.append(
                f"- input cap {_part_label(design, q.ref)}: {v.net} pad {fmt(nearest(q, [v]))}, ground pad "
                f"{fmt(nearest(gnd, ground))}"
            )
    sw_pads: list[Pin] = []
    for s in sw:
        for q in nets[s.net or ""]:
            if q.ref != reg and design.parts[q.ref].kind == "inductor":
                sw_pads.append(q)
                other = next((o for o in design.pins_of(q.ref) if o is not q), None)
                far = f"; its other pad ({other.net}) {fmt(nearest(other, [s]))}" if other else ""
                lines.append(f"- inductor {_part_label(design, q.ref)}: SW pad {q.number} {fmt(nearest(q, [s]))}{far}")
    for b in boot:
        for q in nets[b.net or ""]:
            if q.ref != reg and design.parts[q.ref].kind == "capacitor":
                other = next((o for o in design.pins_of(q.ref) if o is not q), None)
                to_sw = f", other pad {fmt(nearest(other, sw))}" if other else ""
                lines.append(f"- BOOT cap {q.ref}: BOOT pad {fmt(nearest(q, [b]))}{to_sw}")
    for f in fb:
        for q in nets[f.net or ""]:
            if q.ref == reg or not _two_terminal(design, q.ref):
                continue
            node = [p for p in design.pins_of(q.ref) if layout.pad(p)]
            noisy = sw + sw_pads
            closest = min(
                ((p, t, d) for p in node for t in noisy if (d := dist(p, t)) is not None),
                key=lambda x: x[2],
                default=None,
            )
            near_sw = (
                f", nearest switch node: {closest[2]:.2f} mm ({closest[1].ref}.{_pin_label(closest[1])})"
                if closest
                else ""
            )
            lines.append(f"- FB divider {_part_label(design, q.ref)}: FB pad {fmt(nearest(q, [f]))}{near_sw}")
    for s in sw:
        if tracks is None:
            lines.append(f"- SW net {s.net}: no routed KiCad board yet")
        else:
            length, layers = tracks.get(s.net or "", (0.0, set()))
            lines.append(f"- SW net {s.net}: {length:.1f} mm of track on {', '.join(sorted(layers)) or 'no layer'}")
    if len(lines) == 2:
        lines.append("- (no switcher pins: VIN / SW / BOOT / FB not found)")
    return [*lines, ""]


def markdown(
    board: str,
    circuit: list[dict[str, Any]],
    kinds: dict[str, str],
    limits: placement.Limits,
    pcb: Path | None = None,
    image: str | None = None,
) -> str:
    """``layout.md`` (module doc). ``pcb``: the routed KiCad board for track lengths; ``image``: a note about the
    board image in the package."""
    analysis = placement.analyse(circuit, limits)
    design = netcheck.load_design(circuit)
    layout = analysis.layout
    names = {e["source_component_id"]: str(e["name"]) for e in circuit if e.get("type") == "source_component"}
    rotations = {
        names[e["source_component_id"]]: float(e.get("rotation") or 0)
        for e in circuit
        if e.get("type") == "pcb_component" and e.get("source_component_id") in names
    }
    board_el = next((e for e in circuit if e.get("type") == "pcb_board"), {})
    lines = [f"# {board}: layout", ""]
    if not layout.bodies:
        return "\n".join([*lines, "No placement in the circuit JSON."]) + "\n"
    if layout.outline:
        x0, y0, x1, y1 = _box(layout.outline)
        size = f"{x1 - x0:g} x {y1 - y0:g} mm (x {x0:g} .. {x1:g}, y {y0:g} .. {y1:g})"
    else:
        size = "no outline"
    layers = board_el.get("num_layers", 2)
    lines += [
        f"Board: {size}, {layers} copper layers, {board_el.get('thickness', 1.6):g} mm thick.",
        "Coordinates: board frame in mm, +x right, +y up (the board image's top), seen from the top side.",
        "Distances: pad centre to pad centre. Sides (+x / -x / +y / -y) relative to the chip's pads.",
        "**Bottom-side parts are mirrored in x**: their pins sit where this file says, not where the datasheet's",
        "top-view drawing shows them. Judge every pin from the board positions below.",
        f"Verdicts: WRONG SIDE = a part's pad on the pin's net is on another side of the chip than the pin and "
        f"beyond the placement limit (caps {limits.decoupling_mm:g} mm, regulator and other parts "
        f"{limits.regulator_mm:g} mm); far = on the pin's side but beyond the limit; within {NEAR_MM:g} mm any side "
        "is ok. A part is listed once per pad, at the nearest pin of the chip on that pad's net.",
    ]
    if image:
        lines.append(image)
    lines.append("")
    all_chips = chips(design, analysis)
    regulators = sorted(r for r in netcheck.find_regulators(design) if r in all_chips)
    bottom = [c for c in all_chips if layout.bodies[c].layer == "bottom"]
    top = [c for c in all_chips if c not in bottom]
    lines += ["## Bottom side (mirrored in x)", ""]
    if bottom:
        for chip in bottom:
            lines += chip_section(design, analysis, chip, rotations, limits, kinds.get(chip, ""))
    else:
        lines += ["No chips on the bottom side.", ""]
    lines += ["## Top side", ""]
    for chip in top:
        lines += chip_section(design, analysis, chip, rotations, limits, kinds.get(chip, ""))
    if regulators:
        tracks = track_lengths(pcb) if pcb is not None and pcb.exists() else None
        lines += ["## Regulators: loops", ""]
        for reg in regulators:
            lines += regulator_section(design, analysis, reg, tracks)
    lines += ["## Parts", "", "| Ref | Layer | x, y | Rotation |", "|---|---|---|---|"]
    for ref in sorted(layout.bodies, key=_ref_order):
        b = layout.bodies[ref]
        layer = "bottom (mirrored)" if b.layer == "bottom" else b.layer
        lines.append(f"| {ref} | {layer} | {_xy(b.centre)} | {rotations.get(ref, 0):g} |")
    return "\n".join(lines) + "\n"


def _ref_order(ref: str) -> tuple[str, int, str]:
    m = re.match(r"^([A-Za-z]*)(\d*)", ref)
    letters, digits = (m.group(1), m.group(2)) if m else (ref, "")
    return letters.upper(), int(digits) if digits else 0, ref


def bottom_chips(circuit: list[dict[str, Any]], design: Design) -> list[str]:
    """Parts with 3+ pins on the bottom side (each gets the ``bottom`` review items)."""
    layout = placement.load_layout(circuit)
    return sorted(
        r for r in design.parts if len(design.pins_of(r)) >= 3 and (b := layout.bodies.get(r)) and b.layer == "bottom"
    )
