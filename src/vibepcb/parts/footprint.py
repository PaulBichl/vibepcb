"""Footprint: spec + standard -> ``(footprint ...)`` in KiCad 9 format (what Altium 26's importer reads).

Pads come from the IPC-7351B calculation (``ipc7351``) or, for families the standard leaves to the datasheet
(custom, THT, connectors), from the spec's ``[land]``. The rest is the standard (section numbers in
``rules/parts/<standard>.toml``):

- origin in the middle of the pads, rounded rectangle pads, mask / paste expansion 0;
- Mechanical 13 (``layers.mech13``): the body outline without pins;
- Mechanical 15 (``layers.mech15``): placement outline (pads and body + margin, on its grid), centre cross,
  the designator, pin-1 / polarity marks and the PIN1 text;
- silkscreen: body outline clipped to keep its clearance from the pads (and, at connectors, out of the space in
  front of the pins), polarity marks;
- KiCad's own views: F.Fab body + designator, F.CrtYd = the placement outline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibepcb import sexpr
from vibepcb.parts import ipc7351
from vibepcb.sexpr import Sym

if TYPE_CHECKING:
    from vibepcb.parts.spec import Spec
    from vibepcb.parts.standard import Standard
    from vibepcb.sexpr import Node

VERSION = "20241229"  # KiCad 9 footprint file format (Altium 26 imports KiCad up to 9)
GENERATOR = "vibepcb"
CROSS = 0.5  # half length of the centre cross on Mechanical 15, mm
EPS = 1e-6


@dataclass(frozen=True)
class Pad:
    number: str
    x: float
    y: float
    l: float  # noqa: E741 - size along x
    w: float
    shape: str = "roundrect"
    rotation: float = 0.0
    drill: float = 0.0

    @property
    def box(self) -> Box:
        l, w = (self.w, self.l) if round(self.rotation) % 180 == 90 else (self.l, self.w)  # noqa: E741
        return Box(self.x - l / 2, self.y - w / 2, self.x + l / 2, self.y + w / 2)


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def grow(self, d: float) -> Box:
        return Box(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)

    def union(self, other: Box) -> Box:
        return Box(min(self.x0, other.x0), min(self.y0, other.y0), max(self.x1, other.x1), max(self.y1, other.y1))

    def contains(self, x: float, y: float) -> bool:
        return self.x0 - EPS <= x <= self.x1 + EPS and self.y0 - EPS <= y <= self.y1 + EPS


type Segment = tuple[float, float, float, float]


@dataclass
class Footprint:
    name: str
    pads: list[Pad]
    body: Box  # max body outline
    placement: Box
    silk: list[Segment]
    tree: list[Node]
    notes: list[str]  # facts for the check (land source, fillets, ...)
    toe: float | None  # exposed copper beyond the termination at max part size (IPC parts)

    def text(self) -> str:
        return sexpr.dumps(self.tree) + "\n"


def _r(v: float) -> Sym:
    v = round(v, 4)
    return Sym(f"{v:.4f}".rstrip("0").rstrip(".") if v != 0 else "0")


def _snap_out(box: Box, grid: float) -> Box:
    return Box(
        math.floor(box.x0 / grid + EPS) * grid,
        math.floor(box.y0 / grid + EPS) * grid,
        math.ceil(box.x1 / grid - EPS) * grid,
        math.ceil(box.y1 / grid - EPS) * grid,
    )


def body_box(spec: Spec) -> Box:
    """The body at its maximum size, centred: chips run terminal to terminal along x, other parts' l along y."""
    pkg = spec.package
    if pkg.style == "chip":
        x, y = pkg.body_l.max, pkg.body_w.max
    else:
        x, y = pkg.body_w.max, pkg.body_l.max
    return Box(-x / 2, -y / 2, x / 2, y / 2)


