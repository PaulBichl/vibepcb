# Evaluate the board source once per build

A build evaluates the TSX once (after a routing-free placement gate) and converts the KiCad zip, netlist and
SVGs from that one circuit JSON. Every evaluation re-places and re-routes, which takes 10+ minutes on a big board
and can give a different route, so a second evaluation is both slow and inconsistent.
