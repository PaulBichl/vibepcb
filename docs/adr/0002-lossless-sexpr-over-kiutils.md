# Edit KiCad files through our own lossless S-expression module

Fixes read and write KiCad files through `vibepcb.core.sexpr`, which round-trips the text unchanged, instead of
kiutils or kicad-skip. Both libraries dropped KiCad 9 data or crashed on tscircuit's output.
