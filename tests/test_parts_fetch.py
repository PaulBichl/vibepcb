from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

import pytest

from vibepcb.parts import fetch

if TYPE_CHECKING:
    from pathlib import Path

# answers in the documented shapes (no network in tests)
DIGIKEY = {
    "Product": {
        "ManufacturerProductNumber": "LM358DR",
        "Manufacturer": {"Name": "Texas Instruments"},
        "Description": {"ProductDescription": "IC OPAMP GP 2 CIRCUIT 8SOIC"},
        "DatasheetUrl": "//www.ti.com/lit/ds/symlink/lm358.pdf",
        "ProductStatus": {"Status": "Active"},
        "Parameters": [{"ParameterText": "Supplier Device Package", "ValueText": "8-SOIC"}],
        "ProductVariations": [{"DigiKeyProductNumber": "296-1395-1-ND"}],
    }
}
MOUSER = {
    "Errors": [],
    "SearchResults": {
        "NumberOfResult": 2,
        "Parts": [
            {
                "ManufacturerPartNumber": "LM358DR",
                "Manufacturer": "Texas Instruments",
                "Description": "Operational Amplifiers - Op Amps Dual",
                "DataSheetUrl": "https://www.ti.com/lit/ds/symlink/lm358.pdf",
                "LifecycleStatus": "Not Recommended for New Designs",
                "MouserPartNumber": "595-LM358DR",
                "ProductAttributes": [{"AttributeName": "Package / Case", "AttributeValue": "SOIC-8"}],
            },
            {"ManufacturerPartNumber": "LM358DRG4", "Manufacturer": "Texas Instruments"},
        ],
    },
}


class FakeHttp:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, request: urllib.request.Request) -> bytes:
        url = request.full_url
        self.calls.append(url)
        for key, answer in self.answers.items():
            if key in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        msg = "no route"
        raise urllib.error.URLError(msg)


@pytest.fixture(autouse=True)
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(fetch, "CACHE", tmp_path)
    for key in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET", "MOUSER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_digikey_answer() -> None:
    info = fetch.from_digikey(DIGIKEY, "lm358dr")
    assert info is not None
    assert (info.manufacturer, info.package, info.lifecycle) == ("Texas Instruments", "8-SOIC", "active")
    assert info.datasheet_url == "https://www.ti.com/lit/ds/symlink/lm358.pdf"
    assert info.distributor_numbers == {"digikey": "296-1395-1-ND"}
    assert fetch.from_digikey(DIGIKEY, "LM358") is None  # exact match only


def test_mouser_answer() -> None:
    info = fetch.from_mouser(MOUSER, "LM358DR")
    assert info is not None
    assert (info.lifecycle, info.package, info.source) == ("nrnd", "SOIC-8", "mouser")
    assert info.distributor_numbers == {"mouser": "595-LM358DR"}


def test_mouser_ambiguous() -> None:
    data = json.loads(json.dumps(MOUSER))
    data["SearchResults"]["Parts"][1] = dict(data["SearchResults"]["Parts"][0], Manufacturer="ON Semiconductor")
    with pytest.raises(fetch.AmbiguousPart, match="several manufacturers"):
        fetch.from_mouser(data, "LM358DR")
    info = fetch.from_mouser(data, "LM358DR", manufacturer="ON Semi")
    assert info is not None
    assert info.manufacturer == "ON Semiconductor"


def test_digikey_first_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    http = FakeHttp({"oauth2/token": {"access_token": "T", "expires_in": 600}, "productdetails": DIGIKEY})
    info = fetch.lookup("LM358DR", http=http, now=1000.0)
    assert info.source == "digikey"
    fetch.lookup("LM358DR", http=http, now=1100.0)  # token still valid: no second token call
    assert sum("oauth2" in c for c in http.calls) == 1
    fetch.lookup("LM358DR", http=http, now=2000.0)  # expired: a new token
    assert sum("oauth2" in c for c in http.calls) == 2
    token_file = fetch.CACHE / "digikey-token.json"
    assert "secret" not in token_file.read_text(encoding="utf-8")


def test_mouser_fallback_without_digikey_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    http = FakeHttp({"api.mouser.com": MOUSER})
    assert fetch.lookup("LM358DR", http=http).source == "mouser"
    assert not any("digikey" in c for c in http.calls)


def test_offline_uses_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    fetch.lookup("LM358DR", http=FakeHttp({"api.mouser.com": MOUSER}))
    offline = FakeHttp({"api.mouser.com": urllib.error.URLError("down")})
    assert fetch.lookup("LM358DR", http=offline).source == "mouser"


def test_nothing_found() -> None:
    with pytest.raises(fetch.FetchError, match=r"no credentials.*--datasheet"):
        fetch.lookup("LM358DR", http=FakeHttp({}))


def test_datasheet_download(tmp_path: Path) -> None:
    info = fetch.PartInfo("X1", "M", "", "", "https://example.com/x.pdf", "active", "manual")
    path = fetch.download_datasheet(info, http=FakeHttp({"example.com": b"%PDF-1.7 ..."}))
    assert path.read_bytes().startswith(b"%PDF")
    html = fetch.PartInfo("X2", "M", "", "", "https://example.com/y", "active", "manual")
    with pytest.raises(fetch.FetchError, match="by hand"):
        fetch.download_datasheet(html, http=FakeHttp({"example.com": b"<html>"}))
