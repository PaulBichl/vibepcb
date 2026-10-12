"""Generate a KiCad schematic from tscircuit's circuit JSON (netlist-style: boxes + net labels).

tscircuit's schematic layout does not scale: a ~250-part board does not finish in 20 minutes,
even with fixed positions and net labels only. Such boards set ``schematicDisabled``; their
export then has no symbols, and BOM, ERC and the schematic PDF come from this instead: every
part is a box with its pins, every connected pin carries a global label with its net name, and
the parts flow in source order (so a TSX written block by block reads block by block).
"""

from __future__ import annotations

import json
import math
import uuid
from typing import TYPE_CHECKING, Any

from vibepcb import sexpr

if TYPE_CHECKING:
    from pathlib import Path

GRID = 2.54  # pin pitch; everything sits on the 1.27 mm connection grid
PIN_LENGTH = 2.54
FONT = 1.27
CHAR_W = 1.0  # width per label/pin-name character at 1.27 mm (generous)
ROW_WIDTH = 1050.0  # mm, flow width (fit_schematic_sheet picks the paper afterwards)
CELL_GAP = 5.08
ROW_GAP = 7.62
NAMESPACE = uuid.UUID("5b0e6c3e-6f43-4a51-9f7b-2c3f6d1e8a10")

VALUE_KEYS = ("display_resistance", "display_capacitance", "display_inductance", "manufacturer_part_number")


def _uid(*parts: object) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(str(p) for p in parts)))


def _snap(v: float) -> float:
    return round(math.ceil(v / GRID - 1e-9) * GRID, 4)


def _q(text: str) -> str:
    return json.dumps(text)  # KiCad strings: double quotes, backslash escapes


def _fmt(v: float) -> str:
    return f"{round(v, 4):g}"


def has_symbols(sch_path: Path) -> bool:
    """Whether the schematic places any symbol (an export with ``schematicDisabled`` has none)."""
    return any(True for _ in sexpr.children(sexpr.load(sch_path), "symbol"))


def _nets(circuit: list[dict[str, Any]]) -> dict[str, str]:
    """source_port_id -> net name; unnamed nets are named KiCad-style after their first pin."""
    named = {e["subcircuit_connectivity_map_key"]: e["name"] for e in circuit if e.get("type") == "source_net"}
    comps = {e["source_component_id"]: e["name"] for e in circuit if e.get("type") == "source_component"}
    traced = {p for e in circuit if e.get("type") == "source_trace" for p in e.get("connected_source_port_ids", [])}
    by_key: dict[str, list[dict[str, Any]]] = {}
    for e in circuit:
        if e.get("type") == "source_port" and e.get("subcircuit_connectivity_map_key"):
            by_key.setdefault(e["subcircuit_connectivity_map_key"], []).append(e)
    out = {}
    for key, ports in by_key.items():
        if key not in named and not any(p["source_port_id"] in traced for p in ports):
            continue  # a pin nothing connects to
        first = ports[0]
        name = (
            named.get(key) or f"Net-({comps.get(first['source_component_id'], '?')}-Pad{first.get('pin_number', '?')})"
        )
        for p in ports:
            out[p["source_port_id"]] = name
    return out


def _value(comp: dict[str, Any]) -> str:
    for key in VALUE_KEYS:
        if comp.get(key):
            return str(comp[key])
    return str(comp["name"])


