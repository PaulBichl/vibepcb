# Altium import probe

One SOIC-8 footprint and one op-amp symbol in KiCad 9 format, to see what Altium 26's KiCad importer keeps before
`pcb part` relies on it (PART-PLAN phase 0). Import once, then note the results in `docs/TOOLING.md`.

Import: File » Import Wizard » KiCad Design Files; add `probe.kicad_sym` and the `probe.pretty` folder.

| Look at | In the probe | Report |
|---------|--------------|--------|
| Layer map | F.Fab (body + "F.Fab" text + `${REFERENCE}`), F.CrtYd (outer rect), User.13 (body rect), User.15 (outer rect, centre cross, PIN1 dot + text, Reference property) | which Altium layer each one lands on (Mechanical 13 / 15 wanted) |
| Line widths | F.Fab / User.13 0.1, F.CrtYd / User.15 outline 0.05, cross 0.07, silk 0.15 mm | kept or reset |
| Pads | rounded rectangle 1.55 x 0.6, ratio 0.25, mask and paste expansion 0 | still rounded rect, expansions 0 (not "from rules") |
| Reference text | on User.15 at the centre | becomes `.Designator` on Mechanical 15? |
| 3D link | `${KIPRJMOD}/PROBE_SOIC8.step` (no file) | ignored / error |
| Symbol fields | Reference visible top left, Value empty + hidden, Footprint / Datasheet / Description hidden, ProbeVisible visible | which become parameters, which visibility survives |
| Symbol pins | 2 mm length, 2 mm pitch, electrical types input / output / power / no-connect | lengths, types, on Altium's grid |
| Symbol graphics | rectangle 0.254 + triangle | kept |
