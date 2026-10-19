# VibePCB tooling reference

A reusable description of how this project is built, run, checked and tested. Every
name below was checked against the code in `src/vibepcb/`, `tests/`, `pyproject.toml`,
`package.json` and `.pre-commit-config.yaml`.

Contents: 1. Pipeline - 2. Requirements and setup - 3. CLI commands - 4. Hatch scripts -
5. Fixes framework - 6. S-expression module - 7. Track cleaning - 8. Checks and post-fill -
9. Fab outputs - 10. Testing, style, types - 11. External tools - 12. Gotchas -
13. Porting to another project

---

## 1. Pipeline

```
boards/<name>/index.circuit.tsx        source of truth (tscircuit / React TSX)
    │  vibepcb build <name>   placement gate (tsci build --routing-disabled), one routed evaluation, exports from the JSON
    ▼
out/<name>/.work/raw/                  untouched tscircuit output, regenerated each build
    <name>.kicad.zip, <name>.netlist.txt, .pcb.svg, <name>.circuit.json (+ .work/preview/routed.png)
    (no schematic: builds evaluate a copy with schematicDisabled, in .work/src/)
    │  vibepcb fix <name>     unpack zip, rename to <name>.kicad_*, generate the schematic (schgen),
    │                         run registered fixes, schematic.svg (+ .work/preview/schematic.png)
    ▼
out/<name>/kicad/<name>.kicad_pro      KiCad project; open this one in KiCad
    │  vibepcb check <name>   kicad-cli ERC + DRC, refill zones, post-fill fixes, sharp-corner check
    ▼
out/<name>/check.txt, board.svg        (JSON reports and check.json in .work/reports/)
    │  vibepcb fab <name>     gerbers zip, BOM + CPL (JLCPCB format), schematic PDF, 3D renders
    ▼
out/<name>/fab/
```

