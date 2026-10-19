# Routing plan: agent-directed ("manual") routing

Status (2026-10-19): phases 1-6 in, used on three BGA boards that pass `check` (la-bga54m, la-ecp5m,
la-ecp5: ECP5 + BGA SDRAM, 6 layers, coupled USB pair). Phase 3 in (`pcb inspect`, `pcb ratsnest`;
nets with a pour are skipped like `[planes]` nets), phase 4 (bus) is done differently, phase 8 partly
(`pcb place-score`; pin assignment by geometry lives in the board source with `pcb padtable`), phase 7
(fallback autorouter) open.
What changed from the plan, in short:
- `board.py` wraps `fixes._copper` (no refactor of `fixes`); `drc.py` adds rip-up (`ignore`) and pair aliases.
- Router: A* over (x, y, layer) with the heading beside the state (not in it), coarse 0.4 / 0.2 mm search
  first, the fine 0.1 mm search in a tube around it, unguided fine search last; vias; pad exit reservations
  (cost in front of other nets' pads); `avoid_own` (routes of a net meet at pads, not on each other).
- Buses: no lane geometry. `order = "angle"` routes the outermost arcs first (largest angle off the direction to
  the goals, seen from the starts) so the inner ones nest; a pin plan made the same way in the board source
  (sweep around both parts, `pcb padtable` for the pad positions) keeps them planar. Sequential + rip-up
  (time-boxed) beat negotiated congestion (PathFinder, still `method = "negotiate"`), which wandered.
- Length tuning (`tune.py`): 45-degree bumps on the longest straight pieces, to 90 % of `max_skew` or `tune_to`.
- Pairs (`pairs.py`, `[[pair]]`): wide centre line split into P / N; refused when P / N swap sides between the
  ends (swap pins at a symmetric part instead). No stack-up impedance yet.
- Experiments: `[router]` settings per board, `pcb clone` for variants run in parallel.
- Joint rules at escape ends / pads (`joints`, `pad_joints`) exist but are off: they blocked routes.
Original text: Implement phase by phase; each phase ends with its
acceptance test green and `hatch run test` / `style` / `type` passing. Keep this file up to date as
phases land (what was done, what changed from the plan).

## Goal

The agent decides where tracks go; VibePCB turns that intent into DRC-clean copper on the KiCad
board, deterministically, on every build. tscircuit keeps the circuit, the netlist and the
placement, but no longer routes (except as an optional fallback, see phase 7).

Why: tscircuit's router is the weak spot on every non-trivial board (BGA tangles, plane layers cut
by signals, 0.4 mm escape ends knotted, KiCad-invisible gaps that `fix` has to repair). The BGA
escape (`src/vibepcb/fanout.py`) already proves the pattern: plan in code, check clearances in
code, draw in KiCad, KiCad DRC decides.

Non-goals: an interactive GUI; replacing KiCad's DRC (it stays the final authority); a global
autorouter of our own (phase 7 uses an existing one).

## Architecture

```
boards/<b>/index.circuit.tsx   circuit, nets, placement            (agent)
boards/<b>/vibepcb.toml        rules: net classes, length groups,   (agent)
                               diff pairs, planes, escapes, [routing]
boards/<b>/routes.toml         routing intent, per connection/bus   (agent)
        │ build: tscircuit evaluates once with routing disabled -> circuit JSON -> KiCad zip
        ▼
out/<b>/kicad/<b>.kicad_pcb    placed, unrouted board
        │ fix: existing fixes that need no tracks, then vibepcb.routes.apply()
        │      (escapes, then routes.toml in file order, then fallback router for the rest)
        ▼
        │ check: KiCad DRC/ERC + house rules (unchanged)
```

`[routing] mode = "manual"` in `vibepcb.toml` switches a board to this flow; boards without it
keep today's tscircuit-routed flow unchanged (SMU, la-main must keep passing).

### Modules (new unless noted)