def pads_of(spec: Spec, std: Standard) -> tuple[list[Pad], list[str], float | None, tuple[float, float]]:
    """The pads (centred on the pads' middle), notes for the check, the toe of the IPC calculation, and the
    shift applied (the drawing's origin, usually the body centre, moves by it too)."""
    notes: list[str] = []
    toe: float | None = None
    if spec.land is not None:
        raw = [Pad(p.number, p.x, p.y, p.l, p.w, p.shape, p.rotation, p.drill) for p in spec.land.pads]
        notes.append(f"land pattern from the datasheet, page {spec.land.page}")
    else:
        density = std.text("footprint", "density")
        pattern = ipc7351.calculate(spec.package, density, spec.kind)
        raw = [Pad(p.number, p.x, p.y, p.l, p.w) for p in pattern.pads]
        f = pattern.fillets
        notes.append(
            f"land pattern calculated: {f.source}, density {density} (toe {f.toe}, heel {f.heel}, side {f.side})"
        )
        pkg = spec.package
        span = pkg.body_l if pkg.style == "chip" else pkg.span
        if span is not None:
            toe = round((pattern.rows[0].z - span.max) / 2, 4)
    boxes = [p.box for p in raw]
    cx = (min(b.x0 for b in boxes) + max(b.x1 for b in boxes)) / 2
    cy = (min(b.y0 for b in boxes) + max(b.y1 for b in boxes)) / 2
    pads = [Pad(p.number, round(p.x - cx, 4), round(p.y - cy, 4), p.l, p.w, p.shape, p.rotation, p.drill) for p in raw]
    shape = std.text("footprint", "pad_shape")
    pads = [
        Pad(p.number, p.x, p.y, p.l, p.w, shape if p.shape == "roundrect" else p.shape, p.rotation, p.drill)
        for p in pads
    ]
    return pads, notes, toe, (-cx, -cy)


def _clip(seg: Segment, boxes: list[Box]) -> list[Segment]:
    """The parts of an axis-parallel segment outside every box."""
    x0, y0, x1, y1 = seg
    horizontal = abs(y1 - y0) < EPS
    a, b = (min(x0, x1), max(x0, x1)) if horizontal else (min(y0, y1), max(y0, y1))
    pieces = [(a, b)]
    for box in boxes:
        lo, hi = (box.x0, box.x1) if horizontal else (box.y0, box.y1)
        across = y0 if horizontal else x0
        inside = box.y0 < across < box.y1 if horizontal else box.x0 < across < box.x1
        if not inside:
            continue
        nxt = []
        for p, q in pieces:
            if hi <= p or lo >= q:
                nxt.append((p, q))
                continue
            if lo > p:
                nxt.append((p, lo))
            if hi < q:
                nxt.append((hi, q))
        pieces = nxt
    if horizontal:
        return [(p, y0, q, y0) for p, q in pieces]
    return [(x0, p, x0, q) for p, q in pieces]


def _front(pad: Pad, body: Box) -> Box:
    """The space in front of a connector pin: from the pad outward, away from the body's centre."""
    b = pad.box
    far = 1e3
    dx, dy = pad.x - (body.x0 + body.x1) / 2, pad.y - (body.y0 + body.y1) / 2
    if abs(dx) * (body.y1 - body.y0) >= abs(dy) * (body.x1 - body.x0):
        return Box(b.x0, b.y0, far, b.y1) if dx > 0 else Box(-far, b.y0, b.x1, b.y1)
    return Box(b.x0, b.y0, b.x1, far) if dy > 0 else Box(b.x0, -far, b.x1, b.y1)


def silk_outline(spec: Spec, std: Standard, pads: list[Pad], body: Box) -> list[Segment]:
    """The body outline on the silkscreen, clipped clear of the pads by the standard's clearance."""
    width = std.num("choices", "silk_width")
    keep = std.num("footprint", "silk_pad_clearance") + width / 2
    outline = body.grow(width / 2)
    edges: list[Segment] = [
        (outline.x0, outline.y0, outline.x1, outline.y0),
        (outline.x1, outline.y0, outline.x1, outline.y1),
        (outline.x1, outline.y1, outline.x0, outline.y1),
        (outline.x0, outline.y1, outline.x0, outline.y0),
    ]
    blocked = [p.box.grow(keep) for p in pads]
    if spec.kind == "connector" and not std.flag("footprint", "silk_connector_front"):
        blocked += [_front(p, body).grow(keep) for p in pads]
    segments = [s for e in edges for s in _clip(e, blocked)]
    return [s for s in segments if math.hypot(s[2] - s[0], s[3] - s[1]) >= 2 * width]


