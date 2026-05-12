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

def map_rates_to_bins(rates, plan_id):
    """
    Uses domain logic: In CA, On-Peak is ALWAYS the highest rate.
    """
    # Remove duplicates and sort descending
    unique_rates = sorted(list(set(rates)), reverse=True)
    
    if not unique_rates:
        return None

    # Handle 3-bin plans (EV, ELEC)
    if any(x in plan_id for x in ["ELEC", "EV"]):
        # EV and ELEC usually have 3 distinct rates
        if len(unique_rates) >= 3:
            return {
                "onPeak": unique_rates[0],
                "offPeak": unique_rates[1],
                "superOffPeak": unique_rates[2]
            }
        # Fallback if only 2 unique found
        return {
            "onPeak": unique_rates[0],
            "offPeak": unique_rates[-1],
            "superOffPeak": unique_rates[-1]
        }

    # Handle 2-bin plans (E-1, TOU-C, TOU-D)
    # Note: For TOU-C/D, unique_rates will often contain 4 values 
    # (Above/Below baseline combos). We take the highest pair for accuracy.
    if len(unique_rates) >= 2:
        return {
            "onPeak": unique_rates[0],
            "offPeak": unique_rates[1]
        }
    
    return {"onPeak": unique_rates[0], "offPeak": unique_rates[0]}

def parse_pge_marketing_pdf(pdf_path):
    print(f"\n[Neighborhood Scan] Analyzing {os.path.basename(pdf_path)}...")
    
    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"
    
    # Flatten but keep basic spatial separation
    flat_text = " ".join(full_text.split())

    # Define anchors and how far to look after them for numbers
    anchors = {
        "E-1 tiered": "Tiered Rate Plan (E-1)",
        "E-TOU-C": "Time-of-Use (E-TOU-C)",
        "E-TOU-D": "Time-of-Use (E-TOU-D)",
        "E-ELEC": "Electric Home Rate Plan (E-ELEC)",
        "EV2-A": "EV2-A",
        "EV-B": "EV-B"
    }

    final_data = {}

    for plan, marker in anchors.items():
        idx = flat_text.find(marker)
        if idx == -1: continue

        # Take a large chunk (800 chars) after the plan name to catch both seasons
        plan_neighborhood = flat_text[idx : idx + 1000]
        
        # Split neighborhood into Summer/Winter segments
        s_idx = plan_neighborhood.find("Summer")
        w_idx = plan_neighborhood.find("Winter")

        summer_pool = []
        winter_pool = []

        if s_idx != -1 and w_idx != -1:
            # Plan has seasonal sections
            if s_idx < w_idx:
                s_chunk = plan_neighborhood[s_idx:w_idx]
                w_chunk = plan_neighborhood[w_idx:]
            else:
                w_chunk = plan_neighborhood[w_idx:s_idx]
                s_chunk = plan_neighborhood[s_idx:]
            
            summer_pool = [float(m)/100 for m in re.findall(r"(\d+)¢", s_chunk)]
            winter_pool = [float(m)/100 for m in re.findall(r"(\d+)¢", w_chunk)]
        else:
            # Plan might be non-seasonal in text (E-1)
            all_nearby = [float(m)/100 for m in re.findall(r"(\d+)¢", plan_neighborhood)]
            summer_pool = all_nearby
            winter_pool = all_nearby

        final_data[plan] = {
            "summer": map_rates_to_bins(summer_pool, plan),
            "winter": map_rates_to_bins(winter_pool, plan)
        }

    return final_data

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
                
                # Check for significant change to avoid overwriting high-precision data with rounded cents
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
            print("\n>>> Result: Dry Run complete. Sorting logic verified.")
    else:
        print("\n>>> Result: No significant changes detected.")

    if os.path.exists(tmp_pdf): os.remove(tmp_pdf)

if __name__ == "__main__":
    main()
