"""Zones: thermal vias, plane vias, keepout areas, pours, board-outline pours."""

from __future__ import annotations

import copy
import math
import uuid
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import (
    COPPER_LAYERS,
    Box,
    KicadProject,
    _bbox,
    _copper_layers,
    _design_clearances,
    _design_min_track_width,
    _footprints_by_ref,
    _fp_to_board,
    _layer,
    _mm,
    _net_names,
    _net_node,
    _net_of,
    _num,
    _pad_box,
)
from vibepcb.kicad.fixes.board import _box_has, _declared_net, _escape_boxes
from vibepcb.kicad.fixes.copper import _copper, _zone_polygons
from vibepcb.kicad.fixes.references import _graphic_points, _pad_corners
from vibepcb.kicad.fixes.stitch import _pad_via
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from vibepcb.core.sexpr import Node


THERMAL_VIA_DEFAULTS = {"pitch": 1.0, "drill": 0.3, "size": 0.5, "margin": 0.2}  # mm; JLCPCB 4-layer capable


def thermal_pad_vias(project: KicadProject) -> list[str]:
    """A grid of vias in a part's exposed pad (``[thermal_vias.<ref>]``): heat into the inner / bottom planes.

    tscircuit has no via-in-pad; without them a power IC's exposed pad only spreads heat on its own layer
    (LMR33630 at 3 A: Tj about 110 C instead of 75 C). The grid fills the pad's copper box minus ``margin``
    (via edge to pad edge) at ``pitch``, on the pad's net, through all layers. Small drills (0.3 mm) wick
    little solder; the fab tents / plugs them on request. The pad connects solid to the pours
    (``zone_connect 2``, overriding ``zone_thermal_reliefs``' spokes). Runs before the track clean-up,
    so later fixes treat the vias as obstacles and the connectivity roll-back keeps them.
    """
    specs = project.config.get("thermal_vias", {})
    if not specs:
        return []
    names = _net_names(project.pcb)
    existing = {
        (_mm(_num(sexpr.child(v, "at"), 1)), _mm(_num(sexpr.child(v, "at"), 2)))
        for v in sexpr.children(project.pcb, "via")
    }
    changes = []
    for ref, spec in sorted(specs.items()):
        cfg = {**THERMAL_VIA_DEFAULTS, **{k: float(v) for k, v in spec.items() if k != "pad"}}
        fp = next(
            (f for f in sexpr.children(project.pcb, "footprint") if _footprint_ref(f) == ref),
            None,
        )
        if fp is None:
            changes.append(f"{ref}: no such footprint, no thermal vias")
            continue
        pad_name = str(spec.get("pad", ""))
        pads = [pd for pd in sexpr.children(fp, "pad") if str(pd[2]) == "smd"]
        pad = (
            next((pd for pd in pads if str(pd[1]) == pad_name), None)
            if pad_name
            else max(pads, key=_pad_area, default=None)  # the exposed pad is the largest
        )
        net = None if pad is None else _net_of(pad, names)
        net_node = None if net is None else _net_node(project.pcb, net)
        if pad is None or net is None or net_node is None:
            changes.append(f"{ref}: pad {pad_name or '(largest)'} not found or on no net, no thermal vias")
            continue
        xmin, ymin, xmax, ymax = _bbox(_pad_corners(pad, _fp_to_board(fp)))
        inset = cfg["margin"] + cfg["size"] / 2
        nx = max(1, int((xmax - xmin - 2 * inset) / cfg["pitch"] + 1e-6) + 1)
        ny = max(1, int((ymax - ymin - 2 * inset) / cfg["pitch"] + 1e-6) + 1)
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        added = 0
        for i in range(nx):
            for j in range(ny):
                x = _mm(cx + (i - (nx - 1) / 2) * cfg["pitch"])
                y = _mm(cy + (j - (ny - 1) / 2) * cfg["pitch"])
                if (x, y) in existing:
                    continue
                uid = uuid.uuid5(uuid.NAMESPACE_OID, f"thermal-via:{ref}:{x},{y}")
                via = sexpr.loads(
                    f'(via (at {x} {y}) (size {_mm(cfg["size"])}) (drill {_mm(cfg["drill"])}) (layers "F.Cu" "B.Cu") '
                    f'(uuid "{uid}"))'
                )
                via.insert(len(via) - 1, copy.deepcopy(net_node))
                project.pcb.append(via)
                existing.add((x, y))
                added += 1
        if added:
            changes.append(f"{ref}: {added} thermal via(s) ({nx} x {ny}, {net}) in pad {pad[1]}")
        if _solid_zone_connect(pad):
            changes.append(f"{ref}: pad {pad[1]} connects solid to the pours (no thermal spokes on a heat path)")
    return changes


