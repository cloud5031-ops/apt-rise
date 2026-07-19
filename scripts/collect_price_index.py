"""한국부동산원 R-ONE 아파트 매매가격지수 수집 (설계안 3장).

최신 공표월을 동적으로 탐색하고, 13개월간의 데이터를 수집하여 상승률을 계산합니다.
연결 지연(ReadTimeout) 및 일시적 서버 오류를 방지하기 위해 엄격한 재시도 정책을 적용합니다.
"""
import sys
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
import db
from utils import change_rate, recent_months, shift_month

def get_session():
    """urllib3 Retry가 적용된 requests Session 객체를 생성합니다."""
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=2,
        allowed_methods={"GET"},
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def check_data_exists(session: requests.Session, month: str) -> bool:
    """해당 월에 데이터가 존재하는지 API 통신으로 확인합니다."""
    try:
        resp = session.get(
            config.RONE_ENDPOINT,
            params={
                "KEY": config.RONE_API_KEY,
                "Type": "json",
                "pIndex": 1,
                "pSize": 1,
                "STATBL_ID": config.RONE_STATBL_ID,
                "DTACYCLE_CD": config.RONE_DTACYCLE_CD,
                "WRTTIME_IDTFR_ID": month,
            },
            timeout=(10, 120),
        )
        if resp.status_code in (401, 403):
            sys.exit("오류: R-ONE API 인증 실패 (API 키를 확인하세요)")
        resp.raise_for_status()
        data = resp.json()
        
        for block in data.get("SttsApiTblData", []):
            if isinstance(block, dict) and "row" in block:
                return len(block["row"]) > 0
        return False
    except requests.exceptions.RequestException as e:
        sys.exit(f"오류: R-ONE 최신 공표월 탐색 중 통신 실패 ({type(e).__name__})")
        
def find_latest_published_month(session: requests.Session) -> str:
    """현재 월부터 최대 6개월 전까지 탐색하여 데이터가 존재하는 최신 월을 반환합니다."""
    current_month = datetime.now().strftime("%Y%m")
    for i in range(6):
        test_month = shift_month(current_month, -i)
        print(f"{test_month} 공표 여부 탐색 중...")
        if check_data_exists(session, test_month):
            print(f"최신 공표월 발견: {test_month}")
            return test_month
        time.sleep(1.5)
    sys.exit("오류: 최근 6개월 내에 발표된 R-ONE 가격지수 데이터가 없습니다.")

def fetch_month_with_retry(session: requests.Session, month: str) -> list[dict]:
    """해당 월 지수 전체 페이지 수집 (애플리케이션 수준 재시도 포함)."""
    delays = [5, 10, 20, 40, 80]
    for attempt in range(6):
        try:
            rows, page = [], 1
            while True:
                resp = session.get(
                    config.RONE_ENDPOINT,
                    params={
                        "KEY": config.RONE_API_KEY,
                        "Type": "json",
                        "pIndex": page,
                        "pSize": 1000,
                        "STATBL_ID": config.RONE_STATBL_ID,
                        "DTACYCLE_CD": config.RONE_DTACYCLE_CD,
                        "WRTTIME_IDTFR_ID": month,
                    },
                    timeout=(10, 120),
                )
                resp.raise_for_status()
                data = resp.json()
                page_rows = []
                for block in data.get("SttsApiTblData", []):
                    if isinstance(block, dict) and "row" in block:
                        page_rows = block["row"]
                if not page_rows:
                    break
                rows.extend(page_rows)
                if len(page_rows) < 1000:
                    break
                page += 1
            return rows
        except requests.exceptions.RequestException as e:
            if attempt < 5:
                wait_time = delays[attempt]
                print(f"경고: {month} 수집 중 예외 발생 ({type(e).__name__}). {wait_time}초 후 재시도합니다... (시도 횟수: {attempt + 1}/5)")
                time.sleep(wait_time)
            else:
                sys.exit(f"오류: {month} 수집이 5회 연속 실패했습니다 ({type(e).__name__}). 워크플로를 중단합니다.")

def save_month(conn, month: str, rows: list[dict]):
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for r in rows:
        code = str(r.get("CLS_ID") or "").strip()
        name = (r.get("CLS_FULLNM") or r.get("CLS_NM") or "").strip()
        val = r.get("DTA_VAL")
        if not code or val in (None, ""):
            continue
        try:
            index = float(val)
        except ValueError:
            continue
        conn.execute(
            """INSERT INTO region_price_indices
               (region_code, region_name, reference_month, price_index, collected_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(region_code, reference_month) DO UPDATE SET
                 price_index=excluded.price_index, region_name=excluded.region_name,
                 collected_at=excluded.collected_at""",
            (code, name, month, index, now),
        )
        n += 1
    print(f"{month}: 지수 {n}건 저장")
    return n

