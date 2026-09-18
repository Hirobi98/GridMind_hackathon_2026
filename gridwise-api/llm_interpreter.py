"""
LLM Interpreter Module for GridWise API (Groq Cloud API Integration)
======================================================================
Translates natural language operator notes into structured JSON directive objects.
Connects to Groq Cloud API (https://api.groq.com/openai/v1) using GROQ_API_KEY.
Model: llama-3.3-70b-versatile (or llama-3.1-8b-instant).
Includes .env support and graceful fallback to rule-based parser on network errors.
Uses built-in urllib.request (zero third-party dependencies required).
"""

import os
import json
import re
import urllib.request
import urllib.error
from typing import List, Dict, Any

# Automatically load environment variables from .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SYSTEM_PROMPT = """You are an expert energy grid operator assistant. Your job is to convert natural language operator notes into structured JSON directives for 24-hour grid optimization (hours 0 to 23).

Supported directive types:
1. "solar_reduction":
   - Notes describing reduced solar panel output (e.g. cleaning, shading, maintenance, cloud cover).
   - structured_adjustment: {"hours": [int, ...], "factor": float} (factor between 0.0 and 1.0, e.g., 25% -> 0.25)
2. "minimum_battery_reserve":
   - Notes demanding keeping a minimum energy reserve in battery during specific hours.
   - structured_adjustment: {"hours": [int, ...], "minimum_energy_kwh": float}
3. "no_charge_window":
   - Notes prohibiting battery charging during specific hours.
   - structured_adjustment: {"hours": [int, ...]}
4. "no_discharge_window":
   - Notes prohibiting battery discharging during specific hours.
   - structured_adjustment: {"hours": [int, ...]}
5. "max_grid_window":
   - Notes capping grid import power/energy during specific hours.
   - structured_adjustment: {"hours": [int, ...], "max_grid_kwh": float}
6. "no_op":
   - Irrelevant notes or notes that do not affect today's energy schedule.
   - structured_adjustment: null

