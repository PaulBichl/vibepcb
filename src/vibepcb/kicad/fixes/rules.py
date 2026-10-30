"""Design rules written into the KiCad project: board config, rule profiles, custom DRC rules."""

from __future__ import annotations

import json
from typing import Any

from vibepcb.core import sexpr
from vibepcb.core.sexpr import Sym
from vibepcb.kicad.fixes.base import KicadProject, _design_clearances, _mm
from vibepcb.routing import tracks
from vibepcb.settings import rules, stackup
from vibepcb.settings.config import NETCLASS_KEYS

FOOTPRINT_INTERNAL_HOLE_CLEARANCE_MM = 0.15  # still comfortably within common 2-layer fab limits


def footprint_internal_clearance_rule(project: KicadProject) -> list[str]:
    """Add a custom DRC rule: hole clearance *within* one footprint follows its land pattern.

    Imported manufacturer land patterns (e.g. a USB-C receptacle's GND pads 0.17 mm from
    its locating-peg holes) can sit below the board-wide hole clearance; that geometry is
    the part's, not something the layout can fix.
    """
    dru = project.pcb_path.with_suffix(".kicad_dru")
    if dru.exists():
        return []
    dru.write_text(
        "(version 1)\n"
        '(rule "footprint-internal hole clearance"\n'
        '  (condition "A.Parent == B.Parent")\n'
        f"  (constraint hole_clearance (min {FOOTPRINT_INTERNAL_HOLE_CLEARANCE_MM}mm)))\n",
        encoding="utf-8",
    )
    return [f"{dru.name}: footprint-internal hole clearance >= {FOOTPRINT_INTERNAL_HOLE_CLEARANCE_MM} mm"]


THERMAL_GAP_MM = 0.3


THERMAL_SPOKE_MM = 0.3


def zone_thermal_reliefs(project: KicadProject) -> list[str]:
    """Connect pads to copper zones through thermal-relief spokes instead of solid copper.

    tscircuit's pours connect pads solid (``connect_pads yes``): a 0402 with one pad in a
    plane and the other on a thin track heats unevenly in reflow (tombstoning) and the
    plane-side pad is hard to rework. Spokes are narrowed from KiCad's 0.5 mm, which is as
    wide as a 0402 pad and so barely relieves anything.
    """
    changes = []
    for zone in sexpr.children(project.pcb, "zone"):
        connect, fill = sexpr.child(zone, "connect_pads"), sexpr.child(zone, "fill")
        if sexpr.child(zone, "keepout") is not None or connect is None or fill is None:
            continue
        changed = False
        if len(connect) > 1 and connect[1] == Sym("yes"):
            del connect[1]
            changed = True
        for key, value in (("thermal_gap", THERMAL_GAP_MM), ("thermal_bridge_width", THERMAL_SPOKE_MM)):
            node = sexpr.child(fill, key)
            if node is not None and float(str(node[1])) != value:
                node[1] = Sym(_mm(value))
                changed = True
        if changed:
            net = sexpr.child(zone, "net_name")
            layer = sexpr.child(zone, "layer")
            changes.append(
                f"zone {net[1] if net is not None else '?'} on {layer[1] if layer is not None else '?'}: thermal reliefs"
            )
    return changes


THERMAL_SPOKE_RULE = "zone pads: one thermal spoke is enough"


def thermal_spoke_rule(project: KicadProject) -> list[str]:
    """Accept a single thermal spoke per pad (KiCad wants 2 and reports ``starved_thermal`` errors).

    With thermal reliefs on, a small pad hemmed in by its own tracks may only get one spoke
    from the pour; it is still connected (often by a track as well), so where the router put
    its tracks shouldn't fail DRC.
    """
    dru = project.pcb_path.with_suffix(".kicad_dru")
    text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
    if THERMAL_SPOKE_RULE in text:
        return []
    text += f'(rule "{THERMAL_SPOKE_RULE}"\n  (constraint min_resolved_spokes 1))\n'
    dru.write_text(text, encoding="utf-8")
    return [f"{dru.name}: min. 1 thermal spoke per pad"]


ROUTER_CLEARANCE_RULE = "router clearance: float tolerance"


ROUTER_CLEARANCE_SLACK_MM = 0.005  # far below any fab tolerance (~0.025 mm)


def router_clearance_tolerance(project: KicadProject) -> list[str]:
    """Accept copper gaps a few microns short of the net-class clearance.

    The autorouter targets exactly the board's clearance, which becomes the KiCad net-class
    clearance, and floating-point error leaves some via/track gaps 1-2 um short (seen:
    0.1986 mm for 0.2 mm) - errors already in the raw export, no layout change fixes them.
    """
    dru = project.pcb_path.with_suffix(".kicad_dru")
    text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
    if ROUTER_CLEARANCE_RULE in text:
        return []
    clearance = _design_clearances(project)[0] - ROUTER_CLEARANCE_SLACK_MM
    text += (
        f'(rule "{ROUTER_CLEARANCE_RULE}"\n'
        "  (condition \"A.NetClass == 'Default' && B.NetClass == 'Default'\")\n"  # other classes keep theirs
        f"  (constraint clearance (min {_mm(clearance)}mm)))\n"
    )
    dru.write_text(text, encoding="utf-8")
    return [f"{dru.name}: copper clearance >= {_mm(clearance)} mm"]


