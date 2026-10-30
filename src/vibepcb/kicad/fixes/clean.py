"""Track clean-up: zero-length pieces, loops, crossings, tees, junctions, per-net cleaning."""

from __future__ import annotations

import functools
import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import KicadProject, _design_clearances, _design_min_track_width, _mm, _num, _segment_node
from vibepcb.kicad.fixes.copper import _Copper, _copper, _drop, _segments, _source, _with_new_segments
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibepcb.core.sexpr import Node


CLEAN_ROUNDS = 3


def _seg_points(seg: list[Node]) -> tuple[tuple[str, str], tuple[str, str]]:
    """Start and end of a segment as the file writes them (exact: equal text = the same point)."""
    s, e = sexpr.child(seg, "start"), sexpr.child(seg, "end")
    return (str(s[1]), str(s[2])) if s else ("", ""), (str(e[1]), str(e[2])) if e else ("", "")


def _drop_zero_length_segments(project: KicadProject) -> int:
    """Remove zero-length segments; returns how many. Such a piece connects nothing the items at its point don't
    connect anyway, and KiCad reports it as crossing any other net's track nearby (its direction is undefined);
    a via-end snap can collapse a short piece to one."""
    points = [s for s in sexpr.children(project.pcb, "segment") if _seg_points(s)[0] == _seg_points(s)[1]]
    for s in points:
        project.pcb.remove(s)
    return len(points)


def _drop_closed_loops(project: KicadProject) -> int:
    """Remove polylines that leave a pad or via and come back into the same one; returns how many.

    The router sometimes leaves such a loop (bench-psu rev C: six pieces out of a via and back, with a 70 degree
    bend). It connects nothing the pad or via doesn't connect already, but none of the other steps touch it:
    the loop opener works on junctions, the rerouter skips runs whose ends share an anchor. A loop goes only
    if the net's pads stay as connected as before; where its end was a pad's only connection (the end cap
    reaching a pad next to the via), a straight piece from the anchor into that pad replaces it.
    """
    copper = _copper(project.pcb)
    clearance, edge_clearance = _design_clearances(project)
    obstacles = copper.obstacles()
    gone: dict[str, set[int]] = {}
    dropped = 0
    for run in tracks.chains(copper.segs, copper.on_pad_or_via):
        a, b = run.points[0], run.points[-1]
        if len(run.segs) < 2 or not copper.same_anchor(a, b, run.net, run.layer):
            continue
        if all(copper.same_anchor(a, q, run.net, run.layer) for q in run.points):
            continue  # a stub inside the anchor's copper: nothing of its own
        segs = copper.segs_by_net.get(run.net, [])
        removed = gone.setdefault(run.net, set())
        mine = {id(s) for s in run.segs}
        pads, vias = copper.pads_by_net.get(run.net, []), copper.vias_by_net.get(run.net, [])
        now = [s for s in segs if id(s) not in removed]
        after = [s for s in now if id(s) not in mine]
        before = tracks.pad_groups(pads, vias, now)
        stub: tracks.Seg | None = None
        if not tracks.still_connected(before, tracks.pad_groups(pads, vias, after)):
            # the loop's end was a pad's connection (its cap reaching a pad next to the via): a straight piece
            # from the anchor into that pad replaces the loop, if it keeps its clearances
            width = min(s.width for s in run.segs)
            for pad in sorted(pads, key=lambda o: o.distance(a, a)):
                piece = tracks.Seg(a, pad.centre, width, run.layer, run.net)
                fits = all(
                    o.distance(a, pad.centre) >= clearance + width / 2
                    for o in obstacles
                    if o.net != run.net and run.layer in o.layers
                ) and all(e.distance(a, pad.centre) >= edge_clearance + width / 2 for e in copper.edges)
                if fits and tracks.still_connected(before, tracks.pad_groups(pads, vias, [*after, piece])):
                    stub = piece
                    break
            if stub is None:
                continue
        removed |= mine
        index = min(project.pcb.index(_source(s)) for s in run.segs)
        template = _source(run.segs[0])
        for s in run.segs:
            _drop(project.pcb, _source(s))
        if stub is not None:
            project.pcb.insert(index, _segment_node(template, stub.a, stub.b, stub.width))
        dropped += 1
    return dropped


