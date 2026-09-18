# GridWise Energy Optimizer

LLM-assisted 24-hour energy scheduling for campus microgrids.  
Built for the **BUP CSE Fest 2026 Hackathon — GridWise LLM Preliminary Challenge**.

## Architecture

```
POST /optimize-energy
        │
        ▼
┌─────────────────────┐
│ [1] LLM Interpreter │  ← Gemini API with retry + fallback
│   llm_interpreter.py│
└────────┬────────────┘
         ▼
┌─────────────────────┐
│ [2] Guardrail Valid. │  ← Deterministic structural checks
│   guardrails.py      │
└────────┬────────────┘
         ▼
┌─────────────────────┐
│ [3] Math Optimizer   │  ← PuLP/CBC linear programming
│   optimizer.py       │
└────────┬────────────┘
         ▼
┌─────────────────────┐
│ [4] Plan Validator   │  ← Independent replay verification
│   plan_validator.py  │
└────────┬────────────┘
         ▼
    JSON Response
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set environment variables

```bash
# Required
export LLM_API_KEY="your-google-gemini-api-key"

# Optional (defaults shown)
export LLM_MODEL="gemini-2.0-flash"
export LLM_FALLBACK_MODEL="gemini-1.5-flash"
export LLM_TIMEOUT_SECONDS="30"
export LLM_MAX_RETRIES="4"
```

On Windows (PowerShell):
```powershell
$env:LLM_API_KEY = "your-google-gemini-api-key"
```

### 3. Run the server

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

### 4. Test

Health check:
```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

Optimize energy:
```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "SAMPLE-01",
    "operator_notes": [
      "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
      "The sports office moved next month'\''s registration deadline."
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
  }'
```

## Error Handling

| HTTP Status | Error Key | When |
|---|---|---|
| 200 | — | Pipeline succeeded |
| 422 | `llm_parse_error` | LLM returned unparseable JSON |
| 422 | `interpretation_validation_failed` | Guardrail checks failed |
| 422 | `infeasible_scenario` | No feasible LP solution |
| 500 | `plan_validation_failed` | Optimizer output failed replay |
| 500 | `internal_error` | Unhandled exception (no stack trace) |
| 503 | `llm_unavailable` | All LLM retries + fallbacks exhausted |

## LLM Retry & Fallback

On 503 / UNAVAILABLE / high-demand errors:

1. **Retry** up to 4 times with exponential backoff: `1s, 2s, 4s, 8s` + random jitter (0-1s)
2. **Fallback** to `gemini-1.5-flash` if primary model exhausts retries
3. **Controlled 503** response if everything fails — no crash

## File Structure

```
├── app.py                 # FastAPI service (entry point)
├── llm_interpreter.py     # LLM call + retry + fallback + few-shot
├── guardrails.py          # Deterministic directive validation
├── plan_validator.py      # Independent plan replay verification
├── optimizer.py           # PuLP/CBC math optimizer (do not modify)
├── schemas.py             # Pydantic request/response models
├── main.py                # Legacy stub (superseded by app.py)
├── requirements.txt       # Python dependencies
└── post validation/       # Organizer test infrastructure
    ├── validation.py
    └── test/
```
