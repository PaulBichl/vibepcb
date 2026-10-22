from __future__ import annotations

import copy
import itertools
import json
import math
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibepcb import config, fixes, kicad, sexpr, tracks, tscircuit
from vibepcb.sexpr import Sym

if TYPE_CHECKING:
    from vibepcb.sexpr import Node

DATA = Path(__file__).parent / "data"


@pytest.fixture
def project(tmp_path: Path) -> fixes.KicadProject:
    """Like out/<board>/kicad: the project sits next to a (here empty) ``raw`` sibling's place."""
    directory = tmp_path / "kicad"
    directory.mkdir()
    for suffix in (".kicad_pcb", ".kicad_sch"):
        shutil.copy(DATA / f"led-indicator{suffix}", directory / f"board{suffix}")
    return fixes.KicadProject.load(directory, "board")


def _net_names(tree: list[Node]) -> set[str]:
    return {str(n[2]) for n in sexpr.walk(tree) if sexpr.head(n) == "net" and len(n) > 2}


def test_rename_auto_nets(project: fixes.KicadProject) -> None:
    assert ".R1 > .pin2 to .D1 > .anode" in _net_names(project.pcb)
    changes = fixes.rename_auto_nets(project)
    assert changes == ["net '.R1 > .pin2 to .D1 > .anode' -> 'Net-(R1-Pad2)'"]
    assert _net_names(project.pcb) == {"", "VCC", "GND", "Net-(R1-Pad2)"}  # top-level nets and pads
    assert fixes.rename_auto_nets(project) == []  # idempotent


def test_rename_auto_nets_uses_schematic_names(project: fixes.KicadProject) -> None:
    # with the circuit JSON next to it, the PCB gets the generated schematic's net names
    raw = project.directory.parent / "raw"
    raw.mkdir()
    circuit = [
        {"type": "source_component", "source_component_id": "c1", "name": "R1", "ftype": "simple_resistor"},
        {"type": "source_port", "source_port_id": "p1", "source_component_id": "c1", "name": "pin2", "pin_number": 2,
         "subcircuit_connectivity_map_key": "k"},
        {"type": "source_component", "source_component_id": "c2", "name": "U9", "ftype": "simple_chip"},
        *({"type": "source_port", "source_port_id": f"u{i}", "source_component_id": "c2", "name": name,
           "pin_number": i} for i, name in enumerate(("A", "B"), start=1)),
        {"type": "source_port", "source_port_id": "u3", "source_component_id": "c2", "name": "SENSE", "pin_number": 3,
         "subcircuit_connectivity_map_key": "k"},
        {"type": "source_trace", "connected_source_port_ids": ["p1", "u3"]},
    ]  # fmt: skip
    (raw / "board.circuit.json").write_text(json.dumps(circuit), encoding="utf-8")
    assert fixes.rename_auto_nets(project) == ["net '.R1 > .pin2 to .D1 > .anode' -> 'U9_SENSE'"]


def test_propagate_split_pad_nets(project: fixes.KicadProject) -> None:
    assert fixes.propagate_split_pad_nets(project) == []  # no split pads in the fixture
    fp = next(f for f in sexpr.children(project.pcb, "footprint") if sexpr.property_value(f, "Reference") == "R1")
    pad = next(sexpr.children(fp, "pad"))
    twin = [item for item in copy.deepcopy(pad) if sexpr.head(item) != "net"]
    fp.append(twin)
    assert fixes.propagate_split_pad_nets(project) == [
        f"R1 pad {pad[1]}: net 'VCC'",
        "R1: duplicate pad numbers are jumpers",
    ]
    assert sexpr.child(twin, "net") == sexpr.child(pad, "net")
    assert fixes.propagate_split_pad_nets(project) == []  # idempotent


def test_drop_tracks_inside_pads(project: fixes.KicadProject) -> None:
    assert fixes.drop_tracks_inside_pads(project) == []  # every fixture track leaves its pad

    def segment(end_x: float, end_y: float, net: int) -> Node:
        node = sexpr.loads(f"(segment (start 92.5 100) (end {end_x} {end_y}) (width 0.05) (layer F.Cu) (net {net}))")
        project.pcb.append(node)
        return node

    # J1 pad 1 (VCC = net 1) is a 1.5 mm square centred on (92.73, 100)
    stub, leaving, foreign = segment(93, 100.3, 1), segment(95, 100, 1), segment(93, 100.3, 2)
    # an SMD pad does not reach a track on the other copper layer
    fp = next(f for f in sexpr.children(project.pcb, "footprint") if sexpr.property_value(f, "Reference") == "R1")
    smd = next(sexpr.children(fp, "pad"))
    at = sexpr.child(fp, "at")
    pad_at = sexpr.child(smd, "at")
    assert at is not None
    assert pad_at is not None
    x, y = float(str(at[1])) + float(str(pad_at[1])), float(str(at[2]))
    other_side = sexpr.loads(f"(segment (start {x} {y}) (end {x + 0.1} {y}) (width 0.1) (layer B.Cu) (net 1))")
    project.pcb.append(other_side)
    assert fixes.drop_tracks_inside_pads(project) == ["J1: dropped 1 track stub(s) inside its own pads"]
    assert stub not in project.pcb
    assert leaving in project.pcb
    assert foreign in project.pcb  # other net: a real short, not ours to hide
    assert other_side in project.pcb
    assert fixes.drop_tracks_inside_pads(project) == []


def test_drop_tracks_inside_round_pads(project: fixes.KicadProject) -> None:
    # J1 pad 2 (GND = net 2) is a 1.5 mm circle centred on (95.27, 100)
    def segment(end_x: float, end_y: float) -> Node:
        node = sexpr.loads(f"(segment (start 95.27 100) (end {end_x} {end_y}) (width 0.15) (layer F.Cu) (net 2))")
        project.pcb.append(node)
        return node

    # leaves diagonally: 0.99 mm from the centre, past the circle but inside its bounding box
    leaving = segment(95.97, 100.7)
    assert fixes.drop_tracks_inside_pads(project) == []
    assert leaving in project.pcb
    stub = segment(95.6, 100.3)
    assert fixes.drop_tracks_inside_pads(project) == ["J1: dropped 1 track stub(s) inside its own pads"]
    assert stub not in project.pcb


def _edge_cuts(tree: list[Node]) -> int:
    return sum(1 for n in sexpr.children(tree, "gr_line") if sexpr.child(n, "layer") == ["layer", "Edge.Cuts"])


def test_drop_npth_edge_cuts(project: fixes.KicadProject) -> None:
    outline = _edge_cuts(project.pcb)
    assert fixes.drop_npth_edge_cuts(project) == []
    # J1 sits at (94, 100); rotate it 90 deg and give it a 1 mm NPTH at local (2, 0) -> board (94, 98)
    fp = next(f for f in sexpr.children(project.pcb, "footprint") if sexpr.property_value(f, "Reference") == "J1")
    at = sexpr.child(fp, "at")
    assert at is not None
    at[3] = Sym("90")
    fp.append(sexpr.loads('(pad "" np_thru_hole circle (at 2 0 0) (size 1 1) (drill 1) (layers *.Cu *.Mask))'))
    rim = [(94.5, 98), (94, 97.5), (93.5, 98), (94, 98.5), (94.5, 98)]
    for (x1, y1), (x2, y2) in itertools.pairwise(rim):
        project.pcb.append(sexpr.loads(f"(gr_line (start {x1} {y1}) (end {x2} {y2}) (width 0.1) (layer Edge.Cuts))"))
    assert _edge_cuts(project.pcb) == outline + 4
    assert fixes.drop_npth_edge_cuts(project) == ["J1: dropped 4 Edge.Cuts segments duplicating its NPTH holes"]
    assert _edge_cuts(project.pcb) == outline
    assert fixes.drop_npth_edge_cuts(project) == []


def test_allow_footprints_in_keepouts(project: fixes.KicadProject) -> None:
    assert fixes.allow_footprints_in_keepouts(project) == []
    zone = sexpr.loads(
        '(zone (net 0) (net_name "") (layers F.Cu B.Cu)'
        " (keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed)"
        " (copperpour not_allowed) (footprints not_allowed))"
        " (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))"
    )
    project.pcb.append(zone)
    assert fixes.allow_footprints_in_keepouts(project) == ["keepout zone: footprints and pads allowed"]
    keepout = sexpr.child(zone, "keepout")
    assert keepout is not None
    assert sexpr.child(keepout, "footprints") == ["footprints", "allowed"]
    assert sexpr.child(keepout, "pads") == ["pads", "allowed"]
    assert sexpr.child(keepout, "tracks") == ["tracks", "not_allowed"]
    assert fixes.allow_footprints_in_keepouts(project) == []


def test_standard_board_thickness(project: fixes.KicadProject) -> None:
    assert fixes.standard_board_thickness(project)
    general = sexpr.child(project.pcb, "general")
    assert general is not None
    assert sexpr.child(general, "thickness") == ["thickness", "1.6"]
    assert fixes.standard_board_thickness(project) == []


