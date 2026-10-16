"""BGA escapes made by VibePCB instead of tscircuit's router (config: ``[escape.<ref>]``).

tscircuit's router fails on ball grids: it crowds the escape vias on one side, its dogbone solver
crashes on boards with copper pours (tscircuit/core#4660), and saved escape paths
(``<fanout pcbTracePaths>``) crash its TopologyMergingSolver (tscircuit/core#4663; core 0.0.2159). So the escape is planned here, from
the placement-only circuit JSON, and tscircuit never sees the ball field:

1. :func:`plan` - every connected ball leaves the package on the side nearest to it (ties: the
   side facing the rest of its net). Ring 0 (outermost) goes straight out on the top layer, ring 1
   through the channel between two ring-0 balls, deeper balls take a dogbone (short stub to a via
   in the middle of four balls) and leave on an escape layer along a ball line (no pads there).
   All escapes end on one line per side, just outside the package. Balls of plane nets (``[planes]``)
   only get a via to their plane (in a free cell beside the ball; an edge ball hemmed in by channels
   gets a short stub out of the package instead).
2. :func:`standin_tsx` - for the routed evaluation the part gets a stand-in footprint: a small pad
   at each escape end (on the escape's layer) and its silkscreen, and a keepout over the package.
   tscircuit routes the board to the escape ends; the ports of via-only balls have no pad, so it
   leaves them alone.
3. ``fixes.apply_escapes`` puts the real balls back in KiCad and adds the escape tracks and vias.

Coordinates are the part's own (tscircuit footprint frame: unrotated, y up, mm); the part's
rotation must be a multiple of 90 degrees.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb import tracks

if TYPE_CHECKING:
    from pathlib import Path

Point = tuple[float, float]

SIDES: dict[str, Point] = {"N": (0.0, 1.0), "E": (1.0, 0.0), "S": (0.0, -1.0), "W": (-1.0, 0.0)}
ALIGN_WEIGHT = 0.9  # side choice: one ring deeper is worth less than facing the net's other pads
STANDIN_PAD_MM = 0.2  # square stand-in pad at an escape end
STAGGER_PITCHES = 1.0  # channel escapes end this many pitches further out than the straight ones
GRID_TOL = 0.02  # mm: a ball within this of a grid point is on it


class EscapeError(ValueError):
    """The part can't be escaped (not a ball grid, no room for a ball)."""


@dataclass(frozen=True)
class Rules:
    track_width: float = 0.1
    clearance: float = 0.1
    via_diameter: float = 0.4
    via_drill: float = 0.2
    layers: tuple[str, ...] = ("bottom",)  # escape layers for balls that can't leave on top, in order


@dataclass(frozen=True)
class Ball:
    pin: str  # port name, e.g. "pin40"
    x: float
    y: float
    radius: float
    net: str | None  # connectivity group; None = not connected
    towards: Point | None = None  # where the rest of the net is (part frame)
    plane: bool = False  # a plane net ([planes]): only a dogbone via to its plane, no escape out
    net_name: str | None = None  # the plane net's name (via-only balls have no stand-in pad to carry it)


@dataclass
class Escape:
    pin: str
    net: str
    side: str
    end: Point
    end_layer: str
    tracks: list[tuple[str, list[Point]]] = field(default_factory=list)  # (layer, polyline)
    vias: list[Point] = field(default_factory=list)