def _stroke(width: float) -> list[Node]:
    return [Sym("stroke"), [Sym("width"), _r(width)], [Sym("type"), Sym("solid")]]


def _line(seg: Segment, layer: str, width: float) -> list[Node]:
    x0, y0, x1, y1 = seg
    return [
        Sym("fp_line"),
        [Sym("start"), _r(x0), _r(y0)],
        [Sym("end"), _r(x1), _r(y1)],
        _stroke(width),
        [Sym("layer"), layer],
    ]


def _rect(box: Box, layer: str, width: float) -> list[Node]:
    return [
        Sym("fp_rect"),
        [Sym("start"), _r(box.x0), _r(box.y0)],
        [Sym("end"), _r(box.x1), _r(box.y1)],
        _stroke(width),
        [Sym("fill"), Sym("no")],
        [Sym("layer"), layer],
    ]


def _dot(x: float, y: float, radius: float, layer: str) -> list[Node]:
    """A filled dot of outer ``radius`` (circle radius / 2 + stroke width / 2)."""
    return [
        Sym("fp_circle"),
        [Sym("center"), _r(x), _r(y)],
        [Sym("end"), _r(x + radius / 2), _r(y)],
        _stroke(radius),
        [Sym("fill"), Sym("yes")],
        [Sym("layer"), layer],
    ]


def _font(size: float) -> list[Node]:
    return [Sym("effects"), [Sym("font"), [Sym("size"), _r(size), _r(size)], [Sym("thickness"), _r(size * 0.15)]]]


def _text(text: str, x: float, y: float, layer: str, size: float) -> list[Node]:
    return [Sym("fp_text"), Sym("user"), text, [Sym("at"), _r(x), _r(y), Sym("0")], [Sym("layer"), layer], _font(size)]


def _property(key: str, value: str, x: float, y: float, layer: str, size: float, *, hide: bool) -> list[Node]:
    node: list[Node] = [Sym("property"), key, value, [Sym("at"), _r(x), _r(y), Sym("0")], [Sym("layer"), layer]]
    if hide:
        node.append([Sym("hide"), Sym("yes")])
    node.append(_font(size))
    return node


def _pad(pad: Pad, std: Standard) -> list[Node]:
    tht = pad.drill > 0
    shape = pad.shape
    if tht and shape == "roundrect":
        shape = "rect" if pad.number == "1" else "circle" if abs(pad.l - pad.w) < EPS else "oval"
    node: list[Node] = [
        Sym("pad"),
        pad.number,
        Sym("thru_hole" if tht else "smd"),
        Sym(shape),
        [Sym("at"), _r(pad.x), _r(pad.y), *([_r(pad.rotation)] if pad.rotation else [])],
        [Sym("size"), _r(pad.l), _r(pad.w)],
    ]
    if tht:
        node.append([Sym("drill"), _r(pad.drill)])
        node.append([Sym("layers"), "*.Cu", "*.Mask"])
    else:
        node.append([Sym("layers"), "F.Cu", "F.Mask", "F.Paste"])
    if shape == "roundrect":
        radius = min(min(pad.l, pad.w) * std.num("choices", "pad_corner_ratio"), std.num("choices", "pad_corner_max"))
        node.append([Sym("roundrect_rratio"), _r(radius / min(pad.l, pad.w))])
    node.append([Sym("solder_mask_margin"), _r(std.num("footprint", "mask_expansion"))])
    if not tht:
        node.append([Sym("solder_paste_margin"), _r(std.num("footprint", "paste_expansion"))])
    return node