def test_fill_schematic_footprints(project: fixes.KicadProject) -> None:
    assert len(fixes.fill_schematic_footprints(project)) == 3
    footprints = {
        sexpr.property_value(s, "Reference"): sexpr.property_value(s, "Footprint")
        for s in sexpr.children(project.sch, "symbol")
    }
    assert footprints["R1"] == "tscircuit:resistor_res0603"


def test_fix_value_equals_reference(project: fixes.KicadProject) -> None:
    assert fixes.fix_value_equals_reference(project) == [
        "J1: Value 'J1' -> 'pin_header_pinrow2_p2_54_nopinlabels'",
    ]


def test_link_footprint_library(project: fixes.KicadProject) -> None:
    assert fixes.link_footprint_library(project) == []  # no exported library -> nothing to link
    (project.directory / "lib/footprints/x.pretty").mkdir(parents=True)
    assert fixes.link_footprint_library(project)
    table = sexpr.load(project.directory / "fp-lib-table")
    lib = sexpr.child(table, "lib")
    assert lib is not None
    assert sexpr.child(lib, "uri") == ["uri", "${KIPRJMOD}/lib/footprints/x.pretty"]


def test_apply_all_saves_loadable_files(project: fixes.KicadProject) -> None:
    fixes.apply_all(project)
    reloaded = fixes.KicadProject.load(project.directory, "board")
    assert "Net-(R1-Pad2)" in _net_names(reloaded.pcb)


def test_footprint_internal_clearance_rule(project: fixes.KicadProject) -> None:
    assert fixes.footprint_internal_clearance_rule(project) == [
        "board.kicad_dru: footprint-internal hole clearance >= 0.15 mm",
    ]
    rules = (project.directory / "board.kicad_dru").read_text(encoding="utf-8")
    assert '(condition "A.Parent == B.Parent")' in rules
    assert fixes.footprint_internal_clearance_rule(project) == []  # never overwrites a rules file


def test_add_part_numbers(project: fixes.KicadProject) -> None:
    assert fixes.add_part_numbers(project) == []  # no circuit JSON next to the project
    raw = project.directory.parent / "raw"
    raw.mkdir()
    circuit = [
        {"type": "source_component", "name": "D1", "manufacturer_part_number": "KT-0603R",
         "supplier_part_numbers": {"jlcpcb": ["C2286"]}},
        {"type": "source_component", "name": "R1", "supplier_part_numbers": {"jlcpcb": ["C25744"]}},
        {"type": "source_port", "name": "pin1"},
    ]  # fmt: skip
    (raw / "board.circuit.json").write_text(json.dumps(circuit), encoding="utf-8")
    d1 = next(s for s in sexpr.children(project.sch, "symbol") if sexpr.property_value(s, "Reference") == "D1")
    value = sexpr.property_node(d1, "Value")
    assert value is not None
    value[2] = ""  # tscircuit exports LEDs without a value
    assert fixes.add_part_numbers(project) == [
        "R1: LCSC = C25744",
        "D1: MPN = KT-0603R",
        "D1: LCSC = C2286",
        "D1: Value = KT-0603R",
    ]
    assert sexpr.property_value(d1, "LCSC") == "C2286"
    assert fixes.add_part_numbers(project) == []


def test_mark_dnp_parts(project: fixes.KicadProject) -> None:
    assert fixes.mark_dnp_parts(project) == []  # no circuit JSON next to the project
    raw = project.directory.parent / "raw"
    raw.mkdir()
    circuit = [
        {"type": "source_component", "source_component_id": "s1", "name": "D1"},
        {"type": "source_component", "source_component_id": "s2", "name": "R1"},
        {"type": "pcb_component", "source_component_id": "s1", "do_not_place": True},
        {"type": "pcb_component", "source_component_id": "s2", "do_not_place": False},
    ]  # fmt: skip
    (raw / "board.circuit.json").write_text(json.dumps(circuit), encoding="utf-8")
    assert fixes.mark_dnp_parts(project) == ["D1: symbol DNP", "D1: footprint DNP"]
    d1 = next(s for s in sexpr.children(project.sch, "symbol") if sexpr.property_value(s, "Reference") == "D1")
    assert sexpr.child(d1, "dnp") == [Sym("dnp"), Sym("yes")]
    fp = next(f for f in sexpr.children(project.pcb, "footprint") if sexpr.property_value(f, "Reference") == "D1")
    assert sexpr.child(fp, "attr") == [Sym("attr"), Sym("dnp")]
    assert fixes.mark_dnp_parts(project) == []


ZONE = """(zone (net 2) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
  (connect_pads yes (clearance 0.15)) (min_thickness 0.25)
  (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
  (polygon (pts (xy 90 90) (xy 110 90) (xy 110 110) (xy 90 110))))"""


def test_zone_thermal_reliefs(project: fixes.KicadProject) -> None:
    assert fixes.zone_thermal_reliefs(project) == []  # the fixture has no zones
    zone = sexpr.loads(ZONE)
    keepout = sexpr.loads(ZONE.replace("(min_thickness", "(keepout (tracks not_allowed)) (min_thickness"))
    project.pcb += [zone, keepout]
    assert fixes.zone_thermal_reliefs(project) == ["zone GND on F.Cu: thermal reliefs"]
    assert sexpr.child(zone, "connect_pads") == [Sym("connect_pads"), [Sym("clearance"), Sym("0.15")]]
    fill = sexpr.child(zone, "fill")
    assert fill is not None
    assert sexpr.child(fill, "thermal_gap") == [Sym("thermal_gap"), Sym("0.3")]
    assert sexpr.child(fill, "thermal_bridge_width") == [Sym("thermal_bridge_width"), Sym("0.3")]
    assert sexpr.child(keepout, "connect_pads") == [Sym("connect_pads"), Sym("yes"), [Sym("clearance"), Sym("0.15")]]
    assert fixes.zone_thermal_reliefs(project) == []  # idempotent


def test_place_reference_designators(project: fixes.KicadProject) -> None:
    # tscircuit writes a visible 0.4 mm fp_text reference next to the hidden Reference property
    changes = fixes.place_reference_designators(project)
    assert sorted(c.split(":")[0] for c in changes) == ["D1", "J1", "R1"]
    for fp in sexpr.children(project.pcb, "footprint"):
        geometry = fixes._footprint_geometry(fp)
        assert geometry is not None
        text, at = geometry.prop, sexpr.child(geometry.prop, "at")
        assert sexpr.head(text) == "fp_text"
        effects = sexpr.child(text, "effects")
        font = None if effects is None else sexpr.child(effects, "font")
        assert font is not None
        assert sexpr.child(font, "size") == [Sym("size"), Sym("0.8"), Sym("0.8")]
        x, y = fixes._fp_to_board(fp)(fixes._num(at, 1), fixes._num(at, 2))
        box = fixes._text_box(x, y, geometry.ref, fixes.REF_TEXT_SIZE_MM, fixes._num(at, 3))
        assert not any(fixes._overlaps(box, keepout) for keepout in geometry.keepouts), geometry.ref
    assert fixes.place_reference_designators(project) == []  # idempotent


def test_fit_schematic_sheet(project: fixes.KicadProject) -> None:
    paper = sexpr.child(project.sch, "paper")
    assert paper is not None
    paper[1] = Sym("A0")  # tscircuit's oversized sheet
    pins_before = fixes._pin_positions(project.sch)
    changes = fixes.fit_schematic_sheet(project)
    assert changes[0] == "paper A0 -> A4"
    assert changes[-1] == "title block: board"
    assert str(paper[1]) == "A4"
    block = sexpr.child(project.sch, "title_block")
    assert block is not None
    assert sexpr.child(block, "title") == [Sym("title"), "board"]
    # everything moved together, by whole grid steps, onto the sheet
    pins_after = fixes._pin_positions(project.sch)
    shifts = {
        (round(a[0] - b[0], 3), round(a[1] - b[1], 3))
        for a, b in zip(pins_after.values(), pins_before.values(), strict=True)
    }
    assert len(shifts) == 1
    (dx, dy), *_ = shifts
    assert round(dx / fixes.SCHEMATIC_GRID_MM, 6).is_integer()
    assert round(dy / fixes.SCHEMATIC_GRID_MM, 6).is_integer()
    assert all(0 < x < 297 and 0 < y < 210 for x, y in pins_after.values())
    assert fixes.fit_schematic_sheet(project) == []  # idempotent


def test_thermal_spoke_rule(project: fixes.KicadProject) -> None:
    assert fixes.footprint_internal_clearance_rule(project)  # creates the rules file first
    assert fixes.thermal_spoke_rule(project) == ["board.kicad_dru: min. 1 thermal spoke per pad"]
    text = project.pcb_path.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert text.count("(version 1)") == 1
    assert "footprint-internal hole clearance" in text
    assert "(constraint min_resolved_spokes 1)" in text
    assert fixes.thermal_spoke_rule(project) == []  # idempotent


