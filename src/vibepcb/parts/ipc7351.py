"""IPC-7351B land patterns from the package drawing (the standard's "IPC Compliant Wizard" path, §2).

For a lead pair across the package (tip-to-tip span L, foot length T, lead width W):

    Zmax = Lmin + 2 Jt + sqrt(CL^2 + F^2 + P^2)                 outer edge to outer edge
    Gmin = Smax(RMS) - 2 Jh - sqrt(CS(RMS)^2 + F^2 + P^2)       inner edge to inner edge, S = L - 2T
    Xmax = Wmin + 2 Js + sqrt(CW^2 + F^2 + P^2)                 pad width

with S's tolerance taken as RMS (sqrt(CL^2 + 2 CT^2)) instead of the worst case, Jt / Jh / Js the toe / heel /
side fillet goals of the density level, F the board fabrication and P the placement tolerance. Each result is
rounded to the table's round-off. The fillet tables are IPC-7351B tables 3-2, 3-3, 3-5/3-6 and 3-15 (values as
listed by KiCad's footprint generator, kicad-footprint-generator ``ipc_7351b.yaml`` / ``ipc7351B_2terminal.yaml``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibepcb.parts.spec import Dim, Package

FAB_TOL = 0.05  # F: board fabrication tolerance (IPC-7351B / Altium IPC wizard default)
PLACE_TOL = 0.025  # P: placement tolerance


@dataclass(frozen=True)
class Fillets:
    toe: float
    heel: float
    side: float
    round_off: float
    source: str


# family table -> density -> (toe, heel, side); round-off per table
_TABLES: dict[str, tuple[dict[str, tuple[float, float, float]], float, str]] = {
    "gw_large_pitch": (
        {"M": (0.55, 0.45, 0.05), "N": (0.35, 0.35, 0.03), "L": (0.15, 0.25, 0.01)},
        0.05,
        "IPC-7351B table 3-2 (gull-wing, pitch > 0.625 mm)",
    ),
    "gw_small_pitch": (
        {"M": (0.55, 0.45, 0.01), "N": (0.35, 0.35, -0.02), "L": (0.15, 0.25, -0.04)},
        0.05,
        "IPC-7351B table 3-3 (gull-wing, pitch <= 0.625 mm)",
    ),
    "chip_large": (
        {"M": (0.55, 0.0, 0.05), "N": (0.35, 0.0, 0.0), "L": (0.15, 0.0, -0.05)},
        0.05,
        "IPC-7351B table 3-5 (chip >= 0603)",
    ),
    "chip_small": (
        {"M": (0.3, 0.0, 0.05), "N": (0.2, 0.0, 0.0), "L": (0.1, 0.0, -0.05)},
        0.02,
        "IPC-7351B table 3-6 (chip < 0603)",
    ),
    "flat_no_lead": (
        {"M": (0.4, 0.0, -0.04), "N": (0.3, 0.0, -0.04), "L": (0.2, 0.0, -0.04)},
        0.05,
        "IPC-7351B table 3-15 (QFN / DFN, toe fillet)",
    ),
}
# courtyard excess by density (IPC-7351B); the standard's placement margin replaces it where it says so
COURTYARD = {"M": 0.5, "N": 0.25, "L": 0.1}


def fillets(package: Package, density: str) -> Fillets:
    if package.style == "chip":
        table = "chip_small" if package.body_l.nom < 1.6 - 1e-9 else "chip_large"
    elif package.style == "nolead":
        table = "flat_no_lead"
    else:
        table = "gw_large_pitch" if package.pitch > 0.625 else "gw_small_pitch"
    values, round_off, source = _TABLES[table]
    toe, heel, side = values[density]
    return Fillets(toe, heel, side, round_off, source)


def _round(value: float, base: float) -> float:
    return round(round(value / base) * base, 4)


@dataclass(frozen=True)
class LandRow:
    """One pair of opposite lands: Z / G / X, pad size and the pad centre's distance from the package centre."""

    z: float
    g: float
    x: float

    @property
    def pad_length(self) -> float:
        return round((self.z - self.g) / 2, 4)

    @property
    def centre(self) -> float:
        return round((self.z + self.g) / 4, 4)


