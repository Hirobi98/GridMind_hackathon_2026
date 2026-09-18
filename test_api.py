import sys
import os
import json
import pytest
from fastapi.testclient import TestClient

from main import app

# Import post-validation suite
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "post validation")))
try:
    from validation import validate_response
except ImportError:
    validate_response = None

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sample_01_solar_cleaning():
    payload = {
        "scenario_id": "SAMPLE-01",
        "operator_notes": [
            "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
            "The sports office moved next month registration deadline."
        ],
        "hours": [
            {"hour": 0, "demand_kwh": 90, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
            {"hour": 1, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
            {"hour": 2, "demand_kwh": 80, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
            {"hour": 3, "demand_kwh": 80, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
            {"hour": 4, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
            {"hour": 5, "demand_kwh": 95, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
            {"hour": 6, "demand_kwh": 110, "solar_kwh": 5, "tariff_bdt_per_kwh": 8},
            {"hour": 7, "demand_kwh": 130, "solar_kwh": 20, "tariff_bdt_per_kwh": 10},
            {"hour": 8, "demand_kwh": 150, "solar_kwh": 50, "tariff_bdt_per_kwh": 12},
            {"hour": 9, "demand_kwh": 165, "solar_kwh": 90, "tariff_bdt_per_kwh": 14},
            {"hour": 10, "demand_kwh": 175, "solar_kwh": 130, "tariff_bdt_per_kwh": 16},
            {"hour": 11, "demand_kwh": 180, "solar_kwh": 160, "tariff_bdt_per_kwh": 16},
            {"hour": 12, "demand_kwh": 185, "solar_kwh": 180, "tariff_bdt_per_kwh": 15},
            {"hour": 13, "demand_kwh": 180, "solar_kwh": 170, "tariff_bdt_per_kwh": 14},
            {"hour": 14, "demand_kwh": 170, "solar_kwh": 140, "tariff_bdt_per_kwh": 13},
            {"hour": 15, "demand_kwh": 165, "solar_kwh": 90, "tariff_bdt_per_kwh": 14},
            {"hour": 16, "demand_kwh": 170, "solar_kwh": 45, "tariff_bdt_per_kwh": 18},
            {"hour": 17, "demand_kwh": 185, "solar_kwh": 10, "tariff_bdt_per_kwh": 22},
            {"hour": 18, "demand_kwh": 205, "solar_kwh": 0, "tariff_bdt_per_kwh": 28},
            {"hour": 19, "demand_kwh": 215, "solar_kwh": 0, "tariff_bdt_per_kwh": 30},
            {"hour": 20, "demand_kwh": 205, "solar_kwh": 0, "tariff_bdt_per_kwh": 26},
            {"hour": 21, "demand_kwh": 175, "solar_kwh": 0, "tariff_bdt_per_kwh": 18},
            {"hour": 22, "demand_kwh": 135, "solar_kwh": 0, "tariff_bdt_per_kwh": 10},
            {"hour": 23, "demand_kwh": 105, "solar_kwh": 0, "tariff_bdt_per_kwh": 7}
        ],
        "battery": {
            "capacity_kwh": 220,
            "initial_energy_kwh": 110,
            "minimum_energy_kwh": 40,
            "max_charge_kwh_per_hour": 50,
            "max_discharge_kwh_per_hour": 50
        }
    }

    res = client.post("/optimize-energy", json=payload)
    assert res.status_code == 200, f"Error: {res.text}"
    resp_data = res.json()

    # Structural Assertions
    assert resp_data["scenario_id"] == "SAMPLE-01"
    assert len(resp_data["directive_interpretation"]) == 2
    assert resp_data["directive_interpretation"][0]["directive_type"] == "solar_reduction"
    assert resp_data["directive_interpretation"][0]["applies"] is True
    assert resp_data["directive_interpretation"][0]["structured_adjustment"]["factor"] == 0.25
    assert resp_data["directive_interpretation"][1]["directive_type"] == "no_op"
    assert resp_data["directive_interpretation"][1]["applies"] is False

    assert len(resp_data["hourly_plan"]) == 24
    assert resp_data["total_grid_kwh"] > 0
    assert resp_data["total_cost_bdt"] > 0

    # Post-validation Suite Check
    if validate_response:
        errors = validate_response(payload, resp_data)
        assert len(errors) == 0, f"Post-validation errors found: {errors}"


def test_battery_reserve_and_no_charge():
    payload = {
        "scenario_id": "TEST-RESERVE-02",
        "operator_notes": [
            "Keep at least 120 kWh battery reserve from 6 PM to 9 PM.",
            "Do not charge battery between 2 PM and 4 PM."
        ],
        "hours": [
            {"hour": h, "demand_kwh": 100 + h * 5, "solar_kwh": 20 if 6 <= h <= 18 else 0, "tariff_bdt_per_kwh": 10}
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": 300,
            "initial_energy_kwh": 150,
            "minimum_energy_kwh": 50,
            "max_charge_kwh_per_hour": 60,
            "max_discharge_kwh_per_hour": 60
        }
    }

    res = client.post("/optimize-energy", json=payload)
    assert res.status_code == 200
    resp_data = res.json()

    interps = resp_data["directive_interpretation"]
    assert interps[0]["directive_type"] == "minimum_battery_reserve"
    assert interps[0]["applies"] is True
    assert interps[0]["structured_adjustment"]["minimum_energy_kwh"] == 120.0

    assert interps[1]["directive_type"] == "no_charge_window"
    assert interps[1]["applies"] is True
    assert interps[1]["structured_adjustment"]["hours"] == [14, 15]


def test_invalid_hours_validation():
    """Validates FastAPI Pydantic input rejection for invalid hour length."""
    payload = {
        "scenario_id": "TEST-INVALID",
        "operator_notes": ["Irrelevant note"],
        "hours": [
            {"hour": 0, "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": 10}
        ],  # Only 1 hour instead of 24
        "battery": {
            "capacity_kwh": 100,
            "initial_energy_kwh": 50,
            "minimum_energy_kwh": 10,
            "max_charge_kwh_per_hour": 20,
            "max_discharge_kwh_per_hour": 20
        }
    }

    res = client.post("/optimize-energy", json=payload)
    assert res.status_code == 422  # Unprocessable Entity
