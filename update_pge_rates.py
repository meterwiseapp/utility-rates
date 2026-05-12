import os
import json
import sys
import requests
import pandas as pd
import argparse
from datetime import datetime

# --- CONFIGURATION ---
XLSX_URL = "https://www.pge.com/assets/rates/tariffs/res-inclu-tou-current.xlsx"
JSON_FILE = "pge_rates.json"

def download_xlsx(url, save_path):
    print(f"[Network] Downloading XLSX from: {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        with open(save_path, 'wb') as f:
            f.write(response.content)
        print(f"[Network] Download complete ({len(response.content)} bytes)")
    except Exception as e:
        print(f"[Error] Failed to download XLSX: {e}")
        sys.exit(1)

def clean_val(val):
    """Safely converts Excel cell values to float, handling currency and negatives."""
    if pd.isna(val) or str(val).strip() in ["", "-", "None"]: return 0.0
    s = str(val).replace('$', '').replace(',', '').strip()
    if '(' in s and ')' in s:
        s = "-" + s.replace('(', '').replace(')', '')
    try:
        return float(s)
    except:
        return 0.0

def parse_pge_xlsx(file_path):
    print(f"\n[Excel Scan] Processing workbook...")
    xlsx = pd.ExcelFile(file_path)
    extracted_data = {}
    baseline_credit_found = None

    # Markers based exactly on your CSV dump
    plan_markers = {
        "E-1 tiered": ["E1, ESR", "Tiered Energy Charges"],
        "E-TOU-C": ["Rate Schedule E-TOU-C"],
        "E-TOU-D": ["Rate Schedule E-TOU-D"],
        "E-ELEC": ["Rate Schedule E-ELEC"],
        "EV2-A": ["Rate Schedule EV2"],
        "EV-B": ["EV, Rate B"]
    }

    for sheet_name in xlsx.sheet_names:
        print(f"  > Scanning Sheet: {sheet_name}")
        df = xlsx.parse(sheet_name, header=None)
        
        current_plan_id = None
        current_season = "summer"

        for idx, row in df.iterrows():
            # FIX: Explicit list comprehension to force strings and handle the TypeError
            row_vals = [str(item) for item in row.tolist()]
            row_str = " ".join(row_vals).lower()
            
            # 1. Identify Plan Start
            for json_id, markers in plan_markers.items():
                if any(m.lower() in row_str for m in markers):
                    current_plan_id = json_id
                    if current_plan_id not in extracted_data:
                        extracted_data[current_plan_id] = {"summer": {}, "winter": {}}
                    print(f"    [Found] {json_id} block start (Row {idx})")

            if not current_plan_id: continue

            # 2. Update Season Context
            if "summer" in row_str: current_season = "summer"
            elif "winter" in row_str: current_season = "winter"

            # 3. Handle E-1 Tiered (Table 1: Col 8 and 9)
            if current_plan_id == "E-1 tiered":
                if "tiered energy charges" in row_str:
                    t1 = clean_val(row.iloc[8])
                    t2 = clean_val(row.iloc[9])
                    if t1 > 0:
                        extracted_data["E-1 tiered"]["summer"] = {"onPeak": t2, "offPeak": t1}
                        extracted_data["E-1 tiered"]["winter"] = {"onPeak": t2, "offPeak": t1}
                        print(f"      -> E-1 Captured: T1={t1}, T2={t2}")
                continue

            # 4. Determine Column Mapping based on CSV structure
            # Standard (E-TOU-C/D) use Col 8/9 | EV/Tech use Col 7/8
            is_ev_tech = any(x in current_plan_id for x in ["EV", "ELEC"])
            period_col = 7 if is_ev_tech else 8
            rate_col = 8 if is_ev_tech else 9
            
            if len(row) <= max(period_col, rate_col): continue
            
            period_cell = str(row.iloc[period_col]).lower()
            
            if "peak" in period_cell:
                rate = clean_val(row.iloc[rate_col])
                if rate > 0:
                    if "peak" in period_cell and "off" not in period_cell and "part" not in period_cell:
                        extracted_data[current_plan_id][current_season]["onPeak"] = rate
                    elif "off-peak" in period_cell:
                        key = "superOffPeak" if is_ev_tech else "offPeak"
                        extracted_data[current_plan_id][current_season][key] = rate
                    elif "part" in period_cell:
                        extracted_data[current_plan_id][current_season]["offPeak"] = rate
                    
                    print(f"      -> {current_plan_id} {current_season} {period_cell}: {rate}")

                # 5. Baseline Credit (Standard Table: Col 10)
                if not is_ev_tech and len(row) > 10:
                    b_val = clean_val(row.iloc[10])
                    if b_val < 0: 
                        baseline_credit_found = abs(b_val)

    return extracted_data, baseline_credit_found

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run: print("\n!!! DRY RUN MODE: No files will be modified !!!")

    tmp_xlsx = "pge_temp.xlsx"
    download_xlsx(XLSX_URL, tmp_xlsx)
    
    try:
        new_data, b_credit = parse_pge_xlsx(tmp_xlsx)
    except Exception as e:
        print(f"[Error] Parser Failure: {e}")
        if os.path.exists(tmp_xlsx): os.remove(tmp_xlsx)
        return

    if not os.path.exists(JSON_FILE):
        print(f"[Error] {JSON_FILE} not found.")
        return

    with open(JSON_FILE, 'r') as f:
        current_json = json.load(f)

    print("\n[Comparison Ledger: JSON vs Excel]")
    updated = False
    
    if b_credit:
        old_bc = current_json.get("baselineCredit", 0)
        if abs(b_credit - old_bc) > 0.0001:
            print(f"  [CHANGE] Global Baseline Credit: ${old_bc:.5f} -> ${b_credit:.5f}")
            if not args.dry_run: current_json["baselineCredit"] = b_credit
            updated = True

    for plan in ["E-1 tiered", "E-TOU-C", "E-TOU-D", "E-ELEC", "EV2-A", "EV-B"]:
        if plan not in new_data: continue
        for season in ["summer", "winter"]:
            p_res = new_data[plan].get(season, {})
            for b_type in ["onPeak", "offPeak", "superOffPeak"]:
                rate = p_res.get(b_type, 0)
                if rate == 0: continue
                
                current_val = current_json["plans"][plan][season].get(b_type, 0)
                diff = abs(rate - current_val)
                status = "[MATCH]" if diff < 0.0001 else "[CHANGE DETECTED]"
                
                print(f"  {status} {plan:12} ({season:6} {b_type:12}): JSON=${current_val:.5f} | XLSX=${rate:.5f}")

                if diff > 0.0001: 
                    current_json["plans"][plan][season][b_type] = rate
                    updated = True

    if updated and not args.dry_run:
        current_json["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(JSON_FILE, 'w') as f:
            json.dump(current_json, f, indent=2)
        print("\n>>> Result: Success. JSON updated.")
    else:
        print("\n>>> Result: No changes committed.")

    if os.path.exists(tmp_xlsx): os.remove(tmp_xlsx)

if __name__ == "__main__":
    main()
