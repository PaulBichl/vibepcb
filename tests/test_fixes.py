from __future__ import annotations

import copy
import itertools
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibepcb import fixes, kicad, sexpr, tracks
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
    assert fixes.allow_footprints_in_keepouts(project) == ["keepout zone: footprints allowed"]
    keepout = sexpr.child(zone, "keepout")
    assert keepout is not None
    assert sexpr.child(keepout, "footprints") == ["footprints", "allowed"]
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
    bom, pos, zipped = tmp_path / "b-bom.csv", tmp_path / "b-pos.csv", tmp_path / "b-gerbers.zip"
    bom.write_text(
        '"Qty","Designator","Value","Footprint","MPN","LCSC Part #"\n"2","R1,R2","1k","tscircuit:res0402","","C11702"\n',
        encoding="utf-8",
    )
    pos.write_text(
        'Ref,Val,Package,PosX,PosY,Rot,Side\n"","","hole",1,2,0,top\n"R1","1k","res0402",10.5,-20.25,90,top\n',
        encoding="utf-8",
    )
    zipped.write_bytes(b"zip")
    out = kicad.jlcpcb_files(bom, pos, zipped, tmp_path / "jlc")
    assert [p.name for p in out] == ["b-gerbers.zip", "b-bom-jlcpcb.csv", "b-cpl-jlcpcb.csv"]
    assert out[1].read_text(encoding="utf-8").splitlines() == [
        "Comment,Designator,Footprint,LCSC Part #",
        '1k,"R1,R2",res0402,C11702',
    ]
    assert out[2].read_text(encoding="utf-8").splitlines() == [
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
