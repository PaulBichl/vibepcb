from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vibepcb import padtable

if TYPE_CHECKING:
    from pathlib import Path


def test_pad_positions_and_typescript(tmp_path: Path) -> None:
    circuit = tmp_path / "c.json"
    circuit.write_text(
        json.dumps(
            [
                {"type": "source_component", "source_component_id": "sc1", "name": "U2"},
                {"type": "source_port", "source_port_id": "sp1", "source_component_id": "sc1", "name": "DQ0"},
                {"type": "pcb_port", "source_port_id": "sp1", "x": 1.23456, "y": -2.0},
                {"type": "pcb_component", "source_component_id": "sc1", "center": {"x": 1.0, "y": -2.5}},
            ]
        ),
        encoding="utf-8",
    )
    pads, centres = padtable.pad_positions(circuit, ["U2"])
    assert pads == {"U2": {"DQ0": (1.235, -2.0)}}
    assert centres == {"U2": (1.0, -2.5)}
    ts = padtable.to_typescript(pads, centres)
    assert '"DQ0": [1.235, -2.0],' in ts
    assert "U2: [1.0, -2.5]," in ts
    with pytest.raises(ValueError, match="no pads for U9"):
        padtable.pad_positions(circuit, ["U9"])
