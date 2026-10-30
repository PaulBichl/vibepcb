# PART-PLAN: components from a part number

Plan for `pcb part <MPN>`: from a manufacturer part number to a finished component (schematic symbol, footprint,
rough 3D model) that follows the house component standard and imports into Altium Designer 26. Written for the
agent that implements it; phases are in order, each ends with tests that must pass. Follow `AGENTS.md` (Python
conventions, `vibepcb.sexpr` for all KiCad files, library code raises / only `cli.py` prints) and run
`hatch run style`, `hatch run type`, `hatch run test` after every phase; one commit per phase.

## Goal and split of work

```
MPN ─▶ DigiKey / Mouser ─▶ datasheet PDF ─▶ agent reads it ─▶ parts/<mpn>.toml ─▶ generator + standard ─▶ files ─▶ checks
          (fetch.py)          (cached)        (by hand, once)    (source of truth)     (deterministic)
```

- **The agent reads, the code draws.** The datasheet is read by the coding agent and turned into a part spec
  (`parts/<mpn>.toml`). Everything after that is plain, tested Python: the same spec + standard always gives the
  same files. No geometry is ever written by the LLM directly.
- **One spec, three outputs.** Symbol, footprint and 3D model are built from the same numbers, so they can't drift.
- **Sources:** DigiKey and Mouser APIs only (they point to the manufacturer's datasheet). LCSC / EasyEDA are not
  used as a source; `easyeda.py` may serve as an optional second opinion in the checks (phase 6), never as input.
- **3D:** a rough clearance model is enough (body box at the right height). Fully automatic, no manual CAD.
- **Altium:** files are written in **KiCad 9 format** (Altium 26's KiCad importer reads up to KiCad 9; KiCad 10
  files import but lose property visibility). Import via Altium's Import Wizard, then a small Altium script.

Out of scope for now: multi-unit symbols beyond what the standard asks for, BGA ball maps over ~100 balls,
connectors with mechanical tabs drawn from a 3D source, Altium-native file output.

## Inputs the human provides

| Input | Where | Notes |
|-------|-------|-------|
| Component standard | `rules/parts/<standard>.md` | the house document as markdown, kept verbatim |
| DigiKey API credentials | env `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` | Product Information API v4, OAuth2 client credentials (2-legged) |
| Mouser API key | env `MOUSER_API_KEY` | Search API, 30 calls/min, 1000/day |

Secrets never go into the repo, config files or caches. `pcb doctor` reports which are set (not their values).

## Files and layout

```
rules/parts/<standard>.md          the standard (human-written, verbatim)
rules/parts/<standard>.toml        the standard as machine rules (phase 2, agent-written, human-reviewed)
parts/<mpn>.toml                   part spec (phase 3, agent-written from the datasheet; committed)
src/vibepcb/parts/                 new package
    fetch.py        DigiKey / Mouser lookup, datasheet download, cache
    spec.py         part spec dataclasses, TOML load + validation
    standard.py     standard rules: load, apply defaults
    symbol.py       spec + standard -> (kicad_symbol_lib ...)
    footprint.py    spec + standard -> (footprint ...); package families + explicit land pattern
    ipc7351.py      land pattern calculation when the datasheet gives none
    model3d.py      spec -> STEP (CadQuery), optional dependency
    check.py        checks on the generated files (phase 6)
out/parts/<mpn>/                   generated, gitignored
    <mpn>.kicad_sym
    <mpn>.pretty/<mpn>.kicad_mod
    <mpn>.step
    symbol.svg, footprint.svg      previews for the human
    check.txt                      check summary (same style as a board's check.txt)
~/.cache/vibepcb/parts/<mpn>/      API responses (JSON) + datasheet PDF
```

CLI (in `cli.py`, same pattern as the other `cmd_*`):

| Command | Does |
|---------|------|
| `pcb part fetch <MPN>` | API lookup + datasheet download; prints manufacturer, package, lifecycle, datasheet path, a spec skeleton |
| `pcb part build <MPN>` | spec -> symbol, footprint, STEP, previews |
| `pcb part check <MPN>` | checks (phase 6); exit 1 on errors |
| `pcb part all <MPN>` | build + check |
| `pcb part export <MPN> --altium <dir>` | copies the import set (sym, `.pretty`, step, import notes) into a folder for Altium |

## Dependencies

The project is stdlib-only (`dependencies = []`, KiCad files via `vibepcb.sexpr`). Keep it that way for
everything except 3D:

- HTTP: `urllib.request` (as `easyeda.py` does). OAuth2 client credentials is one POST; no SDK.
- TOML: `tomllib`.
- PDF reading is done by the agent (it reads the PDF), not by the package. For the agent's convenience,
  `pcb part fetch` may also write `datasheet.txt` via `pdftotext` when it is on PATH (optional, report in doctor).
- **CadQuery** as an optional extra: `[project.optional-dependencies] parts = ["cadquery"]` plus a hatch env or
  feature. Without it, `build` skips the STEP with a one-line note; nothing else depends on it.

## Phase 1: fetch

`parts/fetch.py`:

- `lookup(mpn) -> PartInfo`: DigiKey first, Mouser if DigiKey has no exact match or no credentials.
  `PartInfo`: manufacturer, MPN as listed, description, package / case string, datasheet URL, lifecycle status
  (active / NRND / obsolete), distributor part numbers, source (`digikey` / `mouser`).
- DigiKey: OAuth2 token (client credentials, cache the token until expiry), then the v4 product details call by
  part number. Check the exact endpoint paths and response fields against DigiKey's v4 Swagger spec before coding;
  write the field mapping in one place.
- Mouser: Search API, search by part number, take the exact-match result; datasheet URL field.
- Exact match only: if several manufacturers list the MPN, stop and list them (the human picks with
  `--manufacturer`).
- Download the datasheet into the cache (follow redirects; some manufacturer sites block plain user agents: set a
  normal one, and on failure print the URL for the human to download manually into the cache path).
- Lifecycle not active -> warning in the output and later in `check.txt`.

Tests: recorded API responses (JSON fixtures, no network) for both distributors -> `PartInfo`; ambiguous match;
missing credentials -> Mouser fallback; no network -> cached result used; token expiry.

## Phase 2: the standard as rules

The standard is a prose document. The agent converts it once into `rules/parts/<standard>.toml`; the human reviews
that file. Every rule carries the section of the document it comes from, so a reviewer can trace it.

Sketch (the real keys follow whatever the document actually specifies; do not invent rules it doesn't state):

```toml
[meta]
name = "house-2026"
source = "rules/parts/house-2026.md"

[symbol]
grid = 2.54                      # mm, pin grid                          (§3.1)
pin_length = 2.54                #                                       (§3.1)
text_size = 1.27                 #                                       (§3.2)
body_line_width = 0.254          #                                       (§3.2)
pin_order = "function"           # function | number                     (§3.3)
power_pins = "top"               # supply pins top, ground bottom        (§3.3)
fields = ["Manufacturer", "MPN", "Datasheet", "Description"]   #         (§3.5)
reference_prefix = { ic = "U", resistor = "R", capacitor = "C", connector = "J" }

[footprint]
naming = "{family}{pins}P{pitch}_{body_l}x{body_w}"  # name pattern       (§4.1)
silkscreen_width = 0.15          #                                       (§4.3)
silk_pad_clearance = 0.2         #                                       (§4.3)
courtyard_margin = 0.25          # mm, IPC nominal density               (§4.4)
courtyard_grid = 0.05            #                                       (§4.4)
pin1_mark = "dot"                # dot | chamfer | line                  (§4.5)
fab_layer = true                 # body outline on F.Fab                 (§4.6)
origin = "centroid"              #                                       (§4.2)
ipc_density = "N"                # L | N | M, when the datasheet has no land pattern

[model3d]
color = "dark_grey"

[unexpressed]                    # rules the code can't check: listed in check.txt as manual items
items = [
  "§5.2 thermal relief for exposed pads follows the board stack-up (reviewer decides)",
]
```

`standard.py` loads it into dataclasses with defaults, rejects unknown keys (typos in a rule file must fail loudly).

Tests: load the sample standard; unknown key -> error; missing section -> defaults; a rule without source -> error.

## Phase 3: part spec from the datasheet (agent workflow)

`pcb part fetch` writes a skeleton `parts/<mpn>.toml` with the phase 1 data filled in. The agent then reads the
datasheet and fills in pins and package. Instructions for this go into `AGENTS.md` (section "Components"):

- Every number gets the datasheet page it came from (`page = 12`), so the human can check it.
- Dimensions as min / nom / max as the drawing gives them; the generator picks what it needs.
- Prefer the **recommended land pattern** from the datasheet. Only when there is none, leave `[land]` out:
  the generator calculates one (IPC-7351B, phase 5).
- Pin names exactly as the datasheet's pin table; electrical type from the pin description
  (input / output / bidirectional / power_in / power_out / passive / no_connect).
- Exposed pad: its own pad with the pin number the datasheet gives it (often the next number, or the GND pin's).
- When the datasheet covers several packages, use only the one matching the MPN's package code; name it.

Example (`parts/TPS62133RGTR.toml`, numbers illustrative):

```toml
[part]
mpn = "TPS62133RGTR"
manufacturer = "Texas Instruments"
description = "3-17V 3A step-down converter"
datasheet = "https://www.ti.com/lit/ds/symlink/tps62133.pdf"
source = "digikey"
lifecycle = "active"
kind = "ic"

[package]
family = "QFN"                     # chip | SOT | SOIC | TSSOP | QFN | DFN | custom
pins = 16
pitch = 0.5
body = { l = [2.9, 3.0, 3.1], w = [2.9, 3.0, 3.1], h_max = 1.0 }
lead = { l = [0.3, 0.4, 0.5], w = [0.18, 0.25, 0.30] }
exposed_pad = { l = 1.7, w = 1.7, pin = "17" }
page = 34

[land]                             # from the datasheet's recommended land pattern; omit to calculate
pad = { l = 0.85, w = 0.28 }
span = 3.85                        # pad centre to pad centre across the package
exposed_pad = { l = 1.7, w = 1.7, paste_windows = 4 }
page = 36

[[pin]]
number = "1"
name = "SW"
type = "power_out"
page = 3
# ... one [[pin]] per pin, 16 + EP
```

`spec.py`: dataclasses + validation: pin numbers unique and complete for the package (1..pins + EP), every pin has
a type, min <= nom <= max, land fits the family, pages present. Validation errors name the field and pin.

Tests: valid spec loads; each validation error with a minimal broken spec; the skeleton writer round-trips.

## Phase 4: symbol

`parts/symbol.py`: spec + standard -> one `(symbol ...)` in a `(kicad_symbol_lib (version 20241209) ...)`
(KiCad 9 format; check the version number against what KiCad 9's own library files use and make it one constant).

- Box symbol, pins on the grid, placement by the standard (`pin_order`, power top / ground bottom, inputs left,
  outputs right by default). Reuse what `schgen.py` already knows about pin grouping where it fits; don't fork it.
- Box size from pin count and the longest pin name (text widths as `schgen` measures them).
- Fields: Reference, Value (= MPN), Footprint (`<library>:<footprint name>`), Datasheet, plus the standard's
  fields; hidden as the standard says. Property visibility is the thing Altium loses on KiCad 10 files, so write
  it explicitly per property.
- Passive two-pin parts (R, C, L, D): standard symbols, not boxes, if the standard asks for them.

Tests: a 16-pin spec -> pin count, all pins on the grid, no overlapping pins, box encloses all pin names, power
pins on top, fields present with the right visibility; `kicad-cli sym export svg` exits 0 (skip if no kicad-cli).

## Phase 5: footprint

`parts/footprint.py`: spec + standard -> `(footprint ...)` (KiCad 9 format, same version-constant rule).

Two paths:

1. **Explicit land pattern** (`[land]` present): pads exactly as given, positioned by the family's geometry
   (span, pitch, rows / sides).
2. **Calculated** (`[land]` absent): `ipc7351.py`, IPC-7351B toe / heel / side fillets for the chosen density from
   the lead and body tolerances. Families first: chip (0201-2512), SOT-23-3/5/6, SOT-223, SOIC, TSSOP / MSOP,
   QFN / DFN with exposed pad. `custom` has no calculation: the spec must give `[land]` with an explicit pad list
   (`[[land.pad]] number, x, y, l, w, shape, rotation`).

Then, for both paths, from the standard:

- name from the naming pattern; origin at the centroid; pad 1 position by IPC zero orientation;
- F.Fab body outline + pin-1 mark; F.SilkS outline kept `silk_pad_clearance` away from pads (clip, don't move);
- F.CrtYd: body / pad extents + `courtyard_margin`, rounded to `courtyard_grid`;
- exposed pad paste: split into windows when the spec asks (`paste_windows`), ~50-80 % coverage;
- reference / value text on F.SilkS / F.Fab as the standard says;
- 3D model link: `${KIPRJMOD}`-relative path to `<mpn>.step` (Altium ignores it; KiCad uses it).

Tests per family against a hand-checked reference (pad centres and sizes to 0.01 mm); IPC calculation against
published example values for at least SOIC-8 and 0603; silkscreen never overlaps a pad (with clearance);
courtyard encloses everything; `kicad-cli fp export svg` exits 0.

## Phase 6: checks

`parts/check.py`, `pcb part check`: writes `out/parts/<mpn>/check.txt` in the same style as a board check (errors,
warnings, manual items), meant to be read by the agent and the human.

Errors:
- kicad-cli fails to load or export either file;
- symbol pin numbers != footprint pad numbers (both directions; EP included);
- any standard rule violated (each rule names its section);
- spec validation failure.

Warnings:
- lifecycle not active;
- land pattern calculated (no datasheet land pattern): "check against datasheet page N";
- pad deviates > 0.1 mm from the distributor CAD footprint, if one is available (optional second opinion; skip
  silently when none; EasyEDA allowed here only as a comparison, never as input);
- body height missing -> 3D model uses a conservative default.

Manual items: the `[unexpressed]` rules of the standard; "compare footprint.svg with datasheet page N".

Spec review: the agent that wrote `parts/<mpn>.toml` must not grade it (same rule as `pcb review` for boards).
`pcb part check --review` runs a fresh-context reviewer (`.claude/agents/pcb-reviewer.md` pattern, `docs/REVIEW.md`)
that re-reads the datasheet pages named in the spec and compares every pin and dimension; disagreements land as
errors in `check.txt` for the human to settle.

Previews: `symbol.svg`, `footprint.svg` (kicad-cli), rasterized to PNG for the agent's own look (as boards do).

Tests: each error and warning triggered by a minimal fixture; clean part -> exit 0.

## Phase 7: 3D model

`parts/model3d.py` (CadQuery, optional):

- Body: box `body.l x body.w x body.h_max` (max dimensions: conservative for clearance), standing off the board by
  the spec's stand-off (default 0 for QFN / DFN, 0.1 mm for gull-wing).
- Leads: simple boxes for gull-wing / J-lead families, flat pads under the body for QFN / DFN; skipped for `custom`.
- Pin-1 mark: a shallow dimple or a coloured top face patch.
- Origin and orientation match the footprint (same centroid, pin 1 at the same corner), z = 0 at the board top.
- Output STEP AP214 into `out/parts/<mpn>/`.
- `custom` parts (connectors, inductors, odd shapes): one box over the maximum outline from the drawing. Rough and
  conservative is the point.

Tests: STEP written and re-readable by CadQuery; bounding box equals max body dims (+ leads); pin-1 corner matches
footprint pad 1; skipped cleanly when CadQuery is missing.

## Phase 8: Altium hand-off

`pcb part export <MPN> --altium <dir>` writes the import set and an `IMPORT.md` with the steps:

1. Altium: File » Import Wizard » KiCad Design Files; add `<mpn>.kicad_sym` and the `<mpn>.pretty` folder as
   library files (`.kicad_mod` imports even though the wizard's list doesn't advertise it).
2. Run `scripts/altium/fix_imported_part.pas` (DelphiScript, new in this phase) on the imported libraries:
   - restore line widths the importer drops (silkscreen, fab, courtyard) from the values in a sidecar JSON
     written by `export` (`<mpn>.altium.json`: layer -> width, field visibility, model path);
   - attach `<mpn>.step` as the 3D body of the footprint;
   - set field visibility from the sidecar (belt and braces, even with KiCad 9 files);
   - link symbol -> footprint.
3. Optional: merge into the house library (IntLib / DbLib) - left to the human.

The DelphiScript can't be tested in CI; test the sidecar JSON instead, and record a manual test of the whole
import once (one QFN, one SOIC, one chip resistor) in `docs/TOOLING.md` with the Altium version used.

Before writing the script, verify on one part what the importer actually keeps (line widths, field visibility,
3D link) and only fix what is really lost; note the findings in `docs/TOOLING.md`.

## Phase 9: docs and doctor

- `AGENTS.md`: section "Components" (phase 3 workflow, how to read a datasheet into a spec, when to stop and ask).
- `docs/TOOLING.md`: commands, files, check table, Altium findings.
- `pcb doctor`: DigiKey / Mouser credentials set, CadQuery available, pdftotext available, KiCad version (warn when
  only KiCad 10 is installed: writing KiCad 9 format still works, but `kicad-cli` validation then reads a newer
  version than Altium will - fine, note it).
- README: one line under "What you get".

## Acceptance

Three real parts end to end, each passing `pcb part check` with no errors and reviewed against the datasheet by
the human, then imported into Altium 26 with correct pads, symbol fields hidden as intended, and a 3D body:

1. a QFN with exposed pad and datasheet land pattern (e.g. a TI buck converter);
2. a SOIC-8 without land pattern in the datasheet (IPC calculation path);
3. a 0603 resistor (chip family, standard passive symbol).

## Open questions (ask the human before the phase that needs it)

- Phase 2: anything in the standard that conflicts with IPC-7351 defaults - which wins?
- Phase 4: one symbol per MPN, or shared generic symbols (e.g. all 0603 resistors)?
- Phase 5: is the footprint name per MPN or per package (shared footprints across parts)?
- Phase 8: target is a file-based SchLib / PcbLib, an IntLib, or an Altium 365 / DbLib library?
