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

def parse_pge_marketing_pdf(pdf_path):
    print(f"\n[Scanning PDF Content] {os.path.basename(pdf_path)}")
    
    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"
    
    # Flatten whitespace to make regex easier across line breaks
    text = " ".join(full_text.split())

    extracted = {
        "E-1 tiered": {"summer": {}, "winter": {}},
        "E-TOU-C": {"summer": {}, "winter": {}},
        "E-TOU-D": {"summer": {}, "winter": {}},
        "E-ELEC": {"summer": {}, "winter": {}},
        "EV2-A": {"summer": {}, "winter": {}},
        "EV-B": {"summer": {}, "winter": {}}
    }

    # 1. E-1 Tiered Logic (Direct search)
    e1_match = re.search(r"Tier 1.*?(\d+)¢.*?Tier 2.*?(\d+)¢", text)
    if e1_match:
        extracted["E-1 tiered"]["summer"]["onPeak"] = float(e1_match.group(2)) / 100
        extracted["E-1 tiered"]["summer"]["offPeak"] = float(e1_match.group(1)) / 100
        extracted["E-1 tiered"]["winter"] = extracted["E-1 tiered"]["summer"]

    # 2. E-TOU-C Logic (Peak 4-9)
    # Sequence in text: 40¢ ... 32¢ 44¢ 32¢ (Summer) / 37¢ ... 29¢ 32¢ 29¢ (Winter)
    # We take the "Above Baseline" rates (44/32 and 32/29)
    etc_s = re.search(r"E-TOU-C.*?Summer Season.*?(\d+)¢.*?(\d+)¢ (\d+)¢ (\d+)¢", text)
    if etc_s:
        extracted["E-TOU-C"]["summer"]["onPeak"] = float(etc_s.group(3)) / 100 # 44
        extracted["E-TOU-C"]["summer"]["offPeak"] = float(etc_s.group(4)) / 100 # 32
    
    etc_w = re.search(r"Winter Season Oct 1–May 31 (\d+)¢.*?(\d+)¢ (\d+)¢ (\d+)¢", text)
    if etc_w:
        extracted["E-TOU-C"]["winter"]["onPeak"] = float(etc_w.group(3)) / 100 # 32
        extracted["E-TOU-C"]["winter"]["offPeak"] = float(etc_w.group(4)) / 100 # 29

    # 3. E-TOU-D Logic (Peak 5-8)
    # Sequence: 52¢ 40¢ 34¢ 48¢ 34¢ (Summer) -> [Peak Above, Peak Below, Off]
    etd_s = re.search(r"E-TOU-D.*?Summer Season.*?(\d+)¢ (\d+)¢ (\d+)¢", text)
    if etd_s:
        extracted["E-TOU-D"]["summer"]["onPeak"] = float(etd_s.group(1)) / 100 # 52
        extracted["E-TOU-D"]["summer"]["offPeak"] = float(etd_s.group(3)) / 100 # 34
    
    etd_w = re.search(r"E-TOU-D.*?Winter Season.*?(\d+)¢ (\d+)¢ (\d+)¢", text)
    if etd_w:
        extracted["E-TOU-D"]["winter"]["onPeak"] = float(etd_w.group(2)) / 100 # 39
        extracted["E-TOU-D"]["winter"]["offPeak"] = float(etd_w.group(1)) / 100 # 35

    # 4. E-ELEC Logic
    # Sequence: 55¢ 33¢ 39¢ (Summer) / 32¢ 28¢ 30¢ (Winter)
    elec_s = re.search(r"E-ELEC.*?Summer Season.*?(\d+)¢ (\d+)¢ (\d+)¢", text)
    if elec_s:
        extracted["E-ELEC"]["summer"]["onPeak"] = float(elec_s.group(1)) / 100 # 55
        extracted["E-ELEC"]["summer"]["offPeak"] = float(elec_s.group(3)) / 100 # 39 (Partial)
        extracted["E-ELEC"]["summer"]["superOffPeak"] = float(elec_s.group(2)) / 100 # 33
        
    elec_w = re.search(r"E-ELEC.*?Winter Season.*?(\d+)¢ (\d+)¢ (\d+)¢", text)
    if elec_w:
        extracted["E-ELEC"]["winter"]["onPeak"] = float(elec_w.group(1)) / 100 # 32
        extracted["E-ELEC"]["winter"]["offPeak"] = float(elec_w.group(3)) / 100 # 30
        extracted["E-ELEC"]["winter"]["superOffPeak"] = float(elec_w.group(2)) / 100 # 28

    # 5. EV2-A and EV-B Logic (Interleaved Sequence)
    # The text dump shows: "23¢ 43¢ 54¢ 26¢ 38¢ 38¢ 62¢"
    ev_s = re.search(r"EV2-A.*?EV-B.*?Summer.*?Summer.*?(\d+)¢ (\d+)¢ (\d+)¢ (\d+)¢ (\d+)¢ (\d+)¢ (\d+)¢", text)
    if ev_s:
        extracted["EV2-A"]["summer"] = {"superOffPeak": float(ev_s.group(1))/100, "offPeak": float(ev_s.group(2))/100, "onPeak": float(ev_s.group(3))/100}
        extracted["EV-B"]["summer"] = {"superOffPeak": float(ev_s.group(4))/100, "offPeak": float(ev_s.group(5))/100, "onPeak": float(ev_s.group(7))/100}

    ev_w = re.search(r"EV2-A.*?EV-B.*?Winter.*?Winter.*?(\d+)¢ (\d+)¢ (\d+)¢ (\d+)¢ (\d+)¢ (\d+)¢ (\d+)¢", text)
    if ev_w:
        extracted["EV2-A"]["winter"] = {"superOffPeak": float(ev_w.group(1))/100, "offPeak": float(ev_w.group(2))/100, "onPeak": float(ev_w.group(3))/100}
        extracted["EV-B"]["winter"] = {"superOffPeak": float(ev_w.group(4))/100, "offPeak": float(ev_w.group(5))/100, "onPeak": float(ev_w.group(7))/100}

    return extracted

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
            for b_type in ["onPeak", "offPeak", "superOffPeak"]:
                rate = seasons[season].get(b_type, 0)
                if rate == 0: continue
                
                current_val = current_json["plans"][plan][season].get(b_type, 0)
                diff = abs(rate - current_val)
                status = "[MATCH]" if diff < 0.00001 else "[CHANGE DETECTED]"
                
                print(f"  {status} {plan:12} ({season:6} {b_type:12}): JSON=${current_val:.5f} | PDF=${rate:.5f}")

                # Using 0.01 threshold to protect precision data
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
            print("\n>>> Result: Dry Run complete. Changes were found.")
    else:
        print("\n>>> Result: No significant changes detected.")

    if os.path.exists(tmp_pdf): os.remove(tmp_pdf)

if __name__ == "__main__":
    main()