def _solid_zone_connect(pad: list[Node]) -> bool:
    """``(zone_connect 2)`` on the pad (KiCad: solid, overriding the zones' thermal reliefs); whether it changed."""
    node = sexpr.child(pad, "zone_connect")
    if node is not None:
        if str(node[1]) == "2":
            return False
        node[1] = Sym("2")
        return True
    uid = next((i for i, item in enumerate(pad) if sexpr.head(item) == "uuid"), len(pad))
    pad.insert(uid, [Sym("zone_connect"), Sym("2")])
    return True


def _footprint_ref(fp: list[Node]) -> str:
    text = next((t for t in sexpr.children(fp, "fp_text") if str(t[1]) == "reference"), None)
    return str(text[2]) if text is not None else (sexpr.property_value(fp, "Reference") or "")


def _pad_area(pad: list[Node]) -> float:
    xmin, ymin, xmax, ymax = _pad_box(pad)
    return (xmax - xmin) * (ymax - ymin)


def plane_pad_vias(project: KicadProject) -> list[str]:
    """Connect the SMD pads of plane nets to their plane through their own via, not by tracks.

    For the nets listed in the board config (``[planes] nets = [...]``, see ``vibepcb.config``):
    ``build`` keeps them out of tscircuit's routing (pad-to-pad tracks knotted up in fine-pitch pin
    fields, 0.5 mm QFP GND / VCC pins), so their only tracks are the ones the TSX draws itself (a
    pin boxed in by its neighbours' fan-out, joined to a nearby pad of the net): those stay. Each
    SMD pad gets a minimum-width track to a via beside it (<= 3 mm, clearance on every layer, inside
    the net's zone on another layer) - common practice; a pad without room joins a neighbouring pad
    of the net that has one (adjacent GND pins), else its own layer's pour or a TSX track must reach it
    (``check`` reports it otherwise).
    """
    nets = list(project.config.get("planes", {}).get("nets", []))
    if not nets:
        return []
    changes = []
    escapes = _escape_boxes(project)  # escaped ball fields (vibepcb.fanout) keep their own escapes
    for net in nets:
        copper = _copper(project.pcb)
        if not any(n == net for n, _ in copper.zones):
            changes.append(f"{net}: no zone on the board, no pad vias")
            continue
        others = [o for o in copper.obstacles() if o.net != net]
        added: list[list[Node]] = []
        smd = [
            o
            for o in copper.pads_by_net.get(net, [])
            if len(o.layers & frozenset(COPPER_LAYERS)) == 1 and not any(_box_has(b, o.centre) for b in escapes)
        ]
        # vias of the net the TSX placed itself (under a chip body, next to a pin boxed in by its neighbours)
        placed = [
            tracks.Obstacle("<via>", frozenset(COPPER_LAYERS), (c,), r) for c, r in copper.vias_by_net.get(net, [])
        ]
        with_via: list[tracks.Obstacle] = []
        no_room = []
        for pad in smd:
            if (link := _pad_link(project, pad, placed, others, net, max_mm=VIA_LINK_MAX_MM)) is not None:
                added.append(link)
                with_via.append(pad)
                continue
            nodes = _pad_via(project, copper, pad, net, others)
            if nodes is None:
                no_room.append(pad)
                continue
            added += nodes
            with_via.append(pad)
            via_at = sexpr.child(nodes[1], "at")
            centre = (_num(via_at, 1), _num(via_at, 2))
            r = _num(sexpr.child(nodes[1], "size"), 1) / 2
            others.append(tracks.Obstacle("<new via>", frozenset(COPPER_LAYERS), (centre,), r))  # spacing to the next
        # a pad without room joins a neighbouring pad of the net that has its via (adjacent GND pins)
        stuck = []
        for pad in no_room:
            link = _pad_link(project, pad, with_via, others, net)
            if link is None:
                stuck.append(pad)
            else:
                added.append(link)
        project.pcb += added
        changes.append(
            f"{net}: {len(with_via)} pad vias to the plane"
            + (f" ({len(no_room) - len(stuck)} pad(s) joined to a neighbour)" if len(no_room) > len(stuck) else "")
        )
        changes += [
            f"{net}: pad at ({_mm(p.centre[0])}, {_mm(p.centre[1])}) has no room for a via (pour or a TSX track must"
            " reach it)"
            for p in stuck
        ]
    return changes


