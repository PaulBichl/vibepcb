"""Cost estimate of a board batch, hand-assembled: parts from LCSC plus the bare boards from JLCPCB.

Parts: each BOM line's LCSC number is looked up in LCSC's product API (price ladder, minimum order, order
multiple, stock); the quantity bought is the batch need (plus ``spare``) raised to the minimum order and
rounded up to the order multiple, priced at the ladder step it reaches. Lookups are cached for a day in
``~/.cache/vibepcb/lcsc``.

Bare boards: JLCPCB's quote API is not public (access by application), so the price comes from
``[cost] pcb_usd`` (a quote from jlcpcb.com) or else from :data:`PCB_ESTIMATES`, labelled an estimate.
Shipping, customs and LCSC handling fees are not included::

    [cost]
    qty = 5            # boards built (default 5, JLCPCB's minimum)
    spare = 0.1        # extra parts to buy, fraction of the need (default 0: LCSC minimums give spares)
    pcb_usd = 31.4     # the bare-board price of a JLCPCB quote for qty boards (replaces the estimate)
"""

from __future__ import annotations

import csv
import json
import math
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vibepcb import board, fixes, sexpr, symlib

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

API = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={}"
CACHE = symlib.CACHE / "lcsc"
CACHE_MAX_AGE_S = 24 * 3600
TIMEOUT_S = 20
DEFAULT_QTY = 5
_LCSC = re.compile(r"^C\d+$")

# Bare-board price (USD) of the batch: (copper layers, max side mm, max boards) -> (price, source).
# Standard options (FR-4 1.6 mm, HASL, green); outside these the estimate is "unknown".
PCB_ESTIMATES: dict[tuple[int, float, int], tuple[float, str]] = {
    (2, 100.0, 5): (2.0, "jlcpcb.com price list: 2-layer up to 100 x 100 mm, 5 pcs"),
    (4, 100.0, 5): (9.55, "assumed: user-reported JLCPCB quote (2024), 4-layer 100 x 100 mm, 5 pcs"),
}


@dataclass(frozen=True)
class Offer:
    """An LCSC part's buying terms."""

    lcsc: str
    model: str
    ladder: tuple[tuple[int, float], ...]  # (from quantity, USD per piece), ascending
    min_buy: int
    multiple: int
    stock: int


@dataclass(frozen=True)
class Line:
    """One BOM line priced for the batch (``offer`` None: no LCSC number or lookup failed)."""

    lcsc: str
    value: str
    refs: str
    per_board: int
    need: int
    offer: Offer | None
    buy: int = 0
    unit_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return self.buy * self.unit_usd

    @property
    def short(self) -> bool:
        return self.offer is not None and self.offer.stock < self.buy


@dataclass(frozen=True)
class Estimate:
    qty: int
    lines: list[Line]
    layers: int
    size_mm: tuple[float, float]
    pcb_usd: float | None
    pcb_source: str

    @property
    def parts_usd(self) -> float:
        return sum(line.total_usd for line in self.lines)

    @property
    def total_usd(self) -> float | None:
        return None if self.pcb_usd is None else self.parts_usd + self.pcb_usd


def parse_offer(data: dict[str, Any]) -> Offer | None:
    """The buying terms from an LCSC product-detail response, None if it has no price."""
    result = data.get("result")
    if not isinstance(result, dict) or not result.get("productPriceList"):
        return None
    ladder = sorted((int(p["ladder"]), float(p["usdPrice"])) for p in result["productPriceList"])
    return Offer(
        lcsc=str(result.get("productCode", "")),
        model=str(result.get("productModel") or ""),
        ladder=tuple(ladder),
        min_buy=max(1, int(result.get("minBuyNumber") or 1)),
        multiple=max(1, int(result.get("split") or 1)),
        stock=int(result.get("stockNumber") or 0),
    )


