"""The post-fill loop of `check`: pour-provided tracks dropped, corners and tees re-checked."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from vibepcb.core import sexpr
from vibepcb.kicad.fixes.base import KicadProject, _mm, _segment_node
from vibepcb.kicad.fixes.clean import _drop_closed_loops, _drop_zero_length_segments, clean_tracks
from vibepcb.kicad.fixes.copper import _copper, _drop, _source
from vibepcb.kicad.fixes.corners import (
    T_ON_LINE_MM,
    collapse_bend_stubs,
    snap_bends_to_45,
    snap_near_ends,
    square_tees,
    straighten_via_exits,
)
from vibepcb.kicad.fixes.push import push_tracks_clear, remove_antennas, separate_ends_on_pads
from vibepcb.kicad.fixes.reroute import reroute_tracks
from vibepcb.kicad.fixes.rules import THERMAL_GAP_MM
from vibepcb.kicad.fixes.vias import relocate_vias
from vibepcb.routing import tracks

if TYPE_CHECKING:
    from collections.abc import Callable


POUR_SAMPLE_MM = 0.05


KEEP_RADIUS_MM = 1.5


ONLY_RADIUS_MM = 0.1  # targeted pruning: tracks at a reported spot only


PIECE_MATCH_MM = 0.25  # the refill after pruning moves a fill piece's corners a little


SPOKE_REACH_MM = 0.05  # fill outline within this of a pad: a spoke reaches it


def prune_pour_tracks(
    project: KicadProject, keep_near: list[tracks.Point] | None = None, only_near: list[tracks.Point] | None = None
) -> list[str]:
    """Remove tracks of a poured net that the (KiCad-filled) pour already provides.

    tscircuit routes every GND connection as a track before it pours; on a board with GND
    pours those tracks are redundant, take routing room and short-circuit the thermal
    reliefs. A track goes when every point along it lies in its net's fill on that layer,
    in one of its net's pads or within the thermal gap around such a pad (so the pad then
    joins the pour through its spokes), all in one piece of the fill. Tracks through gaps the
    pour cannot reach, or bridging two separate fill pieces, stay.
    With ``only_near``, only tracks at those points are candidates (``check``'s fallback: the
    poured-net tracks at acute angles). Tracks within ``KEEP_RADIUS_MM`` of a ``keep_near`` point stay (``check`` passes the
    spots KiCad reported unconnected after an earlier attempt, e.g. where a pour island only
    held on through the track). Needs zone fills, i.e. runs after KiCad's refill (``check``).
    """
    copper = _copper(project.pcb, filled_zones=True)
    gap = THERMAL_GAP_MM + 0.05

    contacts: dict[tuple[int, str], list[tuple[int, tracks.Point]]] = {}  # (pad, layer) -> (piece, outline point)

    def pieces_at_pad(k: int, s: tracks.Seg) -> set[int]:
        """Fill pieces reaching pad k other than along track s: where a track enters a pad the fill
        hugs it up to the pad - that contact goes with the track, it is no thermal spoke."""
        if (k, s.layer) not in contacts:
            pad = copper.pads[k]
            x0, y0, x1, y1 = pad.box
            r = SPOKE_REACH_MM
            contacts[k, s.layer] = [
                (i, v)
                for i, poly in enumerate(copper.zones.get((pad.net, s.layer), []))
                for v in poly
                if x0 - r <= v[0] <= x1 + r and y0 - r <= v[1] <= y1 + r and pad.distance(v, v) <= r
            ]
        clear = s.width / 2 + SPOKE_REACH_MM
        return {i for i, v in contacts[k, s.layer] if tracks.point_seg_distance(v, s.a, s.b) > clear}

    def piece(p: tracks.Point, s: tracks.Seg) -> int | tuple[int, ...] | None:
        """Fill piece (index) of the track's net and layer that p lies in; at a pad the pad (as a
        1-tuple) - it must reach the same piece through its spokes; None: neither."""
        for i, poly in enumerate(copper.zones.get((s.net, s.layer), [])):
            if tracks.point_in_polygon(p, poly):
                return i
        for k, o in enumerate(copper.pads):
            if o.net == s.net and s.layer in o.layers and o.distance(p, p) <= gap:
                return (k,)
        return None

    # KiCad reports a pour fragment that lost its link by a corner of that fill piece: keep
    # every track touching such a piece
    kept_pieces: dict[tuple[str, str], set[int]] = {}
    for key, polys in copper.zones.items():
        main = max(range(len(polys)), key=lambda i: len(polys[i]), default=-1)  # reported by its corner: no clue
        for i, poly in enumerate(polys):
            if i != main and any(math.dist(k, v) <= PIECE_MATCH_MM for k in keep_near or [] for v in poly):
                kept_pieces.setdefault(key, set()).add(i)

    def fill_pieces_touched(s: tracks.Seg) -> set[int]:
        polys = copper.zones.get((s.net, s.layer), [])
        ends = (s.a, s.b, ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2))
        return {i for i, poly in enumerate(polys) if any(tracks.point_in_polygon(p, poly) for p in ends)}

    pruned = 0
    for s in copper.segs:
        if not copper.zones.get((s.net, s.layer)):
            continue
        if any(tracks.point_seg_distance(k, s.a, s.b) <= KEEP_RADIUS_MM for k in keep_near or []):
            continue
        if only_near is not None and not any(
            tracks.point_seg_distance(k, s.a, s.b) <= ONLY_RADIUS_MM for k in only_near
        ):
            continue
        if any(i in kept_pieces.get((s.net, s.layer), set()) for i in fill_pieces_touched(s)):
            continue
        n = max(2, math.ceil(math.dist(s.a, s.b) / POUR_SAMPLE_MM) + 1)
        samples = [
            (s.a[0] + (s.b[0] - s.a[0]) * k / (n - 1), s.a[1] + (s.b[1] - s.a[1]) * k / (n - 1)) for k in range(n)
        ]
        hits = [piece(p, s) for p in samples]
        fills = {h for h in hits if isinstance(h, int)}
        pads = {h[0] for h in hits if isinstance(h, tuple)}
        # one fill piece must hold the whole track and reach every pad it touches; a track over two
        # pieces, or to a pad whose spokes reach another piece, may be all that joins them: it stays
        if None in hits or len(fills) > 1:
            continue
        if not fills and pads:  # the track lies in pad gaps only: the pads must share a piece
            fills = set.intersection(*(pieces_at_pad(k, s) for k in pads))
        if fills and all(fills & pieces_at_pad(k, s) for k in pads):
            _drop(project.pcb, _source(s))
            pruned += 1
    changes = [f"pours: removed {pruned} track segment(s) the pour already connects"] if pruned else []
    return changes + remove_antennas(project, filled_zones=True)


def drop_folded_back_tracks(project: KicadProject) -> list[str]:
    """Drop a track that leaves a point together with a longer track of its net and runs back along it.

    Two pieces sharing an end and leaving it a few degrees apart overlap (a fold: seen on a VCC run, 4.8 mm under
    an 18 mm one, 0.44 deg apart, which KiCad reports as an acute angle). Where the shorter one's far end lies on
    the longer one's copper (KiCad connects whatever ends there to it), the shorter one is copper twice: it goes.
    """
    changes = []
    for _ in range(FOLD_MAX):
        copper = _copper(project.pcb)
        found = None
        for net, segs in copper.segs_by_net.items():
            for s in segs:
                for t in segs:
                    if t is s or t.layer != s.layer or math.dist(s.a, s.b) >= math.dist(t.a, t.b):
                        continue
                    shared = next((e for e in (s.a, s.b) for f in (t.a, t.b) if math.dist(e, f) <= tracks.EPS), None)
                    if shared is None:
                        continue
                    far_s = s.b if math.dist(s.a, shared) <= tracks.EPS else s.a
                    far_t = t.b if math.dist(t.a, shared) <= tracks.EPS else t.a
                    if (
                        tracks.interior_angle(far_s, shared, far_t) <= FOLD_MAX_DEG
                        and tracks.point_seg_distance(far_s, t.a, t.b) <= t.width / 2 - tracks.EPS
                    ):
                        found = (net, s, far_s)
                        break
                if found:
                    break
            if found:
                break
        if found is None:
            break
        net, s, far = found
        project.pcb.remove(_source(s))
        changes.append(f"{net} track folded back along a longer one at ({_mm(far[0])}, {_mm(far[1])}) removed")
    return changes


FOLD_MAX = 200


FOLD_MAX_DEG = 10.0


def split_at_tees(project: KicadProject) -> list[str]:
    """Split a track where another track of its net ends on its middle: every junction becomes a vertex.

    KiCad's ``track_angle`` check misjudges some T-junctions whose branch ends inside the other track (seen:
    an exact 90 deg T reported as 0.001 deg, open-theremin VCC); with the through-track split at the foot it
    measures the real angles. Electrically nothing changes. ``check`` runs it last (the clean-up merges
    straight runs back into one piece).
    """
    changes = []
    for _ in range(SPLIT_TEES_MAX):
        copper = _copper(project.pcb)
        found = None
        for t in copper.segs:
            for s in copper.segs_by_net.get(t.net, []):
                if s is t or s.layer != t.layer:
                    continue
                for e in (s.a, s.b):
                    if min(math.dist(e, t.a), math.dist(e, t.b)) > T_SPLIT_MIN_MM and (
                        tracks.point_seg_distance(e, t.a, t.b) <= T_ON_LINE_MM
                    ):
                        found = (t, e)
                        break
                if found:
                    break
            if found:
                break
        if found is None:
            break
        t, e = found
        node = _source(t)
        index = project.pcb.index(node)
        project.pcb[index : index + 1] = [_segment_node(node, t.a, e, t.width), _segment_node(node, e, t.b, t.width)]
        changes.append(f"{t.net} track split at the T-junction at ({_mm(e[0])}, {_mm(e[1])})")
    return changes


SPLIT_TEES_MAX = 500


T_SPLIT_MIN_MM = 0.01  # an end this close to the track's own end already meets it there


def apply_post_fill(
    project: KicadProject,
    keep_near: list[tracks.Point] | None = None,
    only_near: list[tracks.Point] | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Fixes that need KiCad's zone fill (run by ``check`` after the refill); saves when anything changed.

    ``progress`` gets one line per step (pass, step, seconds, changes): the loop can run for minutes.
    """

    def timed(label: str, step: Callable[[], list[str]]) -> list[str]:
        started = time.monotonic()
        out = step()
        if progress is not None:
            progress(f"{label}: {len(out)} change(s), {time.monotonic() - started:.0f} s")
        return out

    changes = timed("post-fill prune_pour_tracks", lambda: prune_pour_tracks(project, keep_near, only_near))
    if changes:  # the pruned GND tracks leave room for corners that had none before
        steps: list[tuple[str, Callable[[], list[str]]]] = [
            ("snap_near_ends", lambda: snap_near_ends(project)),
            ("clean_tracks", lambda: clean_tracks(project)),
            ("separate_ends_on_pads", lambda: separate_ends_on_pads(project)),
            ("square_tees", lambda: square_tees(project)),
            ("straighten_via_exits", lambda: straighten_via_exits(project)),
            ("snap_bends_to_45", lambda: snap_bends_to_45(project)),
            ("collapse_bend_stubs", lambda: collapse_bend_stubs(project)),
            ("push_tracks_clear", lambda: push_tracks_clear(project)),
            ("relocate_vias", lambda: relocate_vias(project)),
            ("reroute_tracks", lambda: reroute_tracks(project)),
            ("remove_antennas", lambda: remove_antennas(project, filled_zones=True)),
        ]
        for k in range(1, POST_FILL_PASSES + 1):  # each step can leave work for an earlier one: repeat until quiet
            second = []
            for name, step in steps:
                second += timed(f"post-fill pass {k}/{POST_FILL_PASSES} {name}", step)
            changes += [f"second pass: {c}" for c in second]
            if not second:
                break
        # left by a step after the last clean_tracks (reroute_tracks, remove_antennas)
        if loops := _drop_closed_loops(project):
            changes.append(f"tracks: removed {loops} loop(s) leaving a pad or via and coming back into it")
        if dropped := _drop_zero_length_segments(project):
            changes.append(f"tracks: removed {dropped} zero-length track segment(s)")
        sexpr.dump(project.pcb, project.pcb_path)
    return changes


POST_FILL_PASSES = 3


type Fix = Callable[[KicadProject], list[str]]
