import os
import re
import json
import requests
import pdfplumber
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
        "E-1 tiered": {"summer": {"on": 0.0, "mid": 0.0, "off": 0.0}, "winter": {"on": 0.0, "mid": 0.0, "off": 0.0}},
        "E-TOU-C": {"summer": {"on": 0.0, "mid": 0.0, "off": 0.0}, "winter": {"on": 0.0, "mid": 0.0, "off": 0.0}},
        "E-TOU-D": {"summer": {"on": 0.0, "mid": 0.0, "off": 0.0}, "winter": {"on": 0.0, "mid": 0.0, "off": 0.0}},
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
                # Regex to find "XX¢" patterns
                cents_matches = re.findall(r"(\d+)¢", line)
                if not cents_matches: continue
                
                # Logic: Identify the plan by string matching on the same line
                if "Tier 1" in line and "E-1" in line:
                    val = float(cents_matches[0]) / 100
                    extracted_data["E-1 tiered"]["summer"]["on"] = val
                    extracted_data["E-1 tiered"]["winter"]["on"] = val
                
                elif "Peak" in line and "4–9 p.m." in line:
                    val = float(cents_matches[0]) / 100
                    # Standard E-TOU-C Peak
                    extracted_data["E-TOU-C"]["summer"]["on"] = val
                
                elif "Off-Peak" in line and "E-TOU-C" in line:
                    val = float(cents_matches[0]) / 100
                    extracted_data["E-TOU-C"]["summer"]["off"] = val

    return extracted_data

def update_pge_json(new_data):
    if not os.path.exists(JSON_FILE):
        print("Error: pge_rates.json not found.")
        return

    with open(JSON_FILE, 'r') as f:
        current_json = json.load(f)

    updated = False
    for plan, seasons in new_data.items():
        if plan in current_json["plans"]:
            for season, bins in seasons.items():
                for bin_type, rate in bins.items():
                    if rate > 0:
                        # Safety: Only overwrite if the change is significant 
                        # This protects high-precision manual data from rounded marketing data
                        current_rate = current_json["plans"][plan][season].get(bin_type, 0)
                        if abs(rate - current_rate) > 0.01:
                            print(f"  [Update] {plan} {season} {bin_type}: {current_rate} -> {rate}")
                            current_json["plans"][plan][season][bin_type] = rate
                            updated = True

    if updated:
        current_json["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(JSON_FILE, 'w') as f:
            json.dump(current_json, f, indent=2)
        print("JSON Updated successfully via Web Scraper.")
    else:
        print("No significant rate changes detected in marketing PDF.")

if __name__ == "__main__":
    tmp_pdf = "pge_temp.pdf"
    download_pdf(PGE_URL, tmp_pdf)
    data = parse_pge_marketing_pdf(tmp_pdf)
    update_pge_json(data)
    if os.path.exists(tmp_pdf): os.remove(tmp_pdf)
