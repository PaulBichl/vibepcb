"""Post-processing fixes applied to the KiCad files tscircuit exports.

Each fix is a function ``(project: KicadProject) -> list[str]`` that edits the
parsed trees in place and returns a human-readable line per change (empty when
there was nothing to do). Register new fixes in ``FIXES``; they run in order.

The fixes live in submodules by area (``base``, ``board``, ``schematic``, ``references``, ``copper``,
``clean``, ``vias``, ``corners``, ``reroute``, ``push``, ``stitch``, ``post_fill``, ``zones``, ``hooks``,
``rules``); this module holds the registry and re-exports what other parts of VibePCB use.
"""

from __future__ import annotations

import copy
import time

from vibepcb.kicad.fixes.base import (
    COPPER_LAYERS,
    DEFAULT_CLEARANCE_MM,
    DEFAULT_EDGE_CLEARANCE_MM,
    DEFAULT_MIN_TRACK_WIDTH_MM,
    TRACK_NODES,
    KicadProject,
    _copper_layers,
    _design_clearances,
    _design_min_track_width,
    _design_via,
    _footprints_by_ref,
    _fp_to_board,
    _layer,
    _mm,
    _net_names,
    _net_node,
    _net_of,
    _num,
    _overlaps,
    _segment_node,
)
from vibepcb.kicad.fixes.board import (
    PAD_EDGE_MARGIN_MM,
    _escape_plans,
    _kicad_layer,
    allow_footprints_in_keepouts,
    apply_escapes,
    drop_npth_edge_cuts,
    drop_tracks_inside_pads,
    escape_offset,
    mask_covered_pads,
    propagate_split_pad_nets,
    rename_auto_nets,
    repair_zero_width_tracks,
    standard_board_thickness,
)
from vibepcb.kicad.fixes.clean import _split_tees, clean_tracks
from vibepcb.kicad.fixes.copper import (
    KEEPOUT_NET,
    KEEPOUT_VIA_NET,
    _Copper,
    _copper,
    _pad_obstacle,
    _source,
    _zone_polygons,
)
from vibepcb.kicad.fixes.corners import (
    _route_with_stub,
    collapse_bend_stubs,
    corner_touch_up,
    sharp_corners,
    snap_bends_to_45,
    snap_near_ends,
    square_tees,
    straighten_via_exits,
    tuck_corners_into_pads,
)
from vibepcb.kicad.fixes.hooks import UNROUTED_NAME, apply_routes, net_placed_vias, reroute_named_nets, tune_lengths
from vibepcb.kicad.fixes.post_fill import (
    Fix,
    apply_post_fill,
    drop_folded_back_tracks,
    prune_pour_tracks,
    split_at_tees,
)
from vibepcb.kicad.fixes.push import push_tracks_clear, remove_antennas, separate_ends_on_pads
from vibepcb.kicad.fixes.references import (
    REF_TEXT_SIZE_BOTTOM_MM,
    REF_TEXT_SIZE_MM,
    _footprint_geometry,
    _graphic_points,
    _pad_corners,
    _text_box,
    place_reference_designators,
)
from vibepcb.kicad.fixes.reroute import reroute_tracks
from vibepcb.kicad.fixes.rules import (
    apply_board_config,
    apply_rule_profiles,
    complete_netclasses,
    footprint_internal_clearance_rule,
    router_clearance_tolerance,
    strict_track_rules,
    thermal_spoke_rule,
    zone_thermal_reliefs,
)
from vibepcb.kicad.fixes.schematic import (
    RAIL_LIB_PREFIX,
    _label_anchor,
    _pin_positions,
    add_part_numbers,
    drop_loose_power_symbols,
    exclude_testpoints_from_bom,
    fill_schematic_footprints,
    fix_value_equals_reference,
    link_footprint_library,
    mark_dnp_parts,
    schematic_net_labels,
    schematic_no_connects,
    schematic_power_symbols,
)
from vibepcb.kicad.fixes.schematic_sheet import (
    _COORDS,
    _SHEET_META,
    PAPER_SIZES_MM,
    SCHEMATIC_GRID_MM,
    SHEET_FRAME_MM,
    TITLE_BLOCK_MM,
    _sheet_placement,
    fit_schematic_sheet,
)
from vibepcb.kicad.fixes.stitch import (
    ISLANDS_KEPT,
    ISLANDS_REMOVED,
    _pad_via,
    copper_vias,
    drop_vias_at,
    restore_nets,
    set_island_removal,
    split_crossings,
    stitch_pour_islands,
    stitch_unconnected_pads,
    unlabelled_connectors,
    unpoured_copper_layers,
)
from vibepcb.kicad.fixes.vias import (
    DEFAULT_HOLE_TO_HOLE_MM,
    _relocate_via_rerouting,
    merge_duplicate_vias,
    relocate_vias,
    widen_thin_vias,
)
from vibepcb.kicad.fixes.zones import (
    _is_keepout_zone,
    _is_pour_zone,
    _zone_net,
    apply_keepouts,
    apply_pours,
    plane_pad_vias,
    pours_to_board_outline,
    thermal_pad_vias,
)

