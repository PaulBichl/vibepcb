"""``pcb doctor``: what the tool needs on this machine, one OK / WARN / FAIL line each, with a hint."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vibepcb import easyeda, kicad, proc, symlib
from vibepcb.paths import ROOT

if TYPE_CHECKING:
    from collections.abc import Callable

OK, WARN, FAIL = "OK", "WARN", "FAIL"
NETWORK_TIMEOUT_S = 5
PATCH_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "patch-tscircuit.mjs"

# (what it is for, url): the endpoints the tool talks to
ENDPOINTS = (
    ("tscircuit docs (`pcb docs`)", "https://docs.tscircuit.com/llms.txt"),
    ("tscircuit parts engine (`pcb fab`)", "https://jlcsearch.tscircuit.com"),
    ("EasyEDA/LCSC symbols (`fix`)", easyeda.API.split("/api/")[0]),
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


def reachable(url: str) -> str | None:
    """None if ``url`` answers within a few seconds (any HTTP status), else the reason it does not."""
    request = urllib.request.Request(url, headers={"User-Agent": "vibepcb-doctor"}, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_S):
            return None
    except urllib.error.HTTPError:
        return None  # it answered
    except (OSError, ValueError) as exc:
        return str(getattr(exc, "reason", exc))


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else ""


def _tool_version(binary: str, *args: str) -> str:
    try:
        result = proc.run([binary, *args], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return _first_line(result.stdout or result.stderr)


def check_node() -> Check:
    node = shutil.which("node")
    if node is None:
        return Check("node", FAIL, "not found", "run `hatch run setup` (installs Node.js into the hatch env)")
    return Check("node", OK, f"{_tool_version(node, '--version')}  {node}")


def check_bun() -> Check:
    bun = ROOT / "node_modules" / ".bin" / "bun"
    found = str(bun) if bun.exists() or bun.with_suffix(".cmd").exists() else shutil.which("bun")
    if found is None:
        return Check("bun", FAIL, "not found", "run `hatch run setup` (bun is an npm dependency, tsci needs it)")
    return Check("bun", OK, f"{_tool_version(found, '--version')}  {found}")


def check_tsci() -> Check:
    tsci = ROOT / "node_modules" / ".bin" / "tsci"
    npx = shutil.which("npx")
    if not (tsci.exists() or tsci.with_suffix(".cmd").exists()):
        return Check("tsci", FAIL, f"{tsci} missing", "run `hatch run setup` (npm install)")
    if npx is None:
        return Check("tsci", FAIL, "npx not found", "run `hatch run setup` (Node.js into the hatch env)")
    version = _tool_version(npx, "--no-install", "tsci", "--version")
    if not version:
        return Check("tsci", FAIL, "`npx --no-install tsci --version` gives nothing", "re-run `hatch run setup`")
    return Check("tsci", OK, f"{version}  {tsci}")


def check_kicad() -> list[Check]:
    cli = kicad.find_kicad_cli()
    if cli is None:
        return [
            Check(
                "kicad-cli",
                FAIL,
                "not found",
                "install KiCad 9 or 10 (e.g. `sudo pacman -S kicad`, winget/brew) or set $KICAD_CLI; `check`/`fab` need it",
            )
        ]
    version = kicad.cli_version()
    if version is None:
        return [Check("kicad-cli", FAIL, f"{cli} does not answer `version`", "reinstall KiCad or fix $KICAD_CLI")]
    text = ".".join(str(v) for v in version)
    if version[0] not in kicad.SUPPORTED_MAJORS:
        supported = ", ".join(str(m) for m in kicad.SUPPORTED_MAJORS)
        return [Check("kicad-cli", WARN, f"{text}  {cli}", f"supported: KiCad {supported} (fixes tuned on 10)")]
    return [Check("kicad-cli", OK, f"{text}  {cli}")]


def check_symbols() -> Check:
    lib = symlib.library_dir()
    if lib is None:
        return Check(
            "KiCad symbol library",
            WARN,
            "not found",
            "chips get generated boxes instead of KiCad symbols; set $KICAD9_SYMBOL_DIR / $KICAD10_SYMBOL_DIR",
        )
    return Check("KiCad symbol library", OK, str(lib))


def check_png() -> Check:
    if rsvg := shutil.which("rsvg-convert"):
        return Check("rsvg-convert", OK, rsvg)
    try:
        importlib.import_module("cairosvg")  # optional fallback, not a dependency
    except (ImportError, OSError):
        return Check(
            "rsvg-convert",
            WARN,
            "not found (no cairosvg either)",
            "no PNG previews for agents and `inspect`: install librsvg (pacman -S librsvg / apt install librsvg2-bin /"
            " brew install librsvg) or `pip install cairosvg`",
        )
    return Check("rsvg-convert", WARN, "not found, cairosvg is used instead", "librsvg is faster and more exact")


def check_ngspice() -> Check:
    if ngspice := shutil.which("ngspice"):
        return Check("ngspice", OK, ngspice)
    return Check("ngspice", WARN, "not found (optional)", "only needed for boards/<name>/sim/*.cir decks")


def check_patches() -> Check:
    node = shutil.which("node")
    if node is None or not PATCH_SCRIPT.exists():
        return Check("tscircuit patches", WARN, "cannot check", f"needs node and {PATCH_SCRIPT}")
    result = proc.run([node, str(PATCH_SCRIPT), "--check", "--root", str(ROOT)], timeout=120)
    detail = (result.stdout.strip().splitlines() or [""])[0].removeprefix("tscircuit patches: ")
    if result.returncode == 0:
        return Check("tscircuit patches", OK, detail)
    return Check("tscircuit patches", WARN, detail, patch_hint(tscircuit_version(ROOT), tested_tscircuit_version()))


def tscircuit_version(root: Path) -> str | None:
    """The tscircuit version installed in ``root/node_modules``."""
    try:
        return str(json.loads((root / "node_modules" / "tscircuit" / "package.json").read_text())["version"])
    except (OSError, ValueError, KeyError):
        return None


def tested_tscircuit_version() -> str | None:
    """The tscircuit version VibePCB's patches and fixes are made for (its own ``package.json``)."""
    try:
        spec = json.loads((PATCH_SCRIPT.parents[1] / "package.json").read_text())["devDependencies"]["tscircuit"]
    except (OSError, ValueError, KeyError):
        return None
    return str(spec).lstrip("^~=")


