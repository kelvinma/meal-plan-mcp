# Meal Planning MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives Claude Code structured access to grocery sale data, seasonal produce reports, weather forecasts, and a persistent meal history — so it can plan a week of dinners without guessing.

Run `/meal-plan` in Claude Code and get a 7-night plan anchored to what's actually on sale and in season, with your household's dietary sensitivities screened deterministically before anything is suggested.

---

## How it works

Claude Code is the planner. This server is the data layer.

```
/meal-plan (Claude Code slash command)
    │
    ├── meal_planning_get_weekly_ads       ← current sale items, all stores in parallel
    ├── meal_planning_get_seasonal_report  ← local farmers market + USDA terminal data
    ├── meal_planning_get_meal_history     ← last 14 days from SQLite, with protein/carb counts
    ├── meal_planning_get_weather          ← 7-day forecast, grill-viability flag per day
    ├── meal_planning_validate_ingredients ← deterministic sensitivity screening
    └── meal_planning_save_meal_plan       ← write-back to history on confirm
```

At the start of each session the read tools are called in parallel — no scraping happens mid-reasoning. Claude plans against structured inputs, validates ingredients against your sensitivity table, presents the plan, and saves it only on your confirmation.

---

## Prerequisites

- **Claude Code** with a Pro or Team subscription
- **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/) (for setup)
- **OpenWeatherMap API key** — free tier at [openweathermap.org](https://openweathermap.org/api)

---

## Setup

### 1. Install

```bash
git clone https://github.com/you/meal-plan-mcp
cd meal-plan-mcp
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
WEATHER_API_KEY=your_key_here
LOCATION_LAT=35.5951
LOCATION_LON=-82.5515
LOCATION_CITY=Asheville, NC
DB_PATH=./data/meal_history.db
SENSITIVITY_PATH=./data/sensitivity_factors.json
STORES_PATH=./data/stores.json
```

Latitude and longitude are used for weather. `LOCATION_CITY` is a display string in seasonal reports — set it to your city.

### 3. Configure your grocery stores

```bash
cp data/stores.example.json data/stores.json
```

Edit `data/stores.json`. Each entry is a store whose weekly ad Claude will fetch:

```json
[
  {
    "key": "whole_foods",
    "name": "Whole Foods",
    "weekly_ad_url": "https://www.wholefoodsmarket.com/stores/your-city",
    "notes": "Optional hints about how the ad page works."
  }
]
```

`key` is how you identify the store — use any slug you like (`"trader_joes"`, `"aldi"`, etc.).

To add a local farmers market or regional seasonal report source, add an entry with key `"wnc_farmers_market"` (or any key) and set the `weekly_ad_url` to the report page. The seasonal tool picks it up automatically. If your market doesn't have a fetchable page, omit it — the server falls back to USDA terminal market PDFs.

**Store fetch strategies** (record these in each entry's `notes` field):
- If the ad is a PDF: the server fetches the landing page first to find the current PDF URL, then extracts text. Do not put a direct PDF URL — it changes weekly.
- If the store's site requires JavaScript or store selection: use a third-party aggregator URL such as `https://www.myweeklyads.net/<store-name>` instead.

### 4. Configure dietary sensitivities

```bash
cp data/sensitivity_factors.example.json data/sensitivity_factors.json
```

Edit `data/sensitivity_factors.json` with your household's restrictions. Leave it as `[]` if you have none. This file is gitignored and stays local.

```json
[
  {
    "ingredient": "peanut",
    "factor": 5.0,
    "variants": ["peanuts", "peanut butter", "peanut oil"],
    "note": "Optional special-case matching rule."
  }
]
```

`factor` is a relative severity weight — use whatever scale is meaningful to you. The screener passes it through verbatim as part of the `display_label` on any flagged dish. Matching is case-insensitive and covers all listed variants.

One built-in special case: `olive oil` is only flagged for finishing/EVOO usage (e.g. `"extra virgin olive oil"`, `"olive oil (finishing)"`), not for bare `"olive oil"` used as a cooking fat. You can remove this behavior by deleting the `note` field from that entry.

### 5. Register with Claude Code

Add the server to `~/.claude.json` under the top-level `mcpServers` key. Claude Code spawns MCP servers in a non-interactive shell without your shell environment, so `uv run` and version manager shims will fail. Use the real Python binary path from your install (not the venv symlink) and point `PYTHONPATH` at the venv's site-packages:

```json
{
  "mcpServers": {
    "meal_planning": {
      "command": "/absolute/path/to/python3.11",
      "args": ["/absolute/path/to/meal-plan-mcp/server.py"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/meal-plan-mcp/.venv/lib/python3.11/site-packages"
      }
    }
  }
}
```

To find your Python binary path: `which python3.11` or, if using asdf, `ls $(asdf where python)/bin/python3.11`.

The server loads your `.env` automatically at startup, so API keys and data paths don't need to go in the `env` block.

Restart Claude Code after editing the config. Verify the server connected: open Claude Code settings → MCP servers → `meal_planning` should show as active.

---

## Usage

Open Claude Code from the `meal-plan-mcp` project directory (or any directory — the slash command is project-scoped) and run:

```
/meal-plan
```

Claude will:

1. Read your configured stores from `data/stores.json`
2. Fetch weekly ads for all grocery stores, the seasonal report, your meal history, and the 7-day forecast — all in parallel
3. Plan 5 home-cook dinners and 2 restaurant/takeout nights, anchored to current sales and seasonal produce, respecting your sensitivity table and variety constraints
4. Validate every home-cook dish's ingredients against your sensitivity factors
5. Present the plan — flagged dishes are labeled with the ingredient and factor score
6. Ask for confirmation before writing anything to history

Type `y` to save. The plan is written to `data/meal_history.db` and will influence future plans via the variety enforcement logic.

---

## Customization

### Planning rules

The planning rules — number of home vs. restaurant nights, carb preferences, cuisine variety limits, etc. — live in `.claude/commands/meal-plan.md`. Edit that file directly to adjust the rules for your household.

### Sensitivity factors

Add, remove, or adjust entries in `data/sensitivity_factors.json` at any time. The server loads the file at startup, so restart the MCP server (or restart Claude Code) after changes.

### Adding stores mid-season

Just add an entry to `data/stores.json`. The server reads the file at startup. New stores are picked up automatically on the next `/meal-plan` run.

---

## MCP tools

These tools are available to Claude Code once the server is registered. The `/meal-plan` command uses all of them; you can also call them individually in any Claude Code session.

| Tool | Description |
|---|---|
| `meal_planning_get_weekly_ads` | Fetches current sale items for a store key. Returns a list of `{source, item, detail, category}`. On failure returns an error dict — planning continues with degraded data. |
| `meal_planning_get_seasonal_report` | Returns peak produce for your location from your local market and USDA terminal data, plus a `chef_picks` callout. |
| `meal_planning_get_meal_history` | Returns meals from the last N days (default 14, max 60) with pre-aggregated `protein_counts` and `carb_counts`. |
| `meal_planning_validate_ingredients` | Screens a list of dishes against your sensitivity table. Returns a `display_label` for each dish — clear dishes get the plain name, flagged dishes get `"Dish name ⚠️ ingredient (factor)"`. |
| `meal_planning_get_weather` | Returns a daily forecast for a list of ISO dates, including a `grill_viable` boolean per day. |
| `meal_planning_save_meal_plan` | Upserts a list of meal records into history. Safe to call multiple times — deduplicates on `(date, dish_name)`. |

---

## Testing

```bash
# Run the test suite
pytest tests/ -v

# Inspect all tools interactively via the MCP inspector
npx @modelcontextprotocol/inspector .venv/bin/python server.py
```

---

## Project structure

```
meal-plan-mcp/
├── server.py                   # MCP server entry point (FastMCP)
├── .claude/
│   └── commands/
│       └── meal-plan.md        # /meal-plan slash command
├── tools/
│   ├── ads.py                  # weekly ad fetchers
│   ├── seasonal.py             # farmers market + USDA seasonal data
│   ├── history.py              # SQLite meal history
│   ├── screener.py             # sensitivity screening
│   └── weather.py              # OpenWeatherMap forecast
├── data/
│   ├── stores.example.json
│   ├── sensitivity_factors.example.json
│   └── ...                     # stores.json, sensitivity_factors.json, meal_history.db (gitignored)
├── tests/
│   └── test_tools.py
├── pyproject.toml
└── .env.example
```

---

## What this does not do

- **Restaurant scouting** — Claude handles this directly via web search during the planning pass
- **Recipe generation** — dishes are drawn from Claude's training knowledge or cited by name for you to look up
- **Nutritional analysis** — health constraints are enforced through your sensitivity table and planning rules, not computed here
- **Cookbook scraping** — recipes are referenced by title and source, not fetched
