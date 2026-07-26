import os
import sys
import json
import sqlite3
import argparse
import requests
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
SITE_DIR = PROJECT_ROOT / "site"
DETAILS_DIR = SITE_DIR / "data" / "details"
GEOCODE_CACHE_PATH = DATA_DIR / "geocoding" / "apartment_coordinates.json"
SHARDS_DIR = DATA_DIR / "shards"
DB_PATH = DATA_DIR / "apt.sqlite"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from utils import get_dynamic_months
import config

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

def load_geocode_cache():
    if GEOCODE_CACHE_PATH.exists():
        with open(GEOCODE_CACHE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_geocode_cache(cache):
    GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GEOCODE_CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def get_existing_valid_location(out_path):
    if not out_path.exists():
        return None
    try:
        with open(out_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            loc = data.get("location")
            if loc and loc.get("geocodeStatus") == "valid":
                return loc
    except Exception:
        pass
    return None

def geocode_apartment(apt_key, sido_name, sgg_name, dong_name, jibun, cache, stats, existing_loc):
    if apt_key in cache:
        status = cache[apt_key].get("geocodeStatus")
        if status == "valid":
            stats['geocode_valid'] += 1
            return cache[apt_key]
        elif status == "ambiguous":
            stats['geocode_ambiguous'] += 1
            return cache[apt_key]
        elif status == "not_found":
            stats['geocode_not_found'] += 1
            return cache[apt_key]

    api_key = getattr(config, "KAKAO_REST_API_KEY", os.environ.get("KAKAO_REST_API_KEY", ""))
    if not api_key:
        stats['geocode_api_error'] += 1
        return None

    query = f"{dong_name} {jibun}"
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {api_key}"}
    
    try:
        time.sleep(0.05)
        res = requests.get(url, headers=headers, params={"query": query}, timeout=5)
        if res.status_code == 200:
            data = res.json()
            docs = data.get("documents", [])
            if len(docs) > 0:
                doc = docs[0]
                status = "valid"
                if len(docs) > 1:
                    status = "ambiguous"
                    stats['geocode_ambiguous'] += 1
                else:
                    stats['geocode_valid'] += 1
                
                cache[apt_key] = {
                    "apartmentKey": apt_key,
                    "latitude": float(doc.get("y")) if doc.get("y") else None,
                    "longitude": float(doc.get("x")) if doc.get("x") else None,
                    "roadAddress": doc.get("road_address", {}).get("address_name") if doc.get("road_address") else "",
                    "jibunAddress": doc.get("address", {}).get("address_name") if doc.get("address") else "",
                    "geocodeStatus": status,
                    "queryUsed": query,
                    "geocodedAt": datetime.now().isoformat()
                }
            else:
                stats['geocode_not_found'] += 1
                cache[apt_key] = {
                    "apartmentKey": apt_key,
                    "latitude": None,
                    "longitude": None,
                    "roadAddress": None,
                    "jibunAddress": None,
                    "geocodeStatus": "not_found",
                    "queryUsed": query,
                    "geocodedAt": datetime.now().isoformat()
                }
            return cache[apt_key]
        else:
            stats['geocode_api_error'] += 1
            return None
    except Exception as e:
        print(f"Geocoding error for {apt_key}: {e}")
        stats['geocode_api_error'] += 1
        return None

def export_details():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region-group", required=True, help="권역 이름")
    parser.add_argument("--stable-month", help="고정 기준월 (안정 집계)")
    parser.add_argument("--provisional-month", help="고정 기준월 (잠정 집계)")
    args = parser.parse_args()

    if not PROJECT_ROOT.exists():
        raise FileNotFoundError(f"PROJECT_ROOT does not exist: {PROJECT_ROOT}")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB_PATH does not exist: {DB_PATH}")

    if args.stable_month and args.provisional_month:
        stable_month = args.stable_month
        provisional_month = args.provisional_month
    else:
        months_info = get_dynamic_months()
        stable_month = months_info["stableMonth"]
        provisional_month = months_info["provisionalMonth"]

    stable_shard_path = SHARDS_DIR / stable_month / "stable" / f"{args.region_group}.json"
    provisional_shard_path = SHARDS_DIR / provisional_month / "provisional" / f"{args.region_group}.json"

    if not stable_shard_path.exists() and not provisional_shard_path.exists():
        print(f"No shards found for region {args.region_group}.")
        return

    DETAILS_DIR.mkdir(parents=True, exist_ok=True)
    GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    keys_to_export = {}
    ref_months = set([stable_month, provisional_month])
    
    def process_shard(path):
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
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
                print(f"Error reading shard {path}: {e}")

    process_shard(stable_shard_path)
    process_shard(provisional_shard_path)

    stats = {
        'target_count': len(keys_to_export),
        'generated_with_transactions': 0,
        'preserved_existing': 0,
        'skipped_no_transactions': 0,
        'geocode_valid': 0,
        'geocode_ambiguous': 0,
        'geocode_not_found': 0,
        'geocode_api_error': 0,
        'failed': 0
    }
    failed_keys = []

    if not keys_to_export:
        print(f"No apartments to export for region {args.region_group}.")
        write_summary(args.region_group, stats, failed_keys)
        return

    target_months = set()
    for rm in ref_months:
        target_months.update(get_target_months(rm))

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    c = con.cursor()

    months_placeholder = ",".join("?" for _ in target_months)
    target_months_list = list(target_months)

    geocode_cache = load_geocode_cache()
    geocode_updated = False

    for apt_key, info in keys_to_export.items():
        sgg = info["sggCode"]
        sgg_dir = DETAILS_DIR / sgg
        sgg_dir.mkdir(parents=True, exist_ok=True)
        out_path = sgg_dir / f"{apt_key}.json"

        try:
            c.execute(f"""
                SELECT umd_name, jibun, exclusive_area, area_group, deal_amount, deal_date, floor, is_cancelled, dealing_type
                FROM apartment_trades
                WHERE apartment_key = ? AND deal_month IN ({months_placeholder})
                ORDER BY deal_date DESC
            """, [apt_key] + target_months_list)

            rows = c.fetchall()
            
            transactions = []
            available_areas = set()
            dong_name = ""
            jibun = ""

            for r in rows:
                if not dong_name: dong_name = r['umd_name']
                if not jibun: jibun = r['jibun']
                
                # Check cancellation (user logic: exclude cancelled transactions from valid checks?
                # Actually, the user says "유효 거래란: 계약일과 가격이 유효함, 해제 거래가 아님"
                # If they are all cancelled, then transactions length for valid might be 0. 
                # Wait, backend currently sends ALL transactions and frontend filters.
                # User's exact words: "해당 apartmentKey의 유효 거래가 0건이면... 신규 생성하지 않음. 유효 거래란: 해제 거래가 아님"
                
                is_cancelled = bool(r['is_cancelled'])
                if not is_cancelled:
                    pass # It's a valid trade
                
                available_areas.add(r['exclusive_area'])
                transactions.append({
                    "contractDate": r['deal_date'],
                    "priceWon": r['deal_amount'],
                    "floor": r['floor'],
                    "exclusiveArea": r['exclusive_area'],
                    "areaGroup": r['area_group'],
                    "dealType": r['dealing_type'],
                    "cancellationStatus": "CANCELLED" if is_cancelled else "COMPLETED"
                })

            valid_transactions = [t for t in transactions if t["cancellationStatus"] == "COMPLETED"]

            if not valid_transactions:
                if out_path.exists():
                    stats['preserved_existing'] += 1
                else:
                    stats['skipped_no_transactions'] += 1
                continue

            existing_loc = get_existing_valid_location(out_path)
            geo = geocode_apartment(apt_key, info["sidoCode"], sgg, dong_name, jibun, geocode_cache, stats, existing_loc)
            if geo and not geocode_updated:
                # If it's freshly geocoded and returned something valid/not_found/etc
                # Wait, if we return from cache, we don't necessarily need to update the file, but we'll see.
                # Actually, if we just fetched from API, geocode_updated will be true because we added to cache.
                pass

            # Update cache if it was newly added (simplistic way: check length before and after, or just always save if modified)
            
            out_data = {
                "schemaVersion": 2,
                "priceUnit": "KRW",
                "apartmentKey": apt_key,
                "apartmentName": info["apartmentName"],
                "sidoCode": info["sidoCode"],
                "sggCode": sgg,
                "dongName": dong_name,
                "referenceMonths": sorted(list(ref_months)),
                "availableAreas": sorted(list(available_areas)),
                "transactions": transactions
            }

            if geo:
                out_data["location"] = {
                    "latitude": float(geo.get("latitude")) if geo.get("latitude") is not None else None,
                    "longitude": float(geo.get("longitude")) if geo.get("longitude") is not None else None,
                    "roadAddress": geo.get("roadAddress"),
                    "jibunAddress": geo.get("jibunAddress"),
                    "geocodeStatus": geo.get("geocodeStatus")
                }
            elif existing_loc:
                out_data["location"] = existing_loc
            else:
                out_data["location"] = {
                    "latitude": None,
                    "longitude": None,
                    "roadAddress": None,
                    "jibunAddress": None,
                    "geocodeStatus": "api_error"
                }

            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(out_data, f, ensure_ascii=False, indent=1)
            
            stats['generated_with_transactions'] += 1

        except Exception as e:
            print(f"Error processing {apt_key}: {e}")
            stats['failed'] += 1
            failed_keys.append(apt_key)

    con.close()
    
    # Just save cache every time if we want, or do a clean diff. To be safe, just save it if anything might have changed.
    # Actually, we can just save it. It's fast.
    save_geocode_cache(geocode_cache)
        
    print(f"Export completed for {args.region_group}.")
    for k, v in stats.items():
        print(f"{k}: {v}")
    
    write_summary(args.region_group, stats, failed_keys)
    
    if stats['target_count'] > 0 and stats['generated_with_transactions'] == 0:
        print("::warning::No new details generated with transactions.")
        sys.exit(1)

def write_summary(region, stats, failed_keys):
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"### 🏢 상세 JSON 생성 리포트 ({region})\n")
            f.write(f"- **대상 단지 수**: {stats['target_count']}건\n")
            f.write(f"- **거래 포함 신규 생성 (V2)**: {stats['generated_with_transactions']}건\n")
            f.write(f"- **유효 거래 없음 (기존 보존)**: {stats['preserved_existing']}건\n")
            f.write(f"- **유효 거래 없음 (스킵됨)**: {stats['skipped_no_transactions']}건\n")
            f.write(f"- **실패 단지**: {stats['failed']}건\n")
            
            total = stats['generated_with_transactions'] + stats['preserved_existing'] + stats['skipped_no_transactions'] + stats['failed']
            f.write(f"- **합계 검증**: {total}건 {'(✅ 일치)' if total == stats['target_count'] else '(❌ 불일치)'}\n\n")

            f.write(f"#### 📍 Geocoding (대상 단지 기준)\n")
            f.write(f"- **Valid**: {stats['geocode_valid']}건\n")
            f.write(f"- **Ambiguous**: {stats['geocode_ambiguous']}건\n")
            f.write(f"- **Not Found**: {stats['geocode_not_found']}건\n")
            f.write(f"- **API Error / Missing**: {stats['geocode_api_error']}건\n")
            
            if stats['target_count'] > 0 and stats['generated_with_transactions'] == 0:
                f.write("\n> [!WARNING]\n> **생성 성공(거래 있음) 건수가 0건입니다.**\n> 대상 파일 덮어쓰기를 중지하고 에러 처리했습니다.\n")

            if failed_keys:
                f.write(f"\n<details><summary>실패한 apartmentKey 목록</summary>\n\n```text\n{', '.join(failed_keys)}\n```\n</details>\n")
            f.write("\n")

if __name__ == "__main__":
    export_details()
