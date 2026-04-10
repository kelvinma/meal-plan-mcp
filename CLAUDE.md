# Meal Planning MCP Server

## Project Overview

This project is a Python MCP server (`meal_planning_mcp`) that powers a Claude Code–based meal planning workflow. It replaces the single-context Project Instructions approach with a proper tool-backed architecture that enables parallel data fetching, persistent meal history, and deterministic ingredient screening.

The server is consumed by a Claude Code orchestrator that handles planning logic. The MCP server's job is **data** — fetching, persisting, validating — not reasoning.

---

## Architecture

```
Claude Code Orchestrator (planning logic, recipe selection, output assembly)
    │
    ├── meal_planning_get_weekly_ads()         # parallel: all configured stores
    ├── meal_planning_get_seasonal_report()    # local farmers market + USDA terminal data
    ├── meal_planning_get_meal_history()       # SQLite persistence
    ├── meal_planning_validate_ingredients()   # deterministic sensitivity lookup
    ├── meal_planning_get_weather()            # grilling viability
    └── meal_planning_save_meal_plan()         # write-back for history
```

Claude Code calls the read tools in parallel at the start of each session, then passes structured results to the planning pass. No scraping happens mid-reasoning.

---

## Project Structure

```
meal-planning-mcp/
├── CLAUDE.md                   # this file
├── server.py                   # MCP server entry point
├── tools/
│   ├── __init__.py
│   ├── ads.py                  # store flyer fetchers
│   ├── seasonal.py             # local farmers market + USDA data
│   ├── history.py              # SQLite meal history
│   ├── screener.py             # sensitivity flag lookup
│   └── weather.py              # weather fetch
├── data/
│   ├── meal_history.db                    # SQLite database (gitignored)
│   ├── sensitivity_factors.json           # your dietary restrictions (gitignored)
│   ├── sensitivity_factors.example.json   # format reference — copy and edit
│   ├── stores.json                        # your grocery stores (gitignored)
│   └── stores.example.json               # format reference — copy and edit
├── tests/
│   └── test_tools.py
├── pyproject.toml
├── .env.example
└── .env                        # secrets (gitignored)
```

---

## Setup

### Prerequisites

- Python 3.11+
- `uv` (recommended) or `pip`

### Install

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Configure

#### 1. Environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
WEATHER_API_KEY=your_key_here       # openweathermap.org free tier
LOCATION_LAT=40.7128               # your latitude
LOCATION_LON=-74.0060              # your longitude
LOCATION_CITY=New York, NY         # display name used in seasonal reports
DB_PATH=./data/meal_history.db
SENSITIVITY_PATH=./data/sensitivity_factors.json
STORES_PATH=./data/stores.json
```

#### 2. Grocery stores

```bash
cp data/stores.example.json data/stores.json
```

Edit `data/stores.json` to list your actual grocery stores. Each entry needs:

```json
[
  {
    "key": "whole_foods",
    "name": "Whole Foods",
    "weekly_ad_url": "https://www.wholefoodsmarket.com/stores/your-city",
    "notes": "Optional fetch hints for the orchestrator."
  }
]
```

`key` is the identifier passed to `meal_planning_get_weekly_ads`. Use any slug you like (e.g. `"trader_joes"`, `"aldi"`).

#### 3. Dietary restrictions

```bash
cp data/sensitivity_factors.example.json data/sensitivity_factors.json
```

Edit `data/sensitivity_factors.json` with your household's actual sensitivities. Leave it empty (`[]`) if you have none. The file is gitignored — it stays local to your machine.

Each entry:

```json
{
  "ingredient": "peanut",
  "factor": 5.0,
  "variants": ["peanuts", "peanut butter", "peanut oil"],
  "note": "Optional special-case matching rule for the screener."
}
```

`factor` is a relative severity weight used by the orchestrator. Use any scale that's meaningful to you — the screener just passes it through.

### Run

```bash
# stdio transport for Claude Code
python server.py

