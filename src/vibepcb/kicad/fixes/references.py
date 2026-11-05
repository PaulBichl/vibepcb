"""Silkscreen reference designators placed clear of pads, silk and each other."""

from __future__ import annotations

import itertools
import json
import math
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import (
    Box,
    KicadProject,
    _bbox,
    _design_clearances,
    _fp_to_board,
    _layer,
    _mm,
    _natural_key,
    _num,
    _overlaps,
    _pad_box,
    _pad_on_layer,
)
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibepcb.core.sexpr import Node


REF_TEXT_SIZE_MM = 1.0  # top references: JLCPCB asks >= 1.0 mm for legible silkscreen


# bottom references: the back of a two-sided board is densely packed (all the electronics); 0.65 mm with the
# 0.15 mm stroke stays readable under a loupe and roughly halves the references that find no free spot
REF_TEXT_SIZE_BOTTOM_MM = 0.65


# where the preferred size finds no free spot: smaller text, down to the bottom size (the board's min text height)
REF_TEXT_SIZES_MM = {"F": (REF_TEXT_SIZE_MM, 0.8, REF_TEXT_SIZE_BOTTOM_MM), "B": (REF_TEXT_SIZE_BOTTOM_MM,)}


REF_GAPS_MM = (0.05, 0.35, 0.7, 1.1, 1.6)  # text box to courtyard


REF_SHIFTS_MM = (0.0, -0.5, 0.5, -1.0, 1.0, -1.5, 1.5, -2.0, 2.0, -2.5, 2.5, -3.0, 3.0)  # along the courtyard side


REF_TEXT_THICKNESS_MM = 0.15


REF_CLEARANCE_MM = 0.15  # text box to pads / silk / other text


REF_EDGE_CLEARANCE_MM = 0.3


CHAR_ADVANCE = 0.9  # KiCad stroke font: glyph advance per text size, rounded up (0.8 let "R104" overlap "R100")


# board text as an obstacle for clipped silk lines: wide glyphs (+, -, capitals) run past the average advance
# (bench-psu's 22-character 'CH1+  CH1-  CH2+  CH2-' is ~0.9 per character in KiCad's DRC)
BOARD_TEXT_CHAR_ADVANCE = 0.95


def _text_box(
    x: float,
    y: float,
    text: str,
    size: float,
    angle: float,
    effects: list[Node] | None = None,
    advance: float = CHAR_ADVANCE,
) -> Box:
    """Approximate extent of a stroke-font text (``justify`` honoured, angle 0 or 90)."""
    w, h = len(text) * size * advance + REF_TEXT_THICKNESS_MM, size + REF_TEXT_THICKNESS_MM
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


# --- footprint silkscreen clipped clear of pads, other parts' silk and the board edge ---------------------------

SILK_STEP_MM = 0.02  # sampling step along a silk line
SILK_MIN_PIECE_MM = 0.2  # shorter pieces left after clipping are dropped (a dot, not an outline)
SILK_CLEARANCE_MM = 0.05  # silk line edge to another part's silk / board text (KiCad's default minimum: 0)
SILK_PAD_CLEARANCE_MM = 0.1  # silk line edge to a pad's copper (the mask opening around it)


@dataclass(frozen=True)
class _SilkObstacle:
    """A segment (2 points) or convex polygon (3+) the silk's edge keeps ``need`` mm away from."""

    points: tuple[tracks.Point, ...]
    need: float
    box: Box

    @staticmethod
    def of(points: list[tracks.Point], need: float) -> _SilkObstacle:
        return _SilkObstacle(tuple(points), need, _bbox(points, need))

    def distance(self, p: tracks.Point) -> float:
        pts = self.points
        if len(pts) <= 2:
            return tracks.point_seg_distance(p, pts[0], pts[-1])
        if tracks.point_in_polygon(p, list(pts)):
            return 0.0
        return min(tracks.point_seg_distance(p, a, b) for a, b in zip(pts, pts[1:] + pts[:1], strict=True))


def _stroke_width(item: list[Node]) -> float:
    stroke = sexpr.child(item, "stroke")
    width = sexpr.child(stroke if stroke is not None else item, "width")
    return _num(width, 1, 0.12)


def _filled(item: list[Node]) -> bool:
    fill = sexpr.child(item, "fill")
    return fill is not None and str(fill[1]) not in ("no", "none")


