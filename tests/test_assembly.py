from __future__ import annotations

import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from vibepcb.output import assembly, assembly_draw

DATA = Path(__file__).parent / "data"
PCB = DATA / "assembly-mini.kicad_pcb"


@pytest.fixture
def mini() -> assembly.Assembly:
    return assembly.collect(PCB, PCB.with_suffix(".kicad_sch"))


def test_collect_board_data(mini: assembly.Assembly) -> None:
    assert (mini.revision, mini.date, mini.copper_layers) == ("B", "2026-11-01", 2)
    assert (mini.width, mini.height) == (22, 20)
    assert [h.diameter for h in mini.holes] == [3.2]
    assert {p.ref: p.side for p in mini.parts} == {
        "C1": "bottom", "D1": "top", "J1": "top", "R1": "top", "R2": "bottom", "TP1": "bottom", "U1": "bottom",
    }  # fmt: skip
    assert [p.ref for p in mini.testpoints] == ["TP1"]
    assert mini.by_ref["J1"].tht
    assert mini.by_ref["U1"].mpn == "TLV9062IPWR"


def test_polarity_marks(mini: assembly.Assembly) -> None:
    marks = {p.ref: (p.mark.kind, p.mark.pad) if p.mark else None for p in mini.parts}
    assert marks["D1"] == ("cathode", "2")  # tscircuit LED footprint: pad 1 = anode
    assert marks["C1"] == ("plus", "1")  # schematic pin name "+"
    assert marks["U1"] == ("pin1", "1")
    assert marks["R1"] is None
    assert "LED" in mini.by_ref["D1"].note
    assert mini.warnings == []


def test_unknown_diode_polarity_is_a_warning() -> None:
    pads = [assembly.Pad("1", 0, 0, ((0, 0),) * 4, round=False, tht=False),
            assembly.Pad("2", 1, 0, ((1, 0),) * 4, round=False, tht=False)]  # fmt: skip
    mark, note = assembly._mark("D7", "lib:SOD-123", pads, {"1": "1", "2": "2"}, {})
    assert mark is not None
    assert mark.kind == "pin1"
    assert "NOT found" in note


def test_assembly_order(mini: assembly.Assembly) -> None:
    steps = [(s.side, s.refs, s.category) for s in mini.steps]
    # the side with the fine-pitch IC first, small before large, through-hole last; a BOM line per side
    assert steps == [
        ("bottom", ("U1",), "fine-pitch / leadless ICs"),
        ("bottom", ("R2",), "small SMD parts"),
        ("bottom", ("C1",), "large SMD parts"),
        ("top", ("D1",), "small SMD parts"),
        ("top", ("R1",), "small SMD parts"),
        ("top", ("J1",), "through-hole connectors / switches"),
    ]
    assert [s.number for s in mini.steps] == list(range(1, 7))
    assert "pin 1" in mini.steps[0].note
    assert "fine pitch" in mini.steps[0].note
    assert "through-hole" in mini.steps[-1].note


def test_bom_csv_groups(tmp_path: Path) -> None:
    bom = tmp_path / "bom.csv"
    bom.write_text(
        'Comment,Designator,Footprint,LCSC Part #,MPN,Qty\n330R,"R1,R2",resistor_res0603,C23138,,2\n'
        "TLV9062,U1,TSSOP-8,C1,TLV9062IPWR,1\n100uF,C1,ELCAP,,,1\nLED,D1,led,,,1\nheader,J1,hdr,,,1\n",
        encoding="utf-8",
    )
    a = assembly.collect(PCB, PCB.with_suffix(".kicad_sch"), bom_csv=bom)
    r2 = next(s for s in a.steps if s.refs == ("R2",))
    assert (r2.value, r2.footprint, r2.lcsc) == ("330R", "resistor_res0603", "C23138")


def test_bottom_view_is_mirrored_text_is_not(mini: assembly.Assembly) -> None:
    area = (0.0, 0.0, 220.0, 200.0)
    top, bottom = (
        assembly_draw.fit_view("top", mini.outline, area),
        assembly_draw.fit_view("bottom", mini.outline, area),
    )
    x0, y0, x1, _ = mini.outline
    assert top.page(x0, y0)[0] == pytest.approx(bottom.page(x1, y0)[0])  # the board's left edge is on the right
    assert bottom.page(x0, y0)[0] > bottom.page(x1, y0)[0]
    svg = assembly_draw.draw_side(mini, bottom, None, area).svg
    assert "matrix(-" in svg  # the base drawing is flipped ...
    for text in re.findall(r"<text[^>]*>", svg):  # ... the reference text is not
        assert "matrix" not in text
        assert "scale(-" not in text


def test_labels_never_overlap() -> None:
    rng = random.Random(4)
    boxes = []
    for i in range(120):  # a dense field of 0603-sized parts
        x, y = rng.uniform(10, 90), rng.uniform(10, 60)
        boxes.append((f"R{i}", f"R{i}", (x, y, x + 2.4, y + 1.2)))
    obstacles = [(b, k) for k, _, b in boxes]
    labels = assembly_draw.place_labels(boxes, obstacles, (0, 0, 120, 80), size=2.0)
    assert [lb.ref for lb in labels] == [k for k, _, _ in boxes]
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            assert not assembly_draw._overlap(a.box, b.box), (a, b)
    assert any(lb.leader for lb in labels)


def test_html_is_self_contained(mini: assembly.Assembly) -> None:
    page = assembly_draw.render_html(mini, None)
    assert not re.search(r'(src|href)="https?:', page)
    assert "<script src" not in page
    assert "@import" not in page
    assert page.count('<svg class="board"') == 2
    assert "localStorage" in page
    assert "try {" in page
    assert "prefers-color-scheme: dark" in page
    assert 'data-ref="U1"' in page


def test_pdf_pages(mini: assembly.Assembly) -> None:
    pages = assembly_draw.pdf_pages(mini, None)
    # title, bottom + top drawing, 6 steps at 2 per page
    assert len(pages) == 1 + 2 + 3
    assert "BOTTOM SIDE - seen from below" in pages[1]
    assert all(p.startswith("<svg") and "page " in p for p in pages)


@pytest.mark.skipif(shutil.which("rsvg-convert") is None, reason="needs rsvg-convert")
def test_write_pdf(mini: assembly.Assembly, tmp_path: Path) -> None:
    pdf = assembly_draw.write_pdf(mini, None, tmp_path / "mini-assembly.pdf", tmp_path / "pages")
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF")
    if pdfinfo := shutil.which("pdfinfo"):
        info = subprocess.run([pdfinfo, str(pdf)], capture_output=True, text=True, check=True).stdout
        assert re.search(r"Pages:\s+6\b", info)
        assert "A4" in info


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_kicad_base(tmp_path: Path) -> None:
    base = assembly_draw.kicad_base(PCB)
    assert base is not None
    assert "__INK__" in base.pads["bottom"]
    assert "__EDGEW__" in base.edge
    files = assembly_draw.write(PCB, tmp_path, tmp_path / "work")
    assert [f.name for f in files] == ["assembly-mini-assembly.pdf", "assembly-mini-assembly.html"]
