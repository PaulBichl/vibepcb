"""``pcb part export --altium <dir>``: the import set for Altium Designer 26's KiCad importer.

Copies the symbol library, the ``.pretty`` footprint folder and the STEP, and writes ``IMPORT.md`` (the import
steps) plus ``<mpn>.altium.json``: what the importer may lose and the clean-up script restores (line widths per
layer, field visibility, the 3D model, the symbol -> footprint link). Which of these Altium really drops is
settled by the probe in ``scripts/altium/probe`` (findings in docs/TOOLING.md).
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING, Any

from vibepcb import sexpr
from vibepcb.parts import spec

if TYPE_CHECKING:
    from pathlib import Path

    from vibepcb.parts.build import Built

IMPORT_STEPS = """# Import {mpn} into Altium Designer 26

Files: `{sym}` (symbol), `{pretty}/` (footprint `{fp}`), `{step}` (3D body), `{sidecar}` (for the clean-up script).

1. File » Import Wizard » KiCad Design Files. Add `{sym}` and the `{pretty}` folder as library files
   (`.kicad_mod` imports although the wizard's file list doesn't advertise it).
2. Open the imported PcbLib, footprint `{fp}`: Mechanical 13 should hold the body outline, Mechanical 15 the
   placement outline, centre cross, `.Designator` and the pin-1 mark (standard {standard} §2.1). If they landed
   on other layers, note it in VibePCB's docs/TOOLING.md (Altium import findings) and fix `[layers]` in the
   standard's TOML instead of moving them by hand.
3. Attach `{step}` to the footprint as its 3D body (Place » 3D Body » Generic STEP model) if the import did not.
4. Symbol: designator visible top left, comment empty and hidden, no parameters (§1.3): delete Footprint /
   Datasheet / Description if they came in as parameters. Link the symbol to footprint `{fp}`.
5. Merge into the house library (IntLib / DbLib) as usual.
"""


def sidecar(built: Built) -> dict[str, Any]:
    widths: dict[str, list[float]] = {}
    for node in sexpr.walk(built.footprint.tree):
        if sexpr.head(node) in ("fp_line", "fp_rect", "fp_circle"):
            layer = sexpr.child(node, "layer")
            stroke = sexpr.child(node, "stroke")
            width = sexpr.child(stroke, "width") if stroke else None
            if layer and width:
                widths.setdefault(str(layer[1]), [])
                w = float(str(width[1]))
                if w not in widths[str(layer[1])]:
                    widths[str(layer[1])].append(w)
    fields = {}
    for prop in built.symbol.tree:
        if isinstance(prop, list) and sexpr.head(prop) == "property":
            fields[str(prop[1])] = {"value": str(prop[2]), "visible": sexpr.child(prop, "hide") is None}
    std = built.standard
    return {
        "mpn": built.spec.mpn,
        "standard": std.name,
        "symbol": built.symbol.name,
        "footprint": built.footprint.name,
        "model": built.step_file.name if built.step_file else None,
        "layers": {"mech13": std.text("layers", "mech13"), "mech15": std.text("layers", "mech15")},
        "line_widths": {layer: sorted(ws) for layer, ws in sorted(widths.items())},
        "fields": fields,
        "parameters": std.value("symbol", "parameters"),
    }


def export(built: Built, target: Path) -> list[Path]:
    """Copy the import set into ``target``; the files written."""
    target.mkdir(parents=True, exist_ok=True)
    name = spec.file_name(built.spec.mpn)
    pretty = built.footprint_file.parent
    written = [target / built.symbol_file.name]
    shutil.copyfile(built.symbol_file, written[0])
    shutil.copytree(pretty, target / pretty.name, dirs_exist_ok=True)
    written.append(target / pretty.name / built.footprint_file.name)
    if built.step_file:
        written.append(target / built.step_file.name)
        shutil.copyfile(built.step_file, written[-1])
    side = target / f"{name}.altium.json"
    side.write_text(json.dumps(sidecar(built), indent=2) + "\n", encoding="utf-8")
    steps = target / f"IMPORT-{name}.md"
    steps.write_text(
        IMPORT_STEPS.format(
            mpn=built.spec.mpn,
            sym=built.symbol_file.name,
            pretty=pretty.name,
            fp=built.footprint.name,
            step=built.step_file.name if built.step_file else "(no STEP: CadQuery missing)",
            sidecar=side.name,
            standard=built.standard.name,
        ),
        encoding="utf-8",
    )
    return [*written, side, steps]
