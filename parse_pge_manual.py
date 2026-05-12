import os
import re
import json
import argparse
import pdfplumber
from datetime import datetime

# --- CONFIGURATION ---
UPLOAD_DIR = "pge_uploads"
JSON_FILE = "pge_rates.json"

def extract_pge_tariff_data(pdf_path):
    results = {"plan_id": None, "rates": {}, "nbc_total": 0.0}
    
    print(f"\n[Scanning PDF] {os.path.basename(pdf_path)}")
    
    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"
        
        # 1. Identify the Schedule
        plan_match = re.search(r"SCHEDULE\s+(E-[A-Z0-9-]+|EV2?-A|EV-B|E-1)", full_text, re.I)
        if plan_match:
            results["plan_id"] = plan_match.group(1).upper().strip()
            if results["plan_id"] == "E-1": results["plan_id"] = "E-1 tiered"
            print(f"  > Detected Schedule: {results['plan_id']}")

        # 2. NBC Components (Already verified working)
        nbc_patterns = {
            "PPP": r"Public Purpose Programs.*?(\d+\.\d{5})",
            "Nuclear": r"Nuclear Decommissioning.*?(\d+\.\d{5})",
            "Wildfire": r"Wildfire Fund.*?(\d+\.\d{5})",
            "CTC": r"Competition Transition.*?(\d+\.\d{5})",
            "Recovery": r"Recovery Bond.*?(\d+\.\d{5})"
        }
        nbc_sum = 0.0
        for name, pattern in nbc_patterns.items():
            match = re.search(pattern, full_text, re.I)
            if match:
                val = float(match.group(1))
                nbc_sum += val
        results["nbc_total"] = nbc_sum

        # 3. Extract Rates (Improved Table Logic)
        lines = full_text.split('\n')
        current_season = "summer"
        in_total_column = False
        
        print("  > Searching for Rate Table values...")
        for line in lines:
            line_clean = line.strip()
            
            # Context Trackers
            if "Winter" in line_clean: current_season = "winter"
            if "Summer" in line_clean: current_season = "summer"
            
            # Logic: If a line contains a bin label AND a 5-decimal number
            # We check if it's the "Total" by looking for specific markers
            decimals = re.findall(r"(\d+\.\d{5})", line_clean)
            
            if decimals:
                # DEBUG: Uncomment this line if it still fails to see what the PDF is showing
                print(f"    [Raw Line Trace] {line_clean}") 
                
                rate_val = float(decimals[-1]) # The right-most number is usually the Total Bundled
                
                # Check for bin keywords
                is_peak = "Peak" in line_clean and "Off" not in line_clean and "Part" not in line_clean
                is_off = "Off-Peak" in line_clean
                is_part = "Part-Peak" in line_clean or "Partial-Peak" in line_clean
                
                # To ensure we are grabbing the "Total Bundled" row and not just a sub-component,
                # we check if the line contains "Total" or if we recently saw the "Total Bundled" header.
                if "Total" in line_clean or "Bundled" in line_clean or len(decimals) > 5:
                    if is_peak:
                        key = f"{current_season}_on"
                        results["rates"][key] = rate_val
                        print(f"    [Rate Match] {key.upper()}: {rate_val:.5f}")
                    elif is_off:
                        key = f"{current_season}_off"
                        results["rates"][key] = rate_val
                        print(f"    [Rate Match] {key.upper()}: {rate_val:.5f}")
                    elif is_part:
                        key = f"{current_season}_mid"
                        results["rates"][key] = rate_val
                        print(f"    [Rate Match] {key.upper()}: {rate_val:.5f}")
                    elif "Tier 1" in line_clean:
                        results["rates"][f"{current_season}_off"] = rate_val
                        print(f"    [Rate Match] TIER 1: {rate_val:.5f}")
                    elif "Tier 2" in line_clean:
                        results["rates"][f"{current_season}_on"] = rate_val
                        print(f"    [Rate Match] TIER 2: {rate_val:.5f}")

    return results

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run: print("\n!!! PDF DRY RUN MODE: No files will be modified !!!")

    if not os.path.exists(JSON_FILE):
        print(f"[Error] {JSON_FILE} not found.")
        return

    with open(JSON_FILE, 'r') as f:
        data = json.load(f)

    updated = False
    
    # Process all PDFs in the upload directory
    if not os.path.exists(UPLOAD_DIR): return
    
    for filename in os.listdir(UPLOAD_DIR):
        if not filename.lower().endswith(".pdf"): continue
        
        pdf_results = extract_pge_tariff_data(os.path.join(UPLOAD_DIR, filename))
        target_id = pdf_results["plan_id"]
        
        if target_id and target_id in data["plans"]:
            print(f"\n[Comparison Ledger: {target_id}]")
            
            # 1. Compare NBC
            if pdf_results["nbc_total"] > 0:
                old_nbc = data.get("nbcRate", 0)
                diff_nbc = abs(pdf_results["nbc_total"] - old_nbc)
                status = "[MATCH]" if diff_nbc < 0.00001 else "[CHANGE DETECTED]"
                print(f"  {status} Global NBC: JSON={old_nbc:.5f} | PDF={pdf_results['nbc_total']:.5f}")
                
                if diff_nbc > 0.00001 and not args.dry_run:
                    data["nbcRate"] = pdf_results["nbc_total"]
                    updated = True

            # 2. Compare Rates
            for key, val in pdf_results["rates"].items():
                season, bin_type = key.split('_')
                
                # Align with JSON structure
                json_bin = "onPeak"
                if bin_type == "off":
                    json_bin = "superOffPeak" if any(x in target_id for x in ["EV", "ELEC"]) else "offPeak"
                elif bin_type == "mid":
                    json_bin = "offPeak"

                old_val = data["plans"][target_id][season].get(json_bin, 0)
                diff = abs(val - old_val)
                status = "[MATCH]" if diff < 0.00001 else "[CHANGE DETECTED]"
                
                print(f"  {status} {season} {json_bin}: JSON={old_val:.5f} | PDF={val:.5f}")
                
                if diff > 0.00001 and not args.dry_run:
                    data["plans"][target_id][season][json_bin] = val
                    updated = True

    if updated:
        data["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(JSON_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        print("\n>>> Result: Success. JSON updated with precision overrides.")
    else:
        print("\n>>> Result: No changes saved.")

if __name__ == "__main__":
    main()
