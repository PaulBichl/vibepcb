"""``vibepcb`` command line - the build/fix/check loop for LLM-designed boards.

vibepcb new <board>     scaffold boards/<board>/index.circuit.tsx
vibepcb build <board>   [--fast] tscircuit -> out/<board>/raw (KiCad zip, netlist, SVG/PNG previews)
vibepcb fix <board>     raw -> out/<board>/kicad, apply post-processing fixes
vibepcb route <board>   [--only NET] [--dry-run] routes.toml -> tracks on the KiCad board (seconds, no rebuild)
vibepcb inspect <board> [--area x0 y0 x1 y1] [--net PAT] [--layer L] [--scale PX/MM] [--source unrouted]  PNG of the board
vibepcb ratsnest <board> [--source unrouted]  unrouted links (longest first), crossings, worst 10 mm cells
vibepcb place-score <board>..  total ratsnest length and crossings per board (compare `clone` variants)
vibepcb check <board>   KiCad DRC + ERC -> out/<board>/reports (after refill: drop tracks the GND pour provides)
vibepcb all <board>     [--fast] build + fix + check
vibepcb fab <board>     part availability check, gerbers/drill zip, BOM, pick-and-place, schematic PDF, 3D renders
vibepcb publish <board> boards/<board>/hardware/: schematic + layout PDF/PNG, renders, BOM, KiCad project (no fab files)
vibepcb padtable <board> REF..  boards/<board>/pads.ts: pad positions from the placement build (pin plans)
vibepcb rules <board>   the merged design / manufacturing rule profiles ([rules] in vibepcb.toml), each value with its source
vibepcb clone <board> <new>  copy a board and its build (variants to compare, no rebuild)
vibepcb list            list the boards under boards/
vibepcb docs            download tscircuit's LLM docs to docs/tscircuit-llms.txt
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from vibepcb import board as board_model
from vibepcb import (
    config,
    fanout,
    fixes,
    inspect,
    kicad,
    padtable,
    paths,
    ratsnest,
    routes,
    rules,
    schgen,
    sexpr,
    tscircuit,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    if escape := config.load(board).get("escape"):  # BGA escapes by VibePCB: tscircuit routes to their ends
        unrouted = raw / f"{board}.unrouted.circuit.json"
        planes = frozenset(config.load(board).get("planes", {}).get("nets", []))
        plans = fanout.plan_board(unrouted, escape, planes)
        if problems := [v for p in plans for v in fanout.violations(p)]:
            print("\n".join(f"  {v}" for v in problems))
            print(f"escape plan has {len(problems)} problem(s) - see [escape] in vibepcb.toml", file=sys.stderr)
            return 1
        fanout.prepare_source(source, unrouted, plans)
        fanout.save(plans, fanout_path := raw / f"{board}.escapes.json")
        for p in plans:
            vias = sum(len(e.vias) for e in p.escapes)
            print(f"  {p.ref}: {len(p.escapes)} balls escaped ({vias} via dogbones), tscircuit routes to their ends")
        print(f"  escape plan: {fanout_path.relative_to(paths.ROOT)}")
    if config.load(board).get("routing", {}).get("mode") == "manual":  # routes.toml routes, tscircuit only places
        print(f"building {board} with tscircuit (placement only; routing: routes.toml) ...")
        tscircuit.evaluate_unrouted(source, circuit)
        tscircuit.build(circuit, raw, board, JSON_FORMATS)
        if shutil.which("rsvg-convert"):
            _png(raw / f"{board}.pcb.svg", paths.preview_dir(board) / "routed.png", 1200)
        print(
            f"  tscircuit output: {raw.relative_to(paths.ROOT)}/ (internal; `fix` makes the KiCad project and routes it)"
        )
        return 0
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


@contextlib.contextmanager
def _phase(label: str) -> Iterator[None]:
    """Print how long a pipeline step took (the agent sees where a run spends its minutes)."""
    started = time.monotonic()
    yield
    print(f"  time: {label} {time.monotonic() - started:.0f} s")


def cmd_fix(board: str) -> int:
    os.environ.setdefault("VIBEPCB_VERBOSE", "1")  # routing progress (routes.toml) on stderr
    raw, target = paths.raw_dir(board), paths.kicad_dir(board)
    kicad.assemble_project(raw / f"{board}.kicad.zip", target, board)
    sch, circuit = target / f"{board}.kicad_sch", raw / f"{board}.circuit.json"
    if circuit.exists():  # builds skip tscircuit's schematic (does not scale); generate it from the JSON
        with _phase("schematic"):
            for note in schgen.write(circuit, sch, board, paths.board_source(board)):
                print(f"  schematic: {note}")
        print(f"  schematic generated from {circuit.relative_to(paths.ROOT)}")
    project = fixes.KicadProject.load(target, board)
    project.config = config.load(board)
    project.routes = routes.load(paths.board_source(board).parent / routes.ROUTES_NAME)
    with _phase("fixes"):
        report = fixes.apply_all(project)
    for name, changes in report.items():
        for change in changes:
            print(f"  fix {name}: {change}")
    # safety net: nets KiCad finds broken after the track fixes go back to their routed state
    report_path = paths.reports_dir(board) / "fix-drc.json"
    with _phase("KiCad connectivity check"):
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
        with _phase("schematic preview"):
            svg = kicad.export_schematic_svg(sch, paths.work_dir(board))
        svg = svg.replace(paths.board_out(board) / "schematic.svg")
        if shutil.which("rsvg-convert"):
            _png(svg, paths.preview_dir(board) / "schematic.png", SCHEMATIC_PNG_WIDTH)
        print(f"  schematic: {svg.relative_to(paths.ROOT)}")
    print(f"KiCad project: {(target / f'{board}.kicad_pro').relative_to(paths.ROOT)}")
    _write_index(board)
    return 0


def cmd_route(board: str, *, only: str | None = None, dry_run: bool = False, drc: bool = False) -> int:
    """Quick loop: ``routes.toml`` onto the KiCad board as ``fix`` had it before routing, without the clean-up fixes."""
    spec = routes.load(paths.board_source(board).parent / routes.ROUTES_NAME)
    if not spec:
        print(f"no {routes.ROUTES_NAME} for {board}", file=sys.stderr)
        return 1
    target = paths.kicad_dir(board)
    pcb = target / f"{board}.kicad_pcb"
    snapshot = paths.work_dir(board) / fixes.UNROUTED_NAME  # written by `fix` (manual mode) before it routes
    if not snapshot.exists():
        print(f"{snapshot.relative_to(paths.ROOT)} missing - run `pcb fix {board}` first ([routing] mode = \"manual\")",
              file=sys.stderr)  # fmt: skip
        return 1
    project = fixes.KicadProject.load(target, board)
    project.pcb = sexpr.load(snapshot)
    project.config = config.load(board)
    report = routes.apply(project, spec, only=only)
    for r in report.results:
        print(f"  {'routed' if r.ok else 'FAILED'} {r.entry}: {r.detail}")
    print(f"{len(report.results) - len(report.failed)} routed, {len(report.failed)} failed")
    if not dry_run:
        sexpr.dump(project.pcb, pcb)
        print(f"saved {pcb.relative_to(paths.ROOT)}")
    if drc:  # a quick verdict without fix + check: KiCad's DRC on a copy (no clean-up fixes, so a rough count)
        with tempfile.TemporaryDirectory() as tmp, _phase("KiCad DRC"):
            for f in target.glob(f"{board}.kicad_*"):
                shutil.copy(f, tmp)
            sexpr.dump(project.pcb, Path(tmp) / f"{board}.kicad_pcb")
            result = kicad.run_drc(Path(tmp) / f"{board}.kicad_pcb", Path(tmp) / "drc.json")
        counts = Counter(str(v.get("type")) for v in result.violations if v.get("severity") == "error")
        print(f"DRC errors (before the clean-up fixes): {sum(counts.values())}")
        for kind, n in counts.most_common():
            print(f"  {n} x {kind}")
    return 1 if report.failed else 0


def cmd_clone(board: str, new: str) -> int:
    """A copy of a board (source and, if there is one, its build) to try a variant: e.g. other routes.toml
    settings, run in parallel with `pcb fix <new>` + `pcb check <new>`, no rebuild."""
    source, target = paths.board_source(board).parent, paths.board_source(new).parent
    if target.exists():
        print(f"{target.relative_to(paths.ROOT)} exists already", file=sys.stderr)
        return 1
    shutil.copytree(source, target)
    raw = paths.raw_dir(board)
    if raw.is_dir():
        new_raw = paths.raw_dir(new)
        new_raw.mkdir(parents=True, exist_ok=True)
        for f in raw.iterdir():
            if f.is_file():
                name = new + f.name.removeprefix(board) if f.name.startswith(board + ".") else f.name
                shutil.copy(f, new_raw / name)
        print(f"cloned {board} -> {new} (source and build: `pcb fix {new}` next)")
    else:
        print(f"cloned {board} -> {new} (source only: `pcb build {new}` next)")
    return 0


def cmd_padtable(board: str, refs: list[str]) -> int:
    """``boards/<board>/pads.ts``: board positions of the parts' pads from the last placement build (pin plans)."""
    circuit = paths.raw_dir(board) / f"{board}.unrouted.circuit.json"
    if not circuit.exists():
        print(f"{circuit.relative_to(paths.ROOT)} missing - run `pcb build {board}` first", file=sys.stderr)
        return 1
    pads, centres = padtable.pad_positions(circuit, refs)
    target = paths.board_source(board).parent / "pads.ts"
    target.write_text(padtable.to_typescript(pads, centres), encoding="utf-8")
    print(f"wrote {target.relative_to(paths.ROOT)}: " + ", ".join(f"{r} ({len(pads[r])} pads)" for r in refs))
    return 0


