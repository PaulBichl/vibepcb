"""``vibepcb`` command line - the build/fix/check loop for LLM-designed boards.

vibepcb new <board>     scaffold boards/<board>/index.circuit.tsx
vibepcb build <board>   [--fast] tscircuit -> out/<board>/raw (KiCad zip, netlist, SVG/PNG previews)
vibepcb fix <board>     raw -> out/<board>/kicad, apply post-processing fixes
vibepcb check <board>   KiCad DRC + ERC -> out/<board>/reports (after refill: drop tracks the GND pour provides)
vibepcb all <board>     [--fast] build + fix + check
vibepcb fab <board>     part availability check, gerbers/drill zip, BOM, pick-and-place, schematic PDF, 3D renders
vibepcb list            list the boards under boards/
vibepcb docs            download tscircuit's LLM docs to docs/tscircuit-llms.txt
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from vibepcb import fixes, kicad, paths, schgen, tscircuit

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
    "schematic-svg": ".sch.svg",
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
    if fast:  # quick experiments: router effort 2x on a temp copy; final runs go without --fast
        print(f"--fast: autorouterEffortLevel forced to {tscircuit.FAST_EFFORT} (not for final runs)")
        source = tscircuit.fast_source(source, paths.OUT_DIR / board / "fast")
    raw = paths.raw_dir(board)
    circuit = raw / f"{board}.circuit.json"
    # placement gate: a placement-only evaluation takes about a minute and fails before the routing pass
    print(f"checking the placement of {board} (routing disabled) ...")
    errors, report = tscircuit.circuit_errors(
        tscircuit.evaluate_unrouted(source, raw / f"{board}.unrouted.circuit.json"), tscircuit.PLACEMENT_ERRORS
    )
    if errors:
        print(report)
        print(f"placement errors - fix {source.relative_to(paths.ROOT)} first", file=sys.stderr)
        return 1
    # tscircuit evaluates (places + routes) the board on every call: do it once, export the rest from the JSON
    print(f"building {board} with tscircuit (placement + routing) ...")
    tscircuit.export(source, "circuit-json", circuit)
    for change in tscircuit.repair_trace_nets(circuit):
        print(f"  re-linked route {change}")
    errors, report = tscircuit.circuit_errors(circuit)
    print(report)
    if errors:
        print(f"tscircuit found {errors} error(s) - fix {source.relative_to(paths.ROOT)} first", file=sys.stderr)
        return 1
    written = [circuit, *tscircuit.build(circuit, raw, board, JSON_FORMATS)]
    if shutil.which("rsvg-convert"):  # PNGs so an LLM can look at the board / schematic
        for svg in (raw / f"{board}.pcb.svg", raw / f"{board}.sch.svg"):
            subprocess.run(["rsvg-convert", "-w", "1200", "-o", str(svg.with_suffix(".png")), str(svg)], check=True)
            written.append(svg.with_suffix(".png"))
    for f in written:
        print(f"  {f.relative_to(paths.ROOT)}")
    return 0


def cmd_fix(board: str) -> int:
    raw, target = paths.raw_dir(board), paths.kicad_dir(board)
    kicad.assemble_project(raw / f"{board}.kicad.zip", target, board)
    sch, circuit = target / f"{board}.kicad_sch", raw / f"{board}.circuit.json"
    if not schgen.has_symbols(sch) and circuit.exists():  # schematicDisabled: netlist-style schematic
        schgen.write(circuit, sch, board)
        print(f"  schematic generated from {circuit.relative_to(paths.ROOT)} (boxes + net labels)")
    report = fixes.apply_all(fixes.KicadProject.load(target, board))
    for name, changes in report.items():
        for change in changes:
            print(f"  fix {name}: {change}")
    print(f"KiCad project: {(target / f'{board}.kicad_pro').relative_to(paths.ROOT)}")
    return 0


POST_FILL_ATTEMPTS = 4


def cmd_check(board: str) -> int:
    project, reports = paths.kicad_dir(board), paths.OUT_DIR / board / "reports"
    pcb = project / f"{board}.kicad_pcb"
    with ThreadPoolExecutor(max_workers=2) as pool:  # independent kicad-cli processes
        erc_job = pool.submit(kicad.run_erc, project / f"{board}.kicad_sch", reports / "erc.json")
        drc_job = pool.submit(kicad.run_drc, pcb, reports / "drc.json")
        erc, drc = erc_job.result(), drc_job.result()
    # fixes that need KiCad's zone fill; refill + DRC again. Where that loses a connection,
    # retry keeping the tracks around the reported spots; give up (restore) after a few tries
    backup, before = pcb.read_bytes(), drc.unconnected
    keep: list[tuple[float, float]] = []
    for _ in range(POST_FILL_ATTEMPTS):
        changes = fixes.apply_post_fill(fixes.KicadProject.load(project, board), keep)
        if not changes:
            break
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
    loaded = fixes.KicadProject.load(project, board)
    corners = fixes.sharp_corners(loaded)
    unpoured = fixes.unpoured_copper_layers(loaded)
    print(erc)
    print(drc)
    print(f"Track bends: {len(corners)} sharper than 135 deg")
    for net, (x, y), angle in corners:
        print(f"  [error] sharp_corner: {net} bend of {angle:.1f} deg @ ({x:.3f}, {y:.3f}) mm")
    print(f"Pours: {len(unpoured)} copper layer(s) without a plane")
    for layer in unpoured:
        print(f"  [error] no_pour: {layer} - add a <copperpour> (GND or a supply net) on it")
    return 1 if erc.errors or drc.errors or corners or unpoured else 0


def cmd_fab(board: str) -> int:
    # build/export run with the parts engine off (no network lookups); do the availability check once, here
    source = paths.board_source(board)
    if source.exists():
        print("checking part availability (tscircuit parts engine) ...")
        warnings = tscircuit.availability_warnings(source)
        print("\n".join(f"  {w}" for w in warnings) if warnings else "  all parts available")
    for f in kicad.export_fab(paths.kicad_dir(board) / f"{board}.kicad_pcb", paths.OUT_DIR / board / "fab"):
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
    for name in ("new", "build", "fix", "check", "all", "fab"):
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
                handler = {"new": cmd_new, "fix": cmd_fix, "check": cmd_check, "fab": cmd_fab}
                return handler[command](args.board)
    except (tscircuit.TscircuitError, kicad.KicadCliError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
