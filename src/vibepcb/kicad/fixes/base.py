"""Shared ground of the fixes: the project, tree helpers, design-rule defaults, geometry helpers."""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibepcb.core.sexpr import Node
    from vibepcb.routing import tracks


STANDARD_BOARD_THICKNESS_MM = 1.6


FOOTPRINT_LIB_NICKNAME = "tscircuit"  # what tscircuit writes into (footprint "tscircuit:<name>")


log = logging.getLogger(__name__)


@dataclass
class KicadProject:
    """The parsed .kicad_pcb / .kicad_sch of one project directory."""

    directory: Path
    pcb: list[Node]
    sch: list[Node]
    pcb_path: Path
    sch_path: Path
    before_track_fixes: list[Node] | None = None  # board copy taken by apply_all (see restore_nets)
    config: dict[str, Any] = field(default_factory=dict)  # boards/<board>/vibepcb.toml (see vibepcb.config)
    routes: dict[str, Any] = field(default_factory=dict)  # boards/<board>/routes.toml (see vibepcb.routes)
    # reroute_tracks: run key -> fingerprint of the copper around it when no route was found (see there)
    reroute_failed: dict[tuple[Any, ...], tuple[Any, ...]] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path, stem: str) -> KicadProject:
        pcb_path = directory / f"{stem}.kicad_pcb"
        sch_path = directory / f"{stem}.kicad_sch"
        return cls(directory, sexpr.load(pcb_path), sexpr.load(sch_path), pcb_path, sch_path)

    def save(self) -> None:
        sexpr.dump(self.pcb, self.pcb_path)
        sexpr.dump(self.sch, self.sch_path)


def _footprints_by_ref(pcb: list[Node]) -> dict[str, list[Node]]:
    refs = {}
    for fp in sexpr.children(pcb, "footprint"):
        ref = sexpr.property_value(fp, "Reference")
        if ref:
            refs[ref] = fp
    return refs


_AUTO_NET = re.compile(r"^\.|\s>\s|\sto\s")  # e.g. ".R1 > .pin2 to .D1 > .anode"


def _raw_circuit(project: KicadProject) -> Path:
    """tscircuit's circuit JSON of the board: ``out/<board>/.work/raw/`` next to ``kicad/``."""
    stem = project.pcb_path.stem
    work = project.directory.parent / ".work" / "raw" / f"{stem}.circuit.json"
    return work if work.exists() else project.directory.parent / "raw" / f"{stem}.circuit.json"


def _num(node: list[Node] | None, i: int, default: float = 0.0) -> float:
    return float(str(node[i])) if node is not None and len(node) > i else default


