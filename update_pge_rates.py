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
        print(f"[Network] Download complete ({len(response.content)} bytes)")
    except Exception as e:
        print(f"[Error] Failed to download PDF: {e}")
        sys.exit(1)

def extract_rates_from_chunk(text):
    """Finds all XX¢ patterns in a text block and returns them as sorted floats (dollars)"""
    matches = re.findall(r"(\d+)¢", text)
    rates = sorted([float(m) / 100 for m in matches], reverse=True)
    return rates

def parse_pge_marketing_pdf(pdf_path):
    print(f"\n[Scanning PDF Content] {os.path.basename(pdf_path)}")
    
    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

    # Define how to find the start of each plan's data block
    plan_markers = {
        "E-1 tiered": "Tiered Rate Plan (E-1)",
        "E-TOU-C": "Time-of-Use (E-TOU-C)",
        "E-TOU-D": "Time-of-Use (E-TOU-D)",
        "E-ELEC": "Electric Home Rate Plan (E-ELEC)",
        "EV2-A": "Home Charging EV2-A",
        "EV-B": "Electric Vehicle Rate Plan EV-B"
    }

    results = {}

    # Iterate through plans and find their specific blocks of text
    plan_keys = list(plan_markers.keys())
    for i, key in enumerate(plan_keys):
        start_marker = plan_markers[key]
        start_idx = full_text.find(start_marker)
        
        if start_idx == -1:
            print(f"  [Warn] Could not find marker for {key}")
            continue
            
        # The block ends where the next plan starts, or at the end of the file
        end_idx = len(full_text)
        if i + 1 < len(plan_keys):
            next_marker = plan_markers[plan_keys[i+1]]
            found_next = full_text.find(next_marker)
            if found_next != -1: end_idx = found_next
            
        plan_block = full_text[start_idx:end_idx]
        
        # Split block into Summer and Winter
        summer_idx = plan_block.find("Summer")
        winter_idx = plan_block.find("Winter")
        
        results[key] = {"summer": {}, "winter": {}}
        
        # Extract Summer Rates
        if summer_idx != -1:
            s_end = winter_idx if winter_idx > summer_idx else len(plan_block)
            s_rates = extract_rates_from_chunk(plan_block[summer_idx:s_end])
            if s_rates:
                results[key]["summer"]["onPeak"] = s_rates[0] # Highest
                if len(s_rates) >= 2: results[key]["summer"]["offPeak"] = s_rates[1]
                if len(s_rates) >= 3: results[key]["summer"]["superOffPeak"] = s_rates[-1] # Lowest

        # Extract Winter Rates
        if winter_idx != -1:
            w_start = winter_idx
            w_rates = extract_rates_from_chunk(plan_block[w_start:])
            if w_rates:
                results[key]["winter"]["onPeak"] = w_rates[0]
                if len(w_rates) >= 2: results[key]["winter"]["offPeak"] = w_rates[1]
                if len(w_rates) >= 3: results[key]["winter"]["superOffPeak"] = w_rates[-1]

    return results

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
            # Standardizing bins to check against JSON
            bins_to_check = ["onPeak", "offPeak", "superOffPeak"]
            
            for b_type in bins_to_check:
                rate = seasons[season].get(b_type, 0)
                if rate == 0: continue
                
                current_val = current_json["plans"][plan][season].get(b_type, 0)
                diff = abs(rate - current_val)
                
                status = "[MATCH]" if diff < 0.00001 else "[CHANGE DETECTED]"
                print(f"  {status} {plan:12} ({season:6} {b_type:12}): JSON=${current_val:.5f} | PDF=${rate:.5f} | Delta=${diff:.5f}")

                # Threshold: Marketing PDF only has 2-decimal precision (cents).
                # We only update if the difference is more than 1 cent to avoid 
                # overwriting high-precision manual data with rounded marketing data.
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
            print("\n>>> Result: Dry Run complete. Changes were found but NOT saved.")
    else:
        print("\n>>> Result: No significant changes (Delta > $0.01) detected.")

    if os.path.exists(tmp_pdf): os.remove(tmp_pdf)

if __name__ == "__main__":
    main()
