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

        # 2. NBC Components
        nbc_patterns = {
            "PPP": r"Public Purpose Programs.*?(\d+\.\d{5})",
            "Nuclear": r"Nuclear Decommissioning.*?(-?\d+\.\d{5})",
            "Wildfire": r"Wildfire Fund.*?(\d+\.\d{5})",
            "CTC": r"Competition Transition.*?(\d+\.\d{5})",
            "Recovery": r"Recovery Bond Charge.*?(\d+\.\d{5})"
        }
        nbc_sum = 0.0
        for name, pattern in nbc_patterns.items():
            match = re.search(pattern, full_text, re.I)
            if match:
                val = float(match.group(1))
                nbc_sum += val
        results["nbc_total"] = nbc_sum

        # 3. Extract Rates (Based on 'Total Usage' pattern found in trace)
        lines = full_text.split('\n')
        total_usage_count = 0
        
        print("  > Extracting 'Total Usage' rates...")
        for line in lines:
            line_clean = line.strip()
            
            # The trace shows: 'Total Usage $0.52240 (R) $0.39940 (R)'
            if line_clean.startswith("Total Usage"):
                # Find all 5-decimal numbers on this specific line
                decimals = re.findall(r"(\d+\.\d{5})", line_clean)
                
                if len(decimals) >= 2:
                    total_usage_count += 1
                    # 1st line = Summer, 2nd line = Winter
                    season = "summer" if total_usage_count == 1 else "winter"
                    
                    # 1st value = On-Peak, 2nd value = Off-Peak
                    results["rates"][f"{season}_on"] = float(decimals[0])
                    results["rates"][f"{season}_off"] = float(decimals[1])
                    
                    print(f"    [Captured] {season.upper()}: Peak=${decimals[0]}, Off-Peak=${decimals[1]}")
            
            # Handle Tiered E-1 (Usually single values per line)
            elif "Tier 1" in line_clean and total_usage_count == 0:
                decimals = re.findall(r"(\d+\.\d{5})", line_clean)
                if decimals: results["rates"]["summer_off"] = float(decimals[-1])
            elif "Tier 2" in line_clean and total_usage_count == 0:
                decimals = re.findall(r"(\d+\.\d{5})", line_clean)
                if decimals: results["rates"]["summer_on"] = float(decimals[-1])

    return results

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_true = True # Placeholder for logic flow

    if not os.path.exists(JSON_FILE): return

    with open(JSON_FILE, 'r') as f:
        data = json.load(f)

    updated = False
    if not os.path.exists(UPLOAD_DIR): return
    
    for filename in os.listdir(UPLOAD_DIR):
        if not filename.lower().endswith(".pdf"): continue
        
        pdf_results = extract_pge_tariff_data(os.path.join(UPLOAD_DIR, filename))
        target_id = pdf_results["plan_id"]
        
        if target_id and target_id in data["plans"]:
            print(f"\n[Comparison Ledger: {target_id}]")
            
            # Update NBC
            if pdf_results["nbc_total"] > 0:
                old_nbc = data.get("nbcRate", 0)
                if abs(pdf_results["nbc_total"] - old_nbc) > 0.00001:
                    print(f"  [CHANGE] Global NBC: JSON={old_nbc:.5f} | PDF={pdf_results['nbc_total']:.5f}")
                    if "--dry-run" not in sys.argv:
                        data["nbcRate"] = pdf_results["nbc_total"]
                        updated = True

            # Update Bin Rates
            for key, val in pdf_results["rates"].items():
                season, bin_type = key.split('_')
                json_bin = "onPeak" if bin_type == "on" else "offPeak"
                
                # EV/ELEC adjustment: Off-Peak goes to superOffPeak
                if bin_type == "off" and any(x in target_id for x in ["EV", "ELEC"]):
                    json_bin = "superOffPeak"

                old_val = data["plans"][target_id][season].get(json_bin, 0)
                if abs(val - old_val) > 0.00001:
                    print(f"  [CHANGE DETECTED] {season} {json_bin}: JSON={old_val:.5f} | PDF={val:.5f}")
                    if "--dry-run" not in sys.argv:
                        data["plans"][target_id][season][json_bin] = val
                        updated = True

    if updated:
        data["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(JSON_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        print("\n>>> Success: JSON updated.")
    else:
        print("\n>>> No changes saved.")

if __name__ == "__main__":
    main()
