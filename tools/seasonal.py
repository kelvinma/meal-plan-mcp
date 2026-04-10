"""Seasonal produce report — local farmers market + USDA terminal market data."""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import date, datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# USDA AMS terminal market and retail reports — Southeast-relevant
_USDA_REPORTS = [
    # National retail fruit/veg report (weekly, nationwide)
    "https://ams.usda.gov/mnreports/fvwretail.pdf",
    # Southeast terminal market (Atlanta)
    "https://ams.usda.gov/mnreports/fvmatlanta.pdf",
]

# Produce keywords for highlight extraction
_PRODUCE_KEYWORDS = [
    "asparagus", "artichoke", "arugula", "beet", "broccoli", "cabbage",
    "carrot", "cauliflower", "celery", "chard", "collard", "cucumber",
    "eggplant", "endive", "fennel", "garlic", "kale", "leek", "lettuce",
    "mushroom", "onion", "pea", "pepper", "potato", "radish", "rhubarb",
    "scallion", "spinach", "squash", "strawberry", "tomato", "turnip",
    "zucchini", "apple", "blueberry", "cantaloupe", "cherry", "corn",
    "fig", "grape", "melon", "peach", "pear", "plum", "raspberry",
    "sweet potato", "watermelon", "bok choy", "snap pea", "spring onion",
    "kohlrabi", "purslane", "sorrel", "fava bean", "green bean",
]


def _current_season(d: date) -> str:
    m = d.month
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    if m in (9, 10, 11):
        return "fall"
    return "winter"


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        logger.warning("pypdf not installed; cannot extract PDF text")
        return ""
    except Exception as e:
        logger.warning("PDF text extraction failed: %s", e)
        return ""


def _extract_visible_text(html: str) -> str:
    """Strip HTML tags and return visible text."""
    # Remove script/style blocks
    cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    # Remove remaining tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _extract_highlights(text: str) -> list[dict]:
    """Find produce mentions in text and return as highlight dicts."""
    text_lower = text.lower()
    seen: set[str] = set()
    highlights: list[dict] = []

    for keyword in _PRODUCE_KEYWORDS:
        if keyword in text_lower and keyword not in seen:
            seen.add(keyword)
            # Try to grab a small context window around the mention
            idx = text_lower.find(keyword)
            snippet = text[max(0, idx - 30) : idx + len(keyword) + 60].strip()
            # Clean up snippet
            snippet = re.sub(r"\s+", " ", snippet)
            highlights.append({"item": keyword, "notes": snippet[:120]})

    return highlights


def _pick_chef_item(highlights: list[dict], season: str) -> str:
    if not highlights:
        return f"Check local markets for peak {season} produce this week."
    top = highlights[0]
    return f"{top['item'].capitalize()} is available locally — a strong anchor for this week's menu."


async def _fetch_url(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        resp = await client.get(url, headers=_HEADERS, follow_redirects=True, timeout=15.0)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.warning("fetch %s failed: %s", url, e)
        return None


async def get_seasonal_report(stores: list[dict]) -> dict:
    """
    Returns a seasonal produce report combining local farmers market data
    and USDA terminal market data.
    """
    location = os.getenv("LOCATION_CITY", "your area")
    today = date.today()
    season = _current_season(today)

    # Resolve local report URL: SEASONAL_REPORT_URL env or wnc_farmers_market entry
    local_url = os.getenv("SEASONAL_REPORT_URL", "")
    if not local_url:
        market_entry = next((s for s in stores if s["key"] == "wnc_farmers_market"), None)
        if market_entry:
            local_url = market_entry["weekly_ad_url"]

    all_highlights: list[dict] = []
    sources_used: list[str] = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Local farmers market
        if local_url:
            content = await _fetch_url(client, local_url)
            if content:
                text = (
                    _extract_pdf_text(content)
                    if local_url.lower().endswith(".pdf")
                    else _extract_visible_text(content.decode("utf-8", errors="ignore"))
                )
                local_highlights = _extract_highlights(text)
                if local_highlights:
                    all_highlights.extend(local_highlights)
                    sources_used.append("local_market")

        # USDA reports
        for usda_url in _USDA_REPORTS:
            content = await _fetch_url(client, usda_url)
            if content:
                text = _extract_pdf_text(content)
                usda_highlights = _extract_highlights(text)
                for h in usda_highlights:
                    # Deduplicate by item name
                    if not any(x["item"] == h["item"] for x in all_highlights):
                        all_highlights.append(h)
                if usda_highlights:
                    sources_used.append(usda_url.split("/")[-1])

    if not all_highlights:
        logger.warning("seasonal: no produce data fetched from any source")

    # Cap highlights at 12 most prominent items
    highlights = all_highlights[:12]
    chef_picks = _pick_chef_item(highlights, season)

    return {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "season": season,
        "location": location,
        "highlights": highlights,
        "chef_picks": chef_picks,
        "sources": sources_used,
    }