# or with uv
uv run server.py
```

### Register with Claude Code

Add to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "meal_planning": {
      "command": "uv",
      "args": ["run", "/path/to/meal-planning-mcp/server.py"],
      "env": {
        "WEATHER_API_KEY": "your_key_here",
        "LOCATION_LAT": "40.7128",
        "LOCATION_LON": "-74.0060",
        "LOCATION_CITY": "New York, NY"
      }
    }
  }
}
```

---

## Tools Reference

### `meal_planning_get_weekly_ads`

Fetches current sale items from your configured grocery stores. Intended to be called for all stores simultaneously by the orchestrator.

**Input:**
```python
store: str  # must match a "key" in data/stores.json
```

**Output:** JSON array of `SaleItem`:
```json
[
  {
    "source": "whole_foods",
    "item": "wild salmon fillet",
    "detail": "$9.99/lb, save $4",
    "category": "seafood"
  }
]
```

**Implementation notes:**
- Read `data/stores.json` to resolve the store URL for the given key.
- On fetch failure: return `{"source": store, "error": "unreachable", "items": []}` — do not raise. Orchestrator handles degraded data gracefully.
- Store-specific fetch strategies:
  - If the weekly ad is a PDF: fetch the landing page first to extract the current dynamic PDF path, then fetch the PDF with `web_fetch_pdf_extract_text: true`. Do not guess PDF URLs directly.
  - If the ad page requires store selection or JavaScript rendering, look for a third-party aggregator URL (e.g. `myweeklyads.net/<store-name>`) as a fallback and note it in `stores.json`.
  - Whole Foods store pages may embed sales data in a JSON blob within the page HTML rather than visible text — parse accordingly.

---

### `meal_planning_get_seasonal_report`

Returns what produce is currently at peak in your area, independent of any specific store's sale cycle.

**Input:** none

**Output:**
```json
{
  "retrieved_at": "2026-04-08T09:00:00Z",
  "season": "spring",
  "location": "New York, NY",
  "highlights": [
    {
      "item": "asparagus",
      "notes": "early season, local farms starting to show"
    }
  ],
  "chef_picks": "Asparagus at peak — build around it this week."
}
```

**Implementation notes:**
- Primary source: your local farm network or extension service weekly report (configure the URL in `stores.json` under a `"farmers_market"` key, or set a dedicated `SEASONAL_REPORT_URL` env var).
- Secondary source: USDA terminal market PDF for the current week at `https://ams.usda.gov/mnreports/` — use `web_fetch_pdf_extract_text: true`.
- `chef_picks` is a one-sentence callout synthesized from both sources — specific and direct, not boilerplate. One item max, two if a sale/seasonal alignment is compelling.
- If no local report is found, fall back to USDA data alone and note the gap.

---

### `meal_planning_get_meal_history`

Returns recent meal history to prevent repetition. This is the primary mechanism for variety enforcement.

**Input:**
```python
n_days: int  # default: 14, max: 60
```

**Output:**
```json
{
  "meals": [
    {
      "date": "2026-04-07",
      "dish_name": "Miso-glazed salmon with farro and snap peas",
      "primary_protein": "salmon",
      "primary_carb": "farro",
      "cuisine_type": "Japanese-inflected",
      "home_cook": true
    }
  ],
  "protein_counts": {"salmon": 2, "chicken thighs": 1},
  "carb_counts": {"farro": 2, "white beans": 1},
  "last_updated": "2026-04-08T09:00:00Z"
}
```

**Implementation notes:**
- SQLite backend at `$DB_PATH`. Schema defined in `tools/history.py`.
- `protein_counts` and `carb_counts` are pre-aggregated over the `n_days` window — the orchestrator uses these directly as constraints without needing to count itself.
- `home_cook: true/false` distinguishes home meals from restaurant nights in the history.

---

### `meal_planning_save_meal_plan`

Persists the accepted meal plan after the orchestrator finalizes it. Called once per planning session.

