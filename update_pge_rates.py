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
    if pd.isna(val) or str(val).strip() in ["", "-", "None"]: return 0.0
    s = str(val).replace('$', '').replace(',', '').strip()
    if '(' in s and ')' in s:
        s = "-" + s.replace('(', '').replace(')', '')
    try:
        return float(s)
    except:
        return 0.0

def parse_pge_baseline_allowances(xlsx):
    """
    Parses daily baseline allowances from sheet: ElecBaselineEffec220601-Present
    Layout: Winter on left (cols 0-3), Summer on right (cols 4-7)
    Captures 'Individually Metered (Daily)' for Code H (All Elec) and Code B (Basic Elec)
    across territories T, P, R, S, X.
    """
    print("\n[Excel Scan] Scanning Baseline Quantities Sheet...")
    extracted_allowances = {t: {"summer": {}, "winter": {}} for t in ["T", "P", "R", "S", "X"]}
    territories = ["T", "P", "R", "S", "X"]
    
    # 1. Target the Baseline sheet
    target_sheet = None
    for name in xlsx.sheet_names:
        if "ElecBaseline" in name or "Baseline" in name:
            target_sheet = name
            break
            
    if not target_sheet:
        print("  [Warning] Baseline sheet not found in workbook.")
        return {}

    print(f"  > Target Sheet Found: '{target_sheet}'")
    df = xlsx.parse(target_sheet, header=None)

    current_code_left = "allElectric"  # Winter default section
    current_code_right = "allElectric" # Summer default section

    for idx, row in df.iterrows():
        row_str = " ".join([str(cell) for cell in row.dropna().tolist()])
        
        # Section Tracking: Code H (All-Electric) vs Code B (Basic)
        if "CODE H" in row_str.upper() or "ALL ELEC" in row_str.upper():
            current_code_left = "allElectric"
            current_code_right = "allElectric"
        elif "CODE B" in row_str.upper() or "BASIC ELEC" in row_str.upper():
            current_code_left = "basic"
            current_code_right = "basic"

        # Scan row cells for Territory letters
        for col_idx, cell in enumerate(row):
            cell_str = str(cell).strip().upper()
            
            # Match standalone territory letter (T, P, R, S, X)
            t_match = None
            if cell_str in territories:
                t_match = cell_str
            elif cell_str.startswith("TERRITORY "):
                t = cell_str.replace("TERRITORY ", "").strip()
                if t in territories: t_match = t

            if t_match:
                # Look for the first positive numeric value to the right (Individually Metered Daily)
                numeric_vals = []
                for val_idx in range(col_idx + 1, min(col_idx + 4, len(row))):
                    v = clean_val(row.iloc[val_idx])
                    if v > 0:
                        numeric_vals.append(v)
                
                if numeric_vals:
                    individually_metered_val = numeric_vals[0]
                    is_summer_side = col_idx >= 4  # Right 4 columns = Summer
                    
                    season = "summer" if is_summer_side else "winter"
                    code_type = current_code_right if is_summer_side else current_code_left
                    
                    extracted_allowances[t_match][season][code_type] = individually_metered_val
                    print(f"    [Captured] Territory {t_match} ({season:6} - {code_type:11}): {individually_metered_val} kWh/day")

    # Filter out empty structures
    valid_result = {t: data for t, data in extracted_allowances.items() 
                    if "basic" in data["summer"] or "allElectric" in data["summer"]}
    return valid_result

