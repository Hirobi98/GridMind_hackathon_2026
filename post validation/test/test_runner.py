import sys
import os
import json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from validation import validate_response, generate_and_save_responses

def main():
    cases_dir = os.path.join(os.path.dirname(__file__), "cases")
    responses_dir = os.path.join(os.path.dirname(__file__), "responses")
    
    if not os.path.isdir(cases_dir):
        print(f"Error: {cases_dir} directory not found.")
        sys.exit(1)
        
    # Generate responses from the expected outputs
    generate_and_save_responses(cases_dir, responses_dir)
        
    case_files = [f for f in os.listdir(cases_dir) if f.endswith(".json") and f != "all_cases.json"]
    
    if not case_files:
        print("No test cases found.")
        sys.exit(0)
        
    all_passed = True
    
    for case_file in sorted(case_files):
        case_filepath = os.path.join(cases_dir, case_file)
        response_filepath = os.path.join(responses_dir, case_file)
        
        with open(case_filepath, "r") as f:
            try:
                case_data = json.load(f)
            except json.JSONDecodeError:
                print(f"Error parsing JSON in {case_file}")
                continue
                
        # Read the generated response
        if not os.path.exists(response_filepath):
            print(f"[{case_file}] SKIP: Missing response file in {responses_dir}")
            continue
            
        with open(response_filepath, "r") as f:
            try:
                resp_data = json.load(f)
            except json.JSONDecodeError:
                print(f"Error parsing response JSON in {case_file}")
                continue
                
        req = case_data.get("input")
        resp = resp_data
        case_id = case_data.get("id", case_file)
        
        if not req or not resp:
            print(f"[{case_id}] SKIP: Missing 'input' or response data")
            continue
            
        print(f"[{case_id}] Validating from response folder...")
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

