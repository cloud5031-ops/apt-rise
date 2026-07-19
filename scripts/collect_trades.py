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
    parser.add_argument("--stable-month", help="고정 기준월 (안정 집계)")
    parser.add_argument("--provisional-month", help="고정 기준월 (잠정 집계)")
    parser.add_argument("--region-group", help="권역 그룹 (seoul, gyeonggi_incheon 등)")
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
        
    months = months_info["targetMonths"]
    
    with open(config.REGIONS_PATH, encoding="utf-8") as f:
        all_regions = json.load(f)

    if args.sgg:
        sgg_list = args.sgg
        included_sido_codes = list(set([s[:2] for s in sgg_list]))
    elif args.region_group:
        if args.region_group not in config.REGION_GROUPS:
            sys.exit(f"오류: 알 수 없는 권역 그룹 '{args.region_group}'")
        prefixes = config.REGION_GROUPS[args.region_group]
        included_sido_codes = prefixes
        sgg_list = [r["sgg_code"] for r in all_regions if any(r["sgg_code"].startswith(p) for p in prefixes)]
    else:
        sgg_list = [r["sgg_code"] for r in all_regions]
        included_sido_codes = list(set([r["sgg_code"][:2] for r in all_regions]))

    conn = db.connect()
    total = 0
    failed_sgg = set()
    successful_sgg = set()
    
    for month in months:
        print(f"\n=== {month} 실거래가 수집 시작 ===")
        for i, sgg in enumerate(sgg_list, 1):
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
                print(f"[{i}/{len(sgg_list)}] [실패] 지역코드 {sgg} (3회 시도 초과)")
                failed_sgg.add(sgg)
                continue
            
            successful_sgg.add(sgg)
            upsert(conn, trades)
            total += len(trades)
            print(f"[{i}/{len(sgg_list)}] [성공] 지역코드 {sgg}: 응답 {len(trades)}건 DB 반영 (현재까지 실패: {list(failed_sgg)})")
            time.sleep(0.1)  # 과도한 호출 방지
        conn.commit()
    conn.close()
    
    import os
    os.makedirs(config.ROOT, exist_ok=True)
    meta_path = os.path.join(config.ROOT, "run_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "regionGroup": args.region_group or "all",
            "includedSidoCodes": included_sido_codes,
            "expectedSggCodes": sgg_list,
            "successfulSggCodes": list(successful_sgg),
            "failedSggCodes": list(failed_sgg)
        }, f, ensure_ascii=False, indent=2)
    
    print("\n=== 수집 최종 요약 ===")
    print(f"총 대상 시군구: {len(sgg_list)}개")
    print(f"성공: {len(successful_sgg)}개")
    print(f"실패: {len(failed_sgg)}개")
    print(f"총 DB 반영 실거래 건수: {total}건")
    if failed_sgg:
        print(f"\n[실패 지역 목록]")
        for f in failed_sgg:
            print(f"  - {f}")
        sys.exit(1)

if __name__ == "__main__":
    main()

