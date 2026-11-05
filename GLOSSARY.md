# VibePCB

Designing printed circuit boards with an LLM: the circuit is written as tscircuit code, turned into a KiCad
project, corrected and checked until it can go to a fab.

## Language

### Boards and their sources

**Board**:
One PCB design, identified by its name, with its own source folder and output folder.
_Avoid_: project, design, circuit (for the whole thing)

**Board source**:
The tscircuit TSX file that is the single source of truth for a board's circuit and placement.
_Avoid_: schematic, layout file

**Board config**:
The per-board settings the board source cannot express: rule profiles, net classes, planes, keepouts, escapes.
_Avoid_: settings, options

**Intent**:
The designer's written assumptions about what the board must do: supply, rails, voltages, current budget.
_Avoid_: requirements, spec

**Rail**:
A supply net with a nominal voltage, tolerance and current, declared in the intent.
_Avoid_: power net, supply line

**Rule profile**:
A named, reusable set of design or manufacturing limits (a fab's capabilities, an IPC class) that a board
config stacks.
_Avoid_: rule set, DRC settings

**Net class**:
A group of nets, selected by name pattern, that share track width, clearance and diff-pair geometry.

### Pipeline

**Build**:
The stage that evaluates the board source once (placement, then routing) and exports everything from that one
evaluation.
_Avoid_: compile, generate

**Placement gate**:
The routing-free evaluation at the start of a build that stops on placement or overlap errors.

**Raw output**:
The untouched tscircuit export of a build; never edited.

**Fix**:
A scripted, idempotent correction of a systematic tscircuit-to-KiCad export quirk, applied to fresh raw output.
_Avoid_: patch (that word means a change to tscircuit itself), hack, workaround

**Post-fill**:
The corrections that only make sense after KiCad has refilled the pours, run during check and rolled back if
they lose a connection.

**Check**:
The stage that judges a board: ERC, DRC, house rules, circuit verification; its verdict gates fab.
_Avoid_: validate, lint, test

**Vibe mode**:
The default check mode, in which verification issues are warnings.

**Strict mode**:
The check mode in which verification issues are errors; fab requires a strict-clean check.

**Fab output**:
What goes to the manufacturer and the assembler: gerbers, drill, BOM, CPL, renders, bring-up sheet.
_Avoid_: export, deliverables

**Bring-up sheet**:
The power-on checklist for a built board: rails, expected voltages, test points.

### Layout

**Pour**:
A copper zone filling the free area of a layer, tied to GND or a supply net.
_Avoid_: plane (when you mean the copper), fill, flood

**Plane net**:
A net carried by pours instead of routed tracks; every pad on it gets its own via into the pour.

**Antenna**:
A dangling track end or via that connects nothing.
_Avoid_: stub

**Sharp bend**:
A bend between two track segments tighter than 135 degrees.

**Escape**:
The fan-out of a ball grid that VibePCB routes itself, so the autorouter starts from the escape ends instead of
the balls.
_Avoid_: fanout (that is tscircuit's own, unused mechanism), breakout

**Keepout**:
An area where pour, tracks or vias are forbidden, per layer.

**Hotspot**:
A cluster of remaining DRC errors at one place on the board, with the nets and parts that meet there.

**Nudge**:
A small placement move (0.2 to 0.5 mm) of one part to change the router's result near a hotspot.

**Test point**:
A probe pad on a rail, GND or key debug signal, labelled on the silkscreen and kept out of BOM and CPL.

### Verification

**Sim deck**:
A per-board SPICE netlist with behavioural models whose measured results are judged against declared ranges.

**Range**:
The pass interval of one sim measurement, sourced from a datasheet, the intent or the house limits; without a
source it is assumed.

**Finding**:
A recorded verification issue with a stable id, open until fixed or accepted with a reason.
_Avoid_: issue (that is a review answer), warning

**Review**:
The check of every part against its datasheets by a fresh-context reviewer that never sees the designer's
reasoning.
_Avoid_: self-review, audit

**Designer**:
The agent (or person) who writes the board source; it never grades its own board.

**Reviewer**:
The fresh-context agent that performs the review.

### Components

**Part**:
A specific orderable component, identified by its MPN.
_Avoid_: component (in the tooling), device

**Part spec**:
The datasheet facts of one part (package drawing, land, pins), each table citing its datasheet page, from which
symbol, footprint and 3D body are drawn.
_Avoid_: part definition, library entry

**Land pattern**:
The copper pads a part is soldered to: the datasheet's recommended land when it gives one, else calculated.
_Avoid_: footprint (the footprint is the land pattern plus silkscreen, courtyard and fab marks)

**House standard**:
The component standard every generated symbol and footprint follows, each rule tied to a section number.
_Avoid_: library rules, style guide
