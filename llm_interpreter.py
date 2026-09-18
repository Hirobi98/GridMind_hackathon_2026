"""
GridWise LLM Interpreter
=========================
Converts operator notes into structured directives using Google Gemini,
with exponential-backoff retry, model fallback, and strict JSON parsing.

This is the mandatory LLM-in-the-loop step required by the challenge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from typing import Any

try:
    from google import genai
    from google.genai import types
    _NEW_GENAI = True
except ImportError:
    import google.generativeai as genai_legacy
    _NEW_GENAI = False


logger = logging.getLogger("gridwise.llm")

# ── Configuration ────────────────────────────────────────────────────────────

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
LLM_FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "gemini-1.5-flash")
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))
MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "4"))


# ── Custom Exceptions ────────────────────────────────────────────────────────

class LLMUnavailableError(Exception):
    """Raised when all LLM retries and fallback models have been exhausted."""


class LLMParseError(Exception):
    """Raised when the LLM returns malformed / unparseable JSON."""


# ── Retryable error detection ────────────────────────────────────────────────

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}
_RETRYABLE_KEYWORDS = frozenset([
    "unavailable", "high demand", "resource exhausted",
    "overloaded", "rate limit", "quota", "capacity",
    "internal", "deadline exceeded",
])


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception looks like a transient LLM error."""
    exc_str = str(exc).lower()
    # Check for known retryable keywords in the error message
    if any(kw in exc_str for kw in _RETRYABLE_KEYWORDS):
        return True
    # Check for HTTP status codes in the error message
    for code in _RETRYABLE_STATUS_CODES:
        if str(code) in str(exc):
            return True
    return False


# ── System prompt with few-shot examples ─────────────────────────────────────

SYSTEM_PROMPT = """You are a grid-energy directive parser for a university campus microgrid.

Given a list of operator notes, you must classify each note into exactly one structured directive.

## Allowed directive_type values (ONLY these — never invent others):
- solar_reduction
- minimum_battery_reserve
- no_charge_window
- no_discharge_window
- max_grid_window
- no_op

## Time semantics:
- "1 PM to 3 PM" means hours [13, 14] (start inclusive, end exclusive, whole-hour only)
- "noon until 2 PM" means hours [12, 13]
- "6 PM until 9 PM" means hours [18, 19, 20]
- "midnight to 4 AM" means hours [0, 1, 2, 3]
- Always use 24-hour integers 0..23

## Solar reduction semantics:
- "drop to 20%" or "only 20% available" → factor = 0.2
- "80% reduction" → factor = 0.2 (100% - 80% = 20% remaining)
- "one-fifth of normal" → factor = 0.2
- "roughly 25% of the forecast" → factor = 0.25
- "cut in half" → factor = 0.5

## Reserve semantics:
- Extract the numeric minimum_energy_kwh from the note (e.g. "at least 120 kWh" → 120)

## Grid cap semantics:
- Extract max_grid_kwh from the note (e.g. "cap grid at 150 kWh" → 150)

## Rules:
1. Never invent demand, solar, tariff, or battery numbers — only extract what the note says.
2. Notes unrelated to energy operations (e.g. "cafeteria menu changes", "registration deadline") must be classified as no_op with applies=false and structured_adjustment=null.
3. Each note gets exactly ONE directive entry.
4. Return entries in note_index order (0-based), one entry per input note.

## Output format:
Return a JSON object with a single key "directives" containing an array of directive objects.

## Few-shot examples:

Input notes:
["Solar output will drop to about 20% from 1 PM to 3 PM.", "Do not charge the battery between 2 PM and 4 PM.", "Keep at least 120 kWh in reserve from 6 PM until 9 PM."]

Output:
{"directives": [
  {"note_index": 0, "applies": true, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [13, 14], "factor": 0.2}, "explanation": "Solar output drops to 20% between 1 PM and 3 PM."},
  {"note_index": 1, "applies": true, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [14, 15]}, "explanation": "Battery charging is blocked from 2 PM to 4 PM."},
  {"note_index": 2, "applies": true, "directive_type": "minimum_battery_reserve", "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 120}, "explanation": "At least 120 kWh must remain in the battery from 6 PM to 9 PM."}
]}

---

Input notes:
["The cafeteria changed its lunch menu for next week."]

Output:
{"directives": [
  {"note_index": 0, "applies": false, "directive_type": "no_op", "structured_adjustment": null, "explanation": "This note is about the cafeteria menu and has no impact on today's energy schedule."}
]}

---

Input notes:
["Panels will be cleaned from noon until 2 PM — expect only a quarter of the forecasted solar.", "Cap grid imports to 150 kWh per hour between midnight and 4 AM."]

Output:
{"directives": [
  {"note_index": 0, "applies": true, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [12, 13], "factor": 0.25}, "explanation": "Solar availability drops to 25% during panel cleaning from noon to 2 PM."},
  {"note_index": 1, "applies": true, "directive_type": "max_grid_window", "structured_adjustment": {"hours": [0, 1, 2, 3], "max_grid_kwh": 150}, "explanation": "Grid imports are capped at 150 kWh per hour from midnight to 4 AM."}
]}

---

Input notes:
["Do not discharge the battery from 10 AM to noon.", "The solar inverters will be at about one-fifth capacity between 11 AM and 1 PM due to firmware updates."]

Output:
{"directives": [
  {"note_index": 0, "applies": true, "directive_type": "no_discharge_window", "structured_adjustment": {"hours": [10, 11]}, "explanation": "Battery discharge is blocked from 10 AM to noon."},
  {"note_index": 1, "applies": true, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [11, 12], "factor": 0.2}, "explanation": "Solar output reduced to one-fifth (20%) from 11 AM to 1 PM due to firmware updates."}
]}
"""


