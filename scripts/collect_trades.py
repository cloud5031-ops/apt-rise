"""국토교통부 아파트 매매 실거래가 상세 수집 (설계안 4장, 9장).

실행:
  python scripts/collect_trades.py                    # 전국, 최근 3개월
  python scripts/collect_trades.py --sgg 11680        # 특정 시군구
  python scripts/collect_trades.py --months 202604 202605 202606

주의: 개발계정 트래픽 하루 10,000건.
전국 약 250개 시군구 × 3개월 = 750콜 수준이라 매일 돌려도 여유 있다.
"""
import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

import config
import db
from utils import apartment_key, area_group, parse_deal_amount, recent_months


def text(item, tag, default=None):
    el = item.find(tag)
    if el is None or el.text is None:
        return default
    return el.text.strip() or default


def fetch_trades(sgg_code: str, month: str) -> list[dict]:
    """한 시군구 × 한 달 거래 전체 수집 (페이지 반복)."""
    trades, page = [], 1
    while True:
        resp = requests.get(
            config.MOLIT_ENDPOINT,
            params={
                "serviceKey": config.DATA_GO_KR_API_KEY,
                "LAWD_CD": sgg_code,
                "DEAL_YMD": month,
                "pageNo": page,
                "numOfRows": 1000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)

        result_code = root.findtext(".//resultCode", "")
        if result_code not in ("00", "000"):
            msg = root.findtext(".//resultMsg", "unknown")
            raise RuntimeError(f"API 오류 [{sgg_code}/{month}] {result_code}: {msg}")

        items = root.findall(".//item")
        for it in items:
            raw_amount = text(it, "dealAmount")
            area = text(it, "excluUseAr")
            if not raw_amount or not area:
                continue
            y, m, d = text(it, "dealYear"), text(it, "dealMonth"), text(it, "dealDay")
            deal_date = f"{y}-{int(m):02d}-{int(d):02d}"
            trades.append({
                "apt_seq": text(it, "aptSeq"),
                "sgg_code": text(it, "sggCd", sgg_code),
                "umd_name": text(it, "umdNm", ""),
                "jibun": text(it, "jibun"),
                "apt_name": text(it, "aptNm", ""),
                "apt_dong": text(it, "aptDong"),
                "exclusive_area": float(area),
                "deal_amount": parse_deal_amount(raw_amount),
                "deal_date": deal_date,
                "deal_month": month,
                "floor": int(text(it, "floor") or 0) or None,
                "build_year": int(text(it, "buildYear") or 0) or None,
                "is_cancelled": 1 if (text(it, "cdealType") or "").upper() in ("O", "Y") else 0,
                "cancel_date": text(it, "cdealDay"),
                "dealing_type": text(it, "dealingGbn"),
                "registration_date": text(it, "rgstDate"),
            })

        total = int(root.findtext(".//totalCount", "0") or 0)
        if page * 1000 >= total or not items:
            break
        page += 1
    return trades


def upsert(conn, trades: list[dict]):
    now = datetime.now(timezone.utc).isoformat()
    for t in trades:
        key = apartment_key(t["apt_seq"], t["sgg_code"], t["umd_name"], t["jibun"], t["apt_name"])
        # 동일 거래 식별용 결합키 (설계안 4-2: API가 거래 고유 ID를 주지 않음)
        source_key = "|".join(str(x) for x in (
            key, t["exclusive_area"], t["deal_date"], t["deal_amount"], t["floor"],
        ))
        conn.execute(
            """INSERT INTO apartment_trades
               (source_trade_key, apt_seq, apartment_key, sgg_code, umd_name, jibun,
                apt_name, apt_dong, exclusive_area, area_group, deal_amount, deal_date,
                deal_month, floor, build_year, is_cancelled, cancel_date, dealing_type,
                registration_date, collected_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_trade_key) DO UPDATE SET
                 is_cancelled=excluded.is_cancelled,
                 cancel_date=excluded.cancel_date,
                 registration_date=excluded.registration_date,
                 updated_at=excluded.updated_at""",
            (
                source_key, t["apt_seq"], key, t["sgg_code"], t["umd_name"], t["jibun"],
                t["apt_name"], t["apt_dong"], t["exclusive_area"],
                area_group(t["exclusive_area"]), t["deal_amount"], t["deal_date"],
                t["deal_month"], t["floor"], t["build_year"], t["is_cancelled"],
                t["cancel_date"], t["dealing_type"], t["registration_date"], now, now,
            ),
        )


def main():
    if not config.DATA_GO_KR_API_KEY:
        sys.exit("DATA_GO_KR_API_KEY 환경변수가 없습니다.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--sgg", nargs="*", help="시군구 코드 (생략 시 regions.json 전체)")
    parser.add_argument("--months", nargs="*", help="YYYYMM (생략 시 최근 3개월)")
    args = parser.parse_args()

    months = args.months or recent_months(3)  # 설계안 9장: 현재 월 + 직전 2개월
    if args.sgg:
        sgg_list = args.sgg
    else:
        with open(config.REGIONS_PATH, encoding="utf-8") as f:
            sgg_list = [r["sgg_code"] for r in json.load(f)]

    conn = db.connect()
    total = 0
    failed_list = []
    
    for month in months:
        print(f"\n=== {month} 실거래가 수집 시작 ===")
        for sgg in sgg_list:
            trades = []
            success = False
            for attempt in range(1, 4):
                try:
                    trades = fetch_trades(sgg, month)
                    success = True
                    break
                except Exception as e:
                    if attempt < 3:
                        time.sleep(1)
            
            if not success:
                print(f"  [실패] 지역코드 {sgg} (3회 시도 초과)")
                failed_list.append(f"{sgg} ({month})")
                continue
            
            upsert(conn, trades)
            total += len(trades)
            print(f"  [성공] 지역코드 {sgg}: 응답 {len(trades)}건 DB 반영")
            time.sleep(0.1)  # 과도한 호출 방지
        conn.commit()
    conn.close()
    
    print("\n=== 수집 최종 요약 ===")
    print(f"총 대상: {len(months) * len(sgg_list)}건")
    print(f"성공: {len(months) * len(sgg_list) - len(failed_list)}건")
    print(f"실패: {len(failed_list)}건")
    print(f"총 DB 반영 실거래 건수: {total}건")
    if failed_list:
        print(f"\n[실패 지역 목록]")
        for f in failed_list:
            print(f"  - {f}")


if __name__ == "__main__":
    main()

