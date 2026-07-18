"""행정안전부 법정동코드 → 전국 시군구 목록 생성 (설계안 5장).

실행: python scripts/collect_region_codes.py
출력: data/regions.json + region_codes 테이블

법정동코드 10자리 구조:
  앞 2자리 시도 / 다음 3자리 시군구 / 나머지 5자리 읍면동리
시군구 레벨 = 시군구 자리가 000이 아니고, 뒤 5자리가 00000인 코드.
최초 1회 실행 후 분기 1회 정도만 갱신하면 된다.
"""
import json
import sys
from datetime import datetime, timezone

import requests

import config
import db


def fetch_all_rows() -> list[dict]:
    rows, page = [], 1
    while True:
        resp = requests.get(
            config.REGION_CODE_ENDPOINT,
            params={
                "ServiceKey": config.DATA_GO_KR_API_KEY,
                "pageNo": page,
                "numOfRows": 1000,
                "type": "json",
                "flag": "Y",  # 현재 유효한 코드만
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # 응답 구조: {"StanReginCd": [{"head": [...]}, {"row": [...]}]}
        blocks = data.get("StanReginCd", [])
        page_rows = []
        for block in blocks:
            if "row" in block:
                page_rows = block["row"]
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < 1000:
            break
        page += 1
    return rows


def to_sigungu_list(rows: list[dict]) -> list[dict]:
    seen, result = set(), []
    for r in rows:
        code = r.get("region_cd", "")
        name = (r.get("locatadd_nm") or "").strip()
        if len(code) != 10 or not name:
            continue
        if code[2:5] == "000":      # 시도 레벨 제외
            # 예외: 세종특별자치시는 시군구가 없어 시도 코드를 그대로 사용
            if not name.startswith("세종"):
                continue
        if code[5:] != "00000":     # 읍면동 레벨 제외
            continue
        sgg = code[:5]
        if sgg in seen:             # 앞 5자리 중복 제거
            continue
        seen.add(sgg)
        parts = name.split()
        result.append({
            "sgg_code": sgg,
            "sido_name": parts[0],
            "sigungu_name": " ".join(parts[1:]) or parts[0],
            "full_name": name,
        })
    return sorted(result, key=lambda x: x["sgg_code"])


def main():
    if not config.DATA_GO_KR_API_KEY:
        sys.exit("DATA_GO_KR_API_KEY 환경변수가 없습니다.")
    regions = to_sigungu_list(fetch_all_rows())
    now = datetime.now(timezone.utc).isoformat()

    with open(config.REGIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(regions, f, ensure_ascii=False, indent=2)

    conn = db.connect()
    conn.executemany(
        """INSERT INTO region_codes (sgg_code, sido_name, sigungu_name, full_name, updated_at)
           VALUES (:sgg_code, :sido_name, :sigungu_name, :full_name, :now)
           ON CONFLICT(sgg_code) DO UPDATE SET
             sido_name=excluded.sido_name, sigungu_name=excluded.sigungu_name,
             full_name=excluded.full_name, is_active=1, updated_at=excluded.updated_at""",
        [{**r, "now": now} for r in regions],
    )
    conn.commit()
    conn.close()
    print(f"시군구 {len(regions)}개 저장 → {config.REGIONS_PATH}")


if __name__ == "__main__":
    main()