def polarity_pad(spec: Spec, pads: list[Pad]) -> tuple[Pad | None, str]:
    """The pad to mark and how: pin 1 (ICs, connectors), the cathode (diodes, LEDs), + (polar capacitors)."""
    by_number = {p.number: p for p in pads}
    if spec.kind in ("diode", "led"):
        for pin in spec.pins:
            if pin.name.upper() in ("K", "C", "CATHODE", "KA") and pin.number in by_number:
                return by_number[pin.number], "cathode"
        return by_number.get("1"), "cathode"
    if spec.kind == "polar_capacitor":
        for pin in spec.pins:
            if pin.name in ("+", "A", "ANODE", "POS") and pin.number in by_number:
                return by_number[pin.number], "plus"
        return by_number.get("1"), "plus"
    if spec.kind in ("resistor", "capacitor", "inductor", "crystal") and len(pads) <= 2:
        return None, ""
    return by_number.get("1"), "pin1"


def build(spec: Spec, std: Standard, name: str | None = None) -> Footprint:
    pads, notes, toe, (dx, dy) = pads_of(spec, std)
    body = body_box(spec)
    body = Box(body.x0 + dx, body.y0 + dy, body.x1 + dx, body.y1 + dy)
    if spec.land is None:
        name = name or ipc7351.name(spec.package, std.text("footprint", "density"), spec.kind)
    else:
        name = name or spec.land.name
    name = name or f"{spec.package.family.upper()}_{spec.mpn}"
    mech13 = std.text("layers", "mech13")
    mech15 = std.text("layers", "mech15")
    mech_w = std.num("choices", "mech_width")
    silk_w = std.num("choices", "silk_width")
    margin = std.num("choices", "placement_margin")
    lo, hi = std.value("footprint", "placement_margin")
    margin = min(max(margin, lo), hi)
    extent = body
    for p in pads:
        extent = extent.union(p.box)
    placement = _snap_out(extent.grow(margin), std.num("footprint", "placement_grid"))
    silk = silk_outline(spec, std, pads, body)
    # the designator (up to 5 characters, ~0.8 x size each) fits inside the body
    text_size = min(std.num("choices", "assembly_text"), (body.x1 - body.x0) / 5, (body.y1 - body.y0) * 0.5)
    text_size = max(round(text_size, 2), 0.3)

    tree: list[Node] = [
        Sym("footprint"),
        name,
        [Sym("version"), Sym(VERSION)],
        [Sym("generator"), GENERATOR],
        [Sym("generator_version"), "1"],
        [Sym("layer"), "F.Cu"],
        [Sym("descr"), f"{spec.package.family} {spec.package.pins} pins; {notes[0]}; {spec.datasheet}"],
        [Sym("tags"), f"{spec.package.family} {name}"],
        _property("Reference", "REF**", 0, 0, mech15, text_size, hide=False),
        _property("Value", name, 0, placement.y1 + 1, "F.Fab", 1.0, hide=True),
        [Sym("attr"), Sym("through_hole" if any(p.drill for p in pads) else "smd")],
    ]
    # Mechanical 13: body outline
    tree.append(_rect(body, mech13, mech_w))
    # Mechanical 15: placement outline, centre cross, polarity, PIN1
    tree.append(_rect(placement, mech15, mech_w))
    if std.flag("footprint", "centre_cross"):
        cross = min(CROSS, (placement.x1 - placement.x0) / 4, (placement.y1 - placement.y0) / 4)
        tree.append(_line((-cross, 0, cross, 0), mech15, mech_w))
        tree.append(_line((0, -cross, 0, cross), mech15, mech_w))
    mark, how = polarity_pad(spec, pads)
    if mark is not None:
        _polarity(tree, std, mark, how, body, pads, mech15, silk_w, text_size)
    # KiCad's own views
    tree.append(_rect(body, "F.Fab", mech_w))
    tree.append(_text("${REFERENCE}", 0, 0, "F.Fab", text_size))
    tree.append(_rect(placement, "F.CrtYd", std.num("choices", "courtyard_width")))
    tree += [_line(s, "F.SilkS", silk_w) for s in silk]
    tree += [_pad(p, std) for p in pads]
    tree.append(
        [
            Sym("model"),
            f"${{KIPRJMOD}}/{name}.step",
            [Sym("offset"), [Sym("xyz"), Sym("0"), Sym("0"), Sym("0")]],
            [Sym("scale"), [Sym("xyz"), Sym("1"), Sym("1"), Sym("1")]],
            [Sym("rotate"), [Sym("xyz"), Sym("0"), Sym("0"), Sym("0")]],
        ]
    )
    return Footprint(name, pads, body, placement, silk, tree, notes, toe)