def fetch(lcsc: str) -> Offer | None:
    """LCSC's terms for a part (cached for a day), None if unknown or offline."""
    if not _LCSC.match(lcsc):
        return None
    cache = CACHE / f"{lcsc}.json"
    try:
        if time.time() - cache.stat().st_mtime < CACHE_MAX_AGE_S:
            return parse_offer(json.loads(cache.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    request = urllib.request.Request(API.format(lcsc), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    offer = parse_offer(data)
    if offer is not None:
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
    return offer


def buy_quantity(need: int, offer: Offer) -> int:
    """The need raised to the minimum order, rounded up to the order multiple."""
    qty = max(need, offer.min_buy)
    return math.ceil(qty / offer.multiple) * offer.multiple


def unit_price(qty: int, offer: Offer) -> float:
    """The ladder price for buying ``qty`` (the highest step reached)."""
    price = offer.ladder[0][1]
    for start, step_price in offer.ladder:
        if qty >= start:
            price = step_price
    return price


def read_bom(bom: Path) -> list[tuple[str, str, str, int]]:
    """``(lcsc, value, designators, per board)`` per line of KiCad's grouped BOM (:func:`kicad.export_bom`)."""
    with bom.open(encoding="utf-8", newline="") as f:
        return [
            (row.get("LCSC Part #", "").strip(), row["Value"], row["Designator"], int(row["Qty"] or 0))
            for row in csv.DictReader(f)
        ]


def price_lines(
    bom: list[tuple[str, str, str, int]], qty: int, spare: float, lookup: Callable[[str], Offer | None] = fetch
) -> list[Line]:
    """Every BOM line priced for ``qty`` boards (lines sharing an LCSC number are bought together)."""
    need: dict[str, int] = {}
    refs_of: dict[str, list[str]] = {}
    for lcsc, _, refs, per_board in bom:
        if lcsc:
            need[lcsc] = need.get(lcsc, 0) + per_board
            refs_of.setdefault(lcsc, []).append(refs)
    with ThreadPoolExecutor(max_workers=8) as pool:
        offers = dict(zip(need, pool.map(lookup, need), strict=True))
    lines, done = [], set()
    for lcsc, value, line_refs, per_board in bom:
        if lcsc and lcsc in done:
            continue
        done.add(lcsc)
        refs = ",".join(refs_of[lcsc]) if lcsc else line_refs
        total_per_board = need.get(lcsc, per_board)
        batch_need = math.ceil(total_per_board * qty * (1 + spare))
        offer = offers.get(lcsc) if lcsc else None
        if offer is None:
            lines.append(Line(lcsc, value, refs, total_per_board, batch_need, None))
            continue
        buy = buy_quantity(batch_need, offer)
        lines.append(Line(lcsc, value, refs, total_per_board, batch_need, offer, buy, unit_price(buy, offer)))
    return lines


def board_size(pcb: list[sexpr.Node]) -> tuple[float, float]:
    """Width and height (mm) of the Edge.Cuts outline's bounding box."""
    points = [
        p
        for item in pcb[1:]
        if isinstance(item, list) and sexpr.head(item) in ("gr_line", "gr_rect", "gr_arc", "gr_poly", "gr_circle")
        and fixes._layer(item) == "Edge.Cuts"
        for p in fixes._graphic_points(item)
    ]  # fmt: skip
    if not points:
        return 0.0, 0.0
    xs, ys = [x for x, _ in points], [y for _, y in points]
    return round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2)


def pcb_estimate(layers: int, size_mm: tuple[float, float], qty: int) -> tuple[float | None, str]:
    """The batch's bare-board price from :data:`PCB_ESTIMATES` (cheapest fitting entry) and its source."""
    fitting = [
        (price, source)
        for (n, side, max_qty), (price, source) in PCB_ESTIMATES.items()
        if n == layers and max(size_mm) <= side and qty <= max_qty
    ]
    if not fitting:
        return None, f"no estimate for {layers} layers, {size_mm[0]:g} x {size_mm[1]:g} mm, {qty} pcs"
    return min(fitting)


def estimate(
    pcb_path: Path, bom: Path, cost: dict[str, Any], lookup: Callable[[str], Offer | None] = fetch
) -> Estimate:
    """The batch cost of the board: parts (``bom``) priced at LCSC, plus the bare boards."""
    qty = int(cost.get("qty", DEFAULT_QTY))
    pcb = sexpr.load(pcb_path)
    layers, size = len(board.copper_layers(pcb)), board_size(pcb)
    lines = price_lines(read_bom(bom), qty, float(cost.get("spare", 0.0)), lookup)
    pcb_usd: float | None
    if "pcb_usd" in cost:
        pcb_usd, source = float(cost["pcb_usd"]), "quote ([cost] pcb_usd)"
    else:
        pcb_usd, source = pcb_estimate(layers, size, qty)
        source = f"estimate - {source}"
    return Estimate(qty, lines, layers, size, pcb_usd, source)


def render(name: str, est: Estimate) -> str:
    """The estimate as text (``out/<board>/cost.txt``)."""
    out = [
        f"{name}: cost of {est.qty} board(s), hand-assembled (USD, without shipping / customs / LCSC handling)",
        "",
        f"{'LCSC':<10} {'value':<24} {'/board':>6} {'need':>6} {'buy':>6} {'unit':>9} {'total':>9}  refs",
    ]
    for line in sorted(est.lines, key=lambda x: -x.total_usd):
        refs = line.refs if len(line.refs) <= 40 else line.refs[:37] + "..."
        if line.offer is None:
            why = "no LCSC number" if not line.lcsc else "no LCSC price"
            out.append(f"{line.lcsc or '-':<10} {line.value[:24]:<24} {line.per_board:>6} {line.need:>6} "
                       f"{'':>6} {'':>9} {why:>9}  {refs}")  # fmt: skip
            continue
        flag = f"  (stock {line.offer.stock})" if line.short else ""
        out.append(f"{line.lcsc:<10} {line.value[:24]:<24} {line.per_board:>6} {line.need:>6} {line.buy:>6} "
                   f"{line.unit_usd:>9.4f} {line.total_usd:>9.2f}  {refs}{flag}")  # fmt: skip
    unpriced = [line for line in est.lines if line.offer is None]
    short = [line for line in est.lines if line.short]
    pcb = "unknown" if est.pcb_usd is None else f"{est.pcb_usd:.2f}"
    out += [
        "",
        f"parts      {est.parts_usd:>9.2f}  ({len(est.lines) - len(unpriced)} lines priced at LCSC)",
        f"bare PCBs  {pcb:>9}  ({est.layers} layers, {est.size_mm[0]:g} x {est.size_mm[1]:g} mm; {est.pcb_source})",
    ]
    if est.total_usd is not None:
        out.append(f"total      {est.total_usd:>9.2f}  ({est.total_usd / est.qty:.2f} per board)")
    if unpriced:
        out.append(f"not priced: {', '.join(line.refs for line in unpriced)} - add them by hand")
    if short:
        out.append(f"LCSC stock below the buy quantity: {', '.join(line.lcsc for line in short)}")
    if est.pcb_usd is None or est.pcb_source.startswith("estimate"):
        out.append("bare-board price: quote the gerbers at jlcpcb.com and set [cost] pcb_usd in vibepcb.toml")
    return "\n".join(out) + "\n"
