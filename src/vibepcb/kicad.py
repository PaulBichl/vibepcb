"""KiCad side: assemble a project from tscircuit's export and run ``kicad-cli`` checks."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

TSCIRCUIT_STEM = "index.circuit"  # tscircuit names everything after the entry file


class KicadCliError(RuntimeError):
    pass


def find_kicad_cli() -> str | None:
    return os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")


def _kicad_cli(*args: str) -> subprocess.CompletedProcess[str]:
    cli = find_kicad_cli()
    if cli is None:
        msg = "kicad-cli not found - install KiCad (>= 9, e.g. `sudo pacman -S kicad`) or set $KICAD_CLI"
        raise KicadCliError(msg)
    return subprocess.run([cli, *args], capture_output=True, text=True, check=False)


def assemble_project(kicad_zip: Path, library_dir: Path, target: Path, stem: str) -> None:
    """Unpack tscircuit's ``kicad_zip`` export into ``target`` as ``<stem>.kicad_*`` plus ``lib/``."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(kicad_zip) as zf:
        zf.extractall(target)
    for f in target.glob(f"{TSCIRCUIT_STEM}.*"):
        f.rename(target / f"{stem}{f.name.removeprefix(TSCIRCUIT_STEM)}")

    pro_path = target / f"{stem}.kicad_pro"
    pro = json.loads(pro_path.read_text(encoding="utf-8"))
    pro["head"]["project_name"] = stem
    pro["schematic"]["last_opened_files"] = [f"{stem}.kicad_sch"]
    pro["board"]["last_opened_board"] = f"{stem}.kicad_pcb"
    pro_path.write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")

    if library_dir.exists():
        shutil.copytree(library_dir, target / "lib", ignore=shutil.ignore_patterns("*-lib-table"))


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


def run_erc(sch: Path, report: Path) -> CheckResult:
    """Electrical rule check of the schematic."""
    report.parent.mkdir(parents=True, exist_ok=True)
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


def export_fab(pcb: Path, out_dir: Path) -> list[Path]:
    """Manufacturing + documentation outputs into ``out_dir``; returns the files written.

    gerbers + drill (zipped), BOM (CSV, grouped by part), pick-and-place (CSV, mm),
    schematic PDF, top/bottom 3D renders of the board, and the JLCPCB upload set in
    ``jlcpcb/`` (see :func:`jlcpcb_files`).
    """
    sch = pcb.with_suffix(".kicad_sch")
    gerber_dir = out_dir / "gerbers"
    if gerber_dir.exists():
        shutil.rmtree(gerber_dir)
    gerber_dir.mkdir(parents=True)
    _run("pcb", "export", "gerbers", "-o", f"{gerber_dir}/", str(pcb))
    _run("pcb", "export", "drill", "-o", f"{gerber_dir}/", str(pcb))
    archive = out_dir / f"{pcb.stem}-gerbers.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(gerber_dir.iterdir()):
            zf.write(f, f.name)

    bom, pos, sch_pdf = (
        out_dir / f"{pcb.stem}-bom.csv",
        out_dir / f"{pcb.stem}-pos.csv",
        out_dir / f"{pcb.stem}-schematic.pdf",
    )
    _run(
        "sch", "export", "bom", "-o", str(bom), "--fields", ",".join(BOM_FIELDS), "--labels", ",".join(BOM_FIELDS.values()),
        "--group-by", "Value,Footprint,LCSC,MPN", "--ref-range-delimiter", "", "--exclude-dnp", str(sch),
    )  # fmt: skip
    _run(
        "pcb",
        "export",
        "pos",
        "-o",
        str(pos),
        "--format",
        "csv",
        "--units",
        "mm",
        "--side",
        "both",
        "--exclude-dnp",
        str(pcb),
    )
    _run("sch", "export", "pdf", "-o", str(sch_pdf), str(sch))
    renders = []
    for side in ("top", "bottom"):
        render = out_dir / f"{pcb.stem}-{side}.png"
        _run(
            "pcb",
            "render",
            "-o",
            str(render),
            "--side",
            side,
            "--width",
            "1600",
            "--height",
            "1200",
            "--quality",
            "high",
            str(pcb),
        )
        renders.append(render)
    return [archive, bom, pos, sch_pdf, *renders, *jlcpcb_files(bom, pos, archive, out_dir / "jlcpcb")]


def jlcpcb_files(bom: Path, pos: Path, gerbers: Path, out_dir: Path) -> list[Path]:
    """JLCPCB's PCBA upload set: gerber zip, BOM (Comment/Designator/Footprint/LCSC Part #)
    and CPL (Designator/Mid X/Mid Y/Layer/Rotation), from the KiCad exports.

    Footprints without a reference (mounting holes) are left out of the CPL.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = gerbers.stem.removesuffix("-gerbers")
    jlc_bom, jlc_cpl, jlc_zip = (
        out_dir / f"{stem}-bom-jlcpcb.csv",
        out_dir / f"{stem}-cpl-jlcpcb.csv",
        out_dir / gerbers.name,
    )
    with bom.open(encoding="utf-8", newline="") as src, jlc_bom.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.writer(dst)
        writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for row in csv.DictReader(src):
            writer.writerow([row["Value"], row["Designator"], row["Footprint"].split(":")[-1], row["LCSC Part #"]])
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
    shutil.copyfile(gerbers, jlc_zip)
    return [jlc_zip, jlc_bom, jlc_cpl]
