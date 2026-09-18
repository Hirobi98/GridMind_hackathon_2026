"""
Comprehensive Unit & Integration Test Suite for GridWise Energy Optimizer
========================================================================
Tests:
  1. Schemas: Pydantic validation for requests and responses
  2. Guardrails: Input validation, type checking, bounds, overlaps
  3. LLM Interpreter:
     - 503 retry with exponential backoff & jitter
     - Fallback model invocation when primary fails
     - JSON parse retry & failure handling
     - Controlled error propagation
  4. Math Optimizer: Linear Programming schedule optimization
  5. Plan Validator: Independent replay verification
  6. FastAPI Endpoints: /health and /optimize-energy HTTP flow
"""

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from schemas import EnergyRequest, EnergyResponse, BatteryConfig, HourData
from llm_interpreter import (
    _call_llm_with_retry,
    _call_llm_with_fallback,
    _is_retryable,
    _parse_llm_response,
    interpret_notes,
    LLMUnavailableError,
    LLMParseError,
)
from guardrails import validate_interpretation
from optimizer import solve_schedule, InfeasibleScenarioError
from plan_validator import validate_plan
from app import app


# ── Sample Fixtures ──────────────────────────────────────────────────────────

def sample_scenario():
    return {
        "scenario_id": "test_scenario_01",
        "hours": [
            {
                "hour": h,
                "tariff_bdt_per_kwh": 6.0 if (h < 17 or h >= 22) else 12.0,
                "demand_kwh": 100.0,
                "solar_kwh": 50.0 if (8 <= h <= 16) else 0.0,
            }
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": 200.0,
            "max_charge_kwh_per_hour": 50.0,
            "max_discharge_kwh_per_hour": 50.0,
            "initial_energy_kwh": 50.0,
            "minimum_energy_kwh": 20.0,
        },
    }


def sample_directives():
    return [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
            "explanation": "Solar reduction test",
        },
        {
            "note_index": 1,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [14, 15]},
            "explanation": "No charge test",
        },
        {
            "note_index": 2,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "Cafeteria menu note",
        },
    ]


# ── 1. Test Schemas ──────────────────────────────────────────────────────────

def test_pydantic_schemas():
    scenario = sample_scenario()
    payload = {
        "scenario_id": scenario["scenario_id"],
        "operator_notes": ["Note 1", "Note 2"],
        "hours": scenario["hours"],
        "battery": scenario["battery"],
    }
    req = EnergyRequest(**payload)
    assert req.scenario_id == "test_scenario_01"
    assert len(req.hours) == 24
    assert req.battery.capacity_kwh == 200.0


# ── 2. Test Guardrails ───────────────────────────────────────────────────────

def test_guardrails_valid():
    directives = sample_directives()
    errors = validate_interpretation(directives, num_notes=3, battery_capacity=200.0)
    assert errors == []


def test_guardrails_invalid_directive_type():
    directives = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "unknown_type",
            "structured_adjustment": {"hours": [12]},
            "explanation": "Invalid",
        }
    ]
    errors = validate_interpretation(directives, num_notes=1, battery_capacity=200.0)
    assert any("invalid directive_type" in err.lower() for err in errors)


def test_guardrails_negative_reserve():
    directives = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [18], "minimum_energy_kwh": -10.0},
            "explanation": "Negative reserve",
        }
    ]
    errors = validate_interpretation(directives, num_notes=1, battery_capacity=200.0)
    assert any("must be >= 0" in err for err in errors)


# ── 3. Test LLM Retry & Error Handling ─────────────────────────────────────

def test_is_retryable():
    assert _is_retryable(Exception("503 UNAVAILABLE - High demand"))
    assert _is_retryable(Exception("429 Rate limit exceeded"))
    assert _is_retryable(Exception("Resource exhausted"))
    assert not _is_retryable(Exception("400 Bad Request: Invalid JSON"))
    assert not _is_retryable(Exception("401 Unauthorized"))


