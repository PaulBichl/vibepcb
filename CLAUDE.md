# CLAUDE.md

VibePCB: design PCBs with an LLM. The circuit is written in **tscircuit** (React/TSX),
exported to **KiCad**, then post-processed (scripted fixes + DRC/ERC) so the result
opens cleanly in KiCad for final touches and fab output.

## Pipeline

```
boards/<name>/index.circuit.tsx      <- you edit this (the source of truth)
        │  hatch run pcb build <name>   (tsci check, then tsci export)
        ▼
out/<name>/raw/                       <- untouched tscircuit output, never edit
   <name>.kicad.zip, lib/, .netlist.txt, .pcb.png, .sch.png, .circuit.json
        │  hatch run pcb fix <name>     (src/vibepcb/fixes.py)
        ▼
out/<name>/kicad/<name>.kicad_pro     <- KiCad project, open this in KiCad
        │  hatch run pcb check <name>   (kicad-cli ERC + DRC)
        ▼
out/<name>/reports/{erc,drc}.json
```

`hatch run pcb all <name>` = build + fix + check. `out/` is generated and gitignored.

## How to work on a board

1. Write/modify `boards/<name>/index.circuit.tsx`. Use the **tscircuit skill**
   (`.claude/skills/tscircuit/`, elements in `elements/*.md`) for syntax; don't
   invent props. For details beyond it: `hatch run pcb docs` downloads the full
   docs to `docs/tscircuit-llms.txt` - grep it, never read it whole (900 KB).
2. `hatch run pcb build <name>`. It stops if `tsci check` reports errors: fix
   the TSX, not the output. Warnings are reported but don't stop the build.
3. **Look at the result**: Read `out/<name>/raw/<name>.pcb.png` and
   `<name>.sch.png`, and `<name>.netlist.txt` to verify connectivity
   (every pin on the intended net, LED/diode polarity, power pins).
4. `hatch run pcb fix <name>` then `hatch run pcb check <name>`. Fix DRC/ERC
   findings at the *source* (TSX placement, trace widths, net names) when
   possible; only systematic tscircuit->KiCad export quirks belong in
   `fixes.py`.
5. Repeat until `check` exits 0 (errors = 0). Then `hatch run pcb fab <name>`:
   gerbers/drill zip, BOM CSV (with LCSC/MPN), pick-and-place CSV, schematic PDF,
   top/bottom 3D renders in `out/<name>/fab/`, JLCPCB upload set in `fab/jlcpcb/`.
   Never hand over fab files of a board whose `check` fails.

Layout quality bar, enforced by `check` (not by eye): GND connects through the pours
(no redundant GND tracks), no track bend sharper than 135 deg and no acute angle at
junctions, no antennas (dangling track ends / vias), nothing unconnected.

Extra tscircuit checks: `hatch run tsci check netlist|placement|shorts <file>`
(`hatch run tsci ...` = the `tsci` the skill talks about).

## Post-processing fixes (`src/vibepcb/fixes.py`)

A fix is `def my_fix(project: KicadProject) -> list[str]`: edit `project.pcb` /
`project.sch` (parsed S-expression trees) in place, return one line per change,
`[]` when nothing to do, and be **idempotent**. Register it in `FIXES` and add a
test in `tests/test_fixes.py` against the fixtures in `tests/data/`.

- Edit KiCad files only through `vibepcb.sexpr` (lossless). Do **not** use
  kiutils/kicad-skip: they drop KiCad 9 data / crash on tscircuit's output.
- In the tree, `Sym` = bare atom (`F.Cu`, `1.6`), plain `str` = quoted string.
  Keep that distinction when writing values (`Sym("1.6")` vs `"VCC"`).
- Known tscircuit export quirks already handled: selector-style net names,
  1.4 mm board thickness, Value == Reference, empty schematic Footprint fields,
  footprint library nickname mismatch (`tscircuit:` vs `tscircuit_builtin`),
  split pads (same number) with the net on one pad only, track stubs inside pads,
  footprint NPTH holes duplicated as Edge.Cuts, keepouts forbidding footprints,
  net-label stubs exported as plain text, no no-connect flags, GND/supply rails
  as ordinary parts (not power symbols), MPN/LCSC numbers dropped, solid pad-to-pour
  connections (-> thermal reliefs), 0.4 mm reference text at fixed offsets (-> 0.8 mm,
  auto-placed in free space), oversized sheet + empty title block, net labels on the
  pin end of their stub, router gaps 1-2 um short of the clearance (-> 5 um DRC
  slack), and messy routing (`clean_tracks`, geometry in `src/vibepcb/tracks.py`):
  overlapping duplicate routes, loops/lattices of one net,
  T-junctions without a vertex, zig-zags inside pads/vias, spikes, 90 deg / acute
  bends. Every track change there is verified against KiCad's connection rules
  (pad: track end inside it or pad centre on the track; via: via centre on the track;
  track-track: end to end) net by net, else polyline by polyline.
