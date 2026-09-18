"""
GridWise Math Optimizer
========================
Takes a 24-hour energy scenario + a list of already-validated, applies=true
directives (post-guardrail, post-LLM) and produces a cost-minimal, feasible
24-hour schedule using linear programming (PuLP / CBC).

This module has NO knowledge of LLMs or natural language. It only consumes
the structured directive objects defined in the Problem Statement, Section 04.

Expected directive dict shape (one per applicable, non-no_op directive):
    {
        "directive_type": "solar_reduction" | "minimum_battery_reserve"
                           | "no_charge_window" | "no_discharge_window"
                           | "max_grid_window",
        "hours": [int, ...],          # unique ints 0..23, ascending
        # plus the type-specific numeric field(s):
        "factor": float,              # solar_reduction
        "minimum_energy_kwh": float,  # minimum_battery_reserve
        "max_grid_kwh": float,        # max_grid_window
    }

Public entry point: solve_schedule(scenario: dict, directives: list[dict]) -> dict
"""

from __future__ import annotations

import math
from typing import Any

import pulp

N_HOURS = 24
TOL = 1e-6          # internal numeric slack for float comparisons
ROUND_DP = 4         # rounding applied to output numbers (judge tolerance is 0.01)


class InfeasibleScenarioError(Exception):
    """Raised when the LP has no feasible solution (should not happen for
    organizer-valid scenarios, but must be handled safely, not crash the API)."""


# --------------------------------------------------------------------------- #
# 1. Directive application: turn structured directives into per-hour arrays
# --------------------------------------------------------------------------- #

def _build_hourly_limits(
    battery: dict[str, float],
    hours_data: list[dict[str, float]],
    directives: list[dict[str, Any]],
) -> dict[str, list[float]]:
    """
    Reduce all applicable directives into flat per-hour arrays the LP can
    consume directly. Multiple directives touching the same hour are combined
    conservatively (i.e. the tightest constraint wins):
      - solar_reduction:        factors multiply (stacked reductions)
      - minimum_battery_reserve: max() of all applicable reserve requirements
      - no_charge_window:        OR'd (any directive blocking -> blocked)
      - no_discharge_window:     OR'd
      - max_grid_window:         min() of all applicable caps
    """
    base_solar = [h["solar_kwh"] for h in hours_data]
    effective_solar = list(base_solar)

    base_reserve = battery["minimum_energy_kwh"]
    reserve_floor = [base_reserve] * N_HOURS

    max_charge = [battery["max_charge_kwh_per_hour"]] * N_HOURS
    max_discharge = [battery["max_discharge_kwh_per_hour"]] * N_HOURS

    grid_cap = [math.inf] * N_HOURS

    for d in directives:
        dtype = d["directive_type"]
        hours = d["hours"]

        if dtype == "solar_reduction":
            factor = d["factor"]
            for h in hours:
                effective_solar[h] *= factor

        elif dtype == "minimum_battery_reserve":
            min_kwh = d["minimum_energy_kwh"]
            for h in hours:
                reserve_floor[h] = max(reserve_floor[h], min_kwh)

        elif dtype == "no_charge_window":
            for h in hours:
                max_charge[h] = 0.0

        elif dtype == "no_discharge_window":
            for h in hours:
                max_discharge[h] = 0.0

        elif dtype == "max_grid_window":
            cap = d["max_grid_kwh"]
            for h in hours:
                grid_cap[h] = min(grid_cap[h], cap)

        else:
            # Guardrail layer should never let this through; defensively skip.
            continue

    return {
        "effective_solar": effective_solar,
        "reserve_floor": reserve_floor,
        "max_charge": max_charge,
        "max_discharge": max_discharge,
        "grid_cap": grid_cap,
    }


# --------------------------------------------------------------------------- #
# 2. LP construction & solve
# --------------------------------------------------------------------------- #