| Module | Responsibility |
|--------|----------------|
| `vibepcb/board.py` | Board model for routing, built from the parsed `.kicad_pcb`: pads (with net, layers, shape), tracks, vias, zones, keepouts, outline, net classes (from `.kicad_pro`), stack-up (from `vibepcb.toml`). Move `fixes._Copper` / `_copper` here (keep a re-export in `fixes` until callers move). Spatial index for obstacles (reuse `tracks.BoxIndex`). |
| `vibepcb/drc.py` | Incremental clearance checks against the model: `fits_track(net, layer, a, b, width)`, `fits_via(net, centre, size, drill)`, edge / keepout / hole-to-hole, all from the net-class rules. Returns the first blocking item (for error messages). Same numbers KiCad uses (`kicad_pro` + `.kicad_dru`). |
| `vibepcb/router.py` | Single-connection router: pad/via/point -> pad/via/point on given layers, 45-degree A* on a fine grid (generalise `tracks.grid_route`: multi-layer state `(x, y, layer, heading)`, via transitions with a cost, waypoints, corridor box, cost terms for length / bends / vias / layer preference). Commits geometry into the model (so later routes see it). Failure returns a `RouteFailure` with the reason and the blocking items near the best partial path. |
| `vibepcb/routes.py` | Parser + validator for `routes.toml` (schema below), `apply(project, spec) -> report`; deterministic order; content-derived UUIDs (as `_segment_node`); writes segments/vias with the right `(net <id>)` form (`fixes._net_node`). |
| `vibepcb/bus.py` | Bus / river routing: N nets routed side by side keeping order and pitch (phase 4). |
| `vibepcb/tune.py` | Length tuning: meanders (phase 5). |
| `vibepcb/pairs.py` | Coupled differential pairs (phase 6). |
| `vibepcb/stackup.py` | Impedance -> width/gap for a stack-up (phase 6). |
| `vibepcb/inspect.py` | Feedback for the agent: crops, net highlight, reports (phase 3). |
| `vibepcb/fallback.py` | Autorouter for whatever the spec leaves unrouted (phase 7). |
| `cli.py` (existing) | New commands: `pcb route`, `pcb inspect`, `pcb ratsnest` (below). |

Coordinates everywhere in KiCad board millimetres (y down), so route files can be checked
against KiCad directly. Pad references are `REF.PAD` (KiCad pad number, e.g. `U1.76`) or
`REF.pinlabel` resolved through the circuit JSON (`U2.DQ15`).

## `routes.toml` (intent format)

One entry per connection or group; the router fills in the geometry. Order in the file = routing
order (critical first). Everything optional except the endpoints.

```toml
[defaults]
layers = ["F.Cu", "B.Cu"]      # signal layers the router may use (planes never)
via = "default"                # net class via unless overridden

[[route]]                      # one connection (pad to pad)
net = "SD_CLK"                 # or: from = "U1.F2", to = "U2.CLK"
layers = ["F.Cu"]              # restrict
via_max = 0
waypoints = [[112.0, 98.5]]    # pass through (board mm)
corridor = [108, 95, 125, 104] # x0 y0 x1 y1: stay inside
width = 0.15                   # else the net class width

[[route]]
net = "VSET_*"                 # wildcard: every connection of matching nets, in name order

[[bus]]                        # phase 4
name = "SDRAM_DQ"
nets = ["SD_DQ0", "SD_DQ1", "SD_DQ2"]   # order across the lane = this order
from = "U1"                    # part the bus leaves
to = "U2"
layer = "F.Cu"
pitch = 0.3                    # centre to centre
waypoints = [[110, 100], [118, 100]]    # lane centre line

[[pair]]                       # phase 6
nets = ["USB_P", "USB_N"]
class = "USB"                  # width / gap from the net class (or stackup-derived)
from = "U3"
to = "J2"

[[tune]]                       # phase 5
group = "SDRAM_DQ"             # a [length_group] from vibepcb.toml
target = "longest"             # or a length in mm
```

Validation errors name the entry and line (`routes.toml:23: net "SD_CLKX" not on the board`).

## CLI

| Command | Does |
|---------|------|
| `pcb route <b> [--only NET] [--dry-run]` | Run `routes.apply` on the current KiCad project (no rebuild); report per entry: routed / failed (reason, blocker), length, vias. `--dry-run` prints without saving. Fast loop for the agent (seconds). |
| `pcb inspect <b> --net NET` / `--area x0 y0 x1 y1` / `--layer L` | PNG crop (one layer or all, net highlighted, others dimmed) into `.work/preview/inspect-*.png` + a text summary (phase 3). |
| `pcb ratsnest <b>` | Unrouted connections (pad pairs), sorted by length, with a crossing count per region (phase 3; also the placement metric of phase 8). |
| `pcb all <b>` | Unchanged; in manual mode `fix` applies the routes. |

## Phases

### Phase 1 - board model + incremental DRC (no routing yet)

1. `board.py`: move `_Copper`/`_copper` out of `fixes.py` (pure refactor; all fixes keep working).
   Add net-class lookup (`.kicad_pro` `net_settings` + `apply_board_config` patterns) so each net
   knows its width / clearance / via.
2. `drc.py`: `fits_track`, `fits_via`, `blocking(...)`. Clearance = max of both items' net-class
   clearances (KiCad rule), edge clearance from the board setup, hole-to-hole from
   `fixes._min_hole_to_hole`. Via connection rule as in `tracks.via_takes` (centre on the track or
   a track end inside the via - copper overlap is NOT a connection).
