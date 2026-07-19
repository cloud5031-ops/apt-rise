"""개별 아파트 상승률 계산 → 순위 JSON 생성 (설계안 7, 8, 12장).

실행:
  python scripts/compute_apt_rankings.py
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
import zoneinfo

import config
import db
from utils import confidence, median, shift_month, get_dynamic_months


def floor_bucket(floor: int) -> str:
    if floor is None:
        return "알수없음"
    if floor <= 5:
        return "저층"
    if floor <= 15:
        return "중층"
    return "고층"


def load_groups(conn, months: list[str], included_sido_codes: list[str] = None) -> dict:
    """(단지키, 전용면적 첫째자리) → 상세 데이터 구조로 적재."""
    placeholders = ",".join("?" * len(months))
    params = list(months)
    
    where_clause = f"deal_month IN ({placeholders}) AND is_cancelled = 0 AND is_outlier = 0"
    
    if included_sido_codes:
        sido_placeholders = ",".join("?" * len(included_sido_codes))
        # SUBSTR(sgg_code, 1, 2) IN (...)
        where_clause += f" AND SUBSTR(sgg_code, 1, 2) IN ({sido_placeholders})"
        params.extend(included_sido_codes)
        
    rows = conn.execute(
        f"""SELECT apartment_key, exclusive_area, deal_month, deal_amount,
                   floor, dealing_type, apt_name, sgg_code, umd_name
            FROM apartment_trades
            WHERE {where_clause}""",
        params,
    )
    groups = {}
    for r in rows:
        ag = round(r["exclusive_area"], 1)
        gkey = (r["apartment_key"], ag)
        g = groups.setdefault(gkey, {
            "apt_name": r["apt_name"], "sgg_code": r["sgg_code"],
            "umd_name": r["umd_name"], "trades": defaultdict(list),
            "exact_areas": set(),
        })
        g["exact_areas"].add(r["exclusive_area"])
        g["trades"][r["deal_month"]].append({
            "amount": r["deal_amount"],
            "floor": r["floor"] or 0,
            "type": (r["dealing_type"] or "").strip(),
        })
    return groups


def compute_for_month(conn, ref_month: str, regions: dict, now: str, status: str, included_sido_codes: list[str] = None):
    baseline = [shift_month(ref_month, -i) for i in range(1, config.BASELINE_MONTHS + 1)]
    results = []
    
    # ── 기초 통계 데이터 계산 ──
    # 해당 월의 전체 거래(취소 포함, 직거래 포함 등) 건수 측정
    raw_cur = conn.execute(
        "SELECT COUNT(*) as cnt, SUM(is_cancelled) as canc FROM apartment_trades WHERE deal_month = ?",
        (ref_month,)
    ).fetchone()
    
    raw_trade_count = raw_cur["cnt"] or 0
    cancelled_trade_count = raw_cur["canc"] or 0
    
    # 직거래는 is_cancelled=0 중에 dealing_type에 '직'이 포함된 것
    dir_cur = conn.execute(
        "SELECT COUNT(*) as cnt FROM apartment_trades WHERE deal_month = ? AND is_cancelled=0 AND dealing_type LIKE '%직%'",
        (ref_month,)
    ).fetchone()
    direct_trade_count = dir_cur["cnt"] or 0
    
    valid_trade_count = raw_trade_count - cancelled_trade_count
    
    groups = load_groups(conn, [ref_month] + baseline, included_sido_codes)
    candidate_group_count = 0

    for (apt_key, ag), g in groups.items():
        cur_trades = g["trades"].get(ref_month, [])
        if not cur_trades:
            continue
        candidate_group_count += 1
        base_trades = [t for m in baseline for t in g["trades"].get(m, [])]
        
        # 중개거래만 가격 계산에 포함
        cur_normal = [t for t in cur_trades if '직' not in t['type']]
        base_normal = [t for t in base_trades if '직' not in t['type']]
        
        cur_prices = [t['amount'] for t in cur_normal]
        base_prices = [t['amount'] for t in base_normal]
        
        cur_median, base_median = median(cur_prices), median(base_prices)

        rise_amount = rise_rate = None
        if cur_median and base_median:
            rise_amount = round(cur_median - base_median)
            rise_rate = round((cur_median / base_median - 1) * 100, 2)
            
        cur_floors = [t['floor'] for t in cur_trades if t['floor']]
        base_floors = [t['floor'] for t in base_trades if t['floor']]
        cur_med_floor = median(cur_floors)
        base_med_floor = median(base_floors)
        
        floor_mix_warning = False
        if cur_med_floor is not None and base_med_floor is not None:
            if abs(cur_med_floor - base_med_floor) >= 8:
                floor_mix_warning = True
            elif floor_bucket(cur_med_floor) != floor_bucket(base_med_floor):
                floor_mix_warning = True
                
        base_direct_count = sum(1 for t in base_trades if '직' in t['type'])
        cur_direct_count = sum(1 for t in cur_trades if '직' in t['type'])
        direct_trade_warning = (base_direct_count > 0 or cur_direct_count > 0)
        
        warning_reasons = []
        if floor_mix_warning:
            warning_reasons.append("층수 차이(8층 이상 또는 구간 차이)")
        if len(g["exact_areas"]) > 1:
            warning_reasons.append("서로 다른 세부 전용면적 혼합")
        if direct_trade_warning:
            warning_reasons.append("직거래 포함 이력 존재")
        if len(cur_prices) == 2 and len(base_prices) == 2:
            warning_reasons.append("비교/기준 기간 거래량 모두 최소 조건(2건)")
        if rise_rate is not None and abs(rise_rate) >= 20.0:
            warning_reasons.append("20% 이상 급변동")
            
        composition_warning = len(warning_reasons) > 0
        
        conf = confidence(len(cur_prices), len(base_prices))
        if composition_warning:
            if conf == "high": conf = "medium"
            elif conf == "medium": conf = "low"

        conn.execute(
            """INSERT INTO apartment_monthly_metrics
               (apartment_key, reference_month, area_group, apt_name, sgg_code, umd_name,
                current_median_price, baseline_median_price, current_trade_count,
                baseline_trade_count, rise_amount, rise_rate, confidence, calculated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(apartment_key, reference_month, area_group) DO UPDATE SET
                 current_median_price=excluded.current_median_price,
                 baseline_median_price=excluded.baseline_median_price,
                 current_trade_count=excluded.current_trade_count,
                 baseline_trade_count=excluded.baseline_trade_count,
                 rise_amount=excluded.rise_amount, rise_rate=excluded.rise_rate,
                 confidence=excluded.confidence, calculated_at=excluded.calculated_at""",
            (apt_key, ref_month, ag, g["apt_name"], g["sgg_code"], g["umd_name"],
             cur_median, base_median, len(cur_prices), len(base_prices),
             rise_amount, rise_rate, conf, now),
        )

        if (
            rise_rate is not None
            and len(cur_prices) >= config.MIN_CURRENT_TRADES
            and len(base_prices) >= config.MIN_BASELINE_TRADES
            and rise_amount >= config.MIN_RISE_AMOUNT
            and rise_rate >= config.MIN_RISE_RATE
        ):
            from shard_schema import normalize_code
            sgg_str = normalize_code(g["sgg_code"], 5)
            sido_str = sgg_str[:2]
            
            # apt_seq는 현재 g에 없음, 하지만 apt_key 생성 시 원본이 뭐였는지 알 수 있거나 그냥 apartmentKey 자체를 쓴다.
            # apt_key는 utils.py의 apartment_key()에서 생성된 문자열.
            # aptSeq 필드는 필수가 되었으므로, 만약 apt_key가 sgg_code-번호 형태면 번호를 추출하거나 apt_key 자체를 aptSeq로 사용할 수 있다.
            # 가장 좋은 방법은 g에 apt_seq를 저장하는 것. 일단 apt_key에서 파생하거나 apt_key를 쓴다.
            # wait, I'll extract it if it has dash, else it's the whole key.
            apt_seq_val = apt_key.split("-", 1)[1] if "-" in apt_key else apt_key
            
            item = {
                "apartmentKey": apt_key,
                "aptSeq": apt_seq_val,
                "apartmentName": g["apt_name"],
                "sidoCode": sido_str,
                "sggCode": sgg_str,
                "areaGroup": ag,
                "referenceMonth": ref_month,
                "baselineMedian": base_median,
                "currentMedian": cur_median,
                "riseAmount": rise_amount,
                "riseRate": rise_rate,
                "baselineTradeCount": len(base_prices),
                "currentTradeCount": len(cur_prices),
                "confidence": conf,
                "warnings": warning_reasons,
                
                # 기존 랭킹에 쓰이던 부가 정보 (선택적이지만 UI에서 쓸 수 있음)
                "region": f"{regions.get(g['sgg_code'], g['sgg_code'])} {g['umd_name'] or ''}".strip(),
                "exactAreas": list(g["exact_areas"]),
                "baselineMedianFloor": base_med_floor,
                "currentMedianFloor": cur_med_floor,
                "baselineDirectTradeCount": base_direct_count,
                "currentDirectTradeCount": cur_direct_count,
                "floorMixWarning": floor_mix_warning,
                "directTradeWarning": direct_trade_warning,
                "compositionWarning": composition_warning,
                "exclusiveAreaGroup": ag,
                "calculationVersion": "v1.1",
            }
            results.append(item)

    results.sort(key=lambda x: x["riseRate"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
        
    stats = {
        "rawTradeCount": raw_trade_count,
        "validTradeCount": valid_trade_count,
        "cancelledTradeCount": cancelled_trade_count,
        "directTradeCount": direct_trade_count,
        "candidateGroupCount": candidate_group_count,
        "filteredOutGroupCount": candidate_group_count - len(results)
    }
    return results, stats

def save_json_atomically(data: dict, filepath: str):
    temp_path = filepath + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(temp_path, filepath)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--region-group", help="권역 그룹 (shard 생성 모드)")
    parser.add_argument("--stable-month", help="고정 기준월 (안정 집계)")
    parser.add_argument("--provisional-month", help="고정 기준월 (잠정 집계)")
    args = parser.parse_args()

    from utils import get_dynamic_months, validate_fixed_months
    
    # 자동 모드 vs 고정 모드 검증
    if bool(args.stable_month) != bool(args.provisional_month):
        sys.exit("오류: --stable-month와 --provisional-month는 둘 다 지정하거나 둘 다 생략해야 합니다.")
        
    if args.stable_month and args.provisional_month:
        try:
            months_info = validate_fixed_months(args.stable_month, args.provisional_month)
        except ValueError as e:
            sys.exit(f"오류: 기준월 검증 실패 - {e}")
    else:
        months_info = get_dynamic_months()
        
    stable_month = months_info["stableMonth"]
    provisional_month = months_info["provisionalMonth"]
    
    conn = db.connect()
    regions = {
        r["sgg_code"]: r["full_name"]
        for r in conn.execute("SELECT sgg_code, full_name FROM region_codes")
    }
    now = datetime.now(zoneinfo.ZoneInfo("Asia/Seoul")).isoformat()
    
    run_meta = {}
    if args.region_group:
        meta_path = os.path.join(config.ROOT, "run_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                run_meta = json.load(f)
        else:
            sys.exit("오류: run_meta.json 파일이 없습니다. collect_trades.py가 성공적으로 실행되지 않았습니다.")
            
    # 1. 안정 집계 계산
    print(f"=== 안정 집계 ({stable_month}) 계산 시작 ===")
    included_sido_codes = run_meta.get("includedSidoCodes")
    stable_results, stable_stats = compute_for_month(conn, stable_month, regions, now, "stable", included_sido_codes)
    
    # 2. 잠정 집계 계산
    print(f"\n=== 잠정 집계 ({provisional_month}) 계산 시작 ===")
    provisional_results, prov_stats = compute_for_month(conn, provisional_month, regions, now, "provisional", included_sido_codes)
    conn.commit()
    conn.close()

    if args.region_group:
        os.makedirs(os.path.join(config.SHARDS_DIR, stable_month, "stable"), exist_ok=True)
        os.makedirs(os.path.join(config.SHARDS_DIR, provisional_month, "provisional"), exist_ok=True)
        
        def save_shard(month, status, items, stats):
            valid_status = "valid" if len(items) > 0 else "valid_no_matches"
            
            if len(run_meta.get("failedSggCodes", [])) > 0:
                sys.exit(f"오류: 실패한 지역 코드가 있습니다. Shard 저장을 건너뜁니다.")
                
            shard_data = {
                "regionGroup": args.region_group,
                "referenceMonth": month,
                "status": status,
                "validationStatus": valid_status,
                "generatedAt": now,
                "calculationVersion": "v1.1",
                "schemaVersion": "v1.0",
                "includedSidoCodes": run_meta.get("includedSidoCodes", []),
                "expectedSggCodes": run_meta.get("expectedSggCodes", []),
                "successfulSggCodes": run_meta.get("successfulSggCodes", []),
                "failedSggCodes": run_meta.get("failedSggCodes", []),
                "rawTradeCount": stats.get("rawTradeCount", 0),
                "validTradeCount": stats.get("validTradeCount", 0),
                "cancelledTradeCount": stats.get("cancelledTradeCount", 0),
                "directTradeCount": stats.get("directTradeCount", 0),
                "candidateGroupCount": stats.get("candidateGroupCount", 0),
                "filteredOutGroupCount": stats.get("filteredOutGroupCount", 0),
                "items": items
            }
            path = os.path.join(config.SHARDS_DIR, month, status, f"{args.region_group}.json")
            
            from shard_schema import validate_item
            included = run_meta.get("includedSidoCodes", [])
            for i, it in enumerate(items):
                validate_item(it, i, included, path)
                
            save_json_atomically(shard_data, path)
            print(f"Shard 생성 완료: {path} ({valid_status}, {len(items)}건)")
            
        save_shard(stable_month, "stable", stable_results, stable_stats)
        save_shard(provisional_month, "provisional", provisional_results, prov_stats)
        return

    # 기존 방식 (전국 단일 실행 시)
    if not stable_results:
        sys.exit(f"오류: 안정 집계({stable_month}) 단지 순위 결과가 0건입니다. 워크플로를 중단합니다.")
        
    os.makedirs(config.SITE_DATA_DIR, exist_ok=True)
    
    stable_out = {
        "referenceMonth": stable_month,
        "status": "stable",
        "collectedAt": now,
        "items": stable_results[:300],
    }
    stable_path = os.path.join(config.SITE_DATA_DIR, f"apt_rankings_{stable_month}.json")
    latest_path = os.path.join(config.SITE_DATA_DIR, "apt_rankings_latest.json")
    save_json_atomically(stable_out, stable_path)
    save_json_atomically(stable_out, latest_path)
    
    manifest = {
        "stableMonth": stable_month,
        "stableFile": f"apt_rankings_{stable_month}.json",
        "latestFile": "apt_rankings_latest.json",
        "generatedAt": now,
    }
    
    if provisional_results:
        provisional_out = {
            "referenceMonth": provisional_month,
            "status": "provisional",
            "collectedAt": now,
            "items": provisional_results[:300],
        }
        provisional_path = os.path.join(config.SITE_DATA_DIR, f"apt_rankings_{provisional_month}.json")
        save_json_atomically(provisional_out, provisional_path)
        manifest["provisionalMonth"] = provisional_month
        manifest["provisionalFile"] = f"apt_rankings_{provisional_month}.json"
    else:
        manifest["provisionalMonth"] = None
        manifest["provisionalFile"] = None
        
    manifest_path = os.path.join(config.SITE_DATA_DIR, "apt_rankings_manifest.json")
    save_json_atomically(manifest, manifest_path)
    print(f"완료: 안정 집계 {len(stable_results)}건, 잠정 집계 {len(provisional_results)}건")

if __name__ == "__main__":
    main()
