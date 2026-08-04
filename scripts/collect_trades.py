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
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

import config
import db
from utils import apartment_key, area_group, parse_deal_amount, recent_months


# ── 비밀값 마스킹 ────────────────────────────────────────────────
# requests의 예외 메시지에는 요청 URL이 통째로 들어가고, 그 안에 serviceKey
# 쿼리값이 그대로 담긴다. 로그로 나가기 전에 애플리케이션에서 직접 지운다.
# GitHub Actions의 시크릿 마스킹에만 기대면 로컬 실행·테스트 출력이 무방비다.
_SERVICE_KEY_RE = re.compile(r"(serviceKey=)[^&\s'\"<>]*", re.IGNORECASE)
_SECRET_ENV_NAMES = (
    "DATA_GO_KR_API_KEY", "KAKAO_REST_API_KEY", "RONE_API_KEY", "KOSIS_API_KEY",
)
_MIN_SECRET_LEN = 8   # 짧은 값은 무관한 문자열까지 훼손할 수 있어 제외한다


def _secret_values():
    """현재 설정된 키의 원문과 URL 인코딩 변형들."""
    out = set()
    for name in _SECRET_ENV_NAMES:
        raw = os.environ.get(name) or getattr(config, name, "") or ""
        raw = str(raw).strip()
        if len(raw) < _MIN_SECRET_LEN:
            continue
        for variant in (raw,
                        urllib.parse.quote(raw, safe=""),
                        urllib.parse.quote_plus(raw),
                        urllib.parse.unquote(raw)):
            if len(variant) >= _MIN_SECRET_LEN:
                out.add(variant)
    # 긴 것부터 지워야 부분 치환으로 조각이 남지 않는다
    return sorted(out, key=len, reverse=True)


def mask_secrets(text) -> str:
    """serviceKey 쿼리값과 알려진 키 원문을 ***로 가린다."""
    s = str(text)
    s = _SERVICE_KEY_RE.sub(r"\1***", s)
    for value in _secret_values():
        s = s.replace(value, "***")
    return s


# ── 시간 예산 (소프트) ───────────────────────────────────────────
# 주의: 이 예산들은 "절대 상한"이 아니다.
#   requests의 timeout은 connect 수립과 "소켓 읽기 사이 간격"에만 걸리고,
#   응답 본문이 조금씩 계속 도착하면 read timeout이 매번 갱신되어
#   단일 HTTP 요청이 임의로 길어질 수 있다. Python 쪽 검사는 요청과 요청
#   사이(페이지 사이, 쌍 사이)에서만 이뤄지므로 진행 중인 요청을 끊지 못한다.
#   따라서 하드캡은 workflow의 step timeout이며, 여기 값들은 정상 상황에서
#   스스로 멈추기 위한 소프트 예산이다.
CONNECT_TIMEOUT = 10        # 연결 수립
READ_TIMEOUT = 30           # 소켓 읽기 간격
PAIR_BUDGET_SECONDS = 120   # 한 (시군구×월) 시도의 페이지 반복 소프트 예산
MAX_ATTEMPTS = 3            # 총 시도 횟수 (최초 1 + 재시도 2)
RETRY_BACKOFF = (1, 3)      # 시도 사이 대기 (지수 백오프)
CONSECUTIVE_FAILURE_LIMIT = 15

# ── 공공데이터포털 표준 resultCode 분류 ──────────────────────────
# 재시도해도 결과가 같은 설정·권한 오류. 개별 쌍을 반복하지 않고 즉시 종료한다.
FATAL_RESULT_CODES = {
    "10": "INVALID_REQUEST_PARAMETER",
    "11": "NO_MANDATORY_REQUEST_PARAMETERS",
    "12": "NO_OPENAPI_SERVICE",              # 서비스 없음/폐기
    "20": "SERVICE_ACCESS_DENIED",
    "21": "TEMPORARILY_DISABLE_THE_SERVICEKEY",   # 키 일시정지 — run 전체 중단
    "22": "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS",  # 일일 한도 초과
    "30": "SERVICE_KEY_IS_NOT_REGISTERED",
    "31": "DEADLINE_HAS_EXPIRED",
    "32": "UNREGISTERED_IP",
    "33": "UNSIGNED_CALL",
}

# 일시적 장애로 보고 제한적으로 재시도한다.
TRANSIENT_RESULT_CODES = {
    "01": "APPLICATION_ERROR",
    "02": "DB_ERROR",
    "04": "HTTP_ERROR",
    "05": "SERVICETIMEOUT_ERROR",
    "99": "UNKNOWN_ERROR",
}

# NODATA_ERROR. 공공데이터포털 표준 정의상 "데이터 없음"이므로 오류가 아니다.
# 재시도하지 않고 빈 목록으로 반환해 totalCount=0인 정상 응답과 같게 다룬다.
NODATA_RESULT_CODE = "03"

