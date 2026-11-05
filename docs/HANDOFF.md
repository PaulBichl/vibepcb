# Hand-off (2026-11-05)

State after the tooling-quality session: what was done, what is open, where things are.

## Done (on `main`, not pushed)

| Issue | What | Where |
|---|---|---|
| #1 | Schematic: parts of TSX sub-components get a sub-block per instance ("Channel 1") in the section where they are used; no "other" block | `kicad/schgen.py` (`blocks_of`) |
| #2 | Schematic: no `Net-(...)` labels; two-passive nets as plain wires, others named `D4_A` / `J1_12` / `R5_2` | `kicad/schgen.py` |
| #4 | Schematic: one-pin parts (test points) in a row at the bottom of their block | `kicad/schgen.py` |
| #5 | Silkscreen: `clip_footprint_silk`, reference placement ladder (bigger search, smaller text, Fab layer as last resort), silk counts in `check` | `kicad/fixes/references.py`, `check/history.py` |
| #6 | `pcb place-check`: decoupling / crystal / regulator loop / connector-at-edge rules on the unrouted JSON, findings in `findings.toml` | `check/placement.py` |
| #7 | `[keepout.*]` (track-blocking ones) reach tscircuit's router as `<keepout>` | `build/tscircuit.py`, `kicad/fixes/zones.py` |
| #8 | `pcb place-suggest`: positions + turns for passives next to their pins, as TSX edits with ratsnest delta | `check/suggest.py` |
| #10 | `pcb assembly` (also in `fab`): Draftsman-style assembly PDF + interactive HTML for hand soldering | `output/assembly.py`, `output/assembly_draw.py` |
| - | Reviewer gets the layout: `layout.md` (bottom side first, `WRONG SIDE` flags), `board.png`, `<ref>:bottom-mirroring` items, layout hash (`review_layout_stale`); agent prompt leads with the bottom-side mirroring check | `check/layoutdoc.py`, `check/review.py`, `.claude/agents/pcb-reviewer.md`, `rules/review-checklists.toml` |
| - | Agent-skills setup (issue tracker = GitHub, triage labels, domain docs), `GLOSSARY.md`, `docs/adr/0001-0006` | `docs/agents/`, root |

Tests 511 passed / 1 skipped, ruff + mypy clean at `37d38da`.

## Open

- **#3 schematic sheet use** (hierarchical sheets for large boards, smallest paper for small ones): stopped mid-way.
  WIP commit `c360783` on branch `worktree-agent-a8c54aff99f17258a` (worktree
  `.claude/worktrees/agent-a8c54aff99f17258a`): 8 files, +496/-170, the paged `_hierarchy` writer not written,
  untested. Continue from there or restart from the issue; check ERC / schematic-parity counts and the fab PDF
  (all sheets) before merging.
- **#9 place-suggest cluster moves + cross-layer distance**: one-part moves can't fix bench-psu's bucks (the right
  side of each chip is taken by its own passives); a top cap can be suggested over a bottom pin.
- Smaller follow-ups seen on the way (no issue yet):
  - review package `board.png` shows the top side only; add a bottom render (bench-psu has nearly all parts on
    the bottom);
  - `place-check` false-positive candidates: BGA caps judged against interior balls, long HC49 crystals (pad-centre
    distance), bulk input caps under the 5 mm regulator limit; connector rule has no real-board true positive;
  - assembly drawings: 0603 parts ~2 x 1 mm on the step pages; a few leader lines cross parts; polarised caps
    only from pin names / circuit JSON;
  - tscircuit's LED footprint puts the silkscreen bracket on the anode (pad 1): a fix should move it to the
    cathode;
  - `led-indicator` in this repo keeps its circuit JSON in the old `out/<b>/raw/`, which the CLI no longer reads.

## Board repos (sibling checkouts, use `../VibePCB/src` via PYTHONPATH)

- **bench-psu** (ordered, hand-assembled): `out/bench-psu/fab/bench-psu-assembly.pdf` / `.html` generated.
  `boards/bench-psu/v2-notes.md` (uncommitted) lists v2 changes: buck inductors / input caps on the wrong side of
  the bottom-side (x-mirrored) LMR33630s (SW node ~20 mm), redundant TSX mounting-hole keepouts, LED silkscreen on
  the anode. Before soldering: confirm D2 (ESDA25P35-1U1M) cathode against the datasheet.
- **bench-psu, open-theremin**: stored reviews are stale (new net names from #2, and no layout in the old review);
  `pcb review <board> --run` before `check --strict` / `fab`.
- **open-theremin**: `check` still fails with one error (a 131.7 deg VCC bend), unrelated to this session.

## Housekeeping

- Agent worktrees under `.claude/worktrees/` (all merged except the #3 one): `git worktree list`, remove merged
  ones with `git worktree remove <path>` and `git branch -d worktree-agent-<id>`.
- The shared instructions given to the issue agents (worktree, no new hatch env, boards on private copies without
  rebuilding, report format) are kept in `docs/agents/issue-agent-workflow.md`; hand it to the next issue agents.
- Untracked in the repo root, not ours to commit: `Symbol_and_Footprint_Generation_Standards_Rev2022_for_Students.pdf`,
  `image copy*.png`.