def test_router_clearance_tolerance(project: fixes.KicadProject) -> None:
    assert fixes.thermal_spoke_rule(project)
    # no project file in the fixture: the default 0.2 mm clearance applies
    assert fixes.router_clearance_tolerance(project) == ["board.kicad_dru: copper clearance >= 0.195 mm"]
    text = project.pcb_path.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert text.count("(version 1)") == 1
    assert "(constraint min_resolved_spokes 1)" in text
    assert "A.NetClass == 'Default' && B.NetClass == 'Default'" in text
    assert "(constraint clearance (min 0.195mm))" in text
    assert fixes.router_clearance_tolerance(project) == []  # idempotent


def test_schematic_net_labels_sit_on_free_stub_end(project: fixes.KicadProject) -> None:
    (ref, number), (px, py) = next(iter(fixes._pin_positions(project.sch).items()))
    # a tscircuit net-label stub: wire from the pin 5 mm to the left, plain text near the pin
    project.sch.append(sexpr.loads(f"(wire (pts (xy {px} {py}) (xy {px - 5} {py})) (stroke (width 0) (type default)))"))
    project.sch.append(sexpr.loads(f'(text "VCC" (at {px - 1} {py} 0) (effects (font (size 1 1))))'))
    changes = fixes.schematic_net_labels(project)
    assert changes == [f"label 'VCC' at ({fixes._mm(px - 5)}, {fixes._mm(py)})"], (ref, number)
    label = next(sexpr.children(project.sch, "label"))
    assert sexpr.child(label, "at") == [Sym("at"), Sym(fixes._mm(px - 5)), Sym(fixes._mm(py)), Sym("180")]
    effects = sexpr.child(label, "effects")
    assert effects is not None
    assert sexpr.child(effects, "justify") == [Sym("justify"), Sym("right"), Sym("bottom")]
    assert fixes.schematic_net_labels(project) == []  # idempotent: the text is gone


def test_label_anchor_directions() -> None:
    pins = {(0.0, 0.0)}
    assert fixes._label_anchor(1, 0, [(0, 0, 4, 0)], pins) == (4, 0, 0)
    assert fixes._label_anchor(0, -1, [(0, -4, 0, 0)], pins) == (0, -4, 90)  # stub going up
    assert fixes._label_anchor(0, 1, [(0, 0, 0, 4)], pins) == (0, 4, 270)
    assert fixes._label_anchor(9, 9, [(0, 0, 4, 0)], pins) is None  # too far from any wire


def _pad_groups(project: fixes.KicadProject) -> dict[str, list[set[int]]]:
    copper = fixes._copper(project.pcb)
    out = {}
    for net in {s.net for s in copper.segs}:
        pads = [o for o in copper.pads if o.net == net]
        vias = [(c, r) for _, c, r, n in copper.vias if n == net]
        out[net] = tracks.pad_groups(pads, vias, [s for s in copper.segs if s.net == net])
    return out


def test_clean_tracks_keeps_connectivity_and_removes_sharp_bends(project: fixes.KicadProject) -> None:
    before = _pad_groups(project)
    fixes.clean_tracks(project)
    after = _pad_groups(project)
    assert all(tracks.still_connected(before[net], after[net]) for net in before)
    assert fixes.sharp_corners(project) == []
    assert fixes.clean_tracks(project) == []  # idempotent


def test_clean_tracks_chamfers_a_right_angle(project: fixes.KicadProject) -> None:
    fixes.clean_tracks(project)
    copper = fixes._copper(project.pcb)
    s = copper.segs[0]
    # turn the first track into an L with a 90 degree bend in free space
    corner = (s.b[0], s.b[1] - 3)
    node = fixes._source(s)
    project.pcb[project.pcb.index(node) : project.pcb.index(node) + 1] = [
        fixes._segment_node(node, s.a, (s.a[0], corner[1]), s.width),
        fixes._segment_node(node, (s.a[0], corner[1]), corner, s.width),
        fixes._segment_node(node, corner, s.b, s.width),
    ]
    assert fixes.sharp_corners(project)
    assert any("sharp corner" in c for c in fixes.clean_tracks(project))
    assert fixes.sharp_corners(project) == []


def test_strict_track_rules(project: fixes.KicadProject) -> None:
    pro = project.pcb_path.with_suffix(".kicad_pro")
    pro.write_text(
        json.dumps({"board": {"design_settings": {"rule_severities": {"track_dangling": "warning"}}}}), encoding="utf-8"
    )
    changes = fixes.strict_track_rules(project)
    assert changes == [
        "board.kicad_dru: track angles >= 89 deg",
        "board.kicad_pro: track_angle, track_dangling, via_dangling -> error",
    ]
    assert "(constraint track_angle (min 89))" in project.pcb_path.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    severities = json.loads(pro.read_text(encoding="utf-8"))["board"]["design_settings"]["rule_severities"]
    assert severities == {"track_dangling": "error", "via_dangling": "error", "track_angle": "error"}
    assert fixes.strict_track_rules(project) == []  # idempotent


def test_remove_antennas_keeps_inner_layer_connections(project: fixes.KicadProject) -> None:
    """A via joining an F.Cu track to an In2.Cu track (4-layer board) is a real connection."""
    copper = fixes._copper(project.pcb)
    tht = next(o for o in copper.pads if o.net and {"F.Cu", "B.Cu"} <= o.layers)  # through-hole
    smd = next(o for o in copper.pads if o.net == tht.net and o.layers == {"F.Cu"})
    net = next(n[1] for n in sexpr.children(project.pcb, "net") if len(n) > 2 and str(n[2]) == tht.net)
    (sx, sy), (tx, ty) = smd.centre, tht.centre
    vx, vy = (sx + tx) / 2, (sy + ty) / 2 + 3
    for layer, (ax, ay), (bx, by) in (("F.Cu", (sx, sy), (vx, vy)), ("In2.Cu", (vx, vy), (tx, ty))):
        project.pcb.append(
            sexpr.loads(f"(segment (start {ax} {ay}) (end {bx} {by}) (width 0.2) (layer {layer}) (net {net}))")
        )
    project.pcb.append(sexpr.loads(f"(via (at {vx} {vy}) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net {net}))"))
    before = len(list(sexpr.children(project.pcb, "segment"))), len(list(sexpr.children(project.pcb, "via")))

    fixes.remove_antennas(project)

    after = len(list(sexpr.children(project.pcb, "segment"))), len(list(sexpr.children(project.pcb, "via")))
    assert after == before


def test_prune_pour_tracks_drops_tracks_inside_the_fill(project: fixes.KicadProject) -> None:
    copper = fixes._copper(project.pcb)
    target = copper.segs[0]
    xs, ys = (target.a[0], target.b[0]), (target.a[1], target.b[1])
    x0, y0, x1, y1 = min(xs) - 5, min(ys) - 5, max(xs) + 5, max(ys) + 5
    zone = sexpr.loads(
        f'(zone (net "{target.net}") (layer "{target.layer}") (connect_pads (clearance 0.2)) (min_thickness 0.25)'
        f" (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))"
        f" (polygon (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})))"
        f' (filled_polygon (layer "{target.layer}") (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))))'
    )
    project.pcb.append(zone)
    changes = fixes.prune_pour_tracks(project)
    assert changes[0].startswith("pours: removed")
    assert fixes._source(target) not in project.pcb
    kept = fixes._copper(project.pcb).segs
    assert all(
        s.net != target.net
        or s.layer != target.layer
        or not all(tracks.point_in_polygon(p, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]) for p in (s.a, s.b))
        for s in kept
    )
    # a keep-near spot protects the tracks around it
    project2 = fixes.KicadProject.load(project.directory, "board")
    project2.pcb.append(copy.deepcopy(zone))
    fixes.prune_pour_tracks(project2, keep_near=[target.a, target.b])
    assert any(s.a == target.a and s.b == target.b for s in fixes._copper(project2.pcb).segs)


def test_jlcpcb_files(tmp_path: Path) -> None:
    bom, pos = tmp_path / "b-bom.csv", tmp_path / "b-pos.csv"
    bom.write_text(
        '"Qty","Designator","Value","Footprint","MPN","LCSC Part #"\n"2","R1,R2","1k","tscircuit:res0402","","C11702"\n',
        encoding="utf-8",
    )
    pos.write_text(
        'Ref,Val,Package,PosX,PosY,Rot,Side\n"","","hole",1,2,0,top\n"R1","1k","res0402",10.5,-20.25,90,top\n',
        encoding="utf-8",
    )
    out = kicad.jlcpcb_files(bom, pos, tmp_path / "fab", "b")
    assert [p.name for p in out] == ["b-bom.csv", "b-cpl.csv"]
    assert out[0].read_text(encoding="utf-8").splitlines() == [
        "Comment,Designator,Footprint,LCSC Part #,MPN,Qty",
        '1k,"R1,R2",res0402,C11702,,2',
    ]
    assert out[1].read_text(encoding="utf-8").splitlines() == [
        "Designator,Mid X,Mid Y,Layer,Rotation",
        "R1,10.5000mm,-20.2500mm,Top,90",
    ]


