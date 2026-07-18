"""시군구 가격지수 상승률 순위 JSON 생성 (설계안 13장).

최신 공표월을 동적으로 확인하고, 데이터가 0건일 시 실패 처리합니다.
임시 파일을 사용해 원자적으로 JSON을 생성합니다.
"""
import json
import os
import sys
from datetime import datetime, timezone

import config
import db


def main():
    conn = db.connect()
    if len(sys.argv) > 1:
        month = sys.argv[1]
    else:
        row = conn.execute(
            "SELECT MAX(reference_month) AS m FROM region_price_indices"
        ).fetchone()
        month = row["m"]
        if not month:
            sys.exit("오류: 지수 데이터가 없습니다. collect_price_index.py를 먼저 실행하세요.")

    rows = conn.execute(
        """SELECT region_code, region_name, price_index,
                  mom_rate, three_month_rate, yoy_rate
           FROM region_price_indices
           WHERE reference_month = ? AND mom_rate IS NOT NULL
             AND (region_name LIKE '%구' OR region_name LIKE '%시' OR region_name LIKE '%군')
           ORDER BY mom_rate DESC""",
        (month,),
    ).fetchall()
    conn.close()

    items = [
        {
            "rank": i,
            "regionCode": r["region_code"],
            "regionName": r["region_name"],
            "priceIndex": r["price_index"],
            "momRate": r["mom_rate"],
            "threeMonthRate": r["three_month_rate"],
            "yoyRate": r["yoy_rate"],
        }
        for i, r in enumerate(rows, 1)
    ]
    
    if len(items) == 0:
        sys.exit(f"오류: {month} 기준 시군구 순위 생성 실패. (조건을 만족하는 13개월 원자료 부족)")
    
    # 합리적인 범위 검증 (일반적으로 대한민국 시군구는 약 250여개, 아파트 지수는 약 190~200여개)
    if len(items) < 150:
        sys.exit(f"오류: {month} 기준 시군구 순위 생성 실패. 결과 건수가 너무 적습니다 ({len(items)}건).")

    os.makedirs(config.SITE_DATA_DIR, exist_ok=True)
    out = {
        "referenceMonth": month,
        "source": "한국부동산원 전국주택가격동향조사",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    
    path = os.path.join(config.SITE_DATA_DIR, f"region_rankings_{month}.json")
    latest_path = os.path.join(config.SITE_DATA_DIR, "region_rankings_latest.json")
    
    temp_path = path + ".tmp"
    temp_latest_path = latest_path + ".tmp"
    
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(temp_latest_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        
    os.replace(temp_path, path)
    os.replace(temp_latest_path, latest_path)
    
    print(f"지역 순위 {len(items)}건 정상 생성 완료 → {path}, {latest_path}")


if __name__ == "__main__":
    main()