def cmd_inspect(
    board: str, area: list[float] | None, net: str | None, layers: list[str] | None, scale: float, source: str
) -> int:
    """PNG of the board (or an area) with a mm grid and pad labels into ``.work/preview/inspect.png``."""
    target = paths.kicad_dir(board)
    project = fixes.KicadProject.load(target, board)
    if source == "unrouted":
        project.pcb = sexpr.load(paths.work_dir(board) / fixes.UNROUTED_NAME)
    project.config = config.load(board)
    model = board_model.Board.from_project(project)
    if area is None:
        xs = [p for e in model.edges for p in (e.points[0][0], e.points[-1][0])]
        ys = [p for e in model.edges for p in (e.points[0][1], e.points[-1][1])]
        area = [min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1]
    box = (area[0], area[1], area[2], area[3])
    svg = paths.preview_dir(board) / "inspect.svg"
    svg.parent.mkdir(parents=True, exist_ok=True)
    svg.write_text(inspect.render(project, model, box, px_per_mm=scale, layers=layers, net=net), encoding="utf-8")
    png = svg.with_suffix(".png")
    _png(svg, png, round((box[2] - box[0]) * scale))
    print(f"{png.relative_to(paths.ROOT)}  ({box[0]:g},{box[1]:g})-({box[2]:g},{box[3]:g}) mm, {scale:g} px/mm")
    for line in inspect.summary(project, box, net)[:80]:
        print(f"  {line}")
    return 0


