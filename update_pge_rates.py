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
    response = requests.get(url, timeout=20)
    with open(save_path, 'wb') as f:
        f.write(response.content)

def parse_pge_marketing_pdf(pdf_path):
    print(f"[Analyzing Marketing PDF] {os.path.basename(pdf_path)}")
    
    extracted_data = {
        "E-1 tiered": {"summer": {"on": 0.0}, "winter": {"on": 0.0}},
        "E-TOU-C": {"summer": {"on": 0.0, "off": 0.0}, "winter": {"on": 0.0, "off": 0.0}},
        "E-TOU-D": {"summer": {"on": 0.0, "off": 0.0}, "winter": {"on": 0.0, "off": 0.0}},
        "E-ELEC": {"summer": {"on": 0.0, "mid": 0.0, "off": 0.0}, "winter": {"on": 0.0, "mid": 0.0, "off": 0.0}},
        "EV2-A": {"summer": {"on": 0.0, "mid": 0.0, "off": 0.0}, "winter": {"on": 0.0, "mid": 0.0, "off": 0.0}},
        "EV-B": {"summer": {"on": 0.0, "mid": 0.0, "off": 0.0}, "winter": {"on": 0.0, "mid": 0.0, "off": 0.0}}
    }

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            lines = text.split('\n')
            for line in lines:
                cents_matches = re.findall(r"(\d+)¢", line)
                if not cents_matches: continue
                
                val = float(cents_matches[0]) / 100
                
                if "Tier 1" in line and "E-1" in line:
                    extracted_data["E-1 tiered"]["summer"]["on"] = val
                    extracted_data["E-1 tiered"]["winter"]["on"] = val
                elif "Peak" in line and "4–9 p.m." in line:
                    extracted_data["E-TOU-C"]["summer"]["on"] = val
                elif "Off-Peak" in line and "E-TOU-C" in line:
                    extracted_data["E-TOU-C"]["summer"]["off"] = val
                elif "Electrification" in line or "E-ELEC" in line:
                    if "Peak" in line: extracted_data["E-ELEC"]["summer"]["on"] = val
                elif "EV2-A" in line and "Peak" in line:
                    extracted_data["EV2-A"]["summer"]["on"] = val

    return extracted_data

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Process data but do not save to JSON")
    args = parser.parse_args()

    if args.dry_run: print("!!! DRY RUN MODE: No files will be modified !!!")

    tmp_pdf = "pge_temp.pdf"
    download_pdf(PGE_URL, tmp_pdf)
    new_data = parse_pge_marketing_pdf(tmp_pdf)
    
    with open(JSON_FILE, 'r') as f:
        current_json = json.load(f)

    updated = False
    for plan, seasons in new_data.items():
        if plan in current_json["plans"]:
            for season, bins in seasons.items():
                for bin_type, rate in bins.items():
                    # Map 'on' to 'onPeak', etc. to match JSON keys
                    json_key = bin_type + "Peak" if bin_type != "mid" else "offPeak" # Handling mapping differences
                    if bin_type == "on": json_key = "onPeak"
                    elif bin_type == "off": json_key = "offPeak"
                    elif bin_type == "mid": json_key = "offPeak" # PG&E varies terminology

                    if rate > 0:
                        current_rate = current_json["plans"][plan][season].get(json_key, 0)
                        if abs(rate - current_rate) > 0.01:
                            print(f"  [CHANGE] {plan} {season} {json_key}: {current_rate} -> {rate}")
                            current_json["plans"][plan][season][json_key] = rate
                            updated = True

    if updated and not args.dry_run:
        current_json["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(JSON_FILE, 'w') as f:
            json.dump(current_json, f, indent=2)
        print(">>> Success: JSON updated.")
    elif updated and args.dry_run:
        print(">>> Dry Run: Changes detected but not saved.")
    else:
        print(">>> No significant changes detected.")

    if os.path.exists(tmp_pdf): os.remove(tmp_pdf)

if __name__ == "__main__":
    main()
