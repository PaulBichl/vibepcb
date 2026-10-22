# Reviewing a circuit with a fresh agent

`check` verifies what a script can decide (simulations, rating / pull-up / decoupling / divider / floating-pin
rules, see TOOLING.md 8.3). Everything that needs a datasheet is the reviewer's job. The reviewer is a separate
agent with a fresh context, so it cannot just agree with the designer.

## Workflow

1. Designer agent: builds the board, writes `intent.toml` (the assumption list), runs `pcb all <board>`.
2. Reviewer agent (new session, no design conversation). It gets only:
   - `out/<board>/.work/raw/<board>.netlist.txt` and `out/<board>/fab/<board>-bom.csv` (or the BOM from `pcb fab`)
   - `boards/<board>/intent.toml`, `boards/<board>/findings.toml`, `boards/<board>/sim/*.cir`
   - the datasheets of the parts (PDF excerpts, fetched by the reviewer itself, not quoted by the designer)
3. The reviewer derives each limit from the datasheet on its own, then compares with the board. For every
   checklist item below it adds one entry to `findings.toml` with `source = "review"`, citing reference and pin,
   `status = "open"` when the board disagrees. Passing items are not written.
4. Only disagreements reach the human: open findings. The designer fixes the TSX (finding disappears on the next
   `check` only for deterministic ones; a review finding is set to `accepted` with a reason, or deleted when fixed).
5. Before `fab`: `pcb check <board> --strict` must pass (no open findings, no failing / assumed sims).

Entry format (see `src/vibepcb/findings.py`):

```toml
[[finding]]
id = "review:U1:en-threshold"
ref = "U1"
pin = "EN"
text = "EN high threshold is 1.2 V (datasheet p.6); it is tied to VIN (5 V) through R3, fine, but absolute max is 6 V"
status = "open"
reason = ""
source = "review"
```

## Checklists per part type

Cite the datasheet page for each value you derive.

### Voltage regulator (LDO, buck, boost)

- Input range includes the supply (intent `[supply]`), also at the worst case (USB 5.25 V, battery full).
- Dropout at the peak load leaves headroom over the output (LDO).
- Output capacitor: value, ESR and type allowed by the stability section (MLCC vs. tantalum), DC-bias derating.
- Input capacitor as recommended; placement next to VIN.
- Feedback divider gives the intended voltage with the datasheet reference (and tolerance); adjustable parts only.
- EN / PG / SS pins handled as the datasheet requires (EN not floating, threshold vs. the driving rail).
- Inductor (switchers): saturation current above the peak current, value from the datasheet formula.
- Thermal: dissipation (Vin - Vout) * I against the package; exposed pad connected and soldered to copper.
- Current budget in intent is below the part's limit with margin.

### MCU / processor

- Every VDD / VDDA / VCAP / VBAT pin has its decoupling as in the hardware design guide; VDDA filtering.
- Reset: NRST pull-up and cap per datasheet; boot pins at the level for the intended boot mode.
- Clock: crystal load capacitors from the crystal's CL and the pin capacitance, not from a rule of thumb.
- Programming / debug header on the right pins (SWD / JTAG), pull-ups where required.
- Pin functions used (alternate function, 5 V tolerance, ADC channel) exist on the chosen pins.
- Total I/O current within the package limit; no pin driven above VDD + the allowed margin.
- Exposed pad / ground pins connected.

### Connector / interface

- Pinout and pin 1 against the connector datasheet and the mating cable / host (USB, headers, FFC direction).
- Power pins: the current the connector pins and contacts carry; polarity protection where a user plugs it in.
- ESD / TVS on pins that leave the board; series resistors where the interface standard asks.
- Signal levels match on both sides (3V3 vs 5 V); level shifters where they do not.
- USB: CC pull-downs (5.1 k), shield handling, D+/D- order; I2C: pull-up value for bus speed and capacitance.
- Silkscreen labels every pin (the `check` warns when missing).

### Passives and protection

- Capacitor voltage rating vs. the net after derating (the script covers parts with a known rating; check the
  rest against the BOM part numbers) and DC-bias capacitance loss for MLCCs.
- Resistor power for dividers / current-sense / LED resistors, LED current vs. the part's limit.
- Fuse / PTC hold current above the load, trip below the source's limit.
- Diodes: reverse voltage, forward current, orientation.

## Simulations

For each deck check that the `.measure` lines prove what the deck claims and that the behavioural model uses
datasheet values (dropout, gain-bandwidth, ...) with the source in a comment. A range without `source=` is
assumed: find the datasheet value or write down why it is the limit.
