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
    Intelligently maps raw extracted rates to the correct app bins.
    High-to-low sorting ensures On-Peak is always the highest.
    """
    # Remove duplicates and sort descending (highest price first)
    unique = sorted(list(set(rates)), reverse=True)
    if not unique: return None

    # --- 3-BIN PLANS (E-ELEC, EV2-A, EV-B) ---
    if any(x in plan_id for x in ["ELEC", "EV"]):
        if len(unique) >= 3:
            # Sequence: [Highest=On, Middle=Partial/Off, Lowest=Super-Off]
            return {"onPeak": unique[0], "offPeak": unique[1], "superOffPeak": unique[2]}
        return {"onPeak": unique[0], "offPeak": unique[-1], "superOffPeak": unique[-1]}

    # --- 2-BIN PLANS (E-1, TOU-C, TOU-D) ---
    # For TOU-C/D, unique usually contains 4 values (Above and Below baseline).
    # We take the highest pair for Prediction Accuracy (Above Baseline).
    if len(unique) >= 2:
        return {"onPeak": unique[0], "offPeak": unique[1], "superOffPeak": 0.0}
    
    return {"onPeak": unique[0], "offPeak": unique[0], "superOffPeak": 0.0}

def parse_pge_marketing_pdf(pdf_path):
    print(f"\n[Pattern Scan] Analyzing {os.path.basename(pdf_path)}...")
    
    with pdfplumber.open(pdf_path) as pdf:
        full_text = " ".join([ (p.extract_text() or "") for p in pdf.pages ])
    
    # Flatten text stream
    text = " ".join(full_text.split())

    # Map for mapping the PDF's text markers to your JSON IDs
    # Using regex segments to find plan-specific chunks
    plan_segments = {
        "E-1 tiered": r"(Tiered Rate Plan \(E-1\).*?)Time-of-Use",
        "E-TOU-C": r"(Time-of-Use \(E-TOU-C\).*?)Time-of-Use \(E-TOU-D\)",
        "E-TOU-D": r"(Time-of-Use \(E-TOU-D\).*?)Electric Home Rate Plan",
        "E-ELEC": r"(Electric Home Rate Plan \(E-ELEC\).*?)Electric Vehicle",
        "EV2-A": r"(Home Charging EV2-A.*?)Electric Vehicle Rate Plan EV-B",
        "EV-B": r"(Electric Vehicle Rate Plan EV-B.*?)The Electric Home Rate Plan"
    }

    final_data = {}

    for plan_id, pattern in plan_segments.items():
        match = re.search(pattern, text)
        if not match:
            print(f"  [Warn] Failed to find text bucket for {plan_id}")
            continue
            
        bucket = match.group(1)
        
        # Split bucket into Summer and Winter chunks
        s_marker = bucket.find("Summer")
        w_marker = bucket.find("Winter")

        if s_marker != -1 and w_marker != -1:
            # Plan has seasonal sections
            if s_marker < w_marker:
                s_chunk, w_chunk = bucket[s_marker:w_marker], bucket[w_marker:]
            else:
                w_chunk, s_chunk = bucket[w_marker:s_marker], bucket[s_marker:]
            
            s_pool = [float(m)/100 for m in re.findall(r"(\d+)¢", s_chunk)]
            w_pool = [float(m)/100 for m in re.findall(r"(\d+)¢", w_chunk)]
            
            # Special case for E-TOU-C Summer: 40¢ is at the start of the header
            if plan_id == "E-TOU-C":
                # Look slightly before the Summer marker for the first Off-Peak rate
                header_rates = [float(m)/100 for m in re.findall(r"(\d+)¢", bucket[:s_marker])]
                s_pool = header_rates + s_pool

            final_data[plan_id] = {
                "summer": extract_logic(s_pool, plan_id),
                "winter": extract_logic(w_pool, plan_id)
            }
        else:
            # Plan is non-seasonal (E-1)
            pool = [float(m)/100 for m in re.findall(r"(\d+)¢", bucket)]
            final_data[plan_id] = {"summer": extract_logic(pool, plan_id), "winter": extract_logic(pool, plan_id)}

    return final_data

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run: print("\n!!! DRY RUN MODE: No files will be modified !!!")

    tmp_pdf = "pge_temp.pdf"
    download_pdf(PGE_URL, tmp_pdf)
    new_data = parse_pge_marketing_pdf(tmp_pdf)
    
    if not os.path.exists(JSON_FILE):
        print(f"[Error] {JSON_FILE} not found.")
        return

    with open(JSON_FILE, 'r') as f:
        current_json = json.load(f)

    print("\n[Comparison Ledger: JSON vs Scraped]")
    updated = False
    
    for plan in ["E-1 tiered", "E-TOU-C", "E-TOU-D", "E-ELEC", "EV2-A", "EV-B"]:
        if plan not in new_data: continue
        
        for season in ["summer", "winter"]:
            plan_results = new_data[plan].get(season)
            if not plan_results: continue
            
            for b_type in ["onPeak", "offPeak", "superOffPeak"]:
                rate = plan_results.get(b_type, 0)
                if rate == 0: continue
                
                current_val = current_json["plans"][plan][season].get(b_type, 0)
                diff = abs(rate - current_val)
                status = "[MATCH]" if diff < 0.00001 else "[CHANGE DETECTED]"
                
                print(f"  {status} {plan:12} ({season:6} {b_type:12}): JSON=${current_val:.5f} | PDF=${rate:.5f}")

                # Update JSON if change > $0.01 (protects precision data)
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
            print("\n>>> Result: Dry Run complete. All plans successfully matched.")
    else:
        print("\n>>> Result: No significant changes detected.")

    if os.path.exists(tmp_pdf): os.remove(tmp_pdf)

if __name__ == "__main__":
    main()
