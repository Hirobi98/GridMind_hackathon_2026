from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="GridMind")


# ---------- Request Models ----------

class HourData(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float
    solar_kwh: float
    tariff_bdt_per_kwh: float


class BatteryData(BaseModel):
    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float


class EnergyScenario(BaseModel):
    scenario_id: str
    operator_notes: list[str] = Field(..., min_length=1, max_length=3)
    hours: list[HourData] = Field(..., min_length=24, max_length=24)
    battery: BatteryData


# ---------- Endpoints ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/optimize-energy")
def optimize_energy(scenario: EnergyScenario):
    return {
        "scenario_id": scenario.scenario_id,
        "message": "Optimization pipeline not implemented yet"
    }