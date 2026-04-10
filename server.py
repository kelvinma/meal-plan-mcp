"""Meal Planning MCP Server."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator

from tools.ads import get_weekly_ads
from tools.history import get_meal_history, init_db, save_meal_plan
from tools.screener import load_sensitivity_factors, validate_ingredients
from tools.seasonal import get_seasonal_report
from tools.weather import get_weather

load_dotenv()

_BASE_DIR = Path(__file__).parent
_SENSITIVITY_PATH = os.getenv(
    "SENSITIVITY_PATH", str(_BASE_DIR / "data" / "sensitivity_factors.json")
)
_STORES_PATH = os.getenv(
    "STORES_PATH", str(_BASE_DIR / "data" / "stores.json")
)
_DB_PATH = os.getenv("DB_PATH", str(_BASE_DIR / "data" / "meal_history.db"))


def _load_stores(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# Lifespan: load static data once, open DB connection
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(server: FastMCP):
    sensitivity_table = load_sensitivity_factors(_SENSITIVITY_PATH)
    stores = _load_stores(_STORES_PATH)
    db = await init_db(_DB_PATH)
    try:
        yield {"sensitivity": sensitivity_table, "stores": stores, "db": db}
    finally:
        await db.close()


mcp = FastMCP("meal_planning_mcp", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class MealRecord(BaseModel):
    model_config = {"extra": "forbid"}

    date: str
    dish_name: str
    primary_protein: str
    primary_carb: str
    cuisine_type: str
    home_cook: bool = True
    source: str | None = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("date must be ISO format YYYY-MM-DD")
        return v


class MealPlanInput(BaseModel):
    model_config = {"extra": "forbid"}
    meals: list[MealRecord]


class DishInput(BaseModel):
    model_config = {"extra": "forbid"}
    dish_id: str
    dish_name: str
    ingredients: list[str]


class ValidationInput(BaseModel):
    model_config = {"extra": "forbid"}
    dishes: list[DishInput]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def meal_planning_get_meal_history(
    ctx,
    n_days: Annotated[int, Field(default=14, ge=1, le=60)] = 14,
) -> dict:
    """Return recent meal history to enforce variety. Includes pre-aggregated protein and carb counts."""
    db = ctx.request_context.lifespan_context["db"]
    try:
        return await get_meal_history(db, n_days)
    except Exception as e:
        return {"error": str(e), "meals": [], "protein_counts": {}, "carb_counts": {}}


@mcp.tool()
async def meal_planning_save_meal_plan(ctx, plan: MealPlanInput) -> dict:
    """Persist the accepted meal plan. Upserts on (date, dish_name)."""
    db = ctx.request_context.lifespan_context["db"]
    try:
        meals = [m.model_dump() for m in plan.meals]
        return await save_meal_plan(db, meals)
    except Exception as e:
        return {"error": str(e), "saved": 0, "status": "error"}


@mcp.tool()
async def meal_planning_validate_ingredients(
    ctx, validation: ValidationInput
) -> list[dict]:
    """
    Deterministic sensitivity screening. Returns per-dish status, flags, and display_label.
    display_label is the string the orchestrator must use verbatim in final output.
    """
    sensitivity_table = ctx.request_context.lifespan_context["sensitivity"]
    try:
        dishes = [d.model_dump() for d in validation.dishes]
        return validate_ingredients(dishes, sensitivity_table)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def meal_planning_get_weekly_ads(ctx, store: str) -> list[dict] | dict:
    """
    Fetch current sale items for a configured grocery store.
    Call for all stores in parallel at session start.
    Returns a list of SaleItem dicts, or an error dict with an 'error' key on failure.
    """
    stores = ctx.request_context.lifespan_context["stores"]
    try:
        return await get_weekly_ads(store, stores)
    except Exception as e:
        return {"source": store, "error": str(e), "items": []}


@mcp.tool()
async def meal_planning_get_seasonal_report(ctx) -> dict:
    """
    Return what produce is currently at peak in your area.
    Combines local farmers market data and USDA terminal market reports.
    """
    stores = ctx.request_context.lifespan_context["stores"]
    try:
        return await get_seasonal_report(stores)
    except Exception as e:
        return {"error": str(e), "highlights": [], "chef_picks": ""}


@mcp.tool()
async def meal_planning_get_weather(ctx, dates: list[str]) -> list[dict]:
    """
    Return daily forecasts for the given ISO dates.
    Each entry includes condition, high_f, low_f, precip_chance, and grill_viable.
    """
    try:
        return await get_weather(dates)
    except Exception as e:
        return [{"date": d, "error": str(e), "grill_viable": False} for d in dates]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