3. Tests: fixture board (`tests/data/`), compare `drc` verdicts with a kicad-cli DRC run on a
   handful of deliberately bad tracks (one test marked `kicad` that is skipped without kicad-cli).

Accept: `drc` agrees with KiCad DRC on the fixture's clearance / edge / hole cases; all existing
tests pass; la-main and smu-main still pass `check`.

### Phase 2 - single-connection router + `routes.toml` + `pcb route`

1. `router.py`: A* over `(ix, iy, layer, heading)`; grid step 0.05 mm (as `REROUTE_STEP_MM`),
   moves of 45 degrees, turn at most 45 degrees per node (keeps every bend >= 135 deg), via move
   = layer change at a node where `fits_via`; costs: length 1.0/mm, bend 0.2, via 2.0, off
   preferred layer 0.5/mm (tunable constants at module top). Start / end: from the pad centre,
   first piece straight out of the pad (reuse `fixes._straighten_pad_entries` logic). Heuristic:
   octile distance. Bound the search by `corridor` (default: bbox of endpoints + 5 mm, growing to
   +15 mm on failure) and `max_states`.
2. Multi-pad nets: route as a chain/tree: nearest-neighbour order (Prim on pad centres), each new
   pad routed to the nearest point of the already-routed copper of the net (T-junction at 90 deg,
   as `square_tees` wants).
3. `routes.py`: parse, validate, apply in order; skip nets of `[planes]` (they connect through
   their pours + `plane_pad_vias`) unless an entry names them explicitly.
4. Pipeline: `[routing] mode = "manual"` -> `build` runs the routed evaluation with
   `routingDisabled` on the board (prepared copy, like `schematicDisabled`; verify the KiCad zip
   from an unrouted circuit JSON still has the pours - if not, add the pours in `fix` from a
   `[pour.<net>] layers = [...]` config section). `fix` runs the track-free fixes, then
   `routes.apply`, then the clean-up fixes (they also tidy hand routes).
5. `pcb route` command.
6. Tests: router on synthetic boards (two pads with an obstacle between, a forced via, a corridor
   that makes it fail with the right reason, a 3-pad net forming a T); `routes.toml` parser
   errors; determinism (same input twice -> identical file).

Accept: a copy of la-main (`boards/la-main-manual`) with `mode = "manual"` and a `routes.toml`
holding one wildcard entry per section passes `check`, OR the remaining failures are listed with
reasons the agent can act on. Route time for la-main under 5 min.

### Phase 3 - feedback for the agent (`pcb inspect`, `pcb ratsnest`)

1. Crops: `kicad-cli pcb export svg --layers <L>,Edge.Cuts` for the whole board once, then crop
   with a viewBox rewrite (no re-export per crop) and `rsvg-convert` to PNG at a fixed px/mm
   (e.g. 60 px/mm: a 0.1 mm track is 6 px). Net highlight: copy of the board with every other
   net's copper recoloured (edit the SVG: KiCad SVGs group by layer, not net - so instead draw
   the highlighted net's tracks/pads ourselves as an overlay `<g>` from the board model).
