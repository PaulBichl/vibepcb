"""``pcb part fetch``: an MPN at DigiKey / Mouser -> manufacturer, package, lifecycle, datasheet (cached).

DigiKey first (Product Information API v4, OAuth2 client credentials: ``DIGIKEY_CLIENT_ID`` /
``DIGIKEY_CLIENT_SECRET``), Mouser when DigiKey has no exact match or no credentials (Search API,
``MOUSER_API_KEY``). Exact MPN matches only; several manufacturers -> :class:`AmbiguousPart` (pick one with
``manufacturer``). Responses and the datasheet are cached in ``~/.cache/vibepcb/parts/<mpn>/``; offline, the
cached answer is used. Secrets are read from the environment only, never written anywhere.

Response field names (one place, :func:`from_digikey` / :func:`from_mouser`): DigiKey v4 ``productdetails``
(``Product.ManufacturerProductNumber``, ``Manufacturer.Name``, ``Description``, ``DatasheetUrl``,
``ProductStatus.Status``, ``Parameters[].ParameterText/ValueText``), Mouser v1 ``search/partnumber``
(``SearchResults.Parts[]``: ``ManufacturerPartNumber``, ``Manufacturer``, ``Description``, ``DataSheetUrl``,
``LifecycleStatus``, ``MouserPartNumber``). Taken from the vendors' API documentation; check them against a
real answer (cached as JSON) the first time keys are available.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibepcb import symlib

if TYPE_CHECKING:
    from collections.abc import Callable

CACHE = symlib.CACHE / "parts"
DIGIKEY_TOKEN = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_DETAILS = "https://api.digikey.com/products/v4/search/{}/productdetails"
MOUSER_SEARCH = "https://api.mouser.com/api/v1/search/partnumber?apiKey={}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
TIMEOUT_S = 30
LIFECYCLE = {
    "active": "active",
    "new product": "active",
    "new at mouser": "active",
    "not recommended for new designs": "nrnd",
    "nrnd": "nrnd",
    "last time buy": "nrnd",
    "obsolete": "obsolete",
    "discontinued": "obsolete",
    "end of life": "obsolete",
}

type Http = Callable[[urllib.request.Request], bytes]


class FetchError(RuntimeError):
    """No usable answer (no credentials, no match, network down without a cache)."""


class AmbiguousPart(FetchError):  # noqa: N818 - reads better at the call site
    def __init__(self, mpn: str, manufacturers: list[str]) -> None:
        self.manufacturers = manufacturers
        super().__init__(
            f"{mpn} is listed by several manufacturers: {', '.join(manufacturers)}; pick one with --manufacturer"
        )


@dataclass
class PartInfo:
    mpn: str
    manufacturer: str
    description: str
    package: str
    datasheet_url: str
    lifecycle: str  # active | nrnd | obsolete | unknown
    source: str  # digikey | mouser | manual
    distributor_numbers: dict[str, str] = field(default_factory=dict)
    datasheet: str = ""  # local path once downloaded

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _lifecycle(text: str | None) -> str:
    return LIFECYCLE.get((text or "").strip().lower(), "unknown")


def _same(a: str, b: str) -> bool:
    return re.sub(r"[^A-Z0-9]", "", a.upper()) == re.sub(r"[^A-Z0-9]", "", b.upper())


def _https(url: str) -> str:
    return "https:" + url if url.startswith("//") else url


def from_digikey(data: dict[str, Any], mpn: str) -> PartInfo | None:
    """PartInfo from a DigiKey v4 ``productdetails`` answer, None if it isn't the MPN."""
    product = data.get("Product") or {}
    listed = str(product.get("ManufacturerProductNumber", ""))
    if not listed or not _same(listed, mpn):
        return None
    params = {str(p.get("ParameterText")): str(p.get("ValueText")) for p in product.get("Parameters") or []}
    package = params.get("Supplier Device Package") or params.get("Package / Case") or ""
    description = product.get("Description") or {}
    variations = [v for v in product.get("ProductVariations") or [] if v.get("DigiKeyProductNumber")]
    numbers = {"digikey": str(variations[0]["DigiKeyProductNumber"])} if variations else {}
    return PartInfo(
        mpn=listed,
        manufacturer=str((product.get("Manufacturer") or {}).get("Name", "")),
        description=str(description.get("ProductDescription") or description.get("DetailedDescription") or ""),
        package=package,
        datasheet_url=_https(str(product.get("DatasheetUrl") or "")),
        lifecycle=_lifecycle((product.get("ProductStatus") or {}).get("Status")),
        source="digikey",
        distributor_numbers=numbers,
    )


