from __future__ import annotations

import pytest

from vibepcb import doctor, kicad, symlib


def test_table_and_exit_code() -> None:
    checks = [
        doctor.Check("node", doctor.OK, "v22"),
        doctor.Check("rsvg-convert", doctor.WARN, "missing", "install librsvg"),
        doctor.Check("kicad-cli", doctor.FAIL, "not found", "install KiCad"),
    ]
    text = doctor.table(checks)
    assert "-> install librsvg" in text
    assert "-> install KiCad" in text
    assert "1 ok, 1 warning(s), 1 failure(s)" in text
    assert doctor.exit_code(checks) == 1
    assert doctor.exit_code(checks[:2]) == 0


def test_network_check_reports_unreachable_hosts_as_warnings() -> None:
    checks = doctor.check_network(lambda url: None if "docs" in url else "timed out")
    assert [c.status for c in checks] == [doctor.OK, doctor.WARN, doctor.WARN]
    assert "timed out" in checks[1].detail


def test_kicad_missing_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kicad, "find_kicad_cli", lambda: None)
    assert [c.status for c in doctor.check_kicad()] == [doctor.FAIL]


@pytest.mark.parametrize(
    ("version", "status"), [((10, 0, 1), doctor.OK), ((9, 0, 3), doctor.OK), ((8, 0, 9), doctor.WARN)]
)
def test_kicad_version_support(monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int], status: str) -> None:
    monkeypatch.setattr(kicad, "find_kicad_cli", lambda: "/usr/bin/kicad-cli")
    monkeypatch.setattr(kicad, "cli_version", lambda: version)
    assert doctor.check_kicad()[0].status == status


def test_parse_version_and_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    assert kicad.parse_version("10.0.1-rc1\n") == (10, 0, 1)
    assert kicad.parse_version("9.0") == (9, 0, 0)
    assert kicad.parse_version("garbage") is None
    monkeypatch.setattr(kicad, "find_kicad_cli", lambda: "kicad-cli")
    monkeypatch.setattr(kicad, "cli_version", lambda: (8, 0, 0))
    assert kicad.version_warning() is not None
    monkeypatch.setattr(kicad, "cli_version", lambda: (10, 0, 0))
    assert kicad.version_warning() is None
    monkeypatch.setattr(kicad, "find_kicad_cli", lambda: None)
    assert kicad.version_warning() is None


def test_symbol_library_dirs_follow_the_kicad_cli_location() -> None:
    dirs = symlib.cli_relative_dirs("/opt/kicad/10.0/bin/kicad-cli")
    assert dirs[0].endswith("/10.0/share/kicad/symbols")
    assert any(d.endswith("/SharedSupport/symbols") for d in dirs)
    assert any("Program Files/KiCad" in p for p in symlib.LIBRARY_DIRS)


def test_patch_hint_names_the_tested_tscircuit_version() -> None:
    hint = doctor.patch_hint("0.0.2900", "0.0.2786")
    assert '"tscircuit": "0.0.2786"' in hint
    assert "postinstall" in hint
    assert "--root" in doctor.patch_hint("0.0.2786", "0.0.2786")
    assert "--root" in doctor.patch_hint(None, "0.0.2786")


def test_tested_tscircuit_version_reads_the_checkout() -> None:
    version = doctor.tested_tscircuit_version()
    assert version is not None
    assert version[0].isdigit()
