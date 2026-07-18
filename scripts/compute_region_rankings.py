"""시군구 가격지수 상승률 순위 JSON 생성 (설계안 13장).

실행:
  python scripts/compute_region_rankings.py            # DB의 최신 공표월 자동 선택
  python scripts/compute_region_rankings.py 202606
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
            sys.exit("지수 데이터가 없습니다. collect_price_index.py를 먼저 실행하세요.")

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

    os.makedirs(config.SITE_DATA_DIR, exist_ok=True)
    out = {
        "referenceMonth": month,
        "source": "한국부동산원 전국주택가격동향조사",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    path = os.path.join(config.SITE_DATA_DIR, f"region_rankings_{month}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(config.SITE_DATA_DIR, "region_rankings_latest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"지역 순위 {len(items)}건 → {path}")


if __name__ == "__main__":
    main()
