"""Reviewer agent: a fresh-context review of a board against its datasheets (docs/REVIEW.md).

``pcb review <board>`` writes the review package to ``out/<board>/.work/review/``: only the design's outputs
(parts with pins and nets, BOM fields, netlist, ``intent.toml``, sim decks, ``layout.md`` from
``layoutdoc`` and the board image), never the board source or the design conversation, plus ``PROMPT.md`` and an
answer template ``review.toml`` with one ``[[item]]`` per checklist item (``rules/review-checklists.toml``: per
part type, ids ``<ref>:<key>``; ``[bottom]`` for every bottom-side chip, ``<ref>:bottom-mirroring``; and
``board:<key>``)::

    [[item]]
    id = "U1:dropout"
    verdict = "issue"              # ok | issue | unknown
    pin = "VIN"                    # optional, must exist on the part
    cite = "TLV75533 datasheet p.5: dropout 230 mV at 500 mA"
    text = "USB 4.75 V minimum leaves 1.45 V; fine. ..."

``--run`` starts the reviewer (``$VIBEPCB_REVIEWER``, default ``claude -p --agent pcb-reviewer``, a new session)
and merges its answers; ``--merge`` merges answers written another way. Merging validates the file (every item
answered once, ``ok`` / ``issue`` cite a source, ``issue`` / ``unknown`` say why, refs and pins exist), turns
each ``issue`` / ``unknown`` into an open ``source = "review"`` finding in ``boards/<board>/findings.toml``
(status and reason kept for ids already there; review findings no longer reported are dropped), and writes
the record ``boards/<board>/review.toml`` (all answers + the netlist hash + the layout hash). ``check`` reports
the review as missing, stale (netlist changed since: every item) or layout-stale (only the placement changed: the
layout items, keys ``layout`` and the ``[bottom]`` items; ``pcb review`` then carries the other answers over) - an
error in strict mode, so ``fab`` needs a current review.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibepcb.check import findings, layoutdoc, netcheck, placement, sim
from vibepcb.core import paths
from vibepcb.output import bringup
from vibepcb.settings import intent, rules
from vibepcb.settings.config import ConfigError

if TYPE_CHECKING:
    from vibepcb.check.netcheck import Design

CHECKLISTS_NAME = "review-checklists.toml"
RECORD_NAME = "review.toml"
ANSWERS_NAME = "review.toml"
SOURCE = "review"
VERDICTS = ("ok", "issue", "unknown")
ITEM_KEYS = ("id", "verdict", "pin", "cite", "text")
MIN_MCU_GPIO = 4
FTYPE_KINDS = {
    "simple_pin_header": "connector",
    "simple_connector": "connector",
    "simple_diode": "diode",
    "simple_led": "diode",
    "simple_crystal": "crystal",
    "simple_resistor": "passive",
    "simple_capacitor": "passive",
    "simple_inductor": "passive",
    "simple_test_point": "passive",
    "simple_push_button": "passive",
}
REF_LETTERS = re.compile(r"^[A-Za-z]+")
PREFIX_KINDS = {
    **dict.fromkeys(("J", "P", "CN", "CON", "USB", "X"), "connector"),
    **dict.fromkeys(("D", "LED", "TVS", "ZD"), "diode"),
    **dict.fromkeys(("Q", "T", "M"), "transistor"),
    **dict.fromkeys(("Y", "XTAL", "OSC"), "crystal"),
    **dict.fromkeys(("R", "C", "L", "FB", "F", "TP", "SW", "S", "BTN", "K", "H", "MH", "FID", "BZ"), "passive"),
}
BOTTOM = "bottom"  # checklist table asked once per bottom-side chip
BOTTOM_ITEMS = {  # used when a project's checklist file has no [bottom] table
    "bottom-mirroring": "Bottom side, mirrored in x: every part serving a pin sits on that pin's side of the chip, "
    "next to it (layout.md board positions, not the datasheet top view).",
}
LAYOUT_KEYS = {"layout", *BOTTOM_ITEMS}  # item keys answered from layout.md: stale on a placement change
LAYOUT_NAME = "layout.md"
IMAGE_NAME = "board.png"
IMAGE_LAYERS = "top copper (F.Cu), top silkscreen and the outline, seen from the top; bottom-side parts are not in it"
REVIEWER_AGENT = "pcb-reviewer"
REVIEWER_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "Write", "Edit"]
TOOL_AGENT_FILE = Path(__file__).resolve().parents[3] / ".claude" / "agents" / f"{REVIEWER_AGENT}.md"


def agent_definition(path: Path) -> dict[str, dict[str, Any]]:
    """``{name: {description, prompt, tools}}`` for ``claude --agents`` from an agent file (frontmatter + body)."""
    text = path.read_text(encoding="utf-8")
    _, head, body = text.split("---", 2)
    fields = dict(line.split(":", 1) for line in head.strip().splitlines() if ":" in line)
    name = fields.get("name", REVIEWER_AGENT).strip()
    tools = [x.strip() for x in fields.get("tools", "").split(",") if x.strip()] or REVIEWER_TOOLS
    return {name: {"description": fields.get("description", "").strip(), "prompt": body.strip(), "tools": tools}}


def reviewer_command() -> list[str]:
    """``claude -p`` with the reviewer agent passed inline (``--agents``): project repos have no agent file of their
    own, so a bare ``--agent pcb-reviewer`` was not found there. A project's ``.claude/agents/pcb-reviewer.md``
    overrides the tool's."""
    own = paths.ROOT / ".claude" / "agents" / f"{REVIEWER_AGENT}.md"
    definition = agent_definition(own if own.exists() else TOOL_AGENT_FILE)
    return [
        "claude", "-p", "--agents", json.dumps(definition), "--agent", REVIEWER_AGENT,
        "--allowedTools", *REVIEWER_TOOLS, "--",
    ]  # fmt: skip


