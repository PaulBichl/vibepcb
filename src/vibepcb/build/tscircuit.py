"""Thin wrapper around the tscircuit CLI (``tsci``) installed in node_modules."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibepcb.core import proc
from vibepcb.core.paths import ROOT

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class TscircuitError(RuntimeError):
    pass


def _tsci() -> list[str]:
    tsci = ROOT / "node_modules" / ".bin" / "tsci"
    npx = shutil.which("npx")  # npx.cmd on Windows (a bare "npx" is not found there)
    if not tsci.exists() or shutil.which("node") is None or npx is None:
        msg = "tscircuit is not installed - run `hatch run setup` first (`hatch run pcb doctor` checks the setup)"
        raise TscircuitError(msg)
    # tsci's shebang is `env bun`; bun comes from node_modules/.bin as well
    return [npx, "--no-install", "tsci"]


# set by the CLI to show tsci's output live (line by line); without one the output is captured silently
_line_handler: Callable[[str], None] | None = None


def set_line_handler(handler: Callable[[str], None] | None) -> None:
    """Receive every output line of the tsci subprocesses while they run (``None``: capture silently)."""
    global _line_handler  # noqa: PLW0603
    _line_handler = handler


def _run(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a tsci command in the project root; stdout+stderr (merged) go to the line handler as they appear.

    ``env``: extra environment variables for the command.
    """
    full_env = {**os.environ, **env} if env else None
    if _line_handler is None:
        return proc.run(cmd, cwd=ROOT, env=full_env)
    lines = []
    with subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as child:
        if child.stdout is not None:
            for line in child.stdout:
                lines.append(line)
                if text := line.rstrip():
                    _line_handler(text)
    return subprocess.CompletedProcess(cmd, child.returncode, "".join(lines), "")


# read by patches in tscircuit's core / autorouter (patches/tscircuit.json)
UNROUTED_NETS_ENV = "VIBEPCB_UNROUTED_NETS"
TRACE_CLEARANCE_ENV = "VIBEPCB_TRACE_CLEARANCE"


