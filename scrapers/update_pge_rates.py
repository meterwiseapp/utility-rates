#!/usr/bin/env python3
"""
PG&E Residential Rates & CCA Scraper
Fetches and parses:
- Bundled Residential Tariffs (Excel: res-inclu-tou-current.xlsx)
- Baseline Quantities Table (Territories T, P, R, S, X)
- Solar Net Surplus Compensation PDF (AB 920 Rate Table)
- Base Services charges & EV-B Meter Charges
- All 11 PG&E CCA profiles
"""

import os
import json
import sys
import io
import re
import requests
import pandas as pd
import argparse
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup

XLSX_URL = "https://www.pge.com/assets/rates/tariffs/res-inclu-tou-current.xlsx"
PGE_NSC_PDF_URL = "https://www.pge.com/assets/pge/docs/clean-energy/solar/AB920-RateTable.pdf"
PGE_CCA_HUB_URL = "https://www.pge.com/en/account/alternate-energy-providers/community-choice-aggregation.html"

# --- RESOLVE FOLDER PATHS ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(SCRIPT_DIR) in ["scripts", "scrapers"]:
    ROOT_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
else:
    ROOT_DIR = SCRIPT_DIR

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

CANDIDATE_PATHS = [
    os.path.join(ROOT_DIR, "rates", "pge_rates.json"),
    os.path.join(ROOT_DIR, "pge_rates.json"),
    os.path.join(SCRIPT_DIR, "rates", "pge_rates.json"),
    os.path.join(SCRIPT_DIR, "pge_rates.json")
]

JSON_FILE = CANDIDATE_PATHS[0]
for p in CANDIDATE_PATHS:
    if os.path.exists(p):
        JSON_FILE = p
        break

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
}

