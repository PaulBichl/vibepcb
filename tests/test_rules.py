from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibepcb import board, config, rules

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "fab.toml").write_text(
        'kind = "fab"\nmin_clearance = 0.1\nmin_track_width = 0.1\nmin_via_annular_ring = 0.05\nboard_thickness = 1.6\n',
        encoding="utf-8",
    )
    (tmp_path / "strict.toml").write_text(
        'kind = "application"\nmin_clearance = 0.15\nmin_track_width = 0.08\nboard_thickness = 1.2\n'
        '[netclass.HV]\nnets = ["HV_*"]\nclearance = 0.6\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rules, "profile_dirs", lambda: [tmp_path])
    return tmp_path


def test_shipped_profiles_load() -> None:
    for name in ("jlcpcb-2l", "jlcpcb-4l", "jlcpcb-6l", "ipc-class2", "ipc-class3", "hv-60v"):
        assert name in rules.available()
        rules.load_profile(name)
    rs = rules.merge({"rules": {"profiles": ["jlcpcb-4l", "ipc-class3", "hv-60v"]}})
    assert rs is not None
    assert rs.values["min_clearance"] == rules.Setting(0.13, "ipc-class3")
    assert rs.values["min_via_drill"] == rules.Setting(0.15, "jlcpcb-4l")
    assert rs.netclasses["HV60"].values["clearance"].value == 0.6


def test_merge_order(profiles: Path) -> None:
    assert rules.merge({}) is None
    rs = rules.merge({"rules": {"profiles": ["fab", "strict"], "override": {"track_width": 0.2}}})
    assert rs is not None
    assert rs.values["min_clearance"] == rules.Setting(0.15, "strict")  # later profile wins
    assert rs.values["min_track_width"] == rules.Setting(0.1, "fab")  # an application layer only tightens
    assert rs.values["board_thickness"] == rules.Setting(1.2, "strict")
    assert rs.values["track_width"] == rules.Setting(0.2, "override")
    assert rs.floors["min_clearance"] == rules.Setting(0.1, "fab")
    rs = rules.merge({"rules": {"profiles": ["fab", "strict"], "override": {"min_clearance": 0.12}}})
    assert rs is not None
    assert rs.values["min_clearance"] == rules.Setting(0.12, "override")  # overrides win, above the fab
    lines = rules.table(rs)
    assert any("min_clearance" in line and "override" in line and "fab capability 0.1" in line for line in lines)
    assert any(line.startswith("netclass HV: nets HV_* (strict)") for line in lines)


def test_below_fab_capability_raises(profiles: Path) -> None:
    with pytest.raises(rules.RulesError, match="below the fab capability"):
        rules.merge({"rules": {"profiles": ["fab"], "override": {"min_clearance": 0.05}}})
    with pytest.raises(rules.RulesError, match="Default net class track_width"):
        rules.merge({"rules": {"profiles": ["fab"], "override": {"track_width": 0.09}}})
    with pytest.raises(rules.RulesError, match="net class USB clearance"):
        rules.merge({"rules": {"profiles": ["fab"]}, "netclass": {"USB": {"nets": ["USB*"], "clearance": 0.08}}})
    with pytest.raises(rules.RulesError, match="annular ring"):
        rules.merge({"rules": {"profiles": ["fab"], "override": {"via_diameter": 0.3, "via_drill": 0.25}}})
    with pytest.raises(rules.RulesError, match="not found"):
        rules.merge({"rules": {"profiles": ["nope"]}})


def test_board_netclass_overrides_profile_class(profiles: Path) -> None:
    cfg = {"rules": {"profiles": ["fab", "strict"]}, "netclass": {"HV": {"nets": ["VBAT"], "track_width": 0.5}}}
    assert rules.netclasses(cfg) == {"HV": {"nets": ["VBAT"], "clearance": 0.6, "track_width": 0.5}}


def test_board_impedance_netclass_is_solved(profiles: Path) -> None:
    # impedance net classes carry a layer name and target ohms: merge solves them into widths instead of reading floats
    cfg = {
        "rules": {"profiles": ["fab"]},
        "stackup": {"preset": "jlc04161h-7628"},
        "netclass": {"USB": {"nets": ["USB_D*"], "impedance_diff": 90, "diff_pair_gap": 0.2, "layer": "F.Cu"}},
    }
    usb = rules.netclasses(cfg)["USB"]
    assert usb["diff_pair_gap"] == 0.2
    assert 0.3 < usb["diff_pair_width"] < 0.34
    assert "layer" not in usb


def test_rules_config_validation() -> None:
    config.validate({"rules": {"profiles": ["jlcpcb-4l"], "override": {"min_clearance": 0.15}}})
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.validate({"rules": {"override": {"min_gap": 0.15}}})
    with pytest.raises(config.ConfigError, match="positive number"):
        config.validate({"rules": {"override": {"min_clearance": "0.15"}}})
    with pytest.raises(config.ConfigError, match="list of names"):
        config.validate({"rules": {"profiles": "jlcpcb-4l"}})


def test_router_rules_respect_minimums() -> None:
    pro = {
        "net_settings": {"classes": [{"name": "Default", "clearance": 0.1, "via_diameter": 0.3, "via_drill": 0.2}]},
        "board": {"design_settings": {"rules": {"min_clearance": 0.15, "min_via_annular_width": 0.1}}},
    }
    r = board.Rules.from_pro(pro)
    assert r.default.clearance == 0.15
    assert r.default.via_diameter == 0.4