def from_mouser(data: dict[str, Any], mpn: str, manufacturer: str | None = None) -> PartInfo | None:
    """PartInfo from a Mouser ``search/partnumber`` answer: the exact match (raises when several makers list it)."""
    if errors := data.get("Errors"):
        msg = f"Mouser: {'; '.join(str(e.get('Message', e)) for e in errors)}"
        raise FetchError(msg)
    parts = [
        p
        for p in (data.get("SearchResults") or {}).get("Parts") or []
        if _same(str(p.get("ManufacturerPartNumber", "")), mpn)
    ]
    if manufacturer:
        parts = [p for p in parts if manufacturer.lower() in str(p.get("Manufacturer", "")).lower()]
    makers = sorted({str(p.get("Manufacturer", "")) for p in parts})
    if len(makers) > 1:
        raise AmbiguousPart(mpn, makers)
    if not parts:
        return None
    p = parts[0]
    package = ""
    for attr in p.get("ProductAttributes") or []:
        if str(attr.get("AttributeName", "")).lower() in ("package / case", "package/case"):
            package = str(attr.get("AttributeValue", ""))
    return PartInfo(
        mpn=str(p["ManufacturerPartNumber"]),
        manufacturer=makers[0],
        description=str(p.get("Description", "")),
        package=package,
        datasheet_url=_https(str(p.get("DataSheetUrl") or "")),
        lifecycle=_lifecycle(p.get("LifecycleStatus") or "active"),
        source="mouser",
        distributor_numbers={"mouser": str(p["MouserPartNumber"])} if p.get("MouserPartNumber") else {},
    )


def _urlopen(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        data: bytes = response.read()
        return data


def cache_dir(mpn: str) -> Path:
    from vibepcb.parts.spec import file_name

    return CACHE / file_name(mpn)


def _cached(path: Path) -> dict[str, Any] | None:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data


def _token(http: Http, now: float) -> str:
    client_id = os.environ.get("DIGIKEY_CLIENT_ID", "")
    secret = os.environ.get("DIGIKEY_CLIENT_SECRET", "")
    token_file = CACHE / "digikey-token.json"  # the access token only (expires), never the client secret
    cached = _cached(token_file)
    if cached and cached.get("client_id") == client_id and cached.get("expires", 0) > now + 60:
        return str(cached["access_token"])
    body = urllib.parse.urlencode(
        {"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"}
    ).encode()
    answer = json.loads(http(urllib.request.Request(DIGIKEY_TOKEN, data=body, method="POST")))
    token = str(answer["access_token"])
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {"client_id": client_id, "access_token": token, "expires": now + float(answer.get("expires_in", 600))}
        ),
        encoding="utf-8",
    )
    token_file.chmod(0o600)
    return token


def _digikey(mpn: str, http: Http, now: float) -> dict[str, Any]:
    token = _token(http, now)
    request = urllib.request.Request(
        DIGIKEY_DETAILS.format(urllib.parse.quote(mpn, safe="")),
        headers={
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": os.environ.get("DIGIKEY_CLIENT_ID", ""),
            "Accept": "application/json",
        },
    )
    data: dict[str, Any] = json.loads(http(request))
    return data


def _mouser(mpn: str, http: Http) -> dict[str, Any]:
    body = json.dumps({"SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": "Exact"}}).encode()
    request = urllib.request.Request(
        MOUSER_SEARCH.format(os.environ.get("MOUSER_API_KEY", "")),
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    data: dict[str, Any] = json.loads(http(request))
    return data


def credentials() -> dict[str, bool]:
    """Which distributor credentials are set (never their values)."""
    return {
        "digikey": bool(os.environ.get("DIGIKEY_CLIENT_ID") and os.environ.get("DIGIKEY_CLIENT_SECRET")),
        "mouser": bool(os.environ.get("MOUSER_API_KEY")),
    }


def lookup(mpn: str, *, manufacturer: str | None = None, http: Http = _urlopen, now: float | None = None) -> PartInfo:
    """The part at DigiKey, else Mouser; the cached answer when the network fails."""
    now = time.time() if now is None else now
    have = credentials()
    folder = cache_dir(mpn)
    problems: list[str] = []
    for source, ask, parse in (
        ("digikey", lambda: _digikey(mpn, http, now), lambda d: from_digikey(d, mpn)),
        ("mouser", lambda: _mouser(mpn, http), lambda d: from_mouser(d, mpn, manufacturer)),
    ):
        cache = folder / f"{source}.json"
        if not have[source]:
            problems.append(f"{source}: no credentials")
            data = _cached(cache)
        else:
            try:
                data = ask()
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
                problems.append(f"{source}: {e}")
                data = _cached(cache)
            else:
                folder.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
        if data is None:
            continue
        info = parse(data)
        if info is not None and (not manufacturer or manufacturer.lower() in info.manufacturer.lower()):
            return info
        problems.append(f"{source}: no exact match")
    msg = f"{mpn}: not found ({'; '.join(problems)}); give the datasheet by hand: --datasheet <url|file>"
    raise FetchError(msg)


def download_datasheet(info: PartInfo, *, http: Http = _urlopen) -> Path:
    """The datasheet PDF in the cache (downloaded once); ``datasheet.txt`` next to it when pdftotext exists."""
    target = cache_dir(info.mpn) / "datasheet.pdf"
    if not target.is_file():
        url = info.datasheet_url
        if not url:
            msg = f"{info.mpn}: no datasheet URL; save the PDF as {target}"
            raise FetchError(msg)
        if Path(url).is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(url, target)
        else:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"})
            try:
                data = http(request)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                msg = f"datasheet download failed ({e}): download {url} by hand and save it as {target}"
                raise FetchError(msg) from e
            if not data.startswith(b"%PDF"):
                msg = f"{url} did not return a PDF (a web page?): download it by hand and save it as {target}"
                raise FetchError(msg)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    if (pdftotext := shutil.which("pdftotext")) and not target.with_suffix(".txt").is_file():
        subprocess.run([pdftotext, "-layout", str(target), str(target.with_suffix(".txt"))], check=False)
    return target