2. Text summary per net: length per layer, via count, connected yes/no, nearest clearance.
3. (done) `pcb ratsnest`: unrouted pad pairs (from the model's connectivity, `tracks.pad_groups`),
   lengths, and a crossings count (segment intersections of the straight ratsnest lines) overall
   and per 10 mm cell.
4. Tests: crop maths (viewBox), ratsnest on a fixture.

Accept: the agent can read a crop of the la-bga54e BGA area and tell escapes apart (manual check
once, then just keep the command working).

### Phase 4 - bus routing

1. `bus.py`: route N nets as parallel lanes: the lane centre line from `waypoints` (or a single
   connection routed with width = N * pitch as a probe), then offset polylines at `pitch`,
   keeping order; at each end a short fan-in to the pads (in pad order, which phase 8 makes
   match the lane order). Corners: offset curves on 45-degree corners (inner lanes shorter -
   phase 5 tunes).
2. Fall back to per-net routing for a member that can't keep its lane (report it).
3. Tests: 8-net bus around one 90-degree corner; order preserved; pitch kept.

Accept: the SDRAM bus of la-bga54e (39 nets) routed as buses (DQ, address/control) passes
`check` with the existing length groups.

### Phase 5 - length tuning (meanders)

1. `tune.py`: for each net of a length group shorter than the target, insert a trombone /
   accordion meander on its longest straight piece: amplitude <= 3 * width + clearance,
   spacing >= 2 * clearance (KiCad's own tuning-pattern defaults are a good reference), 45-degree
   or rounded corners, never through another net's clearance (`drc`).
2. Target: `longest` member, or a given length; tolerance from the group's `max_skew`.
3. Tests: one short straight net tuned to a target within 0.1 mm; meander clear of a nearby net.

Accept: a group with `max_skew = 2.0` (tighter than la's 45 mm) passes KiCad's skew rule.

### Phase 6 - coupled differential pairs + stack-up

1. `stackup.py`: microstrip / stripline closed-form impedance (IPC-2141 / Wadell formulas) for
   single-ended and edge-coupled differential; inputs from `vibepcb.toml`:
   `[stackup] layers = [{name="F.Cu", copper=0.035}, {dielectric=0.2104, er=4.05}, ...]`
   (JLCPCB's published stack-ups as presets: `preset = "jlc04161h-7628"`); solve width/gap for a
   target (`[netclass.USB] impedance_diff = 90`). Document the expected error vs a field solver
   (a few percent) and that the fab's calculator is the final word.
2. `pairs.py`: route the pair's centre line with the single router (width = 2w + gap +
   clearances), then offset the two tracks at +-(w + gap)/2; uncoupled sections only at the ends
   (pad fan-in), counted against `[diff_pair] max_uncoupled`; skew between P/N fixed with a small
   meander on the shorter (phase 5).
3. Tests: impedance numbers against a few published reference values; pair keeps its gap around
   a corner.

Accept: la-main's USB pair routed by `pairs.py` passes the existing diff-pair rules; the stack-up
numbers for JLCPCB's 4-layer preset are within 5 % of their online calculator (record the values
used in the test).

### Phase 7 - fallback autorouter for the rest

Any autorouter is fine (user's call: "idc"). Order of preference:

1. **Freerouting** (github.com/freerouting/freerouting, Java, CLI mode): export the board after
   the manual routes as Specctra DSN, route the remaining nets headless, import the SES. DSN
   export / SES import: KiCad's `pcbnew` Python module has `ExportSpecctraDSN` / `ImportSpecctraSES`
   (run with KiCad's bundled Python; check the KiCad 10 API names first - kicad-cli has no DSN
   export as of 10.0). Fixed manual routes must be marked locked/fixed in the DSN so Freerouting
   keeps them.
2. tscircuit's router on the remaining connections (today's flow), if DSN round-tripping is
   unworkable.

Wrap it in `fallback.py`: `route_remaining(project, nets=None) -> report`; then the usual clean-up
fixes and `check`. Install: document the Java + Freerouting jar requirement in `docs/TOOLING.md`
(`hatch run setup` could download the jar, pinned version + checksum).

Accept: la-main-manual with only the SDRAM bus and USB pair in `routes.toml`, the rest by the
fallback, passes `check`.

### Phase 8 - placement helpers (routability)

1. Pin assignment: for FPGA-like parts with swappable IO (`pin_groups` in `vibepcb.toml`: which
   balls/pins are interchangeable), assign nets to pins by angle as seen from the partner part
   (the geometric pairing done by hand for la-main / la-bga54), written back as a generated TSX
   constant file the board imports (`boards/<b>/pinmap.ts`), so the netlist stays the source.
2. (done) Ratsnest crossing count (phase 3) as the placement score; `pcb place-score <b>` compares
   board copies.
3. Orientation hints: for each 2-terminal part, the rotation that minimises its ratsnest length.

Accept: la-bga54's hand-made `SDRAM_PINS` pairing reproduced (or beaten on crossings) by the tool.

### Later (not planned in detail)

- Split / region planes with a feed and a "signal crosses a plane split" check (BGA TODO step 4).
- Via-in-pad option for escapes (BGA TODO step 3b).
- Push-and-shove (moving existing routes aside) - only if phases 2-7 show it's needed.

## Open points to verify first (cheap spikes before the phase that needs them)

- Phase 2: does tscircuit's unrouted evaluation still emit `pcb_copper_pour` elements and does
  the KiCad zip from it carry the zones? (If not: pours from config in `fix`.)
- Phase 2: grid step 0.05 mm on a 100 x 70 mm board, 2 layers: A* state count per connection -
  measure; if too slow, a coarse 0.2 mm global pass + fine local pass near pads.
- Phase 7: KiCad 10 `pcbnew` Python DSN/SES API names; Freerouting CLI flags for headless runs
  and for keeping locked tracks.

## Conventions (same as the rest of the repo)

- Python conventions from `CLAUDE.md` (future annotations, `msg` before `raise`, library code
  raises, only `cli.py` prints). Edit KiCad files only through `vibepcb.sexpr`.
- Deterministic output (content-derived UUIDs; sorted iteration).
- Every new fix / pipeline step: a row in `docs/TOOLING.md` section 5.2, tests in `tests/`.
- Regression boards after each phase: la-main, smu-main, smu-front (tscircuit-routed flow must
  keep passing), plus the phase's own acceptance board.