class ReviewError(ConfigError):
    """The review package can't be made, or the reviewer's answers are malformed."""


@dataclass(frozen=True)
class Item:
    id: str  # "<ref>:<key>" or "board:<key>"
    ref: str  # "" for board items
    kind: str
    question: str


@dataclass(frozen=True)
class Answer:
    id: str
    verdict: str
    pin: str = ""
    cite: str = ""
    text: str = ""


def display_path(path: Path) -> str:
    """Path as the agent sees it from the project root (absolute outside it)."""
    return path.relative_to(paths.ROOT).as_posix() if path.is_relative_to(paths.ROOT) else path.as_posix()


def checklists_path() -> Path | None:
    return next((d / CHECKLISTS_NAME for d in rules.profile_dirs() if (d / CHECKLISTS_NAME).is_file()), None)


def load_checklists(path: Path | None = None) -> dict[str, dict[str, str]]:
    path = path or checklists_path()
    if path is None:
        msg = f"{CHECKLISTS_NAME} not found in {', '.join(str(d) for d in rules.profile_dirs())}"
        raise ReviewError(msg)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {kind: {str(k): str(v) for k, v in table.items()} for kind, table in data.items()}


def _components(circuit: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(e["name"]): e for e in circuit if e.get("type") == "source_component" and e.get("name")}


def _prefix_kind(ref: str) -> str | None:
    """Checklist type from the reference designator's letters (imported parts are all ``simple_chip``)."""
    letters = REF_LETTERS.match(ref)
    return PREFIX_KINDS.get(letters.group(0).upper()) if letters else None