# ── Core LLM call ────────────────────────────────────────────────────────────

def _build_user_prompt(operator_notes: list[str]) -> str:
    """Build the user message containing the operator notes to interpret."""
    notes_json = json.dumps(operator_notes)
    return (
        f"Interpret each of the following operator notes into exactly one directive.\n\n"
        f"Operator notes: {notes_json}\n\n"
        f"Return ONLY a JSON object with key \"directives\" containing the array of directive objects. "
        f"No markdown, no explanation outside the JSON."
    )


async def _call_gemini(prompt: str, model_name: str) -> str:
    """Make a single Gemini API call. Returns the raw response text."""
    if _NEW_GENAI:
        def _sync_call() -> str:
            client = genai.Client(api_key=LLM_API_KEY)
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            return res.text or ""
    else:
        def _sync_call() -> str:
            genai_legacy.configure(api_key=LLM_API_KEY)
            model = genai_legacy.GenerativeModel(
                model_name=model_name,
                system_instruction=SYSTEM_PROMPT,
                generation_config=genai_legacy.GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            res = model.generate_content(prompt)
            return res.text or ""

    response_text = await asyncio.wait_for(
        asyncio.to_thread(_sync_call),
        timeout=LLM_TIMEOUT_SECONDS,
    )

    return response_text



async def _call_llm_with_retry(
    prompt: str,
    model_name: str,
    max_retries: int = MAX_RETRIES,
) -> str:
    """
    Call the LLM with exponential backoff + jitter on transient errors.

    Retry schedule: 1s, 2s, 4s, 8s (plus 0-1s random jitter each).
    Only retries on 503/UNAVAILABLE/ResourceExhausted/rate-limit errors.
    Other errors propagate immediately.
    """
    last_exception: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await _call_gemini(prompt, model_name)

        except Exception as exc:
            last_exception = exc

            if not _is_retryable(exc):
                # Non-transient error (e.g. 400 Bad Request, auth error) — fail fast
                logger.error(
                    "LLM non-retryable error on attempt %d/%d with model %s: %s",
                    attempt + 1, max_retries + 1, model_name, exc,
                )
                raise

            if attempt == max_retries:
                # Exhausted all retries
                logger.error(
                    "LLM exhausted %d retries with model %s. Last error: %s",
                    max_retries + 1, model_name, exc,
                )
                raise

            # Exponential backoff: 2^attempt seconds + random jitter (0-1s)
            delay = (2 ** attempt) + random.uniform(0, 1.0)
            logger.warning(
                "LLM transient error (attempt %d/%d) with model %s: %s — "
                "retrying in %.1fs...",
                attempt + 1, max_retries + 1, model_name, exc, delay,
            )
            await asyncio.sleep(delay)

    # Should never reach here, but just in case
    raise last_exception or LLMUnavailableError("LLM call failed")


async def _call_llm_with_fallback(prompt: str) -> str:
    """
    Try the primary model, then fall back to the lighter model.
    If both fail, raise LLMUnavailableError.
    """
    # Try primary model
    try:
        return await _call_llm_with_retry(prompt, LLM_MODEL)
    except Exception as primary_exc:
        if not _is_retryable(primary_exc):
            raise LLMUnavailableError(
                f"Primary model ({LLM_MODEL}) failed with non-retryable error: {primary_exc}"
            ) from primary_exc

        logger.warning(
            "Primary model %s exhausted retries. Falling back to %s...",
            LLM_MODEL, LLM_FALLBACK_MODEL,
        )

    # Try fallback model
    try:
        return await _call_llm_with_retry(prompt, LLM_FALLBACK_MODEL)
    except Exception as fallback_exc:
        raise LLMUnavailableError(
            f"Both models exhausted retries. "
            f"Primary ({LLM_MODEL}): transient failure. "
            f"Fallback ({LLM_FALLBACK_MODEL}): {fallback_exc}"
        ) from fallback_exc


# ── JSON parsing with one retry ──────────────────────────────────────────────

def _parse_llm_response(raw_text: str, num_notes: int) -> list[dict[str, Any]]:
    """
    Parse the LLM's JSON response into a list of directive dicts.
    Raises LLMParseError if the JSON is malformed or structurally wrong.
    """
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"LLM returned invalid JSON: {exc}") from exc

    # Handle both {"directives": [...]} and bare [...]
    if isinstance(data, dict):
        directives = data.get("directives", data.get("directive_interpretation"))
        if directives is None:
            # Maybe the dict IS a single directive? Unlikely but handle gracefully.
            raise LLMParseError(
                f"LLM JSON missing 'directives' key. Got keys: {list(data.keys())}"
            )
    elif isinstance(data, list):
        directives = data
    else:
        raise LLMParseError(f"LLM returned unexpected JSON type: {type(data).__name__}")

    if not isinstance(directives, list):
        raise LLMParseError(f"'directives' is not a list: {type(directives).__name__}")

    if len(directives) != num_notes:
        raise LLMParseError(
            f"Expected {num_notes} directives, got {len(directives)}"
        )

    return directives