`vibepcb all <name>` runs build, fix and check in sequence. `out/` is generated and
git-ignored. `out/<name>/README.md` (written by fix / check / fab) says what to open; internals live in
`.work/`. `.work/raw/` is never edited; all changes happen in `kicad/` through the fixes. PNG previews exist only
in `.work/preview/` (an agent can't read SVG); people get SVGs.

Ownership split: tscircuit produces the netlist and placement; KiCad's own tools
(`kicad-cli`) do ERC, DRC, zone refill and fab exports; this project only adds scripted
corrections for systematic export quirks, plus track cleanup and a few hard rules.

---

## 2. Requirements and setup

| Need | How |
|------|-----|
| Python >= 3.12 | `requires-python = ">=3.12"` (code uses `type` aliases) |
| hatch | the project manager; all commands are `hatch run ...` |
| Node.js + npm | not required on the system: `hatch run setup` installs Node LTS into the hatch env via `nodeenv` |
| tscircuit, bun | npm packages from `package.json` (`tscircuit`, `bun`, `@types/react`) |
| KiCad >= 9 (`kicad-cli`) | system install (`sudo pacman -S kicad`), or set `$KICAD_CLI`. Needed for `check` and `fab` only |
| ngspice | optional, only for `boards/<name>/sim/*.cir` decks (`ngspice -b <deck>`) |
| rsvg-convert | optional, makes the agent's PNG previews in `out/<name>/.work/preview/` from the SVGs |

Setup sequence:

```bash
hatch run setup          # nodeenv --python-virtualenv --node=lts (only if no node on PATH), then npm install
sudo pacman -S kicad     # or any KiCad >= 9 install; or export KICAD_CLI=/path/to/kicad-cli
hatch run pcb docs       # optional: tscircuit LLM docs -> docs/tscircuit-llms.txt
```

`hatch run setup` is one script with two commands. The first runs
`python -c "...shutil.which('node')..." || nodeenv --python-virtualenv --node=lts`, so
it skips the Node install when a `node` is already on PATH.

Hatch env (`[tool.hatch.envs.default]`): `installer = "uv"`, `python = "3.12"`,
`features = ["dev"]`, extra dependency `nodeenv`. The env lives in `.hatch/vibepcb`
(`.hatch/` is git-ignored; the basedpyright config points there).

Runtime dependencies: none (`dependencies = []`, stdlib only). KiCad files are edited by
the project's own `sexpr` module because kiutils and kicad-skip drop KiCad 9 data.

---

## 3. CLI commands

Entry point: `vibepcb = "vibepcb.cli:main"` (`[project.scripts]`). From the project,
`hatch run pcb <args>` runs `vibepcb <args>` inside the hatch env. The module
docstring of `cli.py` doubles as `--help` text.

| Command | Does | Writes | Exit code |
|---------|------|--------|-----------|
| `new <board>` | writes a starter board (a 30x20 mm board with R1, C1 and a trace) to `boards/<board>/index.circuit.tsx`; refuses if the file exists | `boards/<board>/` | 1 if it exists |
| `build <board> [--fast]` | placement gate: `tsci build --routing-disabled`, stops on placement/outside-board/overlap/clearance errors (about 1 min on a 238-part board); then ONE routed `tsci export -f circuit-json` (stops on `*_error` elements), and `kicad_zip`, netlist, PCB SVG are converted from that JSON; PNG via `rsvg-convert`. tscircuit always evaluates a copy in `out/<board>/.work/src/` with `schematicDisabled` (`tscircuit.prepared_source`: its schematic layout does not scale, `fix` generates the schematic); `--fast` also sets `autorouterEffortLevel="2x"` in that copy | `out/<board>/.work/raw/` | 1 on errors or unknown board |
| `fix <board>` | removes and re-creates `out/<board>/kicad/` from the raw zip (the footprint library `lib/footprints/tscircuit_builtin.pretty` is written from the footprints embedded in the board, `kicad.write_footprint_library`), renames the files to the board name, replaces the schematic with `schgen.write` (from the circuit JSON + the TSX sections), then runs `fixes.apply_all` and prints one line per change; exports `schematic.svg` (+ `.work/preview/schematic.png`, 4000 px) and refreshes `README.md` | `out/<board>/kicad/` | 0 (exceptions reported as `error:`, exit 1) |
| `check <board>` | `kicad-cli sch erc`, `kicad-cli pcb drc` (refills zones, saves the board), post-fill loop, sharp-corner and unlabelled-connector checks; prints the summary, writes it to `check.txt`, exports `board.svg` | `out/<board>/check.txt`, `board.svg`, `.work/reports/`, modifies the KiCad PCB | 1 if ERC errors, DRC errors or sharp corners |
| `all <board>` | `build`, then `fix`, then `check`, short-circuiting on a non-zero result | all of the above | first non-zero, else `check`'s result |
| `fab <board>` | part availability check once (parts engine, placement-only evaluation), then `kicad.export_fab` on `out/<board>/kicad/<board>.kicad_pcb`: one file per purpose (gerbers zip, JLCPCB BOM with MPN, CPL, schematic PDF, top/bottom renders); intermediates in `.work/fab/` | `out/<board>/fab/` | 0 |
| `publish <board>` | only if the last `check` passed on the current board (`.work/reports/check.json`, board hash): KiCad project without 3D models, BOM, schematic PDF, renders (`kicad.export_docs`), layout PDF (a page per copper layer) + front/back SVGs (`kicad.export_layout`) | `boards/<board>/hardware/` (committed) | 0 |
| `list` | prints board names (folders with `index.circuit.tsx` under `boards/`) | none | 0 |
| `docs` | downloads `https://docs.tscircuit.com/llms.txt` to `docs/tscircuit-llms.txt` (60 s timeout) | `docs/` | 0 |

Error handling in `main()`: `TscircuitError`, `KicadCliError` and `FileNotFoundError` are
caught and printed as `error: <message>` with exit 1. Other exceptions propagate with a
traceback. Only `cli.py` prints; library modules raise.

`check` detail (the console output is meant to be read by an LLM):

- ERC and DRC are printed through `kicad.CheckResult.__str__`: a summary line
  (`ERC: N issue(s) {...} - full report: <path>`) and one line per violation, plus
  one line per item with its position in mm.
- `Track bends: N sharper than 135 deg` followed by `[error] sharp_corner: ...` lines.
- Exit 1 if `erc.errors`, `drc.errors` or any sharp corner remains. Warnings do not fail
  the check (the severity count uses `--severity-all` for the report, but `errors` only
  counts `severity == "error"`).

---

## 4. Hatch scripts

Defined in `pyproject.toml` under `[tool.hatch.envs.default.scripts]`:

| Script | Command | Purpose |
|--------|---------|---------|
| `setup` | node check/`nodeenv`, then `npm install` | one-time environment setup |
| `pcb` | `vibepcb {args}` | the PCB pipeline (section 3) |
| `tsci` | `npx --no-install tsci {args}` | raw tscircuit CLI, e.g. `hatch run tsci check netlist boards/x/index.circuit.tsx`, `hatch run tsci dev boards/x/index.circuit.tsx` |
| `update-skill` | `rm -rf .claude/skills/tscircuit`, `git clone --depth 1 https://github.com/tscircuit/skill ...`, remove its `.git` | re-vendor the official tscircuit agent skill |
| `clean` | `git clean -ffX .tmp`, `git clean -ffX out` | delete git-ignored temp files and generated boards |
| `test` | `pytest {args:tests}` | run tests |
| `type` | `mypy --install-types --non-interactive src tests {args}` | type check |
| `style` | `ruff check {args:.}` | lint without auto-fix |
| `fix` | `pre-commit install`, then `pre-commit run {args:--all-files}` | format and lint with auto-fixes |
| `update-precommit` | `pre-commit autoupdate --freeze`, `pre-commit install` | bump the frozen hook revisions |

Other env definitions: `hatch-uv` (provides `uv>=0.6.16`), `all` (`dev-mode = true`,
editable install, Python 3.12), `ciall` (`dev-mode = false`, installs the built wheel so
CI tests the packaged code), `ci` (`skip-install = true`).

`[tool.hatch.build.targets.wheel] packages = ["src/vibepcb"]`. Version comes from
`__version__` in `src/vibepcb/__init__.py`.

---

## 5. Fixes framework (`src/vibepcb/fixes.py`)

### 5.1 Contract

```python
type Fix = Callable[[KicadProject], list[str]]
```

- A fix edits `project.pcb` / `project.sch` (parsed S-expression trees) in place and
  returns one human-readable line per change. `[]` means nothing to do.
- **Idempotent**: a second call on the output must return `[]`. The existing tests
  assert this with `assert fixes.xxx(project) == []  # idempotent`.
- Fixes never touch files directly, except the few that also rewrite side files
  (`strict_track_rules` writes `<stem>.kicad_dru` and `.kicad_pro`).
- `KicadProject` (dataclass): `directory`, `pcb`, `sch`, `pcb_path`, `sch_path`.
  `KicadProject.load(directory, stem)` parses both files; `save()` writes both.

### 5.2 Table of all fixes

Run order = `FIXES` order in `fixes.py`. Stage: **build** = `cmd_build`, **fix** = `cmd_fix`
(`fixes.apply_all`), **check** = `cmd_check` (needs KiCad's zone fill, so not in `FIXES`).
Track geometry lives in `src/vibepcb/tracks.py`; every track change is kept only if KiCad's
connection rules still join the net's pads (checked net by net, else polyline by polyline).

| # | Name | What it does | Why (tscircuit / router quirk or rule) |
|---|------|--------------|----------------------------------------|
| - | `tscircuit.routed_errors` (build) | Build stops only on source/netlist errors and an aborted router; tscircuit's copper DRC is reported | `fix` repairs copper issues and KiCad's DRC in `check` is the authority |
| - | `tscircuit.repair_trace_nets` (build) | Re-links each routed trace to the `source_trace` of the net its end ports belong to | Autorouter sometimes tags a route with another net's id; the export then has shorts and broken nets |
| - | `fanout` (build) | For `[escape.<ref>]` parts in `vibepcb.toml`: plans the BGA escape from the placement-only JSON (ring 0 straight out on top, ring 1 through the channel between ring-0 balls, deeper balls a dogbone via + an escape layer along a ball line; side = nearest, ties towards the net's other pads; balls of `[planes]` nets only a via to their plane), checks its clearances, gives the part a stand-in footprint (a pad per escape end, none for via-only balls) and a keepout over the package for the routed evaluation; plan in `.work/raw/<board>.escapes.json` | tscircuit's router crowds BGA escapes on one side, its dogbone solver crashes with copper pours (tscircuit/core#4660), saved `<fanout pcbTracePaths>` crash its TopologyMergingSolver even for one straight escape (tscircuit/core#4663) |
| 1 | `rename_auto_nets` | Selector-style net names -> the schematic's names (`schgen.pin_net_names`: `U1_VREG_1V2` after the chip pin, else `Net-(R1-Pad2)`) | tscircuit exports names KiCad does not use; schematic and board must agree (parity, "Update PCB from Schematic") |
| 2 | `propagate_split_pad_nets` | Gives every pad of a split pad (same number) the net of its siblings; marks the part `duplicate_pad_numbers_are_jumpers` | Export puts the net on one pad only; KiCad reports the rest as unconnected copper |
| 2b | `apply_escapes` | Stand-in pads of escaped parts become the real balls again (nets carried over; via-only plane balls get their plane net), the planned escape tracks and vias are added, the courtyard is drawn, the build's keepout and the pour holes tscircuit cut under it go; `plane_pad_vias` leaves the escaped ball field alone | Second half of the `fanout` build step: tscircuit's tracks end exactly at the escape ends |
| 2c | `apply_keepouts` | `[keepout.<name>]` in `vibepcb.toml` becomes KiCad rule areas (named `vibepcb:keepout:<name>`) over a part's pads + courtyard or a box: no pour, tracks and/or vias on chosen layers (a global GND pour must not run under an antenna, a crystal, a switching node) | tscircuit's `<keepout>` forbids everything and has no per-part, per-layer or pour-only form; KiCad's fill and DRC enforce the area, the router keeps out of its track / via restrictions |
| 2d | `apply_routes` | `[routing] mode = "manual"`: `routes.toml` becomes tracks and vias (`vibepcb.routes`: board model, `drc`, `router`); the board before the routes is saved to `.work/unrouted.kicad_pcb` for `pcb route` | tscircuit's autorouter is the weak spot on boxed-in boards; here the agent steers (order, layers, waypoints, corridors), the router fills in clearance-checked paths |
| 2e | `apply_pours` | `[pour.<name>]` (net, layers, margin) adds a pour for a net and layer the board has no zone for (a copy of the board's pour settings over the outline inset by margin) | tscircuit emits a pour only together with routing: a placement-only build (`[routing] mode = "manual"`) has no pour on e.g. a 3.3 V plane layer, and `check` fails a layer without a pour |
| post | `stitch_pour_islands` (check, after the refill) | A via into each piece of a net's pour that has no via or through-hole pad of its net, at the spot nearest its middle that lies fully in the fill, keeps clearance on every layer and lands in the net's pour on another layer; kept only if KiCad then reports fewer unconnected items | Tracks cut an outer GND pour into pieces; a piece without a via floats (unconnected zone items) although the plane is right below |
| 3 | `repair_zero_width_tracks` | Zero-width segments take the width of the tracks they join | Router occasionally emits width 0 = copper-less, broken connection |
| 4 | `drop_tracks_inside_pads` | Removes segments lying wholly inside a same-net pad; an in-pad wiggle that a track enters at the pad edge becomes one straight piece to the pad centre | Tiny stubs in custom pads add no connection and fail min track width; the wiggles make acute angles with the entering track |
| 5 | `merge_duplicate_vias` | Keeps one via (most layers) where several of one net sit on one spot; same-net vias with holes closer than the hole-to-hole minimum merge, their tracks bridged to the kept one | Router exports through + blind via at one centre; other fixes then trip over the twin. Holes 0.14 mm apart are a drill hazard (`hole_to_hole`) |
| 5b | `plane_pad_vias` | For `[planes] nets` in `vibepcb.toml`: the router's tracks/vias of the net go, every SMD pad gets a track (straight, else a short maze-routed dogleg) to its own via into the net's zone on another layer (<= 3 mm, never in a pad); a pad without room joins a neighbouring same-net pad. All or nothing per net | tscircuit wires plane nets pad to pad; the tracks knot up in fine-pitch pin fields (QFP GND/VCC). Common practice is a via per power pad |
| 6 | `relocate_vias` | Moves vias too close to other nets to the nearest clear spot (<= 3 mm), with the track ends meeting them; else moves it and reroutes the first 2 mm of each of its runs, spliced into the rest | Crowded fan-outs: vias on neighbouring pads (shorts, hole/mask errors); rerouting a whole 50 mm run was one search too big to finish |
| 7 | `snap_near_ends` | Joins a track end that overlaps another track of its net (or ends a hair from its end) onto it | KiCad connects overlapping copper and measures its angle; the router leaves ends microns apart |
| 8 | `clean_tracks` | Per net: drops duplicate routes, loops, zig-zags inside pads, spikes; cuts/chamfers bends sharper than 135 deg; splits T-junctions (only where the snapped track keeps its clearance); a pad's track to a via inside a zone of its net counts as its connection (the via reaches the plane) | Autorouter output is messy; quality bar is no sharp bends / no antennas. Snapping a T pivots the whole track: a 7 mm one swung 0.05 mm into a via |
| 9 | `separate_ends_on_pads` | Pulls apart track ends meeting at an acute angle on a pad/via | KiCad `track_angle` measures them although the pad joins them anyway |
| 10 | `square_tees` | Slides a track ending on another track's side so it meets at 90 deg (in passes: tracks touching one moved in this pass wait for the next) | `track_angle` measures T-junctions too; two branches of one junction moved at once both landed on the other's old position |
| 11 | `push_tracks_clear` | Slides track pieces sideways (<= 0.3 mm) that are a few hundredths too close to other nets; an end on a pad slides along inside the pad | Router squeezes tracks past vias slightly under clearance, often the piece leaving a pin |
| 12 | `reroute_tracks` | Rips up runs that still break a rule and reroutes the bad stretch (<= 25 mm, of a run of any length) with a local 45-deg maze router; keeps pieces a T-branch, via or pad joins mid-run (the stretch stops next to them); counts tracks passing through a via as junction branches; leaves a junction on a slightly tilted track with one off-grid stub; never reroutes its own new routes again | Last resort for cramped spots the geometric fixes cannot solve; old route stays if none found |
| 13 | `remove_antennas` | Deletes tracks with a loose end and vias joining fewer than 2 layers; a track end that runs past a pad of its net is cut back to the pad centre instead (the track is the pad's only link) | Quality bar: no antennas; a route starting at an escape end whose escape track the clean-up removed crosses its ball and sticks out |
| 13b | `tune_lengths` | Meanders (`vibepcb.tune`) on the short nets of each `[length_group]`: bumps with 45-degree corners on the longest straight pieces, height solved for the exact length, every piece clearance-checked (other side / lower bumps / next piece when it does not fit); target `tune_to` or, without it, nets breaking `max_skew` to 90 % of it | KiCad's DRC only checks a group's skew; routing (tscircuit's or VibePCB's) makes lengths by chance |
| 14 | `drop_npth_edge_cuts` | Removes Edge.Cuts copies of footprint NPTH holes | Export writes holes twice; DRC then applies 0.5 mm board-edge clearance to them |
| 15 | `allow_footprints_in_keepouts` | Footprints may overlap keepouts; copper/pour restrictions stay | Export writes `footprints not_allowed`, so an antenna keepout flags its own module |
| 16 | `standard_board_thickness` | 1.4 mm -> 1.6 mm | Export default; fabs default to 1.6 |
| 17 | `fix_value_equals_reference` | Derives Value from the footprint where Value == Reference | tscircuit sets Value = Reference for valueless parts |
| 18 | `fill_schematic_footprints` | Copies each footprint lib id into the schematic's empty Footprint field | Otherwise "Update PCB from Schematic" deletes every footprint |
| 19 | `schematic_net_labels` | Net-name texts next to wires become real net labels | Export draws label stubs as plain text; ERC sees unconnected pins |
| 20 | `schematic_no_connects` | No-connect flag on every schematic pin whose pad has no net | Export has none; ERC fails on every unused pin |
| 21 | `schematic_power_symbols` | GND/supply rail parts become power symbols, plus one PWR_FLAG per rail | Rails are exported as ordinary parts (own net each, in BOM, on board) |
| 22 | `drop_loose_power_symbols` | Removes power symbols whose pin touches no wire, label or pin | tscircuit's schematic can leave a rail symbol standing alone; ERC flags its hidden pin |
| 23 | `add_part_numbers` | Copies MPN / LCSC from the circuit JSON into hidden fields | Export drops them, so the BOM could not say what to buy |
| 24 | `link_footprint_library` | Registers the footprint library under the `tscircuit` nickname | Footprints are `tscircuit:<name>`, library is `tscircuit_builtin` |
| 25 | `footprint_internal_clearance_rule` | Custom DRC rule: hole clearance inside one footprint follows its land pattern | Manufacturer land patterns (USB-C) sit under the board-wide hole clearance |
| 26 | `zone_thermal_reliefs` | Pad-to-pour connections via narrow thermal spokes instead of solid | Solid connect on 0402s causes tombstoning and hard rework |
| 27 | `thermal_spoke_rule` | Accepts a single spoke per pad | Hemmed-in pads get one spoke; KiCad would report `starved_thermal` |
| 28 | `router_clearance_tolerance` | 5 um slack on copper clearance | Router targets exactly the clearance; float error leaves gaps 1-2 um short |
| 29 | `strict_track_rules` | DRC rule "no acute angles" (`track_angle` min 89); `track_dangling`, `via_dangling`, `track_angle` set to error | KiCad only warns about dangling items by default; 135-deg bends are checked by `sharp_corners` |
| 29b | `apply_board_config` | Net classes (widths, clearance, diff-pair width/gap, net-name patterns) into .kicad_pro; length groups as `skew` rules and diff-pair `diff_pair_uncoupled` rule into .kicad_dru, from `boards/<board>/vibepcb.toml` (see `vibepcb/config.py`) | tscircuit has no net classes or KiCad length rules; KiCad DRC (and `check`) then fails on skew / uncoupled length |
| 30 | `place_reference_designators` | 0.8 mm reference text moved to the first free spot around the part | tscircuit's 0.4 mm text at fixed offsets lands on pads/silk/edge |
| 31 | `fit_schematic_sheet` | Smallest landscape ISO sheet that fits, centred, with title block | Export picks A0/A1 and draws in a corner |
| - | `schgen.write` (fix) | Generates the schematic from the circuit JSON: one framed block per TSX section (`{/* ===== title ===== */}`), R/C/L/D/crystal/button symbols, two-terminal parts wired as trees off the chip pins they serve (straight chains, the net label below), boxes with supply pins on top / grounds at the bottom and same-net pins joined into one symbol, rail symbols for GND / positive / negative supplies (a supply must feed a chip power pin), global labels on every wired net (double arrow when it leaves the block), decoupling (rail-to-rail) caps hung on the supply pin of the chip they serve, block arrangement chosen for the smallest paper. Chips get real symbols (section 6.1): pins keep the library pitch, a pin's passives jog to a free row or, on long pin rows / when they would push neighbours, move next to the chip behind a net label; top supply pins carry a bus with the caps hanging down to GND; each unit of a multi-unit part (dual op-amp) is its own cell. Each passive belongs to the chip it sits next to on the PCB (`pcb_component` centres, among the chips it connects to): trees only grow through a chip's own passives, and a chip is placed as one piece with everything it keeps. MOSFETs with only G/S/D pins (SO-8) get KiCad's generic `Q_[NP]MOS_GSD`, repeated pins stacked, P-channel when the source is on a positive supply. Cells are skyline-packed. The drawing is checked against the netlist (`schgen.connectivity_errors`) and redrawn with boxes if a symbol ever broke a net | tscircuit's schematic layout does not finish on a few hundred parts, so builds always disable it; BOM/ERC/PDF need symbols. Runs in about a second on a 239-part board (first run: LCSC downloads, cached) |
| - | `restore_nets` (fix, end) | Nets KiCad still finds unconnected after the track fixes go back to their routed state, then `merge_duplicate_vias` again | Safety net: one unclean net beats a disconnected one; the routed state has the stacked vias back (`holes_co_located`) |
| - | `prune_pour_tracks` (check) | Removes tracks of a poured net that KiCad's fill already provides | tscircuit routes GND as tracks before pouring; redundant, take room, short thermal reliefs |
| - | second pass (check) | After pruning: `clean_tracks`, `separate_ends_on_pads`, `square_tees`, `push_tracks_clear`, `reroute_tracks`, `remove_antennas` again | Pruned tracks free room for corners that had none |
| - | repair after restore (fix) | After `restore_nets`, `relocate_vias` + `push_tracks_clear` once more; undone if KiCad then finds another net broken | A restored net's routed geometry can clash with its cleaned-up neighbours |
| - | post-fill second pass, repeated (check) | The second pass runs up to 3 times while it still changes something; it includes `relocate_vias` | Each step leaves work for an earlier one (a snap creates a T to square, ...) |
| - | `unlabelled_connectors` (check, warning) | Connectors (J..., 3+ pads) with no board silkscreen text within 6 mm | A header nobody can read is useless on the bench |
| - | `stitch_unconnected_pads` (check) | A pad of a poured net that KiCad reports unconnected after the fill gets a via beside it (not in it) into the net's filled plane on another layer, plus a short track; kept only if DRC finds fewer unconnected items | Pads boxed in by other nets' tracks get no copper from their own layer's pour, and tscircuit routes nothing to a poured net (seen: an ESD part's GND pin between the USB lines) |
| - | `restore_nets` (check, last attempt) | If DRC still finds more unconnected items after 4 post-fill attempts, those nets are restored from before the post-fill | Same safety net, after the refill |
| - | `sharp_corners`, `unpoured_copper_layers` (check) | Not fixes, hard gates: bends < 135 deg; copper layers without a pour fail `check` | Quality bar: 135-deg minimum (not expressible in KiCad); common practice = every layer poured |

`apply_all(project, skip=None)` runs every `FIXES` entry (except `skip`), saves once, and returns
`{name: [changes]}`. `fix` prints `fix <name>: <change>` per change.

### 5.3 Adding a fix

1. Write `def my_fix(project: KicadProject) -> list[str]:` in `fixes.py`. Use
   `sexpr` helpers (section 6). Return one line per change. Keep the output deterministic.
2. Register it in `FIXES` at the right position. Order matters: net renames before
   anything keyed on net names, track cleanup after the pad-track fixes.
3. Add a test to `tests/test_fixes.py` using the `project` fixture. It copies
   `tests/data/led-indicator.kicad_pcb` and `.kicad_sch` into a temporary `board.*` project.
   Assert the change, then assert the second call returns `[]`.
4. If the fix needs KiCad's zone fill, it does **not** belong in `FIXES` (section 8).

Fixes must be deterministic. New track UUIDs are derived from content, because KiCad's
zone fill depends on item order and random UUIDs make `check` results vary between runs.

### 5.4 Strict rules

`strict_track_rules` makes KiCad fail on what the project cares about:

- writes `(rule "no acute track angles" (constraint track_angle (min 89)))` to
  `<stem>.kicad_dru` (`MIN_JUNCTION_DEG - 1`). KiCad's `track_angle` is an interior-angle
  "no acute angles" rule; it also measures junction branches, which cannot all be 135 degrees apart.
- sets `track_dangling`, `via_dangling` and `track_angle` to `error` in `<stem>.kicad_pro`
  under `board.design_settings.rule_severities`.

The 135 degree bend minimum is not a KiCad rule here; it is `fixes.sharp_corners` (section 8).

---

## 6. Schematic symbols (`symlib.py`, `easyeda.py`)

`schgen` draws a chip with a real symbol when one fits its netlist, else with a generated box:

1. **KiCad's library** (`symlib.Library`): the installed `*.kicad_sym` directory (`$KICAD*_SYMBOL_DIR`,
   `/usr/share/kicad/symbols`, the flatpak runtime). Names are indexed once (`~/.cache/vibepcb/kicad-symbols.json`,
   keyed by path + mtime); a name matches the MPN's start, a lowercase `x` being a wildcard
   (`STM32G431RBTx` matches `STM32G431RBT6`). `extends` is resolved, resolved symbols are cached.
2. **LCSC / EasyEDA** (`easyeda.fetch_all`, `easyeda.to_symbol`) by the part's LCSC number: the EasyEDA
   component JSON (`https://easyeda.com/api/products/<C...>/components`) is downloaded once into
   `~/.cache/vibepcb/easyeda/` and converted (pins, rect, polyline, polygon, ellipse, arc, text; sub-parts
   become units). Offline, an uncached part keeps the box. Own converter: `easyeda2kicad` is AGPL.

`symlib.fits` decides: identical pin numbers, no stacked pins on different nets, and (KiCad library only)
at least half the pin names agree; LCSC symbols come from the same data as tscircuit's imports, so their
numbers are authoritative. `symlib.for_schematic` makes every pin passive (tscircuit has no pin types)
and shows hidden pins that are not stacked under a visible one. `fixes._pin_positions` is unit-aware.

## 6a. S-expression module (`src/vibepcb/sexpr.py`)

A lossless reader and writer for KiCad files. A node is a `list`; its items are:

- `Sym`: a bare atom (`footprint`, `F.Cu`, `1.6`). `class Sym(str)`.
- `str`: a quoted string (`"VCC"`).
- nested `list`s.

The distinction matters on write. `Sym("1.6")` is written as `1.6`; plain `"1.6"` is written
as `"1.6"`, which KiCad rejects for numeric fields. Keep it in mind for every value you set.

| Function | Purpose |
|----------|---------|
| `loads(text)` / `dumps(node)` | parse one top-level expression; write it back (ends with newline) |
| `load(path)` / `dump(node, path)` | the same with UTF-8 files |
| `head(node)` | leading `Sym` of a list (`"footprint"`) or `None` |
| `children(node, name)` | direct sub-lists whose head is `name` (generator) |
| `child(node, name)` | first such sub-list or `None` |
| `walk(node)` | all list nodes, depth first, including `node` |
| `property_node(node, key)` / `property_value(node, key)` | `(property "<key>" "<value>" ...)` of a footprint or symbol |

Parser details: `loads` raises `ValueError` on unbalanced parens, an unexpected character,
or when the text is not exactly one top-level list. Quoted strings unescape `\"`, `\\`,
`\n`, `\t`, `\r`. The writer puts leaf-only lists on one line and indents lists that
contain sub-lists.

Tests: `tests/test_sexpr.py` checks atoms and escapes, bare-vs-quoted output, a lossless
round trip of the test data (parametrized), rejection of malformed input and the helpers.

---

## 7. Track cleaning (`clean_tracks`, `src/vibepcb/tracks.py`)

`tracks.py` is pure geometry on plain data (`Seg`, `Obstacle`, points as `(x, y)` tuples,
KiCad millimetres with y down). It never touches the S-expression tree. `fixes.py`
extracts the board's copper into these types, calls `tracks`, and writes back.

Constants in `tracks.py`:

| Name | Value | Meaning |
|------|-------|---------|
| `EPS` | 0.01 mm | points closer than this are the same point |
| `SPIKE_DEG` | 10 | a polyline turning back by more than 170 degrees is a spike |
| `MIN_CORNER_DEG` | 135 | interior angle: 180 straight, 135 a 45 degree bend |
| `CORNER_TOLERANCE_DEG` | 0.5 | rounding tolerance |
| `MIN_JUNCTION_DEG` | 90 | minimum branch angle at a junction |
| `PAD_INSET_MM` | 0.1 | a trimmed track ends this far inside a pad/via edge |
| `SHORTCUT_LEG_MM` | 0.3 | corners with a leg this short are cut straight across |
| `CHAMFER_STEPS_MM` | (0.5 ... 0.08) | chamfer sizes tried, largest first |
| `NECK_MAX_MM` | 0.6 | only short new pieces may be narrower than their track |
| `MOVE_STEPS_MM`, `MOVE_DIRECTIONS` | 0.1-0.6 mm, 32 | corner/junction move search |
| `COVER_SAMPLE_MM` | 0.05 mm | sampling for "covered by other copper" |

Main functions in `tracks.py` (names as in the code): `interior_angle`,
`point_seg_distance`, `seg_seg_distance`, `point_in_polygon`, `chains`, `trim_ends`,
`drop_inside_start`, `simplify`, `round_off_corners`, `sharp_corners` (the geometric
check on points; not the same function as `fixes.sharp_corners`), `clearance_check`,
`pad_groups`, `still_connected`, `sharp_junctions`, `tees`, `junctions`, `move_junction`,
`redundant`, `dangling`.

`fixes.clean_tracks` runs up to `CLEAN_ROUNDS = 3` passes of `_clean_round` (per net:
`_clean_net`, with `_split_tees`, `_snap_via_ends`, `_open_junctions`, `_open_loops`).
Per net it:

1. removes duplicate segments covered by the net's other copper;
2. per polyline between pads, vias and junctions: trims the part inside its end pads,
   drops duplicate, spike and collinear points, cuts sharp corners across or chamfers them
   to 45 degrees;
3. keeps every new piece only if it keeps the net-class clearance to other nets' copper and
   the board-edge clearance (`_design_clearances` reads the default net class / rules,
   falling back to 0.2 mm and 0.5 mm);
4. verifies connectivity with `pad_groups` / `still_connected`, which follow KiCad's rules
   (pad: track end inside it or pad centre on the track; via: via centre on the track;
   track-track: end to end). If a net's connectivity breaks, it falls back to per-polyline
   decisions, and a polyline that cannot be cleaned without cutting a connection is left as routed.

Reported summary lines: removed duplicates, dropped points, removed sharp corners,
corners left (no room, check by eye), polylines kept as routed.

Related fix `remove_antennas`: deletes track stubs whose loose end touches no pad, via,
same-net track or (after refill) zone, repeating until nothing dangles; and deletes vias
that connect fewer than two layers.

Tests: `tests/test_tracks.py` covers geometry (angles, simplify, corner rounding,
clearance check, redundant/dangling, pad groups, tees and junctions). `tests/test_fixes.py`
has `test_clean_tracks_*` and `test_strict_track_rules` on the fixture board.

---

## 8. Checks and post-fill logic (`kicad.py`, `cli.cmd_check`, `fixes.apply_post_fill`)

### 8.1 `kicad.py`

- `find_kicad_cli()`: `$KICAD_CLI` or `kicad-cli` on PATH. `_kicad_cli()` raises
  `KicadCliError` with an install hint if none is found.
- `assemble_project(zip, library_dir, target, stem)`: wipes `target`, extracts the zip,
  renames `index.circuit.*` to `<stem>.*`, sets `project_name`, `last_opened_files` and
  `last_opened_board` in the `.kicad_pro` JSON, and copies `lib/` (without `*-lib-table`).
- `run_drc(pcb, report)`: `kicad-cli pcb drc --format json --severity-all --schematic-parity
  --refill-zones --save-board`. It merges `violations`, `unconnected_items` and
  `schematic_parity`. Note `--refill-zones --save-board` rewrites the board file.
- `run_erc(sch, report)`: `kicad-cli sch erc --format json --severity-all`, merges per-sheet violations.
- `CheckResult` (dataclass): `errors` (severity error), `unconnected`,
  `unconnected_spots()` (mm positions of unconnected items), `__str__` (LLM-readable).
- `export_fab(pcb, out_dir)`, `jlcpcb_files(bom, pos, gerbers, out_dir)`: section 9.
- `KicadCliError` is raised for missing CLI or a failed command without a report file.

### 8.2 The `check` post-fill loop

`check` does not just run ERC and DRC:

1. Run ERC and DRC in parallel (two independent kicad-cli processes). The DRC refills zones with KiCad and saves the board
   (tscircuit's pre-computed pour is a mosaic with floating islands; KiCad's fill has
   thermal reliefs and island removal, and the Gerbers use it).
2. Remember the board bytes (`backup`) and the number of unconnected items (`before`).
3. Up to `POST_FILL_ATTEMPTS = 4` times: `fixes.apply_post_fill(project, keep)`. If it
   changes nothing, stop. Otherwise re-run DRC. If unconnected items did not increase, print
   the changes and stop. If they did, add the spots KiCad reports unconnected to `keep`
   and restore `backup`, then retry. `keep` makes `prune_pour_tracks` leave tracks near
   those spots (`KEEP_RADIUS_MM = 1.5`) in place.
4. If all attempts fail (`for ... else`), print `gave up after 4 attempts` and re-run DRC on
   the restored board.
5. Run `fixes.sharp_corners` and print each bend sharper than 135 degrees.
6. Print ERC, DRC and the bend list. Exit 1 on any error.

`apply_post_fill` (`fixes.py`): `prune_pour_tracks`, then if it changed something, a second
pass of `clean_tracks` and `remove_antennas(filled_zones=True)`, then write the PCB.

`prune_pour_tracks`: removes tracks of a poured net when every sample point (every
`POUR_SAMPLE_MM = 0.05` mm) is in the net's fill on that layer, in one of its pads, or in
the thermal gap (`THERMAL_GAP_MM + 0.05`) of such a pad. The pour already connects those
tracks; they only take room and short the thermal reliefs.

`fixes.sharp_corners(project)`: bends between exactly two track ends that are sharper than
134 degrees (`MIN_CORNER_DEG - 1`), not counting bends on a pad or via of their net. This is
the 135 degree rule that KiCad's `track_angle` cannot express.

Why post-fill is separate from `FIXES`: it needs KiCad's zone fill, which only exists after
`kicad-cli` refilled and saved the board. `fix` always re-assembles from the raw zip, so
`FIXES` runs on fresh tscircuit output only. The KiCad 10 save writes `(net "NAME")` without
ids, which the registered fixes do not expect.

---

## 9. Fab outputs (`kicad.export_fab`)

`vibepcb fab <board>` writes into `out/<board>/fab/`. It does not run `check`.

| File | Source |
|------|--------|
| `<board>-gerbers.zip` | `kicad-cli pcb export gerbers` + `drill` into a `gerbers/` folder, zipped (deflate) |
| `<board>-bom.csv` | `sch export bom`, grouped by `Value,Footprint,LCSC,MPN`, `--exclude-dnp`; fields Qty, Designator, Value, Footprint, MPN, LCSC Part # |
| `<board>-pos.csv` | `pcb export pos --format csv --units mm --side both --exclude-dnp` |
| `<board>-schematic.pdf` | `sch export pdf` |
| `<board>-top.png`, `<board>-bottom.png` | `pcb render`, 1600x1200, high quality |
| `jlcpcb/<board>-gerbers.zip` | copy of the gerber zip |
| `jlcpcb/<board>-bom-jlcpcb.csv` | columns Comment, Designator, Footprint (last `:` part), LCSC Part # |
| `jlcpcb/<board>-cpl-jlcpcb.csv` | columns Designator, Mid X, Mid Y (`mm` suffix, 4 decimals), Layer (Top/Bottom), Rotation; rows without a reference (mounting holes) are skipped |

Gerber and drill export clear the `gerbers/` folder first. Run `check` first; the CLI does
not enforce it, but the project rule is "never hand over fab files of a board whose check
fails" (CLAUDE.md).

---

## 10. Testing, style and types

- **Tests**: pytest, `testpaths = ["tests"]`, `--strict-markers --strict-config -ra`, markers
  `slow` and `integration` (declared, not used by a script). Test files: `test_sexpr.py`,
  `test_tracks.py`, `test_fixes.py`. Data: `tests/data/led-indicator.kicad_pcb` and `.kicad_sch`
  (KiCad 9 format). `test_jlcpcb_files` uses `tmp_path`. Run with `hatch run test`.
  Coverage is configured (`[tool.coverage.*]`, branch coverage on `src`) and `pytest-cov` is a dev
  dependency, but no script wraps it; use `pytest --cov` in the env if needed.
- **Lint and format**: ruff. `hatch run style` runs `ruff check .` with no fixes. `hatch run fix`
  runs pre-commit (ruff with `--fix --exit-non-zero-on-fix`, `ruff-format`, trailing whitespace
  (markdown line breaks kept), end-of-file fixer, `check-yaml`). The pre-commit config excludes
  `.claude/skills/tscircuit/`, `tests/data/` and `.gitlab-ci.yml`.
  - Ruff: `target-version = "py312"`, `line-length = 120`, `show-fixes`, `unsafe-fixes = true`.
  - Rule sets: E, W, F, I, UP, B, C4, DTZ, EM, FA, PIE, PT, RSE, RET, SIM, TID252 (no parent
    relative imports), TC, PLE, PLW, PLC, RUF, N. Ignored: E501 (formatter handles length), E722,
    B007, DTZ005, N806, N803, N815, N816, N999, PLC0206, PLW2901, RUF012.
  - isort: `known-first-party = ["vibepcb"]`, `required-imports = ["from __future__ import annotations"]`.
  - pre-commit pins ruff v0.11.0 (frozen); the dev dependency is unpinned.
- **Types**: `hatch run type` runs mypy on `src` and `tests` (`ignore_missing_imports`,
  `no_implicit_reexport`, `strict_equality`, `warn_return_any`, `warn_unused_ignores`, ...).
  basedpyright is configured for editors only (`typeCheckingMode = "standard"`,
  `venv = "vibepcb"` in `.hatch`); mypy is the real checker.
- **Python conventions** (CLAUDE.md): `from __future__ import annotations` in every file,
  typing-only imports under `if TYPE_CHECKING:`, absolute imports, `X | None` hints, error
  messages assigned to `msg` before `raise` (ruff EM), library code raises and only `cli.py` prints.

---

## 11. External tools and where they are used

| Tool | Used by | Found via | Notes |
|------|---------|-----------|-------|
| `tsci` (tscircuit CLI) | `tscircuit.py`, `hatch run tsci` | `node_modules/.bin/tsci`, run as `npx --no-install tsci` | `_tsci()` raises `TscircuitError` if the binary or `node` is missing |
| node / npm | tscircuit, bun | `nodeenv` (hatch env) or system | `shutil.which("node")` is checked |
| bun | tsci's shebang is `env bun` | npm devDependency | comes from `node_modules/.bin` |
| `kicad-cli` (KiCad >= 9) | `kicad.py` | `$KICAD_CLI`, else PATH | ERC, DRC, refill, fab exports, renders |
| `rsvg-convert` | `cli.cmd_build`, `cli.cmd_fix`, `cli.cmd_check` | PATH, optional | makes the PNG previews in `.work/preview/`; skipped silently if absent |
| ngspice | `boards/<name>/sim/*.cir` | PATH | run manually: `ngspice -b <deck>` |
| git | `update-skill`, `clean` | PATH | `git clean -ffX` deletes ignored files in `.tmp` and `out` |
| network | `docs` (llms.txt), `update-skill` (GitHub clone), `fix` (LCSC symbols, first time per part) | - | `fix` works offline: uncached parts get boxes |

The tscircuit skill is vendored in `.claude/skills/tscircuit/` (`SKILL.md`, `elements/*.md`,
`CLI.md`, `SYNTAX.md`, ...). `SKILL.md` frontmatter: `name`, `description`, `allowed-tools`.
Re-vendor with `hatch run update-skill`. pre-commit does not touch it.

---

### Build flow and timings (smu-main, 238 parts, 4 layers)

| Step | Time | Notes |
|------|------|-------|
| `build` | ~75 s with routing cached; ~7-10 min fresh route | placement gate (`--routing-disabled`, ~1 min) + ONE routed `circuit-json` evaluation; KiCad zip, netlist, SVGs converted from the JSON |
| `fix` | ~1.5-2.5 min | assemble from zip, `FIXES`, then KiCad-verified `restore_nets` |
| `check` | ~1.5-5 min | ERC + DRC in parallel, refill, post-fill loop (up to 4 DRC reruns) |

History: used to be circuit-json export (~10 min) + a second `kicad-library` export that re-routed
(32 min) + `fix` 17 min. Rule: never evaluate the TSX more than once per build.

---

## 12. Gotchas

1. **Project root lookup.** `paths.find_root()` uses `$VIBEPCB_ROOT`, else the nearest parent
   of the cwd that holds `boards/` and `package.json`, else the checkout the code lives in
   (`Path(__file__).resolve().parents[2]`). A non-editable install (the `ciall` env) run from
   outside a project therefore needs `$VIBEPCB_ROOT`.
2. **`hatch run setup` first.** `tscircuit._tsci()` checks for `node_modules/.bin/tsci` and
   `node` on PATH; without them `build` fails with "run `hatch run setup` first".
3. **`fix` deletes `out/<board>/kicad/`.** Any manual edits in KiCad are lost on the next
   `fix` (and `all`). Make hand edits on a copy, or move the board source changes into the TSX.
4. **Do not run registered fixes on a checked board.** `check` refills and saves the KiCad
   files; the registered fixes expect fresh tscircuit output. Re-running `fix` is safe
   because it re-assembles from the raw zip.
5. **`check` rewrites the board** (`--refill-zones --save-board`) and restores its backup
   on a failed post-fill attempt. Its output is the file that `fab` exports.
6. **`all` short-circuits**: a `build` failure stops before `fix`. `check` runs only after
   `fix` returned 0.
7. **Two `sharp_corners` functions.** `fixes.sharp_corners(project)` is the board check
   used by `check`; `tracks.sharp_corners(points)` is the geometric helper it builds on.
8. **Warnings do not fail `check`.** Only ERC/DRC `error` severities and sharp corners
   set exit 1. Read the printed warnings anyway.
9. **`schematic_net_labels` and `place_reference_designators` depend on geometry thresholds**
   (`LABEL_SNAP_MM = 3.0`, text box estimates with `CHAR_ADVANCE = 0.8`). Check the
   result on `.work/preview/schematic.png` / `routed.png` / `board.png` after changes.
10. **Board thickness, reference size and clearances are constants** in `fixes.py`
    (`STANDARD_BOARD_THICKNESS_MM = 1.6`, `REF_TEXT_SIZE_MM = 0.8`, `THERMAL_GAP_MM = 0.3`,
    `FOOTPRINT_INTERNAL_HOLE_CLEARANCE_MM = 0.15`). Change them there, not per board.
11. **Router results jump with tiny placement changes.** Nudge the nearby passive by 0.5 mm
    (or 0.2-0.5 mm) and rebuild; `autorouterEffortLevel` helped on one board. tscircuit's
    routing is deterministic, so compare variants as separate boards (`boards/<copy>`).
12. **Hidden 1 mm max length** on pad-to-pad traces between a capacitor and a chip pin
    (tscircuit's decoupling heuristic). Set `maxLength` on such traces and the cap's GND trace.
13. **Right-angle connectors**: `standard="jst_sh"` merges parts-engine candidates before your
    `supplierPartNumbers`; import the exact part with `tsci import` instead.
14. **Split pads**: a chip with several pads sharing a pin number breaks `schPinArrangement`;
    give the extra pads their own numbers in the import or turn the arrangement off.
15. **The `docs` output is big** (about 900 KB). Grep `docs/tscircuit-llms.txt`, never read it whole.
    It is git-ignored.
16. **`hatch run clean`** removes every git-ignored file under `out` and `.tmp`. It does not
    touch `node_modules/` (ignored, but outside those paths) or `.hatch/`.
17. **Parts engine off for exports.** `tscircuit.export` passes `--disable-parts-engine` (no per-part
    network lookups on each build; all parts carry LCSC numbers). `vibepcb fab` runs the availability check
    once (`tscircuit.availability_warnings`, a placement-only `tsci build --routing-disabled`). Measured on
    smu-front, circuit-json export: 7 s with, 6 s without (network up); the output differs only by the
    availability warnings and `supplier_pin1_location_map`, and was byte-deterministic only without the engine.
18. **Output is LLM-oriented.** `tsci check` strips autorouter chatter lines; `CheckResult`
    prints a summary plus per-violation lines. Keep that format if you extend the CLI.

---

## 13. Porting to another project

Generic, reusable without changes (apart from the names):

- `src/vibepcb/sexpr.py`: lossless S-expression reader/writer with navigation helpers.
- `src/vibepcb/tracks.py`: pure polyline and clearance geometry (no KiCad dependency).
- The fixes contract (`(KicadProject) -> list[str]`, `FIXES` registry, `apply_all`, idempotent tests).
- `kicad.py`: `kicad-cli` wrappers, DRC/ERC merge, JLCPCB file export.
- `pyproject.toml`: the hatch scripts, ruff, mypy, pytest and coverage blocks, the `ciall` and
  `ci` envs, and the `nodeenv`-based `setup`.
- `.pre-commit-config.yaml`.
- The `check` post-fill retry loop (`cli.cmd_check`).

Project-specific: the tscircuit wrapper (`tscircuit.py`), the board template in `cli.py`,
the constants in `fixes.py`, and the `CLAUDE.md` workflow that tells an LLM agent how to run
the loop.
