"""
GridWise Plan Validator (Independent Replay)
=============================================
Recomputes the optimizer's hourly plan from scratch and verifies every
constraint from Section 11.3 of the Problem Statement.

This module does NOT trust the optimizer — it independently replays the
battery transitions, energy balances, directive constraints, and aggregates.
"""

from __future__ import annotations

import math
from typing import Any

TOLERANCE = 0.01  # Judge tolerance per the Problem Statement


def _is_close(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE


def validate_plan(
    scenario: dict[str, Any],
    interp: list[dict[str, Any]],
    opt_result: dict[str, Any],
) -> list[str]:
    """
    Independently validate the optimizer's hourly plan.

    Args:
        scenario: The original request scenario (hours, battery, etc.).
        interp: The validated directive interpretations (from guardrails).
        opt_result: The optimizer's output dict (hourly_plan, totals).

    Returns:
        List of error strings. Empty = plan is valid.
    """
    errors: list[str] = []

    hourly_plan = opt_result.get("hourly_plan", [])
    battery = scenario.get("battery", {})
    hours_req = {h["hour"]: h for h in scenario.get("hours", [])}

    capacity = battery.get("capacity_kwh", 0)
    initial_energy = battery.get("initial_energy_kwh", 0)
    base_min_reserve = battery.get("minimum_energy_kwh", 0)
    max_charge_rate = battery.get("max_charge_kwh_per_hour", 0)
    max_discharge_rate = battery.get("max_discharge_kwh_per_hour", 0)

    # ── Check 1: 24 entries, hour 0..23, unique, sorted ──────────────────
    if len(hourly_plan) != 24:
        errors.append(f"Expected 24 hourly_plan entries, got {len(hourly_plan)}")
        return errors  # Can't continue without correct structure

    plan_hours = [p.get("hour") for p in hourly_plan]
    if sorted(plan_hours) != list(range(24)):
        errors.append(
            "hourly_plan must contain exactly hours 0..23, unique and sorted"
        )
        return errors

    # ── Pre-compute effective solar (apply all solar_reduction factors) ───
    effective_solar: dict[int, float] = {
        h: hours_req[h]["solar_kwh"] for h in range(24)
    }
    for d in interp:
        if d.get("applies") and d.get("directive_type") == "solar_reduction":
            adj = d["structured_adjustment"]
            for h in adj["hours"]:
                effective_solar[h] *= adj["factor"]

    # ── Pre-compute per-hour reserve floor ───────────────────────────────
    reserve_floor: dict[int, float] = {h: base_min_reserve for h in range(24)}
    for d in interp:
        if d.get("applies") and d.get("directive_type") == "minimum_battery_reserve":
            adj = d["structured_adjustment"]
            for h in adj["hours"]:
                reserve_floor[h] = max(
                    reserve_floor[h], adj["minimum_energy_kwh"]
                )

    # ── Pre-compute no-charge, no-discharge, grid-cap sets ───────────────
    no_charge_hours: set[int] = set()
    no_discharge_hours: set[int] = set()
    grid_cap: dict[int, float] = {}

    for d in interp:
        if not d.get("applies"):
            continue
        dtype = d.get("directive_type")
        adj = d.get("structured_adjustment", {})

        if dtype == "no_charge_window":
            no_charge_hours.update(adj.get("hours", []))
        elif dtype == "no_discharge_window":
            no_discharge_hours.update(adj.get("hours", []))
        elif dtype == "max_grid_window":
            for h in adj.get("hours", []):
                if h in grid_cap:
                    grid_cap[h] = min(grid_cap[h], adj["max_grid_kwh"])
                else:
                    grid_cap[h] = adj["max_grid_kwh"]

    # ── Replay hour by hour ──────────────────────────────────────────────
    current_battery = initial_energy
    recomputed_total_grid = 0.0
    recomputed_total_cost = 0.0
    recomputed_peak_grid = 0.0

    for entry in hourly_plan:
        h = entry["hour"]
        req_h = hours_req[h]

        grid_kwh = entry.get("grid_kwh", 0)
        solar_used = entry.get("solar_used_kwh", 0)
        action = entry.get("battery_action")
        bat_kwh = entry.get("battery_kwh", 0)
        bat_after = entry.get("battery_energy_after_kwh", 0)

        # ── Check 2: All numerics finite and non-negative ────────────────
        for field_name, val in [
            ("grid_kwh", grid_kwh),
            ("solar_used_kwh", solar_used),
            ("battery_kwh", bat_kwh),
            ("battery_energy_after_kwh", bat_after),
        ]:
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                errors.append(f"Hour {h}: {field_name} is not a finite number: {val}")
            elif val < -TOLERANCE:
                errors.append(f"Hour {h}: {field_name} is negative: {val}")

        # ── Check 3: Solar used ≤ effective solar ────────────────────────
        if solar_used > effective_solar[h] + TOLERANCE:
            errors.append(
                f"Hour {h}: solar_used ({solar_used}) exceeds effective solar "
                f"({effective_solar[h]:.4f})"
            )

        # ── Determine charge / discharge ─────────────────────────────────
        if action == "charge":
            charge = bat_kwh
            discharge = 0.0
        elif action == "discharge":
            charge = 0.0
            discharge = bat_kwh
        elif action == "idle":
            charge = 0.0
            discharge = 0.0
            if bat_kwh > TOLERANCE:
                errors.append(
                    f"Hour {h}: action is 'idle' but battery_kwh = {bat_kwh}"
                )
        else:
            errors.append(f"Hour {h}: unknown battery_action '{action}'")
            charge = 0.0
            discharge = 0.0

        # ── Check 4: Battery transition ──────────────────────────────────
        expected_after = current_battery + charge - discharge
        if not _is_close(bat_after, expected_after):
            errors.append(
                f"Hour {h}: battery transition mismatch. "
                f"Expected {expected_after:.4f}, got {bat_after}"
            )

        # ── Check 5: Battery bounds ──────────────────────────────────────
        if bat_after < reserve_floor[h] - TOLERANCE:
            errors.append(
                f"Hour {h}: battery ({bat_after}) below reserve floor "
                f"({reserve_floor[h]})"
            )
        if bat_after > capacity + TOLERANCE:
            errors.append(
                f"Hour {h}: battery ({bat_after}) exceeds capacity ({capacity})"
            )

        # ── Check 6: Rate limits + no-charge/no-discharge windows ────────
        if charge > max_charge_rate + TOLERANCE:
            errors.append(
                f"Hour {h}: charge ({charge}) exceeds max rate ({max_charge_rate})"
            )
        if discharge > max_discharge_rate + TOLERANCE:
            errors.append(
                f"Hour {h}: discharge ({discharge}) exceeds max rate "
                f"({max_discharge_rate})"
            )
        if h in no_charge_hours and charge > TOLERANCE:
            errors.append(f"Hour {h}: charged during no_charge_window")
        if h in no_discharge_hours and discharge > TOLERANCE:
            errors.append(f"Hour {h}: discharged during no_discharge_window")

        # ── Check 7: Grid cap ────────────────────────────────────────────
        if h in grid_cap and grid_kwh > grid_cap[h] + TOLERANCE:
            errors.append(
                f"Hour {h}: grid_kwh ({grid_kwh}) exceeds grid cap "
                f"({grid_cap[h]})"
            )

        # ── Check 8: Energy balance ──────────────────────────────────────
        lhs = grid_kwh + solar_used + discharge
        rhs = req_h["demand_kwh"] + charge
        if not _is_close(lhs, rhs):
            errors.append(
                f"Hour {h}: energy balance mismatch. "
                f"LHS (grid+solar+discharge = {lhs:.4f}) != "
                f"RHS (demand+charge = {rhs:.4f})"
            )

        # ── Accumulate for aggregate checks ──────────────────────────────
        recomputed_total_grid += grid_kwh
        recomputed_total_cost += grid_kwh * req_h.get("tariff_bdt_per_kwh", 0)
        recomputed_peak_grid = max(recomputed_peak_grid, grid_kwh)

        current_battery = bat_after

    # ── Check 9: End-of-day neutrality ───────────────────────────────────
    if not _is_close(current_battery, initial_energy):
        errors.append(
            f"End-of-day battery ({current_battery}) != "
            f"initial energy ({initial_energy})"
        )

    # ── Check 10: Recomputed aggregates match ────────────────────────────
    reported_total_grid = opt_result.get("total_grid_kwh", 0)
    if not _is_close(reported_total_grid, recomputed_total_grid):
        errors.append(
            f"total_grid_kwh mismatch: reported {reported_total_grid}, "
            f"recomputed {recomputed_total_grid:.4f}"
        )

    reported_total_cost = opt_result.get("total_cost_bdt", 0)
    if not _is_close(reported_total_cost, recomputed_total_cost):
        errors.append(
            f"total_cost_bdt mismatch: reported {reported_total_cost}, "
            f"recomputed {recomputed_total_cost:.4f}"
        )

    reported_peak = opt_result.get("peak_grid_kwh", 0)
    if not _is_close(reported_peak, recomputed_peak_grid):
        errors.append(
            f"peak_grid_kwh mismatch: reported {reported_peak}, "
            f"recomputed {recomputed_peak_grid:.4f}"
        )

    return errors