FIXES: dict[str, Fix] = {
    "rename_auto_nets": rename_auto_nets,
    "propagate_split_pad_nets": propagate_split_pad_nets,
    "apply_rule_profiles": apply_rule_profiles,
    "apply_board_config": apply_board_config,  # before the routing: the router reads the net classes
    "complete_netclasses": complete_netclasses,  # after the classes are written: their schematic keys
    "apply_escapes": apply_escapes,
    "mask_covered_pads": mask_covered_pads,
    "apply_pours": apply_pours,
    "pours_to_board_outline": pours_to_board_outline,
    "apply_keepouts": apply_keepouts,
    "apply_routes": apply_routes,
    "thermal_pad_vias": thermal_pad_vias,  # before the track fixes: obstacles for them, kept by restore_nets
    "repair_zero_width_tracks": repair_zero_width_tracks,
    "drop_tracks_inside_pads": drop_tracks_inside_pads,
    "net_placed_vias": net_placed_vias,  # before the track fixes: they judge copper by net
    "merge_duplicate_vias": merge_duplicate_vias,
    "widen_thin_vias": widen_thin_vias,
    "plane_pad_vias": plane_pad_vias,
    "relocate_vias": relocate_vias,
    "snap_near_ends": snap_near_ends,
    "clean_tracks": clean_tracks,
    "separate_ends_on_pads": separate_ends_on_pads,
    "square_tees": square_tees,
    "straighten_via_exits": straighten_via_exits,
    "snap_bends_to_45": snap_bends_to_45,
    "collapse_bend_stubs": collapse_bend_stubs,
    "push_tracks_clear": push_tracks_clear,
    "reroute_tracks": reroute_tracks,
    "remove_antennas": remove_antennas,
    "tune_lengths": tune_lengths,
    "drop_npth_edge_cuts": drop_npth_edge_cuts,
    "allow_footprints_in_keepouts": allow_footprints_in_keepouts,
    "standard_board_thickness": standard_board_thickness,
    "fix_value_equals_reference": fix_value_equals_reference,
    "fill_schematic_footprints": fill_schematic_footprints,
    "schematic_net_labels": schematic_net_labels,
    "schematic_no_connects": schematic_no_connects,
    "schematic_power_symbols": schematic_power_symbols,
    "drop_loose_power_symbols": drop_loose_power_symbols,
    "add_part_numbers": add_part_numbers,
    "mark_dnp_parts": mark_dnp_parts,
    "exclude_testpoints_from_bom": exclude_testpoints_from_bom,
    "link_footprint_library": link_footprint_library,
    "footprint_internal_clearance_rule": footprint_internal_clearance_rule,
    "zone_thermal_reliefs": zone_thermal_reliefs,
    "thermal_spoke_rule": thermal_spoke_rule,
    "router_clearance_tolerance": router_clearance_tolerance,
    "strict_track_rules": strict_track_rules,
    "place_reference_designators": place_reference_designators,
    "fit_schematic_sheet": fit_schematic_sheet,
}


TRACK_FIXES_START = "repair_zero_width_tracks"  # first geometry fix: restore_nets rolls back to before it


SLOW_FIX_S = 5.0  # fixes slower than this get a timing line in the report


