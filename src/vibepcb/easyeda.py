"""Schematic symbols from LCSC / EasyEDA, by LCSC part number, for chips KiCad's library lacks.

EasyEDA serves every LCSC part's symbol as JSON (shapes in 10 mil units, y down); this converts
pins, rectangles, polylines, polygons, ellipses, arcs and texts into a KiCad ``(symbol ...)``,
one unit per EasyEDA sub-part (dual op-amps come as two triangles). Downloads are cached in
``~/.cache/vibepcb/easyeda``; offline, an uncached part simply has no symbol (``schgen`` draws a box).
"""

from __future__ import annotations

import json
import math
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from vibepcb import sexpr, symlib

if TYPE_CHECKING:
    from collections.abc import Iterable

API = "https://easyeda.com/api/products/{}/components?version=6.4.19.5"
CACHE = symlib.CACHE / "easyeda"
UNIT_MM = 0.254  # EasyEDA symbol unit: 10 mil
GRID = 1.27
TIMEOUT_S = 15
_LCSC = re.compile(r"^C\d+$")


def fetch(lcsc: str) -> dict[str, Any] | None:
    """The EasyEDA component JSON of an LCSC part (cached), None if unavailable."""
    if not _LCSC.match(lcsc):
        return None
    cache = CACHE / f"{lcsc}.json"
    try:
        return dict(json.loads(cache.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    request = urllib.request.Request(API.format(lcsc), headers={"User-Agent": "vibepcb"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not data.get("success") or not isinstance(data.get("result"), dict):
        return None
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return dict(data)


def fetch_all(parts: Iterable[str]) -> dict[str, dict[str, Any]]:
    """LCSC number -> component JSON for every part that could be fetched (in parallel)."""
    wanted = sorted({p for p in parts if _LCSC.match(p)})
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = dict(zip(wanted, pool.map(fetch, wanted), strict=True))
    return {k: v for k, v in found.items() if v is not None}


def _f(v: float) -> str:
    return f"{round(v, 4):g}"


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


class _Converter:
    """One EasyEDA sub-part -> KiCad graphics + pins (library frame: mm, y up)."""

    def __init__(self, ox: float, oy: float, shift: tuple[float, float]) -> None:
        self.ox, self.oy, self.shift = ox, oy, shift

    def pt(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.ox) * UNIT_MM + self.shift[0], -(y - self.oy) * UNIT_MM + self.shift[1]

    def xy(self, x: float, y: float) -> str:
        px, py = self.pt(x, y)
        return f"(xy {_f(px)} {_f(py)})"

    @staticmethod
    def stroke(fill: bool = False) -> str:
        return f"(stroke (width 0.254) (type default)) (fill (type {'outline' if fill else 'none'}))"

    def shape(self, s: str) -> str | None:
        f = s.split("~")
        kind = f[0]
        try:
            if kind == "R":  # R~x~y~rx~ry~w~h~stroke~width~style~fill
                x, y, w, h = float(f[1]), float(f[2]), float(f[5]), float(f[6])
                (x1, y1), (x2, y2) = self.pt(x, y), self.pt(x + w, y + h)
                return f"(rectangle (start {_f(x1)} {_f(y1)}) (end {_f(x2)} {_f(y2)}) {self.stroke()})"
            if kind in {"PL", "PG"}:  # PL~x y x y ...~stroke~width~style~fill
                nums = [float(n) for n in f[1].replace(",", " ").split()]
                pts = list(zip(nums[0::2], nums[1::2], strict=False))
                if kind == "PG" and pts:
                    pts.append(pts[0])
                if len(pts) < 2:
                    return None
                filled = kind == "PG" and len(f) > 5 and f[5] not in {"", "none"}
                return f"(polyline (pts {' '.join(self.xy(x, y) for x, y in pts)}) {self.stroke(filled)})"
            if kind == "E":  # E~cx~cy~rx~ry~stroke~width~style~fill
                cx, cy, r = float(f[1]), float(f[2]), float(f[3])
                px, py = self.pt(cx, cy)
                filled = len(f) > 8 and f[8] not in {"", "none"}
                return f"(circle (center {_f(px)} {_f(py)}) (radius {_f(r * UNIT_MM)}) {self.stroke(filled)})"
            if kind == "A":
                return self.arc(f[1])
            if kind == "T":  # T~L~x~y~rot~color~font~size~weight~~~type~text~visible
                if len(f) < 14 or f[13] == "0" or not f[12].strip():
                    return None
                px, py = self.pt(float(f[2]), float(f[3]))
                return f"(text {_q(f[12])} (at {_f(px)} {_f(py)} 0) (effects (font (size 1 1)) (justify left)))"
        except (ValueError, IndexError):
            return None
        return None

    def arc(self, path: str) -> str | None:
        """SVG ``M x1 y1 A rx ry rot large sweep x2 y2`` -> KiCad three-point arc."""
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", path)]
        if len(nums) < 9:
            return None
        x1, y1, rx, _, _, large, sweep, x2, y2 = nums[:9]
        dx, dy = (x2 - x1) / 2, (y2 - y1) / 2
        d = math.hypot(dx, dy)
        if d == 0 or rx == 0:
            return None
        r = max(rx, d)
        h = math.sqrt(max(r * r - d * d, 0.0))
        sign = 1 if bool(large) != bool(sweep) else -1
        mx, my = x1 + dx, y1 + dy
        cx, cy = mx - sign * h * dy / d, my + sign * h * dx / d
        a1, a2 = math.atan2(y1 - cy, x1 - cx), math.atan2(y2 - cy, x2 - cx)
        delta = a2 - a1
        if sweep and delta < 0:
            delta += 2 * math.pi
        if not sweep and delta > 0:
            delta -= 2 * math.pi
        am = a1 + delta / 2
        xm, ym = cx + r * math.cos(am), cy + r * math.sin(am)
        return f"(arc (start {self.xy(x1, y1)[4:-1]}) (mid {self.xy(xm, ym)[4:-1]}) (end {self.xy(x2, y2)[4:-1]}) {self.stroke()})"

    def pin(self, s: str) -> str | None:
        segments = s.split("^^")
        head, path = segments[0].split("~"), segments[2].split("~")[0] if len(segments) > 2 else ""
        try:
            number = head[3]
            x, y = float(head[4]), float(head[5])
        except (IndexError, ValueError):
            return None
        name = segments[3].split("~")[4] if len(segments) > 3 and len(segments[3].split("~")) > 4 else ""
        m = re.search(r"([hv])\s*(-?\d+(?:\.\d+)?)", path)
        length, angle = 2.54, 0
        if m:
            step = float(m[2])
            length = abs(step) * UNIT_MM
            # direction from the connection point into the body; EasyEDA y grows downwards
            angle = (0 if step > 0 else 180) if m[1] == "h" else (270 if step > 0 else 90)
        px, py = self.pt(x, y)
        return (
            f"(pin passive line (at {_f(px)} {_f(py)} {angle}) (length {_f(length)})"
            f" (name {_q(name or '~')} (effects (font (size 1.27 1.27))))"
            f" (number {_q(number)} (effects (font (size 1.27 1.27)))))"
        )


def _pin_xy(shape: str) -> tuple[float, float] | None:
    head = shape.split("^^", maxsplit=1)[0].split("~")
    try:
        return float(head[4]), float(head[5])
    except (IndexError, ValueError):
        return None


def to_symbol(data: dict[str, Any], name: str) -> symlib.LibSymbol | None:
    """The KiCad symbol of an EasyEDA component JSON (sub-parts become units)."""
    result = data.get("result") or {}
    units = [sp.get("dataStr") or {} for sp in result.get("subparts") or []] or [result.get("dataStr") or {}]
    subsymbols = []
    for unit, ds in enumerate(units, start=1):
        head, shapes = ds.get("head") or {}, ds.get("shape") or []
        ox, oy = float(head.get("x", 0)), float(head.get("y", 0))
        first = next((xy for s in shapes if s.startswith("P~") and (xy := _pin_xy(s))), None)
        if first is None:
            continue
        # shift so the pins land on KiCad's 1.27 mm connection grid
        fx, fy = (first[0] - ox) * UNIT_MM, -(first[1] - oy) * UNIT_MM
        shift = (round(fx / GRID) * GRID - fx, round(fy / GRID) * GRID - fy)
        conv = _Converter(ox, oy, shift)
        graphics = [g for s in shapes if not s.startswith("P~") and (g := conv.shape(s))]
        pins = [p for s in shapes if s.startswith("P~") and (p := conv.pin(s))]
        subsymbols.append(f"(symbol {_q(f'{name}_{unit}_1')} {' '.join(graphics)} {' '.join(pins)})")
    if not subsymbols:
        return None
    text = (
        f"(symbol {_q(name)} (pin_names (offset 0.508)) (exclude_from_sim no) (in_bom yes) (on_board yes)"
        ' (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))'
        f' (property "Value" {_q(name)} (at 0 0 0) (effects (font (size 1.27 1.27))))'
        ' (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))'
        ' (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))'
        f" {' '.join(subsymbols)})"
    )
    node = sexpr.loads(text)
    symbol = symlib.LibSymbol(name, node, symlib.pins_of(node))
    xmin, ymin, xmax, ymax = symbol.bbox()
    cx = (xmin + xmax) / 2
    for prop, y, edge in (("Reference", ymax + 1.27, ymax), ("Value", ymin - 1.27, ymin)):
        x = cx
        if any(abs(p.x - cx) < 2.54 and abs(p.y - edge) < 0.01 for p in symbol.pins):
            # a pin leaves the middle of that edge (op-amp supplies): the text goes beside it
            x, y = cx + 2.54 + 0.6 * len(name), edge - 2.54 if prop == "Reference" else edge + 2.54
        at = sexpr.child(sexpr.property_node(node, prop) or [], "at")
        if at is not None:
            at[1], at[2] = sexpr.Sym(_f(x)), sexpr.Sym(_f(y))
    return symbol