- `check` refills zones with KiCad and saves the board (tscircuit's pour is a
  mosaic with floating islands). The KiCad 10 save writes `(net "NAME")` without
  ids, so registered fixes must only run on fresh tscircuit output (`fix` always
  re-assembles). After the refill, `check` runs `apply_post_fill`: drops tracks the
  GND pour already provides, cleans tracks again, re-runs DRC and retries/restores
  if a connection got lost. KiCad's `track_angle` rule is "no acute angles" (it
  also measures junction branches, which can't all be 135 deg apart); the 135 deg
  bend minimum is `fixes.sharp_corners`.
- Keep fix output deterministic (new track UUIDs are content-derived): KiCad's zone
  fill depends on item order, so random UUIDs make `check` results vary run to run.

## Board design lessons (tscircuit autorouter / parts)

- The router's results jump with tiny placement changes; if `tsci check` shows a
  few clearance/via-in-pad errors, nudge the nearby passive 0.5 mm and rebuild.
- Router clearance targets also become the KiCad rules: board props
  `minTraceToPadEdgeClearance`, `minBoardEdgeClearance` (KiCad default 0.5 mm),
  `minViaHoleDiameter`/`minViaPadDiameter`, `minViaEdgeToPadEdgeClearance`.
- The router keeps edge clearance to the outline only; put a `<keepout>` around
  `<cutout>` slots.
- Check 4-pad tactile switches against the datasheet: tscircuit's 2-pin symbol
  treats pads 1/2 as the contacts. Give the footprint pads the switch's two
  terminals (`portHints`) instead of guessing diagonals.
- Keep `schX/schY` compact (1 unit = 15 mm in KiCad): spread over ~25 x 16 units the
  schematic fits A3.
- If `check` leaves a few sharp bends/junctions in a cramped spot, the routing there
  is the problem: try `autorouterEffortLevel` (10x fixed one cramped board) and 0.2-0.5 mm
  nudges of the parts around it; tscircuit routing is deterministic, so compare
  variants as board copies (`boards/<copy>`) built in parallel.
- Pad-to-pad traces between a capacitor and a chip pin get a hidden 1 mm max length
  (tscircuit's decoupling heuristic; routing is skipped if it can't be met): set
  `maxLength` on such traces and on the cap's GND trace.
- Imported parts (`tsci import`) that carry a custom `symbol={<symbol><port name="pin3" .../>...</symbol>}`
  make `.U1 > port.pin3` match two ports ("PCB port selector is not unique"), so tscircuit cannot save the
  autorouting paths (`.tscircuit/autorouting-artifacts/<entry>/*.pcb-trace-paths.json`, the replayable route
  cache: import the JSON into `<autoroutingphase pcbTracePaths={...} />`). Verified on a one-part board:
  with the symbol it fails, with the `symbol` prop deleted (keep `pinLabels`, `footprint`, `cadModel`) the
  paths are saved. Delete the symbol block of imported chips; the schematic is generated from the netlist anyway.
- SPICE: chips have no models, so per-board ngspice decks with behavioural models live in
  `boards/<name>/sim/*.cir` (`ngspice -b <deck>`).

## Setup / environment

- `hatch run setup` installs Node.js *into the hatch env* (nodeenv) and runs
  `npm install` (tscircuit + bun as npm packages). No system node/bun needed.
- KiCad (>= 9, provides `kicad-cli`) is a system install: `sudo pacman -S kicad`.
  Override the binary with `$KICAD_CLI`. Without it, `check`/`fab` fail; build/fix work.

## Commands

| Command | Purpose |
|---------|---------|
| `hatch run pcb new <name>` | Scaffold `boards/<name>/index.circuit.tsx` |
| `hatch run pcb all <name>` | build + fix + check |
| `hatch run pcb list` | List boards |
| `hatch run tsci <args>` | tscircuit CLI |
| `hatch run test` / `style` / `type` | pytest / ruff / mypy |
| `hatch run update-skill` | Re-vendor the tscircuit skill |

Always run `hatch run style`, `hatch run type` and `hatch run test` after changing Python code.

## Python conventions

Same as the author's other hatch projects: `from __future__ import annotations`
in every file, typing-only imports under `if TYPE_CHECKING:`, absolute imports,
`X | None` hints, error messages assigned to `msg` before `raise` (ruff EM).
Library code raises; only `cli.py` prints.
