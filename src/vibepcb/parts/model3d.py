"""Rough 3D model: a clearance body as STEP (CadQuery, an optional dependency: ``pip install vibepcb[parts]``).

The body is a box at the package's maximum length / width / height (conservative for clearance checks), standing
off the board by the stand-off; gull-wing parts get box leads out to the lead span, chips their end caps, QFN /
DFN flat pads under the body edge. Pin 1 is a shallow dimple at its corner. Origin and orientation match the
footprint (pads centre, pin 1 in the same corner), z = 0 at the board surface. Parts with an explicit land
(custom, connectors, THT) get the body box only.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from vibepcb.parts.footprint import Footprint
    from vibepcb.parts.spec import Spec

LEAD_T = 0.15  # lead thickness of the gull-wing / pad model, mm
DIMPLE = 0.05  # pin-1 dimple depth, mm


def cadquery() -> Any | None:
    try:
        return importlib.import_module("cadquery")
    except ImportError:
        return None


def write_step(spec: Spec, fp: Footprint, path: Path) -> str | None:
    """Write the STEP; the reason when it can't (no CadQuery), else None."""
    cq = cadquery()
    if cq is None:
        return "CadQuery is not installed (`hatch run parts:pcb part ...` or `pip install vibepcb[parts]`)"
    pkg = spec.package
    body = fp.body
    standoff = pkg.standoff
    height = max(pkg.height - standoff, 0.1)
    bx, by = body.x1 - body.x0, body.y1 - body.y0
    cx, cy = (body.x0 + body.x1) / 2, (body.y0 + body.y1) / 2
    # KiCad footprints have y down, STEP / KiCad 3D y up: mirror y
    shape = cq.Workplane("XY").box(bx, by, height, centered=(True, True, False)).translate((cx, -cy, standoff))
    leads = None
    if spec.land is None and pkg.style in ("gullwing", "nolead", "chip"):
        for pad in fp.pads:
            if pkg.exposed_pad and pad.number == pkg.exposed_pad.number:
                continue
            box = pad.box
            if pkg.style == "gullwing":
                # a flat lead from the body edge out to the lead tip (half the max span), lead.w wide
                w = pkg.lead_w.max if pkg.lead_w else min(box.x1 - box.x0, box.y1 - box.y0)
                half_x = pkg.span.max / 2 if pkg.span else box.x1 - cx
                span_y = pkg.span_y or pkg.span
                half_y = span_y.max / 2 if span_y else box.y1 - cy
                if abs(pad.x) * by >= abs(pad.y) * bx:  # left / right row
                    x0, x1 = (cx - half_x, body.x0) if pad.x < cx else (body.x1, cx + half_x)
                    lead = cq.Workplane("XY").box(abs(x1 - x0), w, LEAD_T, centered=(True, True, False))
                    lead = lead.translate(((x0 + x1) / 2, -pad.y, 0))
                else:  # top / bottom row
                    y0, y1 = (cy - half_y, body.y0) if pad.y < cy else (body.y1, cy + half_y)
                    lead = cq.Workplane("XY").box(w, abs(y1 - y0), LEAD_T, centered=(True, True, False))
                    lead = lead.translate((pad.x, -(y0 + y1) / 2, 0))
            elif pkg.style == "chip":
                cap = min(box.x1 - box.x0, bx / 3)
                x = body.x0 + cap / 2 if pad.x < cx else body.x1 - cap / 2
                lead = cq.Workplane("XY").box(cap, by, height, centered=(True, True, False)).translate((x, -cy, 0))
            else:  # QFN / DFN terminal: the land's part under the body
                x0, x1 = max(box.x0, body.x0), min(box.x1, body.x1)
                y0, y1 = max(box.y0, body.y0), min(box.y1, body.y1)
                if x1 - x0 < 0.01 or y1 - y0 < 0.01:
                    continue
                lead = (
                    cq.Workplane("XY")
                    .box(x1 - x0, y1 - y0, LEAD_T / 3, centered=(True, True, False))
                    .translate(((x0 + x1) / 2, -(y0 + y1) / 2, 0))
                )
            leads = lead if leads is None else leads.union(lead)
    pin1 = next((p for p in fp.pads if p.number == "1"), None)
    if pin1 is not None and spec.kind not in ("resistor", "capacitor", "inductor"):
        r = max(min(bx, by) / 12, 0.1)
        px = min(max(pin1.x, body.x0 + 2 * r), body.x1 - 2 * r)
        py = min(max(pin1.y, body.y0 + 2 * r), body.y1 - 2 * r)
        dimple = cq.Workplane("XY").circle(r).extrude(DIMPLE).translate((px, -py, standoff + height - DIMPLE))
        shape = shape.cut(dimple)
    if leads is not None:
        shape = shape.union(leads)
    path.parent.mkdir(parents=True, exist_ok=True)
    cq.exporters.export(shape, str(path), exportType="STEP")
    return None