def classify(design: Design, circuit: list[dict[str, Any]]) -> dict[str, str]:
    """Reference -> checklist type, for the parts that get their own checklist (passives go to [board]).

    The tscircuit element type first (pin header, diode, LED, crystal), then the reference letters (J, D, Q, Y,
    ...: imported parts are all chips), then for chips: regulator (pins / family name), MCU (GPIO pin names), IC.
    """
    comps = _components(circuit)
    regulators = set(netcheck.find_regulators(design))
    out: dict[str, str] = {}
    for ref, part in design.parts.items():
        ftype = str(comps.get(ref, {}).get("ftype", ""))
        if kind := FTYPE_KINDS.get(ftype) or _prefix_kind(ref):
            if kind != "passive":
                out[ref] = kind
        elif ref in regulators:
            out[ref] = "regulator"
        elif part.kind == "chip":
            gpio = sum(bool(bringup.GPIO_NAME.match(p.name.upper())) for p in design.pins_of(ref))
            out[ref] = "mcu" if gpio >= MIN_MCU_GPIO else "ic"
    return out


def items(kinds: dict[str, str], checklists: dict[str, dict[str, str]], bottom: list[str] | None = None) -> list[Item]:
    """The checklist items: the ``[bottom]`` items for every part in ``bottom`` first (bottom-side chips, mirrored),
    then each part's type checklist, then the ``[board]`` items."""
    out = [
        Item(f"{ref}:{key}", ref, BOTTOM, question)
        for ref in sorted(bottom or [])
        for key, question in checklists.get(BOTTOM, BOTTOM_ITEMS).items()
    ]
    out += [
        Item(f"{ref}:{key}", ref, kind, question)
        for ref, kind in sorted(kinds.items(), key=lambda rk: (rk[1], rk[0]))
        for key, question in checklists.get(kind, {}).items()
    ]
    out += [Item(f"board:{key}", "", "board", q) for key, q in checklists.get("board", {}).items()]
    return out


def is_layout_item(item_id: str) -> bool:
    """An item about the placement: a placement change makes its answer stale (``review_layout_stale``)."""
    return item_id.split(":", 1)[-1] in LAYOUT_KEYS


def netlist_hash(design: Design) -> str:
    """What the review looked at: parts (type, value, part text) and every pin's net; layout changes don't count."""
    parts = sorted((p.ref, p.kind, p.value, p.text) for p in design.parts.values())
    pins = sorted((p.ref, p.number, p.name, p.net or "") for p in design.pins)
    return hashlib.sha256(json.dumps([parts, pins]).encode()).hexdigest()[:16]


def _part_numbers(comp: dict[str, Any]) -> tuple[str, str]:
    mpn = str(comp.get("manufacturer_part_number") or comp.get("mpn") or "")
    suppliers = comp.get("supplier_part_numbers") or {}
    lcsc = ", ".join(str(n) for n in suppliers.get("jlcpcb", []) or suppliers.get("lcsc", []))
    return mpn, lcsc


def parts_markdown(board: str, design: Design, circuit: list[dict[str, Any]], kinds: dict[str, str]) -> str:
    comps = _components(circuit)
    lines = [
        f"# {board}: parts",
        "",
        "Type = the checklist the part gets (`-` = covered by the board items). Value as on the BOM.",
        "",
        "| Ref | Type | Value / part | MPN | LCSC |",
        "|---|---|---|---|---|",
    ]
    for ref in sorted(design.parts):
        _part, comp = design.parts[ref], comps.get(ref, {})
        mpn, lcsc = _part_numbers(comp)
        value = next(
            (str(comp[k]) for k in ("display_resistance", "display_capacitance", "display_inductance") if comp.get(k)),
            str(comp.get("display_value") or mpn or comp.get("color") or comp.get("ftype", "")),
        )
        lines.append(f"| {ref} | {kinds.get(ref, '-')} | {value} | {mpn} | {lcsc} |")
    lines += ["", "## Pins", ""]
    for ref in sorted(design.parts):
        pins = ", ".join(f"{p.number} {p.name} = {p.net or '(no net)'}" for p in design.pins_of(ref))
        lines.append(f"- **{ref}**: {pins}")
    return "\n".join(lines) + "\n"


