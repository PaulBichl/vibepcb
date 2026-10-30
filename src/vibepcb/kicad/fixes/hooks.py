"""Fixes that call other VibePCB parts: length tuning, routes.toml, VibePCB-placed vias."""

from __future__ import annotations

import copy
import fnmatch
import importlib
import math

from vibepcb.core import sexpr
from vibepcb.kicad.fixes.base import TRACK_NODES, KicadProject, _mm, _net_names, _net_node, _net_of, _num
from vibepcb.kicad.fixes.copper import _copper
from vibepcb.routing import tracks


def tune_lengths(project: KicadProject) -> list[str]:
    """Meanders for the length groups of the board config (``vibepcb.tune``): KiCad checks the skew, this makes it."""
    tune = importlib.import_module("vibepcb.routing.tune")  # tune imports this module: no import at the top
    return list(tune.tune(project))


def net_placed_vias(project: KicadProject) -> list[str]:
    """Give a ``<via>`` the TSX placed itself the net of the track that ends in it, and drop its empty footprint.

    tscircuit exports such a via with ``(net 0)`` (its net only comes through the trace to it) plus an empty
    ``tscircuit:Unknown`` footprint at its spot. Net-less, the fixes take the via's own track for foreign
    copper (``relocate_vias`` pushed it off its pin: the GND pin lost its plane).
    """
    copper = _copper(project.pcb)
    changes = []
    for node, centre, radius, net in copper.vias:
        if net:
            continue
        ending = {
            s.net
            for s in copper.segs
            if s.net and min(math.dist(s.a, centre), math.dist(s.b, centre)) <= radius - tracks.EPS
        }
        if len(ending) != 1 or (net_node := _net_node(project.pcb, next(iter(ending)))) is None:
            continue
        old = sexpr.child(node, "net")
        if old is not None:
            node.remove(old)
        node.insert(len(node) - 1, copy.deepcopy(net_node))
        changes.append(f"via at ({_mm(centre[0])}, {_mm(centre[1])}): net {next(iter(ending))} (from its track)")
    for fp in [n for n in project.pcb if sexpr.head(n) == "footprint" and isinstance(n, list)]:
        empty = not any(sexpr.head(c) in ("pad", "fp_line", "fp_text", "fp_rect", "fp_circle", "property") for c in fp)
        if empty and len(fp) > 1 and str(fp[1]) == "tscircuit:Unknown":
            at = sexpr.child(fp, "at")
            project.pcb.remove(fp)
            where = f" at ({_mm(_num(at, 1))}, {_mm(_num(at, 2))})" if at is not None else ""
            changes.append(f"empty footprint tscircuit:Unknown{where} removed (a placed via's stand-in)")
    return changes


UNROUTED_NAME = "unrouted.kicad_pcb"


def apply_routes(project: KicadProject) -> list[str]:
    """Manual routing (``[routing] mode = "manual"``): ``routes.toml`` becomes tracks and vias (``vibepcb.routes``).

    The board as it is before the routes goes to ``.work/unrouted.kicad_pcb``: ``pcb route`` redoes the
    routes from it in seconds. Entries that fail are listed; the board keeps what did route.
    """
    if project.config.get("routing", {}).get("mode") != "manual":
        return reroute_named_nets(project)
    routes = importlib.import_module("vibepcb.routing.routes")  # routes imports this module: no import at the top

    snapshot = project.directory.parent / ".work" / UNROUTED_NAME
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    sexpr.dump(project.pcb, snapshot)
    report = routes.apply(project, project.routes)
    return [f"{r.entry}: {r.detail}" if r.ok else f"FAILED {r.entry}: {r.detail}" for r in report.results]


def reroute_named_nets(project: KicadProject) -> list[str]:
    """Auto mode: the nets a ``routes.toml`` ``[[route]] net = "..."`` entry names are re-routed by VibePCB's router.

    tscircuit routes the whole board; where it left a net in a knot (two nets 0.07 mm apart along a run, a
    fan-out it could not untangle) the board names that net in ``routes.toml`` and its tracks and vias are
    ripped up and routed again with :mod:`vibepcb.routes` (which sees every other net's copper). One net at a
    time, in file order; a net that does not route completely keeps tscircuit's copper.
    """
    entries = [e for e in project.routes.get("route", []) if "net" in e]
    if not entries:
        return []
    routes = importlib.import_module("vibepcb.routing.routes")  # routes imports this module: no import at the top

    names = _net_names(project.pcb)
    on_board = sorted({_net_of(n, names) for n in project.pcb if sexpr.head(n) in TRACK_NODES and isinstance(n, list)})
    changes = []
    for entry in entries:
        for net in [n for n in on_board if fnmatch.fnmatchcase(n, str(entry["net"]))]:
            names = _net_names(project.pcb)
            old = [
                n
                for n in project.pcb
                if sexpr.head(n) in TRACK_NODES and isinstance(n, list) and _net_of(n, names) == net
            ]
            project.pcb[:] = [n for n in project.pcb if not any(n is o for o in old)]
            before = len(project.pcb)
            report = routes.apply(project, {**project.routes, "pair": [], "route": [entry]}, only=net)
            if report.results and not report.failed:
                length = sum(r.length for r in report.results)
                vias = sum(r.vias for r in report.results)
                changes.append(f"{net}: re-routed ({len(old)} tscircuit tracks/vias -> {length:.1f} mm, {vias} via(s))")
                continue
            del project.pcb[before:]
            project.pcb += old
            why = report.failed[0].detail if report.failed else "nothing to connect"
            changes.append(f"{net}: kept tscircuit's routing (re-route failed: {why})")
    return changes
