"""Schematic fixes: footprints, values, net labels, power symbols, no-connects, BOM fields."""

from __future__ import annotations

import copy
import itertools
import json
import math
import re
import uuid
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad import testpoints
from vibepcb.kicad.fixes.base import FOOTPRINT_LIB_NICKNAME, KicadProject, _footprints_by_ref, _mm, _num, _raw_circuit

if TYPE_CHECKING:
    from vibepcb.core.sexpr import Node


def fill_schematic_footprints(project: KicadProject) -> list[str]:
    """Copy each footprint's lib id from the PCB into the schematic symbol's empty Footprint field.

    Without it, KiCad's "Update PCB from Schematic" would delete every footprint.
    """
    footprints = _footprints_by_ref(project.pcb)
    changes = []
    for symbol in sexpr.children(project.sch, "symbol"):
        ref = sexpr.property_value(symbol, "Reference")
        prop = sexpr.property_node(symbol, "Footprint")
        fp = footprints.get(ref or "")
        if prop is None or fp is None or prop[2]:
            continue
        prop[2] = str(fp[1])
        changes.append(f"{ref}: Footprint = {fp[1]}")
    return changes


def fix_value_equals_reference(project: KicadProject) -> list[str]:
    """tscircuit sets Value = Reference for parts without a value (J1 -> "J1"); derive one from the footprint."""
    changes = []
    for fp in sexpr.children(project.pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference")
        prop = sexpr.property_node(fp, "Value")
        if prop is None or not ref or prop[2] != ref:
            continue
        value = str(fp[1]).split(":", 1)[-1]
        prop[2] = value
        for symbol in sexpr.children(project.sch, "symbol"):
            sym_value = sexpr.property_node(symbol, "Value")
            if sexpr.property_value(symbol, "Reference") == ref and sym_value is not None:
                sym_value[2] = value
        changes.append(f"{ref}: Value {ref!r} -> {value!r}")
    return changes


def link_footprint_library(project: KicadProject) -> list[str]:
    """Register the exported footprint library under the nickname the board uses.

    tscircuit names footprints ``tscircuit:<name>`` but exports the library as
    ``tscircuit_builtin``; a project-local fp-lib-table bridges the two.
    """
    pretty = next(project.directory.glob("lib/footprints/*.pretty"), None)
    table = project.directory / "fp-lib-table"
    if pretty is None or table.exists():
        return []
    uri = "${KIPRJMOD}/" + pretty.relative_to(project.directory).as_posix()
    tree: list[Node] = [
        Sym("fp_lib_table"),
        [Sym("version"), Sym("7")],
        [
            Sym("lib"),
            [Sym("name"), FOOTPRINT_LIB_NICKNAME],
            [Sym("type"), "KiCad"],
            [Sym("uri"), uri],
            [Sym("options"), ""],
            [Sym("descr"), "Footprints exported by tscircuit"],
        ],
    ]
    sexpr.dump(tree, table)
    return [f"fp-lib-table: {FOOTPRINT_LIB_NICKNAME} -> {uri}"]


LABEL_SNAP_MM = 3.0  # how far a net-name text may sit from the wire it names


def _wire_segments(sch: list[Node]) -> list[tuple[float, float, float, float]]:
    segments = []
    for wire in sexpr.children(sch, "wire"):
        pts = [(_num(xy, 1), _num(xy, 2)) for xy in sexpr.walk(wire) if sexpr.head(xy) == "xy"]
        segments += [(*a, *b) for a, b in itertools.pairwise(pts)]
    return segments


def _closest_on_segment(x: float, y: float, seg: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = seg
    length2 = (x2 - x1) ** 2 + (y2 - y1) ** 2
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - x1) * (x2 - x1) + (y - y1) * (y2 - y1)) / length2))
    return x1 + t * (x2 - x1), y1 + t * (y2 - y1)