def _polarity(
    tree: list[Node],
    std: Standard,
    mark: Pad,
    how: str,
    body: Box,
    pads: list[Pad],
    mech15: str,
    silk_w: float,
    designator: float,
) -> None:
    """Polarity on Mechanical 15 (inside the body) and on the silkscreen (outside, clear of the pads)."""
    mech_w = std.num("choices", "mech_width")
    keep = std.num("footprint", "silk_pad_clearance")
    left = mark.x <= 0
    if std.flag("footprint", "assembly_polarity"):
        if how == "cathode":  # a bar across the body on the cathode side
            x = body.x0 + (body.x1 - body.x0) * 0.2 if left else body.x1 - (body.x1 - body.x0) * 0.2
            tree.append(_line((x, body.y0, x, body.y1), mech15, mech_w * 2))
        elif how == "plus":
            x = body.x0 + (body.x1 - body.x0) * 0.25 if left else body.x1 - (body.x1 - body.x0) * 0.25
            s = min(0.3, (body.y1 - body.y0) / 4)
            tree.append(_line((x - s, 0, x + s, 0), mech15, mech_w))
            tree.append(_line((x, -s, x, s), mech15, mech_w))
        else:
            # a dot in the body corner nearest pin 1, the PIN1 text beside it if it clears the designator
            r = min(0.2, (body.x1 - body.x0) / 10, (body.y1 - body.y0) / 10)
            cx, cy = (body.x0 + body.x1) / 2, (body.y0 + body.y1) / 2
            px = body.x0 + 1.5 * r if mark.x <= cx else body.x1 - 1.5 * r
            py = body.y0 + 1.5 * r if mark.y <= cy else body.y1 - 1.5 * r
            tree.append(_dot(px, py, r, mech15))
            if text := std.text("footprint", "pin1_text"):
                size = min(0.4, (body.y1 - body.y0) / 6)
                width = size * 0.8 * len(text)
                tx = px + 1.5 * r + width / 2 if px < cx else px - 1.5 * r - width / 2
                fits = body.x0 + 0.05 < tx - width / 2 and tx + width / 2 < body.x1 - 0.05
                clear = abs(py - cy) - size / 2 > designator / 2 + 0.1
                if fits and clear:
                    tree.append(_text(text, tx, py, mech15, size))
    if not std.flag("footprint", "silk_polarity"):
        return
    # silkscreen: a dot just outside pad 1's outer corner (ICs, connectors); a bar / + outside the marked pad
    boxes = [p.box for p in pads]
    r = max(silk_w, 0.15)
    b = mark.box
    if how == "pin1":
        candidates = [
            (b.x0 - keep - r - silk_w, b.y0 - keep - r) if left else (b.x1 + keep + r + silk_w, b.y0 - keep - r),
            (b.x0 - keep - r - silk_w, mark.y),
            (mark.x, b.y0 - keep - r - silk_w),
        ]
    else:
        off = keep + r + silk_w
        candidates = [(b.x0 - off, mark.y) if left else (b.x1 + off, mark.y)]
    for cx, cy in candidates:
        if all(not box.grow(keep + r).contains(cx, cy) for box in boxes):
            if how == "pin1":
                tree.append(_dot(cx, cy, r / 2, "F.SilkS"))
            elif how == "cathode":
                h = b.y1 - b.y0
                tree.append(_line((cx, mark.y - h / 2, cx, mark.y + h / 2), "F.SilkS", silk_w))
            else:
                s = 0.3
                tree.append(_line((cx - s, cy, cx + s, cy), "F.SilkS", silk_w))
                tree.append(_line((cx, cy - s, cx, cy + s), "F.SilkS", silk_w))
            return
