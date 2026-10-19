"""Board model for routing: the copper of a KiCad board plus its design rules, extendable in memory.

Built from a parsed project (:class:`fixes.KicadProject`) through ``fixes._copper`` (pads, tracks,
vias, zones, keepouts, outline). Routes added with :meth:`Board.add_track` / :meth:`Board.add_via`
become obstacles for later routes; the KiCad tree is not touched (``routes.apply`` writes it).
Coordinates are KiCad board millimetres (y down).
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb import fixes, sexpr, tracks

if TYPE_CHECKING:
    from vibepcb.fixes import KicadProject

DEFAULT_TRACK_WIDTH_MM = 0.2
DEFAULT_VIA_DIAMETER_MM = 0.6
DEFAULT_VIA_DRILL_MM = 0.3


@dataclass(frozen=True)
class NetClass:
    name: str
    track_width: float = DEFAULT_TRACK_WIDTH_MM
    clearance: float = fixes.DEFAULT_CLEARANCE_MM
    via_diameter: float = DEFAULT_VIA_DIAMETER_MM
    via_drill: float = DEFAULT_VIA_DRILL_MM
    diff_pair_width: float = 0.0  # 0: not set (the class's track width)
    diff_pair_gap: float = 0.0  # 0: not set (the class's clearance)


@dataclass
class Rules:
    """Design rules of a board: net classes (by net-name pattern) and the global limits."""

    default: NetClass = field(default_factory=lambda: NetClass("Default"))
    classes: dict[str, NetClass] = field(default_factory=dict)
    patterns: list[tuple[str, str]] = field(default_factory=list)  # (net-name pattern, class name), in file order
    edge_clearance: float = fixes.DEFAULT_EDGE_CLEARANCE_MM
    hole_to_hole: float = fixes.DEFAULT_HOLE_TO_HOLE_MM
    min_track_width: float = fixes.DEFAULT_MIN_TRACK_WIDTH_MM

    @classmethod
    def from_pro(cls, data: dict) -> Rules:
        """Rules from the parsed .kicad_pro (``net_settings`` classes + patterns, ``rules`` limits).

        Class values below the design-setting minimums (``fixes.apply_rule_profiles`` writes the
        fab's) are raised to them: KiCad's DRC would flag them anyway.
        """
        rules = data.get("board", {}).get("design_settings", {}).get("rules", {})

        def floor(key: str) -> float:
            return float(rules.get(key, 0.0) or 0.0)

        def net_class(c: dict) -> NetClass:
            d = NetClass("")
            drill = max(float(c.get("via_drill", d.via_drill)), floor("min_through_hole_diameter"))
            return NetClass(
                str(c.get("name", "")),
                max(float(c.get("track_width", d.track_width)), floor("min_track_width")),
                max(float(c.get("clearance", d.clearance)), floor("min_clearance")),
                max(
                    float(c.get("via_diameter", d.via_diameter)),
                    floor("min_via_diameter"),
                    round(drill + 2 * floor("min_via_annular_width"), 6),
                ),
                drill,
                float(c.get("diff_pair_width", 0.0) or 0.0),
                float(c.get("diff_pair_gap", 0.0) or 0.0),
            )

        settings = data.get("net_settings", {})
        classes = {c["name"]: net_class(c) for c in settings.get("classes", []) if "name" in c}
        return cls(
            default=classes.get("Default", NetClass("Default")),
            classes=classes,
            patterns=[(str(p["pattern"]), str(p["netclass"])) for p in settings.get("netclass_patterns", [])],
            edge_clearance=float(rules.get("min_copper_edge_clearance", fixes.DEFAULT_EDGE_CLEARANCE_MM)),
            hole_to_hole=float(rules.get("min_hole_to_hole", fixes.DEFAULT_HOLE_TO_HOLE_MM)),
            min_track_width=float(rules.get("min_track_width", fixes.DEFAULT_MIN_TRACK_WIDTH_MM)),
        )

    def class_of(self, net: str) -> NetClass:
        for pattern, name in self.patterns:
            if fnmatch.fnmatchcase(net, pattern) and name in self.classes:
                return self.classes[name]
        return self.default

    def clearance(self, net_a: str, net_b: str) -> float:
        """KiCad's rule: the larger clearance of the two nets' classes."""
        return max(self.class_of(net_a).clearance, self.class_of(net_b).clearance)


@dataclass
class Hole:
    centre: tracks.Point
    radius: float
    net: str
    routed: bool = False  # a via the router added


class Board:
    """Copper by layer with an obstacle index; tracks and vias can be added."""

    def __init__(self, copper: fixes._Copper, rules: Rules, holes: list[Hole], pcb: list[sexpr.Node]) -> None:
        self.copper = copper
        self.rules = rules
        self.holes = holes
        self.layers = copper_layers(pcb)
        self.items: list[tracks.Obstacle] = []  # everything that blocks a track of another net
        # nets that count as one: while a differential pair's centre line is routed as P, N is not in its way
        self.aliases: dict[str, frozenset[str]] = {}
        self.edges = list(copper.edges)
        self._index: tracks.BoxIndex | None = None
        self.add_items(copper.obstacles())

    @classmethod
    def from_project(cls, project: KicadProject) -> Board:
        pro = project.pcb_path.with_suffix(".kicad_pro")
        try:
            rules = Rules.from_pro(json.loads(pro.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            rules = Rules()
        copper = fixes._copper(project.pcb)
        return cls(copper, rules, _holes(project.pcb, copper), project.pcb)

    # --- obstacles ---------------------------------------------------------------

    def add_items(self, items: list[tracks.Obstacle]) -> None:
        self.items += items
        self._index = None  # rebuilt on the next query

    @property
    def index(self) -> tracks.BoxIndex:
        if self._index is None:
            self._index = tracks.BoxIndex([o.box for o in self.items])
        return self._index

    def near(self, x0: float, y0: float, x1: float, y1: float) -> list[tracks.Obstacle]:
        """Obstacles whose bounding box overlaps the box."""
        return [self.items[i] for i in sorted(self.index.overlapping(x0, y0, x1, y1))]

    def checkpoint(self) -> tuple[int, int, int, int]:
        return len(self.items), len(self.copper.segs), len(self.copper.vias), len(self.holes)

    def rollback(self, cp: tuple[int, int, int, int]) -> None:
        """Drop everything added since ``checkpoint()`` (rip-up)."""
        del self.items[cp[0] :], self.copper.segs[cp[1] :], self.copper.vias[cp[2] :], self.holes[cp[3] :]
        self._index = None

    def segs_by_net_now(self, net: str) -> list[tracks.Seg]:
        """The net's tracks as they are now (``copper.segs_by_net`` is cached at load, before any route)."""
        return [s for s in self.copper.segs if s.net == net]

    def add_track(self, net: str, layer: str, a: tracks.Point, b: tracks.Point, width: float) -> None:
        s = tracks.Seg(a, b, width, layer, net)
        self.copper.segs.append(s)
        self.add_items([tracks.Obstacle(net, frozenset({layer}), (a, b), width / 2, routed=True)])

    def add_via(self, net: str, centre: tracks.Point, diameter: float, drill: float) -> None:
        self.copper.vias.append(([], centre, diameter / 2, net))
        self.holes.append(Hole(centre, drill / 2, net, routed=True))
        self.add_items([tracks.Obstacle(net, frozenset(self.layers), (centre,), diameter / 2, routed=True)])


def copper_layers(pcb: list[sexpr.Node]) -> list[str]:
    """The board's copper layers in stack order (from its ``layers`` section)."""
    layers = sexpr.child(pcb, "layers")
    names = {str(n[1]) for n in (layers or [])[1:] if isinstance(n, list) and len(n) > 1}
    return [layer for layer in fixes.COPPER_LAYERS if layer in names]


def _holes(pcb: list[sexpr.Node], copper: fixes._Copper) -> list[Hole]:
    """Drilled holes: vias and through-hole / NPTH pads (round drill; slots by their larger side)."""
    out = [Hole(c, r_drill(v), net) for v, c, _r, net in copper.vias]
    for fp in sexpr.children(pcb, "footprint"):
        to_board = fixes._fp_to_board(fp)
        for pad in sexpr.children(fp, "pad"):
            drill = sexpr.child(pad, "drill")
            if drill is None:
                continue
            nums = [float(str(x)) for x in drill[1:] if _is_number(x)]
            if not nums or max(nums) <= 0:
                continue
            at = sexpr.child(pad, "at")
            c = to_board(fixes._num(at, 1), fixes._num(at, 2))
            out.append(Hole(c, max(nums) / 2, fixes._net_of(pad, copper.names)))
    return out


def r_drill(via: list[sexpr.Node]) -> float:
    drill = sexpr.child(via, "drill")
    return fixes._num(drill, 1) / 2 if drill is not None else DEFAULT_VIA_DRILL_MM / 2


def _is_number(x: object) -> bool:
    try:
        float(str(x))
    except ValueError:
        return False
    return True
