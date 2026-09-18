"""
GridWise Energy Optimizer — FastAPI Service
=============================================
Orchestrates the 4-step pipeline:

    Request → [1] LLM Interpreter → [2] Guardrail Validator
           → [3] Math Optimizer   → [4] Plan Validator → Response

Run: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from schemas import (
    DirectiveInterpretation,
    EnergyRequest,
    EnergyResponse,
    HourlyPlanEntry,
)
from llm_interpreter import (
    LLMParseError,
    LLMUnavailableError,
    interpret_notes,
)
from guardrails import validate_interpretation
from plan_validator import validate_plan
from optimizer import InfeasibleScenarioError, solve_schedule

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("gridwise.app")

# ── FastAPI App ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="GridWise Energy Optimizer",
    description="LLM-assisted 24-hour energy scheduling for campus microgrids",
    version="1.0.0",
)


# ── Global exception handler — never leak stack traces ───────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error"},
    )


# ── Helper: build plan_summary from directives + plan ────────────────────────

def _build_plan_summary(
    interp: list[dict[str, Any]],
    opt_result: dict[str, Any],
) -> str:
    """
    Generate a short 1-2 sentence human-readable strategy description
    from the directives and optimizer output (template-based, no LLM needed).
    """
    parts: list[str] = []

    active_types = [
        d["directive_type"]
        for d in interp
        if d.get("applies")
    ]

    # Describe active directives
    type_descriptions = {
        "solar_reduction": "curtailed solar during maintenance/cleaning",
        "minimum_battery_reserve": "maintained battery reserve during critical hours",
        "no_charge_window": "paused battery charging in the restricted window",
        "no_discharge_window": "paused battery discharge in the restricted window",
        "max_grid_window": "capped grid imports during the constrained period",
    }
    for dt in active_types:
        if dt in type_descriptions:
            parts.append(type_descriptions[dt])

    distractor_count = sum(1 for d in interp if d.get("directive_type") == "no_op")
    if distractor_count:
        parts.append(
            f"ignored {distractor_count} non-energy note{'s' if distractor_count > 1 else ''}"
        )

    # Describe battery strategy from the plan
    plan = opt_result.get("hourly_plan", [])
    charge_hours = [p["hour"] for p in plan if p.get("battery_action") == "charge"]
    discharge_hours = [p["hour"] for p in plan if p.get("battery_action") == "discharge"]

    if charge_hours and discharge_hours:
        parts.append(
            f"charged battery during hours {charge_hours[0]}-{charge_hours[-1]} "
            f"and discharged during hours {discharge_hours[0]}-{discharge_hours[-1]}"
        )

    total_cost = opt_result.get("total_cost_bdt", 0)
    peak = opt_result.get("peak_grid_kwh", 0)
    parts.append(f"achieving total cost {total_cost:.0f} BDT with peak grid {peak:.1f} kWh")

    summary = ". ".join(p.capitalize() if i == 0 else p for i, p in enumerate(parts))
    if not summary.endswith("."):
        summary += "."

    return summary


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    """Health probe. Returns 200 even if LLM key is missing (readiness)."""
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=EnergyResponse)
async def optimize_energy(payload: EnergyRequest):
    """
    Full pipeline: LLM interpret → guardrail → optimize → validate → respond.
    """
    t_start = time.perf_counter()
    scenario_id = payload.scenario_id
    logger.info("[%s] Request received — %d notes", scenario_id, len(payload.operator_notes))

    # Build scenario dict for the optimizer (matches solve_schedule's expected shape)
    scenario = {
        "scenario_id": payload.scenario_id,
        "hours": [h.model_dump() for h in payload.hours],
        "battery": payload.battery.model_dump(),
    }

    try:
        # ── Step 1: LLM Interpretation ───────────────────────────────────
        logger.info("[%s] Step 1: LLM interpretation...", scenario_id)
        t_llm = time.perf_counter()

        raw_interp = await interpret_notes(
            operator_notes=payload.operator_notes,
            scenario=scenario,
        )

        logger.info(
            "[%s] Step 1 done in %.2fs — %d directives",
            scenario_id, time.perf_counter() - t_llm, len(raw_interp),
        )

    except LLMUnavailableError as exc:
        logger.error("[%s] LLM unavailable: %s", scenario_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "llm_unavailable",
                "message": (
                    "The LLM service is currently experiencing high demand. "
                    "All retry attempts and model fallbacks have been exhausted. "
                    "Please try again in a few minutes."
                ),
            },
        )

    except LLMParseError as exc:
        logger.error("[%s] LLM parse error: %s", scenario_id, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "llm_parse_error",
                "message": str(exc),
            },
        )

    # ── Step 2: Guardrail Validation ─────────────────────────────────────
    logger.info("[%s] Step 2: Guardrail validation...", scenario_id)

    guardrail_errors = validate_interpretation(
        interp=raw_interp,
        num_notes=len(payload.operator_notes),
        battery_capacity=payload.battery.capacity_kwh,
    )

    if guardrail_errors:
        logger.warning(
            "[%s] Guardrail validation failed with %d errors: %s",
            scenario_id, len(guardrail_errors), guardrail_errors,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "interpretation_validation_failed",
                "details": guardrail_errors,
            },
        )

    logger.info("[%s] Step 2 passed", scenario_id)

    # ── Step 3: Math Optimizer ───────────────────────────────────────────
    logger.info("[%s] Step 3: Running optimizer...", scenario_id)
    t_opt = time.perf_counter()

    # Filter to applies=true, strip interpretation fields, keep only
    # what solve_schedule expects (directive_type + structured_adjustment fields)
    optimizer_directives = [
        {**d["structured_adjustment"], "directive_type": d["directive_type"]}
        for d in raw_interp
        if d.get("applies") and d.get("directive_type") != "no_op"
    ]

    try:
        opt_result = solve_schedule(scenario, optimizer_directives)
    except InfeasibleScenarioError as exc:
        logger.error("[%s] Optimizer infeasible: %s", scenario_id, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "infeasible_scenario",
                "message": (
                    f"The optimizer could not find a feasible schedule "
                    f"with the given directives: {exc}"
                ),
            },
        )
    except Exception as exc:
        logger.exception("[%s] Optimizer unexpected error: %s", scenario_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "optimizer_error",
                "message": "An unexpected error occurred during optimization.",
            },
        )

    logger.info(
        "[%s] Step 3 done in %.2fs — cost=%.2f BDT",
        scenario_id, time.perf_counter() - t_opt,
        opt_result.get("total_cost_bdt", 0),
    )

    # ── Step 4: Final Validator (independent replay) ─────────────────────
    logger.info("[%s] Step 4: Plan validation (replay)...", scenario_id)

    plan_errors = validate_plan(scenario, raw_interp, opt_result)

    if plan_errors:
        logger.error(
            "[%s] Plan validation FAILED with %d errors: %s",
            scenario_id, len(plan_errors), plan_errors,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "plan_validation_failed",
                "message": (
                    "The optimizer's plan failed independent replay validation. "
                    "This is an internal consistency error."
                ),
                "details": plan_errors,
            },
        )

    logger.info("[%s] Step 4 passed", scenario_id)

    # ── Step 5: Build final response ─────────────────────────────────────
    plan_summary = _build_plan_summary(raw_interp, opt_result)

    # Build typed directive interpretations
    directive_interpretations = [
        DirectiveInterpretation(
            note_index=d["note_index"],
            applies=d["applies"],
            directive_type=d["directive_type"],
            structured_adjustment=d.get("structured_adjustment"),
            explanation=d.get("explanation", ""),
        )
        for d in raw_interp
    ]

    # Build typed hourly plan
    hourly_plan_entries = [
        HourlyPlanEntry(**entry)
        for entry in opt_result["hourly_plan"]
    ]

    response = EnergyResponse(
        scenario_id=payload.scenario_id,
        directive_interpretation=directive_interpretations,
        hourly_plan=hourly_plan_entries,
        total_grid_kwh=opt_result["total_grid_kwh"],
        total_cost_bdt=opt_result["total_cost_bdt"],
        peak_grid_kwh=opt_result["peak_grid_kwh"],
        plan_summary=plan_summary,
    )

    elapsed = time.perf_counter() - t_start
    logger.info(
        "[%s] Pipeline complete in %.2fs — cost=%.2f BDT, peak=%.2f kWh",
        scenario_id, elapsed,
        response.total_cost_bdt, response.peak_grid_kwh,
    )

    return response