def test_repair_zero_width_tracks(project: fixes.KicadProject) -> None:
    assert fixes.repair_zero_width_tracks(project) == []
    wide = sexpr.loads("(segment (start 10 10) (end 12 10) (width 0.4) (layer F.Cu) (net 1))")
    zero = sexpr.loads("(segment (start 12 10) (end 14 10) (width 0) (layer F.Cu) (net 1))")
    lone = sexpr.loads("(segment (start 30 30) (end 31 30) (width 0) (layer F.Cu) (net 2))")
    project.pcb += [wide, zero, lone]
    assert fixes.repair_zero_width_tracks(project) == [
        "track at (12, 10): width 0 -> 0.4 mm",
        "track at (30, 30): width 0 -> 0.15 mm",
    ]
    assert sexpr.child(zero, "width") == ["width", sexpr.Sym("0.4")]
    assert fixes.repair_zero_width_tracks(project) == []


def test_write_footprint_library_round_trip(project: fixes.KicadProject, tmp_path: Path) -> None:
    """A rotated board footprint comes back as the unrotated, net-free library footprint."""
    original = next(f for f in sexpr.children(project.pcb, "footprint") if "pin_header" in str(f[1]))
    rotated = copy.deepcopy(original)
    at_node = sexpr.child(rotated, "at")
    assert at_node is not None
    at_node[:] = [Sym("at"), Sym("94"), Sym("100"), Sym("90")]
    for pad in sexpr.children(rotated, "pad"):
        pad_at = sexpr.child(pad, "at")
        assert pad_at is not None
        pad_at[3] = Sym("90")
        pad.append([Sym("net"), Sym("1"), "VCC"])
    for pad in sexpr.children(original, "pad"):
        pad[:] = [n for n in pad if not (isinstance(n, list) and sexpr.head(n) == "net")]
    pretty = tmp_path / "lib.pretty"
    written = kicad.write_footprint_library([Sym("kicad_pcb"), rotated], pretty)
    assert [p.name for p in written] == ["pin_header_pinrow2_p2_54_nopinlabels.kicad_mod"]
    lib = sexpr.load(written[0])
    assert lib[1] == "pin_header_pinrow2_p2_54_nopinlabels"
    assert sexpr.child(lib, "at") is None
    assert sexpr.child(lib, "uuid") is None
    for lib_pad, pad in zip(sexpr.children(lib, "pad"), sexpr.children(original, "pad"), strict=True):
        assert sexpr.child(lib_pad, "net") is None
        assert sexpr.child(lib_pad, "at") == sexpr.child(pad, "at")
    assert sexpr.property_value(lib, "Reference") == "REF**"


def test_prepared_source(tmp_path: Path) -> None:
    board = tmp_path / "boards" / "b"
    board.mkdir(parents=True)
    source = board / "index.circuit.tsx"
    source.write_text('import { X } from "./imports"\nexport default () => (\n  <board width="1mm">\n  </board>\n)\n')
    text = tscircuit.prepared_source(source, tmp_path / "src").read_text()
    assert '<board schematicDisabled width="1mm">' in text  # tscircuit's schematic layout never runs
    assert f'from "{board / "imports"}"' in text
    fast = tscircuit.prepared_source(source, tmp_path / "fast", effort="2x").read_text()
    assert '<board schematicDisabled autorouterEffortLevel="2x" width="1mm">' in fast
    source.write_text('<board autorouterEffortLevel="10x" schematicDisabled width="1mm">')
    fast = tscircuit.prepared_source(source, tmp_path / "fast", effort="2x").read_text()
    assert fast == '<board autorouterEffortLevel="2x" schematicDisabled width="1mm">'


def test_drop_tracks_inside_pads_keeps_run_to_pad_edge(project: fixes.KicadProject) -> None:
    # J1 pad 1 (VCC = net 1): 1.5 mm square centred on (92.73, 100), right edge at x = 93.48
    run = sexpr.loads("(segment (start 92.73 100) (end 93.48 100) (width 0.2) (layer F.Cu) (net 1))")
    out = sexpr.loads("(segment (start 93.48 100) (end 96 100) (width 0.2) (layer F.Cu) (net 1))")
    project.pcb += [run, out]
    assert fixes.drop_tracks_inside_pads(project) == []  # dropping it would leave `out` ending on the edge
    assert run in project.pcb


def test_drop_tracks_inside_pads_straightens_a_wiggle_from_the_edge(project: fixes.KicadProject) -> None:
    # J1 pad 1 (VCC = net 1): 1.5 mm square centred on (92.73, 100), right edge at x = 93.48 (93.47: just inside)
    out = sexpr.loads("(segment (start 96 100) (end 93.47 100) (width 0.2) (layer F.Cu) (net 1))")
    wiggle = [
        sexpr.loads(f"(segment (start {a}) (end {b}) (width 0.2) (layer F.Cu) (net 1))")
        for a, b in (("93.47 100", "93.3 100.2"), ("93.3 100.2", "93.1 99.9"))
    ]
    project.pcb += [out, *wiggle]
    changes = fixes.drop_tracks_inside_pads(project)
    assert changes == ["J1: 2 in-pad track piece(s) replaced by a straight entry to the pad centre"]
    assert all(w not in project.pcb for w in wiggle)
    copper = fixes._copper(project.pcb)
    assert any({s.a, s.b} == {(93.47, 100.0), (92.73, 100.0)} for s in copper.segs)
    assert fixes.drop_tracks_inside_pads(project) == []


def test_relocate_vias(project: fixes.KicadProject) -> None:
    assert fixes.relocate_vias(project) == []
    # a GND via (net 2) overlapping J1 pad 1 (VCC, 1.5 mm square at (92.73, 100), edge x = 93.48)
    via = sexpr.loads("(via (at 93.6 100) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 2))")
    track = sexpr.loads("(segment (start 93.6 100) (end 93.6 103) (width 0.2) (layer B.Cu) (net 2))")
    project.pcb += [via, track]
    changes = fixes.relocate_vias(project)
    assert len(changes) == 1
    assert "clearance" in changes[0]
    at, start = sexpr.child(via, "at"), sexpr.child(track, "start")
    assert at is not None
    assert start is not None
    x, y = float(str(at[1])), float(str(at[2]))
    assert (float(str(start[1])), float(str(start[2]))) == (x, y)  # the track end moved along
    assert max(x - 93.48, abs(y - 100) - 0.75) >= 0.3 + 0.2 - 1e-6  # via copper >= clearance from the pad
    assert fixes.relocate_vias(project) == []


def test_unpoured_copper_layers(project: fixes.KicadProject) -> None:
    # the fixture has a GND pad but no zones
    assert fixes.unpoured_copper_layers(project) == ["F.Cu", "B.Cu"]
    zone = '(zone (net 2) (net_name "GND") (layer "{}") (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))'
    project.pcb.append(sexpr.loads(zone.format("B.Cu")))
    assert fixes.unpoured_copper_layers(project) == ["F.Cu"]
    project.pcb.append(sexpr.loads(zone.format("F.Cu")))
    assert fixes.unpoured_copper_layers(project) == []


def test_separate_ends_on_pads(project: fixes.KicadProject) -> None:
    assert fixes.separate_ends_on_pads(project) == []
    # two VCC tracks ending on the same point inside J1 pad 1 (1.5 mm square at (92.73, 100)), 45 deg apart
    a = sexpr.loads("(segment (start 92.73 100) (end 90 100) (width 0.2) (layer F.Cu) (net 1))")
    b = sexpr.loads("(segment (start 92.73 100) (end 90 97.27) (width 0.2) (layer F.Cu) (net 1))")
    project.pcb += [a, b]
    changes = fixes.separate_ends_on_pads(project)  # the fixture's own VCC track ends there too
    assert changes[0] == "VCC at (92.73, 100): 2 track end(s) separated on the pad/via"
    assert all("pulled back" in c for c in changes[1:])  # still overlapping at 45 deg: further apart
    sa, sb = sexpr.child(a, "start"), sexpr.child(b, "start")
    assert sa is not None
    assert sb is not None
    assert sa[1:] != sb[1:]  # no shared vertex any more
    assert fixes.separate_ends_on_pads(project) == []


def test_separate_ends_stay_on_the_junctions_via(project: fixes.KicadProject) -> None:
    """A junction on a via just beside a pad: an end moved back onto the pad would leave the via (la-bga54e)."""
    # J1 pad 1 spans x 91.98..93.48; the VCC via (r 0.2) at 93.75 covers x 93.55..93.95: a gap between them
    via = sexpr.loads("(via (at 93.75 100) (size 0.4) (drill 0.2) (layers F.Cu B.Cu) (net 1))")
    a = sexpr.loads("(segment (start 93.56 100) (end 93.2 100) (width 0.2) (layer F.Cu) (net 1))")  # pad <-> via
    b = sexpr.loads(
        "(segment (start 93.56 100) (end 93.45 101.6) (width 0.2) (layer F.Cu) (net 1))"
    )  # 86 deg, off the pad
    project.pcb += [via, b, a]
    fixes.separate_ends_on_pads(project)
    start = sexpr.child(a, "start")
    assert start is not None
    assert math.dist((float(str(start[1])), float(str(start[2]))), (93.75, 100)) <= 0.2  # still on the via


