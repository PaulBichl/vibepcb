"""Single-connection router: A* on a fine grid, 45 degree moves, vias, over the routing model.

``route`` connects two points of one net on the allowed layers and returns the geometry; the
caller commits it (``Board.add_track`` / ``add_via``) so later routes see it. Every bend is at most
45 degrees (interior angle >= 135) because a move may turn by one heading step only. Clearances
come from :mod:`vibepcb.drc` (the numbers KiCad uses); the result is verified with the exact
check before it is returned. Failure raises :class:`RouteFailure` with the reason and a blocker.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb import drc, tracks

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibepcb.board import Board

STEPS_MM = (0.1, 0.05)  # fine grid steps (the tube of a coarse path / the unguided search try 0.1 first)
TUBE_STATES = 100_000  # the fine search inside a tube gives up after this many states
COARSE_LEVELS = ((0.4, 0.15), (0.2, 0.05))  # (grid step, extra clearance): a rough path first, 16x / 4x fewer states
TUBE_MM = (1.2, 3.0)  # the fine search stays this close to the coarse path; a wider tube when that fails
LAST_RESORT_STATES = 60_000  # the unguided fine search when no coarse level found a way
GRID_SLACK_MM = 0.03  # grid nodes keep this much extra clearance: pieces between nodes then pass the exact check
SOFT_COST = 4.0  # per grid node through copper that may be ripped up (a crossing costs ~10-20 mm of detour)
HEURISTIC_WEIGHT = 1.3  # >1: fewer states, routes a little longer than optimal
LENGTH_COST = 1.0  # per mm
BEND_COST = 0.5  # per 45 degrees
VIA_COST = 8.0
OFF_LAYER_COST = 0.5  # per mm on a layer other than the preferred one
CORRIDOR_MARGINS_MM = (5.0, 15.0)  # around the endpoints, grown on failure
MAX_STATES = 200_000
HEADINGS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
NO_HEADING = 8
type Node = tuple[int, int, int]  # grid x, y, layer index


class RouteFailure(Exception):  # noqa: N818 - reads as a result, not an error class
    """No route found: ``reason`` says why, ``blocker`` what stood in the way near the best attempt."""

    def __init__(self, reason: str, blocker: drc.Blocker | None = None) -> None:
        super().__init__(reason if blocker is None else f"{reason}: {blocker}")
        self.reason = reason
        self.blocker = blocker


@dataclass
class Route:
    net: str
    width: float
    pieces: list[tuple[str, tracks.Point, tracks.Point]] = field(default_factory=list)  # (layer, a, b)
    vias: list[tracks.Point] = field(default_factory=list)
    via_size: tuple[float, float] = (0.0, 0.0)  # (diameter, drill)
    crossed: set[str] = field(default_factory=set)  # routed nets this route runs through (to rip up)

    @property
    def length(self) -> float:
        return sum(math.dist(a, b) for _, a, b in self.pieces)

    def commit(self, board: Board) -> None:
        for layer, a, b in self.pieces:
            board.add_track(self.net, layer, a, b, self.width)
        for c in self.vias:
            board.add_via(self.net, c, *self.via_size)


@dataclass
class Spec:
    """What the caller wants: how wide, where it may go."""

    layers: list[str]
    width: float
    via: tuple[float, float] | None  # (diameter, drill); None: no vias
    preferred: str | None = None
    corridor: tracks.Box | None = None  # x0, y0, x1, y1
    soft: frozenset[str] = frozenset()  # nets whose routed copper may be crossed (at a cost)
    soft_cost: float = 4.0  # per grid node through soft copper (negotiation raises it round by round)
    history: dict[tuple[str, int, int], float] | None = None  # extra cost of contested spots (see history_key)


class Tube:
    """The points within ``radius`` of a polyline: the area a fine search may use around a coarse path."""

    def __init__(self, pieces: list[tuple[str, tracks.Point, tracks.Point]], radius: float) -> None:
        self.radius = radius
        self.cells: dict[tuple[int, int], list[tracks.Point]] = {}
        for _, a, b in pieces:
            n = max(1, math.ceil(math.dist(a, b) / (radius / 2)))
            for k in range(n + 1):
                p = (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n)
                self.cells.setdefault((math.floor(p[0] / radius), math.floor(p[1] / radius)), []).append(p)

    def near(self, p: tracks.Point) -> bool:
        cx, cy = math.floor(p[0] / self.radius), math.floor(p[1] / self.radius)
        return any(
            math.dist(p, q) <= self.radius
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for q in self.cells.get((cx + dx, cy + dy), ())
        )


def history_key(layer: str, p: tracks.Point) -> tuple[str, int, int]:
    """Key of the 0.1 mm cell of ``p`` in a negotiation history."""
    return layer, round(p[0] * 10), round(p[1] * 10)


def _octile(dx: int, dy: int, step: float) -> float:
    dx, dy = abs(dx), abs(dy)
    return HEURISTIC_WEIGHT * step * (max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy))


def route(
    board: Board,
    net: str,
    start: tracks.Point,
    goal: tracks.Point,
    spec: Spec,
    *,
    start_layers: list[str] | None = None,
    goal_layers: list[str] | None = None,
    waypoints: list[tracks.Point] | None = None,
) -> Route:
    """Route ``net`` from ``start`` to ``goal`` (pad centres or any points) through the waypoints."""
    for layer in spec.layers:
        if layer not in board.layers:
            msg = f"layer {layer} is not a copper layer of the board"
            raise RouteFailure(msg)
    points = [start, *(waypoints or []), goal]
    result = Route(net, spec.width, via_size=spec.via or (0.0, 0.0))
    heading = NO_HEADING
    layers_in = start_layers or spec.layers
    for i, (a, b) in enumerate(itertools.pairwise(points)):
        last = i == len(points) - 2
        leg, heading, layer = _leg(board, net, a, b, spec, layers_in, goal_layers if last else None, heading)
        result.pieces += leg.pieces
        result.vias += leg.vias
        layers_in = [layer]
    _verify(board, result, spec.soft)
    return result


def _leg(
    board: Board,
    net: str,
    start: tracks.Point,
    goal: tracks.Point,
    spec: Spec,
    start_layers: list[str],
    goal_layers: list[str] | None,
    heading: int,
) -> tuple[Route, int, str]:
    last_failure: RouteFailure | None = None
    stages: list[str] = []

    def area(margin: float) -> tracks.Box:
        return spec.corridor or (
            min(start[0], goal[0]) - margin,
            min(start[1], goal[1]) - margin,
            max(start[0], goal[0]) + margin,
            max(start[1], goal[1]) + margin,
        )

    ends = goal_layers or spec.layers
    found_rough = False
    for margin in CORRIDOR_MARGINS_MM:
        box = area(margin)
        for coarse_step, slack in COARSE_LEVELS:  # a rough path (or the proof there is none) is cheap
            try:
                rough, _, _ = _search(
                    board, net, start, goal, spec, start_layers, ends, heading, box, coarse_step, slack, None
                )
            except RouteFailure as e:
                last_failure = e
                stages.append(f"coarse {coarse_step}: {e.reason[:60]}")
                continue
            found_rough = True
            for radius in TUBE_MM:  # the fine search stays in a tube around the rough path
                try:
                    return _search(
                        board,
                        net,
                        start,
                        goal,
                        spec,
                        start_layers,
                        ends,
                        heading,
                        box,
                        STEPS_MM[0],
                        GRID_SLACK_MM,
                        Tube(rough.pieces, radius),
                        max_states=TUBE_STATES,
                    )
                except RouteFailure as e:
                    last_failure = e
                    stages.append(f"fine {STEPS_MM[0]} in {radius} mm tube: {e.reason[:50]}")
        if spec.corridor is not None:
            break
    if not found_rough:  # no coarse level found a way (a tight spot between nodes): fine search, on a short leash
        try:
            return _search(
                board,
                net,
                start,
                goal,
                spec,
                start_layers,
                ends,
                heading,
                area(CORRIDOR_MARGINS_MM[0]),
                STEPS_MM[0],
                GRID_SLACK_MM,
                None,
                max_states=LAST_RESORT_STATES,
            )
        except RouteFailure as e:
            last_failure = e
            stages.append(f"unguided {STEPS_MM[0]}: {e.reason[:60]}")
    assert last_failure is not None
    raise RouteFailure("; ".join(stages), last_failure.blocker)


def _search(
    board: Board,
    net: str,
    start: tracks.Point,
    goal: tracks.Point,
    spec: Spec,
    start_layers: list[str],
    goal_layers: list[str],
    heading0: int,
    box: tracks.Box,
    step_mm: float,
    slack: float,
    tube: Tube | None,
    max_states: int | None = None,
) -> tuple[Route, int, str]:
    layers = spec.layers
    x0, y0, x1, y1 = box
    snap = 1.5 * step_mm
    blocked: dict[tuple[int, int, int], int] = {}

    def pos(ix: int, iy: int) -> tracks.Point:
        return start[0] + ix * step_mm, start[1] + iy * step_mm

    soft = spec.soft

    def cell(li: int, ix: int, iy: int) -> int:
        """0 free, 1 blocked, 2 free once the soft nets' routed copper is ripped up."""
        key = (li, ix, iy)
        if key not in blocked:
            p = pos(ix, iy)
            w = spec.width + 2 * slack
            outside = not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1) or (tube is not None and not tube.near(p))
            if outside or drc.blocking_track(board, net, layers[li], p, p, w, soft):
                blocked[key] = 1
            else:
                blocked[key] = 2 if soft and drc.blocking_track(board, net, layers[li], p, p, w) else 0
        return blocked[key]

    def via_cost(ix: int, iy: int) -> float | None:
        """Extra cost of a via here, ``None`` when it can't go."""
        assert spec.via is not None
        c = pos(ix, iy)
        if any(o.distance(c, c) < spec.via[0] / 2 for o in board.copper.pads_by_net.get(net, [])):
            return None  # no via in a pad of its own net
        if drc.blocking_via(board, net, c, *spec.via, soft) is not None:
            return None
        return spec.soft_cost if soft and drc.blocking_via(board, net, c, *spec.via) is not None else 0.0

    gx, gy = round((goal[0] - start[0]) / step_mm), round((goal[1] - start[1]) / step_mm)
    goal_li = {layers.index(layer) for layer in goal_layers if layer in layers}
    pref = layers.index(spec.preferred) if spec.preferred in layers else -1
    counter = itertools.count()
    # a state is a grid node (x, y, layer); the heading it was reached with (for the bend rule and cost)
    # is kept beside it, not in it: 8x fewer states than (x, y, layer, heading)
    heap: list[tuple[float, int, float, Node]] = []
    g: dict[Node, float] = {}
    parent: dict[Node, Node | None] = {}
    heading: dict[Node, int] = {}
    for layer in start_layers:
        if layer in layers:
            s = (0, 0, layers.index(layer))
            g[s] = 0.0
            parent[s] = None
            heading[s] = heading0
            heapq.heappush(heap, (_octile(gx, gy, step_mm), next(counter), 0.0, s))
    end: Node | None = None
    states = 0
    best: tuple[float, tracks.Point] = (math.inf, start)
    while heap:
        _, _, cost, cur = heapq.heappop(heap)
        ix, iy, li = cur
        h = heading[cur]
        if cost > g[cur]:
            continue  # stale heap entry
        d_goal = math.dist(pos(ix, iy), goal)
        if d_goal < best[0]:
            best = (d_goal, pos(ix, iy))
        if (
            li in goal_li
            and d_goal <= snap
            and _closing_ok(board, net, layers[li], pos(ix, iy), goal, spec.width, soft)
        ):
            end = cur
            break
        states += 1
        if states > (max_states or MAX_STATES):
            msg = f"search limit ({max_states or MAX_STATES} states) reached"
            raise RouteFailure(msg)
        for nh in range(8) if h == NO_HEADING else ((h - 1) % 8, h, (h + 1) % 8):
            dx, dy = HEADINGS[nh]
            nx, ny = ix + dx, iy + dy
            state = cell(li, nx, ny)
            if state == 1:
                continue
            length = step_mm * (math.sqrt(2) if dx and dy else 1.0)
            step = length * (LENGTH_COST + (OFF_LAYER_COST if pref >= 0 and li != pref else 0.0))
            if h != NO_HEADING and nh != h:
                step += BEND_COST
            if state == 2:
                step += spec.soft_cost
            if spec.history:
                step += spec.history.get(history_key(layers[li], pos(nx, ny)), 0.0)
            ns = (nx, ny, li)
            if cost + step < g.get(ns, math.inf):
                g[ns] = cost + step
                parent[ns] = cur
                heading[ns] = nh
                heapq.heappush(heap, (cost + step + _octile(gx - nx, gy - ny, step_mm), next(counter), cost + step, ns))
        extra = via_cost(ix, iy) if spec.via is not None else None
        if extra is not None:
            for lj in range(len(layers)):
                if lj == li or cell(lj, ix, iy) == 1:
                    continue
                add = VIA_COST + extra + (spec.soft_cost if cell(lj, ix, iy) == 2 else 0.0)
                ns = (ix, iy, lj)
                if cost + add < g.get(ns, math.inf):
                    g[ns] = cost + add
                    parent[ns] = cur
                    heading[ns] = NO_HEADING
                    heapq.heappush(
                        heap, (cost + add + _octile(gx - ix, gy - iy, step_mm), next(counter), cost + add, ns)
                    )
    if end is None:
        blocker = drc.blocking_track(board, net, layers[0], best[1], goal, spec.width)
        msg = f"no path (got within {best[0]:.2f} mm of the target)"
        raise RouteFailure(msg, blocker)
    return _rebuild(net, spec, layers, parent, end, heading[end], pos, goal)


