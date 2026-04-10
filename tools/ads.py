"""Weekly ad fetchers driven by data/stores.json."""

from __future__ import annotations

import io
import json
import logging
import re
from html.parser import HTMLParser
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Simple price pattern: $X.XX or $X
_PRICE_RE = re.compile(r"\$\d+(?:\.\d{1,2})?(?:/\w+)?(?:\s+save\s+\$[\d.]+)?", re.I)


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Extracts visible text from HTML, skipping script/style blocks."""

    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)


def _extract_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return "\n".join(p.text_parts)


def _extract_json_blob(html: str, marker: str) -> Any | None:
    """Find JSON embedded in a script tag identified by a JS variable marker."""
    pattern = re.compile(re.escape(marker) + r"\s*=\s*(\{.*?\});", re.S)
    m = pattern.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Also try Next.js __NEXT_DATA__
    nd = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(\{.*?\})</script>', html, re.S)
    if nd:
        try:
            return json.loads(nd.group(1))
        except json.JSONDecodeError:
            pass
    return None


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages)
    except ImportError:
        logger.warning("pypdf not installed; cannot extract PDF text")
        return ""
    except Exception as e:
        logger.warning("PDF extraction failed: %s", e)
        return ""


def _text_to_sale_items(text: str, source: str, category: str = "general") -> list[dict]:
    """
    Best-effort: return lines that contain a price as SaleItem dicts.
    Falls back to a single raw-text item if nothing is found.
    """
    items: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if _PRICE_RE.search(line) and 3 < len(line) < 200:
            price_match = _PRICE_RE.search(line)
            detail = price_match.group(0) if price_match else ""
            item_name = line[: price_match.start()].strip(" ,-") if price_match else line
            if not item_name:
                continue
            items.append(
                {
                    "source": source,
                    "item": item_name[:120],
                    "detail": detail,
                    "category": category,
                }
            )

    if not items and text.strip():
        # Return a single entry with the full text so the orchestrator can parse it
        items.append(
            {
                "source": source,
                "item": "weekly ad text",
                "detail": text[:3000],
                "category": "raw_text",
            }
        )
    return items


# ---------------------------------------------------------------------------
# PDF URL extraction
# ---------------------------------------------------------------------------

def _find_pdf_url(html: str, base_url: str) -> str | None:
    """Find the first PDF href in the page."""
    matches = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I)
    if not matches:
        # Try data-src or src attributes that end in .pdf
        matches = re.findall(r'(?:src|data-src)=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I)
    if not matches:
        return None
    href = matches[0]
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    # Relative URL — combine with base
    from urllib.parse import urljoin
    return urljoin(base_url, href)


# ---------------------------------------------------------------------------
# Store-specific fetchers
# ---------------------------------------------------------------------------

async def _fetch_earth_fare(client: httpx.AsyncClient, store: dict) -> list[dict]:
    source = store["key"]
    landing_url = store["weekly_ad_url"]

    resp = await client.get(landing_url, headers=_HEADERS, follow_redirects=True)
    resp.raise_for_status()
    pdf_url = _find_pdf_url(resp.text, str(resp.url))
    if not pdf_url:
        logger.warning("earth_fare: no PDF link found on landing page")
        return [{"source": source, "error": "pdf_url_not_found", "items": []}]

    pdf_resp = await client.get(pdf_url, headers=_HEADERS, follow_redirects=True)
    pdf_resp.raise_for_status()
    text = _extract_pdf_text(pdf_resp.content)
    return _text_to_sale_items(text, source, "grocery")


async def _fetch_whole_foods(client: httpx.AsyncClient, store: dict) -> list[dict]:
    source = store["key"]
    url = store["weekly_ad_url"]

    resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    # Try to find embedded JSON (Next.js __NEXT_DATA__ or similar)
    data = _extract_json_blob(html, "window.__NEXT_DATA__")
    if data:
        # Walk the props tree looking for sale/deal arrays
        text_parts: list[str] = []
        _walk_json_for_text(data, text_parts, depth=0, max_depth=10)
        combined = "\n".join(text_parts)
        items = _text_to_sale_items(combined, source)
        if items and items[0].get("category") != "raw_text":
            return items

    # Fallback: extract visible text and look for prices
    text = _extract_text(html)
    return _text_to_sale_items(text, source)


def _walk_json_for_text(obj: Any, out: list[str], depth: int, max_depth: int) -> None:
    """Recursively extract string leaves that look like sale text."""
    if depth > max_depth:
        return
    if isinstance(obj, str):
        if _PRICE_RE.search(obj) or len(obj) > 4:
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_json_for_text(v, out, depth + 1, max_depth)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json_for_text(v, out, depth + 1, max_depth)


async def _fetch_myweeklyads(client: httpx.AsyncClient, store: dict) -> list[dict]:
    """Generic handler for myweeklyads.net aggregator pages."""
    source = store["key"]
    url = store["weekly_ad_url"]

    resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
    resp.raise_for_status()
    text = _extract_text(resp.text)
    return _text_to_sale_items(text, source)


async def _fetch_generic(client: httpx.AsyncClient, store: dict) -> list[dict]:
    source = store["key"]
    url = store["weekly_ad_url"]

    resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        text = _extract_pdf_text(resp.content)
    else:
        text = _extract_text(resp.text)

    return _text_to_sale_items(text, source)


# Map store keys to their fetcher functions
_FETCHERS = {
    "earth_fare": _fetch_earth_fare,
    "whole_foods": _fetch_whole_foods,
    "fresh_market": _fetch_myweeklyads,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_weekly_ads(store_key: str, stores: list[dict]) -> list[dict] | dict:
    """
    Fetch current sale items for the given store key.
    Returns a list of SaleItem dicts, or an error dict on failure.
    """
    store = next((s for s in stores if s["key"] == store_key), None)
    if store is None:
        return {"source": store_key, "error": f"unknown store key: {store_key!r}", "items": []}

    fetcher = _FETCHERS.get(store_key, _fetch_generic)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            return await fetcher(client, store)
    except httpx.TimeoutException:
        logger.warning("%s: request timed out", store_key)
        return {"source": store_key, "error": "timeout", "items": []}
    except httpx.HTTPStatusError as e:
        logger.warning("%s: HTTP %s", store_key, e.response.status_code)
        return {"source": store_key, "error": f"http_{e.response.status_code}", "items": []}
    except Exception as e:
        logger.warning("%s: fetch failed: %s", store_key, e)
        return {"source": store_key, "error": str(e), "items": []}