def clean_tracks(project: KicadProject) -> list[str]:
    """Straighten tscircuit's tracks and take out every corner sharper than 135 degrees.

    The autorouter leaves overlapping duplicate routes of one net, polylines of many short
    pieces, zig-zags inside the pads it connects, spikes where a track runs out and back on
    itself (an antenna) and acute or 90 degree bends. Net by net (``vibepcb.tracks``):
    duplicates covered by the net's other copper go; then per polyline between pads, vias
    and junctions the part inside its end pads is trimmed, duplicate/spike/collinear points
    go, and sharp corners are cut across or chamfered - every new piece only where it keeps
    the net-class clearance to other nets' copper and the board-edge clearance. Each change
    is kept only if the net's pads stay as connected as before (checked per net, else per
    polyline).
    """
    clearance, edge_clearance = _design_clearances(project)
    min_width = _design_min_track_width(project)
    totals = {"duplicates": 0, "dropped": 0, "fixed": 0, "left": 0, "kept": 0}
    loops = _drop_closed_loops(project)  # first: the rounds would reshape a loop's corners instead
    for _ in range(CLEAN_ROUNDS):  # a round's own edits can leave new duplicates/corners
        changed, last = _clean_round(project, clearance, edge_clearance, min_width)
        for key, value in last.items():
            totals[key] = totals[key] + value if key in ("duplicates", "dropped", "fixed") else value
        if not changed:
            break
    changes = []
    if loops:
        changes.append(f"tracks: removed {loops} loop(s) leaving a pad or via and coming back into it")
    if dropped := _drop_zero_length_segments(project):
        changes.append(f"tracks: removed {dropped} zero-length track segment(s)")
    if totals["duplicates"]:
        changes.append(
            f"tracks: removed {totals['duplicates']} track segment(s) lying inside other copper of their net"
        )
    if totals["dropped"]:
        changes.append(
            f"tracks: dropped {totals['dropped']} redundant polyline point(s) (zig-zags in pads, spikes, straight runs)"
        )
    if totals["fixed"]:
        changes.append(f"tracks: removed {totals['fixed']} sharp corner(s) (cut across or chamfered to 45 deg)")
    if totals["left"]:
        changes.append(f"tracks: {totals['left']} sharp corner(s) left - no room to fix, check by eye")
    if totals["kept"]:
        changes.append(f"tracks: {totals['kept']} polyline(s) left as routed - cleaning would have cut a connection")
    return changes


def _split_crossings(project: KicadProject) -> int:
    """Cut tracks of one net that cross each other (an X, no shared vertex) at the crossing.

    The router sometimes runs a net across itself; KiCad joins the two there and measures
    the angle. With a vertex at the crossing the loop and junction clean-up can handle it.
    """
    copper = _copper(project.pcb)
    cuts: dict[int, list[tracks.Point]] = {}
    for _net, segs in copper.segs_by_net.items():
        for i, j in itertools.combinations(range(len(segs)), 2):
            s, t = segs[i], segs[j]
            if s.layer != t.layer:
                continue
            q = tracks.crossing(s.a, s.b, t.a, t.b)
            if q is None or min(math.dist(q, e) for e in (s.a, s.b, t.a, t.b)) <= tracks.EPS:
                continue
            cuts.setdefault(id(s), []).append(q)
            cuts.setdefault(id(t), []).append(q)
    by_id = {id(s): s for s in copper.segs}
    for sid, points in cuts.items():
        seg = by_id[sid]
        node = _source(seg)
        chain = [seg.a, *sorted(points, key=lambda p: math.dist(seg.a, p)), seg.b]
        index = project.pcb.index(node)
        project.pcb[index : index + 1] = [
            _segment_node(node, p, q, seg.width) for p, q in itertools.pairwise(chain) if math.dist(p, q) > tracks.EPS
        ]
    return len(cuts) // 2


