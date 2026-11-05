# Escape ball grids ourselves instead of using tscircuit's router or fanout

BGA balls are escaped by VibePCB (`[escape.<ref>]`), and tscircuit's router starts from the escape ends.
Letting the autorouter into a ball field tangled it (199 acute angles on a 54-ball SDRAM), and tscircuit's own
fanout crashed with pours (tscircuit/core#4660) and with saved paths (#4663).
