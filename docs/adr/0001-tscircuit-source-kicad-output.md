# tscircuit is the source, KiCad is the output

A board is written in tscircuit (TSX) because an LLM writes code far more reliably than it edits KiCad files, and
KiCad is where the result ends up because its DRC/ERC, zone fill and fab exports are the trusted ones. The KiCad
project is regenerated on every `fix`: hand edits there are lost, so changes go into the board source, and only
systematic export quirks are corrected by scripted fixes.
