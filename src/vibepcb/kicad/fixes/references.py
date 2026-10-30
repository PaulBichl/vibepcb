"""Silkscreen reference designators placed clear of pads, silk and each other."""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import (
    Box,
    KicadProject,
    _bbox,
    _fp_to_board,
    _layer,
    _mm,
    _natural_key,
    _num,
    _overlaps,
    _pad_box,
    _pad_on_layer,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibepcb.core.sexpr import Node


REF_TEXT_SIZE_MM = 1.0  # top references: JLCPCB asks >= 1.0 mm for legible silkscreen


# bottom references: the back of a two-sided board is densely packed (all the electronics); 0.65 mm with the
# 0.15 mm stroke stays readable under a loupe and roughly halves the references that find no free spot
REF_TEXT_SIZE_BOTTOM_MM = 0.65


REF_GAPS_MM = (0.05, 0.35, 0.7, 1.1)  # text box to courtyard


REF_SHIFTS_MM = (0.0, -0.5, 0.5, -1.0, 1.0, -1.5, 1.5, -2.0, 2.0, -2.5, 2.5)  # along the courtyard side


REF_TEXT_THICKNESS_MM = 0.15


REF_CLEARANCE_MM = 0.15  # text box to pads / silk / other text


REF_EDGE_CLEARANCE_MM = 0.3


CHAR_ADVANCE = 0.8  # KiCad stroke font: average glyph advance per text size (rough)


def _text_box(x: float, y: float, text: str, size: float, angle: float, effects: list[Node] | None = None) -> Box:
    """Approximate extent of a stroke-font text (``justify`` honoured, angle 0 or 90)."""
    w, h = len(text) * size * CHAR_ADVANCE + REF_TEXT_THICKNESS_MM, size + REF_TEXT_THICKNESS_MM
    justify = sexpr.child(effects, "justify") if effects is not None else None
    flags = {str(f) for f in justify[1:]} if justify is not None else set()
    dx = w / 2 if "left" in flags else -w / 2 if "right" in flags else 0.0
    dy = -h / 2 if "bottom" in flags else h / 2 if "top" in flags else 0.0
    if round(angle) % 180 == 90:
        return x + dy - h / 2, y - dx - w / 2, x + dy + h / 2, y - dx + w / 2
    return x + dx - w / 2, y + dy - h / 2, x + dx + w / 2, y + dy + h / 2


def _graphic_points(node: list[Node]) -> list[tuple[float, float]]:
    if sexpr.head(node) == "fp_circle":
        center, end = sexpr.child(node, "center"), sexpr.child(node, "end")
        cx, cy = _num(center, 1), _num(center, 2)
        r = math.hypot(_num(end, 1) - cx, _num(end, 2) - cy)
        return [(cx - r, cy - r), (cx + r, cy + r)]
    if sexpr.head(node) == "fp_rect":
        (x1, y1), (x2, y2) = ((_num(p, 1), _num(p, 2)) for p in (sexpr.child(node, "start"), sexpr.child(node, "end")))
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
    keys = ("start", "mid", "end")
    pts = [(_num(p, 1), _num(p, 2)) for k in keys if (p := sexpr.child(node, k)) is not None]
    return pts + [(_num(xy, 1), _num(xy, 2)) for xy in sexpr.walk(node) if sexpr.head(xy) == "xy"]


def _segment_boxes(points: list[tuple[float, float]], grow: float) -> list[Box]:
    if len(points) == 1:
        return [_bbox(points, grow)]
    return [_bbox([a, b], grow) for a, b in itertools.pairwise(points)]


@dataclass
class _Footprint:
    ref: str
    node: list[Node]
    prop: list[Node]
    body: Box  # courtyard (or pads) extent
    keepouts: list[Box]  # own pads + silk


def _pad_corners(pad: list[Node], to_board: Callable[[float, float], tuple[float, float]]) -> list[tuple[float, float]]:
    """Board coordinates of the pad's copper bounding box corners (pad rotation included)."""
    pad_at = sexpr.child(pad, "at")
    angle = math.radians(_num(pad_at, 3))
    px, py = to_board(_num(pad_at, 1), _num(pad_at, 2))
    xmin, ymin, xmax, ymax = _pad_box(pad)
    # pad frame -> board: rotate by the pad's absolute angle around its board position
    return [
        (px + u * math.cos(angle) + v * math.sin(angle), py - u * math.sin(angle) + v * math.cos(angle))
        for u, v in ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax))
    ]