@dataclass
class Plan:
    ref: str
    rules: Rules
    balls: list[Ball]
    escapes: list[Escape]
    keepout: tuple[float, float, float, float]  # x0, y0, x1, y1 (part frame)
    graphics: list[dict[str, Any]] = field(default_factory=list)  # silkscreen / courtyard of the real footprint

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> Plan:
        return Plan(
            ref=data["ref"],
            rules=Rules(**{**data["rules"], "layers": tuple(data["rules"]["layers"])}),
            balls=[Ball(**{**b, "towards": tuple(b["towards"]) if b.get("towards") else None}) for b in data["balls"]],
            escapes=[
                Escape(
                    pin=e["pin"],
                    net=e["net"],
                    side=e["side"],
                    end=tuple(e["end"]),
                    end_layer=e["end_layer"],
                    tracks=[(lay, [tuple(p) for p in pts]) for lay, pts in e["tracks"]],
                    vias=[tuple(v) for v in e["vias"]],
                )
                for e in data["escapes"]
            ],
            keepout=tuple(data["keepout"]),
            graphics=data.get("graphics", []),
        )


# --- the plan --------------------------------------------------------------------------------


@dataclass
class _Grid:
    pitch: float
    x0: float
    y0: float
    cols: int
    rows: int

    def index(self, b: Ball) -> tuple[int, int]:
        return round((b.x - self.x0) / self.pitch), round((b.y - self.y0) / self.pitch)

    def at(self, i: float, j: float) -> Point:
        return self.x0 + i * self.pitch, self.y0 + j * self.pitch


def _grid(balls: list[Ball]) -> _Grid:
    xs = sorted({round(b.x, 3) for b in balls})
    ys = sorted({round(b.y, 3) for b in balls})
    steps = [b - a for v in (xs, ys) for a, b in itertools.pairwise(v) if b - a > GRID_TOL]
    if not steps:
        msg = "not a ball grid (fewer than two rows / columns)"
        raise EscapeError(msg)
    pitch = min(steps)
    grid = _Grid(pitch, xs[0], ys[0], round((xs[-1] - xs[0]) / pitch) + 1, round((ys[-1] - ys[0]) / pitch) + 1)
    for b in balls:
        i, j = grid.index(b)
        if math.dist(grid.at(i, j), (b.x, b.y)) > GRID_TOL:
            msg = f"{b.pin} at ({b.x:.3f}, {b.y:.3f}) is off the {pitch:.3f} mm grid"
            raise EscapeError(msg)
    return grid


def _depth(grid: _Grid, i: int, j: int, side: str) -> int:
    return {"N": grid.rows - 1 - j, "S": j, "E": grid.cols - 1 - i, "W": i}[side]


def _cost(grid: _Grid, b: Ball, side: str) -> float:
    i, j = grid.index(b)
    align = 0.0
    if b.towards is not None:
        d = (b.towards[0] - b.x, b.towards[1] - b.y)
        n = math.hypot(*d)
        if n > 0:
            align = (d[0] * SIDES[side][0] + d[1] * SIDES[side][1]) / n
    return _depth(grid, i, j, side) - ALIGN_WEIGHT * align


def _local(grid: _Grid, side: str) -> tuple[Any, Any]:
    """(to_grid, from_side) maps between grid indices and (along, depth) of a side."""
    c, r = grid.cols - 1, grid.rows - 1
    to = {
        "N": lambda i, j: (i, r - j),
        "S": lambda i, j: (i, j),
        "E": lambda i, j: (j, c - i),
        "W": lambda i, j: (j, i),
    }[side]
    back = {
        "N": lambda a, d: (a, r - d),
        "S": lambda a, d: (a, d),
        "E": lambda a, d: (c - d, a),
        "W": lambda a, d: (d, a),
    }[side]
    return to, back


