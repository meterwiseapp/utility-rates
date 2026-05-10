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
    # Match decimals like -0.10892 or 0.79343
    match = re.search(r"(-?\d+\.\d{3,6})", clean)
    return float(match.group(1)) if match else 0.0

def get_best_decimal_from_row(row):
    """Finds the 'Total Rate' by looking for the last valid rate decimal in the row."""
    # We iterate backwards to find the 'Total Electric Rate'
    for cell in reversed(row):
        val = extract_decimal(str(cell))
        # Valid rates are typically between 0.05 and 0.95
        if 0.01 < val < 2.0:
            return val
    return 0.0

def parse_sdge_pdf(pdf_path):
    print(f"\n[Analyzing PDF] {os.path.basename(pdf_path)}")
    
    results = {
        "plan_id": None,
        "is_tiered": False,
        "summer": {"on": None, "mid": None, "off": None},
        "winter": {"on": None, "mid": None, "off": None},
        "baseline_credit": None,
        "service_charge": None
    }

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        full_text = page.extract_text()
        
        # 1. Identify Plan ID
        plan_match = re.search(r"Schedule\s+([A-Z0-9-]+)", full_text)
        if plan_match:
            results["plan_id"] = plan_match.group(1)
            if results["plan_id"] == "DR": results["is_tiered"] = True
            print(f"  > Target: {results['plan_id']} (Tiered: {results['is_tiered']})")

        # 2. Hybrid Extraction: Table + Text RegEx
        # Some rows are missed by extract_table, so we scan the raw lines too
        lines = full_text.split('\n')
        current_season = None

        for line in lines:
            line_clean = line.strip()
            if not line_clean: continue

            # Update Season Context
            if "Summer" in line_clean: current_season = "summer"
            elif "Winter" in line_clean: current_season = "winter"

            if current_season:
                # Use RegEx to find the last decimal on lines containing our keywords
                # This bypasses table-cell alignment issues
                decimals = re.findall(r"\d+\.\d{4,5}", line_clean)
                if not decimals: continue
                
                # The total rate is almost always the last decimal on the line
                rate = float(decimals[-1])

                if results["is_tiered"]:
                    if "Tier 1" in line_clean or "Up to" in line_clean:
                        results[current_season]["on"] = rate
                        print(f"    [Text Match] {current_season} Tier 1: {rate}")
                    elif "Tier 2" in line_clean or "Above" in line_clean:
                        results[current_season]["mid"] = rate
                        results[current_season]["off"] = rate
                        print(f"    [Text Match] {current_season} Tier 2: {rate}")
                else:
                    # TOU logic
                    if "On-Peak" in line_clean: results[current_season]["on"] = rate
                    elif "Off-Peak" in line_clean: results[current_season]["mid"] = rate
                    elif "Super Off-Peak" in line_clean: results[current_season]["off"] = rate

            # Global Attribute Extraction
            if "Baseline Adjustment Credit" in line_clean:
                decimals = re.findall(r"\d+\.\d{4,5}", line_clean)
                if decimals: results["baseline_credit"] = abs(float(decimals[-1]))

            if "Base Services Charge" in line_clean and "$/Day" in line_clean:
                decimals = re.findall(r"\d+\.\d{4,5}", line_clean)
                if decimals: results["service_charge"] = float(decimals[-1])

    return results

def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run: print("!!! DRY RUN MODE !!!")

    if not os.path.exists(UPLOAD_DIR):
        print(f"Error: {UPLOAD_DIR} not found.")
        return

    try:
        with open(JSON_FILE, 'r') as f:
            data = json.load(f)
    except:
        print(f"Error: {JSON_FILE} not found.")
        sys.exit(1)

    overall_updated = False
    
    for filename in os.listdir(UPLOAD_DIR):
        if not filename.lower().endswith(".pdf"): continue
        
        pdf_data = parse_sdge_pdf(os.path.join(UPLOAD_DIR, filename))
        if not pdf_data["plan_id"]: continue
            
        plan_key = "Standard DR" if pdf_data["plan_id"] == "DR" else pdf_data["plan_id"]
        if plan_key not in data["plans"]:
            print(f"  [Skip] {plan_key} not in app dictionary.")
            continue

        p = data["plans"][plan_key]
        
        def update_val(category, bin_name, current_val, new_val):
            nonlocal overall_updated
            # Standard precision check
            if new_val is not None and new_val > 0.01 and abs(new_val - current_val) > 0.00001:
                print(f"    [CHANGE] {plan_key} {category} {bin_name}: {current_val} -> {new_val}")
                overall_updated = True
                return new_val
            return current_val

        # Apply logic
        p["dailyServiceCharge"] = update_val("Fixed", "Service Charge", p.get("dailyServiceCharge", 0), pdf_data["service_charge"])

        for season in ["summer", "winter"]:
            p[season]["onPeak"] = update_val(season.capitalize(), "Tier 1", p[season].get("onPeak"), pdf_data[season]["on"])
            p[season]["offPeak"] = update_val(season.capitalize(), "Tier 2", p[season].get("offPeak"), pdf_data[season]["mid"])
            p[season]["superOffPeak"] = update_val(season.capitalize(), "Tier 2", p[season].get("superOffPeak"), pdf_data[season]["off"])

        if pdf_data["baseline_credit"]:
            data["baselineCredit"] = update_val("Global", "Baseline Credit", data.get("baselineCredit"), pdf_data["baseline_credit"])

    if overall_updated:
        if not dry_run:
            data["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(JSON_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            print("\n>>> Success: JSON updated via PDF.")
        else:
            print("\n>>> Dry Run Complete: Changes detected but not saved.")
    else:
        print("\n>>> No changes detected (JSON matches PDF).")

if __name__ == "__main__":
    main()
