from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibepcb import tscircuit

if TYPE_CHECKING:
    from pathlib import Path


def test_repair_trace_nets(tmp_path: Path) -> None:
    def port(i: int, key: str) -> list[dict[str, object]]:
        return [
            {"type": "source_port", "source_port_id": f"sp{i}", "subcircuit_connectivity_map_key": key},
            {"type": "pcb_port", "pcb_port_id": f"pp{i}", "source_port_id": f"sp{i}"},
        ]

    def route(tid: str, trace: str, a: str, b: str | None) -> dict[str, object]:
        return {
            "type": "pcb_trace",
            "pcb_trace_id": tid,
            "source_trace_id": trace,
            "route": [{"x": 0, "y": 0, "start_pcb_port_id": a}, {"x": 1, "y": 0, "end_pcb_port_id": b}],
        }

    elements = [
        *port(0, "netA"),
        *port(1, "netA"),
        *port(2, "netB"),
        {
            "type": "source_trace",
            "source_trace_id": "stA",
            "subcircuit_connectivity_map_key": "netA",
            "display_name": "A",
        },
        {
            "type": "source_trace",
            "source_trace_id": "stB",
            "subcircuit_connectivity_map_key": "netB",
            "display_name": "B",
        },
        route("wrong", "stB", "pp0", "pp1"),  # A route tagged with net B
        route("right", "stA", "pp0", "pp1"),
        route("short", "stB", "pp0", "pp2"),  # ends on two nets: left for KiCad DRC
        {"type": "pcb_via", "pcb_trace_id": "wrong", "subcircuit_connectivity_map_key": "netB"},
    ]
    path = tmp_path / "board.circuit.json"
    path.write_text(json.dumps(elements), encoding="utf-8")
    assert tscircuit.repair_trace_nets(path) == ["wrong: B -> A"]
    fixed = {e["pcb_trace_id"]: e["source_trace_id"] for e in json.loads(path.read_text()) if e["type"] == "pcb_trace"}
    assert fixed == {"wrong": "stA", "right": "stA", "short": "stB"}
    via = next(e for e in json.loads(path.read_text()) if e["type"] == "pcb_via")
    assert via["subcircuit_connectivity_map_key"] == "netA"
    assert tscircuit.repair_trace_nets(path) == []


def test_routed_errors_block_only_source_and_routing_failures(tmp_path: Path) -> None:
    path = tmp_path / "board.circuit.json"
    path.write_text(
        json.dumps(
            [
                {"type": "pcb_pad_pad_clearance_error", "message": "Via and pad U1.1 are too close"},
                {"type": "pcb_trace_error", "message": "too close"},
                {"type": "source_failed_to_create_component_error", "message": "bad props"},
            ]
        ),
        encoding="utf-8",
    )
    errors, report = tscircuit.routed_errors(path)
    assert errors == 1
    assert "source_failed_to_create_component_error" in report
    assert "1 x pcb_pad_pad_clearance_error" in report


def test_autorouting_crash() -> None:
    log = (
        'Generating circuit JSON...\n\x1b[0m\x1b[31mAsync effect error in PcbTraceRender "autorouting":\n'
        'Error: Subcircuit connectivity map key "net4" matches multiple SRJ connections\n'
        "    at getDifferentialPairSrjConnectionNamesByCohortOrThrow (index.js:1:1)\n"
    )
    assert (
        tscircuit.autorouting_crash(log)
        == 'Error: Subcircuit connectivity map key "net4" matches multiple SRJ connections'
    )
    assert tscircuit.autorouting_crash("Circuit JSON written to x.json") is None


def test_prune_empty_stops_at_content_and_at_dist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "out" / "b" / ".work" / "src").mkdir(parents=True)
    (dist / "out" / "keep").mkdir()
    (dist / "out" / "keep" / "f").write_text("x")
    tscircuit._prune_empty(dist / "out" / "b" / ".work" / "src", dist)
    assert sorted(p.name for p in dist.rglob("*")) == ["f", "keep", "out"]
    (dist / "out" / "keep" / "f").unlink()
    tscircuit._prune_empty(dist / "out" / "keep", dist)
    assert not dist.exists()
    assert tmp_path.exists()
