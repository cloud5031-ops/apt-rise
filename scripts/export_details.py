import os
import json
import sqlite3
import argparse
import config
from utils import get_dynamic_months

def get_target_months(ref_month_str):
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--region-group", required=True, help="권역 이름")
    parser.add_argument("--stable-month", help="고정 기준월 (안정 집계)")
    parser.add_argument("--provisional-month", help="고정 기준월 (잠정 집계)")
    args = parser.parse_args()

    if args.stable_month and args.provisional_month:
        stable_month = args.stable_month
        provisional_month = args.provisional_month
    else:
        months_info = get_dynamic_months()
        stable_month = months_info["stableMonth"]
        provisional_month = months_info["provisionalMonth"]

    keys_to_export = {} # key -> dict of info
    ref_months = set([stable_month, provisional_month])
    
    def process_shard(month, status):
        shard_path = os.path.join(config.SHARDS_DIR, month, status, f"{args.region_group}.json")
        if os.path.exists(shard_path):
            try:
                with open(shard_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data.get("items", []):
                        apt_key = item["apartmentKey"]
                        if apt_key not in keys_to_export:
                            keys_to_export[apt_key] = {
                                "sidoCode": item["sidoCode"],
                                "sggCode": item["sggCode"],
                                "apartmentName": item.get("apartmentName", "")
                            }
            except Exception as e:
                print(f"Error reading shard {shard_path}: {e}")

    process_shard(stable_month, "stable")
    process_shard(provisional_month, "provisional")

    success_count = 0
    zero_count = 0
    fail_count = 0
    failed_keys = []

    if not keys_to_export:
        print(f"No apartments to export for region {args.region_group}.")
        write_summary(args.region_group, 0, 0, 0, [])
        return

    target_months = set()
    for rm in ref_months:
        target_months.update(get_target_months(rm))

    db_path = config.DB_PATH
    if not os.path.exists(db_path):
        print("DB not found.")
        write_summary(args.region_group, 0, 0, len(keys_to_export), list(keys_to_export.keys()))
        return

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    c = con.cursor()

    out_dir_base = os.path.join(config.SITE_DATA_DIR, "details")
    os.makedirs(out_dir_base, exist_ok=True)
    months_placeholder = ",".join("?" for _ in target_months)
    target_months_list = list(target_months)

    for apt_key, info in keys_to_export.items():
        sgg = info["sggCode"]
        sgg_dir = os.path.join(out_dir_base, sgg)
        os.makedirs(sgg_dir, exist_ok=True)
        out_path = os.path.join(sgg_dir, f"{apt_key}.json")

        try:
            c.execute(f"""
                SELECT umd_name, exclusive_area, area_group, deal_amount, deal_date, floor, is_cancelled, dealing_type
                FROM apartment_trades
                WHERE apartment_key = ? AND deal_month IN ({months_placeholder})
                ORDER BY deal_date DESC
            """, [apt_key] + target_months_list)

            rows = c.fetchall()

            if not rows:
                zero_count += 1
                # If there's an existing valid file, don't overwrite it with empty transactions
                if not os.path.exists(out_path):
                    # We can still write a skeleton JSON
                    pass

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

            # Don't overwrite if 0 transactions and file already exists
            if not transactions and os.path.exists(out_path):
                # keep old file
                continue

            out_data = {
                "apartmentKey": apt_key,
                "apartmentName": info["apartmentName"],
                "sidoCode": info["sidoCode"],
                "sggCode": sgg,
                "dongName": dong_name,
                "referenceMonths": sorted(list(ref_months)),
                "availableAreas": sorted(list(available_areas)),
                "transactions": transactions
            }

            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(out_data, f, ensure_ascii=False, indent=1)
            
            if transactions:
                success_count += 1

        except Exception as e:
            print(f"Error processing {apt_key}: {e}")
            fail_count += 1
            failed_keys.append(apt_key)

    con.close()
    print(f"Export completed for {args.region_group}. Success: {success_count}, Zero: {zero_count}, Fail: {fail_count}")
    write_summary(args.region_group, success_count, zero_count, fail_count, failed_keys)

def write_summary(region, success, zero, fail, failed_keys):
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"### 🏢 상세 JSON 생성 리포트 ({region})\n")
            f.write(f"- **성공 단지 (거래 있음)**: {success}건\n")
            f.write(f"- **상세 거래 0건 단지**: {zero}건\n")
            f.write(f"- **실패 단지**: {fail}건\n")
            if failed_keys:
                f.write(f"<details><summary>실패한 apartmentKey 목록</summary>\n\n```text\n{', '.join(failed_keys)}\n```\n</details>\n")
            f.write("\n")

if __name__ == "__main__":
    export_details()