def solve_schedule(scenario: dict[str, Any], directives: list[dict[str, Any]]) -> dict[str, Any]:
    """
    scenario: the parsed request body (scenario_id, hours, battery) -
              operator_notes / LLM interpretation are NOT needed here.
    directives: list of applies=true directive dicts (no_op entries excluded
                by the caller before this function is invoked).

    Returns a dict with hourly_plan / total_grid_kwh / total_cost_bdt /
    peak_grid_kwh, ready to merge into the API response alongside
    directive_interpretation and scenario_id.
    """
    hours_data = sorted(scenario["hours"], key=lambda h: h["hour"])
    if len(hours_data) != N_HOURS or [h["hour"] for h in hours_data] != list(range(N_HOURS)):
        raise ValueError("hours must contain exactly 24 entries for hour 0..23")

    battery = scenario["battery"]
    capacity = battery["capacity_kwh"]
    initial_energy = battery["initial_energy_kwh"]

    limits = _build_hourly_limits(battery, hours_data, directives)
    demand = [h["demand_kwh"] for h in hours_data]
    tariff = [h["tariff_bdt_per_kwh"] for h in hours_data]

    prob = pulp.LpProblem("gridwise_schedule", pulp.LpMinimize)

    grid = [pulp.LpVariable(f"grid_{h}", lowBound=0) for h in range(N_HOURS)]
    solar_used = [pulp.LpVariable(f"solar_used_{h}", lowBound=0) for h in range(N_HOURS)]
    charge = [pulp.LpVariable(f"charge_{h}", lowBound=0) for h in range(N_HOURS)]
    discharge = [pulp.LpVariable(f"discharge_{h}", lowBound=0) for h in range(N_HOURS)]
    batt = [pulp.LpVariable(f"batt_{h}", lowBound=0, upBound=capacity) for h in range(N_HOURS)]

    # Objective: minimize total grid electricity cost
    prob += pulp.lpSum(grid[h] * tariff[h] for h in range(N_HOURS))

    for h in range(N_HOURS):
        prev_energy = initial_energy if h == 0 else batt[h - 1]

        # Energy balance
        prob += (
            grid[h] + solar_used[h] + discharge[h] == demand[h] + charge[h],
            f"balance_{h}",
        )

        # Battery transition
        prob += (batt[h] == prev_energy + charge[h] - discharge[h], f"batt_transition_{h}")

        # Battery bounds (directive-adjusted reserve floor, base capacity ceiling)
        prob += (batt[h] >= limits["reserve_floor"][h], f"reserve_{h}")

        # Solar cap (post solar_reduction)
        prob += (solar_used[h] <= limits["effective_solar"][h], f"solar_cap_{h}")

        # Charge / discharge rate caps (zeroed by no_charge/no_discharge windows)
        prob += (charge[h] <= limits["max_charge"][h], f"charge_cap_{h}")
        prob += (discharge[h] <= limits["max_discharge"][h], f"discharge_cap_{h}")

        # Grid import cap (max_grid_window)
        if math.isfinite(limits["grid_cap"][h]):
            prob += (grid[h] <= limits["grid_cap"][h], f"grid_cap_{h}")

    # End-of-day neutrality
    prob += (batt[N_HOURS - 1] == initial_energy, "end_of_day_neutrality")

    solver = pulp.PULP_CBC_CMD(msg=False)
    prob.solve(solver)

    status = pulp.LpStatus[prob.status]
    if status != "Optimal":
        raise InfeasibleScenarioError(f"LP solve status: {status}")

    hourly_plan = []
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    for h in range(N_HOURS):
        g = max(0.0, pulp.value(grid[h]))
        s = max(0.0, pulp.value(solar_used[h]))
        c = max(0.0, pulp.value(charge[h]))
        dch = max(0.0, pulp.value(discharge[h]))
        e_after = pulp.value(batt[h])

        if c > TOL and dch > TOL:
            # Should not happen (never beneficial under this objective), but
            # guard against solver numerical noise by keeping only the larger.
            if c >= dch:
                dch = 0.0
            else:
                c = 0.0

        if c > TOL:
            action, magnitude = "charge", c
        elif dch > TOL:
            action, magnitude = "discharge", dch
        else:
            action, magnitude = "idle", 0.0

        hourly_plan.append(
            {
                "hour": h,
                "grid_kwh": round(g, ROUND_DP),
                "solar_used_kwh": round(s, ROUND_DP),
                "battery_action": action,
                "battery_kwh": round(magnitude, ROUND_DP),
                "battery_energy_after_kwh": round(e_after, ROUND_DP),
            }
        )

        total_grid += g
        total_cost += g * tariff[h]
        peak_grid = max(peak_grid, g)

    return {
        "hourly_plan": hourly_plan,
        "total_grid_kwh": round(total_grid, ROUND_DP),
        "total_cost_bdt": round(total_cost, ROUND_DP),
        "peak_grid_kwh": round(peak_grid, ROUND_DP),
    }


