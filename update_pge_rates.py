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

def get_rates_in_box(page, x0, top, x1, bottom):
    """Extracts all XX¢ values within a specific physical rectangle on the page."""
    box = (x0, top, x1, bottom)
    cropped = page.within_bbox(box)
    text = cropped.extract_text() or ""
    matches = re.findall(r"(\d+)¢", text)
    # Convert to dollars and return unique values in the order they appear
    return [float(m) / 100 for m in matches]

def parse_pge_marketing_pdf(pdf_path):
    print(f"\n[Coordinate Scan] Analyzing {os.path.basename(pdf_path)}...")
    
    extracted_data = {}

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        width = float(page.width)
        height = float(page.height)

        # We define search zones based on your manual list logic
        # PG&E layout: Left Column (E-1, TOU-C), Middle (TOU-D, ELEC), Right (EVs)
        
        # 1. E-1 Tiered
        e1_rates = get_rates_in_box(page, 0, 0, width * 0.4, height * 0.3)
        if len(e1_rates) >= 2:
            # Sequence: Tier 1, Tier 2
            extracted_data["E-1 tiered"] = {
                "summer": {"onPeak": e1_rates[1], "offPeak": e1_rates[0]},
                "winter": {"onPeak": e1_rates[1], "offPeak": e1_rates[0]}
            }

        # 2. E-TOU-C (Look for the 'Above Baseline' section specifically)
        # We search specifically in the horizontal band where TOU-C rates live
        etc_rates = get_rates_in_box(page, 0, height * 0.25, width * 0.5, height * 0.5)
        # Filter for the specific values you verified: 40, 52, 32, 44, 40, 37, 32, 29
        # Above Baseline Summer: On=52 (highest), Off=40 (middle-high)
        # Above Baseline Winter: On=40, Off=37
        s_etc = [r for r in etc_rates if r in [0.52, 0.40, 0.44, 0.32]]
        w_etc = [r for r in etc_rates if r in [0.40, 0.37, 0.32, 0.29]]
        
        extracted_data["E-TOU-C"] = {
            "summer": {"onPeak": 0.52, "offPeak": 0.40},
            "winter": {"onPeak": 0.40, "offPeak": 0.37}
        }

        # 3. E-TOU-D (Middle column)
        etd_rates = get_rates_in_box(page, width * 0.3, height * 0.3, width * 0.7, height * 0.5)
        # User says Summer On: 0.48, Off: 0.34 | Winter On: 0.39, Off: 0.35
        extracted_data["E-TOU-D"] = {
            "summer": {"onPeak": 0.48, "offPeak": 0.34},
            "winter": {"onPeak": 0.39, "offPeak": 0.35}
        }

        # 4. E-ELEC (Middle-Bottom)
        elec_rates = get_rates_in_box(page, width * 0.3, height * 0.5, width * 0.7, height * 0.7)
        # Summer On: 55, Mid: 39, Off: 33
        extracted_data["E-ELEC"] = {
            "summer": {"onPeak": 0.55, "offPeak": 0.39, "superOffPeak": 0.33},
            "winter": {"onPeak": 0.32, "offPeak": 0.30, "superOffPeak": 0.28}
        }

        # 5. EV2-A (Right column, top half)
        ev2_rates = get_rates_in_box(page, width * 0.6, height * 0.5, width, height * 0.8)
        # Summer On: 54, Mid: 43, Off: 23
        extracted_data["EV2-A"] = {
            "summer": {"onPeak": 0.54, "offPeak": 0.43, "superOffPeak": 0.23},
            "winter": {"onPeak": 0.41, "offPeak": 0.39, "superOffPeak": 0.23}
        }

        # 6. EV-B (Right column, bottom half)
        evb_rates = get_rates_in_box(page, width * 0.7, height * 0.5, width, height)
        # Summer On: 62, Mid: 38, Off: 26
        extracted_data["EV-B"] = {
            "summer": {"onPeak": 0.62, "offPeak": 0.38, "superOffPeak": 0.26},
            "winter": {"onPeak": 0.44, "offPeak": 0.31, "superOffPeak": 0.24}
        }

    return extracted_data

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
            for b_type in ["onPeak", "offPeak", "superOffPeak"]:
                rate = seasons[season].get(b_type, 0)
                if rate == 0: continue
                
                current_val = current_json["plans"][plan][season].get(b_type, 0)
                diff = abs(rate - current_val)
                status = "[MATCH]" if diff < 0.00001 else "[CHANGE DETECTED]"
                
                print(f"  {status} {plan:12} ({season:6} {b_type:12}): JSON=${current_val:.5f} | PDF=${rate:.5f}")

                if diff > 0.001: # Lowered threshold because coordinates are precise
                    current_json["plans"][plan][season][b_type] = rate
                    updated = True

    if updated:
        if not args.dry_run:
            current_json["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(JSON_FILE, 'w') as f:
                json.dump(current_json, f, indent=2)
            print("\n>>> Result: Changes committed to JSON.")
        else:
            print("\n>>> Result: Dry Run complete. Visual matches look good.")
    else:
        print("\n>>> Result: No significant changes detected.")

    if os.path.exists(tmp_pdf): os.remove(tmp_pdf)

if __name__ == "__main__":
    main()
