# TODO

- **Config wizard** (`vibepcb new`): ask for the board's design rules and stack-up up front and
  write them into the board source - layer count and stack-up (which layer carries which plane:
  GND / supply), fab and its limits (clearance, min track/via, board thickness), net classes
  (power widths, high voltage), edge clearance. Defaults = common practice for the chosen fab.
- **Route cache replay**: tscircuit saves route paths only for boards without custom-symbol
  imports (see CLAUDE.md); wire `<autoroutingphase pcbTracePaths>` so unchanged placements
  skip the routing pass.
- **`check` post-fill speed**: not profiled yet (minutes on a 238-part board).

## BGA support (future topic)

Status (tscircuit core 0.0.2891, autorouter 0.0.982, ECP5 CABGA-256 at 0.8 mm on 6 layers, see
the `vibepcb-logic-analyzer` spikes): boundary fan-out escapes 60/99 connections; the dogbone
escape crashes on any board with a copper pour; replayed dogbones (`pcbTracePaths`) crash later in
TopologyMergingSolver; the plain router ran 43+ min and failed on length matching. Easier entry,
smallest step first:

1. **Minimal repros upstream**: tscircuit's own dogbone example + one plane pour (crash), and the
   replayed-dogbone overlap; file both with the TSX (cheap, unblocks everything below).
2. **Small, coarse BGA without pours**: a 1.0 mm pitch BGA of ~36-100 balls on 4 layers, the
   planes added on the KiCad side instead of in tscircuit (a VibePCB `vibepcb.toml` pour entry that
   `fix` writes as KiCad zones) - sidesteps the pour bug.
3. **VibePCB-side fan-out**: generate dogbones / escape vias deterministically (helper as in
   la-bga-db) and let tscircuit route only from the escape vias outward; `check` verifies.
4. **Real chip, few balls used**: a BGA part where only the outer two rings carry signals (e.g. a
   USB / Ethernet PHY or a small FPGA in a 0.8 mm package) - escapes on the top layer, vias only
   for power.
5. **Then the 256-ball FPGA** with a length-matched memory bus.
