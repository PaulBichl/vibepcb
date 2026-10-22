# VibePCB

**LLM-driven PCB design: tscircuit routes, VibePCB repairs, KiCad checks.**

You describe a board in plain words. An AI coding agent (Claude Code or similar) designs it
as code in [tscircuit](https://tscircuit.com) (parts, nets, placement), tscircuit's autorouter
routes it, and VibePCB repairs the routed board and turns it into a clean
[KiCad](https://www.kicad.org) project: checked by KiCad's own rule checks, with schematic, BOM
and fabrication files. KiCad does no routing here: it checks, refills the pours and writes the
fab files. The tooling is written for the agent; you talk to the agent and review what comes out.

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
your description ─▶ agent writes TSX ─▶ tscircuit routes ─▶ KiCad files ─▶ VibePCB repairs ─▶ KiCad DRC/ERC ─▶ fab files
                         ▲                                                                            │
                         └─────────────────────── agent reads the findings and iterates ─────────────┘
```

Who does what:

| Step | Done by | What |
|------|---------|------|
| Circuit + placement | the agent, in TSX | parts (imported from JLCPCB by part number), nets, every part's position, board rules |
| Routing | tscircuit's autorouter | all tracks and vias, a first draft of the copper pours; exported to KiCad once |
| BGA escape | VibePCB | the routes out of a ball grid (tscircuit's router can't); tscircuit routes on from their ends |
| Repair | VibePCB, editing the KiCad files | removes the router's quirks (sharp bends, vias on neighbouring pins, duplicate vias, stubs), a via per pad of a power plane, small local reroutes; generates the schematic with real KiCad symbols |
| Checks | KiCad (`kicad-cli`) | DRC + ERC on refilled pours, plus house rules (poured layers, bend angles, antennas) |
| Output | KiCad (`kicad-cli`) | Gerbers, drill, BOM / pick-and-place, schematic PDF, 3D renders |

1. **Generation (tscircuit).** The circuit is React/TSX. The agent uses tscircuit's own agent
   skill for the syntax, so it doesn't invent props.
2. **Repair (VibePCB).** Scripted, tested fixes that follow KiCad's own connection rules; any net
   KiCad finds broken afterwards is rolled back to its routed state, so a fix never disconnects
   anything. Board settings tscircuit can't express (net classes, length groups, diff-pair rules,
   plane nets, BGA escapes) come from a `vibepcb.toml` next to the board.
3. **Checks decide, not the eye.** KiCad's DRC and ERC plus the house rules decide when a board
   is done. Fab files are only made for boards that pass.

Anything the agent learns about tscircuit's router and export becomes a fix in the tooling, so
the next board starts from there. The full list of fixes and why each exists:
[docs/TOOLING.md](docs/TOOLING.md#52-table-of-all-fixes).

## Getting started

Requirements: [hatch](https://hatch.pypa.io), git, KiCad >= 9 (for checks and fab files), and an
AI coding agent such as [Claude Code](https://claude.com/claude-code).

```bash
git clone https://github.com/PaulBichl/vibepcb.git && cd vibepcb
hatch run setup            # Node.js + tscircuit into the hatch env (no system node needed)
sudo pacman -S kicad       # or your distro's KiCad >= 9 package
```

Then open the repo in the agent and describe the board: what it should do, power, interfaces,
size, fab house. The agent follows [AGENTS.md](AGENTS.md) and does the rest, and hands over the
KiCad project and fab files once the checks pass. `pcb publish` puts the review set (schematic,
layout, renders, BOM, KiCad project; no fab files) into the board's folder for sharing.

Boards can also live in their own repo next to this one (like the examples), running the
tooling from this checkout; `templates/project.gitignore` says what such a repo keeps.

Each board's output is in `out/<board>/`: its `README.md` says what to open (the KiCad project,
`board.svg`, `schematic.svg`, `check.txt`, and `fab/` once fab files are made).

## Limits

- The router is tscircuit's: routing quality depends on it. Dense spots (0.5 mm-pitch chips) can
  need a few placement iterations, which the agent runs itself; a large 4-layer board takes
  minutes per route. Ball grids (BGA) are escaped by VibePCB, the rest is still tscircuit's.
- Length groups and differential pairs are checked (KiCad DRC), not tuned: no meanders.
- Analog performance isn't checked by the tooling. For that, the agent writes SPICE decks per
  board (see the SMU example).
- Review before you order: the checks catch rule violations, not a wrong design choice. Look at
  the schematic, the BOM and the 3D render.

## For developers

The agent-facing reference (commands, pipeline, every fix, timings, gotchas) is in
[docs/TOOLING.md](docs/TOOLING.md); the working rules for the agent are in [AGENTS.md](AGENTS.md).

```bash
hatch run test     # pytest
hatch run style    # ruff
hatch run type     # mypy
```
