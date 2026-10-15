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
TopologyMergingSolver; the plain router ran 43+ min and failed on length matching.

Plan (learning ladder; each step is a board in `../VibePCB-la` that must pass `check`, stock
checked 2026-10-15):

0. **Baseline**: bump tscircuit (0.0.2891 -> 0.0.2900) and re-run the two crash repros (pour +
   dogbone, replayed dogbones); file them upstream if still broken. Cheap, and it decides whether
   step 2 can use tscircuit's dogbones or needs our own.
1. **First BGA = the SDRAM** (`la-bga54`, copy of la-main): W9825G6KH (TSOP-54) -> IS42S16160J-7BLI
   (C1348766, 699 in stock; same 256 Mbit x16 SDR, TFBGA-54, 0.8 mm). 6 x 9 balls, middle three
   columns empty, so every ball is at most 3 balls from an edge: the whole escape problem in
   small. 4 layers stay. Learn: ball pad size / mask (NSMD), dogbone geometry at 0.8 mm
   (0.3/0.5 mm via in the diagonal gap, check JLCPCB's limits), which balls leave on top vs inner.
2. **VibePCB escape generator** (`vibepcb.fanout`), built on step 1: from the footprint's ball grid
   and the net of each ball, write per-ball escapes that end *outside* the package (ring 0 straight
   out on top, ring 1 out between ring 0, deeper rings dogbone + inner layer, power/GND balls a
   dogbone via to their plane) as `pcbTracePaths` JSON the TSX imports; tscircuit routes only from
   those end points (`routeRemaining`). Deterministic, unit-tested on a synthetic grid; `check`
   gets a BGA rule (every used ball escaped, no via in a ball pad unless the fab does via-in-pad).
3. **ECP5 escape spike** (`la-ecp5-escape`): LFE5U-25F-7BG256I (C1550762, 1277 in stock; 0.8 mm
   CABGA-256) alone with decoupling and headers, 6 layers (sig / GND / sig / split PWR 1.1/2.5/3.3 V /
   GND / sig). Only the outer two rings carry signals; pin plan over all four sides. Goal: the
   generator's escapes + power vias + routing to headers pass `check`.
4. **la-ecp5**: the logic analyzer with ECP5 + BGA SDRAM, length-matched bus, split power plane
   (`check`: a diff pair or bus over a plane split = warning). Bonus of the bigger FPGA: DDR
   inputs -> 200 MSa/s.

Open questions to settle on the way: JLCPCB via-in-pad (POFV) price/availability on 4 vs 6 layers;
smallest via they build without surcharge; whether tscircuit's `<breakout>` helps once the escapes
are ours.

Why the plain-router spike failed (la-bga-nobus picture): the pin plan put 39 of 59 signals (the whole
SDRAM bus) into the three right-most ball columns, so every escape left through one 12 mm edge and
the router stacked its vias there; the SDRAM's pin rows ran parallel to that edge (90 deg turn for
every line). Work around the router, don't rely on it:

- pin plan: buses over two sides, outer two ball rings only (197 IO leave plenty); memory rotated so
  its pin rows face the FPGA;
- VibePCB escape generator: full per-ball escapes ending outside the package (ring 0 top straight
  out, ring 1 dogbone + inner layer between ring-0 vias, ...), passed as `pcbTracePaths`; tscircuit
  routes only from those launch points (its crashes happened with escapes ending inside the BGA);
- one layer per bus (`<bus pcbAllowedLayers>`), so buses don't fight for a layer;
- last resort: route the bus with VibePCB's own maze router (`tracks.grid_route`), `check` judges.
