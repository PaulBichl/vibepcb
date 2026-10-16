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
    net = "SD_CLK*"                  # every net matching (not a [planes] net unless named exactly), all pads
"""

from __future__ import annotations

import copy
import fnmatch
import math
import tomllib
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb import board as board_mod
from vibepcb import fixes, router, sexpr, tracks

if TYPE_CHECKING:
    from pathlib import Path

    from vibepcb.fixes import KicadProject

ROUTES_NAME = "routes.toml"
ENTRY_KEYS = {"net", "from", "to", "layers", "via_max", "waypoints", "corridor", "width", "order"}
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
            if (esc := ends.get((ref, str(pad[1])))) is not None and esc.end_layer:
                centre, layers = to_board(esc.end[0], -esc.end[1]), frozenset({fixes._kicad_layer(esc.end_layer)})
            out.append(Pad(ref, str(pad[1]), fixes._net_of(pad, names), centre, layers))
    return out


def apply(project: KicadProject, data: dict[str, Any], *, only: str | None = None) -> Report:
    """Route every entry onto ``project.pcb``; report per connection. Failed entries leave no copper."""
    report = Report()
    board = board_mod.Board.from_project(project)
    pads = board_pads(project)
    by_label = {p.label: p for p in pads}
    planes = set(project.config.get("planes", {}).get("nets", []))
    defaults = data.get("defaults", {})
    new_nodes: list[list[sexpr.Node]] = []
    for i, entry in enumerate(data.get("route", []), 1):
        conns = [
            c
            for c in _connections(entry, i, board, pads, by_label, planes)
            if not only or fnmatch.fnmatchcase(c[0], only)
        ]
        if entry.get("order", "outside-in") == "outside-in":
            conns = _outside_in(conns)
        results, nodes = _run_entry(board, project.pcb, entry, defaults, conns)
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


RIP_UP_BUDGET = 3  # rip-ups allowed per connection of an entry


def _run_entry(
    board: board_mod.Board,
    pcb: list[sexpr.Node],
    entry: dict[str, Any],
    defaults: dict[str, Any],
    conns: list[tuple[str, Pad, Pad, str]],
) -> tuple[list[Result], list[list[sexpr.Node]]]:
    """Route an entry's connections in order, negotiating for space.

    A connection that can't get through may cross the entry's routed copper at a cost (the router counts
    that as soft); the nets it crosses are ripped up and queued again behind it. The greedy order alone
    decides who gets a channel, and ripping up the blockers is how a human re-does the first lane.
    """
    start = board.checkpoint()
    committed: dict[str, tuple[tuple[str, Pad, Pad, str], router.Route]] = {}
    results: dict[str, Result] = {}
    queue = list(conns)
    budget = RIP_UP_BUDGET * len(conns)
    while queue:
        conn = queue.pop(0)
        net, a, b, name = conn
        soft = frozenset(c[0] for c, _ in committed.values()) if budget > 0 else frozenset()
        try:
            r = _route_one(board, entry, defaults, net, a, b, soft)
        except router.RouteFailure as e:
            results[name] = Result(name, False, str(e))
            continue
        victims = [n for n, (c, _) in committed.items() if c[0] in r.crossed]
        if victims:
            budget -= len(victims)
            board.rollback(start)
            for n in victims:
                queue.append(committed.pop(n)[0])
                results.pop(n, None)
            for _, kept in committed.values():
                kept.commit(board)
        r.commit(board)
        committed[name] = (conn, r)
        results[name] = Result(name, True, f"{r.length:.2f} mm, {len(r.vias)} via(s)", r.length, len(r.vias))
    nodes: list[list[sexpr.Node]] = []
    for _, r in committed.values():
        nodes += _nodes(pcb, r)
    return [results[c[3]] for c in conns if c[3] in results], nodes


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
