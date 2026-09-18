import math

TOLERANCE = 0.01

def is_close(a, b):
    return abs(a - b) <= TOLERANCE

def validate_schema(request, response):
    errors = []
    
    # Top-level required fields in response
    required_fields = [
        "scenario_id",
        "directive_interpretation",
        "hourly_plan",
        "total_grid_kwh",
        "total_cost_bdt",
        "peak_grid_kwh",
        "plan_summary"
    ]
    for field in required_fields:
        if field not in response:
            errors.append(f"Missing required response field: {field}")
            
    if response.get("scenario_id") != request.get("scenario_id"):
        errors.append("scenario_id mismatch between request and response")
        
    return errors

def validate_interpretation(request, response):
    errors = []
    operator_notes = request.get("operator_notes", [])
    interps = response.get("directive_interpretation", [])
    
    if len(operator_notes) != len(interps):
        errors.append(f"Expected {len(operator_notes)} interpretations, got {len(interps)}")
        return errors
        
    for i, interp in enumerate(interps):
        if interp.get("note_index") != i:
            errors.append(f"Interpretation {i} note_index should be {i}, got {interp.get('note_index')}")
            
        applies = interp.get("applies")
        dtype = interp.get("directive_type")
        adj = interp.get("structured_adjustment")
        
        if dtype not in [
            "solar_reduction", "minimum_battery_reserve", 
            "no_charge_window", "no_discharge_window", 
            "max_grid_window", "no_op"
        ]:
            errors.append(f"Invalid directive_type '{dtype}' at index {i}")
            
        if dtype == "no_op":
            if applies is not False:
                errors.append(f"no_op directive must have applies = false (index {i})")
            if adj is not None:
                errors.append(f"no_op directive must have structured_adjustment = null (index {i})")
        else:
            if applies is not True:
                errors.append(f"Directive {dtype} must have applies = true (index {i})")
            if adj is None:
                errors.append(f"Directive {dtype} missing structured_adjustment (index {i})")
            elif not isinstance(adj, dict):
                errors.append(f"structured_adjustment must be an object (index {i})")
            else:
                hours = adj.get("hours", [])
                if not isinstance(hours, list):
                    errors.append(f"hours must be an array (index {i})")
                else:
                    if len(hours) == 0:
                        pass # Valid but empty? The doc says "Every listed hour must be unique integer from 0 to 23"
                    elif sorted(list(set(hours))) != hours or not all(isinstance(h, int) and 0 <= h <= 23 for h in hours):
                        errors.append(f"hours must be unique integers 0-23 in ascending order (index {i})")
                
                if dtype == "solar_reduction":
                    factor = adj.get("factor")
                    if factor is None or not (0 <= factor <= 1):
                        errors.append(f"Invalid or missing factor in solar_reduction (index {i})")
                elif dtype == "minimum_battery_reserve":
                    min_res = adj.get("minimum_energy_kwh")
                    if min_res is None or min_res < 0:
                        errors.append(f"Invalid minimum_energy_kwh (index {i})")
                elif dtype == "max_grid_window":
                    max_grid = adj.get("max_grid_kwh")
                    if max_grid is None or max_grid < 0:
                        errors.append(f"Invalid max_grid_kwh (index {i})")
                        
    return errors