**Input:**
```python
class MealPlanInput(BaseModel):
    meals: list[MealRecord]

class MealRecord(BaseModel):
    date: str           # ISO date: "2026-04-08"
    dish_name: str
    primary_protein: str
    primary_carb: str
    cuisine_type: str
    home_cook: bool
    source: str | None  # cookbook title, "NYT Cooking", "original", etc.
```

**Output:** `{"saved": 3, "status": "ok"}`

**Implementation notes:**
- Upsert on `(date, dish_name)` — safe to call multiple times if the orchestrator retries.
- Do not validate dish quality — that's the orchestrator's job. Just persist what's given.

---

### `meal_planning_validate_ingredients`

Deterministic lookup of flagged ingredients against your sensitivity factor table. Replaces inferential sensitivity screening done mid-reasoning.

**Input:**
```python
class ValidationInput(BaseModel):
    dishes: list[DishInput]

class DishInput(BaseModel):
    dish_id: str
    dish_name: str
    ingredients: list[str]
```

**Output:**
```json
[
  {
    "dish_id": "d1",
    "dish_name": "Roasted bell pepper and white bean soup",
    "status": "flagged",
    "flags": [
      {
        "ingredient": "bell pepper",
        "factor": 2.5,
        "note": "documented sensitivity"
      }
    ],
    "display_label": "Roasted bell pepper and white bean soup ⚠️ bell pepper (2.5)"
  }
]
```

**Implementation notes:**
- Sensitivity table lives in `data/sensitivity_factors.json` (see Setup). Load once at startup via lifespan, not on every call.
- Matching is case-insensitive. Variants listed per-entry in the JSON are also matched.
- Special matching rule for `olive oil`: the entry's `note` field controls whether bare `"olive oil"` is flagged. By default, only flag finishing/flavor-forward usage (`"extra virgin olive oil"`, `"olive oil (finishing)"`, etc.) — not a bare `"olive oil"` used as cooking fat. You can remove this special-casing if your sensitivity applies to all olive oil usage.
- `display_label` format: `"<dish name> ⚠️ <ingredient> (<factor>)"`. Multiple flags: comma-separated in the ⚠️ note. Clear dishes return `display_label` equal to `dish_name` with no ⚠️.

---

### `meal_planning_get_weather`

Returns forecast for your location to inform grilling vs. indoor cooking decisions.

**Input:**
```python
dates: list[str]  # ISO dates, e.g. ["2026-04-08", "2026-04-09", "2026-04-10"]
```

**Output:**
```json
[
  {
    "date": "2026-04-08",
    "condition": "partly cloudy",
    "high_f": 68,
    "low_f": 48,
    "precip_chance": 10,
    "grill_viable": true
  }
]
```

**Implementation notes:**
- Uses OpenWeatherMap free tier. Key in `$WEATHER_API_KEY`. Location from `$LOCATION_LAT` / `$LOCATION_LON`.
- `grill_viable: true` when `high_f >= 55`, `precip_chance < 40`, and condition does not include `"rain"` or `"storm"`.
- Return `grill_viable: false` and log a warning if the API is unreachable — do not raise. Planning continues without weather context.

---

## Implementation Standards

### Stack

- **Language:** Python 3.11+
- **Framework:** FastMCP (`mcp[cli]`)
- **Validation:** Pydantic v2 with `model_config`, `field_validator`, `model_dump()`
- **HTTP:** `httpx` with async context managers
- **Persistence:** `aiosqlite` for async SQLite access
- **Transport:** stdio (Claude Code local integration)

### Dependencies (`pyproject.toml`)

```toml
[project]
name = "meal-planning-mcp"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    "mcp[cli]>=1.0.0",
    "httpx>=0.27.0",
    "pydantic>=2.0.0",
    "aiosqlite>=0.20.0",
    "python-dotenv>=1.0.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "ruff"]
```

### Error handling contract

Every tool must return a valid JSON-serializable result even on failure. Never raise an unhandled exception to the MCP transport layer. Use this pattern:

```python
try:
    result = await fetch_something()
    return result
except httpx.TimeoutException:
    return {"error": "timeout", "source": store_key, "items": []}
except Exception as e:
    return {"error": str(e), "source": store_key, "items": []}
```

