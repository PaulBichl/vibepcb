"""Routing intent (``boards/<board>/routes.toml``) turned into tracks and vias on the KiCad board.

The agent says what to connect and where it may go; :mod:`vibepcb.router` finds the geometry,
:mod:`vibepcb.drc` keeps the clearances. Entries run in file order (critical first), each one sees
the copper of the earlier ones. Format::

    [defaults]
    layers = ["F.Cu", "B.Cu"]        # signal layers the router may use (default: all copper layers)

    [[route]]
    from = "U1.F2"                   # pad "REF.PAD" (KiCad pad number) ...
    to = "U2.5"                      #   ... to pad
    layers = ["F.Cu"]                # optional: restrict
    via_max = 0                      # optional: most vias allowed (0: none)
    waypoints = [[112.0, 98.5]]      # optional: board mm, passed in order
    corridor = [108, 95, 125, 104]   # optional: x0 y0 x1 y1, stay inside
    width = 0.15                     # optional: else the net class width

    [[route]]
    net = "SD_*"                     # every net matching (not a [planes] net unless named exactly), all pads
    order = "angle"                  # a bus: outermost arcs first, the inner ones nest (default); "length": shortest
    method = "sequential"            #   first; "name". method: "sequential" (default, in that order) | "negotiate"
    parts = ["J1"]                   # optional: only the connections that touch one of these parts (a bus per header)
"""

from __future__ import annotations

import copy
import fnmatch
import math
import multiprocessing
import os
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb import board as board_mod
from vibepcb import drc, fixes, router, sexpr, tracks

if TYPE_CHECKING:
    from pathlib import Path

    from vibepcb.fixes import KicadProject

ROUTES_NAME = "routes.toml"
ENTRY_KEYS = {"net", "from", "to", "layers", "via_max", "waypoints", "corridor", "width", "order", "method", "parts"}
SECTIONS = {"defaults", "route"}
PLANNED = {"bus", "pair", "tune"}


class RoutesError(ValueError):
    """routes.toml is malformed or names something that is not on the board."""


@dataclass
class Result:
    """What one connection came to."""

    entry: str
    ok: bool
    detail: str  # length / vias on success, the reason on failure
    length: float = 0.0
    vias: int = 0


@dataclass
class Pad:
    ref: str
    number: str
    net: str
    centre: tracks.Point
    layers: frozenset[str]
    exit: tracks.Point | None = None  # where the pad's own track or via leaves it (outside the part), None: none
    ring: tuple[tracks.Point, ...] = ()  # the spots just outside the pad on all four sides (a plane pad's via)

    @property
    def label(self) -> str:
        return f"{self.ref}.{self.number}"


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if not r.ok]


def load(path: Path) -> dict[str, Any]:
    """The parsed, validated routes file (empty without a file)."""
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    validate(data, path.name)
    return data


def validate(data: dict[str, Any], where: str = ROUTES_NAME) -> None:
    if planned := set(data) & PLANNED:
        msg = f"{where}: [[{sorted(planned)[0]}]] is not implemented yet"
        raise RoutesError(msg)
    if unknown := set(data) - SECTIONS:
        msg = f"{where}: unknown section(s) {sorted(unknown)} (known: {sorted(SECTIONS)})"
        raise RoutesError(msg)
    for i, entry in enumerate(data.get("route", []), 1):
        label = f"{where}: [[route]] #{i}"
        if bad := set(entry) - ENTRY_KEYS:
            msg = f"{label}: unknown key(s) {sorted(bad)} (known: {sorted(ENTRY_KEYS)})"
            raise RoutesError(msg)
        has_pads = "from" in entry or "to" in entry
        if has_pads == ("net" in entry and "from" not in entry and "to" not in entry):
            msg = f'{label}: give either net = "..." or from = "REF.PAD" and to = "REF.PAD"'
            raise RoutesError(msg)
        if has_pads and not ("from" in entry and "to" in entry):
            msg = f"{label}: from and to go together"
            raise RoutesError(msg)
        if "corridor" in entry and len(entry["corridor"]) != 4:
            msg = f"{label}: corridor needs 4 numbers (x0 y0 x1 y1)"
            raise RoutesError(msg)


