import sys
import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime

# --- CONFIGURATION ---
ELECTRIC_URL = "https://www.ladwp.com/account/customer-service/electric-rates/residential-rates"
WATER_URL = "https://www.ladwp.com/account/customer-service/water-rates/schedule-residential"

# Map site periods to app JSON keys. 
# Includes both consolidated (Jan-May) and granular (Jan-Mar) to catch all site variations.
E_PERIOD_MAP = {
    r"January\s*-\s*March": ["janMar"],
    r"April\s*-\s*May": ["aprMay"],
    r"January\s*-\s*May": ["janMar", "aprMay"], # Handles consolidated site rows
    r"June": ["june"],
    r"July\s*-\s*September": ["julSep"],
    r"June\s*-\s*September": ["june", "julSep"], # Handles consolidated site rows
    r"October\s*-\s*December": ["octDec"]
}

W_PERIOD_MAP = {
    r"January\s*-\s*June": ["janMar", "aprMay", "june"],
    r"July\s*-\s*December": ["julSep", "octDec"]
}

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}

def extract_rates(row, expected_count):
    """Extracts rates as floats, filtering for utility-sized decimals."""
    cells = row.find_all(['td', 'th'])
    found = []
    for cell in cells:
        # Clean string: remove $, commas, and non-numeric fluff
        text = cell.get_text(strip=True).replace('$', '').replace(',', '')
        # Match decimals like 0.123 or 12.34
        match = re.search(r"(\d+\.\d+)", text)
        if match:
            val = float(match.group(1))
            # Electric is ~0.25, Water is ~15.0. 
            if 0.01 < val < 2.0 or 5.0 < val < 40.0:
                found.append(val)
    return found[:expected_count]

def scrape_section(soup, occurrence, search_text, year_target, pattern_map, is_water=False):
    """Finds target year rates in the nth table containing search_text."""
    count = 0
    results = {}
    
    for table in soup.find_all('table'):
        # Check if table title contains the marker (e.g., 'Total Consumption Charge')
        if search_text.lower() in table.get_text().lower():
            count += 1
            if count == occurrence:
                in_year_block = False
                for row in table.find_all('tr'):
                    row_text = row.get_text(separator=' ', strip=True)
                    
                    # 1. Year Boundary detection
                    if year_target in row_text:
                        in_year_block = True
                        continue
                    elif any(prev in row_text for prev in ["2025", "2024", "2023"]) and year_target not in row_text:
                        in_year_block = False
                    
                    # 2. Pattern matching within the year block
                    if in_year_block:
                        for pattern, json_keys in pattern_map.items():
                            if re.search(pattern, row_text, re.IGNORECASE):
                                expected = 4 if is_water else 3
                                nums = extract_rates(row, expected)
                                if len(nums) >= 3:
                                    # Store results for every app key mapped to this site row
                                    for key in json_keys:
                                        results[key] = nums
                                    print(f"  [Found] {year_target} {pattern} -> {json_keys}: {nums}")
                break
    return results