def export(
    source: Path,
    fmt: str,
    output: Path,
    *,
    unrouted_nets: Sequence[str] = (),
    trace_clearance: float | None = None,
) -> None:
    """``tsci export <source> -f <fmt> -o <output>``; raises with tsci's output on failure.

    ``unrouted_nets``: nets the autorouter leaves out (plane nets: ``fixes.plane_pad_vias`` gives each pad
    its own via to the pour, so the router's tracks of the net would only be thrown away and block others).
    ``trace_clearance``: the autorouter's track-to-track / track-to-via gap (mm); unpatched it is a fixed
    0.1 mm whatever the board says, so its routes broke the board's clearance (KiCad's net class) everywhere.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    # the parts engine looks up every part on the network on each run; `fab` checks availability once
    cmd = [*_tsci(), "export", str(source), "-f", fmt, "-o", str(output.resolve()), "--disable-parts-engine"]
    env = {UNROUTED_NETS_ENV: ",".join(unrouted_nets)} if unrouted_nets else {}
    if trace_clearance is not None:
        env[TRACE_CLEARANCE_ENV] = f"{trace_clearance:g}"
    result = _run(cmd, env or None)
    if result.returncode != 0 or not output.exists():
        msg = f"tsci export -f {fmt} failed:\n{result.stdout}{result.stderr}"
        raise TscircuitError(msg)
    if crash := autorouting_crash(result.stdout + result.stderr):
        msg = f"tscircuit's autorouter crashed, the board is unrouted: {crash}"
        raise TscircuitError(msg)


_ROUTER_CRASH = re.compile(r'Async effect error in PcbTraceRender "autorouting":\s*\n\s*(?:\x1b\[[0-9;]*m)*(.+)')


def autorouting_crash(output: str) -> str | None:
    """The error message if tscircuit's autorouter crashed during an export, else None.

    tscircuit logs the crash and still exits 0 with a circuit JSON that has no traces at all
    (seen: two traces of one net declared as one ``<differentialpair>`` side).
    """
    match = _ROUTER_CRASH.search(output)
    return re.sub(r"\x1b\[[0-9;]*m", "", match[1]).strip() if match else None


FAST_EFFORT = "2x"
_RELATIVE_IMPORT = re.compile(r"""(\bfrom\s*|\bimport\s*\(?\s*)(["'])(\.{1,2}/[^"']*|\.{1,2})\2""")
_EFFORT_PROP = re.compile(r"autorouterEffortLevel\s*=\s*(?:\"[^\"]*\"|'[^']*'|\{[^}]*\})")
_BOARD_TAG = re.compile(r"<board(?=[\s>/])")
_SCHEMATIC_DISABLED = re.compile(r"\bschematicDisabled\b")


def prepared_source(
    source: Path, target_dir: Path, *, effort: str | None = None, keepouts: Sequence[Keepout] = ()
) -> Path:
    """A copy of ``source`` in ``target_dir`` that tscircuit evaluates instead of the board itself.

    ``schematicDisabled`` is always set: tscircuit's schematic layout takes minutes to never on a
    board of a few hundred parts, and ``fix`` generates the schematic from the circuit JSON
    (:mod:`vibepcb.schgen`) anyway. ``effort`` forces ``autorouterEffortLevel`` (``--fast``: quick
    placement experiments only, the router result differs from the full-effort one). Relative
    imports are made absolute so the copy evaluates from its new place. ``keepouts`` (the board
    config's, :func:`router_keepouts`) become ``<keepout>`` elements, so the router keeps out of them.
    """
    text = source.read_text(encoding="utf-8")
    here = source.resolve().parent
    text = _RELATIVE_IMPORT.sub(lambda m: f"{m[1]}{m[2]}{(here / m[3]).resolve().as_posix()}{m[2]}", text)
    if not _BOARD_TAG.search(text):
        msg = f"no <board> tag found in {source}"
        raise TscircuitError(msg)
    if effort is not None:
        prop = f'autorouterEffortLevel="{effort}"'
        if _EFFORT_PROP.search(text):
            text = _EFFORT_PROP.sub(prop, text, count=1)
        else:
            text = _BOARD_TAG.sub(f"<board {prop}", text, count=1)
    if not _SCHEMATIC_DISABLED.search(text):
        text = _BOARD_TAG.sub("<board schematicDisabled", text, count=1)
    if keepouts:
        text = _with_keepouts(text, keepouts, where=source)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    target.write_text(text, encoding="utf-8")
    return target


# --- config keepouts for the router ([keepout.<name>] in vibepcb.toml) ------------------------

# circuit-json-to-kicad puts tscircuit's origin at KiCad (100, 100) and flips y (c2kMatPcb)
KICAD_ORIGIN = (100.0, 100.0)
_BOARD_LAYERS = re.compile(r"""\blayers\s*=\s*(?:\{\s*(\d+)\s*\}|["'](\d+)["'])""")
_BOARD_END = "</board>"


@dataclass(frozen=True)
class Keepout:
    """A config keepout the router must avoid: a box in KiCad board mm (x0, y0, x1, y1, y down)."""

    name: str
    box: tuple[float, float, float, float]
    layers: tuple[str, ...] = ()  # KiCad copper layers; empty = every copper layer
    ref: str | None = None  # the part it covers (its box comes from the placement-only JSON)


def router_keepouts(entries: dict[str, Any], circuit: list[dict[str, Any]] | None = None) -> list[Keepout]:
    """The ``[keepout.<name>]`` entries tscircuit's router can honour, as boxes.

    tscircuit's ``<keepout>`` keeps out tracks *and* vias (and its own pour), so only entries that
    block ``tracks`` reach it (a ``tracks`` only entry then has no vias either, harmless: a via needs a
    track). A ``vias`` only or ``pour`` only entry stays KiCad's (:func:`router_skipped`). Area entries
    need nothing else; ``ref`` entries need the placement (``circuit``: the placement-only circuit JSON)
    and are left out without it, or when the part is not there.
    """
    out = []
    for name, entry in entries.items():
        if "tracks" not in entry.get("block", ["pour"]):
            continue
        layers = tuple(entry.get("layers", ()))
        grow = float(entry.get("margin", 0.0))
        if "area" in entry:
            x0, y0, x1, y1 = (float(v) for v in entry["area"])
            out.append(Keepout(name, (x0 - grow, y0 - grow, x1 + grow, y1 + grow), layers))
        elif circuit is not None and (box := _part_box(circuit, entry["ref"])) is not None:
            x0, y0, x1, y1 = box
            out.append(Keepout(name, (x0 - grow, y0 - grow, x1 + grow, y1 + grow), layers, entry["ref"]))
    return out


def router_skipped(entries: dict[str, Any]) -> list[str]:
    """One line per config keepout tscircuit's router can't be told about (KiCad still enforces it)."""
    out = []
    for name, entry in entries.items():
        block = set(entry.get("block", ["pour"]))
        if "tracks" not in block:
            what = " / ".join(sorted(block))
            out.append(f"keepout {name}: {what} only - KiCad's rule area only (tscircuit's keepout blocks tracks too)")
    return out


def _part_box(circuit: list[dict[str, Any]], ref: str) -> tuple[float, float, float, float] | None:
    """KiCad-frame box of a placed part: its component bounds (pads) and its courtyard."""
    names = {c["source_component_id"]: c.get("name") for c in circuit if c["type"] == "source_component"}
    comp = next(
        (c for c in circuit if c["type"] == "pcb_component" and names.get(c.get("source_component_id")) == ref), None
    )
    if comp is None:
        return None
    cx, cy, w, h = comp["center"]["x"], comp["center"]["y"], comp.get("width", 0), comp.get("height", 0)
    points = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy + h / 2)]
    for c in circuit:
        if c.get("pcb_component_id") != comp["pcb_component_id"]:
            continue
        if c["type"] == "pcb_courtyard_rect":
            x, y, cw, ch = c["center"]["x"], c["center"]["y"], c["width"], c["height"]
            points += [(x - cw / 2, y - ch / 2), (x + cw / 2, y + ch / 2)]
        elif c["type"] == "pcb_courtyard_outline":
            points += [(p["x"], p["y"]) for p in c["outline"]]
    ox, oy = KICAD_ORIGIN
    xs, ys = [ox + x for x, _ in points], [oy - y for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def _tsci_layer(layer: str) -> str:
    """KiCad copper layer -> tscircuit layer name."""
    if layer == "F.Cu":
        return "top"
    if layer == "B.Cu":
        return "bottom"
    return f"inner{layer.removeprefix('In').removesuffix('.Cu')}"


def _board_layer_count(text: str) -> int:
    start = _BOARD_TAG.search(text)
    if start is None:
        return 2
    depth, end = 0, start.end()
    while end < len(text) and not (text[end] == ">" and depth == 0):  # the tag's end, `>` in {...} skipped
        depth += {"{": 1, "}": -1}.get(text[end], 0)
        end += 1
    m = _BOARD_LAYERS.search(text, start.end(), end)
    return int(m[1] or m[2]) if m else 2


def _num(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def keepout_tsx(k: Keepout, layer_count: int) -> str:
    """A ``<keepout>`` for ``k`` (board frame; parts may overlap it, KiCad's rule area allows footprints)."""
    x0, y0, x1, y1 = k.box
    ox, oy = KICAD_ORIGIN
    every = ["F.Cu", *(f"In{i}.Cu" for i in range(1, layer_count - 1)), "B.Cu"]
    layers = [_tsci_layer(lay) for lay in every if not k.layers or lay in k.layers]
    return (
        f'<keepout shape="rect" pcbX={{{_num((x0 + x1) / 2 - ox)}}} pcbY={{{_num(oy - (y0 + y1) / 2)}}} '
        f'width="{_num(x1 - x0)}mm" height="{_num(y1 - y0)}mm" layers={{{json.dumps(layers)}}} allowPlacements />'
    )


def _with_keepouts(text: str, keepouts: Sequence[Keepout], where: Path) -> str:
    end = text.rfind(_BOARD_END)
    if end < 0:
        msg = f"no {_BOARD_END} in {where}: the config keepouts go right before it"
        raise TscircuitError(msg)
    count = _board_layer_count(text)
    lines = "".join(f"    {{/* vibepcb.toml [keepout.{k.name}] */}}\n    {keepout_tsx(k, count)}\n" for k in keepouts)
    return f"{text[:end]}{lines}  {text[end:]}"


def add_keepouts(source: Path, keepouts: Sequence[Keepout]) -> None:
    """Add ``keepouts`` to the prepared copy ``source`` (the part keepouts, once the placement is known)."""
    if keepouts:
        source.write_text(_with_keepouts(source.read_text(encoding="utf-8"), keepouts, source), encoding="utf-8")


def save_keepouts(keepouts: Sequence[Keepout], path: Path) -> None:
    """The keepouts the build gave tscircuit: ``fix`` drops the KiCad zones the export made of them."""
    path.write_text(json.dumps([asdict(k) for k in keepouts], indent=1), encoding="utf-8")


def load_keepouts(path: Path) -> list[Keepout]:
    if not path.exists():
        return []
    return [
        Keepout(d["name"], tuple(d["box"]), tuple(d["layers"]), d.get("ref"))
        for d in json.loads(path.read_text(encoding="utf-8"))
    ]


def evaluate_unrouted(source: Path, output: Path, *, parts_engine: bool = False) -> Path:
    """``tsci build --routing-disabled``: evaluate placement only (no autorouter); returns the circuit JSON.

    With ``parts_engine`` the supplier lookup runs too, which adds ``*_availability_warning`` elements.
    """
    cmd = [*_tsci(), "build", str(source), "--routing-disabled", "--ignore-errors"]
    if not parts_engine:
        cmd.append("--disable-parts-engine")
    result = _run(cmd)
    relative = source.resolve().relative_to(ROOT)
    circuit = (
        ROOT / "dist" / relative.parent / relative.name.removesuffix(".tsx").removesuffix(".circuit") / "circuit.json"
    )
    if result.returncode != 0 or not circuit.exists():
        msg = f"tsci build --routing-disabled failed:\n{result.stdout}{result.stderr}"
        raise TscircuitError(msg)
    # tsci reuses dist/ output: left there, the unrouted JSON comes back from the routed export
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(circuit, output)
    shutil.rmtree(circuit.parent)
    _prune_empty(circuit.parent.parent, ROOT / "dist")
    return output


def _prune_empty(directory: Path, stop: Path) -> None:
    """Remove ``directory`` and its parents up to ``stop`` (included) while they are empty."""
    while directory.is_relative_to(stop) and directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
        if directory == stop:
            break
        directory = directory.parent


def availability_warnings(source: Path) -> list[str]:
    """Supplier availability warnings (one line per part) from a parts-engine evaluation of ``source``."""
    with tempfile.TemporaryDirectory() as tmp:
        circuit = evaluate_unrouted(source, Path(tmp) / "unrouted.circuit.json", parts_engine=True)
        elements = json.loads(circuit.read_text(encoding="utf-8"))
    return [str(e.get("message", "")) for e in elements if str(e.get("type", "")).endswith("availability_warning")]


def build(source: Path, out_dir: Path, stem: str, formats: dict[str, str]) -> list[Path]:
    """Export ``source`` to every ``{tsci format: file suffix}`` in ``formats``; returns the written files."""
    written = []
    for fmt, suffix in formats.items():
        output = out_dir / f"{stem}{suffix}"
        export(source, fmt, output)
        written.append(output)
    return written


# tscircuit's DRC giving up (e.g. on a zero-width route point) is no finding of its own:
# KiCad's DRC in `check` covers the board, and fixes.repair_zero_width_tracks the cause
NON_BLOCKING_ERRORS = (
    "DRC could not complete",
    # router vias crowding a pad: fixes.relocate_vias moves them, KiCad's DRC (check) catches the rest
    "overlaps SMD pad",
)
REPORT_LINES = 40


# what a placement-only evaluation (before routing) can already tell
PLACEMENT_ERRORS = (
    "pcb_placement_error",
    "pcb_component_outside_board_error",
    "pcb_courtyard_overlap_error",
    "pcb_footprint_overlap_error",
    "pcb_pad_pad_clearance_error",
)


def board_clearance(circuit_json: Path) -> float | None:
    """The board's ``minTraceToPadEdgeClearance`` (mm) from a circuit JSON: the clearance KiCad's net class gets."""
    elements = json.loads(circuit_json.read_text(encoding="utf-8"))
    board = next((e for e in elements if e.get("type") == "pcb_board"), None)
    value = board.get("min_trace_to_pad_edge_clearance") if board else None
    return float(value) if isinstance(value, int | float) else None


ANODE_FIRST_FTYPES = ("simple_diode", "simple_led")


def anode_first_refs(circuit_json: Path) -> set[str]:
    """References whose pin 1 is an anode (tscircuit's ``<diode>`` / ``<led>``): their footprints are the reverse of
    JLCPCB's zero orientation for diodes (cathode = pad 1, on the left), so the CPL turns them by 180 deg."""
    elements = json.loads(circuit_json.read_text(encoding="utf-8"))
    names = {  # capacitors carry an "anode" hint on pin 1 too (polarity of electrolytics): diodes and LEDs only
        e["source_component_id"]: e.get("name", "")
        for e in elements
        if e.get("type") == "source_component" and e.get("ftype") in ANODE_FIRST_FTYPES
    }
    return {
        names[e["source_component_id"]]
        for e in elements
        if e.get("type") == "source_port"
        and e.get("source_component_id") in names
        and (e.get("pin_number") == 1 or e.get("name") == "pin1")
        and "anode" in (e.get("port_hints") or [])
    }


def circuit_errors(circuit_json: Path, only: tuple[str, ...] | None = None) -> tuple[int, str]:
    """Blocking errors in a circuit JSON (``*_error`` elements; just the types in ``only``) and a short report."""
    elements = json.loads(circuit_json.read_text(encoding="utf-8"))
    errors = [
        e
        for e in elements
        if str(e.get("type", "")).endswith("_error")
        and (only is None or e["type"] in only)
        and not any(p in str(e.get("message", "")) for p in NON_BLOCKING_ERRORS)
    ]
    counts = Counter(str(e["type"]) for e in errors)
    lines = [f"Errors: {len(errors)}", *(f"  {n} x {t}" for t, n in counts.most_common())]
    lines += [f"- {e['type']}: {e.get('message', '')}" for e in errors[:REPORT_LINES]]
    return len(errors), "\n".join(lines)


def routed_errors(circuit_json: Path) -> tuple[int, str]:
    """Errors that stop the build after routing, and a report that also counts the rest.

    Blocking: source/netlist errors and an aborted autorouter - things KiCad can't judge.
    tscircuit's own copper DRC (clearances, via near pad, unrouted pins of a poured net...)
    is reported only: ``fix`` repairs what it can and KiCad's DRC in ``check`` decides.
    """
    elements = json.loads(circuit_json.read_text(encoding="utf-8"))
    errors = [e for e in elements if str(e.get("type", "")).endswith("_error")]
    blocking = [
        e
        for e in errors
        if (str(e["type"]).startswith("source_") or e["type"] == "pcb_autorouting_error")
        and not any(p in str(e.get("message", "")) for p in NON_BLOCKING_ERRORS)
    ]
    others = Counter(str(e["type"]) for e in errors if not any(e is b for b in blocking))
    lines = [f"Errors: {len(blocking)}", *(f"- {e['type']}: {e.get('message', '')}" for e in blocking[:REPORT_LINES])]
    if others:
        lines.append(
            "tscircuit copper DRC (left to fix/check): " + ", ".join(f"{n} x {t}" for t, n in others.most_common())
        )
    return len(blocking), "\n".join(lines)


def repair_trace_nets(circuit_json: Path) -> list[str]:
    """Re-link routed traces to a ``source_trace`` of the net their end ports belong to.

    The autorouter sometimes tags a route with another net's ``source_trace_id`` (seen: 15 of 557
    routes on a 4-layer board, e.g. an A_MOSI route tagged ".U30 > .AVDD to net.V3V3A"); the KiCad
    export takes the track's net from that link, so the board gets shorts and broken nets.
    Rewrites ``circuit_json`` in place; returns one line per re-linked trace.
    """
    elements = json.loads(circuit_json.read_text(encoding="utf-8"))
    key_of_port = {
        e["source_port_id"]: e.get("subcircuit_connectivity_map_key")
        for e in elements
        if e.get("type") == "source_port"
    }
    port_source = {e["pcb_port_id"]: e.get("source_port_id") for e in elements if e.get("type") == "pcb_port"}
    traces = {e["source_trace_id"]: e for e in elements if e.get("type") == "source_trace"}
    trace_of_key: dict[str, str] = {}
    for tid, t in traces.items():
        trace_of_key.setdefault(str(t.get("subcircuit_connectivity_map_key")), tid)

    changes = []
    relinked: dict[str, str] = {}  # pcb_trace_id -> net key
    for e in elements:
        if e.get("type") != "pcb_trace" or e.get("source_trace_id") not in traces:
            continue
        ends = {p.get(k) for p in e.get("route", []) for k in ("start_pcb_port_id", "end_pcb_port_id") if p.get(k)}
        keys = {key_of_port.get(port_source.get(str(p))) for p in ends} - {None}
        current = traces[e["source_trace_id"]].get("subcircuit_connectivity_map_key")
        if len(keys) != 1 or current in keys:
            continue  # no end port, ends on two nets (the router's own short: KiCad DRC reports it), or fine
        key = keys.pop()
        if key not in trace_of_key:
            continue
        changes.append(
            f"{e.get('pcb_trace_id')}: {traces[e['source_trace_id']].get('display_name')}"
            f" -> {traces[trace_of_key[key]].get('display_name')}"
        )
        e["source_trace_id"] = trace_of_key[key]
        relinked[str(e.get("pcb_trace_id"))] = key
        if "subcircuit_connectivity_map_key" in e:
            e["subcircuit_connectivity_map_key"] = key
    for e in elements:  # the route's vias carry the net too
        if e.get("type") == "pcb_via" and e.get("pcb_trace_id") in relinked:
            e["subcircuit_connectivity_map_key"] = relinked[str(e["pcb_trace_id"])]
    if changes:
        circuit_json.write_text(json.dumps(elements), encoding="utf-8")
    return changes