# label angle -> justification KiCad itself uses for that orientation (text reads away from the anchor)
_LABEL_JUSTIFY = {0: "left", 90: "left", 180: "right", 270: "right"}


def _label_anchor(
    x: float, y: float, segments: list[tuple[float, float, float, float]], pins: set[tuple[float, float]]
) -> tuple[float, float, int] | None:
    """Anchor and angle for a net label whose text sits at ``(x, y)``: the free end of the nearest wire.

    tscircuit's net-label stubs run from a pin outwards; the label goes on the end away from
    the pin, reading away from it, so it doesn't cover pin numbers or names.
    """
    seg = min(segments, key=lambda s: math.dist((x, y), _closest_on_segment(x, y, s)))
    if math.dist((x, y), _closest_on_segment(x, y, seg)) > LABEL_SNAP_MM:
        return None
    a, b = (seg[0], seg[1]), (seg[2], seg[3])
    at_pin = [(round(p[0], 4), round(p[1], 4)) in pins for p in (a, b)]
    if at_pin[0] == at_pin[1]:  # wire between two pins (or two wires): keep the text's place
        px, py = _closest_on_segment(x, y, seg)
        return px, py, 0
    free, fixed = (b, a) if at_pin[0] else (a, b)
    dx, dy = free[0] - fixed[0], free[1] - fixed[1]
    # sheet y grows downwards: a stub going up gets 90 deg
    angle = (0 if dx >= 0 else 180) if abs(dx) >= abs(dy) else (90 if dy < 0 else 270)
    return free[0], free[1], angle


def schematic_net_labels(project: KicadProject) -> list[str]:
    """Turn net-name texts next to wires into real net labels.

    tscircuit draws a net-label stub as a wire plus a plain ``text`` with the net name, so
    in KiCad nothing connects through it and ERC reports the pins as unconnected. Each text
    naming a board net is replaced by a ``label`` on the free end of its stub (see
    :func:`_label_anchor`).
    """
    nets = {str(n[2]) for n in sexpr.children(project.pcb, "net") if len(n) > 2 and n[2]}
    segments = _wire_segments(project.sch)
    pins = set(_pin_positions(project.sch).values())
    changes = []
    for i, node in enumerate(project.sch):
        if sexpr.head(node) != "text" or not isinstance(node, list) or str(node[1]) not in nets or not segments:
            continue
        at = sexpr.child(node, "at")
        anchor = _label_anchor(_num(at, 1), _num(at, 2), segments, pins)
        if anchor is None:
            continue
        px, py, angle = anchor
        label = sexpr.loads(
            f'(label "" (at {_mm(px)} {_mm(py)} {angle}) (effects (font (size 1.27 1.27))'
            f" (justify {_LABEL_JUSTIFY[angle]} bottom)) (uuid {uuid.uuid4()}))"
        )
        label[1] = node[1]
        project.sch[i] = label
        changes.append(f"label {node[1]!r} at ({_mm(px)}, {_mm(py)})")
    return changes


def drop_loose_power_symbols(project: KicadProject) -> list[str]:
    """Remove power symbols whose pin touches no wire, label or other pin.

    tscircuit's schematic layout can leave a GND/supply rail symbol standing alone (seen after
    a part's custom symbol was dropped); it joins nothing, and KiCad's ERC reports its hidden
    power pin as unconnected.
    """
    pins = _pin_positions(project.sch)
    points: dict[tuple[float, float], int] = {}
    for x1, y1, x2, y2 in _wire_segments(project.sch):
        for q in ((round(x1, 4), round(y1, 4)), (round(x2, 4), round(y2, 4))):
            points[q] = points.get(q, 0) + 1
    for kind in ("label", "global_label", "hierarchical_label"):
        for node in sexpr.children(project.sch, kind):
            at = sexpr.child(node, "at")
            if at is not None:
                q = (round(_num(at, 1), 4), round(_num(at, 2), 4))
                points[q] = points.get(q, 0) + 1
    pin_count: dict[tuple[float, float], int] = {}
    for q in pins.values():
        pin_count[q] = pin_count.get(q, 0) + 1
    changes = []
    for symbol in list(sexpr.children(project.sch, "symbol")):
        ref = sexpr.property_value(symbol, "Reference") or ""
        if not ref.startswith("#PWR"):
            continue
        spots = [q for (r, _), q in pins.items() if r == ref]
        if spots and all(q not in points and pin_count[q] == 1 for q in spots):
            project.sch.remove(symbol)
            changes.append(f"{ref} ({sexpr.property_value(symbol, 'Value')}): loose power symbol removed")
    return changes


