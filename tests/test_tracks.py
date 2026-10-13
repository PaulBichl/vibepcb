from __future__ import annotations

import math

from vibepcb import tracks
from vibepcb.tracks import Obstacle, Seg

LAYERS = frozenset({"F.Cu"})


def seg(a: tracks.Point, b: tracks.Point, width: float = 0.2, net: str = "N") -> Seg:
    return Seg(a, b, width, "F.Cu", net)


def anywhere(a: tracks.Point, b: tracks.Point) -> bool:
    return True


def test_interior_angle() -> None:
    assert math.isclose(tracks.interior_angle((0, 0), (1, 0), (2, 0)), 180)
    assert math.isclose(tracks.interior_angle((0, 0), (1, 0), (1, 1)), 90)
    assert math.isclose(tracks.interior_angle((0, 0), (1, 0), (0, 0.001)), 0, abs_tol=0.1)


def test_simplify_drops_collinear_points_and_spikes() -> None:
    assert tracks.simplify([(0, 0), (1, 0), (2, 0), (3, 1)]) == [(0, 0), (2, 0), (3, 1)]
    # out to (3, 0) and back to (2.5, 0): the spike tip goes, then (2, 0) is on a straight run
    assert tracks.simplify([(0, 0), (2, 0), (3, 0), (2.5, 0), (2.5, 2)]) == [(0, 0), (2.5, 0), (2.5, 2)]


def test_round_off_corners_chamfers_a_right_angle() -> None:
    points = tracks.round_off_corners([(0, 0), (5, 0), (5, 5)], anywhere)
    assert points[0] == (0, 0)
    assert points[-1] == (5, 5)
    assert tracks.sharp_corners(points) == []


def test_round_off_corners_cuts_an_acute_corner_across() -> None:
    points = tracks.round_off_corners([(0, 0), (5, 0), (0, 0.5)], anywhere)
    assert tracks.sharp_corners(points) == []


def test_round_off_corners_respects_fits() -> None:
    original: list[tracks.Point] = [(0, 0), (5, 0), (5, 5)]
    assert tracks.round_off_corners(original, lambda a, b: False) == original


def test_clearance_check_and_necking() -> None:
    chain = tracks.Chain([(0, 0), (5, 0)], "F.Cu", "N", [seg((0, 0), (5, 0), width=0.4)])
    pad = Obstacle("OTHER", LAYERS, ((2, 0.5), (3, 0.5), (3, 1.5), (2, 1.5)), polygon=True)
    fits = tracks.clearance_check(chain, [pad], [], clearance=0.2, edge_clearance=0.5, min_width=0.15)
    assert not fits((0, 0.3), (5, 0.3))  # far too close, and too long to neck down
    assert fits((0, -1), (5, -1))
    assert fits((2, 0.2), (2.5, 0.2))  # short piece 0.3 mm from the pad: necks down to fit
    assert 0.15 <= chain.necked[(2, 0.2), (2.5, 0.2)] <= 0.2  # what the 0.3 mm leaves, rounded down
    assert chain.width_at((2, 0.2), (2.5, 0.2)) == chain.necked[(2, 0.2), (2.5, 0.2)]


def test_trim_ends_stops_just_inside_the_pad() -> None:
    def pad(p: tracks.Point) -> int | None:
        return 1 if -0.5 <= p[0] <= 0.5 and -0.5 <= p[1] <= 0.5 else None

    # zig-zag inside the pad, then out to (3, 0)
    points = tracks.trim_ends([(0, 0), (0.2, 0.2), (0.1, -0.1), (3, 0)], pad)
    assert points[-1] == (3, 0)
    assert len(points) == 2
    assert math.isclose(points[0][0], 0.5 - tracks.PAD_INSET_MM, abs_tol=1e-3)


def test_redundant_drops_exact_and_covered_duplicates_only() -> None:
    a = seg((0, 0), (4, 0), width=0.4)
    copy = seg((4, 0), (0, 0), width=0.2)
    assert tracks.redundant([a, copy]) == [1]
    # a narrow track inside a wide one, whose ends are met by other tracks at both sides
    wide = seg((0, 0), (4, 0), width=0.6)
    narrow = seg((1, 0.05), (3, 0.05), width=0.15)
    left, right = seg((1, 0.05), (1, 2)), seg((3, 0.05), (3, 2))
    assert tracks.redundant([wide, narrow, left, right]) == []  # left/right would hang mid-track


