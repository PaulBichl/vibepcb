from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibepcb import config, cost

if TYPE_CHECKING:
    from pathlib import Path

RESISTOR = cost.Offer("C25804", "0603WAF1002T5E", ((50, 0.0022), (500, 0.0015)), 50, 50, 10**6)
CHIP = cost.Offer("C841384", "LMR33630ADDAR", ((1, 0.70), (10, 0.62)), 1, 1, 3)
OFFERS = {o.lcsc: o for o in (RESISTOR, CHIP)}
PCB = """(kicad_pcb (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal))
  (gr_rect (start 10 20) (end 100 80) (layer "Edge.Cuts"))
  (gr_line (start 0 0) (end 500 0) (layer "F.SilkS")))
"""
BOM = """"Qty","Designator","Value","Footprint","MPN","LCSC Part #"
"3","R1,R2,R3","10k","tscircuit:resistor_res0603","","C25804"
"1","R9","10k","tscircuit:resistor_res0402","","C25804"
"2","U1,U2","LMR33630ADDAR","LMR33630ADDAR","LMR33630ADDAR","C841384"
"1","DISP1","OLED","oled","","C999999"
"1","J9","header","pinheader","",""
"""


def test_parse_offer_reads_lcsc_terms() -> None:
    data = {"result": {"productCode": "C25804", "productModel": "X", "minBuyNumber": 50, "split": 50,
                       "stockNumber": 7, "productPriceList": [{"ladder": 500, "usdPrice": 0.0015},
                                                             {"ladder": 50, "usdPrice": 0.0022}]}}  # fmt: skip
    offer = cost.parse_offer(data)
    assert offer == cost.Offer("C25804", "X", ((50, 0.0022), (500, 0.0015)), 50, 50, 7)
    assert cost.parse_offer({"result": None}) is None


@pytest.mark.parametrize(("need", "buy"), [(1, 50), (50, 50), (51, 100), (120, 150)])
def test_buy_quantity_meets_minimum_and_multiple(need: int, buy: int) -> None:
    assert cost.buy_quantity(need, RESISTOR) == buy


def test_unit_price_takes_highest_ladder_step_reached() -> None:
    assert cost.unit_price(50, RESISTOR) == 0.0022
    assert cost.unit_price(499, RESISTOR) == 0.0022
    assert cost.unit_price(500, RESISTOR) == 0.0015


def test_price_lines_merges_lcsc_numbers_and_flags_unpriced(tmp_path: Path) -> None:
    bom = tmp_path / "bom.csv"
    bom.write_text(BOM, encoding="utf-8")
    lines = cost.price_lines(cost.read_bom(bom), qty=5, spare=0.0, lookup=OFFERS.get)
    by = {line.lcsc: line for line in lines}
    assert len(lines) == 4
    assert (by["C25804"].per_board, by["C25804"].need, by["C25804"].buy) == (4, 20, 50)
    assert by["C25804"].refs == "R1,R2,R3,R9"
    assert by["C841384"].buy == 10
    assert by["C841384"].unit_usd == 0.62
    assert by["C841384"].short  # stock 3 < 10
    assert by["C999999"].offer is None
    assert by[""].offer is None


def test_spare_raises_need() -> None:
    (line,) = cost.price_lines([("C841384", "x", "U1", 2)], qty=5, spare=0.1, lookup=OFFERS.get)
    assert line.need == 11


def test_estimate_and_render(tmp_path: Path) -> None:
    pcb, bom = tmp_path / "b.kicad_pcb", tmp_path / "bom.csv"
    pcb.write_text(PCB, encoding="utf-8")
    bom.write_text(BOM, encoding="utf-8")
    est = cost.estimate(pcb, bom, {}, lookup=OFFERS.get)
    assert (est.qty, est.layers, est.size_mm) == (5, 4, (90.0, 60.0))
    assert est.pcb_source.startswith("estimate")
    assert est.total_usd == pytest.approx(50 * 0.0022 + 10 * 0.62 + 9.55)
    quoted = cost.estimate(pcb, bom, {"pcb_usd": 30.0, "qty": 10}, lookup=OFFERS.get)
    assert quoted.pcb_usd == 30.0
    text = cost.render("b", quoted)
    assert "no LCSC price" in text
    assert "no LCSC number" in text
    assert "DISP1" in text
    assert "jlcpcb.com" not in text.splitlines()[-1]  # a quote needs no reminder


def test_pcb_estimate_unknown_outside_table() -> None:
    assert cost.pcb_estimate(4, (90.0, 60.0), 5)[0] == 9.55
    assert cost.pcb_estimate(4, (150.0, 60.0), 5)[0] is None
    assert cost.pcb_estimate(6, (50.0, 50.0), 5)[0] is None


@pytest.mark.parametrize("section", [{"qty": 0}, {"qty": 2.5}, {"spare": -1}, {"price": 1}])
def test_config_rejects_bad_cost(section: dict[str, object]) -> None:
    with pytest.raises(config.ConfigError):
        config.validate({"cost": section})


def test_config_accepts_cost() -> None:
    config.validate({"cost": {"qty": 10, "spare": 0.1, "pcb_usd": 31.4}})