# --------------------------------------------------------------------------- #
# 3. Self-test with a synthetic scenario exercising every directive type
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import json

    demand_pattern = [150, 140, 135, 130, 130, 140, 160, 190, 210, 200, 190, 185,
                       180, 185, 190, 200, 210, 230, 250, 260, 240, 210, 180, 160]
    solar_pattern = [0, 0, 0, 0, 0, 0, 20, 60, 120, 180, 230, 260,
                      270, 260, 230, 180, 120, 60, 20, 0, 0, 0, 0, 0]
    tariff_pattern = [6, 6, 6, 6, 6, 6, 7, 8, 8, 8, 7, 7,
                       7, 7, 7, 8, 9, 10, 10, 9, 8, 7, 6, 6]

    scenario = {
        "scenario_id": "GRID-TEST-1",
        "hours": [
            {"hour": h, "demand_kwh": demand_pattern[h], "solar_kwh": solar_pattern[h],
             "tariff_bdt_per_kwh": tariff_pattern[h]}
            for h in range(N_HOURS)
        ],
        "battery": {
            "capacity_kwh": 500,
            "initial_energy_kwh": 200,
            "minimum_energy_kwh": 50,
            "max_charge_kwh_per_hour": 100,
            "max_discharge_kwh_per_hour": 100,
        },
    }

    directives = [
        # "Solar output will drop to about 20% from 1 PM to 3 PM."
        {"directive_type": "solar_reduction", "hours": [13, 14], "factor": 0.2},
        # "Do not charge the battery between 2 PM and 4 PM."
        {"directive_type": "no_charge_window", "hours": [14, 15]},
        # "Keep at least 120 kWh in reserve from 6 PM until 9 PM."
        {"directive_type": "minimum_battery_reserve", "hours": [18, 19, 20], "minimum_energy_kwh": 120},
        # extra: cap grid import at night
        {"directive_type": "max_grid_window", "hours": [0, 1, 2, 3], "max_grid_kwh": 120},
    ]

    result = solve_schedule(scenario, directives)

    print(json.dumps(result, indent=2))

    # --- sanity checks mirroring the judge's replay logic ---
    plan = result["hourly_plan"]
    assert len(plan) == 24 and [p["hour"] for p in plan] == list(range(24))

    batt_energy = scenario["battery"]["initial_energy_kwh"]
    for p in plan:
        h = p["hour"]
        demand_h = scenario["hours"][h]["demand_kwh"]
        # energy balance
        lhs = p["grid_kwh"] + p["solar_used_kwh"] + (p["battery_kwh"] if p["battery_action"] == "discharge" else 0)
        rhs = demand_h + (p["battery_kwh"] if p["battery_action"] == "charge" else 0)
        assert abs(lhs - rhs) < 0.05, f"energy balance failed at hour {h}: {lhs} vs {rhs}"
        # battery bound (reserve directive at 18-20)
        min_reserve = 120 if h in (18, 19, 20) else 50
        assert p["battery_energy_after_kwh"] >= min_reserve - 0.05, f"reserve violated at hour {h}"
        # no-charge window
        if h in (14, 15):
            assert p["battery_action"] != "charge", f"charged during no_charge_window at hour {h}"
        # max grid window
        if h in (0, 1, 2, 3):
            assert p["grid_kwh"] <= 120.05, f"grid cap violated at hour {h}"

    assert abs(plan[-1]["battery_energy_after_kwh"] - scenario["battery"]["initial_energy_kwh"]) < 0.05, \
        "end-of-day battery neutrality violated"

    print("\nAll sanity checks passed.")