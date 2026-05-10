import os
import re
import json
import sys
import pdfplumber
from datetime import datetime

# --- CONFIGURATION ---
UPLOAD_DIR = "sdge_uploads"
JSON_FILE = "sdge_rates.json"

def extract_decimal(text):
    if not text: return 0.0
    clean = text.replace('$', '').replace(',', '').strip()
    if '(' in clean and ')' in clean:
        clean = "-" + clean.replace('(', '').replace(')', '')
    match = re.search(r"(-?\d+\.\d{3,6})", clean)
    return float(match.group(1)) if match else 0.0

def parse_sdge_pdf(pdf_path):
    print(f"\n[Analyzing PDF] {os.path.basename(pdf_path)}")
    
    results = {
        "plan_id": None,
        "is_tiered": False,
        "summer": {"on": None, "mid": None, "off": None},
        "winter": {"on": None, "mid": None, "off": None},
        "baseline_credit": None,
        "service_charge": None,
        "service_charge_reduced": None 
    }

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        full_text = page.extract_text()
        
        # 1. Identify Plan ID
        plan_match = re.search(r"Schedule\s+([A-Z0-9-]+)", full_text)
        if plan_match:
            results["plan_id"] = plan_match.group(1)
            if results["plan_id"] == "DR": results["is_tiered"] = True
            print(f"  > Target Plan: {results['plan_id']}")

        # 2. Block-Aware Parsing
        lines = full_text.split('\n')
        current_season = None

        for line in lines:
            line_clean = line.strip()
            if not line_clean: continue

            # Track Season context
            if "Summer" in line_clean: current_season = "summer"
            elif "Winter" in line_clean: current_season = "winter"

            if current_season:
                # Find all 5-decimal numbers on the line
                # On SDGE sheets, the Totals are always at the very end of the list
                decimals = re.findall(r"\d+\.\d{5}", line_clean)
                
                if decimals:
                    # CASE A: Standard DR (Tiered) - Look for 2 tiers
                    if results["is_tiered"]:
                        if "Tier 1" in line_clean or "Up to" in line_clean:
                            results[current_season]["on"] = float(decimals[-1])
                            print(f"    [Extracted] {current_season} Tier 1: {decimals[-1]}")
                        elif "Tier 2" in line_clean or "Above" in line_clean:
                            val = float(decimals[-1])
                            results[current_season]["mid"] = val
                            results[current_season]["off"] = val
                            print(f"    [Extracted] {current_season} Tier 2: {val}")
                    
                    # CASE B: TOU Plans - Look for 3-bin block
                    # In your text dump, On, Off, and Super-Off totals appear together at the end
                    elif "On-Peak" in line_clean and "Super Off-Peak" in line_clean:
                        if len(decimals) >= 3:
                            # Mapping based on your text dump order (On, Off, Super)
                            results[current_season]["on"] = float(decimals[-3])
                            results[current_season]["mid"] = float(decimals[-2])
                            results[current_season]["off"] = float(decimals[-1])
                            print(f"    [Extracted] {current_season} Block: On:{decimals[-3]} Mid:{decimals[-2]} Off:{decimals[-1]}")
                    
                    # CASE C: TOU Individual lines (fallback if not blocked)
                    elif "On-Peak" in line_clean:
                        results[current_season]["on"] = float(decimals[-1])
                        print(f"    [Extracted] {current_season} On-Peak: {decimals[-1]}")
                    elif "Super Off-Peak" in line_clean:
                        results[current_season]["off"] = float(decimals[-1])
                        print(f"    [Extracted] {current_season} Super Off-Peak: {decimals[-1]}")
                    elif "Off-Peak" in line_clean:
                        results[current_season]["mid"] = float(decimals[-1])
                        print(f"    [Extracted] {current_season} Off-Peak: {decimals[-1]}")

            # 3. FIXED CHARGE LOGIC (Updated for Reduced Rates)
            if "Base Services Charge" in line_clean:
                # Find the rate at the end of the line
                decimals = re.findall(r"\d+\.\d{5}", line_clean)
                if decimals:
                    val = float(decimals[-1])
                    if "DRAH" in line_clean or "FERA" in line_clean:
                        results["service_charge_reduced"] = val
                        print(f"    [Extracted] Reduced Svc Charge: {val}")
                    else:
                        results["service_charge"] = val
                        print(f"    [Extracted] Standard Svc Charge: {val}")

            # 4. BASELINE CREDIT
            if "Baseline Adjustment Credit" in line_clean:
                # Capture values in parentheses like (0.10892)
                credit_match = re.findall(r"\(?\d+\.\d{5}\)?", line_clean)
                if credit_match:
                    results["baseline_credit"] = abs(extract_decimal(credit_match[-1]))
                    print(f"    [Extracted] Baseline Credit: {results['baseline_credit']}")

    return results

def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run: print("!!! DRY RUN MODE ACTIVE !!!")

    if not os.path.exists(UPLOAD_DIR): return

    try:
        with open(JSON_FILE, 'r') as f:
            data = json.load(f)
    except:
        sys.exit(1)

    overall_updated = False
    
    for filename in os.listdir(UPLOAD_DIR):
        if not filename.lower().endswith(".pdf"): continue
        pdf_data = parse_sdge_pdf(os.path.join(UPLOAD_DIR, filename))
        
        raw_id = pdf_data["plan_id"]
        if not raw_id: continue
        plan_key = "Standard DR" if raw_id == "DR" else raw_id
        if plan_key not in data["plans"]: continue

        p = data["plans"][plan_key]
        
        def update_val(category, bin_name, current_val, new_val):
            nonlocal overall_updated
            if new_val is not None and new_val > 0.0 and abs(new_val - (current_val or 0)) > 0.00001:
                print(f"    [CHANGE] {plan_key} {category} {bin_name}: {current_val} -> {new_val}")
                overall_updated = True
                return new_val
            return current_val

        # Map to JSON slots
        p["dailyServiceCharge"] = update_val("Fixed", "Std Svc Charge", p.get("dailyServiceCharge"), pdf_data["service_charge"])
        p["dailyServiceChargeLowIncome"] = update_val("Fixed", "Reduced Svc Charge", p.get("dailyServiceChargeLowIncome"), pdf_data["service_charge_reduced"])

        for s in ["summer", "winter"]:
            p[s]["onPeak"] = update_val(s.capitalize(), "On/T1", p[s].get("onPeak"), pdf_data[s]["on"])
            p[s]["offPeak"] = update_val(s.capitalize(), "Off/T2", p[s].get("offPeak"), pdf_data[s]["mid"])
            p[s]["superOffPeak"] = update_val(s.capitalize(), "SuperOff/T2", p[s].get("superOffPeak"), pdf_data[s]["off"])

        if pdf_data["baseline_credit"]:
            data["baselineCredit"] = update_val("Global", "Baseline Credit", data.get("baselineCredit"), pdf_data["baseline_credit"])

    if overall_updated:
        if not dry_run:
            data["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(JSON_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            print("\n>>> Success: JSON updated.")
        else:
            print("\n>>> Dry Run Complete: Changes detected but not saved.")
    else:
        print("\n>>> No changes detected between PDF and JSON.")

if __name__ == "__main__":
    main()
