"""Board stack-up and closed-form trace impedance: track widths for a target impedance.

The stack-up comes from ``boards/<board>/vibepcb.toml``::

    [stackup]
    preset = "jlc04161h-7628"      # one of PRESETS, or explicit layers (top to bottom):
    # layers = [{name = "F.Cu", copper = 0.035}, {dielectric = 0.2104, er = 4.4},
    #           {name = "In1.Cu", copper = 0.0152}, ...]

    [netclass.USB]
    nets = ["USB_D*"]
    impedance = 50                 # ohm single-ended -> track_width
    impedance_diff = 90            # ohm differential -> diff_pair_width / diff_pair_gap
    layer = "F.Cu"                 # optional routing layer (default F.Cu, the outer layer)

Geometry: an outer layer is a microstrip over the nearest copper layer (taken as its reference
plane); an inner layer is a stripline between the copper layers above and below (planes). Only
the dielectrics between the trace and those planes count (their thickness-weighted er).

Formulas (all lengths in mm, the ratios are unit-free):

* microstrip Z0: Hammerstad & Jensen (1980), with their finite-thickness correction, as given in
  Wadell, *Transmission Line Design Handbook* (1991) sec. 3.3 - about 1 % against a field solver
  for 0.1 < w/h < 10 (bare trace: no solder mask);
* stripline Z0: Wheeler (1978) with finite thickness, Wadell sec. 3.5 - about 0.5 % for a centred
  trace; an off-centre trace (h1 != h2) uses the parallel approximation
  ``Z = 2 Z(b1) Z(b2) / (Z(b1) + Z(b2))`` with ``b_i = 2 h_i + t`` (Cohn's offset-stripline
  approximation, a few %);
* edge-coupled differential: IPC-2141A's coupling factors on top of the single-ended Z0
  (the National Semiconductor AN-905 form): microstrip ``Zdiff = 2 Z0 (1 - 0.48 exp(-0.96 s/h))``,
  stripline ``Zdiff = 2 Z0 (1 - 0.347 exp(-2.9 s/b))`` (b = plane spacing) - these are the
  weakest part, expect a few % (up to ~5-8 % for very tight or very loose gaps).

Expected error against a 2D field solver (Polar SI9000, the fab's calculator): a few percent
single-ended, more for differential pairs. Solder mask over a microstrip lowers its impedance by
roughly 2-4 % (not modelled: the solved width is a bare-trace width, so the coated trace ends up
a few % under target; the fab's calculator, which models the mask, gives a slightly narrower width). Etch trapezoid, resin content and the real er at your frequency move the result too. **The
fab's own impedance calculator (and an impedance-controlled order) is the final word**: use these
numbers to set up the design rules, then confirm the width/gap with the fab before ordering.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

DEFAULT_LAYER = "F.Cu"
ROUND_MM = 3  # solved widths/gaps are rounded to 1 um

# JLCPCB stack-ups, top to bottom (copper thickness / dielectric thickness in mm, er).
# Source: https://jlcpcb.com/impedance ("PCB Stack-up" tables), read 2026-10-19. JLC lists one
# er per prepreg type (7628: 4.4, 3313: 4.1, 2116: 4.16, 1080: 3.91) and 4.6 for every core,
# without saying at which frequency; finished outer copper 1 oz = 0.035, inner 0.5 oz = 0.0152.
# The 6-layer tables add up to ~1.55 mm, not 1.6 mm: values copied as published, not adjusted.
_JLC_OUTER, _JLC_INNER = 0.035, 0.0152
PRESETS: dict[str, list[dict[str, Any]]] = {
    # JLC04161H-7628: 4 layers, 1.6 mm, 7628 prepreg (JLC's default 4-layer stack-up)
    "jlc04161h-7628": [
        {"name": "F.Cu", "copper": _JLC_OUTER},
        {"dielectric": 0.2104, "er": 4.4},  # prepreg 7628 x1
        {"name": "In1.Cu", "copper": _JLC_INNER},
        {"dielectric": 1.065, "er": 4.6},  # core
        {"name": "In2.Cu", "copper": _JLC_INNER},
        {"dielectric": 0.2104, "er": 4.4},  # prepreg 7628 x1
        {"name": "B.Cu", "copper": _JLC_OUTER},
    ],
    # JLC06161H-7628: 6 layers, 1.6 mm, 7628 prepreg
    "jlc06161h-7628": [
        {"name": "F.Cu", "copper": _JLC_OUTER},
        {"dielectric": 0.2104, "er": 4.4},  # prepreg 7628 x1
        {"name": "In1.Cu", "copper": _JLC_INNER},
        {"dielectric": 0.4, "er": 4.6},  # core
        {"name": "In2.Cu", "copper": _JLC_INNER},
        {"dielectric": 0.2028, "er": 4.4},  # prepreg 7628 x1
        {"name": "In3.Cu", "copper": _JLC_INNER},
        {"dielectric": 0.4, "er": 4.6},  # core
        {"name": "In4.Cu", "copper": _JLC_INNER},
        {"dielectric": 0.2104, "er": 4.4},  # prepreg 7628 x1
        {"name": "B.Cu", "copper": _JLC_OUTER},
    ],
    # JLC06161H-3313: 6 layers, 1.6 mm, 3313 prepreg outside, 2116 in the middle (thin outer
    # dielectric: narrower 50 ohm tracks, which suits fine-pitch parts)
    "jlc06161h-3313": [
        {"name": "F.Cu", "copper": _JLC_OUTER},
        {"dielectric": 0.0994, "er": 4.1},  # prepreg 3313 x1
        {"name": "In1.Cu", "copper": _JLC_INNER},
        {"dielectric": 0.55, "er": 4.6},  # core
        {"name": "In2.Cu", "copper": _JLC_INNER},
        {"dielectric": 0.1088, "er": 4.16},  # prepreg 2116 x1
        {"name": "In3.Cu", "copper": _JLC_INNER},
        {"dielectric": 0.55, "er": 4.6},  # core
        {"name": "In4.Cu", "copper": _JLC_INNER},
        {"dielectric": 0.0994, "er": 4.1},  # prepreg 3313 x1
        {"name": "B.Cu", "copper": _JLC_OUTER},
    ],
}


class StackupError(ValueError):
    """A malformed stack-up, an unknown layer or an impedance the geometry can't reach."""


