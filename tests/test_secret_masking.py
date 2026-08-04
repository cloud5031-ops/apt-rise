# -*- coding: utf-8 -*-
"""서비스 키가 로그·예외 메시지로 새지 않는지 검증한다.

requests의 예외 메시지에는 요청 URL이 통째로 들어가고 그 안에 serviceKey
쿼리값이 담긴다. 실제 run 30881311694 로그에서도 45줄 전부가 이 URL을 담고
있었으므로, 마스킹이 없으면 키가 그대로 공개 로그에 남는다.

이 테스트 자체가 키를 노출하지 않도록, 실패 메시지에는 원문 대신
"키가 발견됨" 사실만 남긴다.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# 실제 키처럼 보이되 저장소에 남아도 무해한 더미. URL 인코딩이 필요한
# 문자(+ / =)를 일부러 포함한다.
FAKE_KEY = "AbCd0123efGh4567+iJkL/mnOp==8901qrst"
ENCODED_KEY = "AbCd0123efGh4567%2BiJkL%2FmnOp%3D%3D8901qrst"


class SecretMaskingTestCase(unittest.TestCase):
    def setUp(self):
        import config
        import collect_trades
        self.config = config
        self.ct = collect_trades
        self._saved = config.DATA_GO_KR_API_KEY
        config.DATA_GO_KR_API_KEY = FAKE_KEY
        self.addCleanup(setattr, config, "DATA_GO_KR_API_KEY", self._saved)

    def assert_no_key(self, text, label):
        """실패해도 키 원문을 출력에 남기지 않는다."""
        for needle, kind in ((FAKE_KEY, "원문"), (ENCODED_KEY, "URL 인코딩")):
            if needle in text:
                self.fail(f"{label}: 서비스 키({kind})가 노출됐다 "
                          f"(길이 {len(text)}자 문자열 안에서 발견)")


class TestMaskSecrets(SecretMaskingTestCase):
    def test_raw_key_in_query_is_masked(self):
        url = ("https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/"
               f"getRTMSDataSvcAptTradeDev?serviceKey={FAKE_KEY}"
               "&LAWD_CD=11110&DEAL_YMD=202602")
        out = self.ct.mask_secrets(url)
        self.assert_no_key(out, "쿼리 중간 위치 원문")
        self.assertIn("serviceKey=***", out)

    def test_encoded_key_in_query_is_masked(self):
        url = (f"https://apis.data.go.kr/path?serviceKey={ENCODED_KEY}"
               "&LAWD_CD=11110")
        out = self.ct.mask_secrets(url)
        self.assert_no_key(out, "URL 인코딩 값")
        self.assertIn("serviceKey=***", out)

    def test_key_as_last_parameter_is_masked(self):
        url = f"https://apis.data.go.kr/path?LAWD_CD=11110&serviceKey={FAKE_KEY}"
        out = self.ct.mask_secrets(url)
        self.assert_no_key(out, "쿼리 마지막 위치")
        self.assertTrue(out.endswith("serviceKey=***"), out)

    def test_bare_key_outside_query_is_masked(self):
        """쿼리 문자열이 아니어도 키 원문이 보이면 지운다."""
        self.assert_no_key(
            self.ct.mask_secrets(f"인증 실패: key={FAKE_KEY} 를 확인하세요"),
            "쿼리 밖 원문")

    def test_useful_context_is_preserved(self):
        """마스킹이 진단에 필요한 정보까지 지우면 안 된다."""
        url = ("https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/"
               f"getRTMSDataSvcAptTradeDev?serviceKey={FAKE_KEY}"
               "&LAWD_CD=11110&DEAL_YMD=202602&pageNo=1")
        out = self.ct.mask_secrets(url)
        for keep in ("apis.data.go.kr", "RTMSDataSvcAptTradeDev",
                     "LAWD_CD=11110", "DEAL_YMD=202602", "pageNo=1"):
            self.assertIn(keep, out, f"진단에 필요한 '{keep}'이 사라졌다")


class TestExceptionMessagesAreMasked(SecretMaskingTestCase):
    """실제 실패 경로에서 만들어지는 예외에 키가 남지 않아야 한다."""

    def _raise_from_requests(self, exc):
        import requests

        def boom(*a, **kw):
            raise exc
        saved = self.ct.requests.get
        self.ct.requests.get = boom
        self.addCleanup(setattr, self.ct.requests, "get", saved)

        with self.assertRaises(self.ct.TransientApiError) as cm:
            self.ct.fetch_trades("11110", "202602", budget_seconds=5)
        return str(cm.exception)

    def test_connect_timeout_message_is_masked(self):
        """run 30881311694에서 실제로 나온 형태의 예외."""
        import requests
        real_url = ("https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/"
                    f"getRTMSDataSvcAptTradeDev?serviceKey={FAKE_KEY}"
                    "&LAWD_CD=11110&DEAL_YMD=202602&pageNo=1&numOfRows=1000")
        exc = requests.exceptions.ConnectTimeout(
            f"HTTPSConnectionPool(host='apis.data.go.kr', port=443): "
            f"Max retries exceeded with url: {real_url} "
            f"(Caused by ConnectTimeoutError(...))")
        message = self._raise_from_requests(exc)

        self.assert_no_key(message, "ConnectTimeout 예외 메시지")
        self.assertIn("ConnectTimeout", message, "예외 종류는 남아야 한다")
        self.assertIn("apis.data.go.kr", message, "호스트는 남아야 한다")

    def test_encoded_key_in_exception_is_masked(self):
        import requests
        exc = requests.exceptions.ConnectionError(
            f"failed for url: https://apis.data.go.kr/p?serviceKey={ENCODED_KEY}&LAWD_CD=11110")
        self.assert_no_key(self._raise_from_requests(exc), "인코딩된 키가 담긴 예외")

    def test_xml_parse_error_body_is_masked(self):
        class Resp:
            status_code = 200
            text = f"<broken>{FAKE_KEY}"

            def raise_for_status(self):
                pass

        saved = self.ct.requests.get
        self.ct.requests.get = lambda *a, **kw: Resp()
        self.addCleanup(setattr, self.ct.requests, "get", saved)

        with self.assertRaises(self.ct.TransientApiError) as cm:
            self.ct.fetch_trades("11110", "202602", budget_seconds=5)
        self.assert_no_key(str(cm.exception), "XML 파싱 실패 시 본문 발췌")


class TestLogOutputIsMasked(SecretMaskingTestCase):
    def test_log_masks_before_printing(self):
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ct.log(f"요청 실패: https://x/y?serviceKey={FAKE_KEY}&LAWD_CD=11110")
        out = buf.getvalue()

        self.assert_no_key(out, "log() 출력")
        self.assertIn("LAWD_CD=11110", out)


if __name__ == "__main__":
    unittest.main()