def plan(ref: str, balls: list[Ball], rules: Rules | None = None) -> Plan:
    """Escape every connected ball of a ball grid (see the module docstring)."""
    rules = rules or Rules()
    grid = _grid(balls)
    p = grid.pitch
    by_index = {grid.index(b): b for b in balls}
    used = sorted((b for b in balls if b.net is not None), key=lambda b: b.pin)
    side_of = {}
    for b in used:
        costs = {s: _cost(grid, b, s) for s in SIDES}
        side_of[b.pin] = min(costs, key=lambda s: (costs[s], s))
    end_offset = p  # escape ends one pitch beyond the outermost ball line of their side
    taken_cells: set[tuple[float, float]] = set()  # grid cells (half indices) with a via or a top channel
    taken_top: set[tuple[str, float]] = set()  # (side, along) lines used on top
    taken_layer: dict[str, set[tuple[str, float]]] = {lay: set() for lay in rules.layers}
    escapes: list[Escape] = []
    # ring by ring, so the outer balls claim their straight lines first
    signals = [b for b in used if not b.plane]
    order = sorted(signals, key=lambda b: (_depth(grid, *grid.index(b), side_of[b.pin]), b.pin))
    deep: list[Ball] = []
    for b in order:
        side = side_of[b.pin]
        to, back = _local(grid, side)
        along, depth = to(*grid.index(b))
        n = SIDES[side]

        def end_at(a: float, side: str = side, back: Any = back, n: Point = n) -> Point:
            # channel escapes (half positions) end one pitch further out than the ball lines: the
            # ends of a side are then 0.8 mm apart instead of 0.4, room for the router's tracks
            x, y = grid.at(*back(a, 0))
            d = end_offset + (STAGGER_PITCHES * p if a % 1 else 0.0)
            return x + n[0] * d, y + n[1] * d

        start = (b.x, b.y)
        clear = all(by_index.get(back(along, d)) is None for d in range(depth))
        if clear and (side, along) not in taken_top:  # nothing in the way: straight out on top
            taken_top.add((side, along))
            escapes.append(Escape(b.pin, b.net or "", side, end_at(along), "top", [("top", [start, end_at(along)])]))
            continue
        if depth == 1:  # through the channel between two ring-0 balls
            lateral = 0.0
            if b.towards is not None:
                ta, _ = to(*_fractional_index(grid, b.towards))
                lateral = ta - along
            for da in sorted((-0.5, 0.5), key=lambda d: -d * lateral):
                ch = along + da
                cell = back(ch, 0.5)
                if (side, ch) in taken_top or cell in taken_cells or not 0 <= ch <= _span(grid, side):
                    continue
                taken_top.add((side, ch))
                taken_cells.add(cell)
                corner = grid.at(*cell)
                escapes.append(
                    Escape(b.pin, b.net or "", side, end_at(ch), "top", [("top", [start, corner, end_at(ch)])])
                )
                break
            else:
                deep.append(b)
            continue
        deep.append(b)
    # plane balls: a via in a free cell beside the ball, nothing else (the plane is the connection);
    # placed before the deep escapes, which then route around them
    for b in (b for b in used if b.plane):
        i, j = grid.index(b)
        cells = sorted(
            (c for c in ((i + di, j + dj) for di in (-0.5, 0.5) for dj in (-0.5, 0.5)) if c not in taken_cells),
            key=lambda c: not (0 < c[0] < grid.cols - 1 and 0 < c[1] < grid.rows - 1),
        )  # inside the grid first (the escapes run on outside it); clearance decides
        copper = _copper(balls, escapes, rules)
        via_spots = [grid.at(*c) for c in cells]
        # an edge ball hemmed in by its neighbours' channels: a stub straight out of the package instead
        away = b.radius + rules.via_diameter / 2 + rules.clearance
        for side in SIDES:
            if _depth(grid, i, j, side) == 0:
                n = SIDES[side]
                via_spots += [(b.x + n[0] * d, b.y + n[1] * d) for d in (max(away, p * 0.75), p)]
        via = next((q for q in via_spots if _via_fits(q, (b.x, b.y), b.net or "", copper, rules)), None)
        if via is None:
            msg = f"{ref}.{b.pin}: no free via spot next to the plane ball"
            raise EscapeError(msg)
        taken_cells.update(c for c in cells if grid.at(*c) == via)
        escapes.append(Escape(b.pin, b.net or "", "", (b.x, b.y), "", [("top", [(b.x, b.y), via])], [via]))
    for b in deep:  # dogbone to a via, out on an escape layer along a ball line
        side = side_of[b.pin]
        to, back = _local(grid, side)
        along, depth = to(*grid.index(b))
        n = SIDES[side]
        copper = [o for o in _copper(balls, escapes, rules) if o.net != b.net]
        found = None
        # via in a cell beside the ball (the outer two first), then onto a free ball line and out
        spots = [
            (da, dd, line) for dd in (-0.5, 0.5) for da in (-0.5, 0.5) for line in (along + da - 0.5, along + da + 0.5)
        ]
        for da, dd, line in spots:
            if found:
                break
            for layer in rules.layers:
                cell = back(along + da, depth + dd)
                if cell in taken_cells or (side, line) in taken_layer[layer] or not 0 <= line <= _span(grid, side):
                    continue
                via = grid.at(*cell)
                x, y = grid.at(*back(line, 0))
                path = [via, grid.at(*back(line, depth + dd - 0.5)), (x + n[0] * end_offset, y + n[1] * end_offset)]
                if _via_fits(via, (b.x, b.y), b.net or "", copper, rules) and _path_fits(layer, path, copper, rules):
                    found = (cell, line, layer, path)
                    break
        if found is None:
            msg = f"{ref}.{b.pin}: no free via spot / escape line next to the ball"
            raise EscapeError(msg)
        cell, line, layer, path = found
        taken_cells.add(cell)
        taken_layer[layer].add((side, line))
        esc = Escape(
            b.pin, b.net or "", side, path[-1], layer, [("top", [(b.x, b.y), path[0]]), (layer, path)], [path[0]]
        )
        escapes.append(esc)
    xs = [b.x for b in balls]
    ys = [b.y for b in balls]
    reach = end_offset - STANDIN_PAD_MM / 2 - rules.clearance  # the stand-in pads stay just outside
    keepout = (min(xs) - reach, min(ys) - reach, max(xs) + reach, max(ys) + reach)
    escapes.sort(key=lambda e: e.pin)
    return Plan(ref, rules, balls, escapes, keepout)


