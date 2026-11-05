# The designer never grades its own board

The datasheet review runs in a separate agent with a fresh context that sees only the design's outputs (parts,
netlist, intent, sim decks), never the board source, the findings or the design conversation. An agent reviewing
its own reasoning tends to agree with it; `fab` requires a review of the current netlist.