def test_dangling_removes_stubs_but_keeps_connected_tracks() -> None:
    main = seg((0, 0), (5, 0))
    stub = seg((5, 0), (6, 1))  # loose at (6, 1)

    def on_pad(p: tracks.Point, s: Seg) -> bool:
        return p in ((0, 0), (5, 0))

    assert tracks.dangling([main, stub], on_pad) == [1]


def test_pad_groups_follow_kicad_connection_rules() -> None:
    p1 = Obstacle("N", LAYERS, ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)), polygon=True)
    p2 = Obstacle("N", LAYERS, ((4.5, -0.5), (5.5, -0.5), (5.5, 0.5), (4.5, 0.5)), polygon=True)
    joined = tracks.pad_groups([p1, p2], [], [seg((0, 0), (5, 0))])
    assert joined == [{0, 1}]
    # a track end connects to a via while its copper overlaps the via (measured in KiCad 10:
    # 0.15 mm track, 0.3 mm via radius: connected at 0.37 mm from the centre, not at 0.39 mm)
    via = [((2.5, 1.0), 0.3)]
    assert tracks.pad_groups([p1, p2], via, [seg((0, 0), (2.5, 0.8)), seg((2.5, 1.0), (5, 0))]) == [{0, 1}]
    assert len(tracks.pad_groups([p1, p2], via, [seg((0, 0), (2.5, 0.5)), seg((2.5, 1.0), (5, 0))])) == 2
    assert tracks.pad_groups([p1, p2], via, [seg((0, 0), (2.5, 1.0)), seg((2.5, 1.0), (5, 0))]) == [{0, 1}]
    assert tracks.still_connected([{0, 1}], [{0, 1}])
    assert not tracks.still_connected([{0, 1}], [{0}, {1}])


def test_tees_and_junctions() -> None:
    through = seg((0, 0), (4, 0))
    branch = seg((2, 0.05), (2, 3))
    found = tracks.tees([through, branch])
    assert len(found) == 1
    i, end, j, q = found[0]
    assert (i, end, j) == (1, 0, 0)
    assert math.isclose(q[0], 2)
    assert math.isclose(q[1], 0)
    y = [seg((0, 0), (2, 0)), seg((2, 0), (4, 0.3)), seg((2, 0), (4, -0.3))]
    assert len(tracks.junctions(y)) == 1
    assert sorted(tracks.sharp_junctions(y)) == [1, 2]


def test_move_junction_opens_an_acute_split() -> None:
    j, others = (2.0, 0.0), [(0.0, 0.0), (4.0, 0.3), (4.0, -0.3)]
    assert tracks.min_branch_angle(j, others) < 90
    q = tracks.move_junction(j, others, anywhere)
    assert q is not None
    assert tracks.min_branch_angle(q, others) > tracks.min_branch_angle(j, others)


def test_obstacle_distance() -> None:
    via = Obstacle("V", LAYERS, ((0, 0),), radius=0.3)
    assert math.isclose(via.distance((1, -1), (1, 1)), 0.7)
    box = Obstacle("P", LAYERS, ((0, 0), (1, 0), (1, 1), (0, 1)), polygon=True)
    assert box.distance((0.5, 0.5), (0.5, 0.5)) == 0
    assert math.isclose(box.distance((2, 0), (2, 1)), 1)
    assert box.distance((10, 10), (11, 11)) > tracks.FAR_MM  # far away: lower bound only


def test_on_track_body_ignores_end_caps() -> None:
    # a 0.8 mm strap ending 0.21 mm short of a pad centre: the round cap covers the centre,
    # but KiCad does not connect them (seen on an ESP32 module's GND pads)
    assert not tracks.on_track_body((0.0, 0.0), (0.0, 0.2125), (0.0, 0.65), 0.4)
    assert tracks.on_track_body((0.0, 0.3), (0.0, 0.2125), (0.0, 0.65), 0.4)
    assert tracks.on_track_body((0.3, 0.3), (0.0, 0.2125), (0.0, 0.65), 0.4)
    assert not tracks.on_track_body((0.5, 0.3), (0.0, 0.2125), (0.0, 0.65), 0.4)


def test_pad_groups_track_end_on_pad_edge() -> None:
    p1 = Obstacle("N", LAYERS, ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)), polygon=True)
    p2 = Obstacle("N", LAYERS, ((4.5, -0.5), (5.5, -0.5), (5.5, 0.5), (4.5, 0.5)), polygon=True)
    # ends exactly on the pad edges (as tscircuit / trimming leave them, up to float noise)
    assert tracks.pad_groups([p1, p2], [], [seg((0.5000004, 0.2), (4.4999996, 0.2))]) == [{0, 1}]