def _split_tees(project: KicadProject, clearance: float, edge_clearance: float) -> int:
    """Give every T-junction a vertex in its through track and snap the ending track onto it.

    Track ends that already sit on a pad or via stay where they are (that is their connection).
    The snapped track pivots about its other end and must keep its clearance (seen: a 7 mm
    track swung 0.05 mm into a via); where it wouldn't, the T stays as it is (KiCad connects an
    end lying on another track's copper).
    """
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()

    def fits(seg: tracks.Seg, a: tracks.Point, b: tracks.Point) -> bool:
        half = seg.width / 2
        return all(
            o.distance(a, b) >= clearance + half for o in obstacles if o.net != seg.net and seg.layer in o.layers
        ) and all(e.distance(a, b) >= edge_clearance + half for e in copper.edges if seg.layer in e.layers)

    found = [
        (i, end, j, q)
        for i, end, j, q in tracks.tees(copper.segs)
        if not copper.attached(copper.segs[i].b if end else copper.segs[i].a, copper.segs[i])
        and fits(copper.segs[i], q, copper.segs[i].a if end else copper.segs[i].b)
    ]
    cuts: dict[int, list[tracks.Point]] = {}
    for i, end, j, q in found:
        node = sexpr.child(_source(copper.segs[i]), ("start", "end")[end])
        if node is not None:
            node[1:] = [Sym(_mm(q[0])), Sym(_mm(q[1]))]
        cuts.setdefault(j, []).append(q)
    for j, points in cuts.items():
        node = _source(copper.segs[j])
        a, b = ((_num(sexpr.child(node, k), 1), _num(sexpr.child(node, k), 2)) for k in ("start", "end"))
        chain = [a, *sorted(points, key=lambda p: math.dist(a, p)), b]
        index = project.pcb.index(node)
        width = copper.segs[j].width
        project.pcb[index : index + 1] = [
            _segment_node(node, p, q, width) for p, q in itertools.pairwise(chain) if math.dist(p, q) > tracks.EPS
        ]
    return len(found)


def _snap_via_ends(project: KicadProject, clearance: float, edge_clearance: float) -> int:
    """Move track ends lying inside a via of their net onto the via's centre.

    KiCad connects a track to a via through the via centre anyway; with every end on the
    centre, two tracks meeting at a via form one polyline whose bend the corner clean-up
    can work on, instead of two overlapping ends at some angle. The moved track pivots
    about its other end: it must keep its clearance (seen: a 1.7 mm track swung into a pad).
    """
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()

    def fits(s: tracks.Seg, a: tracks.Point, b: tracks.Point) -> bool:
        half = s.width / 2
        return all(
            o.distance(a, b) >= clearance + half for o in obstacles if o.net != s.net and s.layer in o.layers
        ) and all(e.distance(a, b) >= edge_clearance + half for e in copper.edges if s.layer in e.layers)

    moved = 0
    for s in copper.segs:
        for key, p in (("start", s.a), ("end", s.b)):
            other = s.b if key == "start" else s.a
            for _, centre, radius, net in copper.vias:
                if net == s.net and 0 < math.dist(p, centre) <= min(radius, s.width / 2) and fits(s, centre, other):
                    node = sexpr.child(_source(s), key)
                    if node is not None:
                        node[1:] = [Sym(_mm(centre[0])), Sym(_mm(centre[1]))]
                        moved += 1
                    break
    return moved


COLLAPSE_BRANCH_MM = 1.0  # a junction may move to the far end of a branch this short


