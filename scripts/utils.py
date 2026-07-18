"""공통 계산 로직 (설계안 6, 7, 8장)."""
import re
from datetime import date


def median(values):
    """중위값. 빈 리스트면 None."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]


def change_rate(current, previous):
    """(현재 ÷ 기준 - 1) × 100. 계산 불가 시 None."""
    if not current or not previous:
        return None
    return round((current / previous - 1) * 100, 2)


def area_group(exclusive_area: float) -> int:
    """전용면적 반올림 그룹화. 84.82 → 85 (설계안 6-2)."""
    return round(exclusive_area)


def normalize_apt_name(name: str) -> str:
    """아파트명 정규화 — 공백·괄호 표기 차이를 흡수해 동일 단지를 묶는다."""
    if not name:
        return ""
    n = re.sub(r"\s+", "", name)
    n = re.sub(r"[()\[\]]", "", n)
    return n


def apartment_key(apt_seq, sgg_code, umd_name, jibun, apt_name) -> str:
    """단지 고유키. aptSeq가 없으면 보조키 조합 사용 (설계안 6-1)."""
    if apt_seq:
        return str(apt_seq)
    return f"{sgg_code}:{umd_name}:{jibun or ''}:{normalize_apt_name(apt_name)}"


def confidence(current_count: int, baseline_count: int) -> str:
    """거래량 기반 신뢰도 (설계안 8-2)."""
    if current_count >= 5 and baseline_count >= 5:
        return "high"
    if current_count >= 2 and baseline_count >= 2:
        return "medium"
    return "low"


# ── 월(YYYYMM) 산술 ──────────────────────────────────────────────

def shift_month(yyyymm: str, delta: int) -> str:
    """'202606'에서 delta개월 이동. shift_month('202606', -1) → '202605'."""
    y, m = int(yyyymm[:4]), int(yyyymm[4:6])
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}{total % 12 + 1:02d}"


def recent_months(n: int, base: str | None = None) -> list[str]:
    """이번 달 포함 최근 n개월 목록 (내림차순)."""
    if base is None:
        today = date.today()
        base = f"{today.year:04d}{today.month:02d}"
    return [shift_month(base, -i) for i in range(n)]


def parse_deal_amount(raw: str) -> int:
    """'125,000' (만원) → 1,250,000,000 (원)."""
    return int(raw.replace(",", "").strip()) * 10_000
