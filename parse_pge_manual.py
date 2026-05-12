import os
import re
import json
import argparse
import sys
import pdfplumber
from datetime import datetime

# --- CONFIGURATION ---
UPLOAD_DIR = "pge_uploads"
JSON_FILE = "pge_rates.json"

def extract_pge_tariff_data(pdf_path):
    """
    Parses official PG&E Residential Tariff Sheets.
    Includes a value-floor to prevent adjustments from overwriting bundled rates.
    """
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
                val = abs(float(match.group(1))) 
                nbc_sum += val
        results["nbc_total"] = nbc_sum

        # 3. Extract Rates
        lines = full_text.split('\n')
        total_usage_count = 0
        
        for line in lines:
            line_clean = line.strip()
            
            # PATTERN 1: 'Total Usage' (High Priority - usually correct)
            if line_clean.startswith("Total Usage"):
                decimals = re.findall(r"(\d+\.\d{5})", line_clean)
                if len(decimals) >= 2:
                    total_usage_count += 1
                    season = "summer" if total_usage_count == 1 else "winter"
                    results["rates"][f"{season}_on"] = float(decimals[0])
                    results["rates"][f"{season}_off"] = float(decimals[1])
                    print(f"    [Captured Total] {season.upper()}: Peak=${decimals[0]}, Off-Peak=${decimals[1]}")
            
            # PATTERN 2: 'Tiered' (E-1 specific fallback)
            elif "Tier 1" in line_clean or "Tier 2" in line_clean:
                # STRATEGIC FILTERS:
                # Skip if line mentions sub-components or adjustments
                if any(x in line_clean for x in ["Adjustment", "Income", "Credit", "Limiter", "Component"]):
                    continue
                
                decimals = re.findall(r"(\d+\.\d{5})", line_clean)
                if decimals:
                    rate_val = float(decimals[-1])
                    
                    # VALUE FLOOR: Bundled rates are > $0.20. Adjustments are usually < $0.05.
                    if rate_val < 0.20:
                        continue
                        
                    if "Tier 1" in line_clean:
                        # Only update if the new value is higher (ensures bundled > adjustment)
                        current = results["rates"].get("summer_off", 0)
                        if rate_val > current:
                            results["rates"]["summer_off"] = rate_val
                            results["rates"]["winter_off"] = rate_val
                            print(f"    [Captured Tier] E-1 TIER 1: {rate_val:.5f}")
                    elif "Tier 2" in line_clean:
                        current = results["rates"].get("summer_on", 0)
                        if rate_val > current:
                            results["rates"]["summer_on"] = rate_val
                            results["rates"]["winter_on"] = rate_val
                            print(f"    [Captured Tier] E-1 TIER 2: {rate_val:.5f}")

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
    if not os.path.exists(UPLOAD_DIR): return
    
    for filename in os.listdir(UPLOAD_DIR):
        if not filename.lower().endswith(".pdf"): continue
        
        pdf_results = extract_pge_tariff_data(os.path.join(UPLOAD_DIR, filename))
        target_id = pdf_results["plan_id"]
        
        if target_id and target_id in data["plans"]:
            print(f"\n[Comparison Ledger: {target_id}]")
            
            # 1. Update NBC
            if pdf_results["nbc_total"] > 0:
                old_nbc = data.get("nbcRate", 0)
                diff_nbc = abs(pdf_results["nbc_total"] - old_nbc)
                status = "[MATCH]" if diff_nbc < 0.00001 else "[CHANGE DETECTED]"
                print(f"  {status} Global NBC: JSON={old_nbc:.5f} | PDF={pdf_results['nbc_total']:.5f}")
                
                if diff_nbc > 0.00001 and not args.dry_run:
                    data["nbcRate"] = pdf_results["nbc_total"]
                    updated = True

            # 2. Update Bin Rates
            for key, val in pdf_results["rates"].items():
                season, bin_type = key.split('_')
                
                json_bin = "onPeak" if bin_type == "on" else "offPeak"
                if bin_type == "off" and any(x in target_id for x in ["EV", "ELEC"]):
                    json_bin = "superOffPeak"

                old_val = data["plans"][target_id][season].get(json_bin, 0)
                diff = abs(val - old_val)
                status = "[MATCH]" if diff < 0.00001 else "[CHANGE DETECTED]"
                
                print(f"  {status} {season} {json_bin}: JSON={old_val:.5f} | PDF={val:.5f}")
                
                if diff > 0.00001 and not args.dry_run:
                    data["plans"][target_id][season][json_bin] = val
                    updated = True

    if updated:
        data["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        if not args.dry_run:
            with open(JSON_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            print("\n>>> Success: JSON updated with high-precision data.")
        else:
            print("\n>>> Dry Run Complete: Changes detected but not saved.")
    else:
        print("\n>>> Result: No significant changes detected in PDF folder.")

if __name__ == "__main__":
    main()