# 종료 코드: 로그를 못 봐도 원인을 구분할 수 있게 나눈다.
EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1    # 일부 시군구 실패 (기존 동작)
EXIT_FATAL_API = 2          # 한도 초과·인증 오류 — 재시도 무의미
EXIT_BUDGET_ABORTED = 3     # 시간 예산 소진 또는 연속 실패로 중단


class FatalApiError(Exception):
    """한도 초과·인증 오류 등 재시도가 무의미한 실패."""


class TransientApiError(Exception):
    """타임아웃·연결 오류·5xx·429 등 재시도 가치가 있는 실패."""


def log(msg: str):
    # 모든 출력이 이 함수를 지나므로 여기서 한 번 더 거른다.
    print(mask_secrets(msg), flush=True)


def text(item, tag, default=None):
    el = item.find(tag)
    if el is None or el.text is None:
        return default
    return el.text.strip() or default


def fetch_trades(sgg_code: str, month: str, budget_seconds: float = PAIR_BUDGET_SECONDS,
                 hard_deadline: float | None = None) -> list[dict]:
    """한 시군구 × 한 달 거래 전체 수집 (페이지 반복).

    budget_seconds는 이 쌍의 소프트 예산, hard_deadline은 실행 전체 예산의
    절대 시각(time.monotonic 기준)이다. 둘 중 먼저 오는 쪽을 마감으로 삼아
    한 쌍이 실행 예산을 통째로 잡아먹지 못하게 한다.
    """
    deadline = time.monotonic() + budget_seconds
    if hard_deadline is not None:
        deadline = min(deadline, hard_deadline)
    trades, page = [], 1
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransientApiError(
                f"pair_budget_exceeded: {budget_seconds:.0f}s/실행예산 초과 (page {page}까지 진행)"
            )

        try:
            resp = requests.get(
                config.MOLIT_ENDPOINT,
                params={
                    "serviceKey": config.DATA_GO_KR_API_KEY,
                    "LAWD_CD": sgg_code,
                    "DEAL_YMD": month,
                    "pageNo": page,
                    "numOfRows": 1000,
                },
                timeout=(CONNECT_TIMEOUT, min(READ_TIMEOUT, max(1.0, remaining))),
            )
        except requests.exceptions.RequestException as e:
            # 예외 객체 자체에 키가 남으면 테스트 실패 출력이나 traceback으로
            # 새어 나간다. 로그 단계가 아니라 여기서 지운다.
            raise TransientApiError(mask_secrets(f"{type(e).__name__}: {e}"))

        if resp.status_code in (401, 403):
            raise FatalApiError(f"HTTP {resp.status_code} (인증/권한)")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientApiError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise TransientApiError(f"HTTP {resp.status_code}")

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            raise TransientApiError(
                mask_secrets(f"XMLParseError: {e} | body[:200]={resp.text[:200]!r}"))

        # 선행 0이 살아 있어야 분류가 맞는다("03" != "3"). 문자열 그대로 비교한다.
        result_code = (root.findtext(".//resultCode", "") or "").strip()
        msg = root.findtext(".//resultMsg", "unknown")

        # 인증·한도 오류는 서비스가 아니라 게이트웨이가 막는 것이라
        # 표준 봉투 대신 OpenAPI_ServiceResponse로 온다. HTTP는 200이고
        # 코드 체계는 같지만 태그 이름이 returnReasonCode/returnAuthMsg다.
        # 이 fallback이 없으면 한도 초과(22)가 "미분류 → Transient"로 재시도된다.
        if not result_code:
            result_code = (root.findtext(".//returnReasonCode", "") or "").strip()
            if result_code:
                msg = (root.findtext(".//returnAuthMsg")
                       or root.findtext(".//errMsg")
                       or "unknown")

        if result_code in FATAL_RESULT_CODES:
            raise FatalApiError(
                f"resultCode {result_code} ({FATAL_RESULT_CODES[result_code]}): {msg}"
            )
        if result_code == NODATA_RESULT_CODE:
            # 데이터 없음은 오류가 아니다. 재시도하지 않고 지금까지 모은 것을 반환한다.
            return trades
        if result_code in TRANSIENT_RESULT_CODES:
            raise TransientApiError(
                f"resultCode {result_code} ({TRANSIENT_RESULT_CODES[result_code]}): {msg}"
            )
        if result_code not in ("00", "000"):
            raise TransientApiError(f"resultCode {result_code} (미분류): {msg}")

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