def _path_fits(layer: str, path: list[Point], others: list[tracks.Obstacle], r: Rules) -> bool:
    """A track along ``path`` on ``layer`` clear of ``others`` (another net's copper)."""
    need = r.clearance + r.track_width / 2 - 1e-6
    return all(o.distance(a, z) >= need for a, z in itertools.pairwise(path) for o in others if layer in o.layers)


def _along(a: Point, b: Point, d: float) -> Point:
    t = d / math.dist(a, b)
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t


def _span(grid: _Grid, side: str) -> int:
    return (grid.cols if side in "NS" else grid.rows) - 1


def _fractional_index(grid: _Grid, q: Point) -> tuple[float, float]:
    return (q[0] - grid.x0) / grid.pitch, (q[1] - grid.y0) / grid.pitch


# --- checks ----------------------------------------------------------------------------------


EVERY_LAYER = frozenset({"top", "bottom", *(f"inner{k}" for k in range(1, 31))})


def _copper(balls: list[Ball], escapes: list[Escape], r: Rules) -> list[tracks.Obstacle]:
    copper = [tracks.Obstacle(b.net or f"<nc {b.pin}>", frozenset({"top"}), ((b.x, b.y),), b.radius) for b in balls]
    for e in escapes:
        copper += [tracks.Obstacle(e.net, EVERY_LAYER, (v,), r.via_diameter / 2) for v in e.vias]
        copper += [
            tracks.Obstacle(e.net, frozenset({lay}), (a, b), r.track_width / 2)
            for lay, pts in e.tracks
            for a, b in itertools.pairwise(pts)
        ]
    return copper


def _via_fits(via: Point, ball: Point, net: str, copper: list[tracks.Obstacle], r: Rules) -> bool:
    """A via (and its top stub from the ball) clear of every other net's copper."""
    others = [o for o in copper if o.net != net]
    return all(o.distance(via, via) >= r.clearance + r.via_diameter / 2 - 1e-6 for o in others) and all(
        o.distance(ball, via) >= r.clearance + r.track_width / 2 - 1e-6 for o in others if "top" in o.layers
    )


