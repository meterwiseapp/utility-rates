#!/usr/bin/env python3
"""
LADWP Rate Scraper & Verification Engine
Fetches residential electric and water rate tables from LADWP.com.
Writes to BOTH 'rates/ladwp_rates.json' and 'ladwp_rates.json' for legacy support.
"""

import sys
import os
import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime

# --- ROBUST PATH RESOLUTION ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(SCRIPT_DIR) == "scrapers":
    ROOT_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
else:
    ROOT_DIR = SCRIPT_DIR

# The 2 canonical file targets to keep 100% in sync
TARGET_FILES = [
    os.path.join(ROOT_DIR, "rates", "ladwp_rates.json"),
    os.path.join(ROOT_DIR, "ladwp_rates.json")
]

# --- CONFIGURATION & URLS ---
ELECTRIC_URL = "https://www.ladwp.com/account/customer-service/electric-rates/residential-rates"
WATER_URL = "https://www.ladwp.com/account/customer-service/water-rates/schedule-residential"

E_PERIOD_MAP = {
    r"January\s*-\s*March": ["janMar"],
    r"April\s*-\s*May": ["aprMay"],
    r"January\s*-\s*May": ["janMar", "aprMay"],
    r"June": ["june"],
    r"July\s*-\s*September": ["julSep"],
    r"June\s*-\s*September": ["june", "julSep"],
    r"October\s*-\s*December": ["octDec"]
}

W_PERIOD_MAP = {
    r"January\s*-\s*June": ["janMar", "aprMay", "june"],
    r"July\s*-\s*December": ["julSep", "octDec"]
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none'
}


class RateChangeRecord:
    def __init__(self, plan_name: str, period: str, tier_name: str, old_rate: float, new_rate: float):
        self.plan_name = plan_name
        self.period = period
        self.tier_name = tier_name
        self.old_rate = old_rate
        self.new_rate = new_rate
        self.delta = new_rate - old_rate
        self.pct_change = (self.delta / old_rate * 100) if old_rate > 0 else 0.0

    @property
    def status(self) -> str:
        if abs(self.delta) < 0.00001:
            return "UNCHANGED"
        if abs(self.pct_change) <= 25.0:
            return "REASONABLE"
        return "WARN: LARGE SHIFT"


def extract_rates(row, expected_count):
    """Extracts rates as floats, filtering for utility-sized decimals."""
    cells = row.find_all(['td', 'th'])
    found = []
    for cell in cells:
        text = cell.get_text(strip=True).replace('$', '').replace(',', '')
        match = re.search(r"(\d+\.\d+)", text)
        if match:
            val = float(match.group(1))
            if 0.01 < val < 2.0 or 5.0 < val < 40.0:
                found.append(val)
    return found[:expected_count]


def scrape_section(soup, occurrence, search_text, year_target, pattern_map, is_water=False):
    """Finds target year rates in the nth table containing search_text."""
    count = 0
    results = {}
    
    for table in soup.find_all('table'):
        if search_text.lower() in table.get_text().lower():
            count += 1
            if count == occurrence:
                in_year_block = False
                for row in table.find_all('tr'):
                    row_text = row.get_text(separator=' ', strip=True)
                    
                    if str(year_target) in row_text:
                        in_year_block = True
                        continue
                    elif any(str(prev) in row_text for prev in range(2020, int(year_target))) and str(year_target) not in row_text:
                        in_year_block = False
                    
                    if in_year_block:
                        for pattern, json_keys in pattern_map.items():
                            if re.search(pattern, row_text, re.IGNORECASE):
                                expected = 4 if is_water else 3
                                nums = extract_rates(row, expected)
                                if len(nums) >= 3:
                                    for key in json_keys:
                                        results[key] = nums
                break
    return results


def scrape_base_rates(soup):
    """Locates and scrapes the R-1A Standard Base Rates table."""
    results = {}
    for table in soup.find_all('table'):
        table_text = table.get_text().lower()
        if "energy charge" in table_text and "base rate" in table_text:
            for row in table.find_all('tr'):
                row_text = row.get_text(separator=' ', strip=True)
                for pattern, json_keys in E_PERIOD_MAP.items():
                    if re.search(pattern, row_text, re.IGNORECASE):
                        nums = extract_rates(row, 3)
                        if len(nums) >= 3:
                            for key in json_keys:
                                results[key] = nums
            break
    return results