def board_pads(project: KicadProject) -> list[Pad]:
    """Every copper pad of the board, in the order ``fixes._copper`` lists them.

    A ball with a VibePCB escape (``[escape.<ref>]``) is the escape's end: its position and layer,
    where the escape track stops and the router takes over.
    """
    names = fixes._net_names(project.pcb)
    ends = {
        (plan.ref, esc.pin.removeprefix("pin")): esc for plan in fixes._escape_plans(project) for esc in plan.escapes
    }
    out = []
    for fp in sexpr.children(project.pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference") or "?"
        to_board = fixes._fp_to_board(fp)
        for pad in sexpr.children(fp, "pad"):
            layers = fixes._copper_layers(pad) if str(pad[2]) != "np_thru_hole" else frozenset(fixes.COPPER_LAYERS)
            if not layers:
                continue
            at = sexpr.child(pad, "at")
            centre = to_board(fixes._num(at, 1), fixes._num(at, 2))
            exit_at = _exit_point(fp, pad, centre, to_board)
            ring = _ring_points(pad, centre, to_board)
            if (esc := ends.get((ref, str(pad[1])))) is not None and esc.end_layer:
                centre, layers = to_board(esc.end[0], -esc.end[1]), frozenset({fixes._kicad_layer(esc.end_layer)})
                exit_at, ring = None, ()  # the escape track leaves the ball already
            out.append(Pad(ref, str(pad[1]), fixes._net_of(pad, names), centre, layers, exit_at, ring))
    return out


RESERVED_KEY = "_reserved"  # internal: the pad exit reservations travel with the defaults
EXIT_GAP_MM = 0.45  # a pad's exit spot: this far beyond its outer edge (room for a via beside a 0.5 mm pitch pin)
EXIT_RADIUS_MM = 0.45


def _exit_point(
    fp: list[sexpr.Node], pad: list[sexpr.Node], centre: tracks.Point, to_board: Any
) -> tracks.Point | None:
    """The spot just outside a pad, away from its part's centre along the pad's main axis."""
    at = sexpr.child(fp, "at")
    fx, fy = fixes._num(at, 1), fixes._num(at, 2)
    dx, dy = centre[0] - fx, centre[1] - fy
    if math.hypot(dx, dy) < 1e-6:
        return None
    ux, uy = (math.copysign(1.0, dx), 0.0) if abs(dx) >= abs(dy) else (0.0, math.copysign(1.0, dy))
    corners = fixes._pad_corners(pad, to_board)
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    half = (abs(ux) * (max(xs) - min(xs)) + abs(uy) * (max(ys) - min(ys))) / 2
    return centre[0] + ux * (half + EXIT_GAP_MM), centre[1] + uy * (half + EXIT_GAP_MM)


def _ring_points(pad: list[sexpr.Node], centre: tracks.Point, to_board: Any) -> tuple[tracks.Point, ...]:
    corners = fixes._pad_corners(pad, to_board)
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    hx, hy = (max(xs) - min(xs)) / 2 + EXIT_GAP_MM, (max(ys) - min(ys)) / 2 + EXIT_GAP_MM
    return (
        (centre[0] + hx, centre[1]),
        (centre[0] - hx, centre[1]),
        (centre[0], centre[1] + hy),
        (centre[0], centre[1] - hy),
    )


def _reservations(
    pads: list[Pad], layers: list[str], nets: set[str], planes: set[str]
) -> dict[tuple[str, int, int], str]:
    """Cells in front of every pad of ``nets`` (its exit), owned by the pad's net: the router charges other nets.

    A plane net's pad needs room for a via on whichever side has it (a row of decoupling caps leaves no room
    along their axis), so it reserves all four sides.
    """
    out: dict[tuple[str, int, int], str] = {}
    steps = round(EXIT_RADIUS_MM * 10)
    for p in pads:
        if p.net not in nets:
            continue
        spots = p.ring if p.net in planes else ((p.exit,) if p.exit is not None else ())
        for spot in spots:
            cx, cy = round(spot[0] * 10), round(spot[1] * 10)
            for i in range(-steps, steps + 1):
                for j in range(-steps, steps + 1):
                    if i * i + j * j <= steps * steps:
                        for layer in layers:
                            out.setdefault((layer, cx + i, cy + j), p.net)
    return out


def apply(project: KicadProject, data: dict[str, Any], *, only: str | None = None) -> Report:
    """Route every entry onto ``project.pcb``; report per connection. Failed entries leave no copper."""
    report = Report()
    board = board_mod.Board.from_project(project)
    pads = board_pads(project)
    by_label = {p.label: p for p in pads}
    planes = set(project.config.get("planes", {}).get("nets", []))
    defaults = dict(data.get("defaults", {}))
    counts: dict[str, int] = {}
    for p in pads:
        counts[p.net] = counts.get(p.net, 0) + 1
    connected = {n for n, k in counts.items() if n and (k > 1 or n in planes)}
    # every pad keeps the spot in front of it free for its own exit (other nets pay to pass): a routed pad
    # for its track, a plane net's pad for the via into its plane
    defaults[RESERVED_KEY] = _reservations(pads, list(defaults.get("layers", board.layers)), connected, planes)
    new_nodes: list[list[sexpr.Node]] = []
    for i, entry in enumerate(data.get("route", []), 1):
        parts = set(entry.get("parts", []))
        conns = [
            c
            for c in _connections(entry, i, board, pads, by_label, planes)
            if (not only or fnmatch.fnmatchcase(c[0], only)) and (not parts or {c[1].ref, c[2].ref} & parts)
        ]
        order = entry.get("order", "angle")
        if order == "angle":
            conns = _by_angle(conns)
        elif order == "outside-in":
            conns = _outside_in(conns)
        elif order == "length":
            conns = sorted(conns, key=lambda c: (math.dist(c[1].centre, c[2].centre), c[3]))
        run = _run_entry if entry.get("method", "sequential") == "negotiate" else _run_sequential
        results, nodes = run(board, project.pcb, entry, defaults, conns)
        report.results += results
        new_nodes += nodes
    project.pcb.extend(new_nodes)
    return report


def _outside_in(conns: list[tuple[str, Pad, Pad, str]]) -> list[tuple[str, Pad, Pad, str]]:
    """Order the connections of a bus so the outermost lanes go first and the inner ones nest inside.

    The bus axis runs from the centroid of the start pads to the centroid of the goal pads; a connection's
    lane is the sideways offset of its midpoint. Few connections (a pair, a net) keep their order.
    """
    if len(conns) < 4:
        return conns
    n = len(conns)
    sx = sum(c[1].centre[0] for c in conns) / n, sum(c[1].centre[1] for c in conns) / n
    gx = sum(c[2].centre[0] for c in conns) / n, sum(c[2].centre[1] for c in conns) / n
    ax, ay = gx[0] - sx[0], gx[1] - sx[1]
    length = math.hypot(ax, ay) or 1.0
    mid = ((sx[0] + gx[0]) / 2, (sx[1] + gx[1]) / 2)

    def lane(c: tuple[str, Pad, Pad, str]) -> float:
        mx, my = (c[1].centre[0] + c[2].centre[0]) / 2 - mid[0], (c[1].centre[1] + c[2].centre[1]) / 2 - mid[1]
        return (ax * my - ay * mx) / length

    return sorted(conns, key=lambda c: (-abs(lane(c)), c[3]))


def _by_angle(conns: list[tuple[str, Pad, Pad, str]]) -> list[tuple[str, Pad, Pad, str]]:
    """Order the connections of a bus by how far their start pad sits off the bus axis, seen from the goals.

    Two groups of pins facing each other are paired without crossings when the outermost arcs are drawn
    first and the inner ones nest inside (a planar order). The axis runs from the centre of the goal pads
    to the centre of the start pads; the sideways angle of a start pad decides.
    """
    if len(conns) < 4:
        return conns
    n = len(conns)
    gx = sum(c[2].centre[0] for c in conns) / n, sum(c[2].centre[1] for c in conns) / n
    sx = sum(c[1].centre[0] for c in conns) / n, sum(c[1].centre[1] for c in conns) / n
    ux, uy = sx[0] - gx[0], sx[1] - gx[1]
    norm = math.hypot(ux, uy) or 1.0
    ux, uy = ux / norm, uy / norm

    def off_axis(c: tuple[str, Pad, Pad, str]) -> float:
        vx, vy = c[1].centre[0] - gx[0], c[1].centre[1] - gx[1]
        return abs(ux * vy - uy * vx) / (math.hypot(vx, vy) or 1.0)

    return sorted(conns, key=lambda c: (-off_axis(c), c[3]))


RIP_UP_ROUNDS = 4  # passes over the connections that failed, each ripping up what blocks them


def _run_sequential(
    board: board_mod.Board,
    pcb: list[sexpr.Node],
    entry: dict[str, Any],
    defaults: dict[str, Any],
    conns: list[tuple[str, Pad, Pad, str]],
) -> tuple[list[Result], list[list[sexpr.Node]]]:
    """Route the connections one after the other in the given order; each sees the earlier ones as obstacles.

    A connection that finds no way is tried again once everything is routed, this time allowed to cross the
    entry's routed copper at a cost: the routes it crosses are ripped up, it is committed, and they are routed
    again behind it. That is how a late connection gets into a pad field the earlier ones closed off.
    """
    started = time.monotonic()
    start = board.checkpoint()
    routed: dict[str, router.Route] = {}
    why: dict[str, str] = {}
    failed: list[tuple[str, Pad, Pad, str]] = []
    for i, conn in enumerate(conns, 1):
        if i % 10 == 0 or i == len(conns):
            _log(f"  {entry.get('net', '?')}: {i}/{len(conns)} connections ({time.monotonic() - started:.0f} s)")
        net, a, b, name = conn
        try:
            r = _route_one(board, entry, defaults, net, a, b)
        except router.RouteFailure as e:
            why[name] = str(e)
            failed.append(conn)
            continue
        r.commit(board)
        routed[name] = r
    for round_no in range(RIP_UP_ROUNDS):
        if not failed:
            break
        _log(
            f"  {entry.get('net', '?')}: rip-up round {round_no + 1}, {len(failed)} to place ({time.monotonic() - started:.0f} s)"
        )
        still: list[tuple[str, Pad, Pad, str]] = []
        queue = list(failed)
        while queue:
            conn = queue.pop(0)
            net, a, b, name = conn
            soft = frozenset(r.net for r in routed.values())
            try:
                r = _route_one(board, entry, defaults, net, a, b, soft, SOFT_COST_START)
            except router.RouteFailure as e:
                why[name] = str(e)
                still.append(conn)
                continue
            victims = [n for n, other in routed.items() if other.net in r.crossed]
            for n in victims:
                del routed[n]
            _rebuild(board, start, routed)
            r.commit(board)
            routed[name] = r
            why.pop(name, None)
            retry = [c for c in conns if c[3] in victims]
            for c in retry:  # the ripped-up connections go again, plain
                try:
                    r2 = _route_one(board, entry, defaults, c[0], c[1], c[2])
                except router.RouteFailure as e:
                    why[c[3]] = str(e)
                    still.append(c)
                    continue
                r2.commit(board)
                routed[c[3]] = r2
        failed = still
    results = [
        Result(
            c[3],
            True,
            f"{routed[c[3]].length:.2f} mm, {len(routed[c[3]].vias)} via(s)",
            routed[c[3]].length,
            len(routed[c[3]].vias),
        )
        if c[3] in routed
        else Result(c[3], False, why.get(c[3], "not routed"))
        for c in conns
    ]
    nodes: list[list[sexpr.Node]] = []
    for c in conns:
        if c[3] in routed:
            nodes += _nodes(pcb, routed[c[3]])
    return results, nodes


NEGOTIATION_ROUNDS = 40
SOFT_COST_START = 3.0  # cost of crossing another route of the entry; grows each round
SOFT_COST_GROWTH = 1.12
HISTORY_STEP = 3.0  # added to the cells where routes conflicted, so the next round avoids them


def _run_entry(
    board: board_mod.Board,
    pcb: list[sexpr.Node],
    entry: dict[str, Any],
    defaults: dict[str, Any],
    conns: list[tuple[str, Pad, Pad, str]],
) -> tuple[list[Result], list[list[sexpr.Node]]]:
    """Route an entry's connections by negotiation (PathFinder).

    Every connection may run through the others' copper at a cost; where routes conflict afterwards,
    the contested spots get a history cost, the crossing gets dearer, and the conflicting connections
    are routed again. The first connection no longer wins the channel just by being first. Whatever
    still conflicts after the last round is dropped (reported as failed), worst offender first.
    """
    started = time.monotonic()
    start = board.checkpoint()
    routed: dict[str, router.Route] = {}
    failures: dict[str, str] = {}
    history: dict[tuple[str, int, int], float] = {}
    nets = frozenset(c[0] for c in conns)
    todo = list(conns)
    cost = SOFT_COST_START
    jobs = _jobs(defaults)
    for round_no in range(NEGOTIATION_ROUNDS):
        _rebuild(board, start, routed)  # every connection of the round sees the same board: independent searches
        outcome = _search_round(board, entry, defaults, conns, todo, nets, cost, history, jobs)
        for conn, r, why in outcome:
            name = conn[3]
            routed.pop(name, None)
            if r is None:
                failures[name] = why
                continue
            failures.pop(name, None)
            routed[name] = r
        _rebuild(board, start, routed)
        graph = _conflicts(board, routed)
        conflicts = {n: len(adj) for n, adj in graph.items()}
        _log(
            f"  negotiation round {round_no + 1}: {len(routed)} routed, {len(failures)} failed, {len(conflicts)} in conflict ({time.monotonic() - started:.0f} s)"
        )
        for name, why in list(failures.items())[:4]:
            _log(f"    failed {name}: {why[:400]}")
        if not conflicts:
            break
        for name in conflicts:
            for layer, p0, p1 in routed[name].pieces:
                n = max(1, round(math.dist(p0, p1) / 0.1))
                for k in range(n + 1):
                    p = (p0[0] + (p1[0] - p0[0]) * k / n, p0[1] + (p1[1] - p0[1]) * k / n)
                    key = router.history_key(layer, p)
                    history[key] = history.get(key, 0.0) + HISTORY_STEP
        cost *= SOFT_COST_GROWTH
        # only an independent set of the conflicting connections moves (in parallel); the ones they conflict
        # with hold still, so a pair does not swap channels forever (that is what routing them in sequence gives)
        movers: set[str] = set()
        for name in sorted(conflicts, key=lambda n: (-conflicts[n], n)):
            if not graph[name] & movers:
                movers.add(name)
        todo = [c for c in conns if c[3] in movers or c[3] in failures]
    keep = bool(os.environ.get("VIBEPCB_KEEP_CONFLICTS"))  # debugging: show the conflicting routes
    while not keep and (
        conflicts := {n: len(adj) for n, adj in _conflicts(board, routed).items()}
    ):  # out of rounds: drop the worst
        worst = max(conflicts, key=lambda n: (conflicts[n], n))
        routed.pop(worst)
        failures[worst] = "conflicts with other routes of the entry after negotiation"
        _rebuild(board, start, routed)
    results = [
        Result(
            c[3],
            True,
            f"{routed[c[3]].length:.2f} mm, {len(routed[c[3]].vias)} via(s)",
            routed[c[3]].length,
            len(routed[c[3]].vias),
        )
        if c[3] in routed
        else Result(c[3], False, failures.get(c[3], "not routed"))
        for c in conns
    ]
    nodes: list[list[sexpr.Node]] = []
    for r in routed.values():
        nodes += _nodes(pcb, r)
    return results, nodes


def _log(text: str) -> None:
    """Progress of a long negotiation on stderr (``VIBEPCB_VERBOSE=1``)."""
    if os.environ.get("VIBEPCB_VERBOSE"):
        print(text, file=sys.stderr, flush=True)


_WORK: tuple[Any, ...] = ()  # what the worker processes of a round inherit by fork


def _jobs(defaults: dict[str, Any]) -> int:
    """Worker processes: ``[defaults] jobs`` or $VIBEPCB_JOBS, else the cores minus 4 (an editor stays usable), at most 12."""
    wanted = os.environ.get("VIBEPCB_JOBS") or defaults.get("jobs")
    if wanted:
        return max(1, int(wanted))
    return max(1, min(12, (os.cpu_count() or 4) - 4))


def _route_task(i: int) -> tuple[int, router.Route | None, str]:
    board, entry, defaults, todo, nets, cost, history = _WORK
    net, a, b, _ = todo[i]
    try:
        return i, _route_one(board, entry, defaults, net, a, b, nets, cost, history), ""
    except router.RouteFailure as e:
        return i, None, str(e)


def _search_round(
    board: board_mod.Board,
    entry: dict[str, Any],
    defaults: dict[str, Any],
    conns: list[tuple[str, Pad, Pad, str]],
    todo: list[tuple[str, Pad, Pad, str]],
    nets: frozenset[str],
    cost: float,
    history: dict[tuple[str, int, int], float],
    jobs: int,
) -> list[tuple[tuple[str, Pad, Pad, str], router.Route | None, str]]:
    """Route the ``todo`` connections against the board as it is, in parallel (fork: the board is shared copy-on-write)."""
    global _WORK  # noqa: PLW0603 - handed to the forked workers
    _WORK = (board, entry, defaults, todo, nets, cost, history)
    try:
        if jobs <= 1 or len(todo) < 3:
            results = [_route_task(i) for i in range(len(todo))]
        else:
            with multiprocessing.get_context("fork").Pool(min(jobs, len(todo))) as pool:
                results = pool.map(_route_task, range(len(todo)), chunksize=1)
    finally:
        _WORK = ()
    return [(todo[i], r, why) for i, r, why in sorted(results, key=lambda t: t[0])]


def _rebuild(board: board_mod.Board, start: tuple[int, int, int, int], routed: dict[str, router.Route]) -> None:
    """Board = what it was at ``start`` plus these routes."""
    board.rollback(start)
    for r in routed.values():
        r.commit(board)


def _conflicts(board: board_mod.Board, routed: dict[str, router.Route]) -> dict[str, set[str]]:
    """Conflict graph: connection name -> the connections whose routed copper it violates clearance to."""
    by_net: dict[str, list[str]] = {}
    for name, r in routed.items():
        by_net.setdefault(r.net, []).append(name)
    graph: dict[str, set[str]] = {}
    for name, r in routed.items():
        nets = {b.net for layer, a, c in r.pieces for b in drc.scan_track(board, r.net, layer, a, c, r.width)}
        nets |= {b.net for v in r.vias for b in drc.scan_via(board, r.net, v, *r.via_size)}
        others = {other for n in nets for other in by_net.get(n, ())}
        if others:
            graph.setdefault(name, set()).update(others)
            for other in others:
                graph.setdefault(other, set()).add(name)
    return graph


def _connections(
    entry: dict[str, Any],
    index: int,
    board: board_mod.Board,
    pads: list[Pad],
    by_label: dict[str, Pad],
    planes: set[str],
) -> list[tuple[str, Pad, Pad, str]]:
    """(net, pad a, pad b, label) of an entry: one pair, or a net's pads chained nearest-first."""
    label = f"{ROUTES_NAME}: [[route]] #{index}"
    if "from" in entry:
        ends = []
        for key in ("from", "to"):
            if entry[key] not in by_label:
                msg = f"{label}: pad {entry[key]!r} not on the board"
                raise RoutesError(msg)
            ends.append(by_label[entry[key]])
        a, b = ends
        if a.net != b.net or not a.net:
            msg = f"{label}: {a.label} (net {a.net or '-'}) and {b.label} (net {b.net or '-'}) are not on one net"
            raise RoutesError(msg)
        if _joined(board, pads, a, b):
            return []  # earlier run / earlier entry already connects them
        return [(a.net, a, b, f"{a.label} -> {b.label} ({a.net})")]
    pattern = entry["net"]
    nets = sorted({p.net for p in pads if p.net and fnmatch.fnmatchcase(p.net, pattern)})
    if not nets:
        msg = f"{label}: net {pattern!r} not on the board"
        raise RoutesError(msg)
    out = []
    for net in nets:
        if net in planes and net != pattern:
            continue  # connects through its pour
        members = [p for p in pads if p.net == net]
        out += [(net, a, b, f"{a.label} -> {b.label} ({net})") for a, b in _chain(board, net, members)]
    return out


def _group_of(board: board_mod.Board, net: str, members: list[Pad]) -> dict[int, int]:
    """Member index -> id of the group of pads the net's existing copper already joins."""
    obstacles = board.copper.pads_by_net.get(net, [])  # same order as board_pads
    if len(obstacles) != len(members):
        return {i: i for i in range(len(members))}
    vias = [(c, r) for _, c, r, n in board.copper.vias if n == net]
    segs = [s for s in board.copper.segs if s.net == net]
    groups = tracks.pad_groups(obstacles, vias, segs)
    return {i: g for g, grp in enumerate(groups) for i in grp}


def _joined(board: board_mod.Board, pads: list[Pad], a: Pad, b: Pad) -> bool:
    members = [p for p in pads if p.net == a.net]
    group_of = _group_of(board, a.net, members)
    return group_of[members.index(a)] == group_of[members.index(b)]


def _chain(board: board_mod.Board, net: str, members: list[Pad]) -> list[tuple[Pad, Pad]]:
    """Prim over pad centres: each pad not yet joined to the first, linked to its nearest joined pad.
    Pads the net's existing copper already connects count as joined."""
    if len(members) < 2:
        return []
    group_of = _group_of(board, net, members)
    joined = [i for i in range(len(members)) if group_of[i] == group_of[0]]
    todo = [i for i in range(len(members)) if i not in joined]
    out = []
    while todo:
        i, j = min(((i, j) for i in todo for j in joined), key=lambda ij: (_dist(members[ij[0]], members[ij[1]]), ij))
        out.append((members[j], members[i]))
        joined += [k for k in todo if group_of[k] == group_of[i]]
        todo = [k for k in todo if k not in joined]
    return out


def _dist(a: Pad, b: Pad) -> float:
    return math.dist(a.centre, b.centre)


def _route_one(
    board: board_mod.Board,
    entry: dict[str, Any],
    defaults: dict[str, Any],
    net: str,
    a: Pad,
    b: Pad,
    soft: frozenset[str] = frozenset(),
    soft_cost: float = 4.0,
    history: dict[tuple[str, int, int], float] | None = None,
) -> router.Route:
    cls = board.rules.class_of(net)
    layers = list(entry.get("layers", defaults.get("layers", board.layers)))
    via_max = entry.get("via_max")
    spec = router.Spec(
        layers,
        float(entry.get("width", cls.track_width)),
        None if via_max == 0 else (cls.via_diameter, cls.via_drill),
        corridor=tuple(float(v) for v in entry["corridor"]) if "corridor" in entry else None,  # type: ignore[arg-type]
        soft=soft - {net},
        soft_cost=soft_cost,
        history=history,
        reserved=defaults.get(RESERVED_KEY),
    )
    r = router.route(
        board,
        net,
        a.centre,
        b.centre,
        spec,
        start_layers=[layer for layer in layers if layer in a.layers],
        goal_layers=[layer for layer in layers if layer in b.layers],
        waypoints=[(float(x), float(y)) for x, y in entry.get("waypoints", [])],
    )
    if via_max is not None and len(r.vias) > via_max:
        msg = f"needs {len(r.vias)} via(s), via_max is {via_max}"
        raise router.RouteFailure(msg)
    return r


def _nodes(pcb: list[sexpr.Node], r: router.Route) -> list[list[sexpr.Node]]:
    """KiCad segment / via nodes of a route, content-derived UUIDs (the same board gives the same file)."""
    net_node = fixes._net_node(pcb, r.net)
    mm = fixes._mm
    nodes = []
    for layer, a, b in r.pieces:
        uid = uuid.uuid5(uuid.NAMESPACE_OID, f"route:{r.net}:{layer}:{mm(a[0])},{mm(a[1])}:{mm(b[0])},{mm(b[1])}")
        nodes.append(
            sexpr.loads(
                f"(segment (start {mm(a[0])} {mm(a[1])}) (end {mm(b[0])} {mm(b[1])}) (width {mm(r.width)}) "
                f'(layer "{layer}") (uuid "{uid}"))'
            )
        )
    for c in r.vias:
        uid = uuid.uuid5(uuid.NAMESPACE_OID, f"route-via:{r.net}:{mm(c[0])},{mm(c[1])}")
        nodes.append(
            sexpr.loads(
                f"(via (at {mm(c[0])} {mm(c[1])}) (size {mm(r.via_size[0])}) (drill {mm(r.via_size[1])}) "
                f'(layers "F.Cu" "B.Cu") (uuid "{uid}"))'
            )
        )
    if net_node is not None:
        for node in nodes:
            node.insert(len(node) - 1, copy.deepcopy(net_node))
    return nodes