# ── Public API ───────────────────────────────────────────────────────────────

async def interpret_notes(
    operator_notes: list[str],
    scenario: dict,
) -> list[dict[str, Any]]:
    """
    Interpret operator notes using an LLM.

    This is the mandatory LLM-in-the-loop step. It calls the Gemini API
    with few-shot examples, retries on 503 errors, falls back to a lighter
    model if needed, and parses the structured JSON response.

    Args:
        operator_notes: List of 1-3 operator note strings.
        scenario: The full scenario dict (used for context, not sent to LLM).

    Returns:
        List of directive dicts, one per note, in note_index order.

    Raises:
        LLMUnavailableError: All retries and fallbacks exhausted.
        LLMParseError: LLM returned unparseable JSON after retry.
    """
    if not LLM_API_KEY:
        raise LLMUnavailableError(
            "LLM_API_KEY environment variable is not set. "
            "Set it to your Google Gemini API key."
        )

    prompt = _build_user_prompt(operator_notes)
    num_notes = len(operator_notes)

    # First attempt at calling + parsing
    raw_text = await _call_llm_with_fallback(prompt)

    try:
        return _parse_llm_response(raw_text, num_notes)
    except LLMParseError as first_parse_err:
        logger.warning(
            "First LLM parse failed (%s), retrying LLM call once...",
            first_parse_err,
        )

    # One parse-failure retry (new LLM call, fresh response)
    raw_text = await _call_llm_with_fallback(prompt)

    try:
        return _parse_llm_response(raw_text, num_notes)
    except LLMParseError as second_parse_err:
        raise LLMParseError(
            f"LLM returned unparseable JSON on both attempts. "
            f"Last error: {second_parse_err}"
        ) from second_parse_err
