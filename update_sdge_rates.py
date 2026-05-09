import sys
import json
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
PRICING_URL = "https://www.sdge.com/residential/pricing-plans"

# Plan ID mapping for cards on the grid
PLAN_MAP = {
    "TOU-DR1": "TOU-DR1",
    "TOU-DR2": "TOU-DR2",
    "TOU-ELEC": "TOU-ELEC",
    "DR-SES": "DR-SES",
    "EV-TOU-5": "EV-TOU-5",
    "EV-TOU-5-P": "EV-TOU-5-P",
    "TOU-DR-P": "TOU-DR-P",
    "Standard DR": "Standard DR",
    "EV-TOU": "EV-TOU"
}

def extract_decimal(text):
    match = re.search(r"(\d+\.\d+)", text)
    if match:
        val = float(match.group(1))
        return round(val / 100, 5) if val > 1.0 else val
    return None

def main():
    dry_run = "--dry-run" in sys.argv
    print(f"--- SDG&E Modal Scraper (Dry Run: {dry_run}) ---")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 1200})
        page = context.new_page()

        print(f"Navigating to {PRICING_URL}...")
        try:
            page.goto(PRICING_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"!!! Page Load Failed: {e}")
            browser.close()
            sys.exit(1)

        # LOAD JSON
        try:
            with open('sdge_rates.json', 'r') as f:
                data = json.load(f)
        except:
            print("Error: sdge_rates.json not found.")
            sys.exit(1)

        updated = False
        season = "summer" if (6 <= datetime.now().month <= 10) else "winter"

        # --- SCRAPE LOOP ---
        for app_id, marker in PLAN_MAP.items():
            print(f"  [Plan] Attempting to open details for {app_id}...")
            
            try:
                # 1. Find the plan card and click 'Learn More'
                # We look for a container that contains the plan name and has a button
                plan_card = page.locator(f"div.card:has-text('{marker}'), div.pricing-plan-card:has-text('{marker}')").first
                learn_more_btn = plan_card.get_by_role("button", name="Learn More")
                
                if not learn_more_btn.is_visible():
                    print(f"    [Skip] 'Learn More' button not found for {marker}")
                    continue
                
                learn_more_btn.click()
                page.wait_for_timeout(1000) # Wait for modal animation

                # 2. Inside the Modal, click 'Non-CCA Customers'
                non_cca_toggle = page.get_by_text("Non-CCA Customers", exact=False)
                if non_cca_toggle.is_visible():
                    non_cca_toggle.click()
                    page.wait_for_timeout(800)
                
                # 3. Grab the rate text from the modal content
                # We look for the cent values specifically in the modal area
                modal_text = page.locator("div.modal-content, div.pricing-modal").inner_text()
                site_rates = re.findall(r"(\d+\.\d+)¢", modal_text)

                if len(site_rates) >= 2:
                    found_vals = [extract_decimal(r) for r in site_rates]
                    if app_id in data["plans"]:
                        target = data["plans"][app_id][season]
                        new_on = found_vals[-1] # Usually On-Peak is last in the table
                        new_off = found_vals[0]
                        
                        if abs(target["onPeak"] - new_on) > 0.005:
                            print(f"    [UPDATE] {app_id}: {target['onPeak']} -> {new_on}")
                            target["onPeak"] = new_on
                            target["offPeak"] = new_off
                            target["superOffPeak"] = found_vals[0] if len(found_vals) < 3 else found_vals[1]
                            updated = True
                        else:
                            print(f"    [MATCH] {app_id} values align.")
                
                # 4. Close Modal to reset for next plan
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)

            except Exception as e:
                print(f"    [Error] Failed to process {app_id}: {e}")
                page.keyboard.press("Escape") # Emergency close

        # SAVE
        if updated and not dry_run:
            data["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open('sdge_rates.json', 'w') as f:
                json.dump(data, f, indent=2)
            print("\n>>> Success: sdge_rates.json updated.")
        else:
            print("\n>>> No updates saved.")

        browser.close()

if __name__ == "__main__":
    main()
