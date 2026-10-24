# AGENTS.md

Instructions for any coding agent (Claude Code, Copilot, Codex, ...). `CLAUDE.md` includes this file.

VibePCB: design PCBs with an LLM. The circuit is written in **tscircuit** (React/TSX),
exported to **KiCad**, then post-processed (scripted fixes + DRC/ERC) so the result
opens cleanly in KiCad for final touches and fab output.

## Pipeline

```
boards/<name>/index.circuit.tsx      <- you edit this (the source of truth)
        │  hatch run pcb build <name>   (placement gate, ONE routed evaluation -> circuit.json,
        │                                everything else converted from that JSON; --fast = router 2x;
        │                                tscircuit's schematic is always off: schematicDisabled copy)
        ▼
out/<name>/.work/raw/                 <- untouched tscircuit output (.kicad.zip, .netlist.txt, .circuit.json)
        │  hatch run pcb fix <name>     (schematic from schgen.py with KiCad / LCSC symbols, then fixes.py)
        ▼
out/<name>/kicad/<name>.kicad_pro     <- KiCad project, open this in KiCad
        │  hatch run pcb check <name>   (kicad-cli ERC + DRC, post-fill fixes, house rules)
        ▼
out/<name>/check.txt, board.svg       <- result (JSON reports in .work/reports/)
```

`hatch run pcb all <name>` = build + fix + check. `out/` is generated and gitignored; its layout:

```
out/<name>/                           <- what a person looks at (README.md there says what to open)
   README.md        index: check verdict, links
   kicad/           the KiCad project (open <name>.kicad_pro)
   board.svg        layout preview (top copper + silkscreen, after check)
   schematic.svg    schematic preview
   check.txt        the check summary
   fab/             after `fab`: <name>-gerbers.zip, -bom.csv, -cpl.csv (JLCPCB format), -schematic.pdf,
                    -top/-bottom.png (3D renders)
   .work/           internals: raw/ (tscircuit output, never edit), src/ (prepared copy), reports/ (ERC/DRC
                    JSON, check.json), preview/*.png (for the agent: routed.png, board.png, schematic.png)
```

Rules of the pipeline (details and timings: `docs/TOOLING.md`):

- **Build evaluates the TSX once** (circuit JSON); KiCad zip, netlist and SVGs are converted from it.
  Never add a second evaluation (each re-places and re-routes; 10+ min on a big board).
- **Placement gate** before routing (`--routing-disabled`, ~1 min): placement/overlap errors stop the build early.
- `build --fast` / `all --fast`: router effort 2x on a temp copy, for experiments only, never final runs.
- `fix` has a KiCad-verified safety net: nets KiCad finds unconnected after the track clean-up are rolled back.
- Exports run with `--disable-parts-engine`; `pcb fab` checks part availability once.
- `node_modules/tscircuit` carries local patches (`patches/tscircuit.json`, applied by `scripts/patch-tscircuit.mjs` as npm
  `postinstall`, so `npm install` keeps them; `hatch run pcb doctor` verifies them).
- **All fixes are listed in `docs/TOOLING.md` section 5.2** (name | what | why) - update it with any fix you add.

## Long builds: run them in the background

A `build` / `all` of a big board takes 10+ minutes and outlasts the synchronous terminal of most agents. Start it
detached (the tool's background mode, or `nohup ... > build.log 2>&1 &`), then poll the log / wait for the exit; do
not re-run it while it is running (each run re-places and re-routes). `--fast` is for experiments only.

## How to work on a board

`build` / `all` / `fix` / `check` / `fab` are verbose (stage log with elapsed time, live tsci output, summary);
add `--quiet` for the short output.

1. Write/modify `boards/<name>/index.circuit.tsx`. Use the **tscircuit skill**
   (`.claude/skills/tscircuit/`, elements in `elements/*.md`) for syntax; don't
   invent props. For details beyond it: `hatch run pcb docs` downloads the full
   docs to `docs/tscircuit-llms.txt` - grep it, never read it whole (900 KB).
2. `hatch run pcb build <name>`. It stops if `tsci check` reports errors: fix
   the TSX, not the output. Warnings are reported but don't stop the build.
3. **Look at the result**: Read `out/<name>/.work/preview/routed.png` and
   `.work/raw/<name>.netlist.txt` to verify connectivity (every pin on the intended net,
   LED/diode polarity, power pins).
4. `hatch run pcb fix <name>` (then look at `out/<name>/.work/preview/schematic.png`, crop
   to zoom) and `hatch run pcb check <name>`. Fix DRC/ERC
   findings at the *source* (TSX placement, trace widths, net names) when
   possible; only systematic tscircuit->KiCad export quirks belong in
   `fixes.py`.
5. Repeat until `check` exits 0 (errors = 0). Then `hatch run pcb fab <name>`:
   gerbers/drill zip, BOM and CPL in JLCPCB's format (BOM with LCSC + MPN), schematic PDF,
   top/bottom 3D renders, a bring-up sheet (`<name>-bringup.md`: rails, expected voltages, test points,
   power-on checklist) and, for a board with an MCU, the firmware pin header `<name>-pins.h`, all in
   `out/<name>/fab/` (after check, look at `.work/preview/board.png`; `hatch run pcb bringup <name>` makes
   just the last two).
   Never hand over fab files of a board whose `check` fails.

Silkscreen: every connector's pins labelled (`showSilkscreenPinLabels` or a `<silkscreentext>` per pin; `check` warns
about connectors without text next to them), LEDs / switches / test points with their function, the board with a
title and revision.

