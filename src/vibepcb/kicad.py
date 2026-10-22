"""KiCad side: assemble a project from tscircuit's export and run ``kicad-cli`` checks."""

from __future__ import annotations

import copy
import csv
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibepcb import proc, sexpr
from vibepcb.sexpr import Sym

if TYPE_CHECKING:
    from subprocess import CompletedProcess

    from vibepcb.sexpr import Node


class KicadCliError(RuntimeError):
    pass


def find_kicad_cli() -> str | None:
    return os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")


SUPPORTED_MAJORS = (9, 10)  # fixes were tuned on KiCad 10; 9 works (the same file format family)
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` from the first version number in ``kicad-cli version`` output (``10.0.1-rc1`` etc.)."""
    match = _VERSION.search(text)
    return (int(match[1]), int(match[2]), int(match[3] or 0)) if match else None


@functools.cache
def cli_version() -> tuple[int, int, int] | None:
    """The installed ``kicad-cli`` version, None if there is no kicad-cli or it does not answer."""
    cli = find_kicad_cli()
    if cli is None:
        return None
    try:
        result = proc.run([cli, "version"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(result.stdout) if result.returncode == 0 else None


def version_warning() -> str | None:
    """A warning for a missing, unreadable or unsupported KiCad, None if it is a supported version."""
    if find_kicad_cli() is None:
        return None  # not installed: the commands that need it say so themselves
    version = cli_version()
    supported = " / ".join(str(m) for m in SUPPORTED_MAJORS)
    if version is None:
        return f"warning: could not read `kicad-cli version`; VibePCB supports KiCad {supported}"
    if version[0] not in SUPPORTED_MAJORS:
        found = ".".join(str(v) for v in version)
        return f"warning: KiCad {found} is not supported (KiCad {supported}; tuned on 10): DRC/ERC results and fixes may differ"
    return None


def _kicad_cli(*args: str) -> CompletedProcess[str]:
    cli = find_kicad_cli()
    if cli is None:
        msg = "kicad-cli not found - install KiCad (>= 9, e.g. `sudo pacman -S kicad`) or set $KICAD_CLI"
        raise KicadCliError(msg)
    return proc.run([cli, *args])


def assemble_project(kicad_zip: Path, target: Path, stem: str) -> None:
    """Unpack tscircuit's ``kicad_zip`` export into ``target`` as ``<stem>.kicad_*`` plus ``lib/``.

    The footprint library is rebuilt from the footprints embedded in the board
    (:func:`write_footprint_library`); no second tscircuit export is needed.
    """
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(kicad_zip) as zf:
        zf.extractall(target)
    # tscircuit names everything after its input (index.circuit.tsx, <board>.circuit.json)
    exported = next(target.glob("*.kicad_pro")).name.removesuffix(".kicad_pro")
    for f in target.glob(f"{exported}.*"):
        f.rename(target / f"{stem}{f.name.removeprefix(exported)}")

    pro_path = target / f"{stem}.kicad_pro"
    pro = json.loads(pro_path.read_text(encoding="utf-8"))
    pro["head"]["project_name"] = stem
    pro["schematic"]["last_opened_files"] = [f"{stem}.kicad_sch"]
    pro["board"]["last_opened_board"] = f"{stem}.kicad_pcb"
    pro_path.write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")

    write_footprint_library(sexpr.load(target / f"{stem}.kicad_pcb"), target / "lib" / "footprints" / LIBRARY_DIR)


LIBRARY_DIR = "tscircuit_builtin.pretty"
FOOTPRINT_NICKNAME = "tscircuit"


def _fmt(value: float) -> Sym:
    return Sym(f"{value:.6f}".rstrip("0").rstrip(".") or "0")


def _unrotate(node: list[Node], angle: float) -> None:
    """Subtract the footprint angle from an ``(at x y a)`` (angles are stored absolute on the board)."""
    if angle and len(node) > 3:
        node[3] = _fmt((float(str(node[3])) - angle) % 360)


def library_footprint(fp: list[Node]) -> list[Node]:
    """A board footprint as a library footprint: no placement, no nets, angles relative to the footprint."""
    lib = copy.deepcopy(fp)
    name = str(lib[1]).split(":", 1)[-1]
    lib[1] = name
    at = sexpr.child(lib, "at")
    angle = float(str(at[3])) if at is not None and len(at) > 3 else 0.0
    drop = {"at", "uuid", "path", "sheetname", "sheetfile", "locked", "tstamp"}
    lib[2:] = [n for n in lib[2:] if not (isinstance(n, list) and sexpr.head(n) in drop)]
    for node in lib[2:]:
        if not isinstance(node, list):
            continue
        if sexpr.head(node) == "property" and node[1] == "Reference":
            node[2] = "REF**"
        elif sexpr.head(node) == "property" and node[1] == "Value":
            node[2] = name
        if sexpr.head(node) in ("property", "fp_text", "pad"):
            place = sexpr.child(node, "at")
            if place is not None:
                _unrotate(place, angle)
            if sexpr.head(node) == "pad":
                node[:] = [
                    n for n in node if not (isinstance(n, list) and sexpr.head(n) in ("net", "pinfunction", "pintype"))
                ]
    return lib


def write_footprint_library(pcb: list[Node], pretty: Path) -> list[Path]:
    """One ``<name>.kicad_mod`` per distinct ``tscircuit:<name>`` footprint of the board into ``pretty``.

    The first top-side instance is used (a bottom-side one is mirrored; the name is skipped
    when it only occurs there, KiCad keeps the embedded copy on the board anyway).
    """
    found: dict[str, list[Node]] = {}
    for fp in sexpr.children(pcb, "footprint"):
        lib_id = str(fp[1])
        layer = sexpr.child(fp, "layer")
        if not lib_id.startswith(f"{FOOTPRINT_NICKNAME}:") or layer is None or layer[1] != "F.Cu":
            continue
        found.setdefault(lib_id.split(":", 1)[1], fp)
    pretty.mkdir(parents=True, exist_ok=True)
    written = []
    for name, fp in sorted(found.items()):
        path = pretty / f"{name}.kicad_mod"
        sexpr.dump(library_footprint(fp), path)
        written.append(path)
    return written


# --- checks -----------------------------------------------------------------------


@dataclass
class CheckResult:
    """Condensed DRC/ERC report; ``str()`` is meant to be read by an LLM."""

    name: str
    report_path: Path
    violations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(v.get("severity") == "error" for v in self.violations)

    @property
    def unconnected(self) -> int:
        return sum(v.get("type") == "unconnected_items" for v in self.violations)

    def unconnected_spots(self) -> list[tuple[float, float]]:
        """Positions of the items in unconnected-item reports (mm)."""
        return [
            (float(item["pos"]["x"]), float(item["pos"]["y"]))
            for v in self.violations
            if v.get("type") == "unconnected_items"
            for item in v.get("items", [])
            if "pos" in item
        ]

    def unconnected_pads(self) -> list[tuple[str, tuple[float, float]]]:
        """(net, position) of the pads in unconnected-item reports."""
        out = []
        for v in self.violations:
            if v.get("type") != "unconnected_items":
                continue
            for item in v.get("items", []):
                m = re.match(r"Pad \S+ \[([^\]]+)\]", str(item.get("description", "")))
                if m and "pos" in item:
                    out.append((m[1], (float(item["pos"]["x"]), float(item["pos"]["y"]))))
        return out

    def unconnected_nets(self) -> set[str]:
        """Nets of the items in unconnected-item reports."""
        return {
            m[1]
            for v in self.violations
            if v.get("type") == "unconnected_items"
            for item in v.get("items", [])
            if (m := re.search(r"\[([^\]]+)\]", str(item.get("description", ""))))
        }

    def spots_of(self, kind: str) -> list[tuple[float, float]]:
        """Positions of the items in reports of one violation type (mm)."""
        return [
            (float(item["pos"]["x"]), float(item["pos"]["y"]))
            for v in self.violations
            if v.get("type") == kind
            for item in v.get("items", [])
            if "pos" in item
        ]

    def __str__(self) -> str:
        counts = Counter(v.get("severity", "?") for v in self.violations)
        lines = [f"{self.name}: {sum(counts.values())} issue(s) {dict(counts)} - full report: {self.report_path}"]
        for v in self.violations:
            lines.append(f"  [{v.get('severity')}] {v.get('type')}: {v.get('description')}")
            for item in v.get("items", []):
                pos = item.get("pos", {})
                lines.append(f"      - {item.get('description')} @ ({pos.get('x')}, {pos.get('y')}) mm")
        return "\n".join(lines)


def run_drc(pcb: Path, report: Path) -> CheckResult:
    """Design rule check incl. unconnected items and schematic parity.

    Zones are refilled by KiCad first and the board is saved: tscircuit's pre-computed pour
    is a mosaic of small polygons with floating islands, KiCad's fill has proper thermal
    reliefs and island removal (and is what the Gerbers from ``export_fab`` then contain).
    """
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)  # a crashed run must not leave the last run's report to be read
    args = ["pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity", "--refill-zones", "--save-board"]
    args += ["-o", str(report), str(pcb)]
    result = _kicad_cli(*args)
    if not report.exists():
        msg = f"kicad-cli pcb drc failed:\n{result.stdout}{result.stderr}"
        raise KicadCliError(msg)
    data = json.loads(report.read_text(encoding="utf-8"))
    violations = []
    for key in ("violations", "unconnected_items", "schematic_parity"):
        violations += data.get(key, [])
    return CheckResult("DRC", report, violations)


def unconnected_nets(pcb: Path, report: Path) -> set[str]:
    """Nets KiCad reports unconnected items of, checked on a copy (DRC refills and saves the board)."""
    with tempfile.TemporaryDirectory() as tmp:
        for f in pcb.parent.glob(f"{pcb.stem}.kicad_*"):
            if f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
                shutil.copy(f, tmp)
        result = run_drc(Path(tmp) / pcb.name, report)
    return result.unconnected_nets()


def run_erc(sch: Path, report: Path) -> CheckResult:
    """Electrical rule check of the schematic."""
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)  # a crashed run must not leave the last run's report to be read
    result = _kicad_cli("sch", "erc", "--format", "json", "--severity-all", "-o", str(report), str(sch))
    if not report.exists():
        msg = f"kicad-cli sch erc failed:\n{result.stdout}{result.stderr}"
        raise KicadCliError(msg)
    data = json.loads(report.read_text(encoding="utf-8"))
    violations = [v for sheet in data.get("sheets", []) for v in sheet.get("violations", [])]
    return CheckResult("ERC", report, violations)


# BOM columns (KiCad field -> CSV header); LCSC/MPN are added by fixes.add_part_numbers
BOM_FIELDS = {
    "${QUANTITY}": "Qty",
    "Reference": "Designator",
    "Value": "Value",
    "Footprint": "Footprint",
    "MPN": "MPN",
    "LCSC": "LCSC Part #",
}


def _run(*args: str) -> None:
    result = _kicad_cli(*args)
    if result.returncode != 0:
        msg = f"kicad-cli {' '.join(args[:3])} failed:\n{result.stdout}{result.stderr}"
        raise KicadCliError(msg)


def export_schematic_svg(sch: Path, out_dir: Path) -> Path:
    """``kicad-cli sch export svg``: the schematic as ``out_dir/<stem>.svg`` (a preview to look at)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _run("sch", "export", "svg", "-o", str(out_dir), str(sch))
    return out_dir / f"{sch.stem}.svg"


def export_fab(pcb: Path, out_dir: Path, work_dir: Path) -> list[Path]:
    """The fab set in ``out_dir``, one file per purpose; returns the files written.

    ``<stem>-gerbers.zip`` (gerbers + drill), ``<stem>-bom.csv`` and ``<stem>-cpl.csv`` in JLCPCB's
    PCBA upload format (BOM with MPN too, see :func:`jlcpcb_files`), ``<stem>-schematic.pdf`` and
    top / bottom 3D renders. Intermediate KiCad exports (single gerbers, KiCad BOM / positions) go
    to ``work_dir``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.iterdir():  # earlier layouts left more files here
        if old.is_dir():
            shutil.rmtree(old)
        else:
            old.unlink()
    gerber_dir = work_dir / "gerbers"
    if gerber_dir.exists():
        shutil.rmtree(gerber_dir)
    gerber_dir.mkdir(parents=True)
    _run("pcb", "export", "gerbers", "-o", f"{gerber_dir}/", str(pcb))
    _run("pcb", "export", "drill", "-o", f"{gerber_dir}/", str(pcb))
    archive = out_dir / f"{pcb.stem}-gerbers.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(gerber_dir.iterdir()):
            zf.write(f, f.name)
    pos = work_dir / f"{pcb.stem}-pos.csv"
    _run("pcb", "export", "pos", "-o", str(pos), "--format", "csv", "--units", "mm", "--side", "both", "--exclude-dnp",
         str(pcb))  # fmt: skip
    bom = export_bom(pcb, work_dir / f"{pcb.stem}-bom-kicad.csv")
    return [
        archive,
        *jlcpcb_files(bom, pos, out_dir, pcb.stem),
        export_schematic_pdf(pcb, out_dir),
        *export_renders(pcb, out_dir),
    ]


def export_docs(pcb: Path, out_dir: Path) -> list[Path]:
    """BOM (CSV, grouped by part; first in the result), schematic PDF and top/bottom 3D renders."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bom = export_bom(pcb, out_dir / f"{pcb.stem}-bom.csv")
    return [bom, export_schematic_pdf(pcb, out_dir), *export_renders(pcb, out_dir)]


def export_bom(pcb: Path, target: Path) -> Path:
    """KiCad's BOM of the schematic, grouped by part, with LCSC / MPN columns."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        "sch", "export", "bom", "-o", str(target), "--fields", ",".join(BOM_FIELDS),
        "--labels", ",".join(BOM_FIELDS.values()), "--group-by", "Value,Footprint,LCSC,MPN",
        "--ref-range-delimiter", "", "--exclude-dnp", str(pcb.with_suffix(".kicad_sch")),
    )  # fmt: skip
    return target


def export_schematic_pdf(pcb: Path, out_dir: Path) -> Path:
    target = out_dir / f"{pcb.stem}-schematic.pdf"
    _run("sch", "export", "pdf", "-o", str(target), str(pcb.with_suffix(".kicad_sch")))
    return target


def export_renders(pcb: Path, out_dir: Path) -> list[Path]:
    """Top / bottom 3D renders (PNG: the only format kicad-cli renders to)."""
    renders = []
    for side in ("top", "bottom"):
        render = out_dir / f"{pcb.stem}-{side}.png"
        _run("pcb", "render", "-o", str(render), "--side", side, "--width", "1600", "--height", "1200", "--quality", "high",
             str(pcb))  # fmt: skip
        renders.append(render)
    return renders


def export_board_svg(pcb: Path, target: Path) -> Path:
    """A layout preview: top copper + silkscreen + outline, cropped to the board."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _run("pcb", "export", "svg", "-o", str(target), "--mode-single", "--layers", "F.Cu,F.SilkS,Edge.Cuts",
         "--exclude-drawing-sheet", "--fit-page-to-board", str(pcb))  # fmt: skip
    return target


_COPPER_LAYER = re.compile(r'\(\d+ "([^"]+\.Cu)" (?:signal|power|mixed|jumper)')


def copper_layers(pcb: Path) -> list[str]:
    """The board's copper layers in stack order (F.Cu, In1.Cu, ..., B.Cu)."""
    head = pcb.read_text(encoding="utf-8").split("(net ", 1)[0]
    layers = list(dict.fromkeys(_COPPER_LAYER.findall(head)))
    return sorted(layers, key=lambda n: (n != "F.Cu", n == "B.Cu", int(re.sub(r"\D", "", n) or 0)))


def export_layout(pcb: Path, out_dir: Path) -> list[Path]:
    """Layout for a first look: a PDF with one page per copper layer (board outline on each) and
    front / back SVGs (copper + silkscreen + outline; the back mirrored, as seen from below)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"{pcb.stem}-layout.pdf"
    _run("pcb", "export", "pdf", "-o", str(pdf), "--mode-multipage", "--layers", ",".join(copper_layers(pcb)),
         "--cl", "Edge.Cuts", str(pcb))  # fmt: skip
    written = [pdf]
    for side, layers, extra in (
        ("front", "F.Cu,F.SilkS,Edge.Cuts", []),
        ("back", "B.Cu,B.SilkS,Edge.Cuts", ["--mirror"]),
    ):
        svg = out_dir / f"{pcb.stem}-layout-{side}.svg"
        _run("pcb", "export", "svg", "-o", str(svg), "--mode-single", "--layers", layers, "--exclude-drawing-sheet",
             "--fit-page-to-board", *extra, str(pcb))  # fmt: skip
        written.append(svg)
    return written


PUBLISHED_PROJECT_FILES = (".kicad_pro", ".kicad_pcb", ".kicad_sch", ".kicad_dru")


def publish_project(kicad_dir: Path, stem: str, target: Path) -> list[Path]:
    """Copy the KiCad project (board, schematic, rules, footprint library) to ``target``.

    Left out: 3D models (tens of MB, fetched again by ``fix``), local settings (.kicad_prl),
    backups. KiCad opens the copy as it is; only its 3D view lacks the part models.
    """
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    written = []
    for suffix in PUBLISHED_PROJECT_FILES:
        src = kicad_dir / f"{stem}{suffix}"
        if src.exists():
            written.append(Path(shutil.copy2(src, target / src.name)))
    for name in ("fp-lib-table", "sym-lib-table"):
        if (kicad_dir / name).exists():
            written.append(Path(shutil.copy2(kicad_dir / name, target / name)))
    if (kicad_dir / "lib").is_dir():
        shutil.copytree(kicad_dir / "lib", target / "lib")
        written.append(target / "lib")
    return written


def jlcpcb_files(bom: Path, pos: Path, out_dir: Path, stem: str) -> list[Path]:
    """BOM (Comment/Designator/Footprint/LCSC Part #, plus MPN and Qty) and CPL
    (Designator/Mid X/Mid Y/Layer/Rotation) in JLCPCB's PCBA upload format, from the KiCad exports.

    Footprints without a reference (mounting holes) are left out of the CPL.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    jlc_bom, jlc_cpl = out_dir / f"{stem}-bom.csv", out_dir / f"{stem}-cpl.csv"
    with bom.open(encoding="utf-8", newline="") as src, jlc_bom.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.writer(dst)
        writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part #", "MPN", "Qty"])
        for row in csv.DictReader(src):
            writer.writerow([row["Value"], row["Designator"], row["Footprint"].split(":")[-1], row["LCSC Part #"],
                             row.get("MPN", ""), row.get("Qty", "")])  # fmt: skip
    with pos.open(encoding="utf-8", newline="") as src, jlc_cpl.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.writer(dst)
        writer.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for row in csv.DictReader(src):
            if row["Ref"]:
                side = "Top" if row["Side"] == "top" else "Bottom"
                writer.writerow(
                    [
                        row["Ref"],
                        f"{float(row['PosX']):.4f}mm",
                        f"{float(row['PosY']):.4f}mm",
                        side,
                        f"{float(row['Rot']):g}",
                    ]
                )
    return [jlc_bom, jlc_cpl]