def _open_junctions(project: KicadProject, clearance: float, edge_clearance: float) -> int:
    """Move junctions (3+ tracks meeting, not on a pad/via) whose branches meet at an acute angle.

    All tracks meeting there move their common end together; each moved track must keep its
    clearances (``tracks.move_junction``). A move is undone if the net's pads are less connected
    afterwards: moving an end swings the track's body, and a pad counts as attached when its centre
    lies on a track's body (bench-psu rev C: a USB-C pad lost its only track that way).
    """
    copper = _copper(project.pcb)
    obstacles = copper.obstacles()
    moved = 0
    touched: set[int] = set()
    before_groups: dict[str, list[set[int]]] = {}

    def net_groups(net: str) -> list[set[int]]:
        segs = [s for s in _segments(project.pcb, copper.names) if s.net == net]
        return tracks.pad_groups(copper.pads_by_net.get(net, []), copper.vias_by_net.get(net, []), segs)

    for (net, layer, _), users in tracks.junctions(copper.segs).items():
        j = copper.segs[users[0][0]].b if users[0][1] else copper.segs[users[0][0]].a
        others = [copper.segs[i].a if end else copper.segs[i].b for i, end in users]
        if copper.on_pad_or_via(j, net, layer) or tracks.min_branch_angle(j, others) >= tracks.MIN_JUNCTION_DEG:
            continue
        relevant = [o for o in obstacles if o.net != net and layer in o.layers]
        edges = [e for e in copper.edges if layer in e.layers]
        # each moved piece keeps clearance at its own width (a 0.15 mm branch off a 0.4 mm track)
        width_to = {other: copper.segs[i].width for other, (i, _) in zip(others, users, strict=True)}
        widest = max(width_to.values())

        def fits(
            a: tracks.Point,
            b: tracks.Point,
            relevant: list[tracks.Obstacle] = relevant,
            edges: list[tracks.Obstacle] = edges,
            width_to: dict[tracks.Point, float] = width_to,
            widest: float = widest,
        ) -> bool:
            width = width_to.get(b, widest)
            return all(o.distance(a, b) >= clearance + width / 2 for o in relevant) and all(
                e.distance(a, b) >= edge_clearance + width / 2 for e in edges
            )

        if any(i in touched for i, _ in users):
            continue  # a track an earlier junction of this pass changed: next round
        q = tracks.move_junction(j, others, fits)
        collapsed: int | None = None
        if q is None or tracks.min_branch_angle(q, others) < tracks.MIN_JUNCTION_DEG:
            # a short branch can disappear: the junction moves to its far end, the rest follow
            for (i, _), far in sorted(zip(users, others, strict=True), key=lambda u: math.dist(j, u[1])):
                if math.dist(j, far) > COLLAPSE_BRANCH_MM:
                    break
                rest = [o for o in others if o is not far]
                if tracks.min_branch_angle(far, rest) >= tracks.MIN_JUNCTION_DEG and all(fits(far, o) for o in rest):
                    q, collapsed = far, i
                    break
        if q is None:
            continue
        if net not in before_groups:
            before_groups[net] = net_groups(net)
        undo: list[tuple[list[Node], list[Node]]] = []  # (node, its old values)
        dropped: tuple[int, list[Node]] | None = None
        for i, end in users:
            if i == collapsed:
                seg_node = _source(copper.segs[i])
                dropped = (project.pcb.index(seg_node), seg_node)
                _drop(project.pcb, seg_node)
                continue
            node = sexpr.child(_source(copper.segs[i]), ("start", "end")[end])
            if node is not None:
                undo.append((node, node[1:]))
                node[1:] = [Sym(_mm(q[0])), Sym(_mm(q[1]))]
        if not tracks.still_connected(before_groups[net], net_groups(net)):
            for node, old in undo:
                node[1:] = old
            if dropped is not None:
                project.pcb.insert(*dropped)
            continue
        touched.update(i for i, _ in users)
        moved += 1
    return moved


