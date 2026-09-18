# ⚡ GridWise Energy Optimizer API

GridWise API is a production-ready FastAPI application designed for 24-hour microgrid energy schedule optimization with battery storage. It combines **LLM-assisted operator note interpretation (Groq Cloud)**, **strict post-LLM guardrail validation**, and **Linear Programming (PuLP / CBC solver)** to minimize grid electricity costs while maintaining battery constraints.

---

## 🛠️ Repository Structure

```text
.
├── main.py              # FastAPI application (GET /health, POST /optimize-energy)
├── optimizer.py         # Linear programming cost minimizer solver (PuLP / CBC)
├── llm_interpreter.py   # LLM note-to-directive translator (Groq Cloud + rule fallback)
├── guardrails.py        # Post-LLM output validator & schema sanitizer
├── test_api.py          # Pytest suite for endpoints & validator integration
├── requirements.txt     # Python dependencies
├── Dockerfile           # Docker container configuration
├── README.md            # Documentation and execution guide
└── .env                 # Environment variables (local dev)
```

---

## 🏗️ System Architecture

```text
               ┌───────────────────────────┐
               │    POST /optimize-energy  │
               └─────────────┬─────────────┘
                             │
                             ▼
               ┌───────────────────────────┐
               │ 1. LLM Note Interpreter   │ (Groq Cloud Llama-3.3-70b / Heuristic Fallback)
               └─────────────┬─────────────┘
                             │
                             ▼
               ┌───────────────────────────┐
               │ 2. Guardrails Validator   │ (Enforces 0..23 hours, factors, safe no_op)
               └─────────────┬─────────────┘
                             │
                             ▼
               ┌───────────────────────────┐
               │ 3. PuLP LP Math Optimizer │ (Cost Minimization Solver)
               └─────────────┬─────────────┘
                             │
                             ▼
               ┌───────────────────────────┐
               │   JSON Response Output    │ (Conforms to Section 10 Schema)
               └───────────────────────────┘
```

---

## ⚙️ Environment Variables

| Variable Name | Description | Required? | Default |
| --- | --- | --- | --- |
| `GROQ_API_KEY` | Groq Cloud API Key for LLM note interpretation | Optional | Uses built-in heuristic rule parser if missing |
| `PORT` | HTTP Server Port | Optional | `8000` (Render binds automatically via `$PORT`) |

*Note: No secrets or hardcoded API keys are included in git tracking.*

---


---

## 💻 Running Locally

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file in the root directory:
```env
GROQ_API_KEY=gsk_your_groq_api_key_here
```

### 3. Start Uvicorn Server

```bash
uvicorn main:app--reload
```

Access Interactive API Documentation at: `http://localhost:8000/docs`

---



---

## 📡 API Endpoints & `curl` Examples

### 1. Health Check (`GET /health`)

**Request:**
```bash
curl -X GET http://localhost:8000/health
```

**Response:**
```json
{
  "status": "ok"
}
```

### 2. Optimize Energy Schedule (`POST /optimize-energy`)

**Request:**
```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
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
  }'
```

**Response (Code 200 OK):**
```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {
        "hours": [12, 13],
        "factor": 0.25
      },
      "explanation": "Solar availability reduced to 25% during specified hours."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note does not affect today's 24-hour energy schedule."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 70.0,
      "solar_used_kwh": 0.0,
      "battery_action": "discharge",
      "battery_kwh": 20.0,
      "battery_energy_after_kwh": 90.0
    }
    /* ... 24 hours plan ... */
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 187.5,
  "plan_summary": "Uses the reduced midday solar availability, ignores the unrelated note, and shifts battery energy toward higher-tariff hours while restoring the initial battery level."
}
```

---

## 🧪 Testing

Run pytest suite:
```bash
pytest test_api.py -v
```

---

## 🤖 LLM Model & Provider

- **LLM Provider:** Groq Cloud (`https://api.groq.com/openai/v1`)
- **Primary Model:** `llama-3.3-70b-versatile`
- **Fallback Models:** `llama-3.1-8b-instant`, `mixtral-8x7b-32768`
- **Offline Mode:** Built-in Heuristic Rule Parser (ensures 100% test passing even without API key)
