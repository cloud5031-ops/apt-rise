# -*- coding: utf-8 -*-
"""수집 파이프라인 회귀 테스트.

외부 네트워크나 추가 의존성 없이 표준 unittest만 사용한다.
requests.get을 가짜 응답으로 바꿔 국토교통부 API를 흉내내고,
"밖에서 관찰 가능한 보장"만 검증한다 — 종료 코드, run_meta.json의 내용,
DB에 남는 행, 실제 API 호출 횟수.

실행:
    python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

STABLE, PROVISIONAL = "202606", "202607"
MONTH_COUNT = 5          # stable-3 ~ provisional

TRADE_ITEM = """<item><aptSeq>1</aptSeq><sggCd>{sgg}</sggCd><umdNm>동</umdNm>
<jibun>1</jibun><aptNm>단지</aptNm><excluUseAr>84.77</excluUseAr>
<dealAmount>80,000</dealAmount><dealYear>{y}</dealYear><dealMonth>{m}</dealMonth>
<dealDay>10</dealDay><floor>5</floor><buildYear>1990</buildYear>
<dealingGbn>중개거래</dealingGbn></item>"""


OK_TEMPLATE = ("<response><header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header>"
               "<body><items>" + TRADE_ITEM +
               "</items><totalCount>1</totalCount></body></response>")


def ok_body(sgg, month):
    y, m = month[:4], int(month[4:])
    return OK_TEMPLATE.format(sgg=sgg, y=y, m=m)


def code_body(code, msg="MSG"):
    return (f"<response><header><resultCode>{code}</resultCode>"
            f"<resultMsg>{msg}</resultMsg></header>"
            "<body><items/><totalCount>0</totalCount></body></response>")


def gateway_body(code, msg="SERVICE ERROR"):
    """게이트웨이가 막았을 때 오는 봉투. resultCode가 아예 없다."""
    return ("<OpenAPI_ServiceResponse><cmmMsgHeader>"
            "<errMsg>SERVICE ERROR</errMsg>"
            f"<returnAuthMsg>{msg}</returnAuthMsg>"
            f"<returnReasonCode>{code}</returnReasonCode>"
            "</cmmMsgHeader></OpenAPI_ServiceResponse>")


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        pass


class CollectTradesTestCase(unittest.TestCase):
    """공통 준비: 임시 작업 디렉터리 + 가짜 지역 목록."""

    SGG_COUNT = 4

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aptrise_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.sgg_list = [f"111{i:02d}" for i in range(self.SGG_COUNT)]
        regions = [{"sgg_code": s, "full_name": f"서울 {s}"} for s in self.sgg_list]
        (self.tmp / "regions.json").write_text(
            json.dumps(regions, ensure_ascii=False), encoding="utf-8")

        import config
        self._config = config
        self._saved = {k: getattr(config, k, None) for k in
                       ("DB_PATH", "ROOT", "REGIONS_PATH", "DATA_GO_KR_API_KEY", "REGION_GROUPS")}
        config.DB_PATH = str(self.tmp / "apt.sqlite")
        config.ROOT = str(self.tmp)
        config.REGIONS_PATH = str(self.tmp / "regions.json")
        config.DATA_GO_KR_API_KEY = "TEST_KEY"
        config.REGION_GROUPS = {"seoul": ["11"]}
        self.addCleanup(self._restore_config)

        import collect_trades
        self.ct = collect_trades
        self._saved_get = collect_trades.requests.get
        self._saved_backoff = collect_trades.RETRY_BACKOFF
        collect_trades.RETRY_BACKOFF = (0, 0)      # 테스트를 빠르게
        self.addCleanup(self._restore_module)

        self.calls = []

    def _restore_config(self):
        for k, v in self._saved.items():
            setattr(self._config, k, v)

    def _restore_module(self):
        self.ct.requests.get = self._saved_get
        self.ct.RETRY_BACKOFF = self._saved_backoff

    def set_responder(self, fn):
        def wrapped(url, params=None, timeout=None, **kw):
            self.calls.append((params["LAWD_CD"], params["DEAL_YMD"]))
            return fn(params["LAWD_CD"], params["DEAL_YMD"])
        self.ct.requests.get = wrapped

    def run_collect(self, extra_args=()):
        argv = ["collect_trades.py", "--region-group", "seoul",
                "--stable-month", STABLE, "--provisional-month", PROVISIONAL,
                *extra_args]
        saved_argv = sys.argv
        sys.argv = argv
        try:
            self.ct.main()
            return 0
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 1
        finally:
            sys.argv = saved_argv

    def run_meta(self):
        return json.loads((self.tmp / "run_meta.json").read_text(encoding="utf-8"))

    def db_counts(self, db_path=None):
        con = sqlite3.connect(db_path or (self.tmp / "apt.sqlite"))
        try:
            progress = con.execute("SELECT COUNT(*) FROM collection_progress").fetchone()[0]
            trades = con.execute("SELECT COUNT(*) FROM apartment_trades").fetchone()[0]
            pairs = con.execute(
                "SELECT sgg_code, deal_month FROM collection_progress").fetchall()
            return progress, trades, set(pairs)
        finally:
            con.close()


class TestNoDataResultCode(CollectTradesTestCase):
    """resultCode 03(NODATA_ERROR)은 오류가 아니라 '거래 없음'이다."""

    def test_03_is_treated_as_empty_success(self):
        self.set_responder(lambda sgg, month: FakeResponse(code_body("03", "NODATA_ERROR")))
        exit_code = self.run_collect()

        self.assertEqual(exit_code, 0, "03만 돌아와도 run은 성공해야 한다")

        expected_pairs = self.SGG_COUNT * MONTH_COUNT
        self.assertEqual(len(self.calls), expected_pairs,
                         "03은 재시도 없이 쌍당 1회만 호출해야 한다")

        progress, trades, _ = self.db_counts()
        self.assertEqual(progress, expected_pairs, "빈 결과도 체크포인트로 기록해야 한다")
        self.assertEqual(trades, 0)

        meta = self.run_meta()
        self.assertEqual(meta["failedSggCodes"], [], "03은 실패가 아니다")
        self.assertEqual(sorted(meta["successfulSggCodes"]), sorted(self.sgg_list))

    def test_03_leading_zero_is_significant(self):
        """'03'과 '3'은 다른 코드다 — 선행 0이 사라지면 분류가 깨진다."""
        self.set_responder(lambda sgg, month: FakeResponse(code_body("03")))
        self.assertEqual(self.ct.fetch_trades("11100", STABLE, budget_seconds=5), [])

        self.set_responder(lambda sgg, month: FakeResponse(code_body("3")))
        with self.assertRaises(self.ct.TransientApiError):
            self.ct.fetch_trades("11100", STABLE, budget_seconds=5)

    def test_zero_total_count_matches_03_behaviour(self):
        """totalCount=0인 정상 응답과 03의 결과가 같아야 한다."""
        self.set_responder(lambda sgg, month: FakeResponse(code_body("00", "OK")))
        exit_code = self.run_collect()

        self.assertEqual(exit_code, 0)
        progress, trades, _ = self.db_counts()
        self.assertEqual(progress, self.SGG_COUNT * MONTH_COUNT)
        self.assertEqual(trades, 0)
        self.assertEqual(self.run_meta()["failedSggCodes"], [])


class TestFatalErrors(CollectTradesTestCase):
    """한도 초과·키 오류는 재시도 없이 run 전체를 즉시 멈춘다."""

    def test_quota_exceeded_aborts_immediately(self):
        self.set_responder(lambda sgg, month: FakeResponse(
            code_body("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR")))
        exit_code = self.run_collect()

        self.assertEqual(exit_code, 2, "한도 초과 전용 종료 코드")
        self.assertEqual(len(self.calls), 1, "재시도하지 않고 첫 실패에서 멈춰야 한다")

        meta = self.run_meta()
        self.assertTrue(meta["abortedReason"])
        self.assertEqual(sorted(meta["failedSggCodes"]), sorted(self.sgg_list),
                         "미수집 시군구는 전부 차단 목록에 들어가야 한다")

    def test_unregistered_key_aborts_immediately(self):
        self.set_responder(lambda sgg, month: FakeResponse(code_body("30")))
        self.assertEqual(self.run_collect(), 2)
        self.assertEqual(len(self.calls), 1)

    def test_gateway_quota_envelope_aborts_immediately(self):
        """한도 초과는 HTTP 200 + returnReasonCode 봉투로도 온다."""
        self.set_responder(lambda sgg, month: FakeResponse(
            gateway_body("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR")))
        self.assertEqual(self.run_collect(), 2, "재시도 대상이 아니라 즉시 중단이어야 한다")
        self.assertEqual(len(self.calls), 1)

    def test_gateway_unregistered_key_envelope_aborts_immediately(self):
        self.set_responder(lambda sgg, month: FakeResponse(
            gateway_body("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR")))
        self.assertEqual(self.run_collect(), 2)
        self.assertEqual(len(self.calls), 1)

    def test_http_403_is_fatal_regardless_of_body(self):
        """본문을 파싱하지 않고도 401/403은 즉시 중단이어야 한다."""
        self.set_responder(lambda sgg, month: FakeResponse("not xml at all", status_code=403))
        self.assertEqual(self.run_collect(), 2)
        self.assertEqual(len(self.calls), 1)


class TestGatewayEnvelope(CollectTradesTestCase):
    """게이트웨이 봉투(returnReasonCode)도 표준 봉투와 같은 정책을 따른다."""

    def test_gateway_nodata_matches_result_code_03(self):
        """returnReasonCode 03도 '거래 없음'으로, 실패가 아니다."""
        self.set_responder(lambda sgg, month: FakeResponse(gateway_body("03", "NODATA_ERROR")))
        exit_code = self.run_collect()

        expected_pairs = self.SGG_COUNT * MONTH_COUNT
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(self.calls), expected_pairs, "재시도 없이 쌍당 1회")
        progress, trades, _ = self.db_counts()
        self.assertEqual(progress, expected_pairs)
        self.assertEqual(trades, 0)
        self.assertEqual(self.run_meta()["failedSggCodes"], [])

    def test_gateway_transient_code_is_retried(self):
        self.set_responder(lambda sgg, month: FakeResponse(gateway_body("01", "APPLICATION_ERROR")))
        self.run_collect()
        self.assertGreater(len(self.calls), self.SGG_COUNT,
                           "일시 오류는 재시도되어야 한다")

    def test_gateway_leading_zero_is_preserved(self):
        """'03'과 '3'은 게이트웨이 봉투에서도 서로 다른 코드다."""
        self.set_responder(lambda sgg, month: FakeResponse(gateway_body("03")))
        self.assertEqual(self.ct.fetch_trades("11100", STABLE, budget_seconds=5), [])

        self.set_responder(lambda sgg, month: FakeResponse(gateway_body("3")))
        with self.assertRaises(self.ct.TransientApiError):
            self.ct.fetch_trades("11100", STABLE, budget_seconds=5)

    def test_standard_envelope_still_wins_when_both_present(self):
        """resultCode가 있으면 그것을 쓴다 (기존 형식 우선)."""
        body = ("<response><header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header>"
                "<cmmMsgHeader><returnReasonCode>22</returnReasonCode></cmmMsgHeader>"
                "<body><items/><totalCount>0</totalCount></body></response>")
        self.set_responder(lambda sgg, month: FakeResponse(body))
        self.assertEqual(self.ct.fetch_trades("11100", STABLE, budget_seconds=5), [])


class TestTransientErrors(CollectTradesTestCase):
    """일시적 장애는 제한적으로만 재시도한다."""

    SGG_COUNT = 1

    def _assert_retries_then_fails(self, responder):
        self.set_responder(responder)
        exit_code = self.run_collect()
        self.assertIn(exit_code, (1, 3))
        # 시군구 1개 × 5개월, 각 쌍마다 MAX_ATTEMPTS 회. 무한 재시도가 아니어야 한다.
        self.assertEqual(len(self.calls), self.ct.MAX_ATTEMPTS * MONTH_COUNT,
                         f"쌍당 정확히 {self.ct.MAX_ATTEMPTS}회만 시도해야 한다")
        self.assertEqual(self.run_meta()["successfulSggCodes"], [])

    def test_timeout_is_retried(self):
        import requests

        def responder(sgg, month):
            raise requests.exceptions.ReadTimeout("timed out")
        self._assert_retries_then_fails(responder)

    def test_connection_error_is_retried(self):
        import requests

        def responder(sgg, month):
            raise requests.exceptions.ConnectionError("connection refused")
        self._assert_retries_then_fails(responder)

    def test_server_error_is_retried(self):
        self._assert_retries_then_fails(
            lambda sgg, month: FakeResponse("<html>502</html>", status_code=502))

    def test_rate_limited_is_retried(self):
        self._assert_retries_then_fails(
            lambda sgg, month: FakeResponse("<html>429</html>", status_code=429))


class TestCircuitBreaker(CollectTradesTestCase):
    """연속 실패가 쌓이면 남은 작업을 붙들지 않고 통제된 방식으로 멈춘다."""

    SGG_COUNT = 20

    def test_consecutive_failures_stop_the_run(self):
        self.set_responder(lambda sgg, month: FakeResponse(code_body("01")))
        exit_code = self.run_collect()

        self.assertEqual(exit_code, 3, "예산·서킷브레이커 전용 종료 코드")

        limit = self.ct.CONSECUTIVE_FAILURE_LIMIT
        self.assertEqual(len(self.calls), limit * self.ct.MAX_ATTEMPTS,
                         "한계에 닿는 즉시 멈춰야 한다")

        total_pairs = self.SGG_COUNT * MONTH_COUNT
        self.assertLess(len(self.calls), total_pairs * self.ct.MAX_ATTEMPTS,
                        "전체를 끝까지 시도하면 안 된다")
        self.assertIn("연속", self.run_meta()["abortedReason"])

    def test_isolated_failures_do_not_stop_the_run(self):
        """드문 실패로는 중단되지 않고 끝까지 진행한다."""
        state = {"n": 0}

        def responder(sgg, month):
            state["n"] += 1
            if state["n"] % 9 == 0:
                return FakeResponse(code_body("02"))
            return FakeResponse(ok_body(sgg, month))

        self.set_responder(responder)
        self.run_collect()

        self.assertIsNone(self.run_meta()["abortedReason"],
                          "서킷브레이커가 발동해서는 안 된다")
        total_pairs = self.SGG_COUNT * MONTH_COUNT
        self.assertGreater(len(self.calls), total_pairs,
                           "재시도를 포함해 모든 쌍을 끝까지 시도해야 한다")


class TestCheckpointDurability(CollectTradesTestCase):
    """거래와 체크포인트는 한 트랜잭션에서 커밋되어 서로 어긋나지 않는다."""

    SGG_COUNT = 4

    def _write_child(self, db_path, root, crash_after=None, log_calls=None):
        crash = ""
        if crash_after is not None:
            crash = (f'\n    if state["n"] > {crash_after}:\n'
                     f'        os._exit(9)\n')
        record = ""
        if log_calls is not None:
            record = (f'\n    open({log_calls!r}, "a", encoding="utf-8")'
                      f'.write(params["LAWD_CD"] + params["DEAL_YMD"] + "\\n")\n')
        src = f'''# -*- coding: utf-8 -*-
import os, sys, json
sys.path.insert(0, {str(SCRIPTS)!r})
import config
config.DB_PATH = {str(db_path)!r}
config.ROOT = {str(root)!r}
config.REGIONS_PATH = {str(self.tmp / "regions.json")!r}
config.DATA_GO_KR_API_KEY = "TEST_KEY"
config.REGION_GROUPS = {{"seoul": ["11"]}}
import collect_trades as ct

BODY = {OK_TEMPLATE!r}

class R:
    def __init__(self, t): self.text, self.status_code = t, 200
    def raise_for_status(self): pass

state = {{"n": 0}}
def fake_get(url, params=None, timeout=None, **kw):
    state["n"] += 1{crash}{record}
    body = BODY.replace("{{sgg}}", params["LAWD_CD"])
    body = body.replace("{{y}}", params["DEAL_YMD"][:4])
    body = body.replace("{{m}}", str(int(params["DEAL_YMD"][4:])))
    return R(body)

ct.requests.get = fake_get
sys.argv = ["collect_trades.py", "--region-group", "seoul",
            "--stable-month", {STABLE!r}, "--provisional-month", {PROVISIONAL!r}]
try:
    ct.main()
except SystemExit as e:
    sys.exit(e.code or 0)
'''
        path = self.tmp / f"child_{crash_after}_{bool(log_calls)}.py"
        path.write_text(src, encoding="utf-8")
        return path

    def test_killed_process_leaves_consistent_committed_state(self):
        child = self._write_child(self.tmp / "apt.sqlite", self.tmp, crash_after=3)
        proc = subprocess.run([sys.executable, str(child)],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 9, "프로세스가 강제 종료되어야 하는 시나리오")

        progress, trades, pairs = self.db_counts()
        self.assertEqual(progress, 3, "커밋된 쌍만 남아야 한다")
        self.assertEqual(trades, progress,
                         "거래와 체크포인트가 같은 트랜잭션이므로 개수가 일치해야 한다")

    def test_restored_database_skips_completed_pairs(self):
        child = self._write_child(self.tmp / "apt.sqlite", self.tmp, crash_after=3)
        subprocess.run([sys.executable, str(child)], capture_output=True,
                       text=True, timeout=180)
        _, _, done_pairs = self.db_counts()
        self.assertTrue(done_pairs)

        # artifact 다운로드를 흉내내 별도 디렉터리로 복원
        restore = self.tmp / "restored"
        restore.mkdir()
        for f in self.tmp.iterdir():
            if f.name.startswith("apt.sqlite"):
                shutil.copy2(f, restore / f.name)

        calls_log = restore / "calls.txt"
        child2 = self._write_child(restore / "apt.sqlite", restore,
                                   log_calls=str(calls_log))
        subprocess.run([sys.executable, str(child2)], capture_output=True,
                       text=True, timeout=180)

        called = set()
        if calls_log.exists():
            called = {line[:5] + line[5:] for line in
                      calls_log.read_text(encoding="utf-8").split()}
        already = {f"{sgg}{month}" for sgg, month in done_pairs}
        self.assertEqual(called & already, set(),
                         "이미 완료된 쌍은 다시 호출하면 안 된다")

        progress, _, _ = self.db_counts(restore / "apt.sqlite")
        self.assertEqual(progress, self.SGG_COUNT * MONTH_COUNT,
                         "이어받아 나머지를 마저 수집해야 한다")


class TestSuccessfulRunContract(CollectTradesTestCase):
    """정상 수집이 후속 단계가 기대하는 형식을 그대로 유지하는지."""

    def test_run_meta_and_database_shape(self):
        self.set_responder(lambda sgg, month: FakeResponse(ok_body(sgg, month)))
        self.assertEqual(self.run_collect(), 0)

        meta = self.run_meta()
        for key in ("regionGroup", "includedSidoCodes", "expectedSggCodes",
                    "successfulSggCodes", "failedSggCodes"):
            self.assertIn(key, meta, "기존 소비자가 읽는 필드가 사라지면 안 된다")
        self.assertEqual(meta["regionGroup"], "seoul")
        self.assertEqual(meta["includedSidoCodes"], ["11"])
        self.assertEqual(sorted(meta["expectedSggCodes"]), sorted(self.sgg_list))
        self.assertEqual(meta["failedSggCodes"], [])

        progress, trades, _ = self.db_counts()
        expected_pairs = self.SGG_COUNT * MONTH_COUNT
        self.assertEqual(progress, expected_pairs)
        self.assertEqual(trades, expected_pairs)

        con = sqlite3.connect(self.tmp / "apt.sqlite")
        try:
            row = con.execute(
                "SELECT apartment_key, sgg_code, deal_month, deal_amount, "
                "exclusive_area, area_group, is_cancelled FROM apartment_trades "
                "LIMIT 1").fetchone()
        finally:
            con.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[3], 800000000, "만원 단위를 원 단위로 환산해야 한다")
        self.assertEqual(row[4], 84.77)
        self.assertEqual(row[5], 85, "면적 그룹은 반올림한 정수")
        self.assertEqual(row[6], 0)

    def test_second_run_is_a_no_op(self):
        self.set_responder(lambda sgg, month: FakeResponse(ok_body(sgg, month)))
        self.assertEqual(self.run_collect(), 0)

        self.calls.clear()
        self.assertEqual(self.run_collect(), 0)
        self.assertEqual(self.calls, [], "이미 수집한 범위는 다시 호출하지 않는다")


class TestPartialDataIsNotPublished(unittest.TestCase):
    """수집이 불완전하면 shard를 저장하지 않고, 기존 shard도 건드리지 않는다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aptrise_guard_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_incomplete_run_meta_blocks_shard_write(self):
        shard_dir = self.tmp / "shards" / PROVISIONAL / "stable"
        shard_dir.mkdir(parents=True)
        shard = shard_dir / "seoul.json"
        shard.write_text(json.dumps({"marker": "PREVIOUS"}), encoding="utf-8")
        before = shard.read_text(encoding="utf-8")

        (self.tmp / "run_meta.json").write_text(json.dumps({
            "regionGroup": "seoul",
            "includedSidoCodes": ["11"],
            "expectedSggCodes": ["11110", "11140"],
            "successfulSggCodes": ["11110"],
            "failedSggCodes": ["11140"],
        }, ensure_ascii=False), encoding="utf-8")

        runner = self.tmp / "run_compute.py"
        runner.write_text(f'''# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import config
config.ROOT = {str(self.tmp)!r}
config.DB_PATH = {str(self.tmp / "apt.sqlite")!r}
config.SHARDS_DIR = {str(self.tmp / "shards")!r}
import db
db.connect().close()
import compute_apt_rankings
sys.argv = ["compute_apt_rankings.py", "--region-group", "seoul",
            "--stable-month", {STABLE!r}, "--provisional-month", {PROVISIONAL!r}]
compute_apt_rankings.main()
''', encoding="utf-8")

        proc = subprocess.run([sys.executable, str(runner)],
                              capture_output=True, text=True, timeout=180)
        self.assertNotEqual(proc.returncode, 0,
                            "불완전한 수집 상태에서는 실패로 끝나야 한다")
        self.assertIn("실패한 지역 코드", proc.stdout + proc.stderr)
        self.assertEqual(shard.read_text(encoding="utf-8"), before,
                         "기존 shard를 덮어써서는 안 된다")


if __name__ == "__main__":
    unittest.main()