def answers_template(work_items: list[Item], given: dict[str, Answer] | None = None) -> str:
    """One ``[[item]]`` per checklist item; ``given``: answers carried over from the last review (filled in)."""
    out = [
        "# Answer every item: verdict = ok | issue | unknown; cite = datasheet + page (needed for ok / issue);",
        "# text = what you found (needed for issue / unknown); pin = optional, a pin of the part.",
        "",
    ]
    for item in work_items:
        where = item.ref or "the board"
        answer = (given or {}).get(item.id, Answer(item.id, ""))
        out += [
            "[[item]]",
            f"# {where} ({item.kind}): {item.question}",
            f"id = {json.dumps(item.id)}",
            *(f"{k} = {json.dumps(getattr(answer, k))}" for k in ITEM_KEYS[1:]),
            "",
        ]
    return "\n".join(out)


def prompt(board: str, review_dir: Path, n_items: int, *, image: bool = False, carried: int = 0) -> str:
    """The reviewer's task. ``image``: ``board.png`` is in the package; ``carried``: items pre-filled from the last
    review of the same netlist (only the layout changed since)."""
    rel = display_path(review_dir)
    picture = f"- `{IMAGE_NAME}`: the routed board, {IMAGE_LAYERS} (use `{LAYOUT_NAME}` for those)\n" if image else ""
    again = (
        f"\n{carried} item(s) are already filled in from the last review of this netlist (the netlist is unchanged; "
        "the placement changed or was not reviewed then). Answer the empty (layout) items; change a filled one only "
        "if `layout.md` shows it is wrong.\n"
        if carried
        else ""
    )
    return f"""# Review of board `{board}`

You are the reviewer, not the designer: check this board against the part datasheets and say where it is
wrong. Work only from the files in `{rel}/` and from datasheets you look up yourself (search by MPN / LCSC
number). Do not read `boards/`, the TSX source, `findings.toml` or the git history: the designer's reasoning
must not steer you. Derive every limit from the datasheet on your own.

Inputs:

- `parts.md`: every part with its checklist type, value, MPN / LCSC and each pin's net
- `netlist.txt`: tscircuit's readable netlist (the same connectivity, per net)
- `intent.toml`: the designer's assumption list (supply, rails, current budget), if any
- `sim/*.cir`: simulation decks, if any (check what their `.measure` lines prove)
- `checks.md`: what the deterministic checks already cover (don't repeat them)
- `{LAYOUT_NAME}`: the placement: bottom-side chips first (mirrored in x: pin rows as they land on the board),
  every chip's pins with board x / y, net and the parts serving each pin (distance, side, verdict), the
  regulator loops, every part's layer / centre / rotation
{picture}
Answer the layout items (`layout`, `bottom-mirroring`) from `{LAYOUT_NAME}` and the datasheet's layout
guidelines / example layout, pin by pin with distances in mm; a part on the wrong side of the chip is an issue.
{again}
Output: fill in `{rel}/{ANSWERS_NAME}` in place, all {n_items} items, keep the ids. Per item:

- `verdict = "ok"`: the board meets it; `cite` the datasheet value you checked against.
- `verdict = "issue"`: the board violates it or is risky; `cite` the source, `text` what is wrong and the fix.
- `verdict = "unknown"`: no datasheet / not decidable from the outputs; `text` says what is missing.
- `pin`: the pin concerned, if one (as in `parts.md`); leave "" otherwise. Items that don't apply
  (an inductor item on an LDO): `ok` with `cite = "n/a: <why>"`.

Be specific (values, pins, page numbers) and short. Finish by checking the file is valid TOML. Your
`issue` / `unknown` answers become open findings the human sees; nothing else reaches them.
"""


def checks_markdown() -> str:
    doc = netcheck.__doc__ or ""
    return "# Deterministic checks already run by `check`\n\n" + doc.split("\n\n", 1)[-1]


