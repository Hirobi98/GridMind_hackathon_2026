"""
GridWise Guardrail Validator
=============================
Deterministic validation of LLM-produced directive interpretations.

Checks structural correctness, type constraints, and value ranges
BEFORE the optimizer ever sees the directives. Returns a list of
error strings — empty list means the interpretation passed all checks.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("gridwise.guardrails")

ALLOWED_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

# Keys expected in structured_adjustment for each directive type
_REQUIRED_KEYS: dict[str, set[str]] = {
    "solar_reduction":          {"hours", "factor"},
    "minimum_battery_reserve":  {"hours", "minimum_energy_kwh"},
    "no_charge_window":         {"hours"},
    "no_discharge_window":      {"hours"},
    "max_grid_window":          {"hours", "max_grid_kwh"},
}


def _is_finite_number(val: Any) -> bool:
    """Check that val is a finite int or float."""
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return math.isfinite(val)
    return False


def _validate_hours_array(hours: Any, idx: int) -> list[str]:
    """Validate a 'hours' field from structured_adjustment."""
    errors: list[str] = []

    if not isinstance(hours, list):
        errors.append(f"[note {idx}] 'hours' must be a list, got {type(hours).__name__}")
        return errors

    if len(hours) == 0:
        errors.append(f"[note {idx}] 'hours' array must not be empty")
        return errors

    for i, h in enumerate(hours):
        if not isinstance(h, int) or isinstance(h, bool):
            errors.append(f"[note {idx}] hours[{i}] must be an int, got {type(h).__name__}: {h}")
            return errors
        if h < 0 or h > 23:
            errors.append(f"[note {idx}] hours[{i}] = {h} is outside 0..23")

    # Check uniqueness
    if len(hours) != len(set(hours)):
        errors.append(f"[note {idx}] 'hours' contains duplicates: {hours}")

    # Check strictly ascending
    if hours != sorted(hours):
        errors.append(f"[note {idx}] 'hours' is not in ascending order: {hours}")

    return errors


def validate_interpretation(
    interp: list[dict[str, Any]],
    num_notes: int,
    battery_capacity: float | None = None,
) -> list[str]:
    """
    Validate the LLM's directive interpretations.

    Args:
        interp: List of directive dicts from the LLM interpreter.
        num_notes: Expected number of notes (must match len(interp)).
        battery_capacity: Optional battery capacity for soft-check warnings.

    Returns:
        List of error strings. Empty list means all checks passed.
    """
    errors: list[str] = []

    # ── Length check ─────────────────────────────────────────────────────
    if len(interp) != num_notes:
        errors.append(
            f"Expected {num_notes} interpretations, got {len(interp)}"
        )
        return errors  # Can't validate individual entries if count is wrong

    # ── note_index check ─────────────────────────────────────────────────
    indices = [d.get("note_index") for d in interp]
    expected_indices = list(range(num_notes))

    if sorted(indices) != expected_indices:
        errors.append(
            f"note_index values must be {expected_indices}, "
            f"got {indices} (sorted: {sorted(indices)})"
        )
        # Try to continue validation anyway

    # ── Per-directive checks ─────────────────────────────────────────────
    for i, d in enumerate(interp):
        idx = d.get("note_index", i)
        dtype = d.get("directive_type")
        applies = d.get("applies")
        adj = d.get("structured_adjustment")
        explanation = d.get("explanation")

        # directive_type must be in allowed set
        if dtype not in ALLOWED_DIRECTIVE_TYPES:
            errors.append(
                f"[note {idx}] invalid directive_type: '{dtype}'. "
                f"Must be one of {sorted(ALLOWED_DIRECTIVE_TYPES)}"
            )
            continue  # Skip further checks for this directive

        # explanation should exist
        if not explanation or not isinstance(explanation, str):
            errors.append(f"[note {idx}] 'explanation' is missing or not a string")

        # ── no_op checks ────────────────────────────────────────────────
        if dtype == "no_op":
            if applies is not False:
                errors.append(
                    f"[note {idx}] no_op must have applies=false, got {applies}"
                )
            if adj is not None:
                errors.append(
                    f"[note {idx}] no_op must have structured_adjustment=null, "
                    f"got {type(adj).__name__}"
                )
            continue  # No further checks needed for no_op

        # ── Non-no_op checks ────────────────────────────────────────────
        if applies is not True:
            errors.append(
                f"[note {idx}] {dtype} must have applies=true, got {applies}"
            )

        if adj is None:
            errors.append(
                f"[note {idx}] {dtype} requires structured_adjustment, got null"
            )
            continue

        if not isinstance(adj, dict):
            errors.append(
                f"[note {idx}] structured_adjustment must be a dict, "
                f"got {type(adj).__name__}"
            )
            continue

        # Check required keys for this directive type
        required = _REQUIRED_KEYS.get(dtype, set())
        missing = required - set(adj.keys())
        if missing:
            errors.append(
                f"[note {idx}] {dtype} structured_adjustment is missing "
                f"keys: {sorted(missing)}"
            )

        # ── Validate 'hours' (common to all non-no_op types) ────────────
        if "hours" in adj:
            errors.extend(_validate_hours_array(adj["hours"], idx))

        # ── Type-specific numeric checks ────────────────────────────────
        if dtype == "solar_reduction":
            factor = adj.get("factor")
            if factor is not None:
                if not _is_finite_number(factor):
                    errors.append(
                        f"[note {idx}] solar_reduction 'factor' must be a "
                        f"finite number, got {factor}"
                    )
                elif not (0 < factor <= 1):
                    errors.append(
                        f"[note {idx}] solar_reduction 'factor' must be in "
                        f"(0, 1], got {factor}"
                    )

        elif dtype == "minimum_battery_reserve":
            min_kwh = adj.get("minimum_energy_kwh")
            if min_kwh is not None:
                if not _is_finite_number(min_kwh):
                    errors.append(
                        f"[note {idx}] minimum_battery_reserve "
                        f"'minimum_energy_kwh' must be a finite number, "
                        f"got {min_kwh}"
                    )
                elif min_kwh < 0:
                    errors.append(
                        f"[note {idx}] minimum_battery_reserve "
                        f"'minimum_energy_kwh' must be >= 0, got {min_kwh}"
                    )
                else:
                    # Soft check: reserve shouldn't exceed battery capacity
                    if battery_capacity is not None and min_kwh > battery_capacity:
                        logger.warning(
                            "[note %d] minimum_energy_kwh (%.1f) exceeds "
                            "battery capacity (%.1f) — this will likely "
                            "cause an infeasible scenario",
                            idx, min_kwh, battery_capacity,
                        )

        elif dtype == "max_grid_window":
            max_grid = adj.get("max_grid_kwh")
            if max_grid is not None:
                if not _is_finite_number(max_grid):
                    errors.append(
                        f"[note {idx}] max_grid_window 'max_grid_kwh' must "
                        f"be a finite number, got {max_grid}"
                    )
                elif max_grid < 0:
                    errors.append(
                        f"[note {idx}] max_grid_window 'max_grid_kwh' must "
                        f"be >= 0, got {max_grid}"
                    )
                elif max_grid > 10000:
                    # Soft check: absurdly high grid cap
                    logger.warning(
                        "[note %d] max_grid_kwh = %.1f seems unusually "
                        "high for a campus microgrid",
                        idx, max_grid,
                    )

        # no_charge_window and no_discharge_window have no extra numeric
        # fields beyond 'hours', which is already validated above.

    return errors