def violations(p: Plan) -> list[str]:
    """Clearance problems of the plan: escape copper of one net too close to another net's copper."""
    r = p.rules
    half = r.track_width / 2
    copper = _copper(p.balls, p.escapes, r)
    problems = []
    for e in p.escapes:
        for lay, pts in e.tracks:
            for a, b in itertools.pairwise(pts):
                for o in copper:
                    if o.net != e.net and lay in o.layers and o.distance(a, b) < r.clearance + half - 1e-6:
                        problems.append(f"{p.ref}.{e.pin} ({e.net}) {lay} track near {o.net} at {o.centre}")
        for v in e.vias:
            for o in copper:
                if o.net != e.net and o.distance(v, v) < r.clearance + r.via_diameter / 2 - 1e-6:
                    problems.append(f"{p.ref}.{e.pin} ({e.net}) via at {v} near {o.net} at {o.centre}")
    escaped = {e.pin for e in p.escapes}
    problems += [
        f"{p.ref}.{b.pin} ({b.net}) not escaped" for b in p.balls if b.net is not None and b.pin not in escaped
    ]
    return sorted(set(problems))


# --- from the circuit JSON -------------------------------------------------------------------


def _to_part(cx: float, cy: float, rotation: float) -> Any:
    a = math.radians(-rotation)
    cos, sin = math.cos(a), math.sin(a)
    return lambda x, y: ((x - cx) * cos - (y - cy) * sin, (x - cx) * sin + (y - cy) * cos)


def balls_from_circuit(
    circuit: list[dict[str, Any]], ref: str, plane_nets: frozenset[str] = frozenset()
) -> tuple[list[Ball], list[dict[str, Any]], float]:
    """The balls of part ``ref`` (part frame), its silkscreen / courtyard graphics, its rotation."""
    names = {c["source_component_id"]: c["name"] for c in circuit if c["type"] == "source_component"}
    comp = next(
        (c for c in circuit if c["type"] == "pcb_component" and names.get(c.get("source_component_id")) == ref), None
    )
    if comp is None:
        msg = f"no part {ref} on the board"
        raise EscapeError(msg)
    rotation = float(comp.get("rotation", 0) or 0)
    if round(rotation) % 90:
        msg = f"{ref}: rotation {rotation} is not a multiple of 90 degrees"
        raise EscapeError(msg)
    to_part = _to_part(comp["center"]["x"], comp["center"]["y"], rotation)
    pcb_id = comp["pcb_component_id"]
    source_port = {p["source_port_id"]: p for p in circuit if p["type"] == "source_port"}
    pcb_port = {p["pcb_port_id"]: p for p in circuit if p["type"] == "pcb_port"}
    # connectivity: ports and nets joined by source traces
    parent: dict[str, str] = {}

    def find(k: str) -> str:
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for t in circuit:
        if t["type"] == "source_trace":
            ids = [*t.get("connected_source_port_ids", []), *t.get("connected_source_net_ids", [])]
            for a, b in itertools.pairwise(ids):
                parent[find(a)] = find(b)
    connected = {
        find(k) for t in circuit if t["type"] == "source_trace" for k in t.get("connected_source_port_ids", [])
    }
    net_names: dict[str, set[str]] = {}
    for n in circuit:
        if n["type"] == "source_net" and n["source_net_id"] in parent:
            net_names.setdefault(find(n["source_net_id"]), set()).add(n["name"])
    members: dict[str, list[Point]] = {}
    for pp in pcb_port.values():
        if pp.get("pcb_component_id") != pcb_id and pp.get("source_port_id") in parent:
            members.setdefault(find(pp["source_port_id"]), []).append(to_part(pp["x"], pp["y"]))
    balls = []
    for pad in circuit:
        if pad["type"] != "pcb_smtpad" or pad.get("pcb_component_id") != pcb_id:
            continue
        port = pcb_port.get(pad.get("pcb_port_id", ""))
        sp = source_port.get(port["source_port_id"]) if port else None
        if sp is None:
            continue
        pin = f"pin{sp['pin_number']}" if sp.get("pin_number") is not None else sp["name"]
        group = find(sp["source_port_id"]) if sp["source_port_id"] in parent else None
        net = group if group in connected else None
        planes = sorted(net_names.get(net or "", set()) & plane_nets)
        plane = net is not None and bool(planes)
        others = members.get(group or "", [])
        towards = (sum(q[0] for q in others) / len(others), sum(q[1] for q in others) / len(others)) if others else None
        radius = pad.get("radius") or min(pad.get("width", 0.3), pad.get("height", 0.3)) / 2
        x, y = to_part(pad["x"], pad["y"])
        balls.append(Ball(pin, round(x, 6), round(y, 6), radius, net, towards, plane, planes[0] if planes else None))
    graphics = []
    for g in circuit:
        if g.get("pcb_component_id") != pcb_id:
            continue
        if g["type"] == "pcb_silkscreen_path":
            graphics.append({"kind": "silkscreenpath", "route": [to_part(q["x"], q["y"]) for q in g["route"]]})
        elif g["type"] == "pcb_courtyard_outline":
            graphics.append({"kind": "courtyardoutline", "route": [to_part(q["x"], q["y"]) for q in g["outline"]]})
    return balls, graphics, rotation