def _pad_box(pad: list[Node]) -> tuple[float, float, float, float]:
    """Pad copper extent ``(xmin, ymin, xmax, ymax)`` in the pad's own frame (custom shapes included)."""
    size = sexpr.child(pad, "size")
    hw, hh = _num(size, 1) / 2, _num(size, 2) / 2
    box = [-hw, -hh, hw, hh]
    primitives = sexpr.child(pad, "primitives")
    for xy in [] if primitives is None else (n for n in sexpr.walk(primitives) if sexpr.head(n) == "xy"):
        x, y = _num(xy, 1), _num(xy, 2)
        box = [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
    return box[0], box[1], box[2], box[3]


def _inside_pad(fp: list[Node], pad: list[Node], x: float, y: float) -> bool:
    """Whether board point ``(x, y)`` lies within ``pad``'s copper.

    Circle and oval pads are tested by their shape (a diagonal track leaving a round THT pad
    is still inside its bounding box), every other pad by its bounding box.
    """
    at, pad_at = sexpr.child(fp, "at"), sexpr.child(pad, "at")
    fp_angle = math.radians(_num(at, 3))
    dx, dy = x - _num(at, 1), y - _num(at, 2)
    # board -> footprint frame (inverse of KiCad's y-down, counter-clockwise rotation)
    lx = dx * math.cos(fp_angle) - dy * math.sin(fp_angle) - _num(pad_at, 1)
    ly = dx * math.sin(fp_angle) + dy * math.cos(fp_angle) - _num(pad_at, 2)
    # footprint -> pad frame; a pad's angle is stored absolute, footprint rotation included
    pad_angle = math.radians(_num(pad_at, 3)) - fp_angle
    u = lx * math.cos(pad_angle) - ly * math.sin(pad_angle)
    v = lx * math.sin(pad_angle) + ly * math.cos(pad_angle)
    if str(pad[3]) in ("circle", "oval") and sexpr.child(pad, "primitives") is None:
        size = sexpr.child(pad, "size")
        hw, hh = _num(size, 1) / 2, _num(size, 2) / 2
        r = min(hw, hh)  # oval = stadium: distance to its centre line (a circle's is a point)
        return math.hypot(max(abs(u) - (hw - r), 0.0), max(abs(v) - (hh - r), 0.0)) <= r
    xmin, ymin, xmax, ymax = _pad_box(pad)
    return xmin <= u <= xmax and ymin <= v <= ymax


def _pad_on_layer(pad: list[Node], layer: str) -> bool:
    layers = sexpr.child(pad, "layers")
    names = [] if layers is None else [str(x) for x in layers[1:]]
    return layer in names or (layer.endswith(".Cu") and "*.Cu" in names)


def _mm(value: float) -> str:
    """Coordinate with KiCad's 0.1 um file precision (``:g`` would round 116.0675 to 116.067)."""
    return f"{value:.4f}".rstrip("0").rstrip(".")


type Box = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax (board, y down)


def _fp_to_board(fp: list[Node]) -> Callable[[float, float], tuple[float, float]]:
    at = sexpr.child(fp, "at")
    fx, fy, angle = _num(at, 1), _num(at, 2), math.radians(_num(at, 3))
    cos, sin = math.cos(angle), math.sin(angle)
    return lambda px, py: (fx + px * cos + py * sin, fy - px * sin + py * cos)


def _bbox(points: list[tuple[float, float]], grow: float = 0.0) -> Box:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _layer(node: list[Node]) -> str:
    layer = sexpr.child(node, "layer")
    return "" if layer is None else str(layer[1])


def _natural_key(ref: str) -> tuple[str, int]:
    m = re.match(r"(\D*)(\d*)", ref)
    return (m[1], int(m[2] or 0)) if m else (ref, 0)


# every copper layer KiCad knows: "*.Cu" pads, vias and the board edge reach the inner layers
# of 4+ layer boards too (a layer that does not exist simply carries no copper)
COPPER_LAYERS = ("F.Cu", *(f"In{i}.Cu" for i in range(1, 31)), "B.Cu")


DEFAULT_CLEARANCE_MM = 0.2


DEFAULT_EDGE_CLEARANCE_MM = 0.5


def _net_names(pcb: list[Node]) -> dict[str, str]:
    """Net number -> name (tscircuit's export references nets by number)."""
    return {str(n[1]): str(n[2]) for n in sexpr.children(pcb, "net") if len(n) > 2}


def _net_of(node: list[Node], names: dict[str, str]) -> str:
    """A node's net name, whether written as ``(net 3)``, ``(net 3 "GND")`` or ``(net "GND")``."""
    net = sexpr.child(node, "net")
    if net is None or len(net) < 2:
        return ""
    last = net[-1]
    return names.get(str(last), "") if isinstance(last, Sym) else str(last)


def _copper_layers(node: list[Node]) -> frozenset[str]:
    layers = sexpr.child(node, "layers")
    names = {str(x) for x in (layers or [])[1:]} | ({_layer(node)} if sexpr.child(node, "layer") is not None else set())
    if "*.Cu" in names or "F&B.Cu" in names:
        return frozenset(COPPER_LAYERS)
    return frozenset(layer for layer in COPPER_LAYERS if layer in names)


DEFAULT_MIN_TRACK_WIDTH_MM = 0.15


def _design_min_track_width(project: KicadProject) -> float:
    """The board's minimum track width (design rules), the floor for necking tracks down."""
    try:
        data = json.loads(project.pcb_path.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_MIN_TRACK_WIDTH_MM
    rules = data.get("board", {}).get("design_settings", {}).get("rules", {})
    return float(rules.get("min_track_width", DEFAULT_MIN_TRACK_WIDTH_MM))


def _design_clearances(project: KicadProject) -> tuple[float, float]:
    """(copper clearance, copper-to-edge clearance) from the project's default net class / rules."""
    pro = project.pcb_path.with_suffix(".kicad_pro")
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_CLEARANCE_MM, DEFAULT_EDGE_CLEARANCE_MM
    classes = data.get("net_settings", {}).get("classes", [])
    clearance = float(classes[0].get("clearance", DEFAULT_CLEARANCE_MM)) if classes else DEFAULT_CLEARANCE_MM
    rules = data.get("board", {}).get("design_settings", {}).get("rules", {})
    return clearance, float(rules.get("min_copper_edge_clearance", DEFAULT_EDGE_CLEARANCE_MM))


def _segment_node(template: list[Node], a: tracks.Point, b: tracks.Point, width: float) -> list[Node]:
    node = copy.deepcopy(template)
    for key, values in (("start", a), ("end", b), ("width", (width,))):
        child = sexpr.child(node, key)
        if child is not None:
            child[1:] = [Sym(_mm(v)) for v in values]
    uid = sexpr.child(node, "uuid")
    if uid is not None:  # derived from the content: the same board always gets the same file
        new = str(
            uuid.uuid5(uuid.NAMESPACE_OID, f"{uid[1]}:{_mm(a[0])},{_mm(a[1])}:{_mm(b[0])},{_mm(b[1])}:{_mm(width)}")
        )
        uid[1] = Sym(new) if isinstance(uid[1], Sym) else new
    return node


REROUTE_STUB_MM = (0.15, 0.3)  # off-grid first piece of a reroute where no grid heading may leave its start


REROUTE_STUB_ANGLE_STEP_DEG = 5


REROUTE_STUB_SAMPLE_DEG = 0.1


REROUTE_STUB_TRIES = 12


REROUTE_STEP_MM = 0.05  # grid of the local maze router


REROUTE_WINDOWS_MM = (2.0, 4.0, 6.0)  # it may leave the old route's bounding box by this much (tried in turn)


REROUTE_MAX_PATH_MM = 25.0  # longest stretch one reroute replaces


REROUTE_GROW_PIECES = (1, 3, 6, 1000)  # pieces kept around the bad stretch, then the whole run


REROUTE_GRID_MARGIN_MM = 0.01  # grid nodes keep this extra distance: pieces between them stay clear


TRACK_NODES = ("segment", "arc", "via")


def _net_node(pcb: list[Node], net: str) -> list[Node] | None:
    """A ``(net ...)`` node of ``net`` as the board writes it on tracks and vias.

    KiCad 9 files give tracks and vias ``(net <id>)`` and pads ``(net <id> "<name>")``; a pad's
    form on a via crashes kicad-cli's DRC (seen with KiCad 10). KiCad 10 saves ``(net "<name>")``
    everywhere. A track's or via's node is taken as it is, a pad's is cut down to its id.
    """
    names = _net_names(pcb)
    pad_node = None
    for node in sexpr.walk(pcb):
        if sexpr.head(node) in ("pad", "segment", "via") and isinstance(node, list):
            child = sexpr.child(node, "net")
            if child is None or _net_of(node, names) != net:
                continue
            if sexpr.head(node) != "pad":
                return child
            pad_node = pad_node or child
    if pad_node is not None and len(pad_node) > 2 and isinstance(pad_node[1], Sym):
        return pad_node[:2]  # (net <id> "<name>") -> (net <id>)
    return pad_node


def _design_via(project: KicadProject) -> tuple[float, float]:
    """(via diameter, drill) of the Default net class."""
    try:
        data = json.loads(project.pcb_path.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
        default = next(c for c in data["net_settings"]["classes"] if c.get("name") == "Default")
        return float(default["via_diameter"]), float(default["via_drill"])
    except (OSError, ValueError, KeyError, StopIteration):
        return DEFAULT_VIA_MM


DEFAULT_VIA_MM = (0.6, 0.3)