def _outline_segments(item: list[Node]) -> list[tuple[tracks.Point, tracks.Point]] | None:
    """Local straight segments of an unfilled ``fp_line`` / ``fp_rect`` / ``fp_poly``; None for anything else."""
    kind = sexpr.head(item)
    if kind not in ("fp_line", "fp_rect", "fp_poly") or _filled(item):
        return None
    points = _graphic_points(item)
    if kind == "fp_poly" and len(points) > 2 and points[0] != points[-1]:
        points.append(points[0])  # a polygon is closed
    return [(a, b) for a, b in itertools.pairwise(points) if a != b]


def _clip(a: tracks.Point, b: tracks.Point, half: float, obstacles: list[_SilkObstacle], inside: Box) -> list:
    """Pieces of segment ``a``-``b`` (stroke ``half`` width) clear of ``obstacles`` and inside ``inside``;
    ``[(a, b)]`` when nothing is in the way."""
    box = _bbox([a, b], half)
    near = [o for o in obstacles if _overlaps(box, o.box)]
    outside = not (inside[0] <= box[0] and inside[1] <= box[1] and box[2] <= inside[2] and box[3] <= inside[3])
    if not near and not outside:
        return [(a, b)]
    n = max(1, math.ceil(math.dist(a, b) / SILK_STEP_MM))
    samples = [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]

    def free(p: tracks.Point) -> bool:
        x, y = p
        if not (inside[0] + half <= x <= inside[2] - half and inside[1] + half <= y <= inside[3] - half):
            return False
        return all(o.distance(p) >= o.need + half for o in near)

    good = [free(p) for p in samples]
    if all(good):
        return [(a, b)]
    pieces, start = [], None
    for i, ok in enumerate([*good, False]):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            p, q = samples[start], samples[i - 1]
            if math.dist(p, q) >= SILK_MIN_PIECE_MM:
                pieces.append((p, q))
            start = None
    return pieces


def _board_to_fp(fp: list[Node]) -> Callable[[float, float], tuple[float, float]]:
    at = sexpr.child(fp, "at")
    fx, fy, angle = _num(at, 1), _num(at, 2), math.radians(_num(at, 3))
    cos, sin = math.cos(angle), math.sin(angle)
    return lambda x, y: ((x - fx) * cos - (y - fy) * sin, (x - fx) * sin + (y - fy) * cos)


def _silk_line(item: list[Node], a: tracks.Point, b: tracks.Point, width: float) -> list[Node]:
    """An ``fp_line`` (local ``a``-``b``) on ``item``'s layer and stroke, with a content-derived uuid."""
    uid = sexpr.child(item, "uuid")
    seed = f"{'' if uid is None else uid[1]}:{_mm(a[0])},{_mm(a[1])}:{_mm(b[0])},{_mm(b[1])}"
    layer = sexpr.child(item, "layer")
    return [
        Sym("fp_line"),
        [Sym("start"), Sym(_mm(a[0])), Sym(_mm(a[1]))],
        [Sym("end"), Sym(_mm(b[0])), Sym(_mm(b[1]))],
        [Sym("stroke"), [Sym("width"), Sym(_mm(width))], [Sym("type"), Sym("solid")]],
        layer if layer is not None else [Sym("layer"), "F.SilkS"],
        [Sym("uuid"), str(uuid.uuid5(uuid.NAMESPACE_OID, seed))],
    ]


def _edge_obstacles(pcb: list[Node], need: float) -> tuple[list[_SilkObstacle], Box | None]:
    """Board outline segments and the outline's bounding box (None without an outline)."""
    out, points = [], []
    for item in pcb:
        if not isinstance(item, list) or _layer(item) != "Edge.Cuts":
            continue
        if sexpr.head(item) in ("gr_line", "gr_rect", "gr_arc", "gr_poly"):
            pts = _graphic_points(item)
            points += pts
            out += [_SilkObstacle.of([p, q], need) for p, q in itertools.pairwise(pts)]
    return out, (_bbox(points) if points else None)