def _footprint_geometry(fp: list[Node], side: str = "F") -> _Footprint | None:
    """Reference, body and keepouts of a part on ``side`` (``F`` top, ``B`` bottom); None for the other side's
    parts and for hidden references. Bottom footprints carry their geometry already flipped, on ``B.*`` layers."""
    # tscircuit writes a visible ``(fp_text reference ...)`` next to a hidden Reference property
    # (KiCad merges the two on save); both keep the text at index 2
    prop = next((t for t in sexpr.children(fp, "fp_text") if str(t[1]) == "reference"), None)
    prop = prop if prop is not None else sexpr.property_node(fp, "Reference")
    if prop is None or not prop[2] or _layer(prop) != f"{side}.SilkS" or _layer(fp) != f"{side}.Cu":
        return None
    effects = sexpr.child(prop, "effects")
    for parent in (prop, effects):
        if parent is not None and (hide := sexpr.child(parent, "hide")) is not None and str(hide[-1]) != "no":
            return None
    to_board = _fp_to_board(fp)
    keepouts, body_points = [], []
    for pad in sexpr.children(fp, "pad"):
        if not (_pad_on_layer(pad, f"{side}.Cu") or str(pad[2]) == "np_thru_hole"):
            continue
        corners = _pad_corners(pad, to_board)
        keepouts.append(_bbox(corners, REF_CLEARANCE_MM))
        body_points += corners
    courtyard = []
    for item in fp:
        if (
            not isinstance(item, list)
            or not str(sexpr.head(item) or "").startswith("fp_")
            or sexpr.head(item) == "fp_text"
        ):
            continue
        points = [to_board(x, y) for x, y in _graphic_points(item)]
        if not points:
            continue
        if _layer(item) == f"{side}.SilkS":
            keepouts += _segment_boxes(points, REF_CLEARANCE_MM)
        elif _layer(item) == f"{side}.CrtYd":
            courtyard += points
    body_points = courtyard or body_points
    if not body_points:
        return None
    return _Footprint(str(prop[2]), fp, prop, _bbox(body_points), keepouts)


def _ref_candidates(body: Box, w: float, h: float) -> list[tuple[float, float, float]]:
    """``(x, y, angle)`` text centres around ``body``, preferred first."""
    xmin, ymin, xmax, ymax = body
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    out = []
    for gap in REF_GAPS_MM:
        for shift in REF_SHIFTS_MM:
            out += [
                (cx + shift, ymin - gap - h / 2, 0.0),  # above
                (cx + shift, ymax + gap + h / 2, 0.0),  # below
                (xmin - gap - h / 2, cy + shift, 90.0),  # left, reading upwards
                (xmax + gap + h / 2, cy + shift, 90.0),  # right
                (xmin - gap - w / 2, cy + shift, 0.0),
                (xmax + gap + w / 2, cy + shift, 0.0),
            ]
    return out


def _grow(box: Box, d: float) -> Box:
    return box[0] - d, box[1] - d, box[2] + d, box[3] + d