def _symbol_unit(name: str) -> int:
    """Unit of a library sub-symbol ``<name>_<unit>_<style>`` (0: drawn in every unit)."""
    m = re.search(r"_(\d+)_(\d+)$", name)
    return int(m[1]) if m else 0


def _pin_positions(sch: list[Node]) -> dict[tuple[str, str], tuple[float, float]]:
    """``(reference, pin number) -> (x, y)`` connection point of every placed symbol pin."""
    lib = sexpr.child(sch, "lib_symbols")
    lib_pins: dict[str, list[tuple[str, float, float, int]]] = {}  # unit 0: common to all units
    for sym in [] if lib is None else sexpr.children(lib, "symbol"):
        pins = lib_pins.setdefault(str(sym[1]), [])
        for sub in [sym, *sexpr.children(sym, "symbol")]:
            unit = _symbol_unit(str(sub[1])) if sub is not sym else 0
            pins += [
                (str(num[1]), _num(pin_at, 1), _num(pin_at, 2), unit)
                for pin in sexpr.children(sub, "pin")
                if (pin_at := sexpr.child(pin, "at")) is not None and (num := sexpr.child(pin, "number")) is not None
            ]
    positions: dict[tuple[str, str], tuple[float, float]] = {}
    for sym in sexpr.children(sch, "symbol"):
        ref, lib_id, at = sexpr.property_value(sym, "Reference"), sexpr.child(sym, "lib_id"), sexpr.child(sym, "at")
        if not ref or lib_id is None or at is None or sexpr.child(sym, "mirror") is not None:
            continue
        angle = math.radians(_num(at, 3))
        unit_node = sexpr.child(sym, "unit")
        unit = int(_num(unit_node, 1, 1)) if unit_node is not None else 1
        for number, px, py, pin_unit in lib_pins.get(str(lib_id[1]), []):
            if pin_unit not in {0, unit}:
                continue
            # library coordinates are y-up, the sheet is y-down
            x = _num(at, 1) + px * math.cos(angle) - py * math.sin(angle)
            y = _num(at, 2) - (px * math.sin(angle) + py * math.cos(angle))
            positions[ref, number] = (round(x, 4), round(y, 4))
    return positions


def schematic_no_connects(project: KicadProject) -> list[str]:
    """Put a no-connect flag on every schematic pin whose PCB pad has no net.

    tscircuit already warns about pins left without a trace, and marks NC pins in its own
    schematic, but exports no ``no_connect`` items, so KiCad ERC fails on every unused pin.
    """
    unused: set[tuple[str, str]] = set()
    for ref, fp in _footprints_by_ref(project.pcb).items():
        pads: dict[str, bool] = {}
        for pad in sexpr.children(fp, "pad"):
            if str(pad[1]):
                pads[str(pad[1])] = pads.get(str(pad[1]), False) or sexpr.child(pad, "net") is not None
        unused |= {(ref, number) for number, has_net in pads.items() if not has_net}

    taken = {
        (round(_num(at, 1), 4), round(_num(at, 2), 4))
        for nc in sexpr.children(project.sch, "no_connect")
        if (at := sexpr.child(nc, "at"))
    }
    for x1, y1, x2, y2 in _wire_segments(project.sch):
        taken |= {(round(x1, 4), round(y1, 4)), (round(x2, 4), round(y2, 4))}

    changes = []
    for (ref, number), (x, y) in sorted(_pin_positions(project.sch).items()):
        if (ref, number) not in unused or (x, y) in taken:
            continue
        project.sch.append(
            [Sym("no_connect"), [Sym("at"), Sym(_mm(x)), Sym(_mm(y))], [Sym("uuid"), Sym(str(uuid.uuid4()))]]
        )
        taken.add((x, y))
        changes.append(f"{ref} pin {number}: no-connect")
    return changes