def test_restore_nets(project: fixes.KicadProject) -> None:
    assert fixes.restore_nets(project, {"VCC"}) == []  # no snapshot yet
    project.before_track_fixes = copy.deepcopy(project.pcb)
    routed = sum(1 for n in sexpr.children(project.pcb, "segment") if sexpr.child(n, "net") == ["net", Sym("1")])
    for seg in [n for n in sexpr.children(project.pcb, "segment") if sexpr.child(n, "net") == ["net", Sym("1")]]:
        project.pcb.remove(seg)  # a clean-up that went wrong
    assert fixes.restore_nets(project, {"VCC"}) == [
        "VCC: tracks/vias restored as routed (KiCad found it unconnected after the clean-up)"
    ]
    assert (
        sum(1 for n in sexpr.children(project.pcb, "segment") if sexpr.child(n, "net") == ["net", Sym("1")]) == routed
    )


def test_merge_duplicate_vias(project: fixes.KicadProject) -> None:
    assert fixes.merge_duplicate_vias(project) == []
    through = sexpr.loads("(via (at 50 50) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 2))")
    blind = sexpr.loads("(via (at 50 50) (size 0.6) (drill 0.3) (layers F.Cu In2.Cu) (net 2))")
    project.pcb += [blind, through]
    assert fixes.merge_duplicate_vias(project) == ["via GND at (50.0, 50.0): 1 duplicate(s) merged"]
    assert through in project.pcb
    assert blind not in project.pcb
    assert fixes.merge_duplicate_vias(project) == []


def test_merge_duplicate_vias_too_close(project: fixes.KicadProject) -> None:
    # two GND vias 0.44 mm apart (0.3 mm holes: 0.14 mm between them, under the 0.25 mm minimum);
    # the track ending in the dropped one is pulled to its centre and bridged to the kept one
    first = sexpr.loads("(via (at 50 50) (size 0.5) (drill 0.3) (layers F.Cu B.Cu) (net 2))")
    second = sexpr.loads("(via (at 50.44 50) (size 0.5) (drill 0.3) (layers F.Cu B.Cu) (net 2))")
    track = sexpr.loads("(segment (start 50.5 50.1) (end 53 50.1) (width 0.2) (layer B.Cu) (net 2))")
    project.pcb += [first, second, track]
    changes = fixes.merge_duplicate_vias(project)
    assert len(changes) == 1
    assert "merged into" in changes[0]
    assert (first in project.pcb) != (second in project.pcb)
    kept = first if first in project.pcb else second
    at = sexpr.child(kept, "at")
    assert at is not None
    copper = fixes._copper(project.pcb)
    gnd_b = [s for s in copper.segs if s.net == "GND" and s.layer == "B.Cu" and min(s.a[1], s.b[1]) > 49]
    ends = {p for s in gnd_b for p in (s.a, s.b)}
    assert (float(str(at[1])), float(str(at[2]))) in ends  # bridged to the kept via
    assert fixes.merge_duplicate_vias(project) == []


def test_relocate_via_rerouting_splices_long_runs(project: fixes.KicadProject) -> None:
    # a GND via overlapping J1 pad 1 (VCC, edge x = 93.48) with a 20 mm B.Cu run: the via moves, only
    # the first ~2 mm of the run are rerouted, the rest (here its far piece) stays as it was
    via = sexpr.loads("(via (at 93.6 100) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 2))")
    near = sexpr.loads("(segment (start 93.6 100) (end 93.6 101) (width 0.2) (layer B.Cu) (net 2))")
    mid = sexpr.loads("(segment (start 93.6 101) (end 93.6 103) (width 0.2) (layer B.Cu) (net 2))")
    far = sexpr.loads("(segment (start 93.6 103) (end 93.6 120) (width 0.2) (layer B.Cu) (net 2))")
    end_via = sexpr.loads("(via (at 93.6 120) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 2))")
    project.pcb += [via, near, mid, far, end_via]
    copper = fixes._copper(project.pcb)
    clearance, edge = fixes._design_clearances(project)
    node, centre, radius, net = next(v for v in copper.vias if v[0] is via)
    moved = fixes._relocate_via_rerouting(project, copper, node, centre, radius, net, clearance, edge)
    assert moved is not None
    assert far in project.pcb  # beyond the splice: untouched
    assert near not in project.pcb
    copper = fixes._copper(project.pcb)
    pad = next(o for o in copper.pads_by_net["VCC"] if o.distance((93.0, 100.0), (93.0, 100.0)) == 0)
    assert pad.distance(moved, moved) >= radius + clearance - 1e-6


def test_square_tees(project: fixes.KicadProject) -> None:
    assert fixes.square_tees(project) == []
    body = sexpr.loads("(segment (start 60 60) (end 70 60) (width 0.2) (layer F.Cu) (net 2))")
    branch = sexpr.loads("(segment (start 63 63) (end 66 60) (width 0.2) (layer F.Cu) (net 2))")  # 45 deg onto the body
    project.pcb += [body, branch]
    changes = fixes.square_tees(project)
    assert len(changes) == 1
    assert "squared" in changes[0]
    assert sexpr.child(branch, "end") == ["end", Sym("63"), Sym("60")]  # foot of the perpendicular from (63, 63)
    assert fixes.square_tees(project) == []


def test_push_tracks_clear(project: fixes.KicadProject) -> None:
    assert fixes.push_tracks_clear(project) == []
    # a VCC zig-zag whose middle piece passes 0.1 mm from a GND via (needs 0.2 mm + half width)
    via = sexpr.loads("(via (at 65 60.45) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 2))")
    pieces = [
        sexpr.loads(f"(segment (start {a}) (end {b}) (width 0.2) (layer F.Cu) (net 1))")
        for a, b in (("60 55", "62 60"), ("62 60", "68 60"), ("68 60", "70 55"))
    ]
    project.pcb += [via, *pieces]
    changes = fixes.push_tracks_clear(project)
    assert len(changes) == 1
    middle = pieces[1]
    start, end = sexpr.child(middle, "start"), sexpr.child(middle, "end")
    assert start is not None
    assert end is not None
    assert float(str(start[2])) == float(str(end[2])) < 60  # moved away from the via, still straight
    assert fixes.push_tracks_clear(project) == []


def test_push_tracks_clear_slides_a_pad_end(project: fixes.KicadProject) -> None:
    # a VCC track leaving J1 pad 1 (1.5 mm square at (92.73, 100)) passes 0.4 mm from a GND via
    # centre (needs 0.3 + 0.2 + 0.1): it moves sideways, its pad end sliding inside the pad
    via = sexpr.loads("(via (at 93.13 102) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 2))")
    piece = sexpr.loads("(segment (start 92.73 100.5) (end 92.73 104) (width 0.2) (layer F.Cu) (net 1))")
    nxt = sexpr.loads("(segment (start 92.73 104) (end 92.73 104.6) (width 0.2) (layer F.Cu) (net 1))")
    project.pcb += [via, piece, nxt]
    assert len(fixes.push_tracks_clear(project)) == 1
    start = sexpr.child(piece, "start")
    assert start is not None
    x = float(str(start[1]))
    assert 91.98 + fixes.PAD_EDGE_MARGIN_MM <= x < 92.73  # moved away from the via, still in the pad
    end, nxt_start = sexpr.child(piece, "end"), sexpr.child(nxt, "start")
    assert end == ["end", *nxt_start[1:]] if nxt_start is not None else False  # neighbour followed
    assert fixes.push_tracks_clear(project) == []


def test_reroute_tracks_long_run(project: fixes.KicadProject) -> None:
    # a 48 mm VCC run between two vias passes through a GND via near its end: only the bad stretch
    # (a few mm) is rerouted, the run's length does not rule it out
    vias = [
        sexpr.loads(f"(via (at {x} 70) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net {n}))")
        for x, n in ((20, 1), (64, 2), (68, 1))
    ]
    xs = (20, 62, 63, 65, 66, 68)
    pieces = [
        sexpr.loads(f"(segment (start {a} 70) (end {b} 70) (width 0.2) (layer F.Cu) (net 1))")
        for a, b in itertools.pairwise(xs)
    ]
    project.pcb += [*vias, *pieces]
    assert len(fixes.reroute_tracks(project)) == 1
    assert pieces[0] in project.pcb  # the long first piece stays
    copper = fixes._copper(project.pcb)
    gnd_via = (64.0, 70.0)
    vcc = [s for s in copper.segs if s.net == "VCC" and min(s.a[1], s.b[1]) > 65]
    assert all(tracks.point_seg_distance(gnd_via, s.a, s.b) >= 0.3 + 0.2 + 0.1 - 1e-6 for s in vcc)


