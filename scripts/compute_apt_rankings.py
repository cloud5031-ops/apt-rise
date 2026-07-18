"""개별 아파트 상승률 계산 → 순위 JSON 생성 (설계안 7, 8, 12장).

실행:
  python scripts/compute_apt_rankings.py            # 이번 달 기준
  python scripts/compute_apt_rankings.py 202606     # 기준월 지정
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import config
import db
from utils import confidence, median, recent_months, shift_month


def floor_bucket(floor: int) -> str:
    if floor is None:
        return "알수없음"
    if floor <= 5:
        return "저층"
    if floor <= 15:
        return "중층"
    return "고층"


def load_groups(conn, months: list[str]) -> dict:
    """(단지키, 전용면적 첫째자리) → 상세 데이터 구조로 적재."""
    placeholders = ",".join("?" * len(months))
    rows = conn.execute(
        f"""SELECT apartment_key, exclusive_area, deal_month, deal_amount,
                   floor, dealing_type, apt_name, sgg_code, umd_name
            FROM apartment_trades
            WHERE deal_month IN ({placeholders})
              AND is_cancelled = 0 AND is_outlier = 0""",
        months,
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
        cur_trades = g["trades"].get(ref_month, [])
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
             rise_amount, rise_rate, conf, now),
        )

        # 순위 JSON에는 필터 통과분만
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
                "area_group": ag,
                "exact_areas": list(g["exact_areas"]),
                "baseline_median_floor": base_med_floor,
                "current_median_floor": cur_med_floor,
                "baseline_direct_trade_count": base_direct_count,
                "current_direct_trade_count": cur_direct_count,
                "floor_mix_warning": floor_mix_warning,
                "direct_trade_warning": direct_trade_warning,
                "composition_warning": composition_warning,
                "warning_reasons": warning_reasons,
                "exclusiveAreaGroup": ag,  # for compatibility
                "currentMedianPrice": cur_median,
                "baselineMedianPrice": base_median,
                "riseAmount": rise_amount,
                "riseRate": rise_rate,
                "currentTradeCount": len(cur_prices),
                "baselineTradeCount": len(base_prices),
                "confidence": conf,
                "calculation_version": "v1.1",
            })

    conn.commit()
    conn.close()

    results.sort(key=lambda x: x["riseRate"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i

    os.makedirs(config.SITE_DATA_DIR, exist_ok=True)
    out = {
        "referenceMonth": ref_month,
        "status": "provisional",
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
