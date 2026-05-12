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
    if pd.isna(val) or val == "-": return 0.0
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

    # Map of JSON ID to the text markers found in your CSV
    plan_markers = {
        "E-1 tiered": ["E1,", "Tiered Energy Charges"],
        "E-TOU-C": ["E-TOU-C"],
        "E-TOU-D": ["E-TOU-D"],
        "E-ELEC": ["E-ELEC"],
        "EV2-A": ["EV2"],
        "EV-B": ["EV, Rate B"]
    }

    for sheet_name in xlsx.sheet_names:
        print(f"  > Scanning Sheet: {sheet_name}")
        df = xlsx.parse(sheet_name, header=None)
        
        current_plan_id = None
        current_season = "summer"

        for idx, row in df.iterrows():
            row_str = " ".join(row.astype(str).tolist()).lower()
            
            # 1. Identify if this row starts a new Plan block
            for json_id, markers in plan_markers.items():
                if any(m.lower() in row_str for m in markers):
                    current_plan_id = json_id
                    if current_plan_id not in extracted_data:
                        extracted_data[current_plan_id] = {"summer": {}, "winter": {}}
                    # print(f"    [Found] Start of {json_id} at row {idx}")

            if not current_plan_id: continue

            # 2. Update Season context
            if "summer" in row_str: current_season = "summer"
            elif "winter" in row_str: current_season = "winter"

            # 3. Handle E-1 Tiered (Unique column-based layout)
            if current_plan_id == "E-1 tiered":
                # Looking for the row with Tier 1 and Tier 2 values (usually row starting with 'Residential Schedules')
                if "tiered energy charges" in row_str:
                    # Based on CSV: Col 8 = T1, Col 9 = T2
                    t1 = clean_val(row.iloc[8])
                    t2 = clean_val(row.iloc[9])
                    if t1 > 0:
                        extracted_data["E-1 tiered"]["summer"] = {"onPeak": t2, "offPeak": t1}
                        extracted_data["E-1 tiered"]["winter"] = {"onPeak": t2, "offPeak": t1}
                continue

            # 4. Handle TOU Plans (E-TOU-C, E-TOU-D, EV, ELEC)
            # We look for rows that contain "Peak" or "Off-Peak"
            period_cell = str(row.iloc[7]).lower() if len(row) > 7 else ""
            if "peak" in period_cell or "part" in period_cell:
                # Based on CSV: Column 9 is rate for Standard, Column 8 for EV/Tech
                rate = clean_val(row.iloc[9]) if "E-TOU" in current_plan_id else clean_val(row.iloc[8])
                
                if rate > 0:
                    # Mapping logic