def review_dir(board: str) -> Path:
    return paths.work_dir(board) / "review"


def _circuit(board: str) -> list[dict[str, Any]]:
    path = paths.raw_dir(board) / f"{board}.circuit.json"
    if not path.exists():
        msg = f"{display_path(path)} not found: run `pcb build {board}` first"
        raise ReviewError(msg)
    data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def board_items(design: Design, circuit: list[dict[str, Any]]) -> tuple[dict[str, str], list[Item]]:
    """The board's part types and checklist items (bottom-side chips get the ``[bottom]`` items too)."""
    kinds = classify(design, circuit)
    return kinds, items(kinds, load_checklists(), layoutdoc.bottom_chips(circuit, design))


def carried_answers(board: str, design: Design, layout: str) -> dict[str, Answer]:
    """Answers of the last review that still hold: all but the layout items when the netlist is the same and only
    the placement changed (nothing when nothing changed: a new review was asked for, or the netlist changed)."""
    record = load_record(board)
    if record is None or record.hash != netlist_hash(design) or record.layout_hash == layout:
        return {}
    return {a.id: a for a in record.answers if not is_layout_item(a.id)}


def write_package(board: str) -> tuple[Path, list[Item]]:
    """The review package in ``out/<board>/.work/review/`` (replaced, except answers already given for the
    same netlist and placement). Returns the directory and the items to answer."""
    circuit = _circuit(board)
    design = netcheck.load_design(circuit)
    kinds, work_items = board_items(design, circuit)
    out = review_dir(board)
    answers = out / ANSWERS_NAME
    keep = answers.read_text(encoding="utf-8") if answers.exists() else None
    stamps = {out / "netlist.hash": netlist_hash(design), out / "layout.hash": layoutdoc.layout_hash(circuit)}
    same = all(f.exists() and f.read_text(encoding="utf-8").strip() == h for f, h in stamps.items())
    if out.exists():
        shutil.rmtree(out)
    (out / "sim").mkdir(parents=True)
    (out / "parts.md").write_text(parts_markdown(board, design, circuit, kinds), encoding="utf-8")
    (out / "checks.md").write_text(checks_markdown(), encoding="utf-8")
    netlist = paths.raw_dir(board) / f"{board}.netlist.txt"
    if netlist.exists():
        shutil.copy2(netlist, out / "netlist.txt")
    image = paths.preview_dir(board) / IMAGE_NAME
    if image.exists():
        shutil.copy2(image, out / IMAGE_NAME)
    pcb = paths.kicad_dir(board) / f"{board}.kicad_pcb"
    note = f"Board image `{IMAGE_NAME}`: {IMAGE_LAYERS}." if image.exists() else "No board image (run `check`)."
    layout_md = layoutdoc.markdown(board, circuit, kinds, placement.load_limits(board), pcb, note)
    (out / LAYOUT_NAME).write_text(layout_md, encoding="utf-8")
    if (spec := intent.intent_path(board)).exists():
        shutil.copy2(spec, out / "intent.toml")
    for deck in sim.deck_paths(board):
        shutil.copy2(deck, out / "sim" / deck.name)
    given = carried_answers(board, design, stamps[out / "layout.hash"])
    carried = sum(i.id in given for i in work_items)
    answers.write_text(keep if keep is not None and same else answers_template(work_items, given), encoding="utf-8")
    for f, h in stamps.items():
        f.write_text(h + "\n", encoding="utf-8")
    text = prompt(board, out, len(work_items), image=image.exists(), carried=carried)
    (out / "PROMPT.md").write_text(text, encoding="utf-8")
    return out, work_items


def reviewer_prompt(board: str) -> str:
    rel = display_path(review_dir(board))
    return f"Review PCB board `{board}`: read {rel}/PROMPT.md and follow it exactly."