def patch_hint(installed: str | None, tested: str | None) -> str:
    """What to do about missing patches: another tscircuit version (a project repo pinned its own) needs
    the tested one first; otherwise the patch script was just not run (``npm install`` runs it as postinstall)."""
    why = "some boards crash the autorouter / pour export without them"
    if installed and tested and installed != tested:
        return (
            f"tscircuit {installed} is installed, VibePCB is patched and tested for {tested}: pin"
            f' "tscircuit": "{tested}" in package.json (a project repo: copy VibePCB\'s package-lock.json and add'
            f' "postinstall": "node {PATCH_SCRIPT}"), then npm install; {why}'
        )
    return f"run `node {PATCH_SCRIPT} --root {ROOT}` (npm install does it as postinstall); {why}"


def check_network(probe: Callable[[str], str | None] = reachable) -> list[Check]:
    checks = []
    for what, url in ENDPOINTS:
        reason = probe(url)
        if reason is None:
            checks.append(Check(f"network: {what}", OK, url))
        else:
            checks.append(Check(f"network: {what}", WARN, f"{url}: {reason}", "needs internet; an offline build works"))
    return checks


def collect(network: Callable[[str], str | None] = reachable) -> list[Check]:
    """Every check, in the order a setup is fixed."""
    return [
        check_node(),
        check_bun(),
        check_tsci(),
        check_patches(),
        *check_kicad(),
        check_symbols(),
        check_png(),
        check_ngspice(),
        *check_network(network),
    ]


def table(checks: list[Check]) -> str:
    """Aligned text table; hints go under the line of every non-OK check."""
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        lines.append(f"{c.status:<4}  {c.name:<{width}}  {c.detail}".rstrip())
        if c.status != OK and c.hint:
            lines.append(f"      {'':<{width}}  -> {c.hint}")
    failed = sum(c.status == FAIL for c in checks)
    warned = sum(c.status == WARN for c in checks)
    lines.append(f"doctor: {len(checks) - failed - warned} ok, {warned} warning(s), {failed} failure(s)")
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