Test points by default: every supply rail and GND gets a `<testpoint name="TP1" footprintVariant="pad"
padShape="circle" padDiameter="1mm" />` (wired with `<trace from=".TP1 > .pin1" to="net.VCC" />`, a
`<silkscreentext>` with the rail name beside it), plus the key debug signals (reset, clocks, UART/SWD, enables).
A connector pin on the net also counts. `check` warns about a rail/GND without one; configure in `vibepcb.toml`
(`[testpoints] required / ignore / severity`, `src/vibepcb/config.py`). Test points are kept out of BOM and CPL by
the `exclude_testpoints_from_bom` fix. Use `padDiameter="1mm"` (other sizes need a 3D model from the network).

Layout quality bar, enforced by `check` (not by eye): a pour on every copper layer
(GND or a supply plane; `check` fails a layer without one), GND connects through the pours
(no redundant GND tracks), no track bend sharper than 135 deg and no acute angle at
junctions, no antennas (dangling track ends / vias), nothing unconnected.

Extra tscircuit checks: `hatch run tsci check netlist|placement|shorts <file>`
(`hatch run tsci ...` = the `tsci` the skill talks about).

## Board config (`boards/<name>/vibepcb.toml`)

What the TSX can't say: rule profiles (`[rules] profiles = ["jlcpcb-4l", "ipc-class2"]` from `rules/*.toml`, `[rules.override]`; fab minimums, Default net class, board thickness; `src/vibepcb/rules.py`), KiCad net classes (track width, clearance, diff-pair width/gap by net-name
pattern), length groups (max skew, e.g. an SDRAM bus), diff-pair rules, plane nets (`[planes]`: a via per
pad), keepout areas (`[keepout.<name>]`: no pour / tracks / vias under a part or in a box, per layer) and BGA escapes (`[escape.<ref>]`: VibePCB escapes the ball grid, tscircuit routes from the escape ends;
`src/vibepcb/fanout.py`). `fix` writes them into the
KiCad project; KiCad's DRC enforces them. Format: `src/vibepcb/config.py`. Nets get the name of a
named `<trace name="SD_DQ0">` (else the chip pin), so name the traces a rule should catch.

## Post-processing fixes (`src/vibepcb/fixes.py`)

A fix is `def my_fix(project: KicadProject) -> list[str]`: edit `project.pcb` /
`project.sch` (parsed S-expression trees) in place, return one line per change,
`[]` when nothing to do, and be **idempotent**. Register it in `FIXES` and add a
test in `tests/test_fixes.py` against the fixtures in `tests/data/`.

- Edit KiCad files only through `vibepcb.sexpr` (lossless). Do **not** use
  kiutils/kicad-skip: they drop KiCad 9 data / crash on tscircuit's output.
- In the tree, `Sym` = bare atom (`F.Cu`, `1.6`), plain `str` = quoted string.
  Keep that distinction when writing values (`Sym("1.6")` vs `"VCC"`).
- Which export/router quirks are already handled, and why: table in
  [docs/TOOLING.md](docs/TOOLING.md#52-table-of-all-fixes). Every track change is verified against
  KiCad's connection rules (pad: the track's end cap reaching its copper, i.e. the end within half the track width, or pad centre on the track; via: via centre on the track or a track end inside it, copper overlap is not enough;
  track-track: end to end), net by net, else polyline by polyline.
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

