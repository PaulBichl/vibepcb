# VibePCB

**LLM-driven PCB design: tscircuit for generation, KiCad for post-processing.**

You describe a board in plain words. An AI coding agent (Claude Code or similar) designs it
as code in [tscircuit](https://tscircuit.com), lets tscircuit place and route it, and VibePCB
turns the result into a clean [KiCad](https://www.kicad.org) project: checked by KiCad's own
rule checks, with schematic, BOM and fabrication files. The tooling is written for the agent;
you talk to the agent and review what comes out.

## What you get

For every board, without touching an EDA tool:

- a **KiCad project** (board, schematic, design rules, footprint library) that opens cleanly in
  KiCad for review or final hand edits
- a **schematic** with real symbols, generated from the netlist (PDF + KiCad)
- a **routed layout** that passes KiCad's DRC and ERC: every copper layer poured, ground through
  the pours, no track bends sharper than 135 deg, no acute junctions, no dangling tracks, nothing
  unconnected
- a **BOM with LCSC / MPN part numbers** (JLCPCB assembly parts)
- **3D renders** (top/bottom) and layout images to look at
- **fabrication files**: Gerbers + drill, pick-and-place, and a ready JLCPCB PCBA upload set

## Examples

| Board | What it shows |
|-------|---------------|
| [vibepcb-smu](https://github.com/PaulBichl/vibepcb-smu) | 4-quadrant source/measure unit, +-30 V / +-1 A, USB-C PD, Ethernet, STM32; 4-layer main board (238 parts) + display board; loop simulated with TI's SPICE models |
| [vibepcb-example-co2-monitor](https://github.com/PaulBichl/vibepcb-example-co2-monitor) | small sensor board, the end-to-end flow on a simple design |

## How it works

```
your description ─▶ agent writes TSX ─▶ tscircuit: place + route ─▶ KiCad files ─▶ VibePCB fixes ─▶ KiCad DRC/ERC ─▶ fab files
                         ▲                                                                              │
                         └────────────────────── agent reads the findings and iterates ────────────────┘
```

1. **Generation (tscircuit).** The circuit is React/TSX: parts (imported from JLCPCB by part
   number), nets, placement hints and board rules. tscircuit's autorouter places and routes it,
   and the circuit is exported to KiCad once. The agent uses tscircuit's own agent skill for the
   syntax, so it doesn't invent props.
2. **Post-processing (KiCad).** tscircuit's KiCad export and router have systematic quirks
   (tracks bent sharper than the rules allow, vias on neighbouring pins, duplicate vias, stubs,
   missing schematic symbols). VibePCB fixes them with scripted, tested fixes that follow
   KiCad's own connection rules; any net KiCad finds broken afterwards is rolled back to its
   routed state, so a fix never disconnects anything. The schematic is generated from the
   netlist with real KiCad symbols.
3. **Checks decide, not the eye.** KiCad's DRC and ERC plus house rules (poured layers, bend
   angles, antennas) decide when a board is done. Fab files are only made for boards that pass.

Anything the agent learns about tscircuit's export becomes a fix in the tooling, so the next
board starts from there. The full list of fixes and why each exists: [docs/TOOLING.md](docs/TOOLING.md#52-table-of-all-fixes).

## Getting started

Requirements: [hatch](https://hatch.pypa.io), git, KiCad >= 9 (for checks and fab files), and an
AI coding agent such as [Claude Code](https://claude.com/claude-code).

```bash
git clone https://github.com/PaulBichl/vibepcb.git && cd vibepcb
hatch run setup            # Node.js + tscircuit into the hatch env (no system node needed)
sudo pacman -S kicad       # or your distro's KiCad >= 9 package
```

Then open the repo in the agent and describe the board: what it should do, power, interfaces,
size, fab house. The agent follows [CLAUDE.md](CLAUDE.md) and does the rest, and hands over the
KiCad project and fab files once the checks pass. `pcb publish` puts the review set (schematic,
layout, renders, BOM, KiCad project; no fab files) into the board's folder for sharing.

Boards can also live in their own repo next to this one (like the examples), running the
tooling from this checkout.

## Limits

- The autorouter is tscircuit's. Dense spots (0.5 mm-pitch chips) can need a few placement
  iterations, which the agent runs itself; a large 4-layer board takes minutes per route.
- Analog performance isn't checked by the tooling. For that, the agent writes SPICE decks per
  board (see the SMU example).
- Review before you order: the checks catch rule violations, not a wrong design choice. Look at
  the schematic, the BOM and the 3D render.

## For developers

The agent-facing reference (commands, pipeline, every fix, timings, gotchas) is in
[docs/TOOLING.md](docs/TOOLING.md); the working rules for the agent are in [CLAUDE.md](CLAUDE.md).

```bash
hatch run test     # pytest
hatch run style    # ruff
hatch run type     # mypy
```
