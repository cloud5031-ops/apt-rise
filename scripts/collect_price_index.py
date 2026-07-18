"""한국부동산원 R-ONE 아파트 매매가격지수 수집 (설계안 3장).

최신 공표월을 동적으로 탐색하고, 13개월간의 데이터를 수집하여 상승률을 계산합니다.
"""
import sys
from datetime import datetime, timezone

import requests

import config
import db
from utils import change_rate, recent_months, shift_month

def check_data_exists(month: str) -> bool:
    """해당 월에 데이터가 존재하는지 API 통신으로 확인합니다."""
    try:
        resp = requests.get(
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
            timeout=10,
        )
        if resp.status_code == 401 or resp.status_code == 403:
            sys.exit("오류: R-ONE API 인증 실패 (API 키를 확인하세요)")
        resp.raise_for_status()
        data = resp.json()
        
        for block in data.get("SttsApiTblData", []):
            if isinstance(block, dict) and "row" in block:
                return len(block["row"]) > 0
        return False
    except requests.RequestException as e:
        sys.exit(f"오류: R-ONE API 통신 실패 ({e})")
        
def find_latest_published_month() -> str:
    """현재 월부터 최대 6개월 전까지 탐색하여 데이터가 존재하는 최신 월을 반환합니다."""
    current_month = datetime.now().strftime("%Y%m")
    for i in range(6):
        test_month = shift_month(current_month, -i)
        print(f"{test_month} 공표 여부 탐색 중...")
        if check_data_exists(test_month):
            print(f"최신 공표월 발견: {test_month}")
            return test_month
    sys.exit("오류: 최근 6개월 내에 발표된 R-ONE 가격지수 데이터가 없습니다.")

def fetch_month(month: str) -> list[dict]:
    """해당 월 지수 전체 페이지 수집."""
    rows, page = [], 1
    while True:
        resp = requests.get(
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
            timeout=30,
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

def main():
    if not config.RONE_API_KEY:
        sys.exit("RONE_API_KEY 환경변수가 없습니다.")
        
    latest_month = find_latest_published_month()
    
    # 최신 월 포함 과거 13개월 생성 (오름차순)
    months = [shift_month(latest_month, -i) for i in range(12, -1, -1)]
    print(f"수집 대상 13개월: {months}")

    conn = db.connect()
    total_saved = 0
    for month in months:
        count = save_month(conn, month, fetch_month(month))
        if count == 0:
            sys.exit(f"오류: {month}의 데이터가 0건입니다. 연속된 13개월 수집에 실패했습니다.")
        total_saved += count
        
    for month in months:
        compute_rates(conn, month)
        
    conn.commit()
    conn.close()
    
    print(f"총 {total_saved}건의 가격지수가 성공적으로 수집/계산되었습니다.")

if __name__ == "__main__":
    main()