def land_row(span: Dim, foot: Dim, width: Dim, f: Fillets) -> LandRow:
    """Z / G / X for leads with tip-to-tip ``span``, foot length ``foot`` and width ``width``."""
    fp = FAB_TOL**2 + PLACE_TOL**2
    s_min = span.min - 2 * foot.max
    s_max = span.max - 2 * foot.min
    s_tol = s_max - s_min
    s_tol_rms = min(s_tol, math.sqrt(span.tol**2 + 2 * foot.tol**2))
    s_max_rms = s_max - (s_tol - s_tol_rms) / 2
    z = span.min + 2 * f.toe + math.sqrt(span.tol**2 + fp)
    g = s_max_rms - 2 * f.heel - math.sqrt(s_tol_rms**2 + fp)
    x = width.min + 2 * f.side + math.sqrt(width.tol**2 + fp)
    return LandRow(_round(z, f.round_off), _round(g, f.round_off), _round(x, f.round_off))


@dataclass(frozen=True)
class Pad:
    number: str
    x: float
    y: float
    l: float  # noqa: E741 - size along x
    w: float  # size along y


@dataclass(frozen=True)
class Pattern:
    pads: list[Pad]
    rows: list[LandRow]  # the row(s) used: one for two-sided parts, x then y for four-sided
    fillets: Fillets
    name: str


def _column(n: int, pitch: float) -> list[float]:
    """Centres of n pads at ``pitch``, centred on 0, first one at the most negative coordinate."""
    return [round((i - (n - 1) / 2) * pitch, 4) for i in range(n)]


