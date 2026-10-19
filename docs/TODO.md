# TODO

- **`doNotPlace` ignored** (found on the Open Theremin): `schgen` marks every part fitted, so unfitted pads (ISP header,
  antenna / touch pads) land in the BOM without LCSC numbers. Carry tscircuit's `doNotPlace` into the schematic
  symbols (`(in_bom no)` / DNP) and leave them out of `fab`'s BOM and CPL.
- ~~**Design / manufacturing rule profiles**~~ done: `rules/*.toml` (jlcpcb-2l/4l/6l, ipc-class2/3, hv-60v),
  `[rules]` in `vibepcb.toml`, `src/vibepcb/rules.py`, fix `apply_rule_profiles`, `pcb rules <board>`
  (TOOLING.md sections 3 and 5.2). Still open from the plan: stack-up (dielectric + er) and `rf`
  impedance profiles for `stackup.py`, the build deriving tscircuit's board props from the profile,
  `fab` stamping the profile names into the fab notes, `pcb new` asking for profiles.
- **Route cache replay**: tscircuit saves route paths only for boards without custom-symbol
  imports (see CLAUDE.md); wire `<autoroutingphase pcbTracePaths>` so unchanged placements
  skip the routing pass.
- **`check` post-fill speed**: not profiled yet (minutes on a 238-part board).

## Agent-directed routing (next big topic)

Plan for implementation (phases, formats, acceptance tests): [ROUTING-PLAN.md](ROUTING-PLAN.md).

## BGA support (future topic)

Status (tscircuit core 0.0.2891, autorouter 0.0.982, ECP5 CABGA-256 at 0.8 mm on 6 layers, see
the `vibepcb-logic-analyzer` spikes): boundary fan-out escapes 60/99 connections; the dogbone
escape crashes on any board with a copper pour; replayed dogbones (`pcbTracePaths`) crash later in
TopologyMergingSolver; the plain router ran 43+ min and failed on length matching.

Plan (learning ladder; each step is a board in `../VibePCB-la` that must pass `check`, stock
checked 2026-10-15). Priorities: learning, then usability; fab price doesn't matter.

Terms: *escape* = the short route from a ball out of the ball field; *dogbone* = the usual escape
for a ball that can't leave on the top layer: a short stub from the ball pad to a via in the gap
between four balls, then out on an inner layer.

Layers (decided): 4 for the BGA-SDRAM step (54 balls, all near the edge); 6 for the ECP5
(sig / GND / sig / split PWR 1.1/2.5/3.3 V / GND / sig): its inner balls are mostly power/GND and
need planes right below. Dogbones, no via-in-pad: 0.8 mm pitch has room for them and it is the
standard way at this pitch (via-in-pad is the answer for 0.5 mm and below).
Vias: mechanically drilled through-hole vias only (no laser microvias / blind / buried - a later
topic). At 0.8 mm pitch a dogbone via of 0.2 mm drill / 0.4 mm pad fits the gap between four balls.

0. **Baseline**: bump tscircuit (0.0.2891 -> 0.0.2900), re-run the two dogbone crash repros; file
   them upstream if still broken.
1. **First BGA = the SDRAM** (`la-bga54`, copy of la-main): W9825G6KH (TSOP-54) -> IS42S16160J-7BLI
   (C1348766, 699 in stock; same 256 Mbit x16 SDR, TFBGA-54, 0.8 mm). 6 x 9 balls, middle three
   columns empty. Learn: ball pad / mask, dogbones at 0.8 mm, which balls leave on which layer.
2. **New tool - VibePCB escape generator** (`vibepcb.fanout`): VibePCB, not the autorouter, routes
   every ball out of the package (ring 0 straight out on top, ring 1 between ring 0, deeper rings
   dogbone + inner layer, power/GND balls dogbone to their plane) and hands tscircuit the end
   points (`pcbTracePaths`); tscircuit routes only the open board. New `check` rule: every used
   ball escaped, no via in a ball pad.
3. **New tool features, tested on the ECP5** (`la-ecp5-escape`, LFE5U-25F-7BG256I C1550762, 1277 in
   stock, alone with decoupling + headers): split power plane (several supply zones on one layer
   from `vibepcb.toml`, tscircuit can't), 6-layer stack-up from the config, power balls to the
   right plane region, pin plan over four sides.
3b. **Via-in-pad option** (after the ECP5 escape works with dogbones): `[escape.<ref>] via_in_pad = true` puts
   the via in the ball pad (filled + capped, still mechanically drilled); `fab` output must call for filled vias,
   `plane_pad_vias` accepts it for those parts only. Compare the same ECP5 board both ways (routing room,
   results). Needed later for 0.65 / 0.5 mm pitch.
4. **la-ecp5**: mostly project implementation (the logic analyzer with ECP5 + BGA SDRAM). New
   on the tool side only: escape lengths counted in the length groups, and a `check` warning for a
   bus or diff pair crossing a plane split.

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