# ---------------------------------------------------------------- formulas


def microstrip_z0(w: float, h: float, t: float, er: float) -> float:
    """Microstrip Z0 (ohm): width w over a plane at h through er, copper thickness t.

    Hammerstad & Jensen, "Accurate models for microstrip computer-aided design" (1980), with
    the thickness correction (Wadell 1991, eqs. 3.3.1-3.3.9 form): ~1 % for 0.1 < w/h < 10.
    """
    u = w / h

    def z_air(x: float) -> float:
        f = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / x) ** 0.7528))
        return 60 * math.log(f / x + math.sqrt(1 + 4 / x**2))

    def e_eff(x: float) -> float:
        a = 1 + math.log((x**4 + (x / 52) ** 2) / (x**4 + 0.432)) / 49 + math.log(1 + (x / 18.1) ** 3) / 18.7
        b = 0.564 * math.pow((er - 0.9) / (er + 3), 0.053)
        return (er + 1) / 2 + (er - 1) / 2 * math.pow(1 + 10 / x, -a * b)

    if t > 0:
        t1 = t / h
        coth2 = 1 / math.tanh(math.sqrt(6.517 * u)) ** 2
        du1 = t1 / math.pi * math.log(1 + 4 * math.e / (t1 * coth2))
        dur = (1 + 1 / math.cosh(math.sqrt(er - 1))) / 2 * du1
    else:
        du1 = dur = 0.0
    u1, ur = u + du1, u + dur
    eff = e_eff(ur) * (z_air(u1) / z_air(ur)) ** 2
    return z_air(ur) / math.sqrt(eff)


def _stripline_centred(w: float, b: float, t: float, er: float) -> float:
    """Centred stripline (planes b apart): Wheeler (1978), Wadell sec. 3.5.1, ~0.5 %."""
    if t > 0:
        x = t / b
        m = 2 / (1 + 2 / 3 * x / (1 - x))
        dw = x / (math.pi * (1 - x)) * (1 - 0.5 * math.log((x / (2 - x)) ** 2 + (0.0796 * x / (w / b + 1.1 * x)) ** m))
    else:
        dw = 0.0
    k = 1 / (w / (b - t) + dw)  # (b - t) / W'
    p = 8 / math.pi * k
    return 30 / math.sqrt(er) * math.log(1 + 4 / math.pi * k * (p + math.sqrt(p**2 + 6.27)))