- Default to common PCB practice (every copper layer poured with GND or a supply, clearances at
  what the fab allows); any deviation needs a reason written in the board source.
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
- The schematic is generated (`schgen.py`), not tscircuit's: `schX/schY` are ignored. Its
  blocks are the TSX sections - group the parts under `{/* ===== title ===== */}` headers
  by function (supply, MCU, ...); a part's passives go in its section.
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
- Fine-pitch MCUs (0.5 mm LQFP): assign pins by package side (each signal leaves on the side facing
  its destination) and use 0.3/0.5 mm vias; with random pins and 0.6 mm vias the router dropped vias
  onto neighbouring pins (STM32 on smu-main: 39 errors -> 0).
- Imported QFP/QFN footprints may be rotated against the datasheet drawing (`pin1location(...)` in the
  footprint name): before assigning pins by side, check where each pin group lands (port positions in
  the circuit JSON) and rotate the part so its groups face their partners. A bus too wide for one side:
  split it over both corners next to the partner, not around one corner.
- Check display/connector footprints' orientation against the datasheet in the 3D render
  (`fab` top PNG): a dip12 7-segment rotated 90 deg passed every check but read sideways.
- BGA: don't let tscircuit's router into a ball field (it tangles: 199 acute angles on a 54-ball SDRAM) and
  don't use its fanout (dogbone + pour crashes, tscircuit/core#4660; saved `pcbTracePaths` + `routeRemaining`
  crash, #4663). Use `[escape.<ref>]`. Saved paths, if ever needed: group-relative coordinates, the first
  point within 1e-4 mm of the pad, every connection of the chip covered.
- SPICE: chips have no models, so per-board ngspice decks with behavioural models live in
  `boards/<name>/sim/*.cir`; `check` runs them (`ngspice -b`) and judges the `.measure` results against
  `* range: <measure> <min> <max> source=datasheet:...` lines (a range without a source is "assumed"; the deck's
  R/C/L must equal the board's values; every regulator needs a deck with `* covers: powerup <ref>`). Write
  what the board must do into `boards/<name>/intent.toml` (rails, voltages, current budget; `src/vibepcb/intent.py`).
  `check` also runs deterministic netlist checks into `boards/<name>/findings.toml` (accept a finding with
  `status = "accepted"` + `reason`). Vibe mode: warnings; `check --strict` fails on them and `fab` needs it.
  Before `fab`: `hatch run pcb review <name> --run` (a fresh-context reviewer checks every part against its
  datasheets; `docs/REVIEW.md`). Its issues land in `findings.toml`: fix the board or accept them with a reason.
  Never answer the review yourself: the designer must not grade its own board.

## Setup / environment

- `hatch run setup` installs Node.js *into the hatch env* (nodeenv) and runs
  `npm install` (tscircuit + bun as npm packages). No system node/bun needed.
- KiCad 9 or 10 (provides `kicad-cli`) is a system install: `sudo pacman -S kicad` (Windows: winget/installer, macOS:
  brew). Fixes were tuned on KiCad 10; 9 is supported, other versions get a warning at startup. Override the binary
  with `$KICAD_CLI`. Without it `check`/`fab` fail; `build` works and `fix` runs with a loud warning that its
  KiCad connectivity safety net is skipped.
- `hatch run pcb doctor` checks the whole setup (node, bun, tsci, patches, KiCad + version, symbol library,
  rsvg-convert, ngspice, network) and says what to install.

## Commands

| Command | Purpose |
|---------|---------|
| `hatch run pcb new <name>` | Scaffold `boards/<name>/index.circuit.tsx` |
| `hatch run pcb all <name>` | build + fix + check |
| `hatch run pcb publish <name>` | review set for GitHub in `boards/<name>/hardware/` (needs a passing `check`; no fab files) |
| `hatch run pcb cost <name> [--qty N]` | Batch cost, hand-assembled: BOM priced at LCSC (min order, multiples, stock) + bare boards (`[cost] pcb_usd` from a jlcpcb.com quote, else a labelled estimate; JLCPCB's quote API is not public) |
| `hatch run pcb rules <name>` | Merged design / manufacturing rule profiles of a board, each value's source |
| `hatch run pcb list` | List boards |
| `hatch run pcb doctor` | Check the setup (tools, KiCad version, patches, network); exit 1 on FAIL |
| `hatch run tsci <args>` | tscircuit CLI |
| `hatch run test` / `style` / `type` | pytest / ruff / mypy |
| `hatch run update-skill` | Re-vendor the tscircuit skill |

Always run `hatch run style`, `hatch run type` and `hatch run test` after changing Python code.

## Python conventions

Same as the author's other hatch projects: `from __future__ import annotations`
in every file, typing-only imports under `if TYPE_CHECKING:`, absolute imports,
`X | None` hints, error messages assigned to `msg` before `raise` (ruff EM).
Library code raises; only `cli.py` prints.
