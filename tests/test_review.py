from __future__ import annotations

import json
import sys
import tomllib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from test_verify import c, chip, circuit, r

from vibepcb import cli, findings, netcheck, paths, review, rules, verify

if TYPE_CHECKING:
    from pathlib import Path

PartSpec = tuple[str, str, dict[str, Any], list[tuple[str, str | None]]]
GPIO: list[tuple[str, str | None]] = [(f"PA{i}", None) for i in range(6)]
PARTS: list[PartSpec] = [
    chip("U1", [("VIN", "+5V"), ("VOUT", "+3V3"), ("EN", "+5V"), ("GND", "GND")]),
    chip("U2", [("VDD", "+3V3"), ("GND", "GND"), *GPIO], manufacturer_part_number="STM32G031F6P6"),
    chip("U3", [("VDD", "+3V3"), ("GND", "GND"), ("SDA", "SDA")]),
    ("J1", "simple_pin_header", {}, [("pin1", "+5V"), ("pin2", "GND")]),
    ("D1", "simple_led", {"supplier_part_numbers": {"jlcpcb": ["C2286"]}}, [("anode", "LED"), ("cathode", "GND")]),
    ("Y1", "simple_crystal", {}, [("pin1", "PA0"), ("pin2", "PA1")]),
    r("R1", 330, "+3V3", "LED"),
    c("C1", 1e-5, "+5V", "GND"),
    chip("Q1", [("G", "LED"), ("S", "GND"), ("D1", None), ("D2", None), ("D3", None), ("D4", None)]),
    chip("J2", [("VBUS", "+5V"), ("GND", "GND"), ("CC1", None)]),
]


def design_of(parts: list[PartSpec]) -> tuple[netcheck.Design, list[dict[str, Any]]]:
    data = circuit(parts)
    return netcheck.load_design(data), data


def test_shipped_checklists() -> None:
    lists = review.load_checklists()
    assert {"regulator", "mcu", "ic", "connector", "diode", "transistor", "crystal", "board"} <= set(lists)
    assert all(lists[kind] for kind in lists)
    assert "review-checklists" not in rules.available()


def test_classify() -> None:
    design, data = design_of(PARTS)
    assert review.classify(design, data) == {
        "U1": "regulator",
        "U2": "mcu",
        "U3": "ic",
        "J1": "connector",
        "D1": "diode",
        "Y1": "crystal",
        "Q1": "transistor",  # an imported chip with D1..D4 pins, not an MCU: the reference letters decide
        "J2": "connector",
    }  # R1 / C1: board items


def test_items() -> None:
    lists = {"regulator": {"a": "A?", "b": "B?"}, "diode": {"x": "X?"}, "board": {"z": "Z?"}}
    got = review.items({"U1": "regulator", "D1": "diode", "R1": "resistor"}, lists)
    assert [i.id for i in got] == ["D1:x", "U1:a", "U1:b", "board:z"]
    assert got[-1].ref == ""


def test_netlist_hash_follows_connectivity_not_order() -> None:
    a, _ = design_of(PARTS)
    b, _ = design_of(list(reversed(PARTS)))
    assert review.netlist_hash(a) == review.netlist_hash(b)
    changed, _ = design_of([*PARTS[:-1], c("C1", 2.2e-5, "+5V", "GND")])
    assert review.netlist_hash(changed) != review.netlist_hash(a)


def answer_all(template: str, verdicts: dict[str, dict[str, str]]) -> str:
    """Fill the template: every item ok with a cite, except the ids in ``verdicts``."""
    out = []
    current = ""
    for line in template.splitlines():
        if line.startswith("id = "):
            current = json.loads(line[5:])
        fields = verdicts.get(current, {"verdict": "ok", "cite": "datasheet p.1"})
        for key in ("verdict", "pin", "cite", "text"):
            if line == f'{key} = ""' and key in fields:
                line = f"{key} = {json.dumps(fields[key])}"
        out.append(line)
    return "\n".join(out)


