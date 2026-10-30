# Parts quick start (second device)

Generate a component to the house standard (`rules/parts/altium-students-2022.md`) and bring it into Altium.
Details: `AGENTS.md` (Components), `docs/TOOLING.md` section 14.

## 1. Set up (once)

```sh
git clone <repo> VibePCB && cd VibePCB      # or: git pull
pip install hatch                          # if missing
hatch run pcb doctor                       # what is missing; parts need KiCad 9/10 (kicad-cli) only
hatch env create parts                     # optional: CadQuery for the 3D bodies (large download)
```

Optional for `fetch`: `DIGIKEY_CLIENT_ID` / `DIGIKEY_CLIENT_SECRET` or `MOUSER_API_KEY` in the environment.
Without them pass `--datasheet <url|file>`.

## 2. Build the two example parts

The specs are in the repo (`parts/ATS060.toml`, `parts/TPS62133RGTR.toml`):

```sh
hatch run parts:pcb part all ATS060          # without the parts env: hatch run pcb part all ATS060 (no STEP)
hatch run parts:pcb part all TPS62133RGTR
hatch run parts:pcb part export ATS060 --altium out/parts/altium
hatch run parts:pcb part export TPS62133RGTR --altium out/parts/altium
```

`check.txt` in `out/parts/<MPN>/` must say `result: PASS`. Previews: `symbol.png`, `footprint.png` there.

## 3. Into Altium (native route, the one to test)

For each part, from `out/parts/altium/`:

1. Set `ScriptDir` at the top of `<MPN>.pas` to this folder, with a trailing backslash
   (e.g. `'C:\parts\altium\'`), so the script finds the `.step`.
2. Open (focus) a PcbLib, **File » Run Script**, pick `<MPN>.pas`, run **CreateFootprint**.
3. Open (focus) a SchLib, run **CreateSymbol**.

Check against the standard:

- footprint: pads (rounded rectangle, mask / paste expansion manual 0), Top Overlay outline + pin-1 dot,
  Mechanical 13 body + pin-1 circle + 3D body, Mechanical 15 placement outline, cross with circle,
  `.Designator` (TrueType Arial), PIN1 pad on pad 1;
- symbol: blue body, black pins on 2 mm, designator `U?` / `Y?` top left in Arial 8, comment empty and hidden,
  no parameters, footprint linked.

The script is written from Altium's scripting reference and was not run on Altium yet: when it stops with an error,
note the message and the line, and send it back; the fix goes into `src/vibepcb/parts/altium_script.py`, not
into the generated `.pas`.

## 4. Fallback: KiCad import route

File » Import Wizard » KiCad Design Files, add `<MPN>.kicad_sym` and the `vibepcb_parts.pretty` folder; steps in
`IMPORT-<MPN>.md`. Known limits: the PIN1 copy is only an outline + text, pin colours come from the importer, the
3D model is only a link. The probe in `scripts/altium/probe/` (README there) shows what the importer keeps.

## 5. A new part

```sh
hatch run pcb part fetch <MPN> --datasheet <url>   # skeleton parts/<MPN>.toml + datasheet in the cache
# fill in [package], [land] (when the datasheet has a land pattern) and [[pin]] from the datasheet
hatch run parts:pcb part all <MPN>
```
