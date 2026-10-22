# Reviewing a circuit with a fresh agent

`check` verifies what a script can decide (simulations, rating / pull-up / decoupling / divider / floating-pin
rules, see TOOLING.md 8.3). Everything that needs a datasheet is the reviewer's job. The reviewer is a separate
agent with a fresh context, so it cannot just agree with the designer, and it sees only the design's outputs.

## Workflow

```
designer: pcb all <board>           build, fix, check (vibe mode: review_missing is a warning)
          pcb review <board> --run  package -> new reviewer session -> answers merged
          fix the board / accept findings with a reason, repeat
          pcb check <board> --strict  needs a review of the current netlist, no open findings
          pcb fab <board>
```

1. `hatch run pcb review <board>` writes the package to `out/<board>/.work/review/`:

   | File | Content |
   |------|---------|
   | `PROMPT.md` | the reviewer's task: inputs, answer rules, what it must not read |
   | `parts.md` | every part: checklist type, value, MPN / LCSC, each pin's net |
   | `netlist.txt` | tscircuit's readable netlist |
   | `intent.toml`, `sim/*.cir` | the assumption list and the sim decks, if any |
   | `checks.md` | what the deterministic checks already cover |
   | `review.toml` | the answer template: one `[[item]]` per checklist item, question as a comment |

   The board source, `findings.toml` and the design conversation are not in it: the designer's reasoning must
   not steer the reviewer.
2. The reviewer, in a new session, fills `review.toml` from datasheets it looks up itself (by MPN / LCSC):
   - `--run` does it in one step: `$VIBEPCB_REVIEWER` (default `claude -p --agent pcb-reviewer`, the agent in
     `.claude/agents/pcb-reviewer.md`, tools Read / Grep / Glob / WebSearch / WebFetch / Write / Edit), then
     merges. Another agent CLI: set `VIBEPCB_REVIEWER` to its non-interactive command; the prompt is appended
     as the last argument.
   - In a Claude Code session, the designer can instead spawn the `pcb-reviewer` subagent (fresh context, never a
     fork) with the prompt `pcb review` prints, then run `pcb review <board> --merge`.
3. Merging validates the answers (every item answered once, `ok` / `issue` cite a source, `issue` / `unknown`
   say why, pins exist on the part) and refuses the file otherwise. `issue` and `unknown` answers become open
   findings `review:<item id>` in `boards/<board>/findings.toml`; the full answers and the netlist hash go to
   `boards/<board>/review.toml` (commit both).
4. Only disagreements reach the human: open findings. The designer fixes the TSX and re-runs the review, or sets
   a finding to `status = "accepted"` with a `reason`. A review finding keeps its status / reason across
   re-reviews as long as the reviewer reports the same item; a fixed one disappears.
5. A change to the netlist (parts, values, part numbers, connections; not the layout) makes the review stale:
   `check` reports `review_stale`, and a strict check (so `fab`) needs a new review.

## Answer format

```toml
[[item]]
# U1 (regulator): LDO: dropout at the peak load leaves headroom ...
id = "U1:dropout"
verdict = "issue"             # ok | issue | unknown
pin = "VIN"                   # optional: a pin of the part, as in parts.md
cite = "TLV75533 datasheet p.5, dropout 230 mV at 500 mA"
text = "USB minimum 4.75 V - 3.3 V leaves 1.45 V: fine; but the 600 mA budget exceeds the 500 mA rating. Use TLV75733."
```

An item that does not apply (an inductor item for an LDO): `verdict = "ok"`, `cite = "n/a: <why>"`.

## Checklists

`rules/review-checklists.toml`, one table per part type, one question per key; `<project>/rules/` overrides the
tool's file. Part types (`review.classify`): the tscircuit element type first (pin header -> connector, diode /
LED, crystal), then the reference letters (imported parts are all chips: J/P/CN -> connector, D -> diode,
Q -> transistor, Y -> crystal, R/C/L/F/SW/TP -> board items), then chips: regulator (VIN + VOUT/FB/SW pins or a
known family), MCU (4+ GPIO-like pin names), other IC. Resistors, capacitors, inductors, fuses, switches and test
points are covered by the `[board]` items (ratings, power, current path, intent, sims).