def test_validate() -> None:
    design, data = design_of(PARTS)
    work = review.items(review.classify(design, data), {"regulator": {"dropout": "?"}, "board": {"sims": "?"}})
    answers = [
        review.Answer("U1:dropout", "issue", pin="VBUS", cite="p.5"),
        review.Answer("board:sims", "maybe"),
        review.Answer("board:sims", "ok"),
        review.Answer("U9:x", "ok", cite="p.1"),
    ]
    problems = review.validate(answers, work, design)
    assert "U1:dropout: issue needs a text" in problems
    assert "U1:dropout: U1 has no pin 'VBUS'" in problems
    assert any("verdict must be one of" in p for p in problems)
    assert "board:sims: answered twice" in problems
    assert "board:sims: ok needs a cite (datasheet + page, or 'n/a: <why>')" in problems
    assert "U9:x: not a checklist item of this board" in problems
    ok = [review.Answer("U1:dropout", "ok", pin="vin", cite="p.5"), review.Answer("board:sims", "ok", cite="n/a")]
    assert (
        review.validate([review.Answer("U1:dropout", "ok", pin="V", cite="p.5"), ok[1]], work, design) == []
    )  # VIN, VOUT
    assert review.validate(ok, work, design) == []
    assert review.validate(ok[:1], work, design) == ["board:sims: not answered"]


def test_parse_answers_errors() -> None:
    with pytest.raises(review.ReviewError, match="needs an id"):
        review.parse_answers('[[item]]\nverdict = "ok"\n')
    with pytest.raises(review.ReviewError, match="unknown key"):
        review.parse_answers('[[item]]\nid = "a"\nverdict = "ok"\nscore = 3\n')
    with pytest.raises(review.ReviewError, match=r"review\.toml"):
        review.parse_answers("[[item]\n")


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "BOARDS_DIR", tmp_path / "boards")
    monkeypatch.setattr(paths, "OUT_DIR", tmp_path / "out")
    (tmp_path / "boards" / "b" / "sim").mkdir(parents=True)
    (tmp_path / "boards" / "b" / "sim" / "powerup.cir").write_text("powerup\n", encoding="utf-8")
    (tmp_path / "boards" / "b" / "intent.toml").write_text('description = "test"\n', encoding="utf-8")
    raw = tmp_path / "out" / "b" / ".work" / "raw"
    raw.mkdir(parents=True)
    (raw / "b.circuit.json").write_text(json.dumps(circuit(PARTS)), encoding="utf-8")
    (raw / "b.netlist.txt").write_text("NET +3V3: U2.VDD ...\n", encoding="utf-8")
    return tmp_path / "boards" / "b"


def test_package(board: Path) -> None:
    folder, work = review.write_package("b")
    assert {p.name for p in folder.iterdir()} == {
        "PROMPT.md",
        "parts.md",
        "checks.md",
        "netlist.txt",
        "intent.toml",
        "sim",
        "review.toml",
        "netlist.hash",
    }
    assert (folder / "sim" / "powerup.cir").exists()
    template = tomllib.loads((folder / "review.toml").read_text(encoding="utf-8"))
    assert [i["id"] for i in template["item"]] == [i.id for i in work]
    parts = (folder / "parts.md").read_text(encoding="utf-8")
    assert "| U2 | mcu |" in parts
    assert "STM32G031F6P6" in parts
    assert "C2286" in parts
    assert "- **U1**: 1 VIN = +5V, 2 VOUT = +3V3" in parts
    assert f"all {len(work)} items" in (folder / "PROMPT.md").read_text(encoding="utf-8")
    # answers already given survive a re-package of the same netlist
    (folder / "review.toml").write_text("# answered\n", encoding="utf-8")
    review.write_package("b")
    assert (folder / "review.toml").read_text(encoding="utf-8") == "# answered\n"


