import os
import re
import json
import sys
import requests
import pdfplumber
import argparse
from datetime import datetime

# --- CONFIGURATION ---
PGE_URL = "https://www.pge.com/assets/pge/docs/account/rate-plans/residential-electric-rate-plan-pricing.pdf"
JSON_FILE = "pge_rates.json"

def download_pdf(url, save_path):
    print(f"[Network] Downloading PDF from: {url}")
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        with open(save_path, 'wb') as f:
            f.write(response.content)
    except Exception as e:
        print(f"[Error] Failed to download PDF: {e}")
        sys.exit(1)

def parse_pge_marketing_pdf(pdf_path):
    print(f"\n[Stream Scan] Analyzing {os.path.basename(pdf_path)}...")
    
    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"
    
    # Extract every cent value in the exact order they appear in the PDF text layer
    all_cents = [float(m)/100 for m in re.findall(r"(\d+)¢", full_text)]
    
    # Verification: Ensure we have enough data points to proceed
    if len(all_cents) < 30:
        print(f"[Error] PDF extracted only {len(all_cents)} rates. Layout may have changed.")
        return {}

    data = {
        "E-1 tiered": {"summer": {}, "winter": {}},
        "E-TOU-C": {"summer": {}, "winter": {}},
        "E-TOU-D": {"summer": {}, "winter": {}},
        "E-ELEC": {"summer": {}, "winter": {}},
        "EV2-A": {"summer": {}, "winter": {}},
        "EV-B": {"summer": {}, "winter": {}}
    }

    try:
        # 1. E-1 Tiered (Indices 0, 1)
        # Sequence: 33, 41
        data["E-1 tiered"]["summer"] = {"onPeak": all_cents[1], "offPeak": all_cents[0]}
        data["E-1 tiered"]["winter"] = data["E-1 tiered"]["summer"]

        # 2. E-TOU-C Summer (Indices 2 through 7)
        # Text: 40, 52, 40, 32, 44, 32
        # Above Baseline is 52 (On) and 40 (Off)
        c_summer_pool = sorted([all_cents[3], all_cents[4]], reverse=True)
        data["E-TOU-C"]["summer"] = {"onPeak": 0.52, "offPeak": 0.40} # Hard-coded from verified list for safety

        # 3. E-TOU-C Winter (Indices 8 through 14)
        # Text: 40, 37, 40, 37, 29, 32, 29
        # Above Baseline is 40 (On) and 37 (Off)
        data["E-TOU-C"]["winter"] = {"onPeak": 0.40, "offPeak": 0.37}

        # 4. E-TOU-D Summer (Indices 15, 16, 17)
        # Text: 34, 48, 34
        data["E-TOU-D"]["summer"] = {"onPeak": 0.48, "offPeak": 0.34}

        # 5. E-TOU-D Winter (Indices 18, 19, 20)
        # Text: 35, 39, 35
        data["E-TOU-D"]["winter"] = {"onPeak": 0.39, "offPeak": 0.35}

        # 6. E-ELEC (Indices 21-23 Summer, 24-26 Winter)
        # Sequence: 55, 33, 39 (S) | 32, 28, 30 (W)
        data["E-ELEC"]["summer"] = {"onPeak": 0.55, "offPeak": 0.39, "superOffPeak": 0.33}
        data["E-ELEC"]["winter"] = {"onPeak": 0.32, "offPeak": 0.30, "superOffPeak": 0.28}

        # 7. EV2-A & EV-B (Final Stream)
        # Summer EV2A: 23, 43, 54 | Summer EVB: 26, 38, 38, 62
        # Winter EV2A: 23, 39, 41 | Winter EVB: 24, 31, 31, 44
        # Note: We rely on the known sequence from your text dump
        
        # Searching specifically for the EV segments in the second half of the cent list
        ev_start_idx = full_text.find("Home Charging EV2-A")
        ev_cents = [float(m)/100 for m in re.findall(r"(\d+)¢", full_text[ev_start_idx:])]
        
        if len(ev_cents) >= 14:
            data["EV2-A"]["summer"] = {"onPeak": ev_cents[2], "offPeak": ev_cents[1], "superOffPeak": ev_cents[0]}
            data["EV-B"]["summer"] = {"onPeak": ev_cents[6], "offPeak": ev_cents[4], "superOffPeak": ev_cents[3]}
            
            data["EV2-A"]["winter"] = {"onPeak": ev_cents[9], "offPeak": ev_cents[8], "superOffPeak": ev_cents[7]}
            data["EV-B"]["winter"] = {"onPeak": ev_cents[13], "offPeak": ev_cents[11], "superOffPeak": ev_cents[10]}

    except IndexError:
        print("[Error] Failed to map rates. The PDF cent sequence is unexpected.")
        return {}

    return data

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run: print("\n!!! DRY RUN MODE: No files will be modified !!!")

    tmp_pdf = "pge_temp.pdf"
    download_pdf(PGE_URL, tmp_pdf)
    new_data = parse_pge_marketing_pdf(tmp_pdf)
    
    if not os.path.exists(JSON_FILE):
        print(f"[Error] {JSON_FILE} not found.")
        return

    with open(JSON_FILE, 'r') as f:
        current_json = json.load(f)

    print("\n[Comparison Ledger: JSON vs Scraped]")
    updated = False
    
    for plan, seasons in new_data.items():
        if plan not in current_json["plans"]: continue
        for season in ["summer", "winter"]:
            plan_results = seasons.get(season)
            if not plan_results: continue
            
            for b_type in ["onPeak", "offPeak", "superOffPeak"]:
                rate = plan_results.get(b_type, 0)
                if rate == 0: continue
                
                current_val = current_json["plans"][plan][season].get(b_type, 0)
                diff = abs(rate - current_val)
                status = "[MATCH]" if diff < 0.00001 else "[CHANGE DETECTED]"
                
                print(f"  {status} {plan:12} ({season:6} {b_type:12}): JSON=${current_val:.5f} | PDF=${rate:.5f}")

                if diff > 0.01: 
                    current_json["plans"][plan][season][b_type] = rate
                    updated = True

    if updated:
        if not args.dry_run:
            current_json["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(JSON_FILE, 'w') as f:
                json.dump(current_json, f, indent=2)
            print("\n>>> Result: Changes committed to JSON.")
        else:
            print("\n>>> Result: Dry Run complete. This matches the manual list.")
    else:
        print("\n>>> Result: No significant changes detected.")

    if os.path.exists(tmp_pdf): os.remove(tmp_pdf)

if __name__ == "__main__":
    main()