def _fixed_obstacles(pcb: list[Node], footprints: list[_Footprint], side: str = "F") -> list[Box]:
    """Board-level things a reference on ``side`` must not cover: edges, that side's silkscreen, vias, and the
    pads of every other part on that side (reference-less parts, the other side's through-hole pads)."""
    obstacles: list[Box] = []
    for item in pcb:
        if not isinstance(item, list):
            continue
        kind, layer = sexpr.head(item), _layer(item)
        if kind in ("gr_line", "gr_rect", "gr_arc", "gr_poly") and layer in ("Edge.Cuts", f"{side}.SilkS"):
            grow = REF_EDGE_CLEARANCE_MM if layer == "Edge.Cuts" else REF_CLEARANCE_MM
            obstacles += _segment_boxes(_graphic_points(item), grow)
        elif kind == "gr_text" and layer == f"{side}.SilkS":
            at, effects = sexpr.child(item, "at"), sexpr.child(item, "effects")
            font = None if effects is None else sexpr.child(effects, "font")
            size = _num(None if font is None else sexpr.child(font, "size"), 1, 1.0)
            box = _text_box(_num(at, 1), _num(at, 2), str(item[1]), size, _num(at, 3), effects)
            obstacles.append(_grow(box, REF_CLEARANCE_MM))
        elif kind == "via":
            at, r = sexpr.child(item, "at"), _num(sexpr.child(item, "size"), 1) / 2
            obstacles.append(_grow((_num(at, 1), _num(at, 2), _num(at, 1), _num(at, 2)), r + REF_CLEARANCE_MM))
    with_ref = {id(g.node) for g in footprints}
    for fp in sexpr.children(pcb, "footprint"):  # mounting holes etc.
        if id(fp) in with_ref:
            continue
        to_board = _fp_to_board(fp)
        for pad in sexpr.children(fp, "pad"):
            if not (_pad_on_layer(pad, f"{side}.Cu") or "thru_hole" in str(pad[2])):
                continue  # an SMD pad on the other side
            pad_size, pad_at = sexpr.child(pad, "size"), sexpr.child(pad, "at")
            x, y = to_board(_num(pad_at, 1), _num(pad_at, 2))
            r = max(_num(pad_size, 1), _num(pad_size, 2)) / 2
            obstacles.append(_grow((x, y, x, y), r + REF_CLEARANCE_MM))
    return obstacles


def _set_ref_font(text: list[Node], size_mm: float = REF_TEXT_SIZE_MM) -> bool:
    """Reference text to ``size_mm``, centred (bottom text stays mirrored); returns whether the size changed."""
    effects = sexpr.child(text, "effects")
    font = None if effects is None else sexpr.child(effects, "font")
    if effects is None or font is None:
        return False
    mirrored = any(sexpr.head(e) == "justify" and Sym("mirror") in e for e in effects)
    effects[:] = [e for e in effects if sexpr.head(e) != "justify"]
    if mirrored:
        effects.append([Sym("justify"), Sym("mirror")])
    changed = False
    size, thickness = Sym(_mm(size_mm)), Sym(_mm(REF_TEXT_THICKNESS_MM))
    for key, value in (("size", [size, size]), ("thickness", [thickness])):
        node = sexpr.child(font, key)
        if node is None:
            font.append([Sym(key), *value])
        elif [str(v) for v in node[1:]] != [str(v) for v in value]:
            node[1:] = value
        else:
            continue
        changed = True
    return changed


def place_reference_designators(project: KicadProject) -> list[str]:
    """Size reference designators legibly and move each to a free spot next to its part.

    tscircuit writes 0.4 mm references (below every fab's minimum) at a fixed offset, so
    they land on pads, on neighbours' silkscreen, on each other or beyond the board edge.
    Each reference gets 1.0 mm text (0.65 mm on the crowded bottom side), placed at the first candidate around its courtyard
    that stays inside the board and clear of pads, vias, holes, silkscreen, board-level text
    and references already placed; parts' bodies (courtyards) are avoided when possible. Where no
    candidate is free, the one covering the fewest of those things is taken (reported).
    Top and bottom parts are placed separately, each against its own side's silkscreen and pads.
    """
    pcb = project.pcb
    edge_points = [
        p for item in pcb if isinstance(item, list) and sexpr.head(item) in ("gr_line", "gr_rect", "gr_arc", "gr_poly")
        and _layer(item) == "Edge.Cuts" for p in _graphic_points(item)
    ]  # fmt: skip
    if not edge_points:
        return []
    board = _bbox(edge_points, -REF_EDGE_CLEARANCE_MM)
    changes = []
    for side in ("F", "B"):
        changes += _place_side_references(pcb, board, side)
    # KiCad's default minimum text height (0.8 mm) would flag every bottom reference: the design rule follows the
    # size chosen here, so DRC still catches text smaller than that
    pro = project.pcb_path.with_suffix(".kicad_pro")
    if pro.exists():
        data = json.loads(pro.read_text(encoding="utf-8"))
        rules = data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
        smallest = min(REF_TEXT_SIZE_MM, REF_TEXT_SIZE_BOTTOM_MM)
        if rules.get("min_text_height") != smallest:
            rules["min_text_height"] = smallest
            pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            changes.append(f"{pro.name}: min text height {_mm(smallest)} mm (bottom references)")
    return changes


