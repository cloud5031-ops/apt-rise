"""환경설정. API 키와 통계코드는 전부 환경변수로 분리한다 (설계안 3-1, 16장)."""
import os

# ── 인증키 (GitHub Actions에서는 repo Secrets로 주입) ──────────────
RONE_API_KEY = os.environ.get("RONE_API_KEY", "")
DATA_GO_KR_API_KEY = os.environ.get("DATA_GO_KR_API_KEY", "")
KOSIS_API_KEY = os.environ.get("KOSIS_API_KEY", "")  # 선택(대체 소스)

# ── R-ONE 통계코드 (개편 대비 환경변수로 오버라이드 가능) ──────────
RONE_STATBL_ID = os.environ.get("RONE_STATBL_ID", "A_2024_00045")
RONE_DTACYCLE_CD = os.environ.get("RONE_DTACYCLE_CD", "MM")
RONE_ENDPOINT = "https://www.reb.or.kr/r-one/openapi/SttsApiTblData.do"

# ── 국토부 실거래가 상세 ───────────────────────────────────────────
MOLIT_ENDPOINT = (
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/"
    "getRTMSDataSvcAptTradeDev"
)

# ── 행정안전부 법정동코드 ─────────────────────────────────────────
REGION_CODE_ENDPOINT = "http://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList"

# ── 경로 ─────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "apt.sqlite")
REGIONS_PATH = os.path.join(ROOT, "data", "regions.json")
SITE_DATA_DIR = os.path.join(ROOT, "site", "data")

# ── 통계 기준 (설계안 7, 8장) ─────────────────────────────────────
BASELINE_MONTHS = 3          # 기준가 = 직전 3개월 중위가격
MIN_CURRENT_TRADES = 2       # 이번 달 최소 거래 건수
MIN_BASELINE_TRADES = 2      # 기준 기간 최소 거래 건수
MIN_RISE_AMOUNT = 30_000_000 # 상승액 3천만 원 이상
MIN_RISE_RATE = 3.0          # 상승률 3% 이상