PAD_LINK_MAX_MM = 1.6  # a pad without via room may join a same-net pad this close (centre to centre)


VIA_LINK_MAX_MM = 3.0  # a pad joins a via of its net placed by the TSX this close (centre to centre)


def _pad_link(
    project: KicadProject,
    pad: tracks.Obstacle,
    targets: list[tracks.Obstacle],
    others: list[tracks.Obstacle],
    net: str,
    *,
    max_mm: float = PAD_LINK_MAX_MM,
) -> list[Node] | None:
    """A straight minimum-width track from ``pad`` to the nearest of ``targets`` on its layer (<= ``max_mm``
    centre to centre), if it keeps the clearance to ``others`` (other nets' copper); None if there is none."""
    clearance, _ = _design_clearances(project)
    width = _design_min_track_width(project)
    layer = next((lay for lay in COPPER_LAYERS if lay in pad.layers), None)
    net_node = _net_node(project.pcb, net)
    if layer is None or net_node is None:
        return None
    on_layer = [o for o in others if layer in o.layers]
    for target in sorted(targets, key=lambda t: math.dist(t.centre, pad.centre)):
        if math.dist(target.centre, pad.centre) > max_mm:
            break
        if layer not in target.layers:
            continue
        a, b = pad.centre, target.centre
        if all(o.distance(a, b) >= clearance + width / 2 for o in on_layer):
            key = f"{_mm(a[0])},{_mm(a[1])}:{_mm(b[0])},{_mm(b[1])}"
            seg = sexpr.loads(
                f"(segment (start {_mm(a[0])} {_mm(a[1])}) (end {_mm(b[0])} {_mm(b[1])}) (width {_mm(width)}) "
                f'(layer "{layer}") (uuid "{uuid.uuid5(uuid.NAMESPACE_OID, "pad-link:" + net + ":" + key)}"))'
            )
            seg.insert(len(seg) - 1, copy.deepcopy(net_node))
            return seg
    return None


KEEPOUT_ZONE_PREFIX = "vibepcb:keepout:"


def _part_area(project: KicadProject, ref: str) -> Box | None:
    """Board-frame bounding box of a part: its pads and its courtyard (F/B.CrtYd)."""
    fp = _footprints_by_ref(project.pcb).get(ref)
    if fp is None:
        return None
    to_board = _fp_to_board(fp)
    points = []
    for pad in sexpr.children(fp, "pad"):
        points += _pad_corners(pad, to_board)
    for g in fp:
        if isinstance(g, list) and _layer(g) in ("F.CrtYd", "B.CrtYd"):
            points += [to_board(x, y) for x, y in _graphic_points(g)]
    return _bbox(points) if points else None