RAIL_LIB_PREFIX = "Custom:rail_"  # tscircuit's GND (rail_down) / supply (rail_up) symbols


PWR_FLAG_LIB = """(symbol "power:PWR_FLAG" (power) (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes))
  (exclude_from_sim no) (in_bom yes) (on_board yes)
  (property "Reference" "#FLG" (at 0 1.905 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Value" "PWR_FLAG" (at 0 3.81 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Description" "Special symbol for telling ERC where power comes from" (at 0 0 0)
    (effects (font (size 1.27 1.27)) (hide yes)))
  (symbol "PWR_FLAG_0_0" (pin power_out line (at 0 0 90) (length 0)
    (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27))))))
  (symbol "PWR_FLAG_0_1" (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27))
    (stroke (width 0) (type default)) (fill (type none))))
  (embedded_fonts no))"""


def _hide(prop: list[Node], *, hidden: bool) -> None:
    effects = sexpr.child(prop, "effects")
    if effects is None:
        return
    effects[:] = [x for x in effects if sexpr.head(x) != "hide"]
    if hidden:
        effects.append([Sym("hide"), Sym("yes")])


def schematic_power_symbols(project: KicadProject) -> list[str]:
    """Make tscircuit's GND / supply rail symbols real KiCad power symbols, plus one PWR_FLAG per rail.

    They are exported as ordinary parts (Reference = net name, in BOM, on board) with a
    passive pin, so every rail symbol is a net of its own: the schematic has a dozen GND
    nets and "Update PCB from Schematic" would add footprint-less parts. As power symbols
    (hidden power-input pin, ``#PWR`` reference, Value = net) they join globally by name.
    Every pin in tscircuit's export is passive, so nothing drives the rails; a PWR_FLAG on
    each tells ERC the rail is supplied (by the USB connector / regulator).
    """
    lib = sexpr.child(project.sch, "lib_symbols")
    rails = [s for s in ([] if lib is None else sexpr.children(lib, "symbol")) if str(s[1]).startswith(RAIL_LIB_PREFIX)]
    if lib is None or not rails or all(sexpr.child(s, "power") is not None for s in rails):
        return []
    for rail in rails:
        if sexpr.child(rail, "power") is None:
            rail.insert(2, [Sym("power")])
        for pin in (n for n in sexpr.walk(rail) if sexpr.head(n) == "pin"):
            pin[1] = Sym("power_in")
            if sexpr.child(pin, "hide") is None:
                pin.append([Sym("hide"), Sym("yes")])

    changes = []
    instances = [
        s
        for s in sexpr.children(project.sch, "symbol")
        if (lib_id := sexpr.child(s, "lib_id")) is not None and str(lib_id[1]).startswith(RAIL_LIB_PREFIX)
    ]
    for n, symbol in enumerate(instances, start=1):
        ref, net = f"#PWR{n:02d}", sexpr.property_value(symbol, "Value") or "?"
        ref_prop, value_prop = sexpr.property_node(symbol, "Reference"), sexpr.property_node(symbol, "Value")
        if ref_prop is None or value_prop is None:
            continue
        ref_prop[2] = ref
        _hide(ref_prop, hidden=True)
        _hide(value_prop, hidden=False)
        for item in ("in_bom", "on_board"):
            if (flag := sexpr.child(symbol, item)) is not None:
                flag[1] = Sym("no")
        for reference in (x for x in sexpr.walk(symbol) if sexpr.head(x) == "reference"):
            reference[1] = ref
        changes.append(f"{ref}: power symbol {net!r}")

    lib.append(sexpr.loads(PWR_FLAG_LIB))
    pins = _pin_positions(project.sch)
    flagged: set[str] = set()
    for symbol in instances:
        rail_net, rail_ref = sexpr.property_value(symbol, "Value"), sexpr.property_value(symbol, "Reference")
        path = next((x for x in sexpr.walk(symbol) if sexpr.head(x) == "path"), None)
        if not rail_net or not rail_ref or rail_net in flagged or (rail_ref, "1") not in pins or path is None:
            continue
        flagged.add(rail_net)
        x, y = (_mm(v) for v in pins[rail_ref, "1"])
        flag_ref = f"#FLG{len(flagged):02d}"
        project.sch.append(
            sexpr.loads(
                f'(symbol (lib_id "power:PWR_FLAG") (at {x} {y} 0) (unit 1) (exclude_from_sim no) (in_bom yes)'
                f" (on_board yes) (dnp no) (uuid {uuid.uuid4()})"
                f' (property "Reference" "{flag_ref}" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))'
                f' (property "Value" "PWR_FLAG" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))'
                f' (property "Footprint" "" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))'
                f' (property "Datasheet" "~" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))'
                f' (pin "1" (uuid {uuid.uuid4()}))'
                f' (instances (project "" (path "{path[1]}" (reference "{flag_ref}") (unit 1)))))'
            )
        )
        changes.append(f"{flag_ref}: PWR_FLAG on {rail_net!r}")
    return changes