def _clean_round(
    project: KicadProject, clearance: float, edge_clearance: float, min_width: float
) -> tuple[bool, dict[str, int]]:
    """One pass of :func:`clean_tracks` over all nets; returns (anything changed, stats)."""
    totals = {"duplicates": 0, "dropped": 0, "fixed": 0, "left": 0, "kept": 0}
    changed = (
        _split_crossings(project)
        + _split_tees(project, clearance, edge_clearance)
        + _snap_via_ends(project, clearance, edge_clearance)
        + _open_junctions(project, clearance, edge_clearance)
        > 0
    )
    stale = False
    copper = _copper(project.pcb)
    for net in sorted({s.net for s in copper.segs}):
        if stale:  # earlier nets' segments were replaced (only segments: see _with_new_segments)
            copper, stale = _with_new_segments(copper, project.pcb), False
        segs = copper.segs_by_net.get(net, [])
        pads = copper.pads_by_net.get(net, [])
        vias = copper.vias_by_net.get(net, [])
        # vias into a zone of the net reach its plane: a pad's track to one is its connection
        planes = [poly for (n, _), polys in copper.zones.items() if n == net for poly in polys]
        anchors = [i for i, (c, _) in enumerate(vias) if any(tracks.point_in_polygon(c, poly) for poly in planes)]
        before = tracks.pad_groups(pads, vias, segs, anchors)

        def connected(
            pieces: list[_Piece],
            pads: list[tracks.Obstacle] = pads,
            vias: list[tuple[tracks.Point, float]] = vias,
            before: list[set[int]] = before,
            anchors: list[int] = anchors,
        ) -> bool:
            rebuilt = [tracks.Seg(a, b, w, layer, "") for _, a, b, w, layer in pieces]
            return tracks.still_connected(before, tracks.pad_groups(pads, vias, rebuilt, anchors))

        drop: set[int] = set()  # duplicates, each dropped only if the net stays connected without it
        for i in tracks.redundant(segs):
            remaining = [(_source(s), s.a, s.b, s.width, s.layer) for j, s in enumerate(segs) if j not in drop | {i}]
            if connected(remaining):
                drop.add(i)
        drop |= _open_loops(copper, segs, drop, connected)
        result = _clean_net(copper, segs, drop, clearance, edge_clearance, min_width)
        if not connected(result.pieces()):
            result.choose(connected)
        if not result.changed():
            continue
        index = min(project.pcb.index(_source(s)) for s in segs)
        for s in segs:
            _drop(project.pcb, _source(s))
        project.pcb[index:index] = [_segment_node(template, a, b, w) for template, a, b, w, _ in result.pieces()]
        changed = stale = True
        for key, value in result.stats().items():
            totals[key] += value
    return changed, totals


def _open_loops(
    copper: _Copper, segs: list[tracks.Seg], drop: set[int], connected: Callable[[list[_Piece]], bool]
) -> set[int]:
    """Tracks to remove where a net meets itself at a sharp angle (junction or T) on a loop.

    Routing each connection separately leaves loops and lattices of one net; their
    junctions have sharp angles that no chamfer fixes. A track at such a junction goes,
    together with whatever is left dangling without it, if the net's pads stay connected.
    Shortest first; each removal is checked against what is left.
    """
    removed: set[int] = set()
    base = [j for j in range(len(segs)) if j not in drop]
    for target in [base[k] for k in tracks.sharp_junctions([segs[j] for j in base])]:
        if target in removed:
            continue
        rest_idx = [j for j in base if j not in removed and j != target]
        rest = [segs[j] for j in rest_idx]
        gone = {target} | {rest_idx[k] for k in tracks.dangling(rest, copper.attached)}
        remaining = [
            (_source(segs[j]), segs[j].a, segs[j].b, segs[j].width, segs[j].layer)
            for j in base
            if j not in removed | gone
        ]
        if connected(remaining):
            removed |= gone
    return removed


type _Piece = tuple[list[Node], tracks.Point, tracks.Point, float, str]  # template node, a, b, width, layer


@dataclass
class _ChainEdit:
    old: list[_Piece]
    new: list[_Piece]
    dropped: int
    fixed: int
    left: int
    use_new: bool = True


@dataclass
class _NetClean:
    fixed_pieces: list[_Piece] = field(default_factory=list)  # duplicates already dropped, untouched loops
    edits: list[_ChainEdit] = field(default_factory=list)
    duplicates: int = 0

    def pieces(self) -> list[_Piece]:
        return self.fixed_pieces + [p for e in self.edits for p in (e.new if e.use_new else e.old)]

    def changed(self) -> bool:
        return bool(self.duplicates) or any(e.use_new and e.new != e.old for e in self.edits)

    def choose(self, connected: Callable[[list[_Piece]], bool]) -> None:
        """Apply the polyline edits one by one, keeping each only while the net stays connected."""
        for edit in self.edits:
            edit.use_new = False
        for edit in self.edits:
            if edit.new != edit.old:
                edit.use_new = True
                if not connected(self.pieces()):
                    edit.use_new = False

    def stats(self) -> dict[str, int]:
        used = [e for e in self.edits if e.use_new]
        return {
            "duplicates": self.duplicates,
            "dropped": sum(e.dropped for e in used),
            "fixed": sum(e.fixed for e in used),
            "left": sum(e.left for e in used) + sum(e.fixed + e.left for e in self.edits if not e.use_new),
            "kept": sum(1 for e in self.edits if not e.use_new and e.new != e.old),
        }