def record_progress(conn, sgg_code: str, month: str, region_group, trade_count: int):
    """수집 완료 표시. 거래 upsert와 같은 트랜잭션에서 기록해 원자적으로 커밋한다."""
    conn.execute(
        """INSERT INTO collection_progress
           (sgg_code, deal_month, region_group, trade_count, collected_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(sgg_code, deal_month) DO UPDATE SET
             region_group=excluded.region_group,
             trade_count=excluded.trade_count,
             collected_at=excluded.collected_at""",
        (sgg_code, month, region_group, trade_count, datetime.now(timezone.utc).isoformat()),
    )


def load_progress(conn) -> set:
    """이미 수집 완료된 (시군구, 월) 집합. DB가 새로 만들어졌으면 빈 집합."""
    try:
        return {(r[0], r[1]) for r in
                conn.execute("SELECT sgg_code, deal_month FROM collection_progress")}
    except Exception:
        return set()


def write_run_meta(region_group, included_sido_codes, sgg_list,
                   successful, failed, not_attempted, aborted_reason):
    """중단되더라도 후속 단계가 상태를 알 수 있도록 수시로 기록한다.

    아직 시도하지 못한 시군구는 failedSggCodes에 함께 넣는다.
    compute_apt_rankings.save_shard()가 failedSggCodes가 비어 있지 않으면
    shard 저장을 거부하므로, 부분 수집 결과가 산출물로 새어 나가지 않는다.
    """
    import os
    os.makedirs(config.ROOT, exist_ok=True)
    meta_path = os.path.join(config.ROOT, "run_meta.json")
    blocked = sorted(set(failed) | set(not_attempted))
    payload = {
        "regionGroup": region_group or "all",
        "includedSidoCodes": included_sido_codes,
        "expectedSggCodes": sgg_list,
        "successfulSggCodes": sorted(successful),
        "failedSggCodes": blocked,
        "attemptFailedSggCodes": sorted(set(failed)),
        "notAttemptedSggCodes": sorted(set(not_attempted)),
        "abortedReason": aborted_reason,
        "completedAt": datetime.now(timezone.utc).isoformat(),
    }
    tmp = meta_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, meta_path)   # 원자적 교체 — 반쯤 쓰인 파일이 남지 않는다


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
    parser.add_argument("--max-runtime-seconds", type=int, default=2700,
                        help="수집 전체 시간 예산(초). 초과하면 통제된 방식으로 중단한다.")
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
    already_done = load_progress(conn)
    if already_done:
        log(f"체크포인트 발견: 이미 수집된 (시군구×월) {len(already_done)}건은 건너뜁니다.")

    total = 0
    failed_sgg = set()
    successful_sgg = set()
    # 시군구가 아니라 (시군구, 월) 쌍 단위로 완료를 센다. 월 중간에 끊기면
    # 같은 시군구라도 일부 월만 모인 상태이므로 "완료"로 볼 수 없다.
    completed_pairs = set()
    consecutive_failures = 0
    aborted_reason = None
    fatal_error = None

    pairs_total = len(sgg_list) * len(months)
    pairs_done = 0
    run_started = time.monotonic()
    budget = args.max_runtime_seconds

    def elapsed():
        return time.monotonic() - run_started

    def fully_collected_sggs():
        """대상 월을 하나도 빠뜨리지 않고 모은 시군구만 '성공'으로 인정한다."""
        return {s for s in sgg_list
                if all((s, m) in completed_pairs for m in months)}

    def snapshot_meta():
        done = fully_collected_sggs()
        # 완전히 모이지 않은 시군구는 전부 차단 목록으로 넘긴다.
        # compute_apt_rankings.save_shard()가 이 목록이 비어 있지 않으면
        # shard 저장을 거부하므로 부분 수집 결과가 발행되지 않는다.
        write_run_meta(
            args.region_group, included_sido_codes, sgg_list,
            done, failed_sgg,
            set(sgg_list) - done - set(failed_sgg),
            aborted_reason,
        )

    log(f"수집 시작: 시군구 {len(sgg_list)}개 × {len(months)}개월 = {pairs_total}쌍 "
        f"| 실행 예산 {budget}초 | 월 {months[0]}~{months[-1]}")

    for month in months:
        if aborted_reason or fatal_error:
            break
        log(f"\n=== {month} 실거래가 수집 시작 ===")
        for i, sgg in enumerate(sgg_list, 1):
            if (sgg, month) in already_done:
                pairs_done += 1
                completed_pairs.add((sgg, month))
                successful_sgg.add(sgg)
                continue

            # 실행 전체 예산을 먼저 확인한다. GitHub의 강제 종료를 기다리지 않고
            # 우리가 통제하는 시점에 멈춰야 run_meta와 DB를 남길 수 있다.
            if elapsed() > budget:
                aborted_reason = (
                    f"실행 예산 {budget}초를 초과해 수집을 중단했습니다 "
                    f"({pairs_done}/{pairs_total}쌍 완료, 경과 {elapsed():.0f}초)."
                )
                log(f"::warning::{aborted_reason}")
                break

            trades = []
            success = False
            for attempt in range(1, MAX_ATTEMPTS + 1):
                t0 = time.monotonic()
                try:
                    trades = fetch_trades(sgg, month,
                                          hard_deadline=run_started + budget)
                    success = True
                    break
                except FatalApiError as e:
                    # 한도 초과·인증 오류는 재시도해도 결과가 같다. 즉시 멈춘다.
                    fatal_error = f"[{sgg}/{month}] {e}"
                    log(f"[{i}/{len(sgg_list)}] [치명] {sgg}/{month} "
                        f"({time.monotonic()-t0:.1f}s) {type(e).__name__}: {e}")
                    break
                except TransientApiError as e:
                    log(f"[{i}/{len(sgg_list)}] [재시도 {attempt}/{MAX_ATTEMPTS}] {sgg}/{month} "
                        f"({time.monotonic()-t0:.1f}s) {type(e).__name__}: {e}")
                    if attempt < MAX_ATTEMPTS:
                        time.sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])
                except Exception as e:
                    log(f"[{i}/{len(sgg_list)}] [재시도 {attempt}/{MAX_ATTEMPTS}] {sgg}/{month} "
                        f"({time.monotonic()-t0:.1f}s) 예상치 못한 {type(e).__name__}: {e}")
                    if attempt < MAX_ATTEMPTS:
                        time.sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])

            if fatal_error:
                break

            pairs_done += 1

            if not success:
                log(f"[{i}/{len(sgg_list)}] [실패] 지역코드 {sgg}/{month} "
                    f"({MAX_ATTEMPTS}회 시도 초과, 경과 {elapsed():.0f}s)")
                failed_sgg.add(sgg)
                consecutive_failures += 1
                snapshot_meta()
                if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                    aborted_reason = (
                        f"연속 {consecutive_failures}회 실패로 수집을 중단했습니다 "
                        f"({pairs_done}/{pairs_total}쌍 진행, 경과 {elapsed():.0f}초)."
                    )
                    log(f"::warning::{aborted_reason}")
                    break
                continue

            consecutive_failures = 0
            completed_pairs.add((sgg, month))
            successful_sgg.add(sgg)
            upsert(conn, trades)
            # 거래와 체크포인트를 한 트랜잭션으로 커밋한다. 여기서 프로세스가
            # 죽어도 커밋된 시군구까지는 DB에 남고, 체크포인트도 정확히 일치한다.
            record_progress(conn, sgg, month, args.region_group, len(trades))
            conn.commit()
            total += len(trades)
            # 거래 0건(resultCode 03 또는 totalCount 0)은 실패가 아니라 정상 완료다.
            # 다만 눈으로 구분되게 태그를 달리한다.
            tag = "[EMPTY]" if not trades else "[성공]"
            detail = "거래 없음" if not trades else f"{len(trades)}건 DB 반영"
            log(f"[{i}/{len(sgg_list)}] {tag} {sgg}/{month}: {detail} "
                f"({pairs_done}/{pairs_total}쌍, 경과 {elapsed():.0f}s, 실패 {len(failed_sgg)}개)")
            snapshot_meta()
            time.sleep(0.1)  # 과도한 호출 방지

    conn.commit()
    conn.close()

    if fatal_error:
        aborted_reason = f"재시도 불가 API 오류로 중단: {fatal_error}"

    snapshot_meta()

    done_sggs = fully_collected_sggs()
    incomplete = set(sgg_list) - done_sggs - set(failed_sgg)

    log("\n=== 수집 최종 요약 ===")
    log(f"총 대상 시군구: {len(sgg_list)}개 ({pairs_total}쌍)")
    log(f"완료한 쌍: {len(completed_pairs)}/{pairs_total}")
    log(f"전체 월 수집 완료 시군구: {len(done_sggs)}개")
    log(f"실패: {len(failed_sgg)}개")
    log(f"일부 월 미수집: {len(incomplete)}개")
    log(f"총 DB 반영 실거래 건수: {total}건")
    log(f"총 소요: {elapsed():.0f}초")
    if failed_sgg:
        log("\n[실패 지역 목록]")
        for f in sorted(failed_sgg):
            log(f"  - {f}")

    if fatal_error:
        log(f"\n::error::한도 초과 또는 인증 오류입니다. {fatal_error}")
        sys.exit(EXIT_FATAL_API)
    if aborted_reason:
        log(f"\n::warning::{aborted_reason}")
        sys.exit(EXIT_BUDGET_ABORTED)
    if failed_sgg or incomplete:
        sys.exit(EXIT_PARTIAL_FAILURE)

if __name__ == "__main__":
    main()

