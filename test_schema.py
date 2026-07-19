import os
import subprocess
import sqlite3
import json

regions = {
    "seoul": [("11110", "11110-1", "A")],
    "gyeonggi_incheon": [("41110", "41110-2", "B"), ("28110", "28110-3", "C")],
    "metro": [("26110", "26110-4", "D")],
    "central": [("36110", "36110-5", "E")],
    "south": [("47110", "47110-6", "F")],
}

print("=== 5개 권역 회귀 테스트 시작 ===")

for region, items in regions.items():
    if os.path.exists("data/apt.sqlite"):
        os.remove("data/apt.sqlite")
    if os.path.exists("run_meta.json"):
        os.remove("run_meta.json")
        
    subprocess.run(["python", "scripts/db.py"], check=True)
    
    conn = sqlite3.connect("data/apt.sqlite")
    for (sgg, key, name) in items:
        conn.execute("INSERT OR IGNORE INTO region_codes (sgg_code, full_name, sido_name, sigungu_name, updated_at) VALUES (?, ?, 'sido', 'sgg', '2026-07-19')", (sgg, name))
        conn.execute(
            """INSERT INTO apartment_trades 
               (source_trade_key, apt_seq, apartment_key, sgg_code, umd_name, jibun, apt_name, 
                apt_dong, exclusive_area, area_group, deal_amount, deal_date, deal_month, 
                floor, build_year, is_cancelled, cancel_date, dealing_type, registration_date, 
                is_outlier, collected_at, updated_at)
               VALUES (?, ?, ?, ?, 'umd', 'jibun', ?, 
                'dong', 84.0, 84, 100000, '2026-06-01', '202606', 
                5, 2000, 0, NULL, '중개', NULL, 
                0, '2026-07-19', '2026-07-19')
            """, (f"src1_{key}", "seq1", key, sgg, name)
        )
        conn.execute(
            """INSERT INTO apartment_trades 
               (source_trade_key, apt_seq, apartment_key, sgg_code, umd_name, jibun, apt_name, 
                apt_dong, exclusive_area, area_group, deal_amount, deal_date, deal_month, 
                floor, build_year, is_cancelled, cancel_date, dealing_type, registration_date, 
                is_outlier, collected_at, updated_at)
               VALUES (?, ?, ?, ?, 'umd', 'jibun', ?, 
                'dong', 84.0, 84, 90000, '2026-05-01', '202605', 
                5, 2000, 0, NULL, '중개', NULL, 
                0, '2026-07-19', '2026-07-19')
            """, (f"src2_{key}", "seq1", key, sgg, name)
        )
    conn.commit()
    conn.close()
    
    # 런 메타 생성
    sido_codes = list(set([s[:2] for (s, _, _) in items]))
    with open("run_meta.json", "w", encoding="utf-8") as f:
        json.dump({"regionGroup": region, "includedSidoCodes": sido_codes}, f)
        
    # 강제로 stable=202606, provisional=202607 로 동작하게 만들 수도 있지만 인자 넘겨줌
    print(f"[{region}] compute_apt_rankings 실행...")
    subprocess.run(["python", "scripts/compute_apt_rankings.py", "--region-group", region, "--stable-month", "202605", "--provisional-month", "202606"], check=True)

print("=== validate_shard.py 실행 ===")
res = subprocess.run(["python", "scripts/validate_shard.py"])
if res.returncode != 0:
    print("Shard 검증 실패!")
    exit(1)

print("=== merge_shards.py 실행 ===")
res = subprocess.run(["python", "scripts/merge_shards.py", "--stable-month", "202605", "--provisional-month", "202606"])
if res.returncode != 0:
    print("Merge 검증 실패!")
    exit(1)

print("테스트 완료! 병합 키 중복 0건, 스키마 검증 통과.")
