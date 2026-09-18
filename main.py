from typing import List, Optional, Literal
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

app = FastAPI(title="GridWise Energy Optimizer")

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
    operator_notes: List[str] = Field(..., min_items=1, max_items=3)
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
    structured_adjustment: Optional[dict] = None
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


# ==================== Pipeline Steps ====================

def step1_llm_interpretation(operator_notes: List[str]) -> List[DirectiveInterpretation]:
    """
    Step 1: Interpret operator_notes using LLM to generate Directive Interpretation.
    (LLM API client like Gemini/OpenAI will be integrated here)
    """
    interpretations = []
    for idx, note in enumerate(operator_notes):
        # Stub response until real LLM integration
        interpretations.append(
            DirectiveInterpretation(
                note_index=idx,
                applies=False,
                directive_type="no_op",
                structured_adjustment=None,
                explanation="No constraint detected by default pipeline."
            )
        )
    return interpretations


def step2_guardrail_validation(interpretations: List[DirectiveInterpretation]) -> List[DirectiveInterpretation]:
    """
    Step 2: Deterministic Guardrail Verification.
    Validates directive type, hour sequence (0-23), and logical value ranges.
    """
    validated = []
    for interp in interpretations:
        if interp.directive_type == "no_op":
            interp.applies = False
            interp.structured_adjustment = None
        else:
            interp.applies = True
            if interp.structured_adjustment and "hours" in interp.structured_adjustment:
                # Ensure hours are unique and constrained between 0 and 23
                hrs = sorted(list(set(interp.structured_adjustment["hours"])))
                interp.structured_adjustment["hours"] = [h for h in hrs if 0 <= h <= 23]
        validated.append(interp)
    return validated


def step3_run_optimization(request: EnergyRequest, directives: List[DirectiveInterpretation]) -> EnergyResponse:
    """
    Step 3: Optimization & Plan Generation.
    Generates a cost-minimized 24-hour energy plan using calculation or solver models.
    """
    hourly_plan = []
    current_battery = request.battery.initial_energy_kwh
    
    # Baseline logic (to be replaced with optimization solver)
    for h in request.hours:
        grid_buy = max(0.0, h.demand_kwh - h.solar_kwh)
        hourly_plan.append(
            HourlyPlanEntry(
                hour=h.hour,
                grid_kwh=grid_buy,
                solar_used_kwh=min(h.demand_kwh, h.solar_kwh),
                battery_action="idle",
                battery_kwh=0.0,
                battery_energy_after_kwh=current_battery
            )
        )

    total_grid = sum(p.grid_kwh for p in hourly_plan)
    total_cost = sum(p.grid_kwh * request.hours[p.hour].tariff_bdt_per_kwh for p in hourly_plan)
    peak_grid = max(p.grid_kwh for p in hourly_plan) if hourly_plan else 0.0

    return EnergyResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=directives,
        hourly_plan=hourly_plan,
        total_grid_kwh=round(total_grid, 2),
        total_cost_bdt=round(total_cost, 2),
        peak_grid_kwh=round(peak_grid, 2),
        plan_summary="Optimized plan based on grid tariffs and operator notes."
    )


# ==================== FastAPI Endpoints ====================

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=EnergyResponse, status_code=status.HTTP_200_OK)
def optimize_energy(payload: EnergyRequest):
    try:
        # 1. LLM Interpretation Step
        raw_directives = step1_llm_interpretation(payload.operator_notes)
        
        # 2. Guardrail Validation Step
        validated_directives = step2_guardrail_validation(raw_directives)
        
        # 3. Optimization Step
        final_response = step3_run_optimization(payload, validated_directives)
        
        return final_response
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing optimization request: {str(e)}"
        )