def add_part_numbers(project: KicadProject) -> list[str]:
    """Copy ``manufacturerPartNumber`` / JLCPCB part numbers from the circuit JSON onto the symbols.

    tscircuit's KiCad export drops them, so the KiCad BOM could not say what to buy.
    Adds hidden ``MPN`` and ``LCSC`` fields (read from ``../raw/<stem>.circuit.json``) and
    uses the MPN as Value where the export left it empty.
    """
    circuit = _raw_circuit(project)
    if not circuit.exists():
        return []
    parts: dict[str, dict[str, str]] = {}
    for element in json.loads(circuit.read_text(encoding="utf-8")):
        if element.get("type") != "source_component":
            continue
        fields = {}
        if mpn := element.get("manufacturer_part_number"):
            fields["MPN"] = str(mpn)
        if lcsc := (element.get("supplier_part_numbers") or {}).get("jlcpcb"):
            fields["LCSC"] = str(lcsc[0])
        if fields:
            parts[str(element.get("name"))] = fields

    changes = []
    for symbol in sexpr.children(project.sch, "symbol"):
        ref = sexpr.property_value(symbol, "Reference") or ""
        template = sexpr.property_node(symbol, "Datasheet")
        if ref not in parts or template is None:
            continue
        for name, value in parts[ref].items():
            if sexpr.property_node(symbol, name) is not None:
                continue
            prop = [item for item in copy.deepcopy(template) if sexpr.head(item) != "id"]
            prop[1], prop[2] = name, value
            effects = sexpr.child(prop, "effects")
            if effects is not None and sexpr.child(effects, "hide") is None:
                effects.append([Sym("hide"), Sym("yes")])
            symbol.insert(symbol.index(template) + 1, prop)
            changes.append(f"{ref}: {name} = {value}")
        value_prop = sexpr.property_node(symbol, "Value")
        if value_prop is not None and not value_prop[2] and "MPN" in parts[ref]:  # e.g. LEDs export no value
            value_prop[2] = parts[ref]["MPN"]
            changes.append(f"{ref}: Value = {value_prop[2]}")
    return changes


