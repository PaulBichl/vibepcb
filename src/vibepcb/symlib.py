"""Real schematic symbols for chips: KiCad's own symbol library, matched by manufacturer part number.

``schgen`` draws a chip with a library symbol when one matches its MPN *and* fits its netlist:
the same pin numbers, mostly the same pin names, a single unit, no hidden pins, stacked pins only
on one net. Anything else falls back to the generated box. The library is KiCad's installed one
(``$KICAD*_SYMBOL_DIR``, the system or flatpak install); its symbol names are indexed once and
cached in ``~/.cache/vibepcb``.
"""

from __future__ import annotations

import copy
import glob
import json
import os
import re
import shutil
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vibepcb import kicad, sexpr
from vibepcb.sexpr import Sym

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vibepcb.sexpr import Node

LIBRARY_DIRS = (
    "/usr/share/kicad/symbols",
    "/usr/local/share/kicad/symbols",
    "/var/lib/flatpak/runtime/org.kicad.KiCad.Library.Symbols/*/stable/*/files/symbols",
    "~/.local/share/flatpak/runtime/org.kicad.KiCad.Library.Symbols/*/stable/*/files/symbols",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
    "/opt/homebrew/share/kicad/symbols",
    "/usr/local/opt/kicad/share/kicad/symbols",
    "C:/Program Files/KiCad/*/share/kicad/symbols",
    "C:/Program Files (x86)/KiCad/*/share/kicad/symbols",
    "~/AppData/Local/Programs/KiCad/*/share/kicad/symbols",
)
CACHE = Path("~/.cache/vibepcb").expanduser()
_TOP_SYMBOL = re.compile(r'^\t\(symbol "([^"]+)"', re.MULTILINE)
_UNIT = re.compile(r"_(\d+)_(\d+)$")
MIN_NAME = 4  # shorter library names match too much
MIN_NAME_MATCH = 0.5  # share of named pins whose names must agree


def cli_relative_dirs(cli: str) -> list[str]:
    """Where the symbol library sits relative to a ``kicad-cli`` binary (Linux/Windows prefix, macOS app bundle)."""
    binary = Path(cli).resolve()
    prefix = binary.parent.parent  # <prefix>/bin/kicad-cli, KiCad\\10.0\\bin\\kicad-cli.exe, KiCad.app/Contents/MacOS
    return [
        (prefix / "share" / "kicad" / "symbols").as_posix(),
        (prefix / "SharedSupport" / "symbols").as_posix(),
        (prefix / "Resources" / "symbols").as_posix(),
    ]


def library_dir() -> Path | None:
    """KiCad's installed symbol library (a directory of ``*.kicad_sym``), if any."""
    for key in sorted(os.environ, reverse=True):
        if re.fullmatch(r"KICAD\d*_SYMBOL_DIR", key) and Path(os.environ[key]).is_dir():
            return Path(os.environ[key])
    cli = kicad.find_kicad_cli()
    found_cli = shutil.which(cli) if cli else None  # $KICAD_CLI may be a bare name
    derived = [glob.escape(d) for d in cli_relative_dirs(found_cli or cli)] if cli else []
    for pattern in [*derived, *LIBRARY_DIRS]:
        for found in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if Path(found).is_dir() and any(Path(found).glob("*.kicad_sym")):
                return Path(found)
    return None


def _stamp(lib: Path) -> str:
    files = list(lib.glob("*.kicad_sym"))
    return f"{lib}:{max(p.stat().st_mtime for p in files)}" if files else ""


def _index(lib: Path) -> dict[str, str]:
    """Symbol name -> library file name, cached by library path and modification time."""
    stamp = _stamp(lib)
    if not stamp:
        return {}
    cache = CACHE / "kicad-symbols.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("stamp") == stamp:
            return dict(data["index"])
    except (OSError, ValueError):
        pass
    index: dict[str, str] = {}
    for file in sorted(lib.glob("*.kicad_sym")):
        for name in _TOP_SYMBOL.findall(file.read_text(encoding="utf-8", errors="replace")):
            index.setdefault(name, file.name)
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"stamp": stamp, "index": index}), encoding="utf-8")
    except OSError:
        pass  # read-only home: index again next time
    return index


