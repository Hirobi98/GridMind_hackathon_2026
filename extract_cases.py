import json
import os

def main():
    cases_dir = "test/cases"
    os.makedirs(cases_dir, exist_ok=True)
    
    with open(os.path.join(cases_dir, "all_cases.json"), "r") as f:
        data = json.load(f)
        
    cases = data.get("cases", [])
    for idx, case in enumerate(cases):
        case_id = case.get("id", f"case_{idx+1}")
        filename = os.path.join(cases_dir, f"{case_id}.json")
        with open(filename, "w") as f_out:
            json.dump(case, f_out, indent=2)
        print(f"Extracted {filename}")

if __name__ == "__main__":
    main()

