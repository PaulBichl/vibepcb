# TODO

- **Config wizard** (`vibepcb new`): ask for the board's design rules and stack-up up front and
  write them into the board source - layer count and stack-up (which layer carries which plane:
  GND / supply), fab and its limits (clearance, min track/via, board thickness), net classes
  (power widths, high voltage), edge clearance. Defaults = common practice for the chosen fab.
- **Route cache replay**: tscircuit saves route paths only for boards without custom-symbol
  imports (see CLAUDE.md); wire `<autoroutingphase pcbTracePaths>` so unchanged placements
  skip the routing pass.
- **`check` post-fill speed**: not profiled yet (minutes on a 238-part board).
