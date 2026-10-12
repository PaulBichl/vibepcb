# VibePCB performance plan

Measured on smu-main (238 parts, 586 routes, 4 layers) in VibePCB-smu:
routing pass ~10 min, `kicad-library` export 32 min (it re-routes), `fix` 17 min, `check` (post-fill) many min.
Rules: CLAUDE.md conventions; run `hatch run style`, `type`, `test` after each step; one commit per step.

## Steps

1. **Drop the `kicad-library` export** (`cli.cmd_build`). Write `lib/footprints/tscircuit_builtin.pretty/<name>.kicad_mod`
   from the footprints embedded in the exported `.kicad_pcb`, one per distinct `tscircuit:<name>`.
   Undo the placement: `(at x y a)` -> none; pad/text angles are stored absolute (fp rotation included, see
   `fixes._inside_pad`), so subtract the footprint angle. New helper in `kicad.py`, called from
   `assemble_project`; `fixes.link_footprint_library` stays. Test: round-trip one rotated footprint from
   `tests/data/led-indicator.kicad_pcb`.
   *Why:* the export re-evaluates and re-routes the TSX; it's the single biggest waste.

2. **`--disable-parts-engine` on `tsci export`** (`tscircuit.export`). Add a `vibepcb fab` pre-step that runs the
   availability check once (or prints "availability not checked"). Measure one smu-front build with/without and
   note the numbers in TOOLING.md.
   *Why:* it does per-part network availability lookups on every build; all parts already have LCSC numbers.

3. **Placement gate before routing** (`cmd_build`). `tsci build <tsx> --routing-disabled --disable-parts-engine
   --ignore-errors` writes `dist/.../circuit.json`; fail on `pcb_placement_error`, `pcb_component_outside_board_error`,
   `pcb_courtyard_overlap_error`, `pcb_footprint_overlap_error`, `pcb_pad_pad_clearance_error`
   (reuse `tscircuit.circuit_errors`, filtered by type).
   *Why:* fails in ~1 min instead of after the routing pass.

4. **Profile `fix` and `check` post-fill** (`python -m cProfile -o` on `vibepcb.cli fix smu-main`, read the top
   20 by cumtime). Expected hot spots: O(n^2) loops over all segments in `fixes.clean_tracks`,
   `remove_antennas`, `tracks.redundant` / `tracks.dangling`. Fix with spatial bucketing (there is already
   `tracks._boxes` / `_near`; use it everywhere) and per-net grouping. Output must stay byte-identical on
   smu-front (diff the `.kicad_pcb` before/after).
   *Why:* 17 min of Python for a board KiCad opens in seconds.

5. **ERC and DRC in parallel** (`cli.cmd_check`: run `kicad.run_erc` and the first `run_drc` in a thread pool).
   *Why:* independent kicad-cli processes, ~1-2 min.

6. **`--fast` flag for `build`/`all`**: board prop `autorouterEffortLevel` forced to `"2x"` via a temp copy of
   the TSX in `out/<board>/fast/` (inject on the `<board` tag). Final runs without it.
   *Why:* quicker nudge experiments.

7. **Route cache**: tscircuit can't save routes for smu-main ("PCB port selector is not unique:
   .Q44 > port.pin3", imported `AO3400A`). Find why the selector is ambiguous (check the import's ports/pads),
   fix the import pattern, document in CLAUDE.md "Board design lessons".
   *Why:* unchanged placement then replays routes instead of recomputing them.

8. **Docs**: CLAUDE.md pipeline section + `docs/TOOLING.md` (build flow, timings). Add the rule:
   "never evaluate the TSX more than once per build".

## Verify
- `hatch run pcb all smu-front` (VibePCB-smu) exits 0; fab files unchanged in content.
- `hatch run pcb all smu-main`: one routing pass in the log; record total time per step in TOOLING.md.