def _place_side_references(pcb: list[Node], board: Box, side: str) -> list[str]:
    footprints = [geo for fp in sexpr.children(pcb, "footprint") if (geo := _footprint_geometry(fp, side)) is not None]
    hard = _fixed_obstacles(pcb, footprints, side) + [box for geo in footprints for box in geo.keepouts]
    changes = []
    placed: list[Box] = []
    size = REF_TEXT_SIZE_MM if side == "F" else REF_TEXT_SIZE_BOTTOM_MM
    h = size + REF_TEXT_THICKNESS_MM
    for geo in sorted(footprints, key=lambda geo: _natural_key(geo.ref)):
        resized = _set_ref_font(geo.prop, size)
        w = len(geo.ref) * size * CHAR_ADVANCE + REF_TEXT_THICKNESS_MM
        bodies = [other.body for other in footprints if other is not geo]
        choice = None
        for obstacles in (hard + placed + bodies, hard + placed):  # avoid other parts' bodies if possible
            for x, y, angle in _ref_candidates(geo.body, w, h):
                box = _text_box(x, y, geo.ref, size, angle)
                inside = board[0] <= box[0] and board[1] <= box[1] and box[2] <= board[2] and box[3] <= board[3]
                if inside and not any(_overlaps(box, o) for o in obstacles):
                    choice = (x, y, angle, box)
                    break
            if choice is not None:
                break
        crowded = 0
        if choice is None:  # no free spot: the candidate covering the fewest things beats tscircuit's fixed offset
            scored = []
            for x, y, angle in _ref_candidates(geo.body, w, h):
                box = _text_box(x, y, geo.ref, size, angle)
                if board[0] <= box[0] and board[1] <= box[1] and box[2] <= board[2] and box[3] <= board[3]:
                    scored.append((sum(_overlaps(box, o) for o in hard + placed), (x, y, angle, box)))
            if not scored:
                if resized:
                    changes.append(f"{geo.ref}: reference resized, no spot inside the board - check by eye")
                continue
            crowded, choice = min(scored, key=lambda c: c[0])
        x, y, angle, box = choice
        placed.append(_grow(box, REF_CLEARANCE_MM))
        at, fp_at = sexpr.child(geo.prop, "at"), sexpr.child(geo.node, "at")
        a = math.radians(_num(fp_at, 3))
        dx, dy = x - _num(fp_at, 1), y - _num(fp_at, 2)
        local = [
            Sym("at"),
            Sym(_mm(dx * math.cos(a) - dy * math.sin(a))),
            Sym(_mm(dx * math.sin(a) + dy * math.cos(a))),
            Sym(_mm(angle)),
        ]
        if at is not None and [str(v) for v in at] != [str(v) for v in local]:
            at[:] = local
            note = f", no free spot: covers {crowded} item(s) - check by eye" if crowded else ""
            changes.append(f"{geo.ref}: reference at ({_mm(x)}, {_mm(y)}) {angle:g} deg{note}")
        elif resized:
            changes.append(f"{geo.ref}: reference resized")
    return changes
