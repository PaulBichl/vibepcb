"""Minimal lossless S-expression reader/writer for KiCad files.

KiCad files are plain S-expressions. Python libraries (kiutils, kicad-skip)
model them with classes and silently drop KiCad 8/9 details they don't know
(property layers/uuids, ...). This module keeps *everything*: a node is a
``list`` whose items are ``Sym`` (bare atom), ``str`` (quoted string) or nested
lists. Fixes edit that tree and :func:`dumps` writes it back.

    tree = load(path)
    for fp in children(tree, "footprint"):
        ref = property_value(fp, "Reference")
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class Sym(str):
    """A bare (unquoted) atom such as ``footprint``, ``F.Cu`` or ``1.6``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"Sym({str.__repr__(self)})"


type Node = Sym | str | list[Node]

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.DOTALL)
_UNESCAPE = {"n": "\n", "t": "\t", "r": "\r"}


def loads(text: str) -> list[Node]:
    """Parse one top-level S-expression."""
    stack: list[list[Node]] = [[]]
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            msg = f"Unexpected character at offset {pos}: {text[pos : pos + 20]!r}"
            raise ValueError(msg)
        pos = m.end()
        opening, closing, quoted, atom = m.groups()
        if opening:
            stack.append([])
        elif closing:
            if len(stack) < 2:
                msg = f"Unbalanced ')' at offset {pos}"
                raise ValueError(msg)
            done = stack.pop()
            stack[-1].append(done)
        elif quoted is not None:
            stack[-1].append(re.sub(r"\\(.)", lambda e: _UNESCAPE.get(e[1], e[1]), quoted))
        else:
            stack[-1].append(Sym(atom))
    if len(stack) != 1 or len(stack[0]) != 1 or not isinstance(stack[0][0], list):
        msg = "Expected exactly one balanced top-level expression"
        raise ValueError(msg)
    return stack[0][0]


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _dump(node: Node, indent: int, out: list[str]) -> None:
    if isinstance(node, Sym):
        out.append(str(node))
    elif isinstance(node, str):
        out.append(_quote(node))
    elif not any(isinstance(n, list) for n in node):
        out.append("(" + " ".join(_atom(n) for n in node) + ")")
    else:
        pad = "  " * (indent + 1)
        out.append("(")
        first = True
        for n in node:
            if isinstance(n, list):
                out.append("\n" + pad)
                _dump(n, indent + 1, out)
            else:
                out.append(("" if first else " ") + _atom(n))
            first = False
        out.append("\n" + "  " * indent + ")")


def _atom(node: Node) -> str:
    assert not isinstance(node, list)
    return str(node) if isinstance(node, Sym) else _quote(node)


def dumps(node: Node) -> str:
    out: list[str] = []
    _dump(node, 0, out)
    return "".join(out) + "\n"


def load(path: Path | str) -> list[Node]:
    return loads(Path(path).read_text(encoding="utf-8"))


def dump(node: Node, path: Path | str) -> None:
    Path(path).write_text(dumps(node), encoding="utf-8")


# --- navigation helpers -------------------------------------------------------


def head(node: Node) -> str | None:
    """The leading symbol of a list node (``footprint`` for ``(footprint ...)``)."""
    if isinstance(node, list) and node and isinstance(node[0], Sym):
        return str(node[0])
    return None


def children(node: list[Node], name: str) -> Iterator[list[Node]]:
    """Direct sub-lists of ``node`` whose head is ``name``."""
    for n in node:
        if isinstance(n, list) and head(n) == name:
            yield n


def child(node: list[Node], name: str) -> list[Node] | None:
    return next(children(node, name), None)


def walk(node: list[Node]) -> Iterator[list[Node]]:
    """All list nodes, depth first, including ``node`` itself."""
    yield node
    for n in node:
        if isinstance(n, list):
            yield from walk(n)


def property_node(node: list[Node], key: str) -> list[Node] | None:
    """``(property "<key>" "<value>" ...)`` child of a footprint / symbol."""
    return next((p for p in children(node, "property") if len(p) > 2 and p[1] == key), None)


def property_value(node: list[Node], key: str) -> str | None:
    prop = property_node(node, key)
    return None if prop is None else str(prop[2])
