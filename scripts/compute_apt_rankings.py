"""개별 아파트 상승률 계산 → 순위 JSON 생성 (설계안 7, 8, 12장).

실행:
  python scripts/compute_apt_rankings.py            # 이번 달 기준
  python scripts/compute_apt_rankings.py 202606     # 기준월 지정

로직:
  이번 달 동일 단지·면적그룹 중위가격
  ÷ 직전 3개월 중위가격 → 상승률
  해제 거래·이상치 제외, 거래량 필터, 신뢰도 부여
출력: site/data/apt_rankings_{월}.json + apartment_monthly_metrics
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import config
import db
from utils import confidence, median, recent_months, shift_month


def load_groups(conn, months: list[str]) -> dict:
    """(단지키, 면적그룹) → {월: [가격...]} 구조로 적재. 해제·이상치 제외."""
    placeholders = ",".join("?" * len(months))
    rows = conn.execute(
        f"""SELECT apartment_key, area_group, deal_month, deal_amount,
                   apt_name, sgg_code, umd_name
            FROM apartment_trades
            WHERE deal_month IN ({placeholders})
              AND is_cancelled = 0 AND is_outlier = 0""",
        months,
    )
    groups = {}
    for r in rows:
        gkey = (r["apartment_key"], r["area_group"])
        g = groups.setdefault(gkey, {
            "apt_name": r["apt_name"], "sgg_code": r["sgg_code"],
            "umd_name": r["umd_name"], "prices": defaultdict(list),
        })
        g["prices"][r["deal_month"]].append(r["deal_amount"])
    return groups


def main():
    ref_month = sys.argv[1] if len(sys.argv) > 1 else recent_months(1)[0]
    baseline = [shift_month(ref_month, -i) for i in range(1, config.BASELINE_MONTHS + 1)]

    conn = db.connect()
    regions = {
        r["sgg_code"]: r["full_name"]
        for r in conn.execute("SELECT sgg_code, full_name FROM region_codes")
    }
    now = datetime.now(timezone.utc).isoformat()
    results = []

    for (apt_key, ag), g in load_groups(conn, [ref_month] + baseline).items():
        cur_prices = g["prices"].get(ref_month, [])
        base_prices = [p for m in baseline for p in g["prices"].get(m, [])]
        cur_median, base_median = median(cur_prices), median(base_prices)

        rise_amount = rise_rate = None
        if cur_median and base_median:
            rise_amount = round(cur_median - base_median)
            rise_rate = round((cur_median / base_median - 1) * 100, 2)

        # 계산 결과는 전 단지 metrics 테이블에 저장 (재활용 대비)
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
             rise_amount, rise_rate,
             confidence(len(cur_prices), len(base_prices)), now),
        )

        # 순위 JSON에는 필터 통과분만 (설계안 8-2)
        if (
            rise_rate is not None
            and len(cur_prices) >= config.MIN_CURRENT_TRADES
            and len(base_prices) >= config.MIN_BASELINE_TRADES
            and rise_amount >= config.MIN_RISE_AMOUNT
            and rise_rate >= config.MIN_RISE_RATE
        ):
            results.append({
                "region": f"{regions.get(g['sgg_code'], g['sgg_code'])} {g['umd_name'] or ''}".strip(),
                "apartmentName": g["apt_name"],
                "exclusiveAreaGroup": ag,
                "currentMedianPrice": cur_median,
                "baselineMedianPrice": base_median,
                "riseAmount": rise_amount,
                "riseRate": rise_rate,
                "currentTradeCount": len(cur_prices),
                "baselineTradeCount": len(base_prices),
                "confidence": confidence(len(cur_prices), len(base_prices)),
            })

    conn.commit()
    conn.close()

    results.sort(key=lambda x: x["riseRate"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i

    os.makedirs(config.SITE_DATA_DIR, exist_ok=True)
    out = {
        "referenceMonth": ref_month,
        "status": "provisional",  # 최신 월은 항상 잠정치 (설계안 9장)
        "collectedAt": now,
        "items": results[:300],
    }
    path = os.path.join(config.SITE_DATA_DIR, f"apt_rankings_{ref_month}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(config.SITE_DATA_DIR, "apt_rankings_latest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"단지 순위 {len(results)}건 → {path}")


if __name__ == "__main__":
    main()
