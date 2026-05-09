import sys
import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime

# --- CONFIGURATION ---
# Primary URL for all residential electric plans
ELECTRIC_URL = "https://www.sdge.com/residential/pricing-plans"
# Gas procurement is often updated monthly on a sibling page
GAS_URL = "https://www.sdge.com/residential/pricing-plans/gas-pricing-plans"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9'
}

# Plan ID mapping to site headers
PLAN_MAP = {
    "TOU-DR1": "TOU-DR1",
    "TOU-DR2": "TOU-DR2",
    "Standard DR": "Standard DR",
    "EV-TOU-5": "EV-TOU-5",
    "TOU-ELEC": "TOU-ELEC",
    "DR-SES": "DR-SES",
    "TOU-DR-P": "TOU-DR-P",
    "EV-TOU-5-P": "EV-TOU-5-P",
    "EV-TOU": "EV-TOU"
}

def extract_cents(text):
    """Converts '34.0¢' or '$0.34' to 0.34 float."""
    match = re.search(r"(\d+\.\d+)", text)
    if match:
        val = float(match.group(1))
        # If it contains the cent symbol or is a whole number > 1, assume cents
        if '¢' in text or val > 1.0:
            return round(val / 100, 5)
        return val
    return None

def main():
    dry_run = "--dry-run" in sys.argv
    print(f"--- Starting SDG&E Scrape (Dry Run: {dry_run}) ---")

    try:
        with open('sdge_rates.json', 'r') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error loading JSON: {e}")
        sys.exit(1)

    # 1. FETCH ELECTRIC DATA
    # Note: On GitHub Actions, we recommend using Playwright if this static fetch fails.
    # For now, we search the text for the 2026 effective blocks.
    resp = requests.get(ELECTRIC_URL, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(resp.text, 'html.parser')
    page_text = soup.get_text(separator=' ')

    updated = False
    now = datetime.now()
    # SDGE Summer: June 1 - Oct 31
    is_summer = (6 <= now.month <= 10)
    season_key = "summer" if is_summer else "winter"

    # Search the text for each plan block
    # We look for the section starting with 'Non-CCA Customers' for Total Rates
    for app_id, marker in PLAN_MAP.items():
        # Find the text block for the specific plan
        pattern = rf"{marker}.*?Non-CCA Customers: Electric Generation and Delivery"
        section = re.search(pattern, page_text, re.IGNORECASE | re.DOTALL)
        
        if section:
            # Look for rates in proximity to tiers
            # Pattern: 'Tier 1' followed by 1-3 rate values
            rates_text = page_text[section.end():section.end()+500]
            found_rates = []
            
            # Find all strings ending in ¢
            for match in re.findall(r"(\d+\.\d+¢)", rates_text):
                found_rates.append(extract_cents(match))
            
            if len(found_rates) >= 2:
                # Map to JSON: Usually Super-Off, Off, On
                # If only 2 found (like DR2), map appropriately
                on = found_rates[-1] # Usually the highest/last
                off = found_rates[0]
                sup = found_rates[0] if len(found_rates) < 3 else found_rates[0]
                
                # Update data object
                if app_id in data["plans"]:
                    target = data["plans"][app_id][season_key]
                    if target["onPeak"] != on:
                        print(f"  [CHANGE] {app_id} {season_key} On-Peak: {target['onPeak']} -> {on}")
                        target["onPeak"] = on
                        target["offPeak"] = off
                        target["superOffPeak"] = sup
                        updated = True
        else:
            print(f"  [Warning] Could not locate 'Generation and Delivery' block for {app_id}")

    # 2. FETCH GAS DATA (Simplified proxy)
    # Using sibling Sempra data (SoCalGas) for procurement as they update simultaneously
    gas_resp = requests.get("https://www.socalgas.com/business/energy-market-services/gas-prices", headers=HEADERS)
    if gas_resp.status_code == 200:
        # Match current month/year procurement rate
        month_full = now.strftime("%B")
        gas_pattern = rf"{month_full}\s+\d{{1,2}},\s+{now.year}\s+(\d+\.\d{{3,5}})"
        gas_match = re.search(gas_pattern, gas_resp.text)
        if gas_match:
            proc_cents = float(gas_match.group(1))
            proc_dollars = round(proc_cents / 100, 5)
            if data["gas"]["procurement"] != proc_dollars:
                print(f"  [CHANGE] Gas Procurement: {data['gas']['procurement']} -> {proc_dollars}")
                data["gas"]["procurement"] = proc_dollars
                updated = True

    # 3. SAVE
    if updated and not dry_run:
        data["lastUpdated"] = now.strftime("%Y-%m-%d %H:%M")
        with open('sdge_rates.json', 'w') as f:
            json.dump(data, f, indent=2)
        print(">>> SUCCESS: sdge_rates.json updated.")
    elif updated:
        print(">>> DRY RUN: Changes detected but not saved.")
    else:
        print(">>> No changes detected.")

if __name__ == "__main__":
    main()