def parse_answers(text: str, where: str = ANSWERS_NAME) -> list[Answer]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{where}: {exc}"
        raise ReviewError(msg) from exc
    entries = data.get("item", [])
    if not isinstance(entries, list):
        msg = f"{where}: use [[item]] tables"
        raise ReviewError(msg)
    out = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not entry.get("id"):
            msg = f"{where}: item #{i} needs an id"
            raise ReviewError(msg)
        if unknown := set(entry) - set(ITEM_KEYS):
            msg = f"{where}: {entry['id']}: unknown key(s) {sorted(unknown)}"
            raise ReviewError(msg)
        out.append(Answer(**{k: str(v).strip() for k, v in entry.items()}))
    return out


def _has_pin(design: Design, ref: str, pin: str) -> bool:
    names = {p.name.upper() for p in design.pins_of(ref)} | {p.number.upper() for p in design.pins_of(ref)}
    # a pin group by its common name counts too ("GND" for GND1..GND20, "VS" for VS_POS)
    return pin.upper() in names or any(n.startswith(pin.upper()) for n in names)


def drop_unknown_pins(answers: list[Answer], work_items: list[Item], design: Design) -> list[Answer]:
    """Answers citing a pin their part doesn't have: the pin goes into the text instead (the finding stays part-level).

    A datasheet pin the footprint merges or omits (bench-psu: ESP32-S3 GND pads 62-65 sit under pad 61) is still a
    finding to judge, not a reason to throw away the whole review.
    """
    refs = {i.id: i.ref for i in work_items}
    out = []
    for a in answers:
        ref = refs.get(a.id, "")
        if a.pin and ref and not _has_pin(design, ref, a.pin):
            a = replace(a, pin="", text=f"{a.text} (cites pin {a.pin}, which {ref} does not have)".strip())
        out.append(a)
    return out


def validate(answers: list[Answer], work_items: list[Item], design: Design) -> list[str]:
    """Problems with the reviewer's answers, one line each (empty = usable)."""
    wanted = {i.id: i for i in work_items}
    problems = []
    seen: set[str] = set()
    for a in answers:
        item = wanted.get(a.id)
        if item is None:
            problems.append(f"{a.id}: not a checklist item of this board")
            continue
        if a.id in seen:
            problems.append(f"{a.id}: answered twice")
        seen.add(a.id)
        if not a.verdict:
            problems.append(f"{a.id}: not answered")
            continue
        if a.verdict not in VERDICTS:
            problems.append(f"{a.id}: verdict must be one of {', '.join(VERDICTS)}, got {a.verdict!r}")
        if a.verdict in {"ok", "issue"} and not a.cite:
            problems.append(f"{a.id}: {a.verdict} needs a cite (datasheet + page, or 'n/a: <why>')")
        if a.verdict in {"issue", "unknown"} and not a.text:
            problems.append(f"{a.id}: {a.verdict} needs a text")
        if a.pin and item.ref and not _has_pin(design, item.ref, a.pin):
            problems.append(f"{a.id}: {item.ref} has no pin {a.pin!r}")
    problems += [f"{i}: not answered" for i in wanted if i not in seen]
    return problems


def to_findings(answers: list[Answer], work_items: list[Item]) -> list[findings.Finding]:
    refs = {i.id: i.ref for i in work_items}
    out = []
    for a in answers:
        if a.verdict == "ok":
            continue
        text = (
            a.text + (f" [{a.cite}]" if a.cite else "") + (f" {findings.UNDECIDED}" if a.verdict == "unknown" else "")
        )
        out.append(findings.Finding(f"{SOURCE}:{a.id}", refs.get(a.id, ""), a.pin, text, source=SOURCE))
    return out


@dataclass(frozen=True)
class Record:
    hash: str  # netlist_hash: every item
    date: str
    answers: list[Answer]
    layout_hash: str = ""  # layoutdoc.layout_hash: the layout items ("" = a record from before layout.md)


def record_path(board: str) -> Path:
    return paths.board_source(board).parent / RECORD_NAME