def compute_rates(conn, month: str):
    """전월·3개월·전년 동월 대비 상승률 갱신 (설계안 3-2)."""
    def index_map(m):
        return {
            row["region_code"]: row["price_index"]
            for row in conn.execute(
                "SELECT region_code, price_index FROM region_price_indices WHERE reference_month=?",
                (m,),
            )
        }

    cur = index_map(month)
    m1 = index_map(shift_month(month, -1))
    m3 = index_map(shift_month(month, -3))
    m12 = index_map(shift_month(month, -12))
    for code, idx in cur.items():
        conn.execute(
            """UPDATE region_price_indices
               SET mom_rate=?, three_month_rate=?, yoy_rate=?
               WHERE region_code=? AND reference_month=?""",
            (
                change_rate(idx, m1.get(code)),
                change_rate(idx, m3.get(code)),
                change_rate(idx, m12.get(code)),
                code,
                month,
            ),
        )

def set_github_output(name, value):
    import os
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"::set-output name={name}::{value}")

def main():
    if not config.RONE_API_KEY:
        sys.exit("RONE_API_KEY 환경변수가 없습니다.")
        
    session = get_session()
    latest_month = find_latest_published_month(session)
    
    # ── NO-OP 로직 판단 ──
    import os, json
    latest_json_path = os.path.join(config.SITE_DATA_DIR, "region_rankings_latest.json")
    date_json_path = os.path.join(config.SITE_DATA_DIR, f"region_rankings_{latest_month}.json")
    
    is_noop = False
    if os.path.exists(latest_json_path) and os.path.exists(date_json_path):
        try:
            with open(latest_json_path, "r", encoding="utf-8") as f:
                latest_data = json.load(f)
            with open(date_json_path, "r", encoding="utf-8") as f:
                date_data = json.load(f)
                
            # 필수 조건 검증
            if (
                latest_data.get("referenceMonth") == latest_month and
                date_data.get("referenceMonth") == latest_month and
                isinstance(latest_data.get("items"), list) and
                len(latest_data["items"]) > 0 and
                latest_data.get("schemaVersion") == "v1.0" and
                latest_data.get("calculationVersion") == "v1.1"
            ):
                # NaN, Infinity 점검 (json 파서는 기본으로 float 처리 가능하지만 엄격히 검사)
                has_invalid_num = False
                for it in latest_data["items"]:
                    for k in ["riseRate", "riseAmount", "priceIndex"]:
                        val = it.get(k)
                        if isinstance(val, float) and (val != val or val == float('inf') or val == float('-inf')):
                            has_invalid_num = True
                            break
                if not has_invalid_num:
                    is_noop = True
        except Exception as e:
            print(f"경고: 기존 JSON 파일 검증 중 오류 발생. 새로 수집합니다 ({e})")
            
    if is_noop:
        print(f"NO-OP: 최신 공표월({latest_month})의 데이터가 이미 정상적으로 존재합니다.")
        set_github_output("action", "no_op")
        set_github_output("reference_month", latest_month)
        sys.exit(0)
    
    # 최신 월 포함 과거 13개월 생성 (오름차순)
    months = [shift_month(latest_month, -i) for i in range(12, -1, -1)]
    print(f"수집 대상 13개월: {months}")

    conn = db.connect()
    total_saved = 0
    
    # 1. 13개월 데이터 순차 수집 및 저장
    for month in months:
        time.sleep(1.5)
        rows = fetch_month_with_retry(session, month)
        count = save_month(conn, month, rows)
        if count == 0:
            sys.exit(f"오류: {month}의 데이터가 0건입니다. 연속된 13개월 수집에 실패했습니다.")
        total_saved += count
        conn.commit()  # 각 월 성공 시 즉시 commit
        
    # 2. 13개월 모두 존재하는지 최종 검증
    placeholders = ",".join(["?"] * len(months))
    saved_months = [
        r["reference_month"] for r in conn.execute(
            f"SELECT DISTINCT reference_month FROM region_price_indices WHERE reference_month IN ({placeholders})",
            months
        )
    ]
    if len(saved_months) != 13:
        sys.exit(f"오류: 13개월 데이터 누락. 저장된 월: {sorted(saved_months)}")
        
    # 3. 모든 데이터가 보장된 상태에서 상승률 계산
    for month in months:
        compute_rates(conn, month)
        
    conn.commit()
    conn.close()
    
    print(f"총 {total_saved}건의 가격지수가 성공적으로 수집/계산되었습니다.")
    set_github_output("action", "updated")
    set_github_output("reference_month", latest_month)
    set_github_output("item_count", total_saved)

if __name__ == "__main__":
    main()
