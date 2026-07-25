import os
import json
import sqlite3
import config
from utils import get_dynamic_months

def get_target_months(ref_month_str):
    # returns list of 5 months ending in ref_month_str
    y = int(ref_month_str[:4])
    m = int(ref_month_str[4:])
    months = []
    for i in range(5):
        tm = m - i
        ty = y
        if tm <= 0:
            tm += 12
            ty -= 1
        months.append(f"{ty:04d}{tm:02d}")
    return sorted(months)

def export_details():
    manifest_path = os.path.join(config.SITE_DATA_DIR, "apt_rankings_manifest.json")
    if not os.path.exists(manifest_path):
        print("Manifest not found.")
        return
        
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
        
    stable_file = manifest.get("stableFile")
    prov_file = manifest.get("provisionalFile")
    
    keys_to_export = set()
    ref_months = set()
    
    def process_file(filename):
        if not filename: return
        p = os.path.join(config.SITE_DATA_DIR, filename)
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
                rm = data.get("referenceMonth")
                if rm: ref_months.add(rm)
                for item in data.get("items", []):
                    keys_to_export.add((item["apartmentKey"], item["sidoCode"], item["sggCode"], item.get("apartmentName", "")))
                    
    process_file(stable_file)
    process_file(prov_file)
    
    if not keys_to_export:
        print("No apartments to export.")
        return
        
    target_months = set()
    for rm in ref_months:
        target_months.update(get_target_months(rm))
        
    db_path = config.DB_PATH
    if not os.path.exists(db_path):
        print("DB not found.")
        return
        
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    c = con.cursor()
    
    # Batch keys by sggCode
    sgg_groups = {}
    for apt_key, sido, sgg, apt_name in keys_to_export:
        if sgg not in sgg_groups:
            sgg_groups[sgg] = []
        sgg_groups[sgg].append((apt_key, sido, apt_name))
        
    out_dir_base = os.path.join(config.SITE_DATA_DIR, "details")
    os.makedirs(out_dir_base, exist_ok=True)
    
    months_placeholder = ",".join("?" for _ in target_months)
    
    for sgg, apts in sgg_groups.items():
        sgg_dir = os.path.join(out_dir_base, sgg)
        os.makedirs(sgg_dir, exist_ok=True)
        
        for apt_key, sido, apt_name in apts:
            # Query trades
            c.execute(f"""
                SELECT umd_name, exclusive_area, area_group, deal_amount, deal_date, floor, is_cancelled, dealing_type
                FROM apartment_trades
                WHERE apartment_key = ? AND deal_month IN ({months_placeholder})
                ORDER BY deal_date DESC
            """, [apt_key] + list(target_months))
            
            rows = c.fetchall()
            
            # Aggregate available areas and format transactions
            transactions = []
            available_areas = set()
            dong_name = ""
            
            for r in rows:
                if not dong_name: dong_name = r['umd_name']
                available_areas.add(r['exclusive_area'])
                
                transactions.append({
                    "contractDate": r['deal_date'],
                    "price": r['deal_amount'],
                    "floor": r['floor'],
                    "exclusiveArea": r['exclusive_area'],
                    "areaGroup": r['area_group'],
                    "dealType": r['dealing_type'],
                    "cancellationStatus": "CANCELLED" if r['is_cancelled'] else "COMPLETED"
                })
                
            out_data = {
                "apartmentKey": apt_key,
                "apartmentName": apt_name,
                "sidoCode": sido,
                "sggCode": sgg,
                "dongName": dong_name,
                "referenceMonths": sorted(list(ref_months)),
                "availableAreas": sorted(list(available_areas)),
                "transactions": transactions
            }
            
            with open(os.path.join(sgg_dir, f"{apt_key}.json"), 'w', encoding='utf-8') as f:
                json.dump(out_data, f, ensure_ascii=False, indent=1)

    con.close()
    print(f"Exported {len(keys_to_export)} apartment detail JSONs.")

if __name__ == "__main__":
    export_details()
