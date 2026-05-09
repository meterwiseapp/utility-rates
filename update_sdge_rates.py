import sys
import json
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
PRICING_URL = "https://www.sdge.com/residential/pricing-plans"

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
    # Matches digits with decimals (e.g. 62.1 or 0.621)
    match = re.search(r"(\d+\.\d+)", text)
    if match:
        val = float(match.group(1))
        # If it's a "cent" value from the site summary (e.g. 62.1), convert to dollars
        if val > 1.0:
            return round(val / 100, 5)
        return val
    return None

def main():
    dry_run = "--dry-run" in sys.argv
    print(f"--- Starting SDG&E Playwright Scraper (Dry Run: {dry_run}) ---")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 2000})
        page = context.new_page()

        print(f"Navigating to {PRICING_URL}...")
        try:
            page.goto(PRICING_URL, wait_until="domcontentloaded", timeout=45000)
            
            # CRITICAL STEP: Click the "Bundled Rates" toggle if it exists
            # This ensures we are not looking at CCA-only delivery rates
            try:
                bundled_button = page.get_by_role("button", name=re.compile("SDG&E", re.I))
                if bundled_button.is_visible():
                    bundled_button.click()
                    page.wait_for_timeout(1000)
                    print("  [Action] Clicked SDG&E Bundled Rates toggle.")
            except:
                pass

            page.wait_for_selector("text=TOU-DR1", timeout=15000)
            soup_text = page.inner_text("body")
        except Exception as e:
            print(f"!!! Error during page load: {e}")
            browser.close()
            sys.exit(1)

        try:
            with open('sdge_rates.json', 'r') as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error loading sdge_rates.json: {e}")
            browser.close()
            sys.exit(1)

        updated = False
        now = datetime.now()
        is_summer = (6 <= now.month <= 10)
        season = "summer" if is_summer else "winter"

        print(f"Scanning for {season.upper()} rates...")
        for app_id, marker in PLAN_MAP.items():
            if marker in soup_text:
                start_idx = soup_text.find(marker)
                # Snip a large block to ensure we capture the whole table content
                relevant_text = soup_text[start_idx : start_idx + 4000]
                
                # New Regex: Look for numbers that look like rates (XX.X or 0.XXXX)
                # We filter these numbers in the next step
                potential_rates = re.findall(r"(\d+\.\d+)", relevant_text)
                
                # Filter out numbers that are definitely not rates (like years or plan IDs)
                found_vals = []
                for val_str in potential_rates:
                    val = float(val_str)
                    # Electric rates are usually 5c to 90c (0.05 to 0.90) 
                    # or expressed as 5.0 to 90.0 on the summary page.
                    if 5.0 < val < 95.0 or 0.05 < val < 0.95:
                        found_vals.append(extract_decimal(val_str))
                
                if len(found_vals) >= 2:
                    if app_id not in data["plans"]: continue
                    target = data["plans"][app_id][season]
                    
                    new_on = found_vals[0]
                    # SDG&E usually lists On, Off, Super-Off in their summary text blocks
                    if abs(target["onPeak"] - new_on) > 0.005:
                        print(f"    [UPDATE] {app_id}: {target['onPeak']} -> {new_on}")
                        target["onPeak"] = new_on
                        target["offPeak"] = found_vals[1]
                        target["superOffPeak"] = found_vals[2] if len(found_vals) >= 3 else found_vals[1]
                        updated = True
                    else:
                        print(f"    [MATCH] {app_id} (Site: {new_on}, JSON: {target['onPeak']})")
                else:
                    # DEBUG: If detection still fails, show what we saw
                    print(f"    [WARN] Found marker {app_id}, but regex only found: {found_vals}")
                    # print(f"DEBUG TEXT SNIP: {relevant_text[:200]}") # Uncomment if still failing
            else:
                print(f"  [MISS] Marker '{marker}' not found on page.")

        if updated and not dry_run:
            data["lastUpdated"] = now.strftime("%Y-%m-%d %H:%M")
            with open('sdge_rates.json', 'w') as f:
                json.dump(data, f, indent=2)
            print("\n>>> Success: sdge_rates.json updated.")
        elif updated:
            print("\n>>> Dry Run: Changes detected but not saved.")
        else:
            print("\n>>> No updates required.")

        browser.close()

if __name__ == "__main__":
    main()