def _clean_net(
    copper: _Copper,
    segs: list[tracks.Seg],
    duplicates: set[int],
    clearance: float,
    edge_clearance: float,
    min_width: float,
) -> _NetClean:
    """The cleaned-up tracks of one net without ``duplicates`` (see :func:`clean_tracks`), not yet written to the board."""
    out = _NetClean()
    out.duplicates = len(duplicates)
    kept = [s for i, s in enumerate(segs) if i not in duplicates]
    obstacles = copper.obstacles()

    def region_of(p: tracks.Point, net: str, layer: str) -> int | None:
        """Index of the same-net pad/via the point lies in (pads first)."""
        for i, o in enumerate(copper.pads):
            if o.net == net and layer in o.layers and o.distance(p, p) <= tracks.EPS:
                return i
        for j, (_, centre, radius, via_net) in enumerate(copper.vias):
            if via_net == net and math.dist(p, centre) <= radius:
                return len(copper.pads) + j
        return None

    def pad_region_of(p: tracks.Point, net: str, layer: str) -> int | None:
        """Like region_of, but rectangular pads only and strictly inside: a track must keep
        running to a via's centre, and round/oval pads are smaller than the box the pad
        test uses."""
        r = region_of(p, net, layer)
        return r if r is not None and r in copper.rect_pads and copper.pads[r].distance(p, p) <= 0 else None

    def touches(region: int, a: tracks.Point, b: tracks.Point, half_width: float) -> bool:
        """Whether a piece a-b passing a pad/via still connects to it the way KiCad sees it: over its centre."""
        centre = copper.pads[region].centre if region < len(copper.pads) else copper.vias[region - len(copper.pads)][1]
        return tracks.on_track_body(centre, a, b, half_width - tracks.EPS)

    in_chain: set[int] = set()
    for chain in tracks.chains(kept, lambda p, net, layer: pad_region_of(p, net, layer) is not None):
        in_chain |= {id(s) for s in chain.segs}
        fits = tracks.clearance_check(chain, obstacles, copper.edges, clearance, edge_clearance, min_width)
        region = functools.partial(region_of, net=chain.net, layer=chain.layer)

        def keeps(
            old: tracks.Point,
            a: tracks.Point,
            b: tracks.Point,
            region: Callable[[tracks.Point], int | None] = region,
            chain: tracks.Chain = chain,
        ) -> bool:
            r = region(old)
            return r is None or touches(r, a, b, chain.width_at(a, b) / 2)

        original = chain.points
        in_pad = functools.partial(pad_region_of, net=chain.net, layer=chain.layer)
        end_vias = [
            (c, r)
            for _, c, r, net in copper.vias
            if net == chain.net and min(math.dist(c, original[0]), math.dist(c, original[-1])) <= tracks.EPS
        ]

        def in_end_via(p: tracks.Point, end_vias: list[tuple[tracks.Point, float]] = end_vias) -> bool:
            return any(math.dist(p, c) <= r for c, r in end_vias)

        points = tracks.simplify(tracks.drop_inside_start(tracks.trim_ends(original, in_pad), in_end_via), keeps)
        old_pieces = set(itertools.pairwise(original))
        if not all(fits(a, b) for a, b in itertools.pairwise(points) if (a, b) not in old_pieces):
            points = tracks.simplify(original, keeps)  # straightening would get too close to something
            if not all(fits(a, b) for a, b in itertools.pairwise(points) if (a, b) not in old_pieces):
                points = original
        sharp = len(tracks.sharp_corners(points))
        dropped = len(original) - len(points)
        points = tracks.round_off_corners(points, fits, keeps)
        remaining = len(tracks.sharp_corners(points))
        old: list[_Piece] = [(_source(s), s.a, s.b, s.width, s.layer) for s in chain.segs]
        if points == original:
            out.edits.append(_ChainEdit(old, old, 0, 0, remaining))
            continue
        template = _source(chain.segs[0])
        new: list[_Piece] = [(template, a, b, chain.width_at(a, b), chain.layer) for a, b in itertools.pairwise(points)]
        out.edits.append(_ChainEdit(old, new, dropped, sharp - remaining, remaining))
    out.fixed_pieces = [(_source(s), s.a, s.b, s.width, s.layer) for s in kept if id(s) not in in_chain]  # closed loops
    return out