The orchestrator checks for `"error"` keys in responses and degrades gracefully.

### Fetch reliability notes

- **PDF-based ads:** The PDF URL is often dynamic. Always fetch the landing page first to extract the current PDF href before attempting the PDF fetch.
- **JS-rendered store pages:** If the store's own site requires store selection or JavaScript to render, find a third-party aggregator (e.g. `myweeklyads.net/<store>`) and record it as the URL in `stores.json`.
- **Farmers market / seasonal pages:** Page structure varies. If no weekly report is detected, fall back to USDA terminal market data for the current week.

### Lifespan management

Load static data once at startup, not per-call:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan():
    sensitivity_table = load_sensitivity_factors(os.getenv("SENSITIVITY_PATH", "./data/sensitivity_factors.json"))
    stores = load_stores(os.getenv("STORES_PATH", "./data/stores.json"))
    db = await init_db(os.getenv("DB_PATH", "./data/meal_history.db"))
    yield {"sensitivity": sensitivity_table, "stores": stores, "db": db}
    await db.close()

mcp = FastMCP("meal_planning_mcp", lifespan=lifespan)
```

---

## Orchestrator Contract

This server does not contain planning logic. The Claude Code orchestrator is responsible for:

- Deciding which nights are home-cook vs. restaurant
- Selecting dishes from the cookbook library or recipe knowledge
- Enforcing carb, protein, and fiber planning rules
- Assembling the final plan output

The MCP server provides clean inputs so the orchestrator can reason without fetching. The orchestrator should:

1. Call `get_weekly_ads` for all configured stores in parallel at session start
2. Call `get_seasonal_report` and `get_meal_history` in the same parallel batch
3. Call `get_weather` for the planning window dates
4. Run planning logic with all structured inputs in context
5. Call `validate_ingredients` on the proposed dish list
6. Apply `display_label` values from screener output verbatim in final output
7. Call `save_meal_plan` with the accepted plan before ending the session

---

## Development Phases

### Phase 1 — Core persistence and screening (build first)

- [x] `tools/history.py` — SQLite schema, `get_meal_history`, `save_meal_plan`
- [x] `tools/screener.py` — `validate_ingredients` with sensitivity JSON table
- [x] `server.py` — FastMCP server wired up with lifespan
- [ ] Manual test via `npx @modelcontextprotocol/inspector`

This phase alone solves variety and deterministic screening. Ship it before touching scrapers.

### Phase 2 — Data fetchers

- [ ] `tools/ads.py` — store fetchers driven by `data/stores.json`
- [ ] `tools/seasonal.py` — local farmers market + USDA
- [ ] `tools/weather.py` — OpenWeatherMap

### Phase 3 — Orchestrator Claude Code script

- [ ] `orchestrator.py` — Claude Code script that dispatches tools in parallel and runs the planning pass
- [ ] Replace Project Instructions with a lean prompt that references tool outputs by name

---

## Testing

```bash
# Run test suite
pytest tests/ -v

# Inspect tools interactively
npx @modelcontextprotocol/inspector uv run server.py

# Verify server starts cleanly
python server.py --help
```

Key test cases:
- `validate_ingredients` correctly flags all entries in your sensitivity table and their variants
- `get_meal_history` returns accurate protein/carb counts over a 14-day window
- `save_meal_plan` upserts safely on duplicate `(date, dish_name)` pairs
- Ad fetchers return `{"error": ..., "items": []}` on network failure, not an exception
- `grill_viable` logic handles edge cases: exactly 55°F, exactly 40% precip chance

---

## What This Does Not Do

- Restaurant scouting — the orchestrator handles this with web search directly
- Recipe generation — that's the orchestrator's planning pass
- Nutritional analysis — outside scope; health constraints are enforced via orchestrator planning rules, not computed here
- Cookbook or recipe site scraping — recipes are recalled from the orchestrator's training knowledge or cited by name for the user to look up