SHARP_CORNER_RULE = "no acute track angles"


STRICT_SEVERITIES = {"track_dangling": "error", "via_dangling": "error", "track_angle": "error"}


BOARD_CONFIG_RULE = "board config"  # prefix of the .kicad_dru rules apply_board_config writes


def apply_board_config(project: KicadProject) -> list[str]:
    """Net classes, length groups and diff-pair rules from ``boards/<board>/vibepcb.toml``.

    tscircuit has no net classes or KiCad length rules: classes (widths, clearance, diff-pair
    width / gap) and their net-name patterns go into the .kicad_pro; each length group becomes a
    ``skew`` rule and the diff-pair settings a ``diff_pair_uncoupled`` rule in the .kicad_dru, so
    KiCad's DRC (and ``check``) fails on them. Rewritten from the config on every run. A class with
    ``impedance`` / ``impedance_diff`` gets its track_width / diff_pair_width / diff_pair_gap solved
    from the ``[stackup]`` (:mod:`vibepcb.stackup`, closed-form: confirm with the fab's calculator),
    replacing any width given in the class; a change line names the layer and stack-up.
    """
    cfg = project.config
    if not cfg:
        return []
    changes = []
    pro = project.pcb_path.with_suffix(".kicad_pro")
    netclasses = rules.netclasses(cfg)  # the board's [netclass] plus those of its rule profiles
    if pro.exists() and netclasses:
        data = json.loads(pro.read_text(encoding="utf-8"))
        settings = data.setdefault("net_settings", {})
        classes = [c for c in settings.get("classes", []) if c.get("name") == "Default"]
        default = classes[0] if classes else {}
        patterns, notes = [], []
        stack = stackup.from_config(cfg["stackup"]) if "stackup" in cfg else None
        for name, cls in netclasses.items():
            entry = {**default, "name": name}
            entry.update({k: float(v) for k, v in cls.items() if k in NETCLASS_KEYS})
            if stack is not None and (solved := stackup.netclass_widths(stack, cls))[0]:
                entry.update(solved[0])
                values = ", ".join(f"{k} {v:g}" for k, v in solved[0].items())
                notes.append(f"{pro.name}: net class {name}: {values} mm ({solved[1]})")
            classes.append(entry)
            patterns += [{"netclass": name, "pattern": str(p)} for p in cls["nets"]]
        if settings.get("classes") != classes or settings.get("netclass_patterns") != patterns:
            settings["classes"] = classes
            settings["netclass_patterns"] = patterns
            pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            changes.append(f"{pro.name}: net classes {', '.join(netclasses)}")
            changes += notes
    dru_forms = []
    for name, group in cfg.get("length_group", {}).items():
        condition = " || ".join(f"A.NetName == '{p}'" for p in group["nets"])
        dru_forms.append(
            f'(rule "{BOARD_CONFIG_RULE}: length group {name}"\n  (condition "{condition}")\n'
            f"  (constraint skew (max {_mm(float(group['max_skew']))}mm)))\n"
        )
    if (uncoupled := cfg.get("diff_pair", {}).get("max_uncoupled")) is not None:
        dru_forms.append(
            f'(rule "{BOARD_CONFIG_RULE}: diff pairs"\n  (condition "A.inDiffPair(\'*\')")\n'
            f"  (constraint diff_pair_uncoupled (max {_mm(float(uncoupled))}mm)))\n"
        )
    if dru_forms:
        dru = project.pcb_path.with_suffix(".kicad_dru")
        text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
        kept = [r for r in _dru_rules(text) if f'(rule "{BOARD_CONFIG_RULE}' not in r]
        new_text = "".join(kept) + "".join(dru_forms)
        if new_text != text:
            dru.write_text(new_text, encoding="utf-8")
            changes.append(f"{dru.name}: {len(dru_forms)} rule(s) from the board config")
    return changes


# KiCad's own net class defaults for the schematic side (what KiCad writes for a new class)
NETCLASS_SCHEMATIC_DEFAULTS: dict[str, Any] = {
    "wire_width": 6,  # mils
    "bus_width": 12,
    "line_style": 0,
    "schematic_color": "rgba(0, 0, 0, 0.000)",  # transparent = the colour theme's
    "pcb_color": "rgba(0, 0, 0, 0.000)",
}


