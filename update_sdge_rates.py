import sys
import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime

# --- CONFIGURATION ---
# The source you provided proves the data is static in the HTML
PRICING_URL = "https://www.sdge.com/residential/pricing-plans"

# Plan ID mapping to the HTML Modal IDs found in your source paste
PLAN_MAP = {
    "TOU-DR1": "TOU-DR1",
    "TOU-DR2": "TOU-DR2",
    "Standard DR": "Standard", # Site uses id="Standard" for the DR plan
    "EV-TOU-5": "EV-TOU-5",
    "EV-TOU-5-P": "EV-TOU-5-P",
    "TOU-DR-P": "TOU-DR-P",
    "TOU-ELEC": "TOU-ELEC",
    "DR-SES": "DR-SES",
    "EV-TOU": "EV-TOU"
}

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}

def extract_cents(text):
    """Converts '34.0¢' to 0.34000"""
    match = re.search(r"(\d+\.\d+)", text)
    if match:
        return round(float(match.group(1)) / 100, 5)
    return None

def main():
    dry_run = "--dry-run" in sys.argv
    print(f"--- Starting SDG&E Content Scraper (Dry Run: {dry_run}) ---")

    try:
        resp = requests.get(PRICING_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        print(f"!!! Page Load Failed: {e}")
        sys.exit(1)

    try:
        with open('sdge_rates.json', 'r') as f:
            data = json.load(f)
    except:
        print("Error: sdge_rates.json not found.")
        sys.exit(1)

    updated = False
    now = datetime.now()
    # SDGE Summer: June 1 - Oct 31
    is_summer = (6 <= now.month <= 10)
    season = "summer" if is_summer else "winter"

    for app_id, modal_id in PLAN_MAP.items():
        # 1. Find the modal for this plan
        modal = soup.find('div', {'id': modal_id})
        if not modal:
            print(f"  [MISS] Modal ID '{modal_id}' not found for {app_id}")
            continue

        # 2. Find the 'Non-CCA' table
        # We look for the collapse section containing "Non-CCA"
        non_cca_section = modal.find(string=re.compile("Non-CCA Customers", re.I))
        if not non_cca_section:
            continue
            
        container = non_cca_section.find_parent('div', class_='panel')
        table = container.find('table') if container else None
        
        if table:
            # Extract rates from the 'Tier 1' row
            # Row 1 is usually headers, Row 3 is usually Tier 1
            rows = table.find_all('tr')
            tier1_row = None
            for row in rows:
                if "Tier 1" in row.get_text() or "Up to 130%" in row.get_text():
                    tier1_row = row
                    break
            
            if tier1_row:
                cells = tier1_row.find_all('td')
                # Values are: Super-Off, Off, On (Standard order for these tables)
                # Note: Some plans (like DR2) only show 2 columns in the table
                found_rates = [extract_cents(c.get_text()) for k, c in enumerate(cells) if extract_cents(c.get_text())]
                
                if len(found_rates) >= 2:
                    new_on = found_rates[-1] # Peak is usually the last column
                    new_off = found_rates[0]
                    new_super = found_rates[0] if len(found_rates) < 3 else found_rates[0] # Map based on length
                    
                    target = data["plans"][app_id][season]
                    # Buffer check: Only update if site differs from JSON by > 0.5 cents
                    if abs(target["onPeak"] - new_on) > 0.005:
                        print(f"  [UPDATE] {app_id}: {target['onPeak']} -> {new_on}")
                        target["onPeak"] = new_on
                        target["offPeak"] = new_off
                        target["superOffPeak"] = new_super
                        updated = True
                    else:
                        print(f"  [MATCH] {app_id} aligns with site ({new_on})")

    if updated and not dry_run:
        data["lastUpdated"] = now.strftime("%Y-%m-%d %H:%M")
        with open('sdge_rates.json', 'w') as f:
            json.dump(data, f, indent=2)
        print("\n>>> Success: sdge_rates.json updated.")
    else:
        print("\n>>> No updates needed.")

if __name__ == "__main__":
    main()