def apply_all(project: KicadProject, skip: set[str] | None = None) -> dict[str, list[str]]:
    """Run every registered fix (except ``skip``), save, and return the changes per fix (slow ones with their time)."""
    report = {}
    for name, fix in FIXES.items():
        if name == TRACK_FIXES_START:
            project.before_track_fixes = copy.deepcopy(project.pcb)
        if name not in (skip or set()):
            started = time.monotonic()
            report[name] = fix(project)
            if (spent := time.monotonic() - started) > SLOW_FIX_S:
                report[name].append(f"took {spent:.0f} s")
    project.save()
    return report


__all__ = [
    "COPPER_LAYERS",
    "DEFAULT_CLEARANCE_MM",
    "DEFAULT_EDGE_CLEARANCE_MM",
    "DEFAULT_HOLE_TO_HOLE_MM",
    "DEFAULT_MIN_TRACK_WIDTH_MM",
    "ISLANDS_KEPT",
    "ISLANDS_REMOVED",
    "KEEPOUT_NET",
    "KEEPOUT_VIA_NET",
    "PAD_EDGE_MARGIN_MM",
    "PAPER_SIZES_MM",
    "RAIL_LIB_PREFIX",
    "REF_TEXT_SIZE_BOTTOM_MM",
    "REF_TEXT_SIZE_MM",
    "SCHEMATIC_GRID_MM",
    "SHEET_FRAME_MM",
    "TITLE_BLOCK_MM",
    "TRACK_NODES",
    "UNROUTED_NAME",
    "_COORDS",
    "_SHEET_META",
    "Fix",
    "KicadProject",
    "_Copper",
    "_copper",
    "_copper_layers",
    "_design_clearances",
    "_design_min_track_width",
    "_design_via",
    "_escape_plans",
    "_footprint_geometry",
    "_footprints_by_ref",
    "_fp_to_board",
    "_graphic_points",
    "_is_keepout_zone",
    "_is_pour_zone",
    "_kicad_layer",
    "_label_anchor",
    "_layer",
    "_mm",
    "_net_names",
    "_net_node",
    "_net_of",
    "_num",
    "_overlaps",
    "_pad_corners",
    "_pad_obstacle",
    "_pad_via",
    "_pin_positions",
    "_relocate_via_rerouting",
    "_route_with_stub",
    "_segment_node",
    "_sheet_placement",
    "_source",
    "_split_tees",
    "_text_box",
    "_zone_net",
    "_zone_polygons",
    "add_part_numbers",
    "allow_footprints_in_keepouts",
    "apply_board_config",
    "apply_escapes",
    "apply_keepouts",
    "apply_post_fill",
    "apply_pours",
    "apply_routes",
    "apply_rule_profiles",
    "clean_tracks",
    "collapse_bend_stubs",
    "complete_netclasses",
    "copper_vias",
    "corner_touch_up",
    "drop_folded_back_tracks",
    "drop_loose_power_symbols",
    "drop_npth_edge_cuts",
    "drop_tracks_inside_pads",
    "drop_vias_at",
    "escape_offset",
    "exclude_testpoints_from_bom",
    "fill_schematic_footprints",
    "fit_schematic_sheet",
    "fix_value_equals_reference",
    "footprint_internal_clearance_rule",
    "link_footprint_library",
    "mark_dnp_parts",
    "mask_covered_pads",
    "merge_duplicate_vias",
    "net_placed_vias",
    "place_reference_designators",
    "plane_pad_vias",
    "pours_to_board_outline",
    "propagate_split_pad_nets",
    "prune_pour_tracks",
    "push_tracks_clear",
    "relocate_vias",
    "remove_antennas",
    "rename_auto_nets",
    "repair_zero_width_tracks",
    "reroute_named_nets",
    "reroute_tracks",
    "restore_nets",
    "router_clearance_tolerance",
    "schematic_net_labels",
    "schematic_no_connects",
    "schematic_power_symbols",
    "separate_ends_on_pads",
    "set_island_removal",
    "sharp_corners",
    "snap_bends_to_45",
    "snap_near_ends",
    "split_at_tees",
    "split_crossings",
    "square_tees",
    "standard_board_thickness",
    "stitch_pour_islands",
    "stitch_unconnected_pads",
    "straighten_via_exits",
    "strict_track_rules",
    "thermal_pad_vias",
    "thermal_spoke_rule",
    "tuck_corners_into_pads",
    "tune_lengths",
    "unlabelled_connectors",
    "unpoured_copper_layers",
    "widen_thin_vias",
    "zone_thermal_reliefs",
]