def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("!!! DRY RUN MODE ACTIVE: No changes will be saved !!!\n")

    try:
        with open('ladwp_rates.json', 'r') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error loading JSON: {e}")
        sys.exit(1)

    year = "2026"
    print(f"Scraping LADWP for {year}...")

    # 1. R-1A (Standard) - First 'Total Consumption' table
    r1a_site_data = scrape_section(e_soup := BeautifulSoup(requests.get(ELECTRIC_URL, headers=HEADERS).text, 'html.parser'), 
                                   1, "Total Consumption Charge", year, E_PERIOD_MAP)

    # 2. R-1B (TOU) - Second 'Total Consumption' table
    r1b_site_data = scrape_section(e_soup, 2, "Total Consumption Charge", year, E_PERIOD_MAP)

    # 3. WATER - First 'Total Consumption' table on water page
    water_site_data = scrape_section(BeautifulSoup(requests.get(WATER_URL, headers=HEADERS).text, 'html.parser'), 
                                     1, "Total Consumption Charge", year, W_PERIOD_MAP, is_water=True)

    updated = False

    # Apply Electric R-1A (SAFE INTEGRATION: Preserves existing base rate keys)
    for key, rates in r1a_site_data.items():
        existing = data["electric"]["standard"].get(key, {})
        new_val = {
            "tier1": rates[0], 
            "tier2": rates[1], 
            "tier3": rates[2],
            "baseTier1": existing.get("baseTier1", 0.07142),
            "baseTier2": existing.get("baseTier2", 0.13001),
            "baseTier3": existing.get("baseTier3", 0.13001)
        }
        if data["electric"]["standard"].get(key) != new_val:
            print(f"  [UPDATE] Electric R-1A {key}")
            data["electric"]["standard"][key] = new_val
            updated = True

    # Apply Electric R-1B
    for key, rates in r1b_site_data.items():
        new_val = {"tier1": rates[0], "tier2": rates[1], "tier3": rates[2]}
        if data["electric"]["tou"].get(key) != new_val:
            print(f"  [UPDATE] Electric R-1B {key}")
            data["electric"]["tou"][key] = new_val
            updated = True

    # Apply Water
    for key, rates in water_site_data.items():
        new_val = {"tier1": rates[0], "tier2": rates[1], "tier3": rates[2], "tier4": rates[3]}
        if data["water"].get(key) != new_val:
            print(f"  [UPDATE] Water {key}")
            data["water"][key] = new_val
            updated = True

    if updated:
        if dry_run:
            print("\n>>> FINISH: Changes detected but not saved (Dry Run).")
        else:
            data["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            data["version"] = data.get("version", 1) + 1
            with open('ladwp_rates.json', 'w') as f:
                json.dump(data, f, indent=2)
            print("\n>>> FINISH: Success! JSON updated.")
    else:
        print("\n>>> FINISH: No new data found. JSON is current.")

if __name__ == "__main__":
    main()        text = cell.get_text(strip=True).replace('$', '').replace(',', '')
        # Match decimals like 0.123 or 12.34
        match = re.search(r"(\d+\.\d+)", text)
        if match:
            val = float(match.group(1))
            # Electric is ~0.25, Water is ~15.0. 
            if 0.01 < val < 2.0 or 5.0 < val < 40.0:
                found.append(val)
    return found[:expected_count]

def scrape_section(soup, occurrence, search_text, year_target, pattern_map, is_water=False):
    """Finds target year rates in the nth table containing search_text."""
    count = 0
    results = {}
    
    for table in soup.find_all('table'):
        # Check if table title contains the marker (e.g., 'Total Consumption Charge')
        if search_text.lower() in table.get_text().lower():
            count += 1
            if count == occurrence:
                in_year_block = False
                for row in table.find_all('tr'):
                    row_text = row.get_text(separator=' ', strip=True)
                    
                    # 1. Year Boundary detection
                    if year_target in row_text:
                        in_year_block = True
                        continue
                    elif any(prev in row_text for prev in ["2025", "2024", "2023"]) and year_target not in row_text:
                        in_year_block = False
                    
                    # 2. Pattern matching within the year block
                    if in_year_block:
                        for pattern, json_keys in pattern_map.items():
                            if re.search(pattern, row_text, re.IGNORECASE):
                                expected = 4 if is_water else 3
                                nums = extract_rates(row, expected)
                                if len(nums) >= 3:
                                    # Store results for every app key mapped to this site row
                                    for key in json_keys:
                                        results[key] = nums
                                    print(f"  [Found] {year_target} {pattern} -> {json_keys}: {nums}")
                break
    return results

def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("!!! DRY RUN MODE ACTIVE: No changes will be saved !!!\n")

    try:
        with open('ladwp_rates.json', 'r') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error loading JSON: {e}")
        sys.exit(1)

    year = "2026"
    print(f"Scraping LADWP for {year}...")

    # 1. R-1A (Standard) - First 'Total Consumption' table
    r1a_site_data = scrape_section(e_soup := BeautifulSoup(requests.get(ELECTRIC_URL, headers=HEADERS).text, 'html.parser'), 
                                   1, "Total Consumption Charge", year, E_PERIOD_MAP)

    # 2. R-1B (TOU) - Second 'Total Consumption' table
    r1b_site_data = scrape_section(e_soup, 2, "Total Consumption Charge", year, E_PERIOD_MAP)

    # 3. WATER - First 'Total Consumption' table on water page
    water_site_data = scrape_section(BeautifulSoup(requests.get(WATER_URL, headers=HEADERS).text, 'html.parser'), 
                                     1, "Total Consumption Charge", year, W_PERIOD_MAP, is_water=True)

    updated = False

    # Apply Electric R-1A
    for key, rates in r1a_site_data.items():
        new_val = {"tier1": rates[0], "tier2": rates[1], "tier3": rates[2]}
        if data["electric"]["standard"].get(key) != new_val:
            print(f"  [UPDATE] Electric R-1A {key}")
            data["electric"]["standard"][key] = new_val
            updated = True

    # Apply Electric R-1B
    for key, rates in r1b_site_data.items():
        new_val = {"tier1": rates[0], "tier2": rates[1], "tier3": rates[2]}
        if data["electric"]["tou"].get(key) != new_val:
            print(f"  [UPDATE] Electric R-1B {key}")
            data["electric"]["tou"][key] = new_val
            updated = True

    # Apply Water
    for key, rates in water_site_data.items():
        new_val = {"tier1": rates[0], "tier2": rates[1], "tier3": rates[2], "tier4": rates[3]}
        if data["water"].get(key) != new_val:
            print(f"  [UPDATE] Water {key}")
            data["water"][key] = new_val
            updated = True

    if updated:
        if dry_run:
            print("\n>>> FINISH: Changes detected but not saved (Dry Run).")
        else:
            data["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            data["version"] = data.get("version", 1) + 1
            with open('ladwp_rates.json', 'w') as f:
                json.dump(data, f, indent=2)
            print("\n>>> FINISH: Success! JSON updated.")
    else:
        print("\n>>> FINISH: No new data found. JSON is current.")

if __name__ == "__main__":
    main()