def _side_silk_obstacles(pcb: list[Node], side: str) -> tuple[list[_SilkObstacle], list[_SilkObstacle]]:
    """(pads of every part on ``side``, board-level silkscreen graphics and text on ``side``)."""
    pads, board = [], []
    for fp in sexpr.children(pcb, "footprint"):
        to_board = _fp_to_board(fp)
        for pad in sexpr.children(fp, "pad"):
            if _pad_on_layer(pad, f"{side}.Cu") or "thru_hole" in str(pad[2]):
                pads.append(_SilkObstacle.of(_pad_corners(pad, to_board), SILK_PAD_CLEARANCE_MM))
    for item in pcb:
        if not isinstance(item, list) or _layer(item) != f"{side}.SilkS":
            continue
        kind = sexpr.head(item)
        if kind in ("gr_line", "gr_rect", "gr_arc", "gr_poly", "gr_circle"):
            need = _stroke_width(item) / 2 + SILK_CLEARANCE_MM
            pts = _graphic_points(item)
            board += [_SilkObstacle.of([p, q], need) for p, q in itertools.pairwise(pts)]
        elif kind == "gr_text":
            at, effects = sexpr.child(item, "at"), sexpr.child(item, "effects")
            font = None if effects is None else sexpr.child(effects, "font")
            size = _num(None if font is None else sexpr.child(font, "size"), 1, 1.0)
            box = _text_box(_num(at, 1), _num(at, 2), str(item[1]), size, _num(at, 3), effects, BOARD_TEXT_CHAR_ADVANCE)
            x0, y0, x1, y1 = box
            board.append(_SilkObstacle.of([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], SILK_CLEARANCE_MM))
    return pads, board


def clip_footprint_silk(project: KicadProject) -> list[str]:
    """Footprint silkscreen lines cut back where they run over pads, other parts' silk or past the board edge.

    tscircuit draws each part's outline at its nominal size: two 0603 caps placed side by side share an
    outline edge, a switch outline crosses its own pads, a connector's outline overhangs the board edge
    (KiCad DRC: silk_overlap, silk_over_copper, silk_edge_clearance). Each unfilled ``fp_line`` / ``fp_rect`` /
    ``fp_poly`` on a silkscreen layer is sampled every :data:`SILK_STEP_MM`; the stretches too close to a pad
    (any part's, its own included), to the board outline, to board-level silk / text (the board source places
    those explicitly; they stay) or to the silk of a part earlier in reference order are removed (one of two
    touching outlines keeps the shared edge), pieces shorter than :data:`SILK_MIN_PIECE_MM` are dropped.
    Arcs, circles and filled shapes (pin-1 marks) are kept and are obstacles for the others.
    """
    pcb = project.pcb
    _, edge_clearance = _design_clearances(project)
    edges, outline = _edge_obstacles(pcb, edge_clearance)
    if outline is None:
        return []
    inside = _grow(outline, -edge_clearance)
    changes = []
    footprints = sorted(
        sexpr.children(pcb, "footprint"),
        key=lambda fp: _natural_key(str(sexpr.property_value(fp, "Reference") or "")),
    )
    for side in ("F", "B"):
        pads, board = _side_silk_obstacles(pcb, side)
        kept: dict[int, list[_SilkObstacle]] = {}  # per footprint: silk the later parts keep clear of
        for fp in footprints:
            to_board = _fp_to_board(fp)
            mine = kept.setdefault(id(fp), [])
            for item in fp:
                if not isinstance(item, list) or _layer(item) != f"{side}.SilkS" or sexpr.head(item) == "fp_text":
                    continue
                if _outline_segments(item) is None and str(sexpr.head(item) or "").startswith("fp_"):
                    need = _stroke_width(item) / 2 + SILK_CLEARANCE_MM
                    pts = [to_board(x, y) for x, y in _graphic_points(item)]
                    mine += [_SilkObstacle.of([p, q], need) for p, q in itertools.pairwise(pts)] if pts else []
        earlier: list[_SilkObstacle] = []
        for fp in footprints:
            ref = str(sexpr.property_value(fp, "Reference") or "?")
            to_board, to_fp = _fp_to_board(fp), _board_to_fp(fp)
            others = [o for key, obs in kept.items() if key != id(fp) for o in obs]
            obstacles = pads + board + edges + others + earlier
            cut = 0
            new_items: list[list[Node]] = []
            for item in list(fp):
                if not isinstance(item, list) or _layer(item) != f"{side}.SilkS":
                    continue
                segments = _outline_segments(item)
                if segments is None:
                    continue
                width = _stroke_width(item)
                pieces, changed = [], False
                for la, lb in segments:
                    a, b = to_board(*la), to_board(*lb)
                    clipped = _clip(a, b, width / 2, obstacles, inside)
                    changed |= clipped != [(a, b)]
                    pieces += clipped
                need = width / 2 + SILK_CLEARANCE_MM
                earlier += [_SilkObstacle.of([p, q], need) for p, q in pieces]
                if not changed:
                    continue
                cut += 1
                fp.remove(item)
                new_items += [_silk_line(item, to_fp(*p), to_fp(*q), width) for p, q in pieces]
            if cut:
                fp.extend(new_items)
                changes.append(f"{ref}: {cut} silkscreen line(s) clipped clear of pads / other silk / board edge")
    return changes


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
    # a reference on the fab layer is one this fix moved there (no free spot on the silkscreen): it is placed
    # again with the others, so a second run finds the same result
    if (
        prop is None
        or not prop[2]
        or _layer(prop) not in (f"{side}.SilkS", f"{side}.Fab")
        or _layer(fp) != f"{side}.Cu"
    ):
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
    Each reference gets 1.0 mm text (0.65 mm on the crowded bottom side), placed at the first candidate around its
    courtyard that stays inside the board and clear of pads, vias, holes, silkscreen, board-level text
    and references already placed; parts' bodies (courtyards) are avoided when possible. Where no
    candidate is free, smaller text is tried (top: 0.8, then 0.65 mm); where none fits either, the reference
    goes onto the fab layer (assembly drawing, centred on the part; reported) instead of over a pad or a text.
    Top and bottom parts are placed separately, each against its own side's silkscreen and pads.
    Runs after :func:`clip_footprint_silk`, so the outlines it avoids are the clipped ones.
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
    sizes = REF_TEXT_SIZES_MM[side]
    for geo in sorted(footprints, key=lambda geo: _natural_key(geo.ref)):
        bodies = [other.body for other in footprints if other is not geo]
        choice, size = None, sizes[0]
        for size in sizes:
            # other parts' bodies avoided if possible, then only what DRC flags (pads, silk, text, edge)
            choice = _free_spot(geo, size, board, [hard + placed + bodies, hard + placed])
            if choice is not None:
                break
        resized = _set_ref_font(geo.prop, size)
        if choice is None:
            # no free spot even at the smallest size: off the silkscreen onto the fab layer (assembly drawing),
            # centred on the part, rather than printed over a pad or another text
            x0, y0, x1, y1 = geo.body
            if _move_reference(geo, (x0 + x1) / 2, (y0 + y1) / 2, 0.0, f"{side}.Fab") or resized:
                changes.append(f"{geo.ref}: no free spot on the silkscreen - reference moved to {side}.Fab")
            continue
        x, y, angle, box = choice
        placed.append(_grow(box, REF_CLEARANCE_MM))
        if _move_reference(geo, x, y, angle, f"{side}.SilkS"):
            note = f", {_mm(size)} mm text" if size != sizes[0] else ""
            changes.append(f"{geo.ref}: reference at ({_mm(x)}, {_mm(y)}) {angle:g} deg{note}")
        elif resized:
            changes.append(f"{geo.ref}: reference resized")
    return changes


