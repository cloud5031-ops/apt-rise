"""최근 36개월 상세 거래 이력 백필 (수동 workflow 전용).

현재 안정·잠정 순위 shard 합집합에 등장하는 apartmentKey에 대해,
지정한 월 범위의 국토부 실거래를 시군구×월 단위로 수집하여
site/data/details/{sgg}/{key}.json 에 병합한다.

- 같은 시군구×월 API 응답은 실행 중 1회만 호출한다 (여러 단지가 공유).
- 수집 성공한 시군구×월은 장부(data/details_backfill_state.json)에 기록되어
  --resume 실행 시 건너뛴다. 실패한 월은 기록하지 않아 재시도 대상이 된다.
- 병합은 details_history.merge_transactions를 사용하므로 기존 이력이 보존된다.

실행 예:
  python scripts/backfill_details_history.py --region-group seoul --resume
  python scripts/backfill_details_history.py --region-group south \
      --start-month 202307 --end-month 202406
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import config
import db
from collect_trades import fetch_trades, upsert
from utils import shift_month, get_dynamic_months
from details_history import (
    HISTORY_MONTHS, history_window, window_months,
    merge_transactions, compute_history_coverage,
    load_backfill_state, save_backfill_state,
    record_collected_months, collected_months_for,
)

SHARDS_DIR = PROJECT_ROOT / "data" / "shards"
DETAILS_DIR = PROJECT_ROOT / "site" / "data" / "details"
MANIFEST_PATH = PROJECT_ROOT / "site" / "data" / "apt_rankings_manifest.json"

# 연속 실패가 이 횟수에 도달하면 수집을 중단한다 (일일 API 한도 소진 방어).
MAX_CONSECUTIVE_FAILURES = 15


def read_manifest_months():
    """manifest에서 anchor(잠정월)와 안정월을 읽는다. 실패 시 동적 계산."""
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            m = json.load(f)
        stable = m.get("stableMonth")
        provisional = m.get("provisionalMonth")
        if re.match(r"^\d{6}$", stable or "") and re.match(r"^\d{6}$", provisional or ""):
            return stable, provisional
    except Exception:
        pass
    info = get_dynamic_months()
    return info["stableMonth"], info["provisionalMonth"]


def load_target_apartments(region_group, stable_month, provisional_month):
    """export_details.py와 동일한 방식: stable+provisional shard 합집합."""
    keys = {}
    for month, kind in ((stable_month, "stable"), (provisional_month, "provisional")):
        path = SHARDS_DIR / month / kind / f"{region_group}.json"
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("items", []):
                k = item["apartmentKey"]
                if k not in keys:
                    keys[k] = {
                        "sidoCode": item["sidoCode"],
                        "sggCode": item["sggCode"],
                        "apartmentName": item.get("apartmentName", ""),
                    }
        except Exception as e:
            print(f"shard 읽기 실패 {path}: {e}")
    return keys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region-group", required=True)
    parser.add_argument("--start-month", help="수집 시작월 YYYYMM (기본: anchor-35)")
    parser.add_argument("--end-month", help="수집 끝월 YYYYMM (기본: anchor=잠정월)")
    parser.add_argument("--resume", action="store_true",
                        help="장부에 기록된 시군구×월은 건너뛴다")
    args = parser.parse_args()

    if not config.DATA_GO_KR_API_KEY:
        sys.exit("DATA_GO_KR_API_KEY 환경변수가 없습니다.")
    if args.region_group not in config.REGION_GROUPS:
        sys.exit(f"알 수 없는 권역 그룹: {args.region_group}")

    stable_month, anchor = read_manifest_months()
    full_start, full_end = history_window(anchor)

    start = args.start_month or full_start
    end = args.end_month or full_end
    for v in (start, end):
        if not re.match(r"^\d{6}$", v):
            sys.exit(f"월 형식 오류: {v} (YYYYMM)")
    if start > end:
        sys.exit(f"시작월({start})이 끝월({end})보다 큽니다.")
    if start < full_start:
        print(f"주의: 시작월 {start}은 36개월 창({full_start}~) 이전입니다. {full_start}로 조정합니다.")
        start = full_start
    if end > full_end:
        print(f"주의: 끝월 {end}은 anchor({full_end}) 이후입니다. {full_end}로 조정합니다.")
        end = full_end

    months = []
    m = start
    while m <= end:
        months.append(m)
        m = shift_month(m, 1)

    targets = load_target_apartments(args.region_group, stable_month, anchor)
    if not targets:
        print(f"대상 단지가 없습니다 (권역 {args.region_group}).")
        write_summary(args.region_group, {
            'target_count': 0, 'history_complete': 0, 'history_partial': 0,
            'api_failed_sgg_months': 0, 'zero_tx': 0, 'preserved': 0,
            'fetched_sgg_months': 0, 'skipped_sgg_months': 0, 'updated_files': 0,
        }, [])
        return

    sgg_codes = sorted(set(info["sggCode"] for info in targets.values()))
    state = load_backfill_state()

    print(f"권역 {args.region_group}: 단지 {len(targets)}개, 시군구 {len(sgg_codes)}개, "
          f"월 범위 {start}~{end} ({len(months)}개월), anchor {anchor}")

    # ── 1. 수집 (시군구×월 1회 호출, resume 시 장부 스킵) ─────────
    conn = db.connect()
    fetched = 0
    skipped = 0
    failed_pairs = []
    consecutive_failures = 0
    aborted_reason = None

    for sgg in sgg_codes:
        if aborted_reason:
            break
        have = collected_months_for(state, sgg) if args.resume else set()
        for month in months:
            if month in have:
                skipped += 1
                continue
            success = False
            for attempt in range(1, 4):
                try:
                    trades = fetch_trades(sgg, month)
                    success = True
                    break
                except Exception:
                    if attempt < 3:
                        time.sleep(1)
            if not success:
                print(f"[실패] {sgg}/{month} (3회 시도 초과)")
                failed_pairs.append(f"{sgg}/{month}")
                consecutive_failures += 1
                # 일일 API 한도를 넘기면 남은 수천 건이 전부 실패하면서
                # 재시도·타임아웃만으로 Actions 6시간 한도를 태운다.
                # 연속 실패가 이어지면 수집을 접고 여기까지 성공한 분량을
                # 병합·push하도록 정상 경로로 빠져나간다.
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    aborted_reason = (
                        f"연속 {consecutive_failures}회 API 실패로 수집을 중단했습니다. "
                        "일일 호출 한도 소진일 가능성이 높습니다. "
                        "여기까지 성공한 분량은 저장되며, --resume 으로 이어서 실행하세요."
                    )
                    print(f"::warning::{aborted_reason}")
                    break
                continue
            consecutive_failures = 0
            upsert(conn, trades)
            record_collected_months(state, sgg, [month])
            fetched += 1
            time.sleep(0.1)
        conn.commit()
        # 시군구 단위로 장부 저장 (중단 시에도 진행분 보존)
        save_backfill_state(state)

    # ── 2. 단지별 병합 저장 ──────────────────────────────────────
    import sqlite3
    months_placeholder = ",".join("?" for _ in months)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    stats = {
        'target_count': len(targets),
        'history_complete': 0,
        'history_partial': 0,
        'api_failed_sgg_months': len(failed_pairs),
        'zero_tx': 0,
        'preserved': 0,
        'fetched_sgg_months': fetched,
        'skipped_sgg_months': skipped,
        'updated_files': 0,
        'aborted_reason': aborted_reason,
    }

    for apt_key, info in targets.items():
        sgg = info["sggCode"]
        out_path = DETAILS_DIR / sgg / f"{apt_key}.json"

        c.execute(f"""
            SELECT umd_name, jibun, exclusive_area, area_group, deal_amount,
                   deal_date, floor, is_cancelled, dealing_type
            FROM apartment_trades
            WHERE apartment_key = ? AND deal_month IN ({months_placeholder})
            ORDER BY deal_date DESC
        """, [apt_key] + months)
        rows = c.fetchall()

        new_tx = []
        dong_name = ""
        for r in rows:
            if not dong_name:
                dong_name = r["umd_name"] or ""
            new_tx.append({
                "contractDate": r["deal_date"],
                "priceWon": r["deal_amount"],
                "floor": r["floor"],
                "exclusiveArea": r["exclusive_area"],
                "areaGroup": r["area_group"],
                "dealType": r["dealing_type"],
                "cancellationStatus": "CANCELLED" if r["is_cancelled"] else "COMPLETED",
            })

        existing_data = None
        if out_path.exists():
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except Exception:
                existing_data = None

        if not new_tx and existing_data is None:
            stats['zero_tx'] += 1
            continue

        existing_tx = (existing_data or {}).get("transactions") or []
        merged_tx = merge_transactions(existing_tx, new_tx, anchor)

        if not merged_tx:
            # 기존 파일이 있어도 빈 배열로 덮어쓰지 않는다
            stats['preserved'] += 1
            continue

        coverage = compute_history_coverage(sgg, anchor, state)
        if coverage["complete"]:
            stats['history_complete'] += 1
        else:
            stats['history_partial'] += 1

        if existing_data is not None:
            # 기존 필드(location, referenceMonths 등) 전부 보존, 이력 관련만 갱신
            out_data = dict(existing_data)
        else:
            out_data = {
                "schemaVersion": 2,
                "priceUnit": "KRW",
                "apartmentKey": apt_key,
                "apartmentName": info["apartmentName"],
                "sidoCode": info["sidoCode"],
                "sggCode": sgg,
                "dongName": dong_name,
                "referenceMonths": sorted({stable_month, anchor}),
                "location": {
                    "latitude": None, "longitude": None,
                    "roadAddress": None, "jibunAddress": None,
                    "geocodeStatus": "api_error",
                },
            }
        if dong_name and not out_data.get("dongName"):
            out_data["dongName"] = dong_name
        out_data["transactions"] = merged_tx
        out_data["availableAreas"] = sorted({
            t["exclusiveArea"] for t in merged_tx
            if t.get("exclusiveArea") is not None
        })
        out_data["historyCoverage"] = coverage

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out_data, f, ensure_ascii=False, indent=1)
        stats['updated_files'] += 1

    conn.close()
    save_backfill_state(state)

    print("\n=== 백필 요약 ===")
    for k, v in stats.items():
        print(f"{k}: {v}")
    if failed_pairs:
        print(f"실패한 시군구/월: {', '.join(failed_pairs[:30])}"
              + (" ..." if len(failed_pairs) > 30 else ""))

    write_summary(args.region_group, stats, failed_pairs)

    if aborted_reason:
        print(f"\n::warning::{aborted_reason}")

    # 수집 시도가 전부 실패했을 때만 실패 처리.
    # 서킷브레이커로 중단됐어도 일부라도 받아왔다면 정상 종료해
    # push 스텝이 진행분과 장부를 저장하도록 한다.
    if fetched == 0 and skipped == 0 and failed_pairs:
        sys.exit(1)


def write_summary(region, stats, failed_pairs):
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    with open(summary_file, "a", encoding="utf-8") as f:
        f.write(f"### 📚 36개월 상세 이력 백필 ({region})\n")
        f.write(f"- **대상 단지 수**: {stats['target_count']}건\n")
        f.write(f"- **36개월 완료 단지**: {stats['history_complete']}건\n")
        f.write(f"- **일부 기간 단지**: {stats['history_partial']}건\n")
        f.write(f"- **갱신된 상세 JSON**: {stats['updated_files']}건\n")
        f.write(f"- **기존 파일 보존(병합 결과 없음)**: {stats['preserved']}건\n")
        f.write(f"- **거래 0건 (파일 미생성)**: {stats['zero_tx']}건\n")
        f.write(f"- **API 호출 성공 (시군구×월)**: {stats['fetched_sgg_months']}건\n")
        f.write(f"- **장부 스킵 (이미 수집됨)**: {stats['skipped_sgg_months']}건\n")
        f.write(f"- **API 실패 (시군구×월)**: {stats['api_failed_sgg_months']}건\n")
        if stats.get('aborted_reason'):
            f.write("\n> [!WARNING]\n> **수집이 조기 중단되었습니다.**\n> "
                    + stats['aborted_reason'] + "\n")
        if failed_pairs:
            f.write("\n<details><summary>실패한 시군구/월 목록</summary>\n\n```text\n"
                    + ", ".join(failed_pairs) + "\n```\n</details>\n")
        f.write("\n")


if __name__ == "__main__":
    main()