def test_merge_and_status(board: Path) -> None:
    folder, _ = review.write_package("b")
    design = netcheck.load_design(circuit(PARTS))
    assert review.status("b", design) == (
        "Review: none",
        "review_missing: no reviewer-agent review yet (`pcb review b --run`, docs/REVIEW.md)",
    )
    template = (folder / "review.toml").read_text(encoding="utf-8")
    (folder / "review.toml").write_text(
        answer_all(
            template,
            {
                "U1:dropout": {"verdict": "issue", "pin": "VIN", "cite": "TLV755 p.5", "text": "4.75 V in is too low"},
                "Y1:load-caps": {"verdict": "unknown", "text": "no crystal datasheet"},
            },
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    lines, n = review.merge("b", now=now)
    assert n == 2
    assert "1 issue(s), 1 unknown" in lines[0]
    found = {f.id: f for f in findings.load("b")}
    assert found["review:U1:dropout"].ref == "U1"
    assert found["review:U1:dropout"].pin == "VIN"
    assert found["review:U1:dropout"].text == "4.75 V in is too low [TLV755 p.5]"
    assert found["review:Y1:load-caps"].text.endswith("(reviewer could not decide)")
    assert all(f.source == "review" for f in found.values())
    summary, problem = review.status("b", design)
    assert problem is None
    assert summary.startswith("Review: 2026-10-22 12:00 UTC")
    # accepted with a reason survives the next merge; a fixed issue disappears
    path = board / "findings.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'status = "open"\nreason = ""\nsource = "review"',
            'status = "accepted"\nreason = "lab supply"\nsource = "review"',
            1,
        ),
        encoding="utf-8",
    )
    (folder / "review.toml").write_text(
        answer_all(
            template,
            {"U1:dropout": {"verdict": "issue", "pin": "VIN", "cite": "TLV755 p.5", "text": "4.75 V in is too low"}},
        ),
        encoding="utf-8",
    )
    review.merge("b", now=now)
    again = {f.id: f for f in findings.load("b")}
    assert set(again) == {"review:U1:dropout"}
    assert again["review:U1:dropout"].status == "accepted"
    assert again["review:U1:dropout"].reason == "lab supply"
    # a netlist change makes the review stale
    changed = netcheck.load_design(circuit([*PARTS[:-1], c("C1", 2.2e-5, "+5V", "GND")]))
    assert (
        review.status("b", changed)[1]
        == "review_stale: the netlist changed since the review of 2026-10-22 12:00 UTC (`pcb review b --run`)"
    )


def test_merge_rejects_incomplete_answers(board: Path) -> None:
    review.write_package("b")
    with pytest.raises(review.ReviewError, match="not answered"):
        review.merge("b")
    assert not review.record_path("b").exists()


def test_check_counts_a_missing_review(board: Path) -> None:
    report = verify.run("b", strict=True)
    assert "  [error] review_missing: no reviewer-agent review yet" in "\n".join(report.lines)


def test_cli_review_run_with_a_fake_reviewer(board: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "fake_reviewer.py"
    fake.write_text(
        "import json, pathlib, re, sys\n"
        "path = pathlib.Path(re.search(r'read (\\S+)/PROMPT.md', sys.argv[-1]).group(1)) / 'review.toml'\n"
        "out = []\n"
        "for line in path.read_text().splitlines():\n"
        "    if line == 'verdict = \"\"': line = 'verdict = \"ok\"'\n"
        "    if line == 'cite = \"\"': line = 'cite = \"datasheet p.1\"'\n"
        "    out.append(line)\n"
        "path.write_text('\\n'.join(out))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBEPCB_REVIEWER", f"{sys.executable} {fake}")
    assert cli.main(["review", "b", "--run"]) == 0
    assert review.record_path("b").exists()
    assert findings.load("b") == []


def test_reviewer_command_passes_the_agent_inline() -> None:
    cmd = review.reviewer_command()
    assert cmd[:2] == ["claude", "-p"]
    definition = json.loads(cmd[cmd.index("--agents") + 1])
    assert set(definition) == {"pcb-reviewer"}
    agent = definition["pcb-reviewer"]
    assert "datasheet" in agent["prompt"]
    assert "WebSearch" in agent["tools"]
    assert cmd[cmd.index("--agent") + 1] == "pcb-reviewer"