def _closing_ok(
    board: Board, net: str, layer: str, p: tracks.Point, goal: tracks.Point, width: float, soft: frozenset[str]
) -> bool:
    return p == goal or drc.blocking_track(board, net, layer, p, goal, width, soft) is None


def _rebuild(
    net: str,
    spec: Spec,
    layers: list[str],
    parent: dict[Node, Node | None],
    end: Node,
    end_heading: int,
    pos: Callable[[int, int], tracks.Point],
    goal: tracks.Point,
) -> tuple[Route, int, str]:
    chain: list[Node] = []
    cur: Node | None = end
    while cur is not None:
        chain.append(cur)
        cur = parent[cur]
    chain.reverse()
    result = Route(net, spec.width, via_size=spec.via or (0.0, 0.0))
    run: list[tracks.Point] = []
    run_layer = layers[chain[0][2]]
    for s in chain:
        p = pos(s[0], s[1])
        layer = layers[s[2]]
        if layer != run_layer:
            result.pieces += _pieces(run_layer, run)
            result.vias.append(p)
            run, run_layer = [p], layer
        elif not run or run[-1] != p:
            run.append(p)
    if run[-1] != goal:
        run.append(goal)
    result.pieces += _pieces(run_layer, run)
    return result, end_heading, run_layer


def _pieces(layer: str, points: list[tracks.Point]) -> list[tuple[str, tracks.Point, tracks.Point]]:
    points = tracks.simplify(points)
    return [(layer, a, b) for a, b in itertools.pairwise(points) if a != b]


def _verify(board: Board, r: Route, soft: frozenset[str] = frozenset()) -> None:
    """The exact check of what the grid search produced (it only looked at grid nodes).

    Routed copper of the ``soft`` nets may be in the way: those nets are listed in ``r.crossed``.
    """
    for layer, a, b in r.pieces:
        if (blocker := drc.blocking_track(board, r.net, layer, a, b, r.width, soft)) is not None:
            msg = "route failed the exact clearance check"
            raise RouteFailure(msg, blocker)
        r.crossed |= {x.net for x in drc.scan_track(board, r.net, layer, a, b, r.width) if x.net in soft}
    for c in r.vias:
        if (blocker := drc.blocking_via(board, r.net, c, *r.via_size, soft)) is not None:
            msg = "via failed the exact clearance check"
            raise RouteFailure(msg, blocker)
        r.crossed |= {x.net for x in drc.scan_via(board, r.net, c, *r.via_size) if x.net in soft}
