"""``vibepcb`` command line - the build/fix/check loop for LLM-designed boards.

vibepcb new <board>     scaffold boards/<board>/index.circuit.tsx
vibepcb build <board>   [--fast] tscircuit -> out/<board>/raw (KiCad zip, netlist, SVG/PNG previews)
vibepcb fix <board>     raw -> out/<board>/kicad, apply post-processing fixes
vibepcb check <board>   KiCad DRC + ERC -> out/<board>/reports (after refill: drop tracks the GND pour provides)
vibepcb all <board>     [--fast] build + fix + check
vibepcb fab <board>     part availability check, gerbers/drill zip, BOM, pick-and-place, schematic PDF, 3D renders
vibepcb publish <board> boards/<board>/hardware/: schematic + layout PDF/PNG, renders, BOM, KiCad project (no fab files)
vibepcb list            list the boards under boards/
vibepcb docs            download tscircuit's LLM docs to docs/tscircuit-llms.txt
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from vibepcb import config, fixes, kicad, paths, schgen, sexpr, tscircuit

if TYPE_CHECKING:
    from pathlib import Path

SCHEMATIC_PNG_WIDTH = 4000  # px: an A0 sheet stays readable when zoomed into a crop
TSCIRCUIT_LLM_DOCS = "https://docs.tscircuit.com/llms.txt"

TEMPLATE = """\
export default () => (
  <board width="30mm" height="20mm">
    <resistor name="R1" resistance="1k" footprint="0603" pcbX={-3} pcbY={0} />
    <capacitor name="C1" capacitance="100nF" footprint="0603" pcbX={3} pcbY={0} />
    <trace from=".R1 > .pin2" to=".C1 > .pin1" />
  </board>
)
"""

# exported from the circuit JSON (no re-evaluation / re-routing); the footprint library is rebuilt from the board
JSON_FORMATS = {
    "kicad_zip": ".kicad.zip",
    "readable-netlist": ".netlist.txt",
    "pcb-svg": ".pcb.svg",
}


def cmd_new(board: str) -> int:
    source = paths.board_source(board)
    if source.exists():
        print(f"{source} already exists", file=sys.stderr)
        return 1
    source.parent.mkdir(parents=True)
    source.write_text(TEMPLATE, encoding="utf-8")
    print(f"created {source.relative_to(paths.ROOT)}")
    return 0


def cmd_build(board: str, *, fast: bool = False) -> int:
    source = paths.board_source(board)
    if not source.exists():
        print(f"no such board: {source} (boards: {', '.join(paths.list_boards())})", file=sys.stderr)
        return 1
    if fast:  # quick experiments: router effort 2x; final runs go without --fast
        print(f"--fast: autorouterEffortLevel forced to {tscircuit.FAST_EFFORT} (not for final runs)")
    # evaluated as a copy without tscircuit's schematic (`fix` generates it from the circuit JSON)
    board_tsx = source
    source = tscircuit.prepared_source(
        source, paths.work_dir(board) / "src", effort=tscircuit.FAST_EFFORT if fast else None
    )
    raw = paths.raw_dir(board)
    circuit = raw / f"{board}.circuit.json"
    # placement gate: a placement-only evaluation takes about a minute and fails before the routing pass
    print(f"checking the placement of {board} (routing disabled) ...")
    errors, report = tscircuit.circuit_errors(
        tscircuit.evaluate_unrouted(source, raw / f"{board}.unrouted.circuit.json"), tscircuit.PLACEMENT_ERRORS
    )
    if errors:
        print(report)
        print(f"placement errors - fix {board_tsx.relative_to(paths.ROOT)} first", file=sys.stderr)
        return 1
    # tscircuit evaluates (places + routes) the board on every call: do it once, export the rest from the JSON
    print(f"building {board} with tscircuit (placement + routing) ...")
    tscircuit.export(source, "circuit-json", circuit)
    for change in tscircuit.repair_trace_nets(circuit):
        print(f"  re-linked route {change}")
    errors, report = tscircuit.routed_errors(circuit)
    print(report)
    if errors:
        print(f"tscircuit found {errors} error(s) - fix {board_tsx.relative_to(paths.ROOT)} first", file=sys.stderr)
        return 1
    tscircuit.build(circuit, raw, board, JSON_FORMATS)
    if shutil.which("rsvg-convert"):  # the routed board as tscircuit left it, for an agent to look at
        _png(raw / f"{board}.pcb.svg", paths.preview_dir(board) / "routed.png", 1200)
    print(f"  tscircuit output: {raw.relative_to(paths.ROOT)}/ (internal; `fix` makes the KiCad project)")
    return 0


def _png(svg: Path, png: Path, width: int) -> None:
    png.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsvg-convert", "-w", str(width), "-b", "white", "-o", str(png), str(svg)], check=True)


def cmd_fix(board: str) -> int:
    raw, target = paths.raw_dir(board), paths.kicad_dir(board)
    kicad.assemble_project(raw / f"{board}.kicad.zip", target, board)
    sch, circuit = target / f"{board}.kicad_sch", raw / f"{board}.circuit.json"
    if circuit.exists():  # builds skip tscircuit's schematic (does not scale); generate it from the JSON
        for note in schgen.write(circuit, sch, board, paths.board_source(board)):
            print(f"  schematic: {note}")
        print(f"  schematic generated from {circuit.relative_to(paths.ROOT)}")
    project = fixes.KicadProject.load(target, board)
    project.config = config.load(board)
    report = fixes.apply_all(project)
    for name, changes in report.items():
        for change in changes:
            print(f"  fix {name}: {change}")
    # safety net: nets KiCad finds broken after the track fixes go back to their routed state
    report_path = paths.reports_dir(board) / "fix-drc.json"
    lost = kicad.unconnected_nets(project.pcb_path, report_path)
    restored = fixes.restore_nets(project, lost)
    for change in restored:
        print(f"  fix restore_nets: {change}")
    project.save()
    if restored:  # the routed geometry of a restored net can clash with its cleaned-up neighbours
        before = copy.deepcopy(project.pcb)
        repairs = fixes.relocate_vias(project) + fixes.push_tracks_clear(project)
        if repairs:
            project.save()
            if broken := kicad.unconnected_nets(project.pcb_path, report_path) - lost:  # broke something: undo
                project.pcb[:] = before
                project.save()
                print(f"  fix after restore: undone, it disconnected {', '.join(sorted(broken))}")
            else:
                for change in repairs:
                    print(f"  fix after restore: {change}")
    if kicad.find_kicad_cli():  # preview to look at: SVG for people, PNG for an agent
        svg = kicad.export_schematic_svg(sch, paths.work_dir(board))
        svg = svg.replace(paths.board_out(board) / "schematic.svg")
        if shutil.which("rsvg-convert"):
            _png(svg, paths.preview_dir(board) / "schematic.png", SCHEMATIC_PNG_WIDTH)
        print(f"  schematic: {svg.relative_to(paths.ROOT)}")
    print(f"KiCad project: {(target / f'{board}.kicad_pro').relative_to(paths.ROOT)}")
    _write_index(board)
    return 0


POST_FILL_ATTEMPTS = 4


def cmd_check(board: str) -> int:
    project, reports = paths.kicad_dir(board), paths.reports_dir(board)
    pcb = project / f"{board}.kicad_pcb"
    with ThreadPoolExecutor(max_workers=2) as pool:  # independent kicad-cli processes
        erc_job = pool.submit(kicad.run_erc, project / f"{board}.kicad_sch", reports / "erc.json")
        drc_job = pool.submit(kicad.run_drc, pcb, reports / "drc.json")
        erc, drc = erc_job.result(), drc_job.result()
    # fixes that need KiCad's zone fill; refill + DRC again. Where that loses a connection,
    # retry keeping the tracks around the reported spots, then prune only the tracks at acute
    # angles; give up (restore) after a few tries
    backup, before = pcb.read_bytes(), drc.unconnected
    keep: list[tuple[float, float]] = []
    acute = drc.spots_of("track_angle")  # the fallback prunes only the tracks at acute angles
    for attempt in range(POST_FILL_ATTEMPTS):
        only = acute if attempt >= POST_FILL_ATTEMPTS // 2 else None
        changes = fixes.apply_post_fill(fixes.KicadProject.load(project, board), keep, only)
        if not changes:
            break
        drc = kicad.run_drc(pcb, reports / "drc.json")
        if drc.unconnected > before and attempt == POST_FILL_ATTEMPTS - 1:  # last resort: broken nets go back
            loaded = fixes.KicadProject.load(project, board)
            loaded.before_track_fixes = sexpr.loads(backup.decode("utf-8"))
            changes += fixes.restore_nets(loaded, drc.unconnected_nets())
            loaded.save()
            drc = kicad.run_drc(pcb, reports / "drc.json")
        if drc.unconnected <= before:
            for change in changes:
                print(f"  post-fill: {change}")
            break
        keep += drc.unconnected_spots()
        pcb.write_bytes(backup)
    else:
        print(f"  post-fill: gave up after {POST_FILL_ATTEMPTS} attempts, board left as filled")
        drc = kicad.run_drc(pcb, reports / "drc.json")
    if pads := drc.unconnected_pads():  # pads their pour can't reach: a via to the net's plane
        loaded, stitched = fixes.KicadProject.load(project, board), pcb.read_bytes()
        changes = fixes.stitch_unconnected_pads(loaded, pads)
        if any("stitching via" in c for c in changes):
            loaded.save()
            fewer = kicad.run_drc(pcb, reports / "drc.json")
            if fewer.unconnected < drc.unconnected:
                drc = fewer
            else:
                pcb.write_bytes(stitched)
                drc = kicad.run_drc(pcb, reports / "drc.json")
        for change in changes:
            print(f"  post-fill: {change}")
    loaded = fixes.KicadProject.load(project, board)
    corners = fixes.sharp_corners(loaded)
    unpoured = fixes.unpoured_copper_layers(loaded)
    summary = [str(erc), str(drc), f"Track bends: {len(corners)} sharper than 135 deg"]
    summary += [
        f"  [error] sharp_corner: {net} bend of {a:.1f} deg @ ({x:.3f}, {y:.3f}) mm" for net, (x, y), a in corners
    ]
    summary += [f"  [warning] unlabelled_connector: {w}" for w in fixes.unlabelled_connectors(loaded)]
    summary.append(f"Pours: {len(unpoured)} copper layer(s) without a plane")
    summary += [f"  [error] no_pour: {layer} - add a <copperpour> (GND or a supply net) on it" for layer in unpoured]
    passed = not (erc.errors or drc.errors or corners or unpoured)
    _write_check_status(board, pcb, passed)
    verdict = "PASSED" if passed else f"FAILED ({erc.errors + drc.errors + len(corners) + len(unpoured)} error(s))"
    summary.append(f"check: {verdict}")
    print("\n".join(summary))
    (paths.board_out(board) / "check.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    svg = kicad.export_board_svg(pcb, paths.board_out(board) / "board.svg")  # the filled, final board
    if shutil.which("rsvg-convert"):
        _png(svg, paths.preview_dir(board) / "board.png", BOARD_PNG_WIDTH)
    _write_index(board)
    return 0 if passed else 1


BOARD_PNG_WIDTH = 2400


def _write_index(board: str) -> None:
    """``out/<board>/README.md``: what to open, the check verdict, the fab files - one place to start."""
    out = paths.board_out(board)
    check = out / "check.txt"
    verdict = check.read_text(encoding="utf-8").splitlines()[-1] if check.exists() else "check: not run yet"
    lines = [
        f"# {board}",
        "",
        f"Generated by VibePCB from `boards/{board}/` - don't edit here; rebuild with `hatch run pcb all {board}`.",
        "",
        f"- **{verdict}** - details: [check.txt](check.txt)",
        f"- **KiCad project**: [kicad/{board}.kicad_pro](kicad/{board}.kicad_pro) (open in KiCad)",
        "- **Previews**: [board.svg](board.svg) (top copper + silkscreen), [schematic.svg](schematic.svg)",
    ]
    fab = out / "fab"
    if fab.is_dir() and any(fab.iterdir()):
        files = ", ".join(f"[{f.name}](fab/{f.name})" for f in sorted(fab.iterdir()))
        lines.append(f"- **Fab** (JLCPCB upload: gerbers zip, BOM, CPL): {files}")
    else:
        lines.append(f"- **Fab**: not made yet - `hatch run pcb fab {board}` (only for a board that passes check)")
    lines += ["", "Internals (tscircuit output, JSON reports, PNG previews for the agent) are in `.work/`."]
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _board_hash(pcb: Path) -> str:
    return hashlib.sha256(pcb.read_bytes()).hexdigest()


def _write_check_status(board: str, pcb: Path, passed: bool) -> None:
    """``reports/check.json``: the verdict of the last ``check`` and the board it was for."""
    status = {"passed": passed, "board_sha256": _board_hash(pcb)}
    (paths.reports_dir(board) / "check.json").write_text(json.dumps(status, indent=2), encoding="utf-8")


def _check_passed(board: str, pcb: Path) -> bool:
    """Whether the last ``check`` passed on the board as it is now."""
    path = paths.reports_dir(board) / "check.json"
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(status.get("passed")) and status.get("board_sha256") == _board_hash(pcb)


def cmd_fab(board: str) -> int:
    # build/export run with the parts engine off (no network lookups); do the availability check once, here
    source = paths.board_source(board)
    if source.exists():
        print("checking part availability (tscircuit parts engine) ...")
        warnings = tscircuit.availability_warnings(tscircuit.prepared_source(source, paths.work_dir(board) / "src"))
        print("\n".join(f"  {w}" for w in warnings) if warnings else "  all parts available")
    pcb = paths.kicad_dir(board) / f"{board}.kicad_pcb"
    for f in kicad.export_fab(pcb, paths.board_out(board) / "fab", paths.work_dir(board) / "fab"):
        print(f"  {f.relative_to(paths.ROOT)}")
    _write_index(board)
    return 0


def cmd_publish(board: str) -> int:
    """Copy what a visitor needs for a first look into ``boards/<board>/hardware/`` (committed):
    schematic PDF, layout PDF + PNGs, 3D renders, BOM and the KiCad project - no fab files."""
    project = paths.kicad_dir(board)
    pcb = project / f"{board}.kicad_pcb"
    if not _check_passed(board, pcb):
        print(f"error: `pcb check {board}` has not passed on this board - run it (and fix the board) first",
              file=sys.stderr)  # fmt: skip
        return 1
    target = paths.board_source(board).parent / "hardware"
    written = kicad.publish_project(project, board, target / "kicad")
    written += kicad.export_docs(pcb, target)
    written += kicad.export_layout(pcb, target)
    for f in written:
        print(f"  {f.relative_to(paths.ROOT)}")
    return 0


def cmd_docs() -> int:
    target = paths.DOCS_DIR / "tscircuit-llms.txt"
    target.parent.mkdir(exist_ok=True)
    with urllib.request.urlopen(TSCIRCUIT_LLM_DOCS, timeout=60) as response:
        target.write_bytes(response.read())
    print(f"saved {target.relative_to(paths.ROOT)} - grep it for component props/examples")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vibepcb", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("new", "build", "fix", "check", "all", "fab", "publish"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("board")
        if name in ("build", "all"):
            command_parser.add_argument("--fast", action="store_true", help="router effort 2x (quick experiments)")
    sub.add_parser("list")
    sub.add_parser("docs")
    args = parser.parse_args(argv)

    try:
        match args.command:
            case "list":
                print("\n".join(paths.list_boards()))
                return 0
            case "docs":
                return cmd_docs()
            case "all":
                return cmd_build(args.board, fast=args.fast) or cmd_fix(args.board) or cmd_check(args.board)
            case command:
                if command == "build":
                    return cmd_build(args.board, fast=args.fast)
                handler = {"new": cmd_new, "fix": cmd_fix, "check": cmd_check, "fab": cmd_fab, "publish": cmd_publish}
                return handler[command](args.board)
    except (tscircuit.TscircuitError, kicad.KicadCliError, config.ConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
