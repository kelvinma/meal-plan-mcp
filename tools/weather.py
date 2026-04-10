"""Weather forecast via OpenWeatherMap free tier."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"


def _grill_viable(high_f: float, precip_chance: float, condition: str) -> bool:
    c = condition.lower()
    return high_f >= 55 and precip_chance < 40 and "rain" not in c and "storm" not in c


async def get_weather(dates: list[str]) -> list[dict]:
    api_key = os.getenv("WEATHER_API_KEY", "")
    lat = os.getenv("LOCATION_LAT", "")
    lon = os.getenv("LOCATION_LON", "")

    def _fallback(err: str) -> list[dict]:
        return [
            {
                "date": d,
                "condition": "unknown",
                "high_f": 0,
                "low_f": 0,
                "precip_chance": 100,
                "grill_viable": False,
                "error": err,
            }
            for d in dates
        ]

    if not api_key or not lat or not lon:
        logger.warning("Weather: API key or location not configured")
        return _fallback("not_configured")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _FORECAST_URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "appid": api_key,
                    "units": "imperial",
                    "cnt": 40,  # 5 days of 3-hour intervals
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("Weather: request timed out")
        return _fallback("timeout")
    except Exception as e:
        logger.warning("Weather fetch failed: %s", e)
        return _fallback(str(e))

    # Aggregate 3-hour intervals by date
    by_date: dict[str, dict] = defaultdict(
        lambda: {"temps": [], "precip_chances": [], "conditions": []}
    )
    for item in data.get("list", []):
        dt = datetime.fromtimestamp(item["dt"], tz=timezone.utc)
        day_str = dt.strftime("%Y-%m-%d")
        by_date[day_str]["temps"].append(item["main"]["temp"])
        # pop is probability of precipitation, 0–1
        by_date[day_str]["precip_chances"].append(item.get("pop", 0) * 100)
        desc = (item.get("weather") or [{}])[0].get("description", "unknown")
        by_date[day_str]["conditions"].append(desc)

    results: list[dict] = []
    for date_str in dates:
        if date_str not in by_date:
            results.append(
                {
                    "date": date_str,
                    "condition": "no forecast available",
                    "high_f": 0,
                    "low_f": 0,
                    "precip_chance": 0,
                    "grill_viable": False,
                }
            )
            continue

        day = by_date[date_str]
        high_f = round(max(day["temps"]))
        low_f = round(min(day["temps"]))
        precip_chance = round(max(day["precip_chances"]))
        # most-common condition description
        condition = max(set(day["conditions"]), key=day["conditions"].count)
        results.append(
            {
                "date": date_str,
                "condition": condition,
                "high_f": high_f,
                "low_f": low_f,
                "precip_chance": precip_chance,
                "grill_viable": _grill_viable(high_f, precip_chance, condition),
            }
        )

    return results