def mark_dnp_parts(project: KicadProject) -> list[str]:
    """Keep tscircuit's ``doNotPlace`` parts (bare pads: ISP headers, antenna / touch pads) out of BOM and CPL.

    Neither the KiCad export nor ``schgen`` carries the flag, so those pads landed in the fab BOM and CPL. They
    are board features, not parts left off: the symbol goes ``in_bom no`` and the footprint ``board_only`` /
    out of BOM and position files, like a test point. (A KiCad DNP flag drew a big red cross over each of them
    in the schematic, as if the antenna or the touch pad were missing from the board.)
    """
    circuit = _raw_circuit(project)
    if not circuit.exists():
        return []
    elements = json.loads(circuit.read_text(encoding="utf-8"))
    names = {e["source_component_id"]: str(e.get("name")) for e in elements if e.get("type") == "source_component"}
    unplaced = {
        names[e["source_component_id"]]
        for e in elements
        if e.get("type") == "pcb_component" and e.get("do_not_place") and e.get("source_component_id") in names
    }
    changes = []
    for symbol in sexpr.children(project.sch, "symbol"):
        ref = sexpr.property_value(symbol, "Reference") or ""
        if ref not in unplaced:
            continue
        flag, dnp = sexpr.child(symbol, "in_bom"), sexpr.child(symbol, "dnp")
        changed = False
        if flag is not None and flag[1] != Sym("no"):
            flag[1] = Sym("no")
            changed = True
        if dnp is not None and dnp[1] != Sym("no"):
            dnp[1] = Sym("no")
            changed = True
        if changed:
            changes.append(f"{ref}: symbol out of the BOM (board feature)")
    wanted = [Sym("board_only"), Sym("exclude_from_pos_files"), Sym("exclude_from_bom")]
    for fp in sexpr.children(project.pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or ""
        if ref not in unplaced:
            continue
        attr = sexpr.child(fp, "attr")
        if attr is None:
            props = [i for i, item in enumerate(fp) if sexpr.head(item) == "property"]
            fp.insert(props[-1] + 1 if props else len(fp), [Sym("attr"), *wanted])
        elif missing := [w for w in wanted if w not in attr]:
            attr.extend(missing)
        else:
            continue
        changes.append(f"{ref}: footprint board_only, out of BOM and CPL")
    return changes


def exclude_testpoints_from_bom(project: KicadProject) -> list[str]:
    """Keep ``<testpoint>`` pads out of the fab BOM and CPL: a bare pad is nothing to buy or place.

    The export gives the test point a symbol "in BOM" and a footprint without attributes, so JLCPCB
    would list it as a part. The symbol goes ``in_bom no``; the footprint gets
    ``(attr board_only exclude_from_pos_files exclude_from_bom)`` (not DNP: the pad is meant to be on the board).
    """
    refs = {p.ref for p in testpoints.parts(project.pcb) if testpoints.is_test_point(p)}
    changes = []
    for symbol in sexpr.children(project.sch, "symbol"):
        flag = sexpr.child(symbol, "in_bom")
        if (sexpr.property_value(symbol, "Reference") or "") in refs and flag is not None and flag[1] != Sym("no"):
            flag[1] = Sym("no")
            changes.append(f"{sexpr.property_value(symbol, 'Reference')}: symbol out of the BOM")
    wanted = [Sym("board_only"), Sym("exclude_from_pos_files"), Sym("exclude_from_bom")]
    for fp in sexpr.children(project.pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or ""
        if ref not in refs:
            continue
        attr = sexpr.child(fp, "attr")
        if attr is None:
            props = [i for i, item in enumerate(fp) if sexpr.head(item) == "property"]
            fp.insert(props[-1] + 1 if props else len(fp), [Sym("attr"), *wanted])
        elif missing := [w for w in wanted if w not in attr]:
            attr.extend(missing)
        else:
            continue
        changes.append(f"{ref}: footprint board_only, out of BOM and CPL")
    return changes