def stripline_z0(w: float, h1: float, h2: float, t: float, er: float) -> float:
    """Stripline Z0 (ohm): dielectric h1 above, h2 below the trace (planes h1 + t + h2 apart).

    Centred (h1 == h2): Wheeler. Off-centre: parallel combination of the two centred lines with
    the trace's distances to each plane (Cohn's offset approximation, a few %).
    """
    if math.isclose(h1, h2):
        return _stripline_centred(w, h1 + h2 + t, t, er)
    z1, z2 = _stripline_centred(w, 2 * h1 + t, t, er), _stripline_centred(w, 2 * h2 + t, t, er)
    return 2 * z1 * z2 / (z1 + z2)


def microstrip_zdiff(w: float, s: float, h: float, t: float, er: float) -> float:
    """Edge-coupled microstrip, gap s: IPC-2141A ``2 Z0 (1 - 0.48 exp(-0.96 s / h))``."""
    return 2 * microstrip_z0(w, h, t, er) * (1 - 0.48 * math.exp(-0.96 * s / h))


def stripline_zdiff(w: float, s: float, h1: float, h2: float, t: float, er: float) -> float:
    """Edge-coupled stripline, gap s: IPC-2141A ``2 Z0 (1 - 0.347 exp(-2.9 s / b))``, b = plane spacing."""
    b = h1 + h2 + t
    return 2 * stripline_z0(w, h1, h2, t, er) * (1 - 0.347 * math.exp(-2.9 * s / b))


# ---------------------------------------------------------------- stack-up


@dataclass(frozen=True)
class Geometry:
    """A routing layer's cross-section: dielectric above (h1, None for a microstrip) and below."""

    layer: str
    t: float
    h1: float | None
    h2: float
    er: float

    @property
    def kind(self) -> str:
        return "microstrip" if self.h1 is None else "stripline"

    def z0(self, w: float) -> float:
        if self.h1 is None:
            return microstrip_z0(w, self.h2, self.t, self.er)
        return stripline_z0(w, self.h1, self.h2, self.t, self.er)

    def zdiff(self, w: float, s: float) -> float:
        if self.h1 is None:
            return microstrip_zdiff(w, s, self.h2, self.t, self.er)
        return stripline_zdiff(w, s, self.h1, self.h2, self.t, self.er)


@dataclass(frozen=True)
class Stackup:
    """Copper layers (name, thickness) and dielectrics (thickness, er), top to bottom."""

    name: str
    layers: tuple[tuple[str | None, float, float], ...]  # (copper name | None, thickness, er)

    @property
    def copper(self) -> list[str]:
        return [n for n, _, _ in self.layers if n is not None]

    def geometry(self, layer: str) -> Geometry:
        names = [n for n, _, _ in self.layers]
        if layer not in names:
            msg = f"stack-up {self.name}: no copper layer {layer!r} (has {', '.join(self.copper)})"
            raise StackupError(msg)
        i = names.index(layer)
        t = self.layers[i][1]
        above, below = self._dielectrics(i, -1), self._dielectrics(i, 1)
        if above and below:
            h1, h2 = sum(d for d, _ in above), sum(d for d, _ in below)
            er = sum(d * e for d, e in above + below) / (h1 + h2)
            return Geometry(layer, t, h1, h2, er)
        side = below or above
        if not side:
            msg = f"stack-up {self.name}: {layer} has no reference plane"
            raise StackupError(msg)
        h = sum(d for d, _ in side)
        return Geometry(layer, t, None, h, sum(d * e for d, e in side) / h)

    def _dielectrics(self, i: int, step: int) -> list[tuple[float, float]]:
        """The dielectrics from copper layer i to the next copper layer in direction step ([] at the edge)."""
        out: list[tuple[float, float]] = []
        i += step
        while 0 <= i < len(self.layers):
            name, d, e = self.layers[i]
            if name is not None:
                return out
            out.append((d, e))
            i += step
        return []


