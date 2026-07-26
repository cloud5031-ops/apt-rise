"""상세 JSON 36개월 거래 이력 병합 모듈.

CI의 SQLite DB는 매 실행 재생성되는 휘발성 저장소이므로,
저장소에 커밋되는 site/data/details/*.json 자체를 누적 저장소로 사용한다.
이 모듈은 기존 상세 JSON의 transactions와 새로 수집된 거래를 병합하고,
36개월 창 밖의 과거 거래를 제거하며, 수집이력 장부(details_backfill_state.json)를
근거로 historyCoverage 메타데이터를 계산한다.

설계 원칙:
- 병합은 항상 "기존 ∪ 신규" — 삭제는 36개월 트림뿐이므로 데이터 유실이 없다.
- dedupe 키는 DB의 source_trade_key와 동일 조합(계약일|가격|층|면적)을 쓴다.
- 같은 키가 충돌하면 새 레코드가 이긴다 (해제 상태 갱신 반영).
- historyCoverage.complete는 거래 유무가 아니라 "해당 시군구의 36개월 수집이
  실제 수행되었는가"(장부 기준)로 판단한다. 거짓 '최근 3년' 표시를 막기 위함.
"""
import json
from pathlib import Path

from utils import shift_month

HISTORY_MONTHS = 36

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKFILL_STATE_PATH = PROJECT_ROOT / "data" / "details_backfill_state.json"


def history_window(end_month: str) -> tuple[str, str]:
    """끝 기준월(포함) 기준 최근 36개 달력월 창. ('202307', '202606') 형태."""
    start_month = shift_month(end_month, -(HISTORY_MONTHS - 1))
    return start_month, end_month


def window_months(end_month: str) -> list[str]:
    """창에 포함되는 YYYYMM 목록 (오름차순 36개)."""
    start, _ = history_window(end_month)
    return [shift_month(start, i) for i in range(HISTORY_MONTHS)]


def tx_dedupe_key(t: dict) -> str:
    """DB source_trade_key와 동일 조합: 계약일|가격|층|전용면적."""
    return "|".join(str(x) for x in (
        t.get("contractDate"), t.get("priceWon"), t.get("floor"), t.get("exclusiveArea"),
    ))


def tx_month(t: dict) -> str:
    """contractDate('YYYY-MM-DD') → 'YYYYMM'. 파싱 불가 시 빈 문자열."""
    d = str(t.get("contractDate") or "")
    if len(d) >= 7 and d[4] == "-":
        return d[:4] + d[5:7]
    return ""


def merge_transactions(existing_tx: list, new_tx: list, end_month: str) -> list:
    """기존 거래와 신규 거래를 병합한다.

    - dedupe 키 충돌 시 신규 레코드 우선 (해제 상태 등 최신 정보 반영)
    - 36개월 창 밖(과거) 거래 제거
    - 계약일 최신순 정렬
    """
    start_month, _ = history_window(end_month)
    merged = {}
    for t in (existing_tx or []):
        merged[tx_dedupe_key(t)] = t
    for t in (new_tx or []):
        merged[tx_dedupe_key(t)] = t  # 신규 우선

    result = [
        t for t in merged.values()
        if start_month <= tx_month(t) <= end_month
    ]
    result.sort(key=lambda t: (str(t.get("contractDate") or ""), tx_dedupe_key(t)), reverse=True)
    return result


# ── 수집이력 장부 ────────────────────────────────────────────────

def load_backfill_state() -> dict:
    """장부 로드. 구조: {"sggMonths": {sggCode: [YYYYMM, ...]}}"""
    if BACKFILL_STATE_PATH.exists():
        try:
            with open(BACKFILL_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("sggMonths"), dict):
                    return data
        except Exception:
            pass
    return {"sggMonths": {}}


def save_backfill_state(state: dict) -> None:
    BACKFILL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 월 목록 정렬·중복 제거 후 저장 (장부는 항상 정규형 유지)
    norm = {"sggMonths": {}}
    for sgg, months in (state.get("sggMonths") or {}).items():
        norm["sggMonths"][sgg] = sorted(set(months))
    with open(BACKFILL_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(norm, f, ensure_ascii=False, indent=1)


def record_collected_months(state: dict, sgg_code: str, months: list[str]) -> None:
    """sgg에 대해 수집 완료한 월을 장부에 기록한다."""
    bucket = state.setdefault("sggMonths", {}).setdefault(sgg_code, [])
    for m in months:
        if m not in bucket:
            bucket.append(m)


def collected_months_for(state: dict, sgg_code: str) -> set:
    return set((state.get("sggMonths") or {}).get(sgg_code, []))


def compute_history_coverage(sgg_code: str, end_month: str, state: dict) -> dict:
    """historyCoverage 메타데이터 계산.

    complete = 해당 sgg의 36개월 창 전체가 장부에 수집 완료로 기록됨.
    """
    start_month, _ = history_window(end_month)
    needed = set(window_months(end_month))
    have = collected_months_for(state, sgg_code)
    return {
        "startMonth": start_month,
        "endMonth": end_month,
        "months": HISTORY_MONTHS,
        "complete": needed.issubset(have),
    }