def test_reroute_tracks_keeps_a_t_junction(project: fixes.KicadProject) -> None:
    # as above, but a VCC branch from (62, 73) ends on the run's side at (62, 70): rerouting the run
    # would leave that branch ending on nothing, so the run stays
    vias = [
        sexpr.loads(f"(via (at {x} 70) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net {n}))")
        for x, n in ((60, 1), (64, 2), (68, 1))
    ]
    run = sexpr.loads("(segment (start 60 70) (end 68 70) (width 0.2) (layer F.Cu) (net 1))")
    branch = sexpr.loads("(segment (start 62 73) (end 62 70) (width 0.2) (layer F.Cu) (net 1))")
    project.pcb += [*vias, run, branch]
    fixes.reroute_tracks(project)
    assert run in project.pcb
    assert sexpr.child(run, "start") == ["start", Sym("60"), Sym("70")]
    assert sexpr.child(run, "end") == ["end", Sym("68"), Sym("70")]


def test_reroute_tracks_leaves_a_via_off_a_passing_track(project: fixes.KicadProject) -> None:
    # VCC via at (60, 70); a VCC track passes through it along y = 70 and a VCC run leaves from a point
    # on that track inside the via at 45 deg to it (KiCad: acute track_angle) -> rerouted to leave at >= 90 deg
    via = sexpr.loads("(via (at 60 70) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 1))")
    passing = sexpr.loads("(segment (start 60.2 70) (end 56 70) (width 0.2) (layer F.Cu) (net 1))")
    run = sexpr.loads("(segment (start 59.9 70) (end 56.9 73) (width 0.2) (layer F.Cu) (net 1))")
    end_via = sexpr.loads("(via (at 56.9 73) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 1))")
    project.pcb += [via, passing, run, end_via]
    assert len(fixes.reroute_tracks(project)) == 1
    copper = fixes._copper(project.pcb)
    start = (59.9, 70.0)
    first = next(s for s in copper.segs if s.net == "VCC" and start in (s.a, s.b) and s.source is not passing)
    nxt = first.b if first.a == start else first.a
    assert tracks.interior_angle(nxt, start, (56.0, 70.0)) >= tracks.MIN_JUNCTION_DEG - 0.5


def test_route_with_stub_leaves_a_tilted_junction() -> None:
    # a junction at (60, 60) on a track tilted 1 deg: every grid heading meets one side at 89 deg or less
    branches = [(56.0, 60.07), (64.0, 59.93)]

    def leaves_ok(p: tracks.Point, towards: tracks.Point, neighbour: tracks.Point | None) -> bool:
        return all(tracks.interior_angle(towards, p, f) >= tracks.MIN_JUNCTION_DEG - 0.5 for f in branches)

    start, goal = (60.0, 60.0), (61.0, 62.0)
    route = fixes._route_with_stub(start, goal, None, None, lambda p: True, lambda a, b: True, leaves_ok)
    assert route is not None
    assert route[0] == start
    assert route[-1] == goal
    assert leaves_ok(start, route[1], None)
    assert all(
        tracks.interior_angle(route[i - 1], route[i], route[i + 1]) >= tracks.MIN_CORNER_DEG - 0.5
        for i in range(1, len(route) - 1)
    )


def test_reroute_tracks(project: fixes.KicadProject) -> None:
    # a VCC run between two vias straight through a GND via: rerouted round it, ends kept
    vias = [
        sexpr.loads(f"(via (at {x} 70) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net {n}))")
        for x, n in ((60, 1), (64, 2), (68, 1))
    ]
    run = sexpr.loads("(segment (start 60 70) (end 68 70) (width 0.2) (layer F.Cu) (net 1))")
    project.pcb += [*vias, run]
    changes = fixes.reroute_tracks(project)
    assert len(changes) == 1
    copper = fixes._copper(project.pcb)
    vcc = [s for s in copper.segs if s.net == "VCC" and s.layer == "F.Cu" and min(s.a[1], s.b[1]) > 65]
    ends = {p for s in vcc for p in (s.a, s.b)}
    assert (60.0, 70.0) in ends
    assert (68.0, 70.0) in ends
    gnd_via = next(o for o in copper.obstacles() if o.net == "GND" and o.points == ((64.0, 70.0),))
    assert all(gnd_via.distance(s.a, s.b) >= 0.2 + 0.1 - 1e-6 for s in vcc)


def test_square_tees_at_a_junction(project: fixes.KicadProject) -> None:
    # three GND tracks meet at (60, 60); two of them leave 45 deg apart
    tracks_ = [
        sexpr.loads(f"(segment (start 60 60) (end {x} {y}) (width 0.2) (layer F.Cu) (net 2))")
        for x, y in ((55, 60), (70, 60), (63, 57))
    ]
    project.pcb += tracks_
    changes = fixes.square_tees(project)
    assert len(changes) == 1
    assert sexpr.child(tracks_[2], "start") == ["start", Sym("63"), Sym("60")]  # now a T at 90 deg onto (60,60)-(70,60)
    assert fixes.square_tees(project) == []


def test_square_tees_two_acute_branches_of_one_junction(project: fixes.KicadProject) -> None:
    # junction at (60, 60): branches to (63, 57) and (64, 59) are 31 deg apart, each acute to the other;
    # only one may leave per pass, else both land on the other's old position and the junction falls apart
    tracks_ = [
        sexpr.loads(f"(segment (start 60 60) (end {x} {y}) (width 0.2) (layer F.Cu) (net 2))")
        for x, y in ((55, 60), (63, 57), (64, 59))
    ]
    project.pcb += tracks_
    assert fixes.square_tees(project)
    copper = fixes._copper(project.pcb)
    gnd = [s for s in copper.segs if s.net == "GND" and s.layer == "F.Cu" and max(s.a[0], s.b[0]) < 70]
    for s in gnd:
        for end in (s.a, s.b):
            if end in ((55.0, 60.0), (63.0, 57.0), (64.0, 59.0)):
                continue
            assert any(
                t is not s
                and (
                    min(math.dist(end, t.a), math.dist(end, t.b)) <= tracks.EPS
                    or tracks.point_seg_distance(end, t.a, t.b) <= tracks.EPS
                )
                for t in gnd
            ), f"loose end {end}"
    assert fixes.square_tees(project) == []


def test_snap_near_ends(project: fixes.KicadProject) -> None:
    assert fixes.snap_near_ends(project) == []
    a = sexpr.loads("(segment (start 50 70) (end 55 70.004) (width 0.15) (layer F.Cu) (net 2))")  # 4 um short
    b = sexpr.loads("(segment (start 55 70) (end 58 73) (width 0.15) (layer F.Cu) (net 2))")
    c = sexpr.loads("(segment (start 60 80) (end 60 76.1) (width 0.15) (layer F.Cu) (net 2))")  # 0.1 mm beside d
    d = sexpr.loads("(segment (start 58 76) (end 62 76) (width 0.15) (layer F.Cu) (net 2))")
    project.pcb += [a, b, c, d]
    assert len(fixes.snap_near_ends(project)) == 2
    assert sexpr.child(a, "end") == ["end", Sym("55"), Sym("70")]
    assert sexpr.child(c, "end") == ["end", Sym("60"), Sym("76")]
    assert fixes.snap_near_ends(project) == []


def test_drop_loose_power_symbols(project: fixes.KicadProject) -> None:
    assert fixes.schematic_power_symbols(project)
    assert fixes.drop_loose_power_symbols(project) == []  # every rail symbol of the fixture is wired
    power = [
        s
        for s in sexpr.children(project.sch, "symbol")
        if (sexpr.property_value(s, "Reference") or "").startswith("#PWR")
    ]
    loose = power[0]
    at = sexpr.child(loose, "at")
    assert at is not None
    at[1:3] = [Sym("250"), Sym("250")]  # off into empty sheet space
    changes = fixes.drop_loose_power_symbols(project)
    assert len(changes) == 1
    assert loose not in project.sch
    assert all(s in project.sch for s in power[1:])
    assert fixes.drop_loose_power_symbols(project) == []


