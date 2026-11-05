# Generate the schematic from the netlist, not from tscircuit

The KiCad schematic is drawn by `schgen.py` from the circuit JSON with KiCad / LCSC symbols, one block per board
source section; tscircuit's schematic is always disabled and `schX` / `schY` are ignored. tscircuit's schematic
layout does not finish on a few hundred parts, while BOM, ERC and the schematic PDF need a schematic; a generated
one is checked against the netlist by construction.