def test_llm_retry_backoff_success():
    async def _run():
        call_count = 0

        async def mock_gemini(prompt, model_name):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("503 UNAVAILABLE - High demand")
            return '{"directives": []}'

        with patch("llm_interpreter._call_gemini", side_effect=mock_gemini), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            res = await _call_llm_with_retry("test prompt", "gemini-2.0-flash", max_retries=3)
            assert res == '{"directives": []}'
            assert call_count == 3
            assert mock_sleep.call_count == 2

    asyncio.run(_run())


def test_llm_retry_exhaustion_and_fallback():
    async def _run():
        primary_calls = 0
        fallback_calls = 0

        async def mock_gemini(prompt, model_name):
            nonlocal primary_calls, fallback_calls
            if model_name == "gemini-2.0-flash":
                primary_calls += 1
                raise Exception("503 UNAVAILABLE - High demand")
            elif model_name == "gemini-1.5-flash":
                fallback_calls += 1
                if fallback_calls < 2:
                    raise Exception("503 UNAVAILABLE - High demand")
                return '{"directives": []}'
            raise Exception("Unknown model")

        with patch("llm_interpreter._call_gemini", side_effect=mock_gemini), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            res = await _call_llm_with_fallback("test prompt")
            assert res == '{"directives": []}'
            assert primary_calls == 5  # 1 initial + 4 retries
            assert fallback_calls == 2  # 1 initial + 1 retry

    asyncio.run(_run())


def test_llm_all_retries_fail_raises_unavailable():
    async def _run():
        async def mock_gemini(prompt, model_name):
            raise Exception("503 UNAVAILABLE - High demand")

        with patch("llm_interpreter._call_gemini", side_effect=mock_gemini), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(LLMUnavailableError) as exc_info:
                await _call_llm_with_fallback("test prompt")
            assert "Both models exhausted retries" in str(exc_info.value)

    asyncio.run(_run())


# ── 4. Test Math Optimizer & Plan Validator ─────────────────────────────────

def test_optimizer_and_plan_validator():
    scenario = sample_scenario()
    directives = [
        {"directive_type": "solar_reduction", "hours": [13, 14], "factor": 0.2},
        {"directive_type": "no_charge_window", "hours": [14, 15]},
    ]
    raw_interp = sample_directives()

    res = solve_schedule(scenario, directives)
    assert "hourly_plan" in res
    assert len(res["hourly_plan"]) == 24
    assert res["total_cost_bdt"] > 0

    # Validate plan with replay validator
    errors = validate_plan(scenario, raw_interp, res)
    assert errors == []


# ── 5. Test FastAPI Endpoints ────────────────────────────────────────────────

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_optimize_energy_success():
    scenario = sample_scenario()
    payload = {
        "scenario_id": scenario["scenario_id"],
        "operator_notes": ["Solar drop to 20% from 1 PM to 3 PM."],
        "hours": scenario["hours"],
        "battery": scenario["battery"],
    }

    mock_llm_response = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
            "explanation": "Solar drop to 20% from 1 PM to 3 PM.",
        }
    ]

    with patch("app.interpret_notes", new_callable=AsyncMock) as mock_interp:
        mock_interp.return_value = mock_llm_response
        response = client.post("/optimize-energy", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["scenario_id"] == "test_scenario_01"
        assert len(data["hourly_plan"]) == 24
        assert "total_cost_bdt" in data


def test_optimize_energy_llm_503_returns_clean_http_503():
    scenario = sample_scenario()
    payload = {
        "scenario_id": scenario["scenario_id"],
        "operator_notes": ["Note 1"],
        "hours": scenario["hours"],
        "battery": scenario["battery"],
    }

    with patch("app.interpret_notes", side_effect=LLMUnavailableError("High demand exhausted")):
        response = client.post("/optimize-energy", json=payload)
        assert response.status_code == 503
        data = response.json()
        assert data["detail"]["error"] == "llm_unavailable"
        assert "high demand" in data["detail"]["message"].lower()
