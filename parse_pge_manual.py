import os
import re
import json
import argparse
import pdfplumber
from datetime import datetime

# --- CONFIGURATION ---
UPLOAD_DIR = "pge_uploads"
JSON_FILE = "pge_rates.json"

def extract_pge_precision_rates(pdf_path):
    # Searches for 5-decimal precision (e.g., 0.12345)
    results = {"plan_id": None, "rates": {}, "nbc_total": 0.0}
    
    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"
        
        # 1. Identify the Schedule
        # Looks for "Schedule E-TOU-C", "Schedule EV-B", etc.
        plan_match = re.search(r"Schedule\s+(E-[A-Z0-9-]+|EV-[A-Z]|EV2-A)", full_text, re.IGNORECASE)
        if plan_match:
            results["plan_id"] = plan_match.group(1).upper()
            print(f"  > Detected Plan: {results['plan_id']}")

        # 2. Extract NBC Components (The "Unbundled" math)
        # PG&E lists these as specific line items. We sum them for the App's NBC floor.
        ppp = re.search(r"Public Purpose Programs.*?(\d+\.\d{5})", full_text)
        nd = re.search(r"Nuclear Decommissioning.*?(\d+\.\d{5})", full_text)
        wf = re.search(r"Wildfire Fund Charge.*?(\d+\.\d{5})", full_text)
        
        if ppp: results["nbc_total"] += float(ppp.group(1))
        if nd: results["nbc_total"] += float(nd.group(1))
        if wf: results["nbc_total"] += float(wf.group(1))

        # 3. Extract Total Bundled Rates
        # These appear in the far right column of PG&E tariff tables
        # Pattern: Peak/Off-Peak labels followed by the rate
        bundled_matches = re.findall(r"(?:Peak|Off-Peak|Part-Peak).*?(\d+\.\d{5})", full_text)
        if bundled_matches:
            # Note: We rely on the order in the PDF (Usually On, Part, Off)
            if len(bundled_matches) >= 2:
                results["rates"]["on"] = float(bundled_matches[0])
                results["rates"]["off"] = float(bundled_matches[-1])
            if len(bundled_matches) == 3:
                results["rates"]["mid"] = float(bundled_matches[1])

    return results

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(UPLOAD_DIR): os.makedirs(UPLOAD_DIR)
    
    with open(JSON_FILE, 'r') as f:
        data = json.load(f)

    updated = False
    for filename in os.listdir(UPLOAD_DIR):
        if not filename.lower().endswith(".pdf"): continue
        
        print(f"[Processing Tariff] {filename}")
        p_data = extract_pge_precision_rates(os.path.join(UPLOAD_DIR, filename))
        
        plan_id = p_data["plan_id"]
        if plan_id and plan_id in data["plans"]:
            # Update NBC if found
            if p_data["nbc_total"] > 0:
                old_nbc = data.get("nbcRate", 0)
                if abs(p_data["nbc_total"] - old_nbc) > 0.00001:
                    print(f"    [CHANGE] Global NBC Rate: {old_nbc} -> {p_data['nbc_total']}")
                    data["nbcRate"] = p_data["nbc_total"]
                    updated = True

            # Update Plan Rates (Assuming Summer as the primary update target)
            for bin_name, rate in p_data["rates"].items():
                json_key = {"on": "onPeak", "mid": "offPeak", "off": "superOffPeak"}.get(bin_name, "offPeak")
                old_rate = data["plans"][plan_id]["summer"].get(json_key, 0)
                
                if abs(rate - old_rate) > 0.00001:
                    print(f"    [CHANGE] {plan_id} {json_key}: {old_rate} -> {rate}")
                    data["plans"][plan_id]["summer"][json_key] = rate
                    updated = True

    if updated and not args.dry_run:
        data["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(JSON_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        print("\n>>> Success: JSON updated with high-precision data.")
    elif updated and args.dry_run:
        print("\n>>> Dry Run Complete: Changes detected but not saved.")
    else:
        print("\n>>> No changes detected in tariff PDFs.")

if __name__ == "__main__":
    main()