def generate(circuit_json: Path, stem: str) -> str:
    """The ``.kicad_sch`` text for the circuit."""
    circuit = json.loads(circuit_json.read_text(encoding="utf-8"))
    nets = _nets(circuit)
    ports: dict[str, list[dict[str, Any]]] = {}
    for e in circuit:
        if e.get("type") == "source_port":
            ports.setdefault(e["source_component_id"], []).append(e)
    comps = [e for e in circuit if e.get("type") == "source_component" and ports.get(e["source_component_id"])]
    root = _uid(stem, "root")

    lib, placed, labels = [], [], []
    x = y = row_h = 0.0
    for comp in comps:
        name = str(comp["name"])
        pins = sorted(ports[comp["source_component_id"]], key=lambda p: (p.get("pin_number") or 0, p["name"]))
        n_left = math.ceil(len(pins) / 2)
        sides = [pins[:n_left], pins[n_left:]]
        longest = max(len(str(p["name"])) for p in pins)
        half_w = _snap(max(2 * GRID, longest * CHAR_W + GRID))
        rows = max(len(s) for s in sides)
        half_h = _snap((rows + 1) * GRID / 2)
        label_w = [max([len(nets.get(p["source_port_id"], "")) * CHAR_W + 4 for p in s] or [0]) for s in sides]
        cell_w = label_w[0] + 2 * (half_w + PIN_LENGTH) + label_w[1]
        cell_h = 2 * half_h + 2 * GRID * 2  # reference above, value below
        if x and x + cell_w > ROW_WIDTH:
            x, y, row_h = 0.0, y + row_h + ROW_GAP, 0.0
        cx, cy = _snap(x + label_w[0] + half_w + PIN_LENGTH), _snap(y + cell_h / 2)
        x += cell_w + CELL_GAP
        row_h = max(row_h, cell_h)

        lib_id = f"vibepcb:{name}"
        pin_defs, pin_refs = [], []
        for side, group in enumerate(sides):
            for i, p in enumerate(group):
                py = half_h - GRID * (i + 1)  # symbol frame: y up
                px = -(half_w + PIN_LENGTH) if side == 0 else half_w + PIN_LENGTH
                number = str(p.get("pin_number") or p["name"])
                pin_defs.append(
                    f"(pin passive line (at {_fmt(px)} {_fmt(py)} {0 if side == 0 else 180}) (length {_fmt(PIN_LENGTH)})"
                    f" (name {_q(str(p['name']))} (effects (font (size {FONT} {FONT}))))"
                    f" (number {_q(number)} (effects (font (size {FONT} {FONT})))))"
                )
                pin_refs.append(f"(pin {_q(number)} (uuid {_uid(stem, name, 'pin', number)}))")
                net = nets.get(p["source_port_id"])
                if net:
                    ax, ay = cx + px, cy - py  # sheet frame: y down
                    angle, justify = (180, "right") if side == 0 else (0, "left")
                    labels.append(
                        f"(global_label {_q(net)} (shape passive) (at {_fmt(ax)} {_fmt(ay)} {angle})"
                        f" (effects (font (size {FONT} {FONT})) (justify {justify}))"
                        f" (uuid {_uid(stem, name, 'label', number)}) (fields_autoplaced no))"
                    )
        lib.append(
            f"(symbol {_q(lib_id)} (pin_names (offset 0.508)) (exclude_from_sim no) (in_bom yes) (on_board yes)"
            f' (property "Reference" "U" (at 0 0 0) (effects (font (size {FONT} {FONT}))))'
            f' (property "Value" "" (at 0 0 0) (effects (font (size {FONT} {FONT}))))'
            f' (property "Footprint" "" (at 0 0 0) (effects (font (size {FONT} {FONT})) (hide yes)))'
            f' (property "Datasheet" "~" (at 0 0 0) (effects (font (size {FONT} {FONT})) (hide yes)))'
            f" (symbol {_q(name + '_0_1')} (rectangle (start {_fmt(-half_w)} {_fmt(half_h)})"
            f" (end {_fmt(half_w)} {_fmt(-half_h)}) (stroke (width 0.254) (type default)) (fill (type background))))"
            f" (symbol {_q(name + '_1_1')} {' '.join(pin_defs)}))"
        )
        placed.append(
            f"(symbol (lib_id {_q(lib_id)}) (at {_fmt(cx)} {_fmt(cy)} 0) (unit 1) (exclude_from_sim no)"
            f" (in_bom yes) (on_board yes) (dnp no) (uuid {_uid(stem, name)}) (fields_autoplaced no)"
            f' (property "Reference" {_q(name)} (at {_fmt(cx)} {_fmt(cy - half_h - GRID)} 0)'
            f" (effects (font (size {FONT} {FONT}))))"
            f' (property "Value" {_q(_value(comp))} (at {_fmt(cx)} {_fmt(cy + half_h + GRID)} 0)'
            f" (effects (font (size {FONT} {FONT}))))"
            f' (property "Footprint" "" (at {_fmt(cx)} {_fmt(cy)} 0) (effects (font (size {FONT} {FONT})) (hide yes)))'
            f' (property "Datasheet" "~" (at {_fmt(cx)} {_fmt(cy)} 0) (effects (font (size {FONT} {FONT})) (hide yes)))'
            f" {' '.join(pin_refs)}"
            f' (instances (project {_q(stem)} (path "/{root}" (reference {_q(name)}) (unit 1)))))'
        )

    body = "\n".join([*placed, *labels])
    return (
        f'(kicad_sch (version 20250114) (generator "vibepcb") (generator_version "1") (uuid {root})'
        f' (paper "A0")\n(lib_symbols\n{chr(10).join(lib)}\n)\n{body}\n'
        '(sheet_instances (path "/" (page "1"))) (embedded_fonts no))\n'
    )


def write(circuit_json: Path, sch_path: Path, stem: str) -> None:
    """Replace ``sch_path`` with the generated schematic (pretty-printed by the S-expression writer)."""
    sch_path.write_text(generate(circuit_json, stem), encoding="utf-8")
    sexpr.dump(sexpr.load(sch_path), sch_path)
