from __future__ import annotations

import copy
import tomllib
from typing import Any

import pytest

from vibepcb.parts import spec

SOIC8: dict[str, Any] = {
    "part": {
        "mpn": "LM358DR",
        "manufacturer": "Texas Instruments",
        "description": "Dual op-amp",
        "datasheet": "https://www.ti.com/lit/ds/symlink/lm358.pdf",
        "kind": "opamp",
    },
    "package": {
        "family": "SOIC",
        "pins": 8,
        "pitch": 1.27,
        "span": [5.8, 6.2],
        "body": {"l": [4.8, 5.0], "w": [3.8, 4.0], "h": [1.75]},
        "lead": {"l": [0.4, 1.27], "w": [0.31, 0.51]},
        "page": 30,
    },
    "pin": [
        {"number": str(n), "name": name, "type": t, "page": 3}
        for n, (name, t) in enumerate(
            [
                ("OUT1", "output"),
                ("IN1-", "input"),
                ("IN1+", "input"),
                ("GND", "power_in"),
                ("IN2+", "input"),
                ("IN2-", "input"),
                ("OUT2", "output"),
                ("VCC", "power_in"),
            ],
            start=1,
        )
    ],
}


def broken(path: str, value: Any) -> dict[str, Any]:
    data = copy.deepcopy(SOIC8)
    *keys, last = path.split(".")
    node: Any = data
    for key in keys:
        node = node[int(key)] if key.isdigit() else node[key]
    if value is None:
        del node[last]
    else:
        node[last] = value
    return data


def test_valid_spec() -> None:
    s = spec.parse(SOIC8)
    assert s.package.span == spec.Dim(5.8, 6.0, 6.2)
    assert s.package.height == 1.75
    assert s.package.style == "gullwing"
    assert s.symbol_shape == "opamp"
    assert s.pad_numbers == [str(n) for n in range(1, 9)]


def test_exposed_pad_is_a_pad() -> None:
    data = broken("package.exposed_pad", {"l": [2.0, 2.2], "w": [2.0, 2.2], "number": "9"})
    with pytest.raises(spec.SpecError, match=r"no pin for pad\(s\) \['9'\]"):
        spec.parse(data)
    data["pin"].append({"number": "9", "name": "EP", "type": "power_in", "page": 3})
    assert spec.parse(data).pad_numbers[-1] == "9"


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("package.family", "BGA", "family"),
        ("package.page", None, "page"),
        ("package.span", [6.2, 5.8], "min <= nom <= max"),
        ("package.span", "6", "a dimension is"),
        ("package.lead", {}, "needs span|needs lead"),
        ("package.pins", 7, "even pin count"),
        ("package.colour", "red", "unknown key"),
        ("part.kind", "widget", "kind"),
        ("part.mpn", None, "missing 'mpn'"),
        ("pin.0.type", "inout", "type"),
        ("pin.0.page", None, r"\[\[pin\]\] 1: 'page'"),
        ("pin.1.number", "1", "duplicate pin"),
        ("pin.7.number", "10", r"no pin for pad\(s\) \['8'\]"),
        ("package.span", [1.0, 2.0], "span.min - 2 x lead.l.max"),
    ],
)
def test_broken_specs(path: str, value: Any, message: str) -> None:
    with pytest.raises(spec.SpecError, match=message):
        spec.parse(broken(path, value))


def test_pin_without_pad() -> None:
    data = copy.deepcopy(SOIC8)
    data["pin"].append({"number": "10", "name": "X", "type": "input", "page": 3})
    with pytest.raises(spec.SpecError, match=r"pin\(s\) \['10'\] have no pad"):
        spec.parse(data)


def test_custom_family_needs_land() -> None:
    data = broken("package.family", "custom")
    with pytest.raises(spec.SpecError, match=r"\[land\]"):
        spec.parse(data)
    data["land"] = {
        "page": 31,
        "pad": [{"number": str(n), "x": 0.0, "y": float(n), "l": 1.5, "w": 0.6} for n in range(1, 9)],
    }
    assert spec.parse(data).land is not None


def test_skeleton_round_trips() -> None:
    text = spec.skeleton(mpn='A"B/1', manufacturer="ACME", datasheet="https://x/y.pdf", package_hint="8-SOIC")
    data = tomllib.loads(text)
    assert data["part"]["mpn"] == 'A"B/1'
    assert "8-SOIC" in text
    with pytest.raises(spec.SpecError):  # placeholders are not a valid spec
        spec.parse(data)
    assert spec.file_name('A"B/1') == "A_B_1"