DEFAULT_PGE_CCA_PROFILES = {
    "CLEANPOWERSF": {
        "name": "CleanPowerSF",
        "fullName": "CleanPowerSF (San Francisco)",
        "pciaRate": 0.0150,
        "tiers": {
            "supergreen": { "name": "SuperGreen (100%)", "rateAdder": 0.0200 },
            "green": { "name": "Green (50%)", "rateAdder": -0.0050 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "AVA": {
        "name": "Ava Community Energy",
        "fullName": "Ava Community Energy (East Bay / Alameda)",
        "pciaRate": 0.0150,
        "tiers": {
            "renewable100": { "name": "Renewable 100", "rateAdder": 0.0150 },
            "bright_choice": { "name": "Bright Choice", "rateAdder": -0.0075 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "PCE": {
        "name": "Peninsula Clean Energy",
        "fullName": "Peninsula Clean Energy (San Mateo County)",
        "pciaRate": 0.0150,
        "tiers": {
            "ecogreen": { "name": "ECO100 (100% Renewable)", "rateAdder": 0.0100 },
            "ecoplus": { "name": "ECOplus (50% Renewable)", "rateAdder": -0.0100 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "MCE": {
        "name": "MCE Clean Energy",
        "fullName": "MCE (Marin, Napa, Solano, Contra Costa)",
        "pciaRate": 0.0150,
        "tiers": {
            "deep_green": { "name": "Deep Green (100%)", "rateAdder": 0.0150 },
            "light_green": { "name": "Light Green (60%)", "rateAdder": 0.0 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "SVCE": {
        "name": "Silicon Valley Clean Energy",
        "fullName": "Silicon Valley Clean Energy (Santa Clara County)",
        "pciaRate": 0.0150,
        "tiers": {
            "greenprime": { "name": "GreenPrime (100%)", "rateAdder": 0.0150 },
            "greenstart": { "name": "GreenStart (Standard)", "rateAdder": 0.0 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "SJCE": {
        "name": "San Jose Clean Energy",
        "fullName": "San Jose Clean Energy (City of San Jose)",
        "pciaRate": 0.0150,
        "tiers": {
            "totalgreen": { "name": "TotalGreen (100% Renewable)", "rateAdder": 0.0180 },
            "greensource": { "name": "GreenSource (62% Clean)", "rateAdder": -0.0050 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "3CE_PGE": {
        "name": "Central Coast Energy",
        "fullName": "Central Coast Community Energy (Central Coast)",
        "pciaRate": 0.0150,
        "tiers": {
            "3c_prime": { "name": "3Cprime (100% Clean)", "rateAdder": 0.0150 },
            "3c_choice": { "name": "3Cchoice (Standard)", "rateAdder": -0.0050 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "SCP": {
        "name": "Sonoma Clean Power",
        "fullName": "Sonoma Clean Power (Sonoma & Mendocino)",
        "pciaRate": 0.0150,
        "tiers": {
            "evergreen": { "name": "Evergreen (100% Local)", "rateAdder": 0.0250 },
            "cleanstart": { "name": "CleanStart (Default)", "rateAdder": -0.0050 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "PIONEER": {
        "name": "Pioneer Community Energy",
        "fullName": "Pioneer Community Energy (Placer & El Dorado)",
        "pciaRate": 0.0150,
        "tiers": {
            "green100": { "name": "100% Renewable", "rateAdder": 0.0150 },
            "standard": { "name": "Standard Choice", "rateAdder": -0.0050 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "VCE": {
        "name": "Valley Clean Energy",
        "fullName": "Valley Clean Energy (Yolo County)",
        "pciaRate": 0.0150,
        "tiers": {
            "ultragreen": { "name": "UltraGreen (100% Clean)", "rateAdder": 0.0150 },
            "standardgreen": { "name": "Standard Green", "rateAdder": -0.0050 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    },
    "RCEA": {
        "name": "Redwood Coast Energy",
        "fullName": "Redwood Coast Energy Authority (Humboldt County)",
        "pciaRate": 0.0150,
        "tiers": {
            "repower_plus": { "name": "REpower+ (100% Renewable)", "rateAdder": 0.0100 },
            "repower": { "name": "REpower (Standard)", "rateAdder": -0.0050 },
            "optOut": { "name": "Opted Out (100% PG&E)", "rateAdder": 0.0 }
        }
    }
}


class RateChangeRecord:
    def __init__(self, item_name: str, old_rate: float, new_rate: float):
        self.item_name = item_name
        self.old_rate = round(float(old_rate), 5)
        self.new_rate = round(float(new_rate), 5)
        self.delta = round(self.new_rate - self.old_rate, 5)
        if self.old_rate > 0:
            self.pct_change = round((self.delta / self.old_rate) * 100, 2)
        else:
            self.pct_change = 0.0

    @property
    def status(self) -> str:
        if abs(self.delta) < 0.000001:
            return "UNCHANGED"
        if self.old_rate == 0.0:
            return "NEW ENTRY"
        if abs(self.pct_change) <= 25.0:
            return "REASONABLE"
        return "WARN: LARGE SHIFT"


def clean_val(val):
    if pd.isna(val) or str(val).strip() in ["", "-", "None"]: return 0.0
    s = str(val).replace('$', '').replace(',', '').strip()
    if '(' in s and ')' in s:
        s = "-" + s.replace('(', '').replace(')', '')
    try:
        return float(s)
    except:
        return 0.0


def fetch_pge_solar_clawback_rates():
    print(f"[*] Scanning PG&E AB920 NSC Rate Table PDF: {PGE_NSC_PDF_URL}")
    nsc_rate = 0.03200
    sbp_export_rate = 0.07500
    
    try:
        res = requests.get(PGE_NSC_PDF_URL, headers=HEADERS, timeout=20)
        if res.status_code == 200:
            try:
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(res.content))
                text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
            except ImportError:
                text = res.content.decode('latin1', errors='ignore')
            
            matches = re.findall(r"\$?0\.0\d{3,5}", text)
            if matches:
                cleaned = [float(m.replace("$", "")) for m in matches if float(m.replace("$", "")) > 0.005]
                if cleaned:
                    nsc_rate = cleaned[-1]
                    print(f"  ✓ Parsed Latest PG&E NSC Rate from PDF: ${nsc_rate:.5f}/kWh")
    except Exception as e:
        print(f"  [Notice] NSC PDF fetch notice: {e}")

    return {"nscRate": nsc_rate, "sbpExportRate": sbp_export_rate}


def parse_pge_baseline_allowances(xlsx):
    print("[*] Parsing Baseline Quantities Sheet...")
    extracted_allowances = {t: {"summer": {}, "winter": {}} for t in ["T", "P", "R", "S", "X"]}
    territories = ["T", "P", "R", "S", "X"]
    
    target_sheet = next((name for name in xlsx.sheet_names if "ElecBaseline" in name or "Baseline" in name), None)
    if not target_sheet: return {}

    df = xlsx.parse(target_sheet, header=None)
    current_code_left = "allElectric"
    current_code_right = "allElectric"

    for idx, row in df.iterrows():
        row_str = " ".join([str(cell) for cell in row.dropna().tolist()])
        if "CODE H" in row_str.upper() or "ALL ELEC" in row_str.upper():
            current_code_left = "allElectric"
            current_code_right = "allElectric"
        elif "CODE B" in row_str.upper() or "BASIC ELEC" in row_str.upper():
            current_code_left = "basic"
            current_code_right = "basic"

        for col_idx, cell in enumerate(row):
            cell_str = str(cell).strip().upper()
            t_match = None
            if cell_str in territories: t_match = cell_str
            elif cell_str.startswith("TERRITORY "):
                t = cell_str.replace("TERRITORY ", "").strip()
                if t in territories: t_match = t

            if t_match:
                numeric_vals = []
                for val_idx in range(col_idx + 1, min(col_idx + 4, len(row))):
                    v = clean_val(row.iloc[val_idx])
                    if v > 0: numeric_vals.append(v)
                
                if numeric_vals:
                    individually_metered_val = numeric_vals[0]
                    is_summer_side = col_idx >= 4
                    season = "summer" if is_summer_side else "winter"
                    code_type = current_code_right if is_summer_side else current_code_left
                    extracted_allowances[t_match][season][code_type] = individually_metered_val

    return {t: data for t, data in extracted_allowances.items() if "basic" in data["summer"] or "allElectric" in data["summer"]}


def parse_pge_xlsx(file_path):
    print("[*] Processing PG&E Excel workbook...")
    xlsx = pd.ExcelFile(file_path)
    extracted_data = {}
    baseline_credit_found = None
    
    fixed_fees = {
        "baseServiceStandard": 24.15 / 30.0,
        "baseServiceFERA":     12.00 / 30.0,
        "baseServiceCARE":      6.00 / 30.0,
        "evbMeterCharge":      0.41300
    }

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
        df = xlsx.parse(sheet_name, header=None)
        current_plan_id = None
        current_season = "summer"

        for idx, row in df.iterrows():
            first_cell = str(row.iloc[0]).strip()
            row_str = " ".join([str(i) for i in row.dropna().tolist()])
            row_upper = row_str.upper()

            if "EV, RATE B" in row_upper or "EV-B" in row_upper:
                if ("METER CHARGE" in row_upper or "CUSTOMER CHARGE" in row_upper) and not any(p in row_upper for p in ["PEAK", "OFF-PEAK", "TIER"]):
                    for c in row:
                        val = clean_val(c)
                        if 0.10 <= val <= 1.50:
                            fixed_fees["evbMeterCharge"] = val
                            break

            if ("BASE SERVICE CHARGE" in row_upper or "BASE SERVICES CHARGE" in row_upper) and not any(p in row_upper for p in ["PEAK", "OFF-PEAK", "TIER", "SCHEDULE"]):
                found_numbers = [clean_val(c) for c in row if clean_val(c) > 0]
                for val in found_numbers:
                    if 20.0 <= val <= 30.0: fixed_fees["baseServiceStandard"] = round(val / 30.0, 5)
                    elif 10.0 <= val <= 15.0: fixed_fees["baseServiceFERA"] = round(val / 30.0, 5)
                    elif 4.0 <= val <= 8.0: fixed_fees["baseServiceCARE"] = round(val / 30.0, 5)

            found_anchor = False
            for json_id, markers in plan_identities.items():
                if any(m in first_cell for m in markers):
                    if not any(ex in first_cell for ex in exclusion_markers) or "E1" in first_cell:
                        current_plan_id = json_id
                        found_anchor = True
                        if current_plan_id not in extracted_data:
                            extracted_data[current_plan_id] = {"summer": {}, "winter": {}}
                        break
            
            if not found_anchor and any(ex in first_cell for ex in exclusion_markers) and "E1" not in first_cell:
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
                continue

            is_ev_tech = any(x in current_plan_id for x in ["EV", "ELEC"])
            period_col = 7 if is_ev_tech else 8
            rate_col = 8 if is_ev_tech else 9
            
            if len(row) > max(period_col, rate_col):
                period_cell = str(row.iloc[period_col]).strip()
                if "Peak" in period_cell:
                    rate = clean_val(row.iloc[rate_col])
                    if rate > 0:
                        if period_cell == "Peak": extracted_data[current_plan_id][current_season]["onPeak"] = rate
                        elif period_cell == "Off-Peak":
                            key = "superOffPeak" if is_ev_tech else "offPeak"
                            extracted_data[current_plan_id][current_season][key] = rate
                        elif period_cell in ["Partial-Peak", "Part-Peak"]:
                            extracted_data[current_plan_id][current_season]["offPeak"] = rate

                    if current_plan_id == "E-TOU-C" and len(row) > 10:
                        b_val = clean_val(row.iloc[10])
                        if b_val < 0: baseline_credit_found = abs(b_val)

    extracted_allowances = parse_pge_baseline_allowances(xlsx)
    return extracted_data, baseline_credit_found, extracted_allowances, fixed_fees


def cleanup_bins(data):
    for plan_id in ["E-1 tiered", "E-TOU-C", "E-TOU-D"]:
        if plan_id in data:
            for season in ["summer", "winter"]:
                if season in data[plan_id]:
                    data[plan_id][season]["superOffPeak"] = 0.0
    return data


def flatten_json(d, parent_key="", sep="."):
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_json(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def print_comparison_table(existing_data, new_data, is_dry_run):
    flat_existing = flatten_json(existing_data)
    flat_new = flatten_json(new_data)
    all_keys = sorted(list(set(flat_existing.keys()) | set(flat_new.keys())))

    records: list[RateChangeRecord] = []
    for key in all_keys:
        val_exist = flat_existing.get(key, 0.0)
        val_new = flat_new.get(key, 0.0)
        if isinstance(val_exist, (int, float)) and isinstance(val_new, (int, float)):
            records.append(RateChangeRecord(key, float(val_exist), float(val_new)))

    print("\n" + "=" * 94)
    print("                           PG&E RATE VERIFICATION AUDIT")
    print("=" * 94)
    print(f"{'Tariff / Fixed Item':<38} | {'Old Rate':<10} | {'New Rate':<10} | {'Delta ($)':<10} | {'Shift %':<8} | {'Status'}")
    print("-" * 94)

    updated_count = 0
    warning_count = 0

    for r in records:
        delta_str = f"{r.delta:+.5f}" if abs(r.delta) >= 0.00001 else "$0.00000"
        
        if r.status == "NEW ENTRY":
            pct_str = "NEW"
        elif r.status == "UNCHANGED":
            pct_str = "0.00%"
        else:
            pct_str = f"{r.pct_change:+.2f}%"

        status_tag = f"[{r.status}]"
        if r.status in ["REASONABLE", "NEW ENTRY"]:
            updated_count += 1
        elif r.status == "WARN: LARGE SHIFT":
            updated_count += 1
            warning_count += 1

        print(f"{r.item_name:<38} | ${r.old_rate:<9.5f} | ${r.new_rate:<9.5f} | {delta_str:<10} | {pct_str:<8} | {status_tag}")

    print("=" * 94)
    mode_text = "[DRY RUN: NO FILES MODIFIED]" if is_dry_run else "[COMMITTED TO DISK]"
    print(f"Summary: {len(records)} Total Audited | {updated_count} Changed | {warning_count} Warnings | {mode_text}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print comparison report without writing to disk")
    args = parser.parse_args()

    tmp_xlsx = os.path.join(SCRIPT_DIR, "pge_temp.xlsx")
    try:
        response = requests.get(XLSX_URL, headers=HEADERS, timeout=30)
        response.raise_for_status()
        with open(tmp_xlsx, 'wb') as f:
            f.write(response.content)
    except Exception as e:
        print(f"[Error] Failed to download XLSX: {e}")
        return
    
    try:
        new_plans, b_credit, new_allowances, fixed_fees = parse_pge_xlsx(tmp_xlsx)
        new_plans = cleanup_bins(new_plans)
        solar_rates = fetch_pge_solar_clawback_rates()
    except Exception as e:
        print(f"[Error] Parser Failure: {e}")
        if os.path.exists(tmp_xlsx): os.remove(tmp_xlsx)
        return

    existing_json = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE, 'r') as f:
            existing_json = json.load(f)

    updated_json = json.loads(json.dumps(existing_json))
    updated_json["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    if b_credit: updated_json["baselineCredit"] = b_credit
    if "fixed" not in updated_json: updated_json["fixed"] = {}
    for k, v in fixed_fees.items(): updated_json["fixed"][k] = v

    if "nscRate" in solar_rates: updated_json["nscRate"] = solar_rates["nscRate"]
    if "sbpExportRate" in solar_rates: updated_json["sbpExportRate"] = solar_rates["sbpExportRate"]
    updated_json["nbcRate"] = existing_json.get("nbcRate", 0.02154)

    if new_allowances:
        if "baselineAllowances" not in updated_json: updated_json["baselineAllowances"] = {}
        for t, seasons in new_allowances.items():
            updated_json["baselineAllowances"][t] = seasons

    if "plans" not in updated_json: updated_json["plans"] = {}
    for plan, seasons in new_plans.items():
        if plan not in updated_json["plans"]: updated_json["plans"][plan] = {}
        for season, bins in seasons.items():
            if season not in updated_json["plans"][plan]: updated_json["plans"][plan][season] = {}
            for b_type, rate in bins.items():
                if rate > 0: updated_json["plans"][plan][season][b_type] = rate

    if "cca" not in updated_json: updated_json["cca"] = {}
    for cca_id, profile in DEFAULT_PGE_CCA_PROFILES.items():
        updated_json["cca"][cca_id] = profile

    print_comparison_table(existing_json, updated_json, is_dry_run=args.dry_run)

    if not args.dry_run:
        os.makedirs(os.path.dirname(JSON_FILE), exist_ok=True)
        with open(JSON_FILE, 'w') as f:
            json.dump(updated_json, f, indent=2)
        print(f"[SUCCESS] Updated PG&E rates committed to {JSON_FILE}")

    if os.path.exists(tmp_xlsx): os.remove(tmp_xlsx)


if __name__ == "__main__":
    main()