def load_existing_json():
    """Safely loads current JSON file from any valid path or returns base fallback schema."""
    for p in TARGET_FILES:
        if os.path.exists(p):
            try:
                with open(p, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] Failed to parse JSON from {p}: {e}")
            
    return {
        "version": 1,
        "utility": "LADWP",
        "lastUpdated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "periods": {
            "janMar": [1, 2, 3],
            "aprMay": [4, 5],
            "june": [6],
            "julSep": [7, 8, 9],
            "octDec": [10, 11, 12]
        },
        "electric": { "standard": {}, "tou": {} },
        "water": {},
        "trash": { "flatRate": 72.80 }
    }


def print_comparison_report(records: list[RateChangeRecord], dry_run: bool):
    """Renders a clean, formatted ASCII rate verification report."""
    print("\n" + "=" * 94)
    print("                           LADWP RATE CHANGE VERIFICATION AUDIT")
    print("=" * 94)
    print(f"{'Plan & Period':<26} | {'Tier / Line':<12} | {'Old Rate':<10} | {'New Rate':<10} | {'Delta ($)':<10} | {'Shift %':<8} | {'Status'}")
    print("-" * 94)

    updated_count = 0
    warning_count = 0

    for r in records:
        delta_str = f"{r.delta:+.5f}" if abs(r.delta) >= 0.00001 else "$0.00000"
        pct_str = f"{r.pct_change:+.2f}%" if abs(r.delta) >= 0.00001 else "0.00%"
        
        status_tag = f"[{r.status}]"
        if r.status == "REASONABLE":
            updated_count += 1
        elif r.status == "WARN: LARGE SHIFT":
            updated_count += 1
            warning_count += 1

        label = f"{r.plan_name} ({r.period})"
        print(f"{label:<26} | {r.tier_name:<12} | ${r.old_rate:<9.5f} | ${r.new_rate:<9.5f} | {delta_str:<10} | {pct_str:<8} | {status_tag}")

    print("=" * 94)
    mode_text = "[DRY RUN: NO FILES MODIFIED]" if dry_run else "[COMMITTED TO DISK]"
    print(f"Summary: {len(records)} Total Audited | {updated_count} Changed | {warning_count} Warnings | {mode_text}\n")