def _load_project(board: str, source: str = "current") -> fixes.KicadProject:
    """The board's KiCad project with its config; ``source="unrouted"``: the board before ``routes.toml``."""
    project = fixes.KicadProject.load(paths.kicad_dir(board), board)
    if source == "unrouted":
        project.pcb = sexpr.load(paths.work_dir(board) / fixes.UNROUTED_NAME)
    project.config = config.load(board)
    return project


RATSNEST_TOP = 40


def cmd_ratsnest(board: str, source: str) -> int:
    """The unrouted links, longest first, with totals and the cells where links cross most."""
    links = ratsnest.unrouted(_load_project(board, source))
    for k in sorted(links, key=lambda k: (-k.length, k.net, k.a, k.b))[:RATSNEST_TOP]:
        print(f"  {k.length:7.2f} mm  {k.net}: {k.a} -> {k.b}")
    if len(links) > RATSNEST_TOP:
        print(f"  ... {len(links) - RATSNEST_TOP} more")
    cells = ratsnest.cell_crossings(links)
    total = sum(k.length for k in links)
    print(f"{len(links)} unrouted link(s), {total:.1f} mm, {sum(cells.values())} crossing(s)")
    size = ratsnest.CELL_MM
    for (cx, cy), n in cells.most_common(5):
        print(f"  cell ({cx * size:g},{cy * size:g})-({(cx + 1) * size:g},{(cy + 1) * size:g}) mm: {n} crossing(s)")
    return 0


