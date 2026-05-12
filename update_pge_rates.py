import os
import re
import json
import sys
import requests
import pdfplumber
import argparse
from datetime import datetime

# --- CONFIGURATION ---
PGE_URL = "https://www.pge.com/assets/pge/docs/account/rate-plans/residential-electric-rate-plan-pricing.pdf"
JSON_FILE = "pge_rates.json"

def download_pdf(url, save_path):
    print(f"[Network] Downloading PDF from: {url}")
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        with open(save_path, 'wb') as f:
            f.write(response.content)
    except Exception as e:
        print(f"[Error] Failed to download PDF: {e}")
        sys.exit(1)

def extract_logic(rates, plan_id):
    """
    Applies logic to unique rates extracted from a plan's segment.
    Ensures On-Peak is always the highest.
    """
    # Remove duplicates and sort descending
    unique = sorted(list(set(rates)), reverse=True)
    if not unique: return None

    # 3-Bin Plans (E-ELEC, EV2-A, EV-B)
    if any(x in plan_id for x in ["ELEC", "EV"]):
        if len(unique) >= 3:
            return {"onPeak": unique[0], "offPeak": unique[1], "superOffPeak": unique[2]}
        return {"onPeak": unique[0], "offPeak": unique[-1], "superOffPeak": unique[-1]}

    # 2-Bin Plans (E-1, TOU-C, TOU-D)
    # Picks the highest two values (which maps to 'Above Baseline' for TOU-C/D)
    if len(unique) >= 2:
        return {"onPeak": unique[0], "offPeak": unique[1]}
    
    return {"onPeak": unique[0], "offPeak": unique[0]}

def parse_pge_marketing_pdf(pdf_path):
    print(f"\n[Contextual Stream Scan] Analyzing {os.path.basename(pdf_path)}...")
    
    with pdfplumber.open(pdf_path) as pdf:
        full_text = " ".join([ (p.extract_text() or "") for p in pdf.pages ])
    
    # Flatten all whitespace to ensure reliable indexing
    text = " ".join(full_text.split())

    # We define 'Moats' (start/end anchors) to isolate plan text
    moats = {
        "E-1 tiered": ("Tiered Rate Plan (E-1)", "Time-of-Use Rate Plans"),
        "E-TOU-C": ("Time-of-Use (E-TOU-C)", "Time-of-Use (E-TOU-D)"),
        "E-TOU-D": ("Time-of-Use (E-TOU-D)", "Electric Home Rate Plan"),
        "E-ELEC": ("Electric Home Rate Plan (E-ELEC)", "Electric Vehicle (EV)"),
        "EV-Both": ("Electric Vehicle (EV) Rate Plans", "The Electric Home Rate Plan includes")
    }

    raw_data = {}

    # Extract raw cent pools for each moat
    for plan_id, (start_m, end_m) in moats.items():
        start_idx = text.find(start_m)
        end_idx = text.find(end_m)
        if start_idx == -1: continue
        if end_idx == -1: end_idx = len(text)
        
        segment = text[start_idx:end_idx]
        
        # Split segment into Summer/Winter chunks
        s_idx = segment.find("Summer")
        w_idx = segment.find("Winter")

        if s_idx != -1 and w_idx != -1:
            summer_pool = [float(m)/100 for m in re.findall(r"(\d+)¢", segment[s_idx:w_idx])]
            winter_pool = [float(m)/100 for m in re.findall(r"(\d+)¢", segment[w_idx:])]
            raw_data[plan_id] = {"summer": summer_pool, "winter": winter_pool}
        else:
            # Plan without seasonal labels (E-1)
            pool = [float(m)/100 for m in re.findall(r"(\d+)¢", segment)]
            raw_data[plan_id] = {"summer": pool, "winter": pool}

    # Final Mapping
    final_data = {}

    # Standard Plans
    for p_id in ["E-1 tiered", "E-TOU-C", "E-TOU-D", "E-ELEC"]:
        if p_id in raw_data:
            final_data[p_id] = {
                "summer": extract_logic(raw_data[p_id]["summer"], p_id),
                "winter": extract_logic(raw_data[p_id]["winter"], p_id)
            }

    # Interleaved EV Plans (Special Logic)
    # The text dump shows EV2A and EVB rates appear in a single sequence of 7 rates:
    # [EV2A-Off, EV2A-Mid, EV2A-On, EVB-Off, EVB-Mid, EVB-Mid-Dup, EVB-On]
    if "EV-Both" in raw_data:
        for season in ["summer", "winter"]:
            pool = raw_data["EV-Both"][season]
            if len(pool) >= 7:
                # EV2-A Mapping (Indices 0, 1, 2)
                ev2a_pool = pool[0:3]
                final_data["EV2-A"] = final_data.get("EV2-A", {"summer": {}, "winter": {}})
                final_data["EV2-A"][season] = extract_logic(ev2a_pool, "EV2-A")
                
                # EV-B Mapping (Indices 3, 4, 5, 6)
                evb_pool = pool[3:7]
                final_data["EV-B"] = final_data.get("EV-B", {"summer": {}, "winter": {}})
                final_data["EV-B"][season] = extract_logic(evb_pool, "EV-B")

    return final_data

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run: print("\n!!! DRY RUN MODE: No files will be modified !!!")

    tmp_pdf = "pge_temp.pdf"
    download_pdf(PGE_URL, tmp_pdf)
    new_data = parse_pge_marketing_pdf(tmp_pdf)
    
    with open(JSON_FILE, 'r') as f:
        current_json = json.load(f)

    print("\n[Comparison Ledger: JSON vs Scraped]")
    updated = False
    
    for plan, seasons in new_data.items():
        if plan not in current_json["plans"]: continue
        for season in ["summer", "winter"]:
            plan_results = seasons.get(season)
            if not plan_results: continue
            
            for b_type in ["onPeak", "offPeak", "superOffPeak"]:
                rate = plan_results.get(b_type, 0)
                if rate == 0: continue
                
                current_val = current_json["plans"][plan][season].get(b_type, 0)
                diff = abs(rate - current_val)
                status = "[MATCH]" if diff < 0.00001 else "[CHANGE DETECTED]"
                
                print(f"  {status} {plan:12} ({season:6} {b_type:12}): JSON=${current_val:.5f} | PDF=${rate:.5f}")

                if diff > 0.01: 
                    current_json["plans"][plan][season][b_type] = rate
                    updated = True

    if updated:
        if not args.dry_run:
            current_json["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(JSON_FILE, 'w') as f:
                json.dump(current_json, f, indent=2)
            print("\n>>> Result: Changes committed to JSON.")
        else:
            print("\n>>> Result: Dry Run complete. Data verified against manual list.")
    else:
        print("\n>>> Result: No significant changes detected.")

    if os.path.exists(tmp_pdf): os.remove(tmp_pdf)

if __name__ == "__main__":
    main()
