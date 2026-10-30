from __future__ import annotations

import pytest

from vibepcb.parts import standard


def test_shipped_standard_loads() -> None:
    std = standard.load()
    assert std.name == "altium-students-2022"
    assert std.num("symbol", "pin_pitch") == 2.0
    assert std.rule("symbol", "pin_pitch").source.startswith("§1.2")
    assert std.text("footprint", "density") == "N"
    assert std.value("symbol", "parameters") == []
    assert std.font_mm("symbol", "pin_font_pt") == pytest.approx(1.524)
    assert std.rule("choices", "silk_width").source  # the reason
    assert any(item.startswith("§1.4") for item in std.unexpressed)
    # every rule of the shipped file names its section
    for section in ("symbol", "footprint", "layers"):
        for key, rule in std.rules[section].items():
            assert rule.source.startswith("§"), f"[{section}] {key}: {rule.source}"


def test_missing_section_takes_defaults() -> None:
    std = standard.parse({"meta": {"name": "x"}}, name="x")
    assert std.rule("symbol", "pin_pitch") == standard.Rule(2.54, "default")
    assert std.unexpressed == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"symbl": {}}, "unknown section"),
        ({"symbol": {"pin_pich": {"value": 2.0, "source": "§1"}}}, "unknown key"),
        ({"symbol": {"pin_pitch": 2.0}}, "value = ..., source"),
        ({"symbol": {"pin_pitch": {"value": 2.0}}}, "value = ..., source"),
        ({"symbol": {"pin_pitch": {"value": 2.0, "source": ""}}}, "value = ..., source"),
        ({"choices": {"silk_width": {"value": 0.2, "source": "x"}}}, "reason"),
        ({"symbol": {"pin_pitch": {"value": "2", "source": "§1"}}}, "expected float"),
        ({"symbol": {"exposed_pad_pin": {"value": 1, "source": "§1"}}}, "expected bool"),
        ({"footprint": {"density": {"value": "X", "source": "§2"}}}, "density"),
        ({"unexpressed": {"items": [1]}}, "list of strings"),
    ],
)
def test_bad_rule_files_fail(data: dict[str, object], message: str) -> None:
    with pytest.raises(standard.StandardError, match=message):
        standard.parse(data, name="x")


def test_unknown_standard() -> None:
    with pytest.raises(standard.StandardError, match="not found"):
        standard.load("no-such-standard")