def cmd_place_score(boards: list[str], source: str) -> int:
    """One line per board: ratsnest length and crossings (placement variants side by side)."""
    for board in boards:
        s = ratsnest.score(_load_project(board, source))
        print(f"{board}: {s['links']} link(s), {s['length']:.1f} mm, {s['crossings']} crossing(s)")
    return 0


POST_FILL_ATTEMPTS = 4


def _pad_spots(result: kicad.CheckResult) -> set[tuple[str, tuple[float, float]]]:
    """(net, position) of the pads KiCad reports unconnected, rounded so two runs compare."""
    return {(net, (round(x, 3), round(y, 3))) for net, (x, y) in result.unconnected_pads()}


def cmd_check(board: str) -> int:
    project, reports = paths.kicad_dir(board), paths.reports_dir(board)
    pcb = project / f"{board}.kicad_pcb"
    with ThreadPoolExecutor(max_workers=2) as pool, _phase("ERC + DRC"):  # independent kicad-cli processes
        erc_job = pool.submit(kicad.run_erc, project / f"{board}.kicad_sch", reports / "erc.json")
        drc_job = pool.submit(kicad.run_drc, pcb, reports / "drc.json")
        erc, drc = erc_job.result(), drc_job.result()
    # fixes that need KiCad's zone fill; refill + DRC again. Where that loses a connection,
    # retry keeping the tracks around the reported spots, then prune only the tracks at acute
    # angles; give up (restore) after a few tries
    post_fill_started = time.monotonic()
    backup, before = pcb.read_bytes(), drc.unconnected
    before_pads = _pad_spots(drc)
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
        # kept only if no pad lost its connection: the count alone can hide a new loss behind a fixed item
        lost = _pad_spots(drc) - before_pads
        if drc.unconnected <= before and not lost:
            for change in changes:
                print(f"  post-fill: {change}")
            break
        keep += [pos for _, pos in sorted(lost)] or drc.unconnected_spots()
        pcb.write_bytes(backup)
    else:
        print(f"  post-fill: gave up after {POST_FILL_ATTEMPTS} attempts, board left as filled")
        drc = kicad.run_drc(pcb, reports / "drc.json")
    print(f"  time: post-fill up to here {time.monotonic() - post_fill_started:.0f} s")
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
    if drc.unconnected:  # pour pieces cut off by tracks: a stitching via each into the plane below
        loaded, stitched = fixes.KicadProject.load(project, board), pcb.read_bytes()
        changes = fixes.stitch_pour_islands(loaded)
        if changes:
            loaded.save()
            fewer = kicad.run_drc(pcb, reports / "drc.json")
            if fewer.unconnected < drc.unconnected:
                drc = fewer
                for change in changes:
                    print(f"  post-fill: {change}")
            else:
                pcb.write_bytes(stitched)
                drc = kicad.run_drc(pcb, reports / "drc.json")
                print(
                    f"  post-fill: {len(changes)} stitching via(s) for pour pieces undone (no fewer unconnected items)"
                )
    loaded = fixes.KicadProject.load(project, board)
    corners = fixes.sharp_corners(loaded)
    unpoured = fixes.unpoured_copper_layers(loaded)
    summary = [str(erc), str(drc), f"Track bends: {len(corners)} sharper than 135 deg"]
    summary += [
        f"  [error] sharp_corner: {net} bend of {a:.1f} deg @ ({x:.3f}, {y:.3f}) mm" for net, (x, y), a in corners
    ]
    summary += [f"  [warning] unlabelled_connector: {w}" for w in fixes.unlabelled_connectors(loaded)]
    splits = fixes.split_crossings(loaded)
    summary.append(f"Plane splits: {len(splits)} track(s) crossing a region edge")
    summary += [f"  [warning] split_crossing: {w}" for w in splits[:40]]
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


