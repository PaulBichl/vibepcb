# Feedback: missing functionality and portability

Ideas from a read-through of the repo (source and docs only, nothing run). Sorted by design phase; the
portability notes at the end are what came up while evaluating a Windows / WSL + Copilot setup.

## 1. Before the circuit

The agent goes from a prompt straight to TSX. Nothing records what the board is supposed to do, so nothing
later can be checked against it.

- **Intent, written by the agent, not the human.** From the prompt ("USB-powered sensor board with an
  ESP32") the agent derives the implied requirements: 5 V in, 3V3 rail, ~500 mA budget, startup peak, ...
  It writes them as a short assumption list (about ten lines). The human only confirms or corrects
  ("no, battery powered").
- **Component selection.** Parametric part search, availability / lifecycle, alternates. Plus a check that
  the footprint and pinout match the datasheet (pin 1, pin functions, exposed pad).

## 2. Verifying the circuit

### Simulation

Today: per-board ngspice decks in `boards/<name>/sim/*.cir`, run by hand. They are hard to enforce across
projects and can silently drift from the board.

- **Part of `check`.** Each deck states what it proves with `.measure` lines and pass ranges. `check` runs
  ngspice in batch mode and reports values outside their range.
- **Deck values must match the board.** `check` compares the R/C/L values in a deck with the BOM values of
  the same references. If R5 changed on the board but not in the deck, the sim no longer counts as passed.
  This is the main enforcement gap today.
- **Required sims per circuit type** via a shared rule profile, e.g. every regulator needs a power-up and a
  load-step deck. Shared profiles give the same policy in every project.

What to simulate: only behaviour over time or frequency, where a calculation or a netlist rule isn't enough.

| Area | Examples |
|---|---|
| Power | startup / inrush, load step, rail sequencing, undervoltage lockout |
| Analog | filter response, amplifier gain / bandwidth / stability, ADC input settling |
| Timing | RC reset delay, debounce |
| Protection | TVS clamping, reverse polarity, current limit |

Not worth simulating: digital logic, pull-ups, LED series resistors (a rule check covers those).

Where the pass ranges come from (the agent must not set its own pass mark):

1. **Datasheets of the parts receiving the signal**: MCU VDD range, ADC input range, capacitor voltage
   ratings. Automatic, no human.
2. **House defaults per circuit type**, shipped with the tool like `rules/*.toml`: overshoot, ripple, phase
   margin, derating. Written once.
3. **The agent's assumption list** from section 1.
4. **Anything without a source** gets a conservative default, marked "assumed" in the `check` summary.

Weak spot: chips have no SPICE models, so the agent writes behavioural ones. Their parameters (dropout,
gain-bandwidth, ...) should cite the datasheet value, the same as the ranges.

### Review by an agent

- **Separate reviewer with a fresh context.** It sees only the outputs (netlist, BOM, datasheet excerpts,
  assumption list), not the design conversation, so it can't just agree with itself. It derives limits from
  the datasheets independently; only disagreements with the designer agent reach the human.
- **Deterministic netlist checks first.** With voltage-named nets (`+3V3`) a script can check, without an
  agent:
  - part voltage ratings against the net voltage
  - one pull-up per I2C bus
  - every power pin decoupled
  - feedback divider values against the named output voltage
  - EN / RESET / BOOT pins not floating
- **The agent covers only what scripts can't decide**: datasheet-specific requirements, from a checklist
  per part type (regulator, MCU, connector).
- **Findings file**: one line per checklist item, citing reference and pin. Open findings fail `check`
  (strict mode) until marked accepted.

### Two modes

- **Vibe (default):** failed sims, assumed ranges and open findings are warnings; the iteration loop isn't
  blocked.
- **Strict (before `fab`):** they fail `check`.

Human effort per board: one assumption list at the start, a handful of flagged items at the end.

## 3. Layout

- **Placement help.** Placement is hand-typed coordinates plus `place-score`. Missing: an optimizer and
  constraints like "decoupling cap close to its pin", "connector on the board edge".
- **Signal / power integrity.** Impedance, return paths, high-current and thermal design: mostly missing as
  rules and as analysis.

## 4. Iteration

No human-readable change log is needed during the agent's loop. One cheap aid for the agent itself:

- **Regression table between runs**: DRC errors, unrouted connections, ratsnest length, sharp corners,
  compared with the previous run. Anything that got worse is flagged, so the agent doesn't oscillate between
  two variants.

## 5. After the layout (one person + one agent)

- **Bring-up sheet** generated from the design: rails and expected voltages, test points, expected current
  at first power-on.
- **Firmware header** generated from the MCU pin assignments.

## 6. Portability (Windows / WSL, Copilot)

On WSL2 the repo runs essentially as-is. Native Windows needs these fixes:

| Where | Problem |
|---|---|
| [tscircuit.py](../src/vibepcb/tscircuit.py) `_tsci` | `subprocess.run(["npx", ...])`: Windows needs `npx.cmd` (`shutil.which`) |
| [tscircuit.py](../src/vibepcb/tscircuit.py) `prepared_source` | absolute import paths written with backslashes into TS strings (`\b`, `\v` escapes); use `.as_posix()` |
| [routes.py](../src/vibepcb/routes.py) `_search_round` | `multiprocessing` `fork` context does not exist on Windows |
| all `subprocess.run(..., text=True)` | decoded with the locale code page; pass `encoding="utf-8", errors="replace"` |
| [symlib.py](../src/vibepcb/symlib.py) `LIBRARY_DIRS` | no Windows paths; derive from the `kicad-cli` location |
| [cli.py](../src/vibepcb/cli.py) `_png` | needs `rsvg-convert`; previews silently missing, `inspect` fails |
| `pyproject.toml` `update-skill` | `rm -rf` |

Also:

- `fix` calls `kicad-cli` unconditionally (connection safety net), but the docs say KiCad is only needed
  for `check` and `fab`.
- The docs say "KiCad >= 9", but several fixes were tuned on KiCad 10 (comments in `fixes.py`,
  `tracks.py`). State the supported versions and check `kicad-cli version` at startup.
- The local patches to `node_modules/tscircuit` are lost on `npm install`; version them
  (`patch-package` / `pnpm patch`).
- A `pcb doctor` command (node, bun, tsci, kicad-cli path + version, symbol library, rsvg-convert, network)
  would catch most setup problems up front.
- Agent files: `AGENTS.md` is read by Copilot, Codex and others; `CLAUDE.md` can point to it. Long builds
  (10+ min) outlast synchronous agent terminals, so tell the agent to run them in the background.