def apply_keepouts(project: KicadProject) -> list[str]:
    """Keepout areas from the board config (``[keepout.<name>]``, see ``vibepcb.config``).

    A global pour (GND) under an antenna, a crystal or a switching node is wrong; here each keepout is a
    KiCad rule area over a part's pads / courtyard (or a box) that forbids a pour, tracks and / or vias
    on the chosen layers. KiCad's zone fill and DRC enforce it, and the router stays out of the track and
    via areas. Rewritten from the config on every run.
    """
    cfg = project.config.get("keepout", {})
    for z in [z for z in sexpr.children(project.pcb, "zone") if _is_keepout_zone(z)]:
        project.pcb.remove(z)
    layer_names = {
        str(n[1]) for n in (sexpr.child(project.pcb, "layers") or [])[1:] if isinstance(n, list) and len(n) > 1
    }
    board_layers = [lay for lay in COPPER_LAYERS if lay in layer_names]
    changes = []
    for name, entry in cfg.items():
        if "area" in entry:
            x0, y0, x1, y1 = (float(v) for v in entry["area"])
        else:
            part = _part_area(project, entry["ref"])
            if part is None:
                changes.append(f"{name}: part {entry['ref']} not on the board, keepout skipped")
                continue
            x0, y0, x1, y1 = part
        grow = float(entry.get("margin", 0.0))
        x0, y0, x1, y1 = x0 - grow, y0 - grow, x1 + grow, y1 + grow
        layers = [lay for lay in entry.get("layers", board_layers) if lay in board_layers]
        block = set(entry.get("block", ["pour"]))
        rules = (
            "".join(
                f"({rule} {'not_allowed' if key in block else 'allowed'})"
                for key, rule in (("tracks", "tracks"), ("vias", "vias"), ("pour", "copperpour"))
            )
            + "(pads allowed)(footprints allowed)"
        )
        uid = uuid.uuid5(uuid.NAMESPACE_OID, f"keepout:{name}")
        layer_list = " ".join(f'"{lay}"' for lay in layers)
        project.pcb.append(sexpr.loads(
            f'(zone (net 0) (net_name "") (layers {layer_list}) (uuid "{uid}") (name "{KEEPOUT_ZONE_PREFIX}{name}") '
            f"(hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25) (keepout {rules}) "
            f'(placement (enabled no) (sheetname "")) (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) '
            f"(polygon (pts (xy {_mm(x0)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y1)}) (xy {_mm(x0)} {_mm(y1)}))))"
        ))  # fmt: skip
        changes.append(
            f"{name}: no {', '.join(sorted(block))} on {', '.join(layers)} in ({_mm(x0)}, {_mm(y0)})-({_mm(x1)}, {_mm(y1)})"
        )
    return changes


POUR_ZONE_PREFIX = "vibepcb:pour:"


def apply_pours(project: KicadProject) -> list[str]:
    """Copper pours from the board config (``[pour.<name>]``, see ``vibepcb.config``).

    tscircuit only emits a pour once routing happened, so the placement-only build of a board with
    ``[routing] mode = "manual"`` has none on a plane layer of a supply (a ``<copperpour>`` on an inner
    layer). A pour for a net and layer the board has no zone for yet is added here, as a copy of the
    board's existing pour settings (else KiCad's usual ones), over the board outline inset by ``margin``.
    With ``area`` (x0, y0, x1, y1, board mm) the pour covers only that box - a split plane or a patch on a
    signal layer (e.g. a BGA's core supply under it) - and ``priority`` lets it win over a full pour of the
    same layer; such pours are rewritten from the config on every run.
    """
    cfg = project.config.get("pour", {})
    zones = [z for z in sexpr.children(project.pcb, "zone") if sexpr.child(z, "keepout") is None]
    have = {(_zone_net(project.pcb, z), lay) for z in zones for lay in _copper_layers(z)}
    template = next((z for z in zones if sexpr.child(z, "fill") is not None), None) or sexpr.loads(
        '(zone (net 0) (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000000") (hatch edge 0.5) '
        "(connect_pads (clearance 0.2)) (min_thickness 0.25) "
        "(fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3) (island_removal_mode 0)))"
    )
    edge_points = [
        pt
        for g in project.pcb
        if isinstance(g, list)
        and sexpr.head(g) in ("gr_line", "gr_rect", "gr_arc", "gr_poly")
        and _layer(g) == "Edge.Cuts"
        for pt in _graphic_points(g)
    ]
    changes = []
    for z in [z for z in sexpr.children(project.pcb, "zone") if _is_pour_zone(z)]:
        project.pcb.remove(z)  # area pours are rewritten from the config on every run
    for name, entry in cfg.items():
        net, margin = str(entry["net"]), float(entry.get("margin", 0.5))
        declared = _declared_net(project.pcb, net)
        if declared is None:
            changes.append(f"{name}: net {net} not on the board, pour skipped")
            continue
        if not edge_points:
            changes.append(f"{name}: no board outline, pour skipped")
            continue
        x0, y0, x1, y1 = _bbox(edge_points, -margin)
        area = entry.get("area")
        if area is not None:  # a region of the layer (a split plane, a patch under a part), inside the outline
            ax0, ay0, ax1, ay1 = (float(v) for v in area)
            x0, y0, x1, y1 = max(x0, ax0), max(y0, ay0), min(x1, ax1), min(y1, ay1)
        for layer in entry["layers"]:
            if area is None and (net, layer) in have:
                continue
            zone = copy.deepcopy(template)
            for key in ("net", "net_name", "layer", "layers", "filled_polygon", "polygon"):
                for old in list(sexpr.children(zone, key)):
                    zone.remove(old)
            uid = uuid.uuid5(uuid.NAMESPACE_OID, f"pour:{name}:{layer}")
            head = [copy.deepcopy(declared), [Sym("layer"), layer]]
            if len(declared) > 2:  # (net <id> "<name>") on a pad: a zone writes (net <id>) (net_name "<name>")
                head = [[Sym("net"), declared[1]], [Sym("net_name"), net], [Sym("layer"), layer]]
            outline = sexpr.loads(
                f"(polygon (pts (xy {_mm(x0)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y1)}) (xy {_mm(x0)} {_mm(y1)})))"
            )
            uid_node = sexpr.child(zone, "uuid")
            if uid_node is not None:
                uid_node[1] = str(uid)
            zone[1:1] = head
            if area is not None:
                zone.insert(len(head) + 1, [Sym("name"), f"{POUR_ZONE_PREFIX}{name}"])
                for old in list(sexpr.children(zone, "priority")):
                    zone.remove(old)
                zone.insert(len(head) + 2, [Sym("priority"), Sym(str(int(entry.get("priority", 1))))])
            zone.append(outline)
            project.pcb.append(zone)
            where = f" in ({_mm(x0)}, {_mm(y0)})-({_mm(x1)}, {_mm(y1)})" if area is not None else ""
            changes.append(f"{name}: {net} pour on {layer}{where}")
    return changes