def test_apply_board_config(project: fixes.KicadProject) -> None:
    assert fixes.apply_board_config(project) == []  # no config
    pro = project.pcb_path.with_suffix(".kicad_pro")
    pro.write_text(json.dumps({"net_settings": {"classes": [{"name": "Default", "clearance": 0.2}]}}), encoding="utf-8")
    project.config = {
        "netclass": {"USB": {"nets": ["USB_D*"], "track_width": 0.2, "diff_pair_gap": 0.15}},
        "length_group": {"SDRAM_DQ": {"nets": ["SD_DQ*"], "max_skew": 2}},
        "diff_pair": {"max_uncoupled": 5},
    }
    assert len(fixes.apply_board_config(project)) == 2
    settings = json.loads(pro.read_text(encoding="utf-8"))["net_settings"]
    usb = next(c for c in settings["classes"] if c["name"] == "USB")
    assert usb == {"name": "USB", "clearance": 0.2, "track_width": 0.2, "diff_pair_gap": 0.15}
    assert settings["netclass_patterns"] == [{"netclass": "USB", "pattern": "USB_D*"}]
    dru = project.pcb_path.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert "(constraint skew (max 2mm))" in dru
    assert "A.NetName == 'SD_DQ*'" in dru
    assert "(constraint diff_pair_uncoupled (max 5mm))" in dru
    assert fixes.apply_board_config(project) == []  # idempotent
    project.config["length_group"]["SDRAM_DQ"]["max_skew"] = 1
    assert len(fixes.apply_board_config(project)) == 1
    dru = project.pcb_path.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert dru.count("length group SDRAM_DQ") == 1  # replaced, not appended


def test_apply_rule_profiles(project: fixes.KicadProject) -> None:
    assert fixes.apply_rule_profiles(project) == []  # no [rules]
    pro = project.pcb_path.with_suffix(".kicad_pro")
    default = {"name": "Default", "track_width": 0.1, "clearance": 0.1, "via_diameter": 0.3, "via_drill": 0.2}
    pro.write_text(json.dumps({"net_settings": {"classes": [default]}}), encoding="utf-8")
    project.config = {
        "rules": {"profiles": ["jlcpcb-2l", "ipc-class2", "hv-60v"], "override": {"track_width": 0.2}},
        "netclass": {"USB": {"nets": ["USB_D*"], "track_width": 0.2}},
    }
    assert fixes.apply_rule_profiles(project)
    data = json.loads(pro.read_text(encoding="utf-8"))
    design = data["board"]["design_settings"]["rules"]
    assert design["min_track_width"] == 0.1
    assert design["min_clearance"] == 0.1
    assert design["min_through_hole_diameter"] == 0.15
    assert design["min_via_diameter"] == 0.25
    assert design["min_via_annular_width"] == 0.1  # ipc-class2 tightens jlcpcb's 0.05
    assert design["min_hole_to_hole"] == 0.2
    assert design["min_copper_edge_clearance"] == 0.2
    assert design["min_text_thickness"] == 0.15
    cls = data["net_settings"]["classes"][0]
    # profile value for the track width, via raised to the 0.1 mm ring, clearance already at the minimum
    assert cls == {"name": "Default", "track_width": 0.2, "clearance": 0.1, "via_diameter": 0.4, "via_drill": 0.2}
    dru = project.pcb_path.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert "(constraint hole_to_hole (min 0.45mm))" in dru
    assert "(constraint annular_width (min 0.18mm))" in dru
    assert fixes.apply_rule_profiles(project) == []  # idempotent
    # the profile's net class and the board's go in together; the Default stays first
    assert fixes.apply_board_config(project)
    settings = json.loads(pro.read_text(encoding="utf-8"))["net_settings"]
    assert [c["name"] for c in settings["classes"]] == ["Default", "HV60", "USB"]
    assert next(c for c in settings["classes"] if c["name"] == "HV60")["clearance"] == 0.6
    assert {"netclass": "HV60", "pattern": "HV_*"} in settings["netclass_patterns"]
    project.config["rules"]["override"]["board_thickness"] = 0.8
    assert fixes.standard_board_thickness(project) == ["board thickness 1.4 mm -> 0.8 mm"]
    general = sexpr.child(project.pcb, "general")
    assert general is not None
    assert sexpr.child(general, "thickness") == ["thickness", "0.8"]
    project.config["rules"]["override"]["min_clearance"] = 0.05
    with pytest.raises(config.ConfigError, match="below the fab capability"):
        fixes.apply_rule_profiles(project)


def test_board_config_validation() -> None:
    config.validate({"netclass": {"USB": {"nets": ["USB*"], "track_width": 0.2}}})
    with pytest.raises(config.ConfigError):
        config.validate({"netclasses": {}})
    with pytest.raises(config.ConfigError):
        config.validate({"length_group": {"X": {"nets": ["A*"]}}})
    with pytest.raises(config.ConfigError):
        config.validate({"netclass": {"USB": {"nets": ["U*"], "trace_width": 0.2}}})


def test_split_tees_keeps_clearance(project: fixes.KicadProject) -> None:
    # a VCC branch ends 0.08 mm off a VCC track's centre line (a T). Snapping it onto the line would
    # bring it 0.597 mm from a GND via centre (needs 0.3 + 0.2 + 0.1 = 0.6): it stays put
    through = sexpr.loads("(segment (start 64.7 70) (end 70 70) (width 0.2) (layer F.Cu) (net 1))")
    branch = sexpr.loads("(segment (start 64.95 70.08) (end 65 75) (width 0.2) (layer F.Cu) (net 1))")
    via = sexpr.loads("(via (at 64.353 70) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 2))")
    project.pcb += [through, branch, via]
    clearance, edge = fixes._design_clearances(project)
    assert fixes._split_tees(project, clearance, edge) == 0
    assert sexpr.child(branch, "start") == ["start", Sym("64.95"), Sym("70.08")]
    project.pcb.remove(via)  # without the via the T is snapped
    assert fixes._split_tees(project, clearance, edge) == 1


def test_stitch_unconnected_pads(project: fixes.KicadProject) -> None:
    # the SMD GND pad at (101.83, 102) gets a via into the (filled) GND pour on B.Cu
    zone = sexpr.loads(
        '(zone (net 2 "GND") (layer "B.Cu") (filled_polygon (layer "B.Cu") '
        "(pts (xy 90 90) (xy 110 90) (xy 110 110) (xy 90 110))))"
    )
    project.pcb.append(zone)
    before = {v[1] for v in fixes._copper(project.pcb).vias}
    # KiCad lists a pad once per unconnected pair: three entries, one via
    changes = fixes.stitch_unconnected_pads(project, [("GND", (101.83, 102.0))] * 3)
    assert changes == ["GND pad at (101.83, 102): stitching via to its plane"]
    assert len({v[1] for v in fixes._copper(project.pcb).vias} - before) == 1
    copper = fixes._copper(project.pcb)
    clearance, _ = fixes._design_clearances(project)
    via = next(v for v in copper.vias if v[1] not in before)
    assert via[3] == "GND"
    pad = next(o for o in copper.pads_by_net["GND"] if o.distance((101.83, 102.0), (101.83, 102.0)) <= 0.01)
    assert pad.distance(via[1], via[1]) >= via[2] + clearance - 1e-6  # beside the pad, not in it
    centre, radius = via[1], via[2]
    assert math.dist(centre, (101.83, 102.0)) <= 2.0
    assert all(o.distance(centre, centre) >= radius + clearance - 1e-6 for o in copper.obstacles() if o.net != "GND")
    assert any(  # from the pad centre (101.825, 102) to the via
        s.net == "GND" and s.b == centre and math.dist(s.a, (101.83, 102.0)) < 0.01 for s in copper.segs
    )
    # a net without a pour elsewhere gets nothing
    assert fixes.stitch_unconnected_pads(project, [("VCC", (92.73, 100.0))]) == []


def test_unconnected_pads_from_report() -> None:
    result = kicad.CheckResult(
        "DRC",
        Path("drc.json"),
        [
            {
                "type": "unconnected_items",
                "items": [
                    {"description": "Pad 2 [GND] of U2 on F.Cu", "pos": {"x": 1.5, "y": 2.0}},
                    {"description": "Zone [GND] on F.Cu, priority 0", "pos": {"x": 3.0, "y": 4.0}},
                ],
            }
        ],
    )
    assert result.unconnected_pads() == [("GND", (1.5, 2.0))]


def test_plane_pad_vias(project: fixes.KicadProject) -> None:
    assert fixes.plane_pad_vias(project) == []  # no [planes] in the config
    zone = sexpr.loads(
        '(zone (net 2 "GND") (layer "B.Cu") (polygon (pts (xy 85 90) (xy 110 90) (xy 110 110) (xy 85 110))))'
    )
    project.pcb.append(zone)
    project.config = {"planes": {"nets": ["GND"]}}
    routed_before = [s for s in fixes._copper(project.pcb).segs if s.net == "GND"]
    assert routed_before  # the fixture routes GND with tracks
    changes = fixes.plane_pad_vias(project)
    assert len(changes) == 1
    assert "pad vias to the plane" in changes[0]
    copper = fixes._copper(project.pcb)
    clearance, _ = fixes._design_clearances(project)
    smd = [o for o in copper.pads_by_net["GND"] if len(o.layers) == 1]
    assert len(copper.vias_by_net["GND"]) == len(smd)
    for pad in smd:  # each SMD pad: a track from its centre to a via beside it
        via = next(
            c
            for c, r in copper.vias_by_net["GND"]
            if any(s.net == "GND" and {s.a, s.b} == {pad.centre, c} for s in copper.segs)
        )
        radius = copper.vias_by_net["GND"][0][1]
        assert pad.distance(via, via) >= radius + clearance - 1e-6
    assert all(s.a in {p.centre for p in smd} for s in copper.segs if s.net == "GND")  # the router's tracks are gone