def plan_board(
    circuit_path: Path, parts: dict[str, dict[str, Any]], plane_nets: frozenset[str] = frozenset()
) -> list[Plan]:
    """Plans for the parts of the ``[escape.<ref>]`` config sections, from a placement-only circuit JSON."""
    circuit = json.loads(circuit_path.read_text(encoding="utf-8"))
    plans = []
    for ref, cfg in parts.items():
        balls, graphics, _ = balls_from_circuit(circuit, ref, plane_nets)
        defaults = Rules()
        rules = Rules(
            track_width=float(cfg.get("track_width", defaults.track_width)),
            clearance=float(cfg.get("clearance", defaults.clearance)),
            via_diameter=float(cfg.get("via_diameter", defaults.via_diameter)),
            via_drill=float(cfg.get("via_drill", defaults.via_drill)),
            layers=tuple(cfg.get("layers", defaults.layers)),
        )
        p = plan(ref, balls, rules)
        p.graphics = graphics
        plans.append(p)
    return plans


# --- the stand-in for the routed evaluation --------------------------------------------------


def _n(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def standin_tsx(p: Plan) -> str:
    """The stand-in ``<footprint>`` (part frame): a pad per escape end + the real silkscreen.

    No courtyard: tscircuit counts a courtyard over a keepout as a placement error and skips the
    routing; ``fixes.apply_escapes`` draws it in KiCad.
    """
    pads = [_standin_pad(p, e) for e in p.escapes if e.end_layer]  # via-only plane balls: none
    art = [
        f"<{g['kind']} " + ("route" if g["kind"] == "silkscreenpath" else "outline")
        + "={" + json.dumps([{"x": round(x, 4), "y": round(y, 4)} for x, y in g["route"]]) + "} />"
        for g in p.graphics
        if g["kind"] == "silkscreenpath"
    ]  # fmt: skip
    return "<footprint>" + "".join(pads + art) + "</footprint>"


def _standin_pad(p: Plan, e: Escape) -> str:
    """The stand-in pad of an escape end. An end further out than the keepout edge (a staggered
    channel escape) gets a strip from the keepout edge to the end: tscircuit sees the escape's copper
    outside the keepout and keeps clear of it (it may join the strip anywhere; the escape track runs
    along it)."""
    half = STANDIN_PAD_MM / 2
    x, y = e.end
    w = h = STANDIN_PAD_MM
    x0, y0, x1, y1 = p.keepout
    edge = {"N": y1, "S": y0, "E": x1, "W": x0}.get(e.side)
    if edge is not None:
        n = SIDES[e.side]
        inner = edge + (n[0] + n[1]) * p.rules.clearance  # just outside the keepout
        coord = x if n[0] else y
        length = abs(coord - inner) + half
        if length > STANDIN_PAD_MM + 1e-6:
            mid = (coord + half * (n[0] + n[1]) + inner) / 2
            if n[0]:
                x, w = mid, length
            else:
                y, h = mid, length
    layer = "" if e.end_layer == "top" else f' layer="{e.end_layer}"'
    return (
        f'<smtpad portHints={{["{e.pin}"]}} pcbX="{_n(x)}mm" pcbY="{_n(y)}mm" width="{_n(w)}mm" '
        f'height="{_n(h)}mm" shape="rect"{layer} />'
    )


def keepout_tsx(p: Plan, cx: float, cy: float, rotation: float, layers: list[str]) -> str:
    """A ``<keepout>`` over the package (board frame), all copper layers: the escapes live there."""
    x0, y0, x1, y1 = p.keepout
    w, h = (x1 - x0, y1 - y0) if round(rotation) % 180 == 0 else (y1 - y0, x1 - x0)
    a = math.radians(rotation)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    bx, by = cx + mx * math.cos(a) - my * math.sin(a), cy + mx * math.sin(a) + my * math.cos(a)
    return (
        f'<keepout shape="rect" pcbX={{{_n(bx)}}} pcbY={{{_n(by)}}} width="{_n(w)}mm" height="{_n(h)}mm" '
        f"layers={{{json.dumps(layers)}}} />"
    )


def inject(tsx: str, ref: str, footprint: str, keepout: str) -> str:
    """``tsx`` with the part's tag given the stand-in footprint, the keepout right after it."""
    tag = re.compile(r'<[A-Za-z_][\w.]*\b[^<>]*?\bname="' + re.escape(ref) + r'"[^<>]*?/>')
    m = tag.search(tsx)
    if m is None:
        msg = f'no self-closing tag with name="{ref}" in the board source'
        raise EscapeError(msg)
    new = m[0][:-2].rstrip() + f" footprint={{{footprint}}} />" + keepout
    return tsx[: m.start()] + new + tsx[m.end() :]


def prepare_source(source: Path, circuit_path: Path, plans: list[Plan]) -> None:
    """Rewrite the prepared board copy: each planned part gets its stand-in footprint and keepout."""
    circuit = json.loads(circuit_path.read_text(encoding="utf-8"))
    board: dict[str, Any] = next((c for c in circuit if c["type"] == "pcb_board"), {})
    count = int(board.get("num_layers", 2) or 2)
    layers = ["top", *(f"inner{k}" for k in range(1, count - 1)), "bottom"]
    names = {c["source_component_id"]: c["name"] for c in circuit if c["type"] == "source_component"}
    text = source.read_text(encoding="utf-8")
    for p in plans:
        comp = next(c for c in circuit if c["type"] == "pcb_component" and names.get(c["source_component_id"]) == p.ref)
        rotation = float(comp.get("rotation", 0) or 0)
        keepout = keepout_tsx(p, comp["center"]["x"], comp["center"]["y"], rotation, layers)
        text = inject(text, p.ref, standin_tsx(p), keepout)
    source.write_text(text, encoding="utf-8")


def save(plans: list[Plan], path: Path) -> None:
    path.write_text(json.dumps([p.to_json() for p in plans], indent=1), encoding="utf-8")


def load(path: Path) -> list[Plan]:
    if not path.exists():
        return []
    return [Plan.from_json(d) for d in json.loads(path.read_text(encoding="utf-8"))]