def pours_to_board_outline(project: KicadProject) -> list[str]:
    """One zone per net and layer over the whole pour area, instead of tscircuit's poured shapes.

    tscircuit exports its pour result as the zone outlines: each poured piece its own zone (13 on one layer of
    open-theremin), with holes wherever it had left a pocket empty. KiCad's refill only fills inside the
    outlines, so a pocket tscircuit skipped stayed bare copper-free board whatever the later stitching did. The
    zones of a net on a layer become one, with the first one's settings, over the bounding box of their
    outlines (the board inset by the pour's edge margin). Config pours (``[pour.<name>]``) and keepouts stay.
    """
    groups: dict[tuple[str, str], list[list[Node]]] = {}
    for z in sexpr.children(project.pcb, "zone"):
        if sexpr.child(z, "keepout") is not None or _is_pour_zone(z):
            continue
        layers = _copper_layers(z)
        if len(layers) != 1:
            continue
        groups.setdefault((_zone_net(project.pcb, z), next(iter(layers))), []).append(z)
    changes = []
    for (net, layer), zones in sorted(groups.items()):
        outlines = [pts for z in zones for _, pts in _zone_polygons(z, filled=False)]
        if len(zones) == 1 and all(len(pts) <= 4 for pts in outlines):
            continue  # already one plain area
        points = [q for pts in outlines for q in pts]
        if not points:
            continue
        x0, y0, x1, y1 = _bbox(points)
        keep = zones[0]
        for z in zones[1:]:
            project.pcb.remove(z)
        for old in [*sexpr.children(keep, "polygon"), *sexpr.children(keep, "filled_polygon")]:
            keep.remove(old)
        keep.append(
            sexpr.loads(
                f"(polygon (pts (xy {_mm(x0)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y0)}) (xy {_mm(x1)} {_mm(y1)}) "
                f"(xy {_mm(x0)} {_mm(y1)})))"
            )
        )
        changes.append(
            f"{net} on {layer}: {len(zones)} poured shape(s) ({sum(len(o) for o in outlines)} points) -> one zone over "
            f"({_mm(x0)}, {_mm(y0)})-({_mm(x1)}, {_mm(y1)})"
        )
    return changes


def _is_pour_zone(zone: list[Node]) -> bool:
    name = sexpr.child(zone, "name")
    return name is not None and len(name) > 1 and str(name[1]).startswith(POUR_ZONE_PREFIX)


def _zone_net(pcb: list[Node], zone: list[Node]) -> str:
    return _net_of(zone, _net_names(pcb))


def _is_keepout_zone(zone: list[Node]) -> bool:
    name = sexpr.child(zone, "name")
    return name is not None and len(name) > 1 and str(name[1]).startswith(KEEPOUT_ZONE_PREFIX)