def _two_rows(pins: int) -> tuple[list[int], list[int]]:
    """Pitch slots (top = 0) of the left pins (counted down) and the right pins (counted up).

    Even counts fill both rows; odd SOT packages leave a slot free as JEDEC draws them: SOT-23-3 has pins 1 / 2
    at the outer left slots and pin 3 in the middle right, SOT-23-5 has 1-3 left and 4 / 5 at the outer right.
    """
    if pins % 2 == 0:
        n = pins // 2
        return list(range(n)), list(reversed(range(n)))
    if pins == 3:
        return [0, 2], [1]
    n = (pins + 1) // 2
    return list(range(n)), [slot for slot in reversed(range(n)) if slot != n // 2]


EP_CLEARANCE = 0.2  # min copper gap between a terminal pad and the exposed pad (KiCad generator's IPC rule)


def _clear_exposed_pad(pads: list[Pad], ep: Pad, gap: float) -> list[Pad]:
    """Trim the inner (heel) end of terminal pads that come closer than ``gap`` to the exposed pad."""
    out = []
    for p in pads:
        if abs(p.x) > abs(p.y) and abs(p.y) < ep.w / 2 + p.w / 2 + gap:  # left / right row facing the EP
            outer = abs(p.x) + p.l / 2
            inner = max(abs(p.x) - p.l / 2, ep.l / 2 + gap)
            sign = 1 if p.x > 0 else -1
            p = Pad(p.number, round(sign * (outer + inner) / 2, 4), p.y, round(outer - inner, 4), p.w)
        elif abs(p.y) >= abs(p.x) and abs(p.x) < ep.l / 2 + p.l / 2 + gap:  # top / bottom row
            outer = abs(p.y) + p.w / 2
            inner = max(abs(p.y) - p.w / 2, ep.w / 2 + gap)
            sign = 1 if p.y > 0 else -1
            p = Pad(p.number, p.x, round(sign * (outer + inner) / 2, 4), p.l, round(outer - inner, 4))
        out.append(p)
    return out


def calculate(package: Package, density: str = "N", kind: str = "ic", ep_clearance: float = EP_CLEARANCE) -> Pattern:
    """The land pattern of an IPC family package, pads numbered in IPC zero orientation (pin 1 top left)."""
    if package.style is None:
        msg = f"family {package.family} has no IPC calculation (needs an explicit [land])"
        raise ValueError(msg)
    f = fillets(package, density)
    pads: list[Pad] = []
    rows: list[LandRow] = []
    if package.style == "chip":
        if package.lead_l is None:
            msg = "chip package without lead.l"
            raise ValueError(msg)
        row = land_row(package.body_l, package.lead_l, package.lead_w or package.body_w, f)
        rows.append(row)
        pads = [Pad("1", -row.centre, 0.0, row.pad_length, row.x), Pad("2", row.centre, 0.0, row.pad_length, row.x)]
    else:
        if package.span is None or package.lead_l is None or package.lead_w is None:
            msg = f"{package.family} package needs span, lead.l and lead.w"
            raise ValueError(msg)
        row = land_row(package.span, package.lead_l, package.lead_w, f)
        rows.append(row)
        if package.sides == 2:
            left, right = _two_rows(package.pins)
            ys = _column(max(*left, *right) + 1, package.pitch)
            numbers = iter(range(1, package.pins + 1))
            pads += [Pad(str(next(numbers)), -row.centre, ys[slot], row.pad_length, row.x) for slot in left]
            pads += [Pad(str(next(numbers)), row.centre, ys[slot], row.pad_length, row.x) for slot in right]
        else:
            pins_x = package.pins_x if package.pins_x is not None else package.pins // 4
            pins_y = (package.pins - 2 * pins_x) // 2
            row_y = land_row(package.span_y or package.span, package.lead_l, package.lead_w, f)
            rows.append(row_y)
            ys = _column(pins_y, package.pitch)
            xs = _column(pins_x, package.pitch)
            n = 0
            for y in ys:  # left, top to bottom
                n += 1
                pads.append(Pad(str(n), -row.centre, y, row.pad_length, row.x))
            for x in xs:  # bottom, left to right
                n += 1
                pads.append(Pad(str(n), x, row_y.centre, row_y.x, row_y.pad_length))
            for y in reversed(ys):  # right, bottom to top
                n += 1
                pads.append(Pad(str(n), row.centre, y, row.pad_length, row.x))
            for x in reversed(xs):  # top, right to left
                n += 1
                pads.append(Pad(str(n), x, -row_y.centre, row_y.x, row_y.pad_length))
    if ep := package.exposed_pad:
        ep_pad = Pad(ep.number, 0.0, 0.0, round(ep.w.nom, 4), round(ep.l.nom, 4))  # ep.l runs along y
        pads = [*_clear_exposed_pad(pads, ep_pad, ep_clearance), ep_pad]
    return Pattern(pads, rows, f, name(package, density, kind))


def _h(value: float) -> str:
    return str(round(value * 100))


def name(package: Package, density: str, kind: str = "ic") -> str:
    """IPC-7351 footprint name, e.g. ``SOIC127P600X175-8N``, ``QFN50P300X300X100-17N``, ``RESC1608X55N``."""
    pitch = _h(package.pitch)
    height = _h(package.height)
    pins = package.pins + (1 if package.exposed_pad else 0)
    family = package.family
    if package.style == "chip":
        prefix = {
            "resistor": "RESC",
            "capacitor": "CAPC",
            "polar_capacitor": "CAPCP",
            "inductor": "INDC",
            "diode": "DIOC",
            "led": "LEDC",
        }.get(kind, "CHIP")
        return f"{prefix}{round(package.body_l.nom * 10):02d}{round(package.body_w.nom * 10):02d}X{height}{density}"
    span = _h(package.span.nom) if package.span else "0"
    if family in ("QFN", "DFN"):
        prefix = "QFN" if family == "QFN" else "SON"
        return f"{prefix}{pitch}P{_h(package.body_l.nom)}X{_h(package.body_w.nom)}X{height}-{pins}{density}"
    if family == "QFP":
        span_y = _h(package.span_y.nom) if package.span_y else span
        return f"QFP{pitch}P{span}X{span_y}X{height}-{pins}{density}"
    prefix = {"SOIC": "SOIC", "SOT": "SOT", "SOD": "SOD"}.get(family, "SOP")
    return f"{prefix}{pitch}P{span}X{height}-{pins}{density}"
