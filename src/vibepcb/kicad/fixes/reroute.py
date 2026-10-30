"""Local rerouting of track stretches that break a rule."""

from __future__ import annotations

import itertools
import math
from typing import TYPE_CHECKING

from vibepcb.kicad.fixes.base import (
    REROUTE_GRID_MARGIN_MM,
    REROUTE_GROW_PIECES,
    REROUTE_MAX_PATH_MM,
    REROUTE_WINDOWS_MM,
    KicadProject,
    _design_clearances,
    _mm,
    _segment_node,
    log,
)
from vibepcb.kicad.fixes.copper import _Copper, _copper, _drop, _source
from vibepcb.kicad.fixes.corners import _anchored_pieces, _piece_key, _route_with_stub
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from collections.abc import Callable


def reroute_tracks(project: KicadProject) -> list[str]:
    """Rip up and reroute the track runs that still break a rule, with a local 45-degree maze router.

    A run (polyline between pads, vias, junctions and loose ends) is a problem if a piece is
    closer than the clearance to another net's copper or the board edge, a bend is sharper
    than 135 deg, or it meets other tracks at a junction at an acute angle. The new route
    keeps the run's ends and width, turns at most 45 deg at a time, leaves the junctions at
    90 deg or more and keeps every clearance; the old one stays if no such route exists.
    """
    clearance, edge_clearance = _design_clearances(project)
    changes: list[str] = []
    tried: set[tuple[str, str, tuple[int, int], tuple[int, int]]] = set()
    made: set[frozenset[tuple[int, int]]] = set()  # pieces this call routed
    for _ in range(40):  # one run per pass: the copper changes with every reroute
        copper = _copper(project.pcb)
        obstacles = copper.obstacles()
        at_end: dict[tuple[str, str, tuple[int, int]], list[tuple[tracks.Seg, tracks.Point]]] = {}
        for s in copper.segs:
            for e, f in ((s.a, s.b), (s.b, s.a)):
                at_end.setdefault((s.net, s.layer, tracks._key(e)), []).append((s, f))
        runs = tracks.chains(copper.segs, copper.on_pad_or_via)
        done = False
        for run in runs:
            pts = run.points
            key = (run.net, run.layer, tracks._key(pts[0]), tracks._key(pts[-1]))
            length = sum(math.dist(a, b) for a, b in itertools.pairwise(pts))
            if len(pts) < 2 or key in tried:
                continue
            if all(_piece_key(a, b) in made for a, b in itertools.pairwise(pts)):
                continue  # this call's own route: rerouting it again only creeps
            if copper.same_anchor(pts[0], pts[-1], run.net, run.layer):
                continue  # inside one via or pad: no copper of its own to reroute
            width = max(
                {s.width for s in run.segs}, key=lambda w: sum(math.dist(s.a, s.b) for s in run.segs if s.width == w)
            )
            half = width / 2
            widest = REROUTE_WINDOWS_MM[-1]
            x0, y0 = min(p[0] for p in pts) - widest, min(p[1] for p in pts) - widest
            x1, y1 = max(p[0] for p in pts) + widest, max(p[1] for p in pts) + widest
            far = clearance + 1.0
            others = [
                o
                for o in obstacles
                if o.net != run.net
                and run.layer in o.layers
                and o.box[0] < x1 + far
                and o.box[2] > x0 - far
                and o.box[1] < y1 + far
                and o.box[3] > y0 - far
            ]
            edges = [e for e in copper.edges if run.layer in e.layers]
            # the search depends only on the run, its width and the copper (any net) within reach: a run that
            # found no route with exactly this surrounding finds none again - skip it (later post-fill passes
            # retried every hopeless run with dozens of exhaustive grid searches)
            fingerprint = (
                tuple(tracks._key(p) for p in pts),
                round(width * 1000),
                tuple(
                    sorted(
                        (
                            o.net,
                            tuple(sorted(o.layers)),
                            tuple(tracks._key(q) for q in o.points),
                            round(o.radius * 1000),
                        )
                        for o in obstacles
                        if o.box[0] < x1 + far and o.box[2] > x0 - far and o.box[1] < y1 + far and o.box[3] > y0 - far
                    )
                ),
            )
            if project.reroute_failed.get(key) == fingerprint:
                continue
            mine = {id(s) for s in run.segs}

            def fits(
                a: tracks.Point,
                b: tracks.Point,
                others: list[tracks.Obstacle] = others,
                edges: list = edges,
                half: float = half,
            ) -> bool:
                return all(o.distance(a, b) >= clearance + half for o in others) and all(
                    e.distance(a, b) >= edge_clearance + half for e in edges
                )

            def branches(
                p: tracks.Point,
                run: tracks.Chain = run,
                mine: set[int] = mine,
                copper: _Copper = copper,
                at_end: dict[tuple[str, str, tuple[int, int]], list[tuple[tracks.Seg, tracks.Point]]] = at_end,
            ) -> list[tracks.Point]:
                """Far ends of the other tracks meeting at p.

                On a pad or via, tracks merely ending there make no junction angle; a track whose
                side p lies on (it passes through, its copper overlapping the run's end) does.
                """
                if copper.on_pad_or_via(p, run.net, run.layer):
                    return [
                        e
                        for t in copper.segs_by_net.get(run.net, [])
                        if t.layer == run.layer
                        and id(t) not in mine
                        and min(math.dist(p, t.a), math.dist(p, t.b)) > tracks.EPS
                        and tracks.point_seg_distance(p, t.a, t.b) <= t.width / 2
                        for e in (t.a, t.b)
                    ]
                return [f for s, f in at_end.get((run.net, run.layer, tracks._key(p)), []) if id(s) not in mine]

            def junction_ok(
                p: tracks.Point,
                towards: tracks.Point,
                branches: Callable[[tracks.Point], list[tracks.Point]] = branches,
            ) -> bool:
                return all(tracks.interior_angle(towards, p, f) >= tracks.MIN_JUNCTION_DEG - 0.5 for f in branches(p))

            clear_at = clearance + half + REROUTE_GRID_MARGIN_MM
            edge_at = edge_clearance + half + REROUTE_GRID_MARGIN_MM

            def free_within(
                window: float,
                others: list[tracks.Obstacle] = others,
                edges: list[tracks.Obstacle] = edges,
                box: tuple[float, float, float, float] = (x0 + widest, y0 + widest, x1 - widest, y1 - widest),
                clear_at: float = clear_at,
                edge_at: float = edge_at,
            ) -> Callable[[tracks.Point], bool]:
                """``free(pt)`` for routes leaving the old route's box by at most ``window``."""

                index = tracks.BoxIndex([o.box for o in others])
                memo: dict[tuple[int, int], bool] = {}
                reach = clear_at + tracks.EPS

                def free(pt: tracks.Point) -> bool:
                    x, y = pt
                    if not (box[0] - window <= x <= box[2] + window and box[1] - window <= y <= box[3] + window):
                        return False
                    key = (round(x * 1000), round(y * 1000))
                    if key not in memo:  # every grid node is asked once per heading: remember
                        near = index.overlapping(x - reach, y - reach, x + reach, y + reach)
                        memo[key] = all(others[i].distance(pt, pt) >= clear_at for i in near) and all(
                            e.distance(pt, pt) >= edge_at for e in edges
                        )
                    return memo[key]

                return free

            n = len(pts)
            bad = {i for i in range(n - 1) if not fits(pts[i], pts[i + 1])}
            for k in range(1, n - 1):
                if (
                    not copper.on_pad_or_via(pts[k], run.net, run.layer)
                    and tracks.interior_angle(pts[k - 1], pts[k], pts[k + 1]) < tracks.MIN_CORNER_DEG - 1
                ):
                    bad |= {k - 1, k}
            if not junction_ok(pts[0], pts[1]):
                bad.add(0)
            if not junction_ok(pts[-1], pts[-2]):
                bad.add(n - 2)
            if not bad:
                continue
            tried.add(key)
            anchored = _anchored_pieces(copper, run, mine)
            new_points = None
            # reroute the bad stretch only - its legal surroundings stay; widen it while nothing fits
            for grow in REROUTE_GROW_PIECES:
                lo, hi = max(0, min(bad) - grow), min(n - 2, max(bad) + grow)
                stretch = sum(math.dist(pts[i], pts[i + 1]) for i in range(lo, hi + 1))
                if stretch > REROUTE_MAX_PATH_MM:  # a long run: only its bad part is rerouted
                    break
                # something joins the run on an anchored piece: it must stay - start / end the
                # stretch next to it, give up if it lies inside
                inside = [i for i in anchored if lo <= i <= hi]
                lo = max([i + 1 for i in inside if i <= min(bad)], default=lo)
                hi = min([i - 1 for i in inside if i >= max(bad)], default=hi)
                if lo > hi or any(lo <= i <= hi for i in anchored):
                    log.debug("reroute %s %s: stretch carries a T-junction, via or pad", run.net, run.layer)
                    break
                start, goal = pts[lo], pts[hi + 1]
                before = pts[lo - 1] if lo > 0 else None
                after = pts[hi + 2] if hi + 2 < n else None

                def leaves_ok(
                    p: tracks.Point,
                    towards: tracks.Point,
                    neighbour: tracks.Point | None,
                    junction_ok: Callable[[tracks.Point, tracks.Point], bool] = junction_ok,
                ) -> bool:
                    """At a kept neighbour: a bend of 135 deg or more; at the run's end: the junction rule."""
                    if neighbour is None:
                        return junction_ok(p, towards)
                    return tracks.interior_angle(neighbour, p, towards) >= tracks.MIN_CORNER_DEG - 0.5

                route = None
                for window in REROUTE_WINDOWS_MM:
                    free_here = free_within(window)
                    route = _route_with_stub(start, goal, before, after, free_here, fits, leaves_ok)
                    if route is None:  # the far end may be the one no grid heading can leave
                        back = _route_with_stub(goal, start, after, before, free_here, fits, leaves_ok)
                        route = None if back is None else back[::-1]
                    if route is not None:
                        break
                if route is None or len(route) < 2:
                    log.debug("reroute %s %s %s-%s: no route", run.net, run.layer, start, goal)
                    continue
                candidate = [*pts[:lo], *route, *pts[hi + 2 :]]
                bends = [
                    tracks.interior_angle(candidate[i - 1], candidate[i], candidate[i + 1])
                    for i in range(1, len(candidate) - 1)
                    if not copper.on_pad_or_via(candidate[i], run.net, run.layer)
                ]
                new_length = sum(math.dist(p, q) for p, q in itertools.pairwise(candidate))
                if (
                    all(fits(p, q) for p, q in itertools.pairwise(candidate))
                    and all(angle >= tracks.MIN_CORNER_DEG - 0.5 for angle in bends)
                    and junction_ok(candidate[0], candidate[1])
                    and junction_ok(candidate[-1], candidate[-2])
                    and new_length <= 3 * length + 5
                ):
                    new_points = candidate
                    break
                log.debug("reroute %s %s %s-%s: route rejected", run.net, run.layer, start, goal)
            if new_points is not None:
                # the new route must join what the old run joined: the net's pads as connected as before
                mine_ids = {id(s) for s in run.segs}
                net_segs = copper.segs_by_net.get(run.net, [])
                pads, vias = copper.pads_by_net.get(run.net, []), copper.vias_by_net.get(run.net, [])
                rebuilt = [s for s in net_segs if id(s) not in mine_ids] + [
                    tracks.Seg(p, q, width, run.layer, run.net) for p, q in itertools.pairwise(new_points)
                ]
                if not tracks.still_connected(
                    tracks.pad_groups(pads, vias, net_segs), tracks.pad_groups(pads, vias, rebuilt)
                ):
                    log.debug("reroute %s %s: new route would split the net", run.net, run.layer)
                    new_points = None
            if new_points is None:
                project.reroute_failed[key] = fingerprint
                continue
            template = _source(run.segs[0])
            index = min(project.pcb.index(_source(s)) for s in run.segs)
            for s in run.segs:
                _drop(project.pcb, _source(s))
            for k, (p, q) in enumerate(itertools.pairwise(new_points)):
                project.pcb.insert(index + k, _segment_node(template, p, q, width))
                made.add(_piece_key(p, q))
            changes.append(
                f"{run.net} on {run.layer}: run ({_mm(pts[0][0])}, {_mm(pts[0][1])}) - "
                f"({_mm(pts[-1][0])}, {_mm(pts[-1][1])}) rerouted"
            )
            done = True
            break
        if not done:
            break
    return changes
