import os
import json
import sys
from validation import validate_response

def main():
    cases_dir = "test/cases"
    if not os.path.isdir(cases_dir):
        print(f"Error: {cases_dir} directory not found.")
        sys.exit(1)
        
    case_files = [f for f in os.listdir(cases_dir) if f.endswith(".json") and f != "all_cases.json"]
    
    if not case_files:
        print("No test cases found.")
        sys.exit(0)
        
    all_passed = True
    
    for case_file in sorted(case_files):
        filepath = os.path.join(cases_dir, case_file)
        with open(filepath, "r") as f:
            try:
                case_data = json.load(f)
            except json.JSONDecodeError:
                print(f"Error parsing JSON in {case_file}")
                continue
                
        # Some cases might have the top-level format with "input" and "expected_output"
        # Others might just have the raw schema.
        req = case_data.get("input")
        resp = case_data.get("expected_output")
        case_id = case_data.get("id", case_file)
        
        if not req or not resp:
            print(f"[{case_id}] SKIP: Missing 'input' or 'expected_output'")
            continue
            
        print(f"[{case_id}] Validating...")
        errors = validate_response(req, resp)
        
        if errors:
            print(f"[{case_id}] FAIL: {len(errors)} errors found.")
            for e in errors:
                print(f"  - {e}")
            all_passed = False
        else:
            print(f"[{case_id}] PASS")
            
    if all_passed:
        print("\nAll cases PASSED!")
        sys.exit(0)
    else:
        print("\nSome cases FAILED.")
        sys.exit(1)

if __name__ == "__main__":
    main()

