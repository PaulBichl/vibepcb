# VibePCB

Design PCBs with an LLM: the circuit is code ([tscircuit](https://tscircuit.com),
React/TSX), exported to [KiCad](https://www.kicad.org), and post-processed there:
scripted fixes for export quirks, ERC/DRC via `kicad-cli`, and a real KiCad
project for final hand edits and Gerbers.

```
boards/<name>/index.circuit.tsx ──build──▶ out/<name>/raw ──fix──▶ out/<name>/kicad ──check──▶ reports
       (LLM writes this)            tsci check + export      scripted fixes        kicad-cli ERC/DRC
```

## Setup

Requirements: [hatch](https://hatch.pypa.io), git, and for `check`/`fab` KiCad >= 9.

```bash
hatch run setup            # Node.js into the hatch env + npm install (tscircuit, bun)
sudo pacman -S kicad       # or your distro's KiCad >= 9 package; provides kicad-cli (or set $KICAD_CLI)
hatch run pcb docs         # optional: tscircuit's LLM docs -> docs/tscircuit-llms.txt
```

## Usage

```bash
hatch run pcb all led-indicator    # build + fix + check the smoke-test board (worked example: github.com/PaulBichl/vibepcb-example-co2-monitor)
hatch run pcb list                 # list the boards
hatch run pcb new my-board         # scaffold boards/my-board/index.circuit.tsx
hatch run pcb build my-board       # tsci check + export (KiCad, netlist, PNG/SVG previews)
hatch run pcb fix my-board         # -> out/my-board/kicad/my-board.kicad_pro
hatch run pcb check my-board       # ERC + DRC, exits 1 on errors
hatch run pcb fab my-board         # gerbers/drill zip, BOM, pick-and-place, schematic PDF, renders
hatch run tsci dev boards/my-board/index.circuit.tsx   # live browser preview
```

## Working with an LLM

Open the repo in Claude Code (or another agent) and describe the board. The
agent reads [CLAUDE.md](CLAUDE.md) for the loop and the vendored
[tscircuit skill](.claude/skills/tscircuit/SKILL.md) for the syntax. It edits the
TSX, builds, looks at the PNG previews and netlist, and iterates on the
`tsci check` / KiCad ERC/DRC findings. Anything tscircuit consistently gets wrong
in the KiCad export becomes a fix in `src/vibepcb/fixes.py`.

## Layout

| Path | What |
|------|------|
| `boards/` | tscircuit sources, one folder per board |
| `src/vibepcb/cli.py` | `vibepcb` command (`hatch run pcb ...`) |
| `src/vibepcb/tscircuit.py` | wrapper around `tsci` |
| `src/vibepcb/kicad.py` | project assembly + `kicad-cli` ERC/DRC/fab |
| `src/vibepcb/fixes.py` | post-processing fixes on the KiCad files |
| `src/vibepcb/tracks.py` | track-cleaning geometry (bends, junctions, clearances) |
| `src/vibepcb/sexpr.py` | lossless KiCad S-expression reader/writer |
| `src/vibepcb/paths.py` | project root and `boards/` / `out/` layout |
| `tests/` | pytest suite, fixture board in `tests/data/` |
| `docs/TOOLING.md` | full tooling reference (commands, fixes, checks, gotchas) |
| `.claude/skills/tscircuit/` | official tscircuit agent skill (`hatch run update-skill`) |
| `out/` | generated, gitignored |

## Development

```bash
hatch run test     # pytest
hatch run style    # ruff
hatch run type     # mypy
hatch run fix      # pre-commit (ruff --fix, format, whitespace)
```
