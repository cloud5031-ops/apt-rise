import json
import os
import sys
from datetime import datetime
import zoneinfo

import config
from utils import get_dynamic_months

def load_and_validate_shards(month, status):
    shard_dir = os.path.join(config.SHARDS_DIR, month, status)
    if not os.path.exists(shard_dir):
        return None
        
    expected_groups = set(config.REGION_GROUPS.keys())
    shards = []
    
    for filename in os.listdir(shard_dir):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(shard_dir, filename), "r", encoding="utf-8") as f:
            shards.append(json.load(f))
            
    found_groups = set(s.get("regionGroup") for s in shards)
    
    if found_groups != expected_groups:
        print(f"[{status}] 오류: 5개 권역 Shard가 모두 존재하지 않습니다. (발견된 권역: {found_groups})")
        return None
        
    # Validate each shard
    for s in shards:
        if s.get("validationStatus") not in ("valid", "valid_no_matches"):
            print(f"[{status}] 오류: Shard {s.get('regionGroup')} validationStatus 비정상.")
            return None
        if len(s.get("failedSggCodes", [])) > 0:
            print(f"[{status}] 오류: Shard {s.get('regionGroup')} failedSggCodes가 0이 아님.")
            return None
        if len(s.get("expectedSggCodes", [])) != len(s.get("successfulSggCodes", [])):
            print(f"[{status}] 오류: Shard {s.get('regionGroup')} expected/successful SggCodes 개수 불일치.")
            return None
            
    # Check consistency
    ref_months = set(s.get("referenceMonth") for s in shards)
    statuses = set(s.get("status") for s in shards)
    calc_versions = set(s.get("calculationVersion") for s in shards)
    schema_versions = set(s.get("schemaVersion") for s in shards)
    
    if len(ref_months) > 1 or len(statuses) > 1 or len(calc_versions) > 1 or len(schema_versions) > 1:
        print(f"[{status}] 오류: Shard 메타데이터 불일치.")
        return None
        
    return shards

def merge_and_sort(shards):
    merged_items = []
    seen = {}
    
    for s in shards:
        region_group = s.get("regionGroup", "unknown")
        for item in s.get("items", []):
            key = (item["apartmentKey"], item.get("area_group", item.get("exclusiveAreaGroup")), item.get("referenceMonth", ""))
            
            if key in seen:
                prev_item, prev_region = seen[key]
                print(f"오류: 권역 간 데이터 중복 발생!")
                print(f"- apartmentKey: {item['apartmentKey']}")
                print(f"- areaGroup: {key[1]}")
                print(f"- referenceMonth: {key[2]}")
                print(f"- 첫 번째 shard: regionGroup={prev_region}")
                print(f"- 두 번째 shard: regionGroup={region_group}")
                print(f"- 첫 번째 item: sidoCode={prev_item.get('sidoCode')}, sggCode={prev_item.get('sggCode')}, aptName={prev_item.get('apartmentName')}, full_name={prev_item.get('full_name', '')}")
                print(f"- 두 번째 item: sidoCode={item.get('sidoCode')}, sggCode={item.get('sggCode')}, aptName={item.get('apartmentName')}, full_name={item.get('full_name', '')}")
                sys.exit(1)
                
            seen[key] = (item, region_group)
            merged_items.append(item)
            
    # 정렬: 1. riseRate DESC, 2. riseAmount DESC, 3. currentTradeCount DESC, 4. apartmentKey ASC
    merged_items.sort(key=lambda x: (
        x.get("riseRate", 0),
        x.get("riseAmount", 0),
        x.get("currentTradeCount", 0),
        x.get("apartmentKey", "")
    ), reverse=True)
    
    # 4번 조건(apartmentKey ASC)을 위해 다시 정렬 (파이썬 sort는 안정적립이므로 역순 정렬 후 ASC 정렬 불필요, 
    # 하지만 튜플 안에서 혼합 정렬이 안되므로 음수화 트릭 사용하거나 커스텀 키 사용)
    # 튜플 정렬 시 문자열은 역순이 어려우므로 1,2,3을 음수화해서 ASC 정렬하는 것이 올바름.
    merged_items.sort(key=lambda x: (
        -x.get("riseRate", 0),
        -x.get("riseAmount", 0),
        -x.get("currentTradeCount", 0),
        x.get("apartmentKey", "")
    ))
    
    for i, item in enumerate(merged_items, 1):
        item["rank"] = i
        
    return merged_items

def save_json_atomically(data: dict, filepath: str):
    temp_path = filepath + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(temp_path, filepath)

def main():
    import argparse
    parser = argparse.ArgumentParser()
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
    now = datetime.now(zoneinfo.ZoneInfo("Asia/Seoul")).isoformat()
    
    # 1. 안정 집계 병합
    stable_shards = load_and_validate_shards(stable_month, "stable")
    if not stable_shards:
        sys.exit("오류: 안정 집계 Shard 병합 실패. 워크플로를 중단합니다.")
        
    stable_items = merge_and_sort(stable_shards)
    if len(stable_items) == 0:
        sys.exit("오류: 병합 후 안정 집계 항목이 0건입니다.")
        
    region_updated_at = {}
    for s in stable_shards:
        region_updated_at[s["regionGroup"]] = s["generatedAt"]
        
    # 2. 잠정 집계 병합
    provisional_shards = load_and_validate_shards(provisional_month, "provisional")
    provisional_items = []
    if provisional_shards:
        provisional_items = merge_and_sort(provisional_shards)
        # 잠정 집계에서도 regionUpdatedAt을 반영할 수 있으나 보통 안정/잠정은 같은 수집 루프에서 생성이므로 동일시간
    
    # 3. 배포
    os.makedirs(config.SITE_DATA_DIR, exist_ok=True)
    
    stable_out = {
        "referenceMonth": stable_month,
        "status": "stable",
        "collectedAt": now,
        "items": stable_items[:300],
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
        "regionUpdatedAt": region_updated_at,
    }
    
    if provisional_items:
        provisional_out = {
            "referenceMonth": provisional_month,
            "status": "provisional",
            "collectedAt": now,
            "items": provisional_items[:300],
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
    
    print(f"전국 병합 완료: 안정 집계 {len(stable_items)}건, 잠정 집계 {len(provisional_items)}건")

if __name__ == "__main__":
    main()