def _pattern(name: str) -> re.Pattern[str]:
    """KiCad names use a lowercase ``x`` for "any" (``STM32G431RBTx``, ``BZX84Cxx``)."""
    return re.compile("".join("." if c == "x" else re.escape(c.upper()) for c in name))


def candidates(mpn: str, index: Iterable[str]) -> list[str]:
    """Library names matching the start of ``mpn``, best (longest) first."""
    target = mpn.upper()
    found = [
        n
        for n in index
        if len(n) >= MIN_NAME and (n[0] == "x" or n[0].upper() == target[:1]) and _pattern(n).match(target)
    ]
    return sorted(found, key=lambda n: (-len(n), n))


@dataclass(frozen=True)
class LibPin:
    number: str
    name: str
    x: float  # connection point, library frame (y up)
    y: float
    angle: int  # direction the pin points from its connection point into the body
    hidden: bool
    unit: int = 1  # 0: common to all units


@dataclass
class LibSymbol:
    name: str  # library name, e.g. "STM32G431RBTx"
    node: list[Node]  # flattened ``(symbol ...)`` without ``extends``
    pins: list[LibPin]

    def units(self) -> set[int]:
        return {
            int(m[1])
            for sub in sexpr.children(self.node, "symbol")
            if (m := _UNIT.search(str(sub[1]))) and int(m[1]) > 0
        }

    def bbox(self, unit: int | None = None) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) of the graphics and pins (of one unit), library frame."""
        xs, ys = [], []
        for sub in sexpr.children(self.node, "symbol"):
            m = _UNIT.search(str(sub[1]))
            if unit is not None and m and int(m[1]) not in {0, unit}:
                continue
            for item in sexpr.walk(sub):
                if sexpr.head(item) in {"xy", "start", "end", "center", "mid"} and len(item) > 2:
                    xs.append(float(str(item[1])))
                    ys.append(float(str(item[2])))
        for pin in self.pins:
            if unit is None or pin.unit in {0, unit}:
                xs.append(pin.x)
                ys.append(pin.y)
        return (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)


class Library:
    """Lazily parsed KiCad symbol library."""

    def __init__(self, lib: Path | None = None) -> None:
        self.dir = lib if lib is not None else library_dir()
        self.index = _index(self.dir) if self.dir else {}
        self.stamp = _stamp(self.dir) if self.dir else ""
        self._files: dict[str, list[Node]] = {}

    def _file(self, name: str) -> list[Node]:
        if name not in self._files:
            assert self.dir is not None
            self._files[name] = sexpr.load(self.dir / name)
        return self._files[name]

    def _raw(self, name: str) -> list[Node] | None:
        file = self.index.get(name)
        if file is None:
            return None
        return next((s for s in sexpr.children(self._file(file), "symbol") if str(s[1]) == name), None)

    def symbol(self, name: str) -> LibSymbol | None:
        """The symbol ``name`` with ``extends`` resolved, cached on disk (library files are big)."""
        if self.dir is None or name not in self.index:
            return None
        key = re.sub(r"[^A-Za-z0-9_.+-]", "_", name)
        cache = CACHE / "kicad-symbols" / f"{zlib.crc32(self.stamp.encode()):08x}-{key}.kicad_sym"
        try:
            node = sexpr.loads(cache.read_text(encoding="utf-8"))
            return LibSymbol(name, node, pins_of(node))
        except (OSError, ValueError, IndexError):
            pass
        symbol = self._resolve(name)
        if symbol is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(sexpr.dumps(symbol.node), encoding="utf-8")
            except OSError:
                pass
        return symbol

    def _resolve(self, name: str) -> LibSymbol | None:
        """The symbol ``name`` with ``extends`` resolved (the parent's units, the child's fields)."""
        raw = self._raw(name)
        if raw is None:
            return None
        node = copy.deepcopy(raw)
        parent_ref = sexpr.child(node, "extends")
        if parent_ref is not None:
            parent = self._resolve(str(parent_ref[1]))
            if parent is None:
                return None
            node.remove(parent_ref)
            own = {str(p[1]) for p in sexpr.children(node, "property")}
            for item in parent.node[2:]:
                head = sexpr.head(item)
                if head == "symbol" and isinstance(item, list):
                    sub = copy.deepcopy(item)
                    sub[1] = str(sub[1]).replace(parent.name, name, 1)
                    node.append(sub)
                elif head == "property" and str(item[1]) not in own:
                    node.append(copy.deepcopy(item))
                elif head in {"pin_names", "pin_numbers"} and sexpr.child(node, head) is None:
                    node.insert(2, copy.deepcopy(item))
        return LibSymbol(name, node, pins_of(node))


def pins_of(node: list[Node]) -> list[LibPin]:
    """Every pin of a ``(symbol ...)`` with its unit (De Morgan alternative bodies skipped)."""
    pins = []
    for sub in sexpr.children(node, "symbol"):
        m = _UNIT.search(str(sub[1]))
        if m and int(m[2]) > 1:  # De Morgan alternative body
            continue
        unit = int(m[1]) if m else 1
        for pin in sexpr.children(sub, "pin"):
            at, number, name = sexpr.child(pin, "at"), sexpr.child(pin, "number"), sexpr.child(pin, "name")
            if at is None or number is None:
                continue
            hidden = sexpr.child(pin, "hide") is not None or any(str(x) == "hide" for x in pin)
            pins.append(
                LibPin(
                    str(number[1]),
                    str(name[1]) if name is not None else "",
                    float(str(at[1])),
                    float(str(at[2])),
                    int(float(str(at[3]))) % 360 if len(at) > 3 else 0,
                    hidden,
                    unit,
                )
            )
    return pins


def _norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def _names_agree(ours: str, theirs: str) -> bool:
    a, b = _norm(ours), _norm(theirs)
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    tokens = {t for t in re.split(r"[^A-Z0-9]+", theirs.upper()) if t}
    return a in tokens or any(_norm(t) == a for t in re.split(r"[/,]", theirs))


def fits(symbol: LibSymbol, pins: dict[str, tuple[str, str | None]], *, names: bool = True) -> str | None:
    """Why ``symbol`` does not fit a part with ``pins`` (number -> (name, net)), or None if it does.

    ``names=False`` skips the pin-name check: for symbols from the same source as the part's
    footprint (LCSC), whose pin numbers are authoritative and whose names tscircuit rewrote."""
    numbers = {p.number for p in symbol.pins}
    if numbers != set(pins):
        return f"pin numbers differ ({len(numbers ^ set(pins))})"
    at: dict[tuple[int, float, float], set[str | None]] = {}
    for p in symbol.pins:
        at.setdefault((p.unit, p.x, p.y), set()).add(pins[p.number][1])
    if any(len(nets) > 1 for nets in at.values()):
        return "stacked pins on different nets"
    named = [
        p
        for p in symbol.pins
        if not re.fullmatch(r"(pin)?\d+", pins[p.number][0], re.IGNORECASE) and p.name not in {"", "~"}
    ]
    if named and names:
        agree = sum(_names_agree(pins[p.number][0], p.name) for p in named)
        if agree < MIN_NAME_MATCH * len(named):
            return f"pin names differ ({agree}/{len(named)} agree)"
    return None


def for_schematic(symbol: LibSymbol, lib_id: str) -> list[Node]:
    """The library symbol renamed to ``lib_id``, every pin passive (tscircuit's netlist has no pin types,
    and power-input pins on internal nets would only trip ERC). Hidden pins not stacked under a
    visible one are shown: as passive pins KiCad no longer joins them to a net by name."""
    visible = {(p.unit, p.x, p.y) for p in symbol.pins if not p.hidden}
    node = copy.deepcopy(symbol.node)
    short = lib_id.split(":", 1)[-1]
    node[1] = lib_id
    for sub in list(sexpr.children(node, "symbol")):
        m = _UNIT.search(str(sub[1]))
        if m and int(m[2]) > 1:
            node.remove(sub)
            continue
        sub[1] = f"{short}{m[0]}" if m else str(sub[1])
        for pin in sexpr.children(sub, "pin"):
            pin[1] = Sym("passive")
            at = sexpr.child(pin, "at")
            unit = int(m[1]) if m else 1
            if at is not None and (unit, float(str(at[1])), float(str(at[2]))) not in visible:
                pin[:] = [x for x in pin if not (sexpr.head(x) == "hide" or str(x) == "hide")]
    for prop in sexpr.children(node, "property"):
        if str(prop[1]) == "Footprint":
            prop[2] = ""
    return node
