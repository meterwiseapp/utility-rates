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
    """
    Parses official PG&E Residential Tariff Sheets with detailed logging.
    """
    results = {"plan_id": None, "rates": {}, "nbc_total": 0.0}
    nbc_components = {}
    
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
        else:
            print("  [!] Warning: No Schedule (e.g. E-TOU-C) found in text.")

        # 2. Extract NBC Components (Search for individual pieces)
        # PG&E Tariff Table patterns
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
                nbc_components[name] = val
                nbc_sum += val
                print(f"    [NBC Match] {name}: {val:.5f}")
        
        results["nbc_total"] = nbc_sum
        print(f"    [NBC Result] Calculated Total: {nbc_sum:.5f}")

        # 3. Extract Total Bundled Rates (Line-by-line Scan)
        lines = full_text.split('\n')
        current_season = "summer" 
        
        print("  > Scanning Rate Table for 'Total Bundled' lines...")
        for line in lines:
            line_clean = line.strip()
            
            # Detect Season Shifts
            if "Winter" in line_clean: 
                current_season = "winter"
                # print(f"    [Context] Switched to WINTER")
            elif "Summer" in line_clean: 
                current_season = "summer"
                # print(f"    [Context] Switched to SUMMER")
            
            # Look for "Total Bundled" which is the sum of all components
            if "Total Bundled" in line_clean:
                # Find all 5-decimal numbers on the line
                decimals = re.findall(r"(\d+\.\d{5})", line_clean)
                if decimals:
                    rate_val = float(decimals[-1]) # Usually the last column
                    
                    # Logic mapping based on keywords on the SAME line
                    if "Peak" in line_clean and "Off" not in line_clean and "Part" not in line_clean:
                        key = f"{current_season}_on"
                        results["rates"][key] = rate_val
                        print(f"    [Rate Match] {key.upper()}: {rate_val:.5f} (from: '{line_clean[:40]}...')")
                    elif "Off-Peak" in line_clean:
                        key = f"{current_season}_off"
                        results["rates"][key] = rate_val
                        print(f"    [Rate Match] {key.upper()}: {rate_val:.5f}")
                    elif "Part-Peak" in line_clean or "Partial-Peak" in line_clean:
                        key = f"{current_season}_mid"
                        results["rates"][key] = rate_val
                        print(f"    [Rate Match] {key.upper()}: {rate_val:.5f}")
                    elif "Tier 1" in line_clean:
                        results["rates"][f"{current_season}_off"] = rate_val # Map T1 to Off for E-1
                        print(f"    [Rate Match] E-1 TIER 1: {rate_val:.5f}")
                    elif "Tier 2" in line_clean:
                        results["rates"][f"{current_season}_on"] = rate_val # Map T2 to On for E-1
                        print(f"    [Rate Match] E-1 TIER 2: {rate_val:.5f}")

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