def test_net_node_track_form(project: fixes.KicadProject) -> None:
    # tracks / vias take (net <id>); a pad's (net <id> "<name>") on a via crashes kicad-cli's DRC
    for n in [n for n in project.pcb if sexpr.head(n) in ("segment", "via")]:
        project.pcb.remove(n)  # only pads left to copy the net from
    node = fixes._net_node(project.pcb, "GND")
    assert node is not None
    assert len(node) == 2


def test_clean_tracks_keeps_pad_to_plane_via(project: fixes.KicadProject) -> None:
    # a GND pad's track to a via inside a GND zone is the pad's way to the plane: clean_tracks keeps it
    # (its connectivity model has no pours; without the via as an anchor the track looks redundant)
    zone = sexpr.loads(
        '(zone (net 2 "GND") (layer "B.Cu") (polygon (pts (xy 90 90) (xy 110 90) (xy 110 110) (xy 90 110))))'
    )
    via = sexpr.loads("(via (at 102.6 100.8) (size 0.4) (drill 0.2) (layers F.Cu B.Cu) (net 2))")
    # as plane_pad_vias' dogleg leaves it: a piece inside the pad, then out to the via
    track = [
        sexpr.loads("(segment (start 101.825 102) (end 101.825 101.6) (width 0.15) (layer F.Cu) (net 2))"),
        sexpr.loads("(segment (start 101.825 101.6) (end 102.6 100.8) (width 0.15) (layer F.Cu) (net 2))"),
    ]
    names = fixes._net_names(project.pcb)
    for n in [
        n
        for n in project.pcb
        if isinstance(n, list) and sexpr.head(n) in ("segment", "via") and fixes._net_of(n, names) == "GND"
    ]:
        project.pcb.remove(n)  # the pad's only connection is the new via (as after plane_pad_vias)
    project.pcb += [zone, via, *track]
    fixes.clean_tracks(project)
    copper = fixes._copper(project.pcb)
    assert any(
        s.net == "GND" and min(math.dist(s.a, (102.6, 100.8)), math.dist(s.b, (102.6, 100.8))) <= 0.2
        for s in copper.segs
    )


def test_unlabelled_connectors(project: fixes.KicadProject) -> None:
    refs = [r for r, fp in fixes._footprints_by_ref(project.pcb).items() if r.startswith("J")]
    warnings = fixes.unlabelled_connectors(project)
    big = [r for r in refs if len(list(sexpr.children(fixes._footprints_by_ref(project.pcb)[r], "pad"))) >= 3]
    assert len(warnings) == len(big)
    if big:  # a text next to it silences the warning
        at = sexpr.child(fixes._footprints_by_ref(project.pcb)[big[0]], "at")
        assert at is not None
        text = f'(gr_text "SIG" (at {at[1]} {at[2]}) (layer "F.SilkS") (effects (font (size 1 1))))'
        project.pcb.append(sexpr.loads(text))
        assert len(fixes.unlabelled_connectors(project)) == len(big) - 1


def test_apply_keepouts_makes_rule_areas_from_the_config(project: fixes.KicadProject) -> None:
    project.config = {
        "keepout": {
            "under_r1": {"ref": "R1", "margin": 1.0, "layers": ["F.Cu"], "block": ["pour", "vias"]},
            "box": {"area": [90, 90, 92, 94]},
            "ghost": {"ref": "X9"},
        }
    }
    changes = fixes.apply_keepouts(project)
    assert any(c.startswith("under_r1: no pour, vias on F.Cu") for c in changes)
    assert "ghost: part X9 not on the board, keepout skipped" in changes
    zones = {
        str(sexpr.child(z, "name")[1]): z  # type: ignore[index]
        for z in sexpr.children(project.pcb, "zone")
        if sexpr.child(z, "keepout") is not None
    }
    area = zones["vibepcb:keepout:under_r1"]
    keepout = sexpr.child(area, "keepout")
    assert keepout is not None
    assert sexpr.child(keepout, "copperpour") == ["copperpour", Sym("not_allowed")]
    assert sexpr.child(keepout, "vias") == ["vias", Sym("not_allowed")]
    assert sexpr.child(keepout, "tracks") == ["tracks", Sym("allowed")]
    box = zones["vibepcb:keepout:box"]
    layers = sexpr.child(box, "layers")
    assert layers is not None
    assert layers[1:] == ["F.Cu", "B.Cu"]  # default: every copper layer
    xs = {float(str(xy[1])) for xy in sexpr.walk(sexpr.child(box, "polygon") or []) if sexpr.head(xy) == "xy"}
    assert xs == {90.0, 92.0}
    # rewritten, not duplicated
    fixes.apply_keepouts(project)
    assert len([z for z in sexpr.children(project.pcb, "zone") if fixes._is_keepout_zone(z)]) == 2
    project.config = {}
    fixes.apply_keepouts(project)
    assert not [z for z in sexpr.children(project.pcb, "zone") if fixes._is_keepout_zone(z)]


def test_keepout_config_is_validated() -> None:
    with pytest.raises(config.ConfigError, match="either ref"):
        config.validate({"keepout": {"a": {"margin": 1}}})
    with pytest.raises(config.ConfigError, match="block takes"):
        config.validate({"keepout": {"a": {"ref": "U1", "block": ["copper"]}}})
    config.validate({"keepout": {"a": {"ref": "U1", "block": ["pour", "vias"]}, "b": {"area": [0, 0, 1, 1]}}})


def test_apply_pours_adds_a_missing_pour_from_the_config(project: fixes.KicadProject) -> None:
    project.config = {
        "pour": {
            "supply": {"net": "VCC", "layers": ["B.Cu"], "margin": 1.0},
            "none": {"net": "NOPE", "layers": ["F.Cu"]},
        }
    }
    before = len(list(sexpr.children(project.pcb, "zone")))
    changes = fixes.apply_pours(project)
    assert "supply: VCC pour on B.Cu" in changes
    assert "none: net NOPE not on the board, pour skipped" in changes
    assert len(list(sexpr.children(project.pcb, "zone"))) == before + 1
    assert fixes.apply_pours(project) == ["none: net NOPE not on the board, pour skipped"]  # idempotent
    zone = [z for z in sexpr.children(project.pcb, "zone") if sexpr.child(z, "keepout") is None][-1]
    assert str(sexpr.child(zone, "layer")[1]) == "B.Cu"  # type: ignore[index]
    assert fixes._zone_net(project.pcb, zone) == "VCC"


def test_stitch_pour_islands_puts_a_via_into_a_floating_piece(project: fixes.KicadProject) -> None:
    # a GND piece on F.Cu (top right corner, no pad / via in it) over the GND pour on B.Cu
    project.pcb.append(sexpr.loads(
        '(zone (net 2 "GND") (layer "F.Cu") (filled_polygon (layer "F.Cu") '
        "(pts (xy 104 93.5) (xy 109 93.5) (xy 109 96) (xy 104 96))))"
    ))  # fmt: skip
    project.pcb.append(sexpr.loads(
        '(zone (net 2 "GND") (layer "B.Cu") (filled_polygon (layer "B.Cu") '
        "(pts (xy 91 93.5) (xy 109 93.5) (xy 109 106.5) (xy 91 106.5))))"
    ))  # fmt: skip
    changes = fixes.stitch_pour_islands(project)
    # the B.Cu pour holds the header's through-hole GND pin: only the F.Cu piece floats
    assert [c.split(":")[0] for c in changes] == ["GND pour piece on F.Cu"]
    vias = [v[1] for v in fixes._copper(project.pcb).vias if v[3] == "GND"]
    assert any(104 < x < 109 and 93.5 < y < 96 for x, y in vias)
    assert fixes.stitch_pour_islands(project) == []


def test_apply_pours_area_pour_is_a_named_prioritised_box(project: fixes.KicadProject) -> None:
    project.config = {"pour": {"core": {"net": "VCC", "layers": ["B.Cu"], "area": [95, 95, 100, 100], "priority": 2}}}
    changes = fixes.apply_pours(project)
    assert changes == ["core: VCC pour on B.Cu in (95, 95)-(100, 100)"]
    zones = [z for z in sexpr.children(project.pcb, "zone") if fixes._is_pour_zone(z)]
    assert len(zones) == 1
    assert sexpr.child(zones[0], "priority") == ["priority", Sym("2")]
    xs = {float(str(xy[1])) for xy in sexpr.walk(sexpr.child(zones[0], "polygon") or []) if sexpr.head(xy) == "xy"}
    assert xs == {95.0, 100.0}
    fixes.apply_pours(project)  # rewritten, not added twice
    assert len([z for z in sexpr.children(project.pcb, "zone") if fixes._is_pour_zone(z)]) == 1
