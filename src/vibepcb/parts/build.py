"""``pcb part build``: ``parts/<mpn>.toml`` -> symbol, footprint, 3D model and previews in ``out/parts/<mpn>/``.

out/parts/<mpn>/
    <mpn>.kicad_sym                      the symbol library (one symbol)
    vibepcb_parts.pretty/<fp>.kicad_mod   the footprint (library nickname = symbol's Footprint field)
    <fp>.step                            rough 3D body (needs CadQuery, else skipped with a note)
    symbol.svg/.png, footprint.svg/.png  previews (kicad-cli; PNG for the agent's own look)
    check.txt                            `pcb part check`
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibepcb import kicad, paths, proc
from vibepcb.parts import footprint, model3d, spec, standard, symbol

if TYPE_CHECKING:
    from pathlib import Path

# KiCad's default theme colours for pin lines, pin numbers and pin names: a KiCad symbol can't carry a pin colour,
# Altium takes it from its preferences (§1.4: black), so the preview shows that colour
KICAD_PIN_COLOURS = ("#840000", "#A90000", "#006464")
FOOTPRINT_LAYERS = "F.Cu,F.Mask,F.SilkS,User.13,User.15"


def parts_dir() -> Path:
    """Where the part specs live: ``<project>/parts``."""
    return paths.ROOT / "parts"


def spec_path(mpn: str) -> Path:
    return parts_dir() / f"{spec.file_name(mpn)}.toml"


def out_dir(mpn: str) -> Path:
    return paths.OUT_DIR / "parts" / spec.file_name(mpn)


@dataclass
class Built:
    spec: spec.Spec
    standard: standard.Standard
    symbol: symbol.Symbol
    footprint: footprint.Footprint
    dir: Path
    symbol_file: Path
    footprint_file: Path
    step_file: Path | None
    notes: list[str] = field(default_factory=list)  # skipped steps
    load_errors: list[str] = field(default_factory=list)  # kicad-cli could not read / export a file
    previews: list[Path] = field(default_factory=list)


def _export(args: list[str], what: str, target: Path, built: Built) -> Path | None:
    """kicad-cli export of one library into ``target``'s folder; the SVG it wrote, renamed to ``target``."""
    cli = kicad.find_kicad_cli()
    if cli is None:
        built.notes.append(f"no kicad-cli: {what} not loaded by KiCad, no preview")
        return None
    with_dir = target.parent / f".{target.stem}"
    shutil.rmtree(with_dir, ignore_errors=True)
    with_dir.mkdir(parents=True)
    result = proc.run([cli, *args, "-o", str(with_dir)], timeout=120)
    svgs = sorted(with_dir.glob("*.svg"))
    if result.returncode != 0 or not svgs:
        built.load_errors.append(f"kicad-cli could not load the {what}: {(result.stderr or result.stdout).strip()}")
        shutil.rmtree(with_dir, ignore_errors=True)
        return None
    svgs[0].replace(target)
    shutil.rmtree(with_dir, ignore_errors=True)
    return target


def build(mpn: str, *, standard_name: str = standard.DEFAULT, path: Path | None = None) -> Built:
    """Generate every file of a part (library code: raises, never prints)."""
    source = path or spec_path(mpn)
    if not source.is_file():
        msg = f"no part spec {source}: run `pcb part fetch {mpn}` and fill it in from the datasheet"
        raise FileNotFoundError(msg)
    s = spec.load(source)
    std = standard.load(standard_name)
    fp = footprint.build(s, std)
    sym = symbol.build(s, std, fp.name)
    target = out_dir(s.mpn)
    target.mkdir(parents=True, exist_ok=True)
    symbol_file = target / f"{spec.file_name(s.mpn)}.kicad_sym"
    symbol_file.write_text(sym.text(), encoding="utf-8")
    pretty = target / f"{symbol.LIBRARY}.pretty"
    pretty.mkdir(exist_ok=True)
    for old in pretty.glob("*.kicad_mod"):
        old.unlink()
    footprint_file = pretty / f"{fp.name}.kicad_mod"
    footprint_file.write_text(fp.text(), encoding="utf-8")
    built = Built(s, std, sym, fp, target, symbol_file, footprint_file, None)
    for old in target.glob("*.step"):  # a renamed footprint leaves its old model behind
        old.unlink()
    step = target / f"{fp.name}.step"
    if reason := model3d.write_step(s, fp, step):
        built.notes.append(f"no 3D model: {reason}")
    else:
        built.step_file = step
    for svg, args, what in (
        (target / "symbol.svg", ["sym", "export", "svg", str(symbol_file)], "symbol"),
        (target / "footprint.svg", ["fp", "export", "svg", "--layers", FOOTPRINT_LAYERS, str(pretty)], "footprint"),
    ):
        if written := _export(args, what, svg, built):
            if what == "symbol":
                pin = "#" + "".join(f"{v:02X}" for v in std.value("symbol", "text_color"))
                text = written.read_text(encoding="utf-8")
                for colour in KICAD_PIN_COLOURS:
                    text = text.replace(colour, pin)
                written.write_text(text, encoding="utf-8")
            built.previews.append(written)
    return built