def complete_netclasses(project: KicadProject) -> list[str]:
    """Give every net class of the .kicad_pro the schematic keys KiCad expects (wire width, colour, line style).

    tscircuit's export and the rule fixes write the PCB keys only; without ``wire_width`` / ``schematic_color``
    KiCad drew every schematic wire with no stroke at all: the plotted SVG / PDF (and the preview) showed parts
    and labels but none of the wires between them.
    """
    pro = project.pcb_path.with_suffix(".kicad_pro")
    if not pro.exists():
        return []
    data = json.loads(pro.read_text(encoding="utf-8"))
    classes = data.setdefault("net_settings", {}).setdefault("classes", [])
    if not any(c.get("name") == "Default" for c in classes):
        classes.insert(0, {"name": "Default"})
    changes = []
    for cls in classes:
        missing = {k: v for k, v in NETCLASS_SCHEMATIC_DEFAULTS.items() if k not in cls}
        if "priority" not in cls:
            missing["priority"] = 2147483647 if cls.get("name") == "Default" else 0  # KiCad's: Default lowest
        if missing:
            cls.update(missing)
            changes.append(f"{pro.name}: net class {cls.get('name')}: schematic keys {', '.join(missing)}")
    if changes:
        pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return changes


def apply_rule_profiles(project: KicadProject) -> list[str]:
    """Design / manufacturing rule profiles (``[rules]`` in ``vibepcb.toml``, see ``vibepcb.rules``).

    The merged minimums go into the .kicad_pro design settings (KiCad's DRC and the router read
    them there), the Default net class takes the profile values (else its own, raised to the
    minimums), and pad-hole rules KiCad has no setting for become .kicad_dru rules. Runs before the
    routing fixes so ``Board.from_project`` sees the profile.
    """
    rs = rules.merge(project.config)
    if rs is None:
        return []
    changes = []
    pro = project.pcb_path.with_suffix(".kicad_pro")
    if pro.exists():
        data = json.loads(pro.read_text(encoding="utf-8"))
        design = data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
        new_rules = {k: v for k, v in rules.pro_rules(rs).items() if design.get(k) != v}
        design.update(new_rules)
        classes = data.setdefault("net_settings", {}).setdefault("classes", [])
        index = next((i for i, c in enumerate(classes) if c.get("name") == "Default"), None)
        if index is None:
            classes.insert(0, {"name": "Default"})
            index = 0
        default = rules.default_class(rs, classes[index])
        new_default = {k: v for k, v in default.items() if classes[index].get(k) != v}
        classes[index] = default
        if new_rules or new_default:
            pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            changes += [f"{pro.name}: {k} = {v:g} mm" for k, v in {**new_rules, **new_default}.items()]
    dru = project.pcb_path.with_suffix(".kicad_dru")
    text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
    added = rules.dru_rules(rs)
    forms = _dru_rules(text)
    kept = [r for r in forms if f'(rule "{rules.DRU_RULE_PREFIX}' not in r]
    new_text = "".join(kept) + "".join(added)
    if new_text != "".join(forms):
        dru.write_text(new_text, encoding="utf-8")
        changes.append(f"{dru.name}: {len(added)} rule(s) from the rule profiles")
    return changes


def _dru_rules(text: str) -> list[str]:
    """A .kicad_dru split into its top-level forms (version line, rules), each with its newline."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                out.append(text[start : i + 1] + "\n")
    return out


def strict_track_rules(project: KicadProject) -> list[str]:
    """Make KiCad DRC fail on sharp track corners and on antennas (dangling tracks/vias).

    KiCad 10's ``track_angle`` (interior angle, 180 = straight) applies one minimum to
    plain bends and to the branches of junctions alike; three branches can't all be 135
    degrees apart, so KiCad gets "no acute angles" and the 135 degree bend minimum is
    checked by :func:`sharp_corners` (``check``). KiCad only warns about dangling tracks
    and vias by default.
    """
    changes = []
    dru = project.pcb_path.with_suffix(".kicad_dru")
    text = dru.read_text(encoding="utf-8") if dru.exists() else "(version 1)\n"
    if SHARP_CORNER_RULE not in text:
        minimum = tracks.MIN_JUNCTION_DEG - 1
        text += f'(rule "{SHARP_CORNER_RULE}"\n  (constraint track_angle (min {minimum:g})))\n'
        dru.write_text(text, encoding="utf-8")
        changes.append(f"{dru.name}: track angles >= {minimum:g} deg")
    pro = project.pcb_path.with_suffix(".kicad_pro")
    if pro.exists():
        data = json.loads(pro.read_text(encoding="utf-8"))
        severities = data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rule_severities", {})
        missing = {k: v for k, v in STRICT_SEVERITIES.items() if severities.get(k) != v}
        if missing:
            severities.update(missing)
            pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            changes.append(f"{pro.name}: {', '.join(sorted(missing))} -> error")
    return changes