Output Format Requirements:
Return ONLY a valid JSON object matching:
{
  "directive_type": "solar_reduction" | "minimum_battery_reserve" | "no_charge_window" | "no_discharge_window" | "max_grid_window" | "no_op",
  "structured_adjustment": dict or null,
  "explanation": "short string rationale"
}
"hours" MUST be a list of unique integers between 0 and 23. Do not include markdown ticks like ```json.
"""


def _parse_time_range_to_hours(text: str) -> List[int]:
    """Helper to parse common English time ranges into list of 0..23 hour integers."""
    text_lower = text.lower()
    
    # Noon until 2 PM / 12 PM to 2 PM / 12 to 14
    if ("noon" in text_lower or "12" in text_lower) and ("2 pm" in text_lower or "14" in text_lower or "2pm" in text_lower):
        return [12, 13]
    if "1 pm" in text_lower and "3 pm" in text_lower:
        return [13, 14]
    if "2 pm" in text_lower and "4 pm" in text_lower:
        return [14, 15]
    if "6 pm" in text_lower and ("9 pm" in text_lower or "21" in text_lower):
        return [18, 19, 20]
    
    match = re.search(r'(\d+)\s*(am|pm)?\s*(?:to|until|-)\s*(\d+)\s*(am|pm)?', text_lower)
    if match:
        h1 = int(match.group(1))
        p1 = match.group(2)
        h2 = int(match.group(3))
        p2 = match.group(4)
        
        if p1 == "pm" and h1 < 12: h1 += 12
        if p2 == "pm" and h2 < 12: h2 += 12
        
        if 0 <= h1 < h2 <= 24:
            return list(range(h1, h2))
            
    return []


def heuristic_rule_interpreter(note: str) -> Dict[str, Any]:
    """
    Fallback rule-based parser for offline/dummy testing or when LLM API call fails.
    Extracts directive types, hours, and parameters from standard operator notes.
    """
    note_lower = note.lower()

    # Rule 1: Solar reduction
    if any(k in note_lower for k in ["solar", "cleaning", "wash", "cloud", "shade"]):
        hours = _parse_time_range_to_hours(note)
        if not hours:
            hours = [12, 13]

        factor = 1.0
        pct_match = re.search(r'(\d+(?:\.\d+)?)\s*%', note)
        if pct_match:
            factor = float(pct_match.group(1)) / 100.0

        return {
            "directive_type": "solar_reduction",
            "structured_adjustment": {
                "hours": hours,
                "factor": factor
            },
            "explanation": f"Solar availability reduced to {int(factor * 100)}% during specified hours."
        }

    # Rule 2: Minimum battery reserve
    if any(k in note_lower for k in ["reserve", "keep at least", "minimum battery"]):
        hours = _parse_time_range_to_hours(note)
        if not hours:
            hours = [18, 19, 20]

        kwh_match = re.search(r'(\d+(?:\.\d+)?)\s*kwh', note_lower)
        min_kwh = float(kwh_match.group(1)) if kwh_match else 50.0

        return {
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {
                "hours": hours,
                "minimum_energy_kwh": min_kwh
            },
            "explanation": f"Keep minimum battery reserve of {min_kwh} kWh during designated hours."
        }

    # Rule 3: No charge window
    if any(k in note_lower for k in ["do not charge", "no charge", "stop charging", "prevent charge"]):
        hours = _parse_time_range_to_hours(note)
        if not hours:
            hours = [14, 15]

        return {
            "directive_type": "no_charge_window",
            "structured_adjustment": {
                "hours": hours
            },
            "explanation": "Prohibit battery charging during specified hours."
        }

    # Rule 4: No discharge window
    if any(k in note_lower for k in ["do not discharge", "no discharge", "stop discharging", "prevent discharge"]):
        hours = _parse_time_range_to_hours(note)
        if not hours:
            hours = [10, 11]

        return {
            "directive_type": "no_discharge_window",
            "structured_adjustment": {
                "hours": hours
            },
            "explanation": "Prohibit battery discharging during specified hours."
        }

    # Rule 5: Max grid window
    if any(k in note_lower for k in ["max grid", "cap grid", "limit grid"]):
        hours = _parse_time_range_to_hours(note)
        if not hours:
            hours = [0, 1, 2, 3]

        kwh_match = re.search(r'(\d+(?:\.\d+)?)\s*kwh', note_lower)
        max_kwh = float(kwh_match.group(1)) if kwh_match else 100.0

        return {
            "directive_type": "max_grid_window",
            "structured_adjustment": {
                "hours": hours,
                "max_grid_kwh": max_kwh
            },
            "explanation": f"Cap grid import at {max_kwh} kWh during specified window."
        }

    # Rule 6: Distractor / Irrelevant note -> no_op
    return {
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "This note does not affect today's 24-hour energy schedule."
    }


def call_groq_api(api_key: str, note: str) -> Dict[str, Any]:
    """
    Direct API call to Groq Cloud endpoint (https://api.groq.com/openai/v1/chat/completions).
    Uses urllib.request (standard library).
    """
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key.strip()}"
    }

    models_to_try = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"]

    for model in models_to_try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Operator Note: '{note}'"}
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"}
        }

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    resp_body = response.read().decode("utf-8")
                    data = json.loads(resp_body)
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0]["message"]["content"].strip()
                        if content.startswith("```json"):
                            content = content.split("```json")[1].split("```")[0].strip()
                        elif content.startswith("```"):
                            content = content.split("```")[1].split("```")[0].strip()
                        return json.loads(content)
        except Exception:
            continue

    raise RuntimeError("Groq API calls failed for all attempted models.")


def interpret_note(note: str) -> Dict[str, Any]:
    """
    Attempts to call Groq Cloud API if GROQ_API_KEY is present.
    Falls back gracefully to heuristic rule parser if call fails.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if api_key and api_key.strip():
        try:
            return call_groq_api(api_key.strip(), note)
        except Exception as e:
            print(f"[LLM Interpreter Warning] Groq API call failed ({e}). Falling back to heuristic rule parser.")
            return heuristic_rule_interpreter(note)
    else:
        return heuristic_rule_interpreter(note)


def interpret_operator_notes(notes: List[str]) -> List[Dict[str, Any]]:
    """
    Main function to interpret a list of operator notes.
    """
    results = []
    for idx, note in enumerate(notes):
        raw = interpret_note(note)
        raw["note_index"] = idx
        results.append(raw)
    return results
