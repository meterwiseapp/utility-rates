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
    if pd.isna(val): return 0.0
    s = str(val).replace('$', '').replace(',', '').strip()
    if '(' in s and ')' in s:
        s = "-" + s.replace('(', '').replace(')', '')
    try:
        return float(s)
    except:
        return 0.0

def find_column_indices(df):
    """
    Scans the first 15 rows of a sheet to find the functional column indices.
    Returns a dict of {key: index}
    """
    indices = {"schedule": 0, "season": -1, "period": -1, "rate": -1, "baseline_credit": -1, "t1": -1, "t2": -1}
    
    # We look at the first 15 rows to find header keywords
    for i in range(min(len(df), 15)):
        row = df.iloc[i].astype(str).str.lower().tolist()
        for idx, cell in enumerate(row):
            if "schedule" in cell: indices["schedule"] = idx
            if "season" in cell: indices["season"] = idx
            if "time-of-use" in cell or "period" in cell: indices["period"] = idx
            if "energy charge" in cell: indices["rate"] = idx
            if "baseline credit" in cell: indices["baseline_credit"] = idx
            if "tier 1 usage" in cell: indices["t1"] = idx
            if "tier 2 usage" in cell: indices["t2"] = idx
            
    return indices

def parse_pge_xlsx(file_path):
    print(f"\n[Excel Scan] Processing workbook...")
    xlsx = pd.ExcelFile(file_path)
    extracted_data = {}
    baseline_credit_found = None

    plan_map = {
        "E-1 tiered": "E1",
        "E-TOU-C": "E-TOU-C",
        "E-TOU-D": "E-TOU-D",
        "E-ELEC": "E-ELEC",
        "EV2-A": "EV2",
        "EV-B": "EV, Rate B"
    }

    for sheet_name in xlsx.sheet_names:
        df = xlsx.parse(sheet_name, header=None)
        col = find_column_indices(df)
        
        # Check if this sheet has the minimum columns needed to be useful
        if col["rate"] == -1: continue

        for json_id, search_term in plan_map.items():
            # Find rows matching the plan name in the detected Schedule column
            mask = df.iloc[:, col["schedule"]].astype(str).str.contains(search_term, na=False, case=False)
            matches = df[mask]
            
            if matches.empty: continue
            
            if json_id not in extracted_data:
                extracted_data[json_id] = {"summer": {}, "winter": {}}

            start_idx = matches.index[0]
            # Scan 12 rows following the plan name to capture all TOU/Season combinations
            for i in range(start_idx, min(start_idx + 12, len(df))):
                row = df.iloc[i]
                
                # Special Case: E-1 Tiered (No separate TOU rows)
                if json_id == "E-1 tiered":
                    if col["t1"] != -1 and col["t2"] != -1:
                        t1 = clean_val(row.iloc[col["t1"]])
                        t2 = clean_val(row.iloc[col["t2"]])
                        if t1 > 0:
                            extracted_data[json_id]["summer"] = {"onPeak": t2, "offPeak": t1}
                            extracted_data[json_id]["winter"] = {"onPeak": t2, "offPeak": t1}
                            break
                    continue

                # Standard TOU Logic
                season_raw = str(row.iloc[col["season"]]).lower() if col["season"] != -1 else ""
                period_raw = str(row.iloc[col["period"]]).lower() if col["period"] != -1 else ""
                rate = clean_val(row.iloc[col["rate"]])
                
                if rate <= 0: continue

                target_season = "summer" if "summer" in season_raw else "winter" if "winter" in season_raw else None
                # Many sheets merge the season cell; if season is empty, use the last known season
                if target_season is None and i > start_idx:
                    # Look up one row for season
                    target_season = "summer" if "summer" in str(df.iloc[i-1, col["season"]]).lower() else "winter"

                if target_season:
                    if "peak" in period_raw and "off" not in period_raw and "part" not in period_raw:
                        extracted_dat
