"""Meal history persistence using SQLite via aiosqlite."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

import aiosqlite

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    dish_name TEXT NOT NULL,
    primary_protein TEXT NOT NULL,
    primary_carb TEXT NOT NULL,
    cuisine_type TEXT NOT NULL,
    home_cook INTEGER NOT NULL DEFAULT 1,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(date, dish_name)
)
"""


async def init_db(db_path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute(CREATE_TABLE_SQL)
    await db.commit()
    return db


async def get_meal_history(db: aiosqlite.Connection, n_days: int = 14) -> dict:
    n_days = min(n_days, 60)
    async with db.execute(
        """
        SELECT date, dish_name, primary_protein, primary_carb, cuisine_type, home_cook, source
        FROM meals
        WHERE date >= date('now', ? || ' days')
        ORDER BY date DESC
        """,
        (f"-{n_days}",),
    ) as cursor:
        rows = await cursor.fetchall()

    meals = [
        {
            "date": row["date"],
            "dish_name": row["dish_name"],
            "primary_protein": row["primary_protein"],
            "primary_carb": row["primary_carb"],
            "cuisine_type": row["cuisine_type"],
            "home_cook": bool(row["home_cook"]),
            "source": row["source"],
        }
        for row in rows
    ]

    protein_counts = dict(Counter(m["primary_protein"] for m in meals))
    carb_counts = dict(Counter(m["primary_carb"] for m in meals))

    return {
        "meals": meals,
        "protein_counts": protein_counts,
        "carb_counts": carb_counts,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


async def save_meal_plan(db: aiosqlite.Connection, meals: list[dict]) -> dict:
    saved = 0
    for meal in meals:
        await db.execute(
            """
            INSERT INTO meals (date, dish_name, primary_protein, primary_carb, cuisine_type, home_cook, source)
            VALUES (:date, :dish_name, :primary_protein, :primary_carb, :cuisine_type, :home_cook, :source)
            ON CONFLICT(date, dish_name) DO UPDATE SET
                primary_protein = excluded.primary_protein,
                primary_carb = excluded.primary_carb,
                cuisine_type = excluded.cuisine_type,
                home_cook = excluded.home_cook,
                source = excluded.source
            """,
            {
                "date": meal["date"],
                "dish_name": meal["dish_name"],
                "primary_protein": meal["primary_protein"],
                "primary_carb": meal["primary_carb"],
                "cuisine_type": meal["cuisine_type"],
                "home_cook": int(meal.get("home_cook", True)),
                "source": meal.get("source"),
            },
        )
        saved += 1
    await db.commit()
    return {"saved": saved, "status": "ok"}