def parse_pge_xlsx(file_path):
    print(f"\n[Excel Scan] Processing workbook...")
    xlsx = pd.ExcelFile(file_path)
    extracted_data = {}
    baseline_credit_found = None

    plan_identities = {
        "E-1 tiered": ["Residential Schedules", "E1,"],
        "E-TOU-C": ["Rate Schedule E-TOU-C"],
        "E-TOU-D": ["Rate Schedule E-TOU-D"],
        "E-ELEC": ["Rate Schedule E-ELEC"],
        "EV2-A": ["Rate Schedule EV2"],
        "EV-B": ["EV, Rate B"]
    }
    
    exclusion_markers = ["EM", "EM-TOU", "ES,", "ET,", "Master"]

    for sheet_name in xlsx.sheet_names:
        print(f"  > Scanning Sheet: {sheet_name}")
        df = xlsx.parse(sheet_name, header=None)
        
        current_plan_id = None
        current_season = "summer"

        for idx, row in df.iterrows():
            first_cell = str(row.iloc[0]).strip()
            row_str = " ".join([str(i) for i in row.tolist()])
            
            found_anchor = False
            for json_id, markers in plan_identities.items():
                if any(m in first_cell for m in markers):
                    if not any(ex in first_cell for ex in exclusion_markers) or "E1" in first_cell:
                        current_plan_id = json_id
                        found_anchor = True
                        if current_plan_id not in extracted_data:
                            extracted_data[current_plan_id] = {"summer": {}, "winter": {}}
                        print(f"    [Found] {json_id} block start (Row {idx})")
                        break
            
            if not found_anchor and any(ex in first_cell for ex in exclusion_markers) and "E1" not in first_cell:
                if current_plan_id:
                    print(f"    [Boundary] Stopping data collection at row {idx} due to non-residential marker: {first_cell}")
                current_plan_id = None
                continue

            if not current_plan_id: continue

            if "Summer" in row_str: current_season = "summer"
            elif "Winter" in row_str: current_season = "winter"

            if current_plan_id == "E-1 tiered":
                if "Tiered Energy Charges" in row_str:
                    t1 = clean_val(row.iloc[8])
                    t2 = clean_val(row.iloc[9])
                    if t1 > 0:
                        extracted_data["E-1 tiered"]["summer"] = {"onPeak": t2, "offPeak": t1}
                        extracted_data["E-1 tiered"]["winter"] = {"onPeak": t2, "offPeak": t1}
                        print(f"      -> Captured Res E-1: T1={t1}, T2={t2}")
                continue

            is_ev_tech = any(x in current_plan_id for x in ["EV", "ELEC"])
            period_col = 7 if is_ev_tech else 8
            rate_col = 8 if is_ev_tech else 9
            
            if len(row) > max(period_col, rate_col):
                period_cell = str(row.iloc[period_col]).strip()
                
                if "Peak" in period_cell:
                    rate = clean_val(row.iloc[rate_col])
                    if rate > 0:
                        if period_cell == "Peak":
                            extracted_data[current_plan_id][current_season]["onPeak"] = rate
                        elif period_cell == "Off-Peak":
                            key = "superOffPeak" if is_ev_tech else "offPeak"
                            extracted_data[current_plan_id][current_season][key] = rate
                        elif period_cell in ["Partial-Peak", "Part-Peak"]:
                            extracted_data[current_plan_id][current_season]["offPeak"] = rate
                        
                        print(f"      -> {current_plan_id} {current_season} {period_cell}: {rate}")

                    if current_plan_id == "E-TOU-C" and len(row) > 10:
                        b_val = clean_val(row.iloc[10])
                        if b_val < 0: baseline_credit_found = abs(b_val)

    # Extract Baseline Allowances
    extracted_allowances = parse_pge_baseline_allowances(xlsx)

    return extracted_data, baseline_credit_found, extracted_allowances

def cleanup_bins(data):
    two_tier_plans = ["E-1 tiered", "E-TOU-C", "E-TOU-D"]
    for plan_id in two_tier_plans:
        if plan_id in data:
            for season in ["summer", "winter"]:
                if season in data[plan_id]:
                    data[plan_id][season]["superOffPeak"] = 0.0
    return data
    
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run: print("\n!!! DRY RUN MODE: No files will be modified !!!")

    tmp_xlsx = "pge_temp.xlsx"
    download_xlsx(XLSX_URL, tmp_xlsx)
    
    try:
        new_data, b_credit, new_allowances = parse_pge_xlsx(tmp_xlsx)
        new_data = cleanup_bins(new_data)
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
    
    # 1. Update Global Baseline Credit
    if b_credit:
        old_bc = current_json.get("baselineCredit", 0)
        if abs(b_credit - old_bc) > 0.0001:
            print(f"  [CHANGE] Global Baseline Credit: ${old_bc:.5f} -> ${b_credit:.5f}")
            if not args.dry_run: current_json["baselineCredit"] = b_credit
            updated = True

    # 2. Update Baseline Allowances Table
    if new_allowances:
        if "baselineAllowances" not in current_json:
            current_json["baselineAllowances"] = {}
        
        for t, seasons in new_allowances.items():
            if t not in current_json["baselineAllowances"]:
                current_json["baselineAllowances"][t] = seasons
                updated = True
                print(f"  [NEW] Added Baseline Territory {t} to JSON")
            else:
                for season in ["summer", "winter"]:
                    for code in ["basic", "allElectric"]:
                        val = seasons.get(season, {}).get(code, 0)
                        if val > 0:
                            curr_val = current_json["baselineAllowances"][t].get(season, {}).get(code, 0)
                            if abs(val - curr_val) > 0.01:
                                print(f"  [CHANGE] Territory {t} ({season:6} {code:11}): {curr_val} -> {val} kWh/day")
                                if not args.dry_run:
                                    current_json["baselineAllowances"][t][season][code] = val
                                updated = True

    # 3. Update Plan Rates
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