def validate_gridwise_and_directives(request, response):
    errors = []
    
    hours_req = {h["hour"]: h for h in request.get("hours", [])}
    battery_req = request.get("battery", {})
    interps = response.get("directive_interpretation", [])
    hourly_plan = response.get("hourly_plan", [])
    
    if len(hourly_plan) != 24:
        errors.append(f"Expected 24 hourly_plan entries, got {len(hourly_plan)}")
        return errors
        
    plan_hours = [p.get("hour") for p in hourly_plan]
    if sorted(plan_hours) != list(range(24)):
        errors.append("hourly_plan must contain exactly hours 0 through 23")
        return errors
        
    # Determine effective solar
    effective_solar = {h: hours_req[h]["solar_kwh"] for h in range(24)}
    for interp in interps:
        if interp.get("applies") and interp.get("directive_type") == "solar_reduction":
            adj = interp["structured_adjustment"]
            for h in adj["hours"]:
                effective_solar[h] *= adj["factor"]
                
    current_battery = battery_req.get("initial_energy_kwh", 0)
    
    total_grid = 0
    total_cost = 0
    peak_grid = 0
    
    for plan in hourly_plan:
        h = plan.get("hour")
        if h not in hours_req:
            errors.append(f"Hour {h} not found in request")
            continue
            
        req_h = hours_req[h]
        grid_kwh = plan.get("grid_kwh", 0)
        solar_used = plan.get("solar_used_kwh", 0)
        action = plan.get("battery_action")
        bat_kwh = plan.get("battery_kwh", 0)
        bat_after = plan.get("battery_energy_after_kwh", 0)
        
        total_grid += grid_kwh
        total_cost += grid_kwh * req_h.get("tariff_bdt_per_kwh", 0)
        peak_grid = max(peak_grid, grid_kwh)
        
        if grid_kwh < 0: errors.append(f"Hour {h}: grid_kwh < 0")
        if solar_used < 0: errors.append(f"Hour {h}: solar_used_kwh < 0")
        if bat_kwh < 0: errors.append(f"Hour {h}: battery_kwh < 0")
        if solar_used > effective_solar[h] + TOLERANCE:
            errors.append(f"Hour {h}: solar_used ({solar_used}) > effective ({effective_solar[h]})")
            
        charge = bat_kwh if action == "charge" else 0
        discharge = bat_kwh if action == "discharge" else 0
        
        # Energy balance
        lhs = grid_kwh + solar_used + discharge
        rhs = req_h.get("demand_kwh", 0) + charge
        if not is_close(lhs, rhs):
            errors.append(f"Hour {h}: Energy balance mismatch: LHS ({lhs}) != RHS ({rhs})")
            
        # Battery state
        if action == "charge":
            expected_after = current_battery + bat_kwh
        elif action == "discharge":
            expected_after = current_battery - bat_kwh
        elif action == "idle":
            expected_after = current_battery
            if bat_kwh != 0:
                errors.append(f"Hour {h}: Action is idle but battery_kwh is {bat_kwh}")
        else:
            errors.append(f"Hour {h}: Unknown battery_action '{action}'")
            expected_after = current_battery
            
        if not is_close(bat_after, expected_after):
            errors.append(f"Hour {h}: Battery transition mismatch. Expected {expected_after}, got {bat_after}")
            
        # Rate limits
        if charge > battery_req.get("max_charge_kwh_per_hour", 0) + TOLERANCE:
            errors.append(f"Hour {h}: Exceeded max charge rate")
        if discharge > battery_req.get("max_discharge_kwh_per_hour", 0) + TOLERANCE:
            errors.append(f"Hour {h}: Exceeded max discharge rate")
            
        # Bounds & Reserve
        min_reserve = battery_req.get("minimum_energy_kwh", 0)
        for interp in interps:
            if interp.get("applies") and interp.get("directive_type") == "minimum_battery_reserve":
                adj = interp["structured_adjustment"]
                if h in adj["hours"]:
                    min_reserve = max(min_reserve, adj["minimum_energy_kwh"])
                    
        if bat_after < min_reserve - TOLERANCE:
            errors.append(f"Hour {h}: Battery {bat_after} fell below minimum reserve {min_reserve}")
        if bat_after > battery_req.get("capacity_kwh", 0) + TOLERANCE:
            errors.append(f"Hour {h}: Battery {bat_after} exceeded capacity {battery_req.get('capacity_kwh')}")
            
        # Hard constraint directives
        for interp in interps:
            if not interp.get("applies"): continue
            dtype = interp.get("directive_type")
            adj = interp.get("structured_adjustment")
            if h in adj["hours"]:
                if dtype == "no_charge_window" and charge > 0:
                    errors.append(f"Hour {h}: Charged during no_charge_window")
                if dtype == "no_discharge_window" and discharge > 0:
                    errors.append(f"Hour {h}: Discharged during no_discharge_window")
                if dtype == "max_grid_window" and grid_kwh > adj["max_grid_kwh"] + TOLERANCE:
                    errors.append(f"Hour {h}: Exceeded max_grid_window ({grid_kwh} > {adj['max_grid_kwh']})")
                    
        current_battery = bat_after
        
    # End of day neutrality
    if not is_close(current_battery, battery_req.get("initial_energy_kwh", 0)):
        errors.append(f"End of day battery ({current_battery}) != initial ({battery_req.get('initial_energy_kwh')})")
        
    # Totals validation
    if not is_close(response.get("total_grid_kwh", 0), total_grid):
        errors.append(f"total_grid_kwh mismatch: Expected {total_grid}, got {response.get('total_grid_kwh')}")
    if not is_close(response.get("total_cost_bdt", 0), total_cost):
        errors.append(f"total_cost_bdt mismatch: Expected {total_cost}, got {response.get('total_cost_bdt')}")
    if not is_close(response.get("peak_grid_kwh", 0), peak_grid):
        errors.append(f"peak_grid_kwh mismatch: Expected {peak_grid}, got {response.get('peak_grid_kwh')}")
        
    return errors

def validate_response(request, response):
    """
    Main validation function that orchestrates the substeps.
    """
    all_errors = []
    
    schema_errors = validate_schema(request, response)
    if schema_errors:
        all_errors.extend(schema_errors)
        return all_errors # Stop if basic schema is wrong
        
    interp_errors = validate_interpretation(request, response)
    all_errors.extend(interp_errors)
    
    gridwise_errors = validate_gridwise_and_directives(request, response)
    all_errors.extend(gridwise_errors)
    
    return all_errors
