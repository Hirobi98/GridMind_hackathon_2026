"""
Guardrails Validator Module for GridWise API
=============================================
Validates and sanitizes raw LLM output to prevent invalid directives, malformed hours,
or out-of-bounds parameter values from crashing the application or violating grid logic.
"""

from typing import List, Dict, Any

VALID_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op"
}

def sanitize_hours(raw_hours: Any) -> List[int]:
    """
    Cleans and validates hours array:
    - Filters only integers strictly between 0 and 23
    - Deduplicates (unique)
    - Sorts in ascending order
    """
    if not isinstance(raw_hours, list):
        return []
    
    cleaned = set()
    for h in raw_hours:
        if isinstance(h, (int, float)) and not isinstance(h, bool):
            ih = int(h)
            if 0 <= ih <= 23:
                cleaned.add(ih)
                
    return sorted(list(cleaned))


def make_no_op(note_index: int, explanation: str = "Invalid or unparseable directive converted to no_op.") -> Dict[str, Any]:
    """Returns a standardized no_op directive interpretation."""
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": explanation
    }


def validate_guardrails(raw_interpretations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Validates each raw interpretation from LLM or heuristic parser.
    Ensures Section 04 schema compliance. Never raises unhandled exceptions.
    """
    validated = []
    
    for idx, item in enumerate(raw_interpretations):
        note_idx = item.get("note_index", idx)
        dtype = item.get("directive_type")
        explanation = item.get("explanation", "Directive processed by guardrails.")
        adj = item.get("structured_adjustment")

        # 1. Validate directive_type support
        if dtype not in VALID_DIRECTIVE_TYPES or dtype == "no_op":
            validated.append(make_no_op(note_idx, explanation if dtype == "no_op" else "Unsupported directive type converted to no_op."))
            continue

        # 2. For non-no_op directives, structured_adjustment must be a dictionary
        if not isinstance(adj, dict):
            validated.append(make_no_op(note_idx, "Missing or invalid structured_adjustment object."))
            continue

        # 3. Clean and validate hours array
        hours = sanitize_hours(adj.get("hours"))
        if not hours:
            validated.append(make_no_op(note_idx, "Invalid or empty hours specification."))
            continue

        clean_adj = {"hours": hours}

        # 4. Validate directive-specific numeric parameters
        is_valid = True

        if dtype == "solar_reduction":
            factor = adj.get("factor")
            if factor is None and "solar_factor" in adj:
                factor = adj.get("solar_factor")
                
            if factor is not None and isinstance(factor, (int, float)) and 0.0 <= factor <= 1.0:
                clean_adj["factor"] = float(factor)
            else:
                is_valid = False

        elif dtype == "minimum_battery_reserve":
            min_kwh = adj.get("minimum_energy_kwh")
            if min_kwh is None and "min_kwh" in adj:
                min_kwh = adj.get("min_kwh")
            if min_kwh is None and "minimum_reserve_kwh" in adj:
                min_kwh = adj.get("minimum_reserve_kwh")
                
            if min_kwh is not None and isinstance(min_kwh, (int, float)) and min_kwh >= 0.0:
                clean_adj["minimum_energy_kwh"] = float(min_kwh)
            else:
                is_valid = False

        elif dtype == "max_grid_window":
            max_kwh = adj.get("max_grid_kwh")
            if max_kwh is None and "max_kw" in adj:
                max_kwh = adj.get("max_kw")
            if max_kwh is None and "grid_cap_kwh" in adj:
                max_kwh = adj.get("grid_cap_kwh")
                
            if max_kwh is not None and isinstance(max_kwh, (int, float)) and max_kwh >= 0.0:
                clean_adj["max_grid_kwh"] = float(max_kwh)
            else:
                is_valid = False

        elif dtype in ("no_charge_window", "no_discharge_window"):
            # Only hours needed
            pass

        if not is_valid:
            validated.append(make_no_op(note_idx, "Numeric constraint out of range or missing."))
            continue

        # 5. Build valid active directive
        validated.append({
            "note_index": note_idx,
            "applies": True,
            "directive_type": dtype,
            "structured_adjustment": clean_adj,
            "explanation": explanation
        })

    return validated