def dumps_record(record: Record) -> str:
    out = [
        "# Last reviewer-agent review (pcb review --merge); its issue / unknown answers are in findings.toml.",
        f"hash = {json.dumps(record.hash)}  # netlist the review saw (review.netlist_hash)",
        f"layout_hash = {json.dumps(record.layout_hash)}  # placement its layout items saw (layoutdoc.layout_hash)",
        f"date = {json.dumps(record.date)}",
        "",
    ]
    for a in record.answers:
        out += ["[[item]]", *(f"{k} = {json.dumps(getattr(a, k))}" for k in ITEM_KEYS), ""]
    return "\n".join(out)


def load_record(board: str) -> Record | None:
    path = record_path(board)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    answers = parse_answers(text, path.name)
    return Record(str(data.get("hash", "")), str(data.get("date", "")), answers, str(data.get("layout_hash", "")))


def merge(board: str, *, now: datetime | None = None) -> tuple[list[str], int]:
    """Validate the reviewer's answers and record them. Returns summary lines and the number of review findings."""
    circuit = _circuit(board)
    design = netcheck.load_design(circuit)
    _, work_items = board_items(design, circuit)
    answers_file = review_dir(board) / ANSWERS_NAME
    if not answers_file.exists():
        msg = f"no answers: run `pcb review {board}` and let the reviewer fill {display_path(answers_file)}"
        raise ReviewError(msg)
    answers = parse_answers(answers_file.read_text(encoding="utf-8"), answers_file.name)
    answers = drop_unknown_pins(answers, work_items, design)
    if problems := validate(answers, work_items, design):
        shown = "\n  ".join(problems[:40]) + (f"\n  ... {len(problems) - 40} more" if len(problems) > 40 else "")
        msg = f"{display_path(answers_file)}: {len(problems)} problem(s):\n  {shown}"
        raise ReviewError(msg)
    found = to_findings(answers, work_items)
    findings.save(board, findings.merge(findings.load(board), found, source=SOURCE))
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    record_path(board).write_text(
        dumps_record(Record(netlist_hash(design), stamp, answers, layoutdoc.layout_hash(circuit))), encoding="utf-8"
    )
    counts = {v: sum(a.verdict == v for a in answers) for v in VERDICTS}
    lines = [
        f"review: {len(answers)} item(s): {counts['ok']} ok, {counts['issue']} issue(s), {counts['unknown']} unknown",
        *(f"  {f.line()}" for f in found),
        f"recorded in {display_path(record_path(board))}; open findings in findings.toml",
    ]
    return lines, len(found)


def status(board: str, design: Design, layout: str | None = None) -> tuple[str, str | None]:
    """``check``'s summary line about the review, and the problem if it is missing or stale. ``layout``: the
    board's :func:`layoutdoc.layout_hash` (``None``: don't judge the placement). A netlist change makes the whole
    review stale (``review_stale``); a placement change only its layout items (``review_layout_stale``:
    ``pcb review`` then carries the other answers over)."""
    record = load_record(board)
    if record is None:
        return (
            "Review: none",
            f"review_missing: no reviewer-agent review yet (`pcb review {board} --run`, docs/REVIEW.md)",
        )
    if record.hash != netlist_hash(design):
        return f"Review: {record.date}, stale", (
            f"review_stale: the netlist changed since the review of {record.date} (`pcb review {board} --run`)"
        )
    if layout is not None and record.layout_hash != layout:
        why = "the placement changed since" if record.layout_hash else "no layout was reviewed in"
        return f"Review: {record.date}, layout items stale", (
            f"review_layout_stale: {why} the review of {record.date}; the layout items need a new look "
            f"(`pcb review {board} --run` keeps the other answers)"
        )
    issues = sum(a.verdict != "ok" for a in record.answers)
    return f"Review: {record.date}, {len(record.answers)} item(s), {issues} not ok (open ones are findings)", None
