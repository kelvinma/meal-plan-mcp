"""Deterministic ingredient sensitivity screening."""

from __future__ import annotations

import json
import re
from pathlib import Path


def load_sensitivity_factors(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def _build_lookup(table: list[dict]) -> dict[str, tuple[float, str, str]]:
    """
    Returns a dict mapping normalized term -> (factor, canonical_ingredient, note).
    Covers canonical names and all variants.
    """
    lookup: dict[str, tuple[float, str, str]] = {}
    for entry in table:
        canonical = entry["ingredient"].lower()
        factor = entry["factor"]
        note = entry.get("note", "documented sensitivity")
        for term in [canonical] + [v.lower() for v in entry.get("variants", [])]:
            lookup[term] = (factor, canonical, note)
    return lookup


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _is_finishing_olive_oil(ingredient: str) -> bool:
    """Return True only if the olive oil usage is finishing/flavor-forward."""
    lower = ingredient.lower()
    finishing_markers = [
        "extra virgin",
        "evoo",
        "finishing",
        "drizzle",
    ]
    return any(marker in lower for marker in finishing_markers)


def validate_ingredients(
    dishes: list[dict],
    sensitivity_table: list[dict],
) -> list[dict]:
    lookup = _build_lookup(sensitivity_table)
    results = []

    for dish in dishes:
        dish_id = dish["dish_id"]
        dish_name = dish["dish_name"]
        ingredients = dish["ingredients"]
        flags = []

        for raw_ingredient in ingredients:
            normalized = _normalize(raw_ingredient)

            # Special-case olive oil: only flag finishing/EVOO usage
            if "olive oil" in normalized:
                if not _is_finishing_olive_oil(normalized):
                    continue
                # It's a finishing olive oil — check it falls under the olive oil entry
                match = lookup.get("olive oil")
                if match:
                    factor, canonical, note = match
                    flags.append({
                        "ingredient": raw_ingredient,
                        "factor": factor,
                        "note": note,
                    })
                continue

            # Check exact match first, then substring match against all lookup keys
            match = lookup.get(normalized)
            if match is None:
                for term, entry in lookup.items():
                    if "olive oil" in term:
                        continue  # handled above
                    if term in normalized or normalized in term:
                        match = entry
                        break

            if match:
                factor, canonical, note = match
                flags.append({
                    "ingredient": raw_ingredient,
                    "factor": factor,
                    "note": note,
                })

        if flags:
            flag_summary = ", ".join(
                f"{f['ingredient']} ({f['factor']})" for f in flags
            )
            display_label = f"{dish_name} ⚠️ {flag_summary}"
            status = "flagged"
        else:
            display_label = dish_name
            status = "clear"

        results.append({
            "dish_id": dish_id,
            "dish_name": dish_name,
            "status": status,
            "flags": flags,
            "display_label": display_label,
        })

    return results