def _free_spot(
    geo: _Footprint, size: float, board: Box, obstacle_sets: list[list[Box]]
) -> tuple[float, float, float, Box] | None:
    """The first candidate around ``geo`` inside ``board`` clear of the first obstacle set that leaves one."""
    h = size + REF_TEXT_THICKNESS_MM
    w = len(geo.ref) * size * CHAR_ADVANCE + REF_TEXT_THICKNESS_MM
    candidates = []
    for x, y, angle in _ref_candidates(geo.body, w, h):
        box = _text_box(x, y, geo.ref, size, angle)
        if board[0] <= box[0] and board[1] <= box[1] and box[2] <= board[2] and box[3] <= board[3]:
            candidates.append((x, y, angle, box))
    for obstacles in obstacle_sets:
        for x, y, angle, box in candidates:
            if not any(_overlaps(box, o) for o in obstacles):
                return x, y, angle, box
    return None


def _move_reference(geo: _Footprint, x: float, y: float, angle: float, layer: str) -> bool:
    """Reference text of ``geo`` to board ``(x, y)`` / ``angle`` on ``layer``; returns whether anything changed."""
    at, fp_at = sexpr.child(geo.prop, "at"), sexpr.child(geo.node, "at")
    a = math.radians(_num(fp_at, 3))
    dx, dy = x - _num(fp_at, 1), y - _num(fp_at, 2)
    local = [
        Sym("at"),
        Sym(_mm(dx * math.cos(a) - dy * math.sin(a))),
        Sym(_mm(dx * math.sin(a) + dy * math.cos(a))),
        Sym(_mm(angle)),
    ]
    changed = False
    if at is not None and [str(v) for v in at] != [str(v) for v in local]:
        at[:] = local
        changed = True
    node = sexpr.child(geo.prop, "layer")
    if node is not None and str(node[1]) != layer:
        node[1] = layer if isinstance(node[1], str) else Sym(layer)
        changed = True
    return changed