def from_config(section: Mapping[str, Any]) -> Stackup:
    """The ``[stackup]`` section: ``preset = "..."`` or ``layers = [...]`` (copper first and last)."""
    if ("preset" in section) == ("layers" in section):
        msg = f"[stackup] needs either preset = one of {sorted(PRESETS)} or layers = [...]"
        raise StackupError(msg)
    if "preset" in section:
        name = str(section["preset"]).lower()
        if name not in PRESETS:
            msg = f"[stackup] unknown preset {section['preset']!r} (known: {sorted(PRESETS)})"
            raise StackupError(msg)
        items, label = PRESETS[name], name
    else:
        items, label = section["layers"], "[stackup] layers"
    layers: list[tuple[str | None, float, float]] = []
    for item in items:
        if "copper" in item and isinstance(item.get("name"), str) and set(item) == {"name", "copper"}:
            layers.append((item["name"], float(item["copper"]), 0.0))
        elif set(item) == {"dielectric", "er"}:
            layers.append((None, float(item["dielectric"]), float(item["er"])))
        else:
            msg = f'[stackup] layer {item!r}: give {{name = "F.Cu", copper = 0.035}} or {{dielectric = 0.2, er = 4.4}}'
            raise StackupError(msg)
        if layers[-1][1] <= 0 or (layers[-1][0] is None and layers[-1][2] < 1):
            msg = f"[stackup] layer {item!r}: thickness must be > 0 and er >= 1"
            raise StackupError(msg)
    if len(layers) < 3 or layers[0][0] is None or layers[-1][0] is None:
        msg = "[stackup] layers must start and end with a copper layer and have a dielectric between"
        raise StackupError(msg)
    if any(a[0] is not None and b[0] is not None for a, b in itertools.pairwise(layers)):
        msg = "[stackup] two copper layers need a dielectric between them"
        raise StackupError(msg)
    return Stackup(label, tuple(layers))


# ---------------------------------------------------------------- solver


def _bisect(f: Callable[[float], float], target: float, lo: float, hi: float, rising: bool) -> float:
    """x in [lo, hi] with f(x) == target for a monotonic f (geometric bisection: x spans decades)."""
    flo, fhi = f(lo), f(hi)
    if not min(flo, fhi) <= target <= max(flo, fhi):
        msg = f"{target:g} ohm is out of reach ({min(flo, fhi):.1f}..{max(flo, fhi):.1f} ohm for {lo:g}..{hi:g} mm)"
        raise StackupError(msg)
    for _ in range(100):
        mid = math.sqrt(lo * hi)
        if (f(mid) < target) == rising:
            lo = mid
        else:
            hi = mid
        if hi / lo < 1 + 1e-9:
            break
    return math.sqrt(lo * hi)


W_MIN, W_MAX = 0.01, 20.0  # mm: the solver's search range for widths and gaps


def solve_width(geo: Geometry, z0: float) -> float:
    """Track width (mm) for a single-ended Z0 (wider = lower impedance)."""
    return _bisect(geo.z0, z0, W_MIN, W_MAX, rising=False)


def solve_pair(
    geo: Geometry, zdiff: float, *, width: float | None = None, gap: float | None = None
) -> tuple[float, float]:
    """(width, gap) for a differential Zdiff: fix one and solve the other, or neither: gap = width."""
    if width is not None and gap is not None:
        msg = "solve_pair: fix the width or the gap, not both"
        raise StackupError(msg)
    if width is not None:
        return width, _bisect(lambda s: geo.zdiff(width, s), zdiff, W_MIN, W_MAX, rising=True)
    if gap is not None:
        return _bisect(lambda w: geo.zdiff(w, gap), zdiff, W_MIN, W_MAX, rising=False), gap
    w = _bisect(lambda w: geo.zdiff(w, w), zdiff, W_MIN, W_MAX, rising=False)
    return w, w


def netclass_widths(stack: Stackup, cls: Mapping[str, Any]) -> tuple[dict[str, float], str]:
    """The widths a net class's ``impedance`` / ``impedance_diff`` ask for, and where they came from.

    ``impedance`` sets track_width. ``impedance_diff`` sets diff_pair_width / diff_pair_gap: a
    given diff_pair_gap stays and the width is solved; else, with ``impedance`` too, the pair keeps
    the single-ended width and the gap is solved; else gap = width. ({}, "") without either key.
    """
    if "impedance" not in cls and "impedance_diff" not in cls:
        return {}, ""
    geo = stack.geometry(str(cls.get("layer", DEFAULT_LAYER)))
    out: dict[str, float] = {}
    parts = []
    if "impedance" in cls:
        out["track_width"] = round(solve_width(geo, float(cls["impedance"])), ROUND_MM)
        parts.append(f"{float(cls['impedance']):g} ohm")
    if "impedance_diff" in cls:
        target = float(cls["impedance_diff"])
        if "diff_pair_gap" in cls:
            w, s = solve_pair(geo, target, gap=float(cls["diff_pair_gap"]))
        elif "track_width" in out:
            w, s = solve_pair(geo, target, width=out["track_width"])
        else:
            w, s = solve_pair(geo, target)
        out["diff_pair_width"], out["diff_pair_gap"] = round(w, ROUND_MM), round(s, ROUND_MM)
        parts.append(f"{target:g} ohm diff")
    where = f"{' / '.join(parts)} {geo.kind} on {geo.layer}, stack-up {stack.name}"
    return out, where