def cmd_rules(board: str) -> int:
    if not paths.board_source(board).exists():
        print(f"error: no board {board!r} (boards/{board}/index.circuit.tsx)", file=sys.stderr)
        return 1
    ruleset = rules.merge(config.load(board))
    if ruleset is None:
        print(f"{board}: no [rules] in vibepcb.toml (available profiles: {', '.join(rules.available())})")
        return 0
    print("\n".join(rules.table(ruleset)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vibepcb", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("new", "build", "fix", "route", "inspect", "check", "all", "fab", "publish"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("board")
        if name in ("build", "all"):
            command_parser.add_argument("--fast", action="store_true", help="router effort 2x (quick experiments)")
        if name == "inspect":
            command_parser.add_argument("--area", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"))
            command_parser.add_argument("--net", help="net pattern drawn at full strength, others dimmed")
            command_parser.add_argument("--layer", action="append", help="copper layer to draw (repeatable)")
            command_parser.add_argument("--scale", type=float, default=40.0, help="pixels per mm")
            command_parser.add_argument("--source", choices=["current", "unrouted"], default="current")
        if name == "route":
            command_parser.add_argument("--only", help="route only nets matching this pattern")
            command_parser.add_argument("--dry-run", action="store_true", help="report, don't save")
            command_parser.add_argument(
                "--drc", action="store_true", help="KiCad DRC on the result (rough: no clean-up)"
            )
    clone_parser = sub.add_parser("clone")
    clone_parser.add_argument("board")
    clone_parser.add_argument("new", help="name of the copy (boards/<new>, out/<new>)")
    padtable_parser = sub.add_parser("padtable")
    padtable_parser.add_argument("board")
    padtable_parser.add_argument("refs", nargs="+", help="parts whose pad positions go into boards/<board>/pads.ts")
    ratsnest_parser = sub.add_parser("ratsnest")
    ratsnest_parser.add_argument("board")
    score_parser = sub.add_parser("place-score")
    score_parser.add_argument("boards", nargs="+", help="boards to compare (e.g. `clone` variants)")
    for p in (ratsnest_parser, score_parser):
        p.add_argument("--source", choices=["current", "unrouted"], default="current")
    sub.add_parser("rules").add_argument("board")
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
            case "rules":
                return cmd_rules(args.board)
            case "clone":
                return cmd_clone(args.board, args.new)
            case "padtable":
                return cmd_padtable(args.board, args.refs)
            case "ratsnest":
                return cmd_ratsnest(args.board, args.source)
            case "place-score":
                return cmd_place_score(args.boards, args.source)
            case "all":
                return cmd_build(args.board, fast=args.fast) or cmd_fix(args.board) or cmd_check(args.board)
            case command:
                if command == "build":
                    return cmd_build(args.board, fast=args.fast)
                if command == "inspect":
                    return cmd_inspect(args.board, args.area, args.net, args.layer, args.scale, args.source)
                if command == "route":
                    return cmd_route(args.board, only=args.only, dry_run=args.dry_run, drc=args.drc)
                handler = {"new": cmd_new, "fix": cmd_fix, "check": cmd_check, "fab": cmd_fab, "publish": cmd_publish}
                return handler[command](args.board)
    except (
        tscircuit.TscircuitError,
        kicad.KicadCliError,
        config.ConfigError,
        routes.RoutesError,
        FileNotFoundError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