def main():
    dry_run = "--dry-run" in sys.argv
    data = load_existing_json()
    current_year = datetime.now().year
    print(f"[*] Starting LADWP rate scrape for {current_year}...")

    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Fetch Electric Page
    try:
        e_resp = session.get(ELECTRIC_URL, timeout=20)
        e_resp.raise_for_status()
        e_soup = BeautifulSoup(e_resp.text, 'html.parser')
    except Exception as e:
        print(f"[ERROR] Could not fetch LADWP electric page: {e}", file=sys.stderr)
        sys.exit(1)

    r1a_site_data = scrape_section(e_soup, 1, "Total Consumption Charge", current_year, E_PERIOD_MAP) or \
                    scrape_section(e_soup, 1, "Total Consumption Charge", current_year - 1, E_PERIOD_MAP)
    r1a_base_site_data = scrape_base_rates(e_soup)

    r1b_site_data = scrape_section(e_soup, 2, "Total Consumption Charge", current_year, E_PERIOD_MAP) or \
                    scrape_section(e_soup, 2, "Total Consumption Charge", current_year - 1, E_PERIOD_MAP)

    # 2. Fetch Water Page
    try:
        w_resp = session.get(WATER_URL, timeout=20)
        w_resp.raise_for_status()
        w_soup = BeautifulSoup(w_resp.text, 'html.parser')
        water_site_data = scrape_section(w_soup, 1, "Total Consumption Charge", current_year, W_PERIOD_MAP, is_water=True) or \
                          scrape_section(w_soup, 1, "Total Consumption Charge", current_year - 1, W_PERIOD_MAP, is_water=True)
    except Exception as e:
        print(f"[WARN] Water rate fetch failed: {e}")
        water_site_data = {}

    change_records: list[RateChangeRecord] = []
    has_updates = False

    # Audit R-1A (Standard)
    for period_key, rates in r1a_site_data.items():
        existing = data["electric"]["standard"].get(period_key, {})
        base_rates = r1a_base_site_data.get(period_key, [
            existing.get("baseTier1", 0.07142),
            existing.get("baseTier2", 0.13001),
            existing.get("baseTier3", 0.13001)
        ])

        old_t1 = existing.get("tier1", rates[0])
        old_t2 = existing.get("tier2", rates[1])
        old_t3 = existing.get("tier3", rates[2])

        change_records.append(RateChangeRecord("R-1A Standard", period_key, "Tier 1", old_t1, rates[0]))
        change_records.append(RateChangeRecord("R-1A Standard", period_key, "Tier 2", old_t2, rates[1]))
        change_records.append(RateChangeRecord("R-1A Standard", period_key, "Tier 3", old_t3, rates[2]))

        new_val = {
            "tier1": rates[0], "tier2": rates[1], "tier3": rates[2],
            "baseTier1": base_rates[0], "baseTier2": base_rates[1], "baseTier3": base_rates[2]
        }
        if data["electric"]["standard"].get(period_key) != new_val:
            data["electric"]["standard"][period_key] = new_val
            has_updates = True

    # Audit R-1B (TOU)
    for period_key, rates in r1b_site_data.items():
        existing = data["electric"]["tou"].get(period_key, {})
        old_t1 = existing.get("tier1", rates[0])
        old_t2 = existing.get("tier2", rates[1])
        old_t3 = existing.get("tier3", rates[2])

        change_records.append(RateChangeRecord("R-1B TOU", period_key, "High Peak", old_t1, rates[0]))
        change_records.append(RateChangeRecord("R-1B TOU", period_key, "Low Peak", old_t2, rates[1]))
        change_records.append(RateChangeRecord("R-1B TOU", period_key, "Base", old_t3, rates[2]))

        new_val = {"tier1": rates[0], "tier2": rates[1], "tier3": rates[2]}
        if data["electric"]["tou"].get(period_key) != new_val:
            data["electric"]["tou"][period_key] = new_val
            has_updates = True

    # Audit Water (Tier 1 - 4)
    for period_key, rates in water_site_data.items():
        if len(rates) >= 4:
            existing = data["water"].get(period_key, {})
            old_t1 = existing.get("tier1", rates[0])
            old_t2 = existing.get("tier2", rates[1])
            old_t3 = existing.get("tier3", rates[2])
            old_t4 = existing.get("tier4", rates[3])

            change_records.append(RateChangeRecord("Water", period_key, "Tier 1", old_t1, rates[0]))
            change_records.append(RateChangeRecord("Water", period_key, "Tier 2", old_t2, rates[1]))
            change_records.append(RateChangeRecord("Water", period_key, "Tier 3", old_t3, rates[2]))
            change_records.append(RateChangeRecord("Water", period_key, "Tier 4", old_t4, rates[3]))

            new_val = {"tier1": rates[0], "tier2": rates[1], "tier3": rates[2], "tier4": rates[3]}
            if data["water"].get(period_key) != new_val:
                data["water"][period_key] = new_val
                has_updates = True

    # Print Full Audit Table
    print_comparison_report(change_records, dry_run)

    # WRITE TO BOTH DESTINATIONS IF UPDATED
    if (has_updates or not dry_run):
        today_stamp = datetime.now().strftime("%Y-%m-%d")
        data["lastUpdated"] = today_stamp
        if has_updates:
            data["version"] = data.get("version", 1) + 1

        for target_path in TARGET_FILES:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, 'w') as f:
                json.dump(data, f, indent=2)
            print(f"[SUCCESS] Written updated rate table to: {target_path}")
    else:
        print("[INFO] No changes detected. All online tables match current cache.")


if __name__ == "__main__":
    main()
