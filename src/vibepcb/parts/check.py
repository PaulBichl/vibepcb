"""``pcb part check``: the generated files against the standard -> ``out/parts/<mpn>/check.txt``.

Errors fail the check: KiCad can't load a file, symbol pins and footprint pads differ, a rule of the standard is
broken (each names its section). Warnings: lifecycle not active, a land pattern to compare with the datasheet.
Manual items: what the code can't judge (the standard's ``[unexpressed]`` rules, the drawing comparison).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb import paths, sexpr

if TYPE_CHECKING:
    from vibepcb.parts.build import Built
    from vibepcb.parts.footprint import Box, Segment
    from vibepcb.parts.standard import Standard

EPS = 1e-6
KICAD_FIELDS = {"Reference", "Value", "Footprint", "Datasheet", "Description"}


@dataclass
class Report:
    mpn: str
    standard: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        lines = [
            f"part check: {self.mpn} (standard {self.standard})",
            f"result: {verdict} ({len(self.errors)} errors, {len(self.warnings)} warnings, "
            f"{len(self.manual)} manual items)",
        ]
        for title, items in (("ERRORS", self.errors), ("WARNINGS", self.warnings), ("MANUAL", self.manual)):
            if items:
                lines += ["", title, *(f"  - {item}" for item in items)]
        if self.files:
            lines += ["", "FILES", *(f"  {f}" for f in self.files)]
        return "\n".join(lines) + "\n"


def _dist(seg: Segment, box: Box) -> float:
    x0, y0, x1, y1 = seg
    steps = max(int(math.hypot(x1 - x0, y1 - y0) / 0.05), 1)
    best = math.inf
    for i in range(steps + 1):
        x = x0 + (x1 - x0) * i / steps
        y = y0 + (y1 - y0) * i / steps
        best = min(best, math.hypot(max(box.x0 - x, 0, x - box.x1), max(box.y0 - y, 0, y - box.y1)))
    return best


def _symbol_rules(built: Built, std: Standard, report: Report) -> None:
    sym = built.symbol
    pitch = std.num("symbol", "pin_pitch")
    pitch_src = std.rule("symbol", "pin_pitch").source
    for number, (x, y, _a) in sym.pin_at.items():
        if abs(x / pitch - round(x / pitch)) > EPS or abs(y / pitch - round(y / pitch)) > EPS:
            report.errors.append(f"symbol pin {number} at ({x}, {y}) is off the {pitch} mm grid ({pitch_src})")
    points = list(sym.pin_at.values())
    for i, (x, y, _a) in enumerate(points):
        for x2, y2, _b in points[i + 1 :]:
            if math.hypot(x - x2, y - y2) < pitch - EPS:
                report.errors.append(f"symbol pins closer than {pitch} mm at ({x}, {y}) ({pitch_src})")
    length = std.num("symbol", "pin_length")
    for pin in sexpr.walk(sym.tree):
        if sexpr.head(pin) == "pin":
            got = sexpr.child(pin, "length")
            if got is None or abs(float(str(got[1])) - length) > EPS:
                report.errors.append(
                    f"symbol pin length {got} != {length} mm ({std.rule('symbol', 'pin_length').source})"
                )
    ref = sexpr.property_node(sym.tree, "Reference")
    if ref is None or sexpr.child(ref, "hide") is not None:
        report.errors.append(f"designator not visible ({std.rule('symbol', 'designator_visible').source})")
    elif std.text("symbol", "designator_position") == "top_left":
        at = sexpr.child(ref, "at")
        x0, _y0, _x1, y1 = sym.box
        if at is None or float(str(at[1])) > x0 + EPS or float(str(at[2])) < y1 - EPS:
            report.errors.append(f"designator not at the top left ({std.rule('symbol', 'designator_position').source})")
    value = sexpr.property_node(sym.tree, "Value")
    if std.flag("symbol", "comment_empty_hidden") and (
        value is None or value[2] != "" or sexpr.child(value, "hide") is None
    ):
        report.errors.append(
            f"comment (Value) not empty and hidden ({std.rule('symbol', 'comment_empty_hidden').source})"
        )
    extra = [
        str(n[1])
        for n in sym.tree
        if isinstance(n, list) and sexpr.head(n) == "property" and str(n[1]) not in KICAD_FIELDS
    ]
    allowed = set(std.value("symbol", "parameters"))
    if bad := [k for k in extra if k not in allowed]:
        report.errors.append(f"symbol parameters {bad} ({std.rule('symbol', 'parameters').source})")
    names = [p.name for p in sym.pins]
    if std.flag("symbol", "number_repeated_names") and (dupes := sorted({n for n in names if names.count(n) > 1})):
        report.errors.append(f"repeated pin names {dupes} ({std.rule('symbol', 'number_repeated_names').source})")
    ep = built.spec.package.exposed_pad
    if ep and std.flag("symbol", "exposed_pad_pin") and ep.number not in sym.pin_at:
        report.errors.append(f"exposed pad {ep.number} has no pin ({std.rule('symbol', 'exposed_pad_pin').source})")
    for p in sym.pins:
        expected = {"input": "inputs", "output": "outputs"}.get(p.type)
        if expected and p.side not in (std.text("symbol", expected), "top", "bottom"):
            report.warnings.append(
                f"pin {p.number} {p.name} ({p.type}) on the {p.side} ({std.rule('symbol', expected).source})"
            )


def _footprint_rules(built: Built, std: Standard, report: Report) -> None:
    fp = built.footprint
    boxes = [p.box for p in fp.pads]
    mid_x = (min(b.x0 for b in boxes) + max(b.x1 for b in boxes)) / 2
    mid_y = (min(b.y0 for b in boxes) + max(b.y1 for b in boxes)) / 2
    if abs(mid_x) > 1e-3 or abs(mid_y) > 1e-3:
        report.errors.append(f"origin not in the middle of the pads ({std.rule('footprint', 'origin').source})")
    mask = std.num("footprint", "mask_expansion")
    paste = std.num("footprint", "paste_expansion")
    shape = std.text("footprint", "pad_shape")
    pin1 = std.text("footprint", "pin1_text")
    for pad in sexpr.children(fp.tree, "pad"):
        number = pad[1]
        m = sexpr.child(pad, "solder_mask_margin")
        if m is None or abs(float(str(m[1])) - mask) > EPS:
            report.errors.append(
                f"pad {number}: solder mask expansion != {mask} ({std.rule('footprint', 'mask_expansion').source})"
            )
        if pad[2] == "smd":
            q = sexpr.child(pad, "solder_paste_margin")
            if q is None or abs(float(str(q[1])) - paste) > EPS:
                report.errors.append(
                    f"pad {number}: paste expansion != {paste} ({std.rule('footprint', 'paste_expansion').source})"
                )
            if pad[3] != shape and built.spec.land is None:
                report.errors.append(
                    f"pad {number}: {pad[3]}, not {shape} ({std.rule('footprint', 'pad_shape').source})"
                )
    toe_min = std.num("footprint", "toe_min")
    if fp.toe is not None and fp.toe < toe_min - EPS:
        report.errors.append(
            f"only {fp.toe} mm copper beyond the termination at max part size, needs {toe_min} "
            f"({std.rule('footprint', 'toe_min').source})"
        )
    elif fp.toe is None and any(not p.drill for p in fp.pads):
        report.manual.append(
            f"land pattern from the datasheet: check >= {toe_min} mm copper beyond the terminations "
            f"({std.rule('footprint', 'toe_min').source})"
        )
    keep = std.num("footprint", "silk_pad_clearance") + std.num("choices", "silk_width") / 2
    for seg in fp.silk:
        if (d := min(_dist(seg, b) for b in boxes)) < keep - 1e-3:
            gap = d - std.num("choices", "silk_width") / 2
            report.errors.append(
                f"silkscreen {gap:.3f} mm from a pad ({std.rule('footprint', 'silk_pad_clearance').source})"
            )
    if std.num("choices", "silk_width") < std.num("footprint", "silk_min_width") - EPS:
        report.errors.append(f"silkscreen line below the minimum ({std.rule('footprint', 'silk_min_width').source})")
    grid = std.num("footprint", "placement_grid")
    lo, _hi = std.value("footprint", "placement_margin")
    pl = fp.placement
    for v in (pl.x0, pl.y0, pl.x1, pl.y1):
        if abs(v / grid - round(v / grid)) > 1e-4:
            report.errors.append(
                f"placement outline off the {grid} mm grid ({std.rule('footprint', 'placement_grid').source})"
            )
            break
    for b in [*boxes, fp.body]:
        if b.x0 - pl.x0 < lo - EPS or pl.x1 - b.x1 < lo - EPS or b.y0 - pl.y0 < lo - EPS or pl.y1 - b.y1 < lo - EPS:
            report.errors.append(
                f"placement outline closer than {lo} mm to the body / pads ({std.rule('footprint', 'placement_margin').source})"
            )
            break
    layers = {
        str(sexpr.child(n, "layer")[1])  # type: ignore[index]
        for n in sexpr.walk(fp.tree)
        if sexpr.head(n) in ("fp_rect", "fp_line", "fp_circle", "fp_text") and sexpr.child(n, "layer") is not None
    }
    mech13, mech15 = std.text("layers", "mech13"), std.text("layers", "mech15")
    if mech13 not in layers:
        report.errors.append(f"no component outline on {mech13} ({std.rule('layers', 'mech13').source})")
    if mech15 not in layers:
        report.errors.append(f"no assembly information on {mech15} ({std.rule('layers', 'mech15').source})")
    texts = {str(n[2]): sexpr.child(n, "layer") for n in sexpr.walk(fp.tree) if sexpr.head(n) == "fp_text"}
    if std.flag("footprint", "assembly_designator") and texts.get(".Designator") != ["layer", mech15]:
        report.errors.append(f"no .Designator on {mech15} ({std.rule('footprint', 'assembly_designator').source})")
    ic = built.spec.kind in ("ic", "opamp", "connector", "transistor")
    if pin1 and ic and texts.get(pin1) != ["layer", mech15]:
        report.errors.append(f"pad 1 not copied to {mech15} as {pin1} ({std.rule('footprint', 'pin1_text').source})")


def run(built: Built) -> Report:
    std = built.standard
    s = built.spec
    report = Report(s.mpn, std.name)
    report.errors += built.load_errors
    pins = set(built.symbol.pin_at)
    pads = {p.number for p in built.footprint.pads}
    if missing := sorted(pins - pads):
        report.errors.append(f"symbol pins without a footprint pad: {missing}")
    if missing := sorted(pads - pins):
        report.errors.append(f"footprint pads without a symbol pin: {missing}")
    _symbol_rules(built, std, report)
    _footprint_rules(built, std, report)
    if s.lifecycle != "active":
        report.warnings.append(f"lifecycle: {s.lifecycle} (from {s.source or 'the spec'})")
    if s.land is None:
        report.warnings.append(
            f"land pattern calculated ({built.footprint.notes[0].split(': ', 1)[1]}): compare footprint.svg with the"
            f" package drawing, datasheet page {s.package.page}, and any recommended land pattern there"
        )
    else:
        report.manual.append(f"compare footprint.svg with the datasheet land pattern, page {s.land.page}")
    report.manual.append(
        f"compare symbol.svg pin numbers / names / types with the datasheet pin table (pages "
        f"{', '.join(str(p) for p in sorted({p.page for p in s.pins}))})"
    )
    report.manual += built.notes
    report.manual += std.unexpressed
    for path in (built.symbol_file, built.footprint_file, built.step_file, *built.previews):
        if path is not None:
            report.files.append(str(path.relative_to(paths.ROOT)) if path.is_relative_to(paths.ROOT) else str(path))
    return report
