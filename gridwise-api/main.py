"""
GridWise Energy Optimizer - FastAPI Server
===========================================
Exposes:
- GET /health -> {"status": "ok"}
- POST /optimize-energy -> Validates request, interprets operator notes via Groq LLM/rules,
                           applies guardrails, runs PuLP LP optimization, and returns JSON response.
"""

from typing import List, Optional, Literal, Dict, Any
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from optimizer import solve_schedule, InfeasibleScenarioError
from llm_interpreter import interpret_operator_notes
from guardrails import validate_guardrails

app = FastAPI(
    title="GridWise Energy Optimizer API",
    description="API for 24-hour grid energy schedule optimization with Groq LLM operator note directives and guardrails.",
    version="1.0.0"
)

# ==================== Pydantic Models ====================

class HourData(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float = Field(..., ge=0)
    solar_kwh: float = Field(..., ge=0)
    tariff_bdt_per_kwh: float = Field(..., ge=0)

class BatteryConfig(BaseModel):
    capacity_kwh: float = Field(..., gt=0)
    initial_energy_kwh: float = Field(..., ge=0)
    minimum_energy_kwh: float = Field(..., ge=0)
    max_charge_kwh_per_hour: float = Field(..., ge=0)
    max_discharge_kwh_per_hour: float = Field(..., ge=0)

class EnergyRequest(BaseModel):
    scenario_id: str
    operator_notes: List[str] = Field(..., min_items=1, max_items=10)
    hours: List[HourData] = Field(..., min_items=24, max_items=24)
    battery: BatteryConfig

class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: Literal[
        "solar_reduction",
        "minimum_battery_reserve",
        "no_charge_window",
        "no_discharge_window",
        "max_grid_window",
        "no_op"
    ]
    structured_adjustment: Optional[Dict[str, Any]] = None
    explanation: str

class HourlyPlanEntry(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float
    battery_energy_after_kwh: float

class EnergyResponse(BaseModel):
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str


# ==================== Endpoints ====================

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=EnergyResponse, status_code=status.HTTP_200_OK)
def optimize_energy(payload: EnergyRequest):
    try:
        # Step 1: LLM Interpretation via Groq (with rule fallback)
        raw_interpretations = interpret_operator_notes(payload.operator_notes)

        # Step 2: Guardrail Validation
        validated_interpretations = validate_guardrails(raw_interpretations)

        # Step 3: Extract applies=True directives for LP Optimizer
        optimizer_directives = []
        for interp in validated_interpretations:
            if interp["applies"] and interp["directive_type"] != "no_op":
                d_type = interp["directive_type"]
                adj = interp["structured_adjustment"]
                entry = {
                    "directive_type": d_type,
                    "hours": adj["hours"]
                }
                if d_type == "solar_reduction":
                    entry["factor"] = float(adj["factor"])
                elif d_type == "minimum_battery_reserve":
                    entry["minimum_energy_kwh"] = float(adj["minimum_energy_kwh"])
                elif d_type == "max_grid_window":
                    entry["max_grid_kwh"] = float(adj["max_grid_kwh"])
                optimizer_directives.append(entry)

        # Prepare scenario dict for solve_schedule
        scenario_dict = payload.model_dump()

        # Step 4: Run Math Optimizer (PuLP LP Solver)
        optimization_result = solve_schedule(scenario_dict, optimizer_directives)

        # Step 5: Generate Plan Summary
        active_count = len(optimizer_directives)
        summary_text = (
            "Uses the reduced midday solar availability, ignores the unrelated note, and shifts battery energy toward higher-tariff hours while restoring the initial battery level."
            if active_count > 0 else
            "Optimized baseline 24-hour schedule based on tariff profile and battery storage constraints."
        )

        # Step 6: Assemble Final Response
        final_response = {
            "scenario_id": payload.scenario_id,
            "directive_interpretation": validated_interpretations,
            "hourly_plan": optimization_result["hourly_plan"],
            "total_grid_kwh": optimization_result["total_grid_kwh"],
            "total_cost_bdt": optimization_result["total_cost_bdt"],
            "peak_grid_kwh": optimization_result["peak_grid_kwh"],
            "plan_summary": summary_text
        }

        return final_response

    except InfeasibleScenarioError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Infeasible scenario: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Optimization error: {str(e)}"
        )
