# -*- coding: utf-8 -*-
"""진단 workflow와 스크립트의 안전성·분류 정확성 검증.

두 가지를 지킨다.
  1) 진단이 서비스 키를 어떤 형태로도 흘리지 않는다.
  2) 진단 결과가 나쁜 것과 진단이 실패한 것을 혼동하지 않는다.

네트워크에 나가지 않는다. probe 함수를 대체해 판정 로직만 검사한다.

실행:
    python -m unittest discover -s tests -v
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
WORKFLOW = REPO / ".github" / "workflows" / "data-go-kr-network-diagnostic.yml"
SCRIPT = SCRIPTS / "diagnose_network.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# 저장소에 남아도 무해한 더미. URL 인코딩이 필요한 문자를 일부러 넣는다.
FAKE_KEY = "QqWw1234eErR5678+tTyY/uUiI==9012oOpP"
ENCODED_KEY = "QqWw1234eErR5678%2BtTyY%2FuUiI%3D%3D9012oOpP"
PLUS_KEY = "QqWw1234eErR5678%2BtTyY%2FuUiI%3D%3D9012oOpP"


def load_workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def diagnostic_job():
    return next(iter(load_workflow()["jobs"].values()))


class TestWorkflowShape(unittest.TestCase):
    """13번 요구: 수동 전용, 쓰기 권한 없음, 발행 없음."""

    def setUp(self):
        self.raw = WORKFLOW.read_text(encoding="utf-8")
        self.data = load_workflow()
        # PyYAML은 YAML 1.1 규칙으로 키 `on`을 True로 읽는다.
        self.triggers = self.data.get("on", self.data.get(True))

    def test_manual_dispatch_only(self):
        self.assertIn("workflow_dispatch", self.triggers)
        self.assertNotIn("schedule", self.triggers, "진단은 자동 실행하지 않는다")
        self.assertNotIn("push", self.triggers)

    def test_permissions_are_read_only(self):
        self.assertEqual(self.data.get("permissions"), {"contents": "read"})

    def test_никогда_publishes(self):
        for banned in ("git commit", "git push", "git add", "HEAD:main"):
            self.assertNotIn(banned, self.raw, f"진단이 '{banned}'를 하면 안 된다")

    def test_egress_ip_is_opt_in(self):
        option = self.triggers["workflow_dispatch"]["inputs"]["check_egress_ip"]
        self.assertEqual(option["type"], "boolean")
        self.assertIs(option["default"], False, "기본값은 꺼져 있어야 한다")

    def test_probe_failures_do_not_fail_the_job(self):
        job = diagnostic_job()
        by_name = {s["name"]: s for s in job["steps"] if "name" in s}
        for name in ("Runner info", "curl probes (no secret involved)",
                     "Network diagnostic"):
            self.assertTrue(by_name[name].get("continue-on-error"),
                            f"'{name}' 실패가 job을 빨갛게 만들면 안 된다")

    def test_result_is_uploaded_even_on_failure(self):
        job = diagnostic_job()
        upload = next(s for s in job["steps"]
                      if s.get("name") == "Upload diagnostic result")
        self.assertEqual(upload.get("if"), "always()")


class TestNoSecretLeakInWorkflow(unittest.TestCase):
    """12번 요구: 셸 트레이싱·env 덤프·시크릿 echo 금지."""

    def setUp(self):
        self.raw = WORKFLOW.read_text(encoding="utf-8")

    def test_no_shell_tracing(self):
        self.assertNotRegex(self.raw, r"set\s+-[a-z]*x", "set -x 금지")

    def test_no_env_dump(self):
        for banned in (r"\benv\s*$", r"\benv\s*\|", r"\bprintenv\b", r"\bset\s*$"):
            self.assertNotRegex(self.raw, banned, "환경변수 전체 덤프 금지")

    def test_secret_is_never_echoed(self):
        for line in self.raw.splitlines():
            if "secrets." in line:
                self.assertNotIn("echo", line, f"시크릿을 출력하면 안 된다: {line.strip()}")
        self.assertNotRegex(self.raw, r"echo[^\n]*DATA_GO_KR_API_KEY")

    def test_secret_only_reaches_the_python_step(self):
        job = diagnostic_job()
        users = [s["name"] for s in job["steps"]
                 if "DATA_GO_KR_API_KEY" in str(s.get("env", {}))]
        self.assertEqual(users, ["Network diagnostic"],
                         "키는 파이썬 진단 스텝에서만 쓰여야 한다")

    def test_curl_step_carries_no_secret(self):
        job = diagnostic_job()
        curl_step = next(s for s in job["steps"]
                         if s.get("name") == "curl probes (no secret involved)")
        self.assertNotIn("secrets", str(curl_step))
        self.assertNotIn("-v", curl_step["run"].split(), "curl -v는 헤더를 노출한다")


class TestMasking(unittest.TestCase):
    """1번 요구: 진단 스크립트가 자체 마스킹을 갖는다."""

    def setUp(self):
        import diagnose_network
        self.dn = diagnose_network
        self._saved = os.environ.get("DATA_GO_KR_API_KEY")
        os.environ["DATA_GO_KR_API_KEY"] = FAKE_KEY
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("DATA_GO_KR_API_KEY", None)
        else:
            os.environ["DATA_GO_KR_API_KEY"] = self._saved

    def assert_no_key(self, text, label):
        for needle, kind in ((FAKE_KEY, "원문"), (ENCODED_KEY, "URL 인코딩")):
            if needle in text:
                self.fail(f"{label}: 서비스 키({kind})가 노출됐다")

    def test_does_not_import_collection_code(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("import collect_trades", source)
        self.assertNotIn("from collect_trades", source)

    def test_raw_key_is_masked(self):
        self.assert_no_key(self.dn.mask(f"key={FAKE_KEY} 뒤에 문자열"), "원문")

    def test_encoded_key_is_masked(self):
        self.assert_no_key(self.dn.mask(f"?serviceKey={ENCODED_KEY}&LAWD_CD=11110"),
                           "URL 인코딩")

    def test_quote_plus_variant_is_masked(self):
        self.assert_no_key(self.dn.mask(f"x={PLUS_KEY}"), "quote_plus")

    def test_service_key_query_param_is_blanked(self):
        out = self.dn.mask("https://apis.data.go.kr/p?serviceKey=WHATEVER&LAWD_CD=11110")
        self.assertIn("serviceKey=***", out)
        self.assertIn("LAWD_CD=11110", out, "진단에 필요한 정보는 남아야 한다")

    def test_log_masks_before_printing(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.dn.log(f"실패: https://x/y?serviceKey={FAKE_KEY}&LAWD_CD=11110")
        self.assert_no_key(buf.getvalue(), "log() 출력")


class VerdictHarness(unittest.TestCase):
    """probe 결과를 직접 구성해 판정 로직만 검사한다 (네트워크 없음)."""

    def setUp(self):
        import diagnose_network
        self.dn = diagnose_network

    def host(self, name, *, ipv4=("1.2.3.4",), ipv6=(),
             dns_ok=True, tcp_ok=True, tls_ok=True, http_ok=True,
             tcp_ok_v6=None):
        dn = self.dn
        entry = {"host": name, "role": "test",
                 "dns": {"host": name, "ipv4": list(ipv4), "ipv6": list(ipv6),
                         "ok": dns_ok, "elapsed": 0.01,
                         "error": None if dns_ok else "NXDOMAIN",
                         "ipv6_status": "present" if ipv6 else "AAAA record absent"},
                 "tcp": [], "tls": [], "http": []}
        if not dns_ok:
            return entry
        families = [("IPv4", ip, tcp_ok) for ip in ipv4]
        families += [("IPv6", ip, tcp_ok if tcp_ok_v6 is None else tcp_ok_v6)
                     for ip in ipv6]
        for label, ip, ok in families:
            entry["tcp"].append({
                "ip": ip, "family": label,
                "attempts": [{"attempt": i, "ip": ip, "ok": ok, "elapsed": 10.0,
                              "error": None if ok else
                              "TimeoutError: timed out"} for i in (1, 2, 3)]})
            if ok:
                entry["tls"].append({"ip": ip, "family": label, "ok": tls_ok,
                                     "elapsed": 0.2, "subject": "CN=x",
                                     "error": None if tls_ok else "SSLError: bad handshake"})
                entry["http"].append({"ip": ip, "family": label,
                                      "ok": http_ok and tls_ok,
                                      "status": "HTTP/1.1 200 OK" if http_ok else None,
                                      "elapsed": 0.3, "error": None})
            else:
                entry["tls"].append({"ip": ip, "family": label, "ok": None,
                                     "skipped": "TCP 실패로 미실행"})
                entry["http"].append({"ip": ip, "family": label, "ok": None,
                                      "skipped": "TCP 실패로 미실행"})
        return entry

    def hosts(self, target, peer=None, control=None):
        dn = self.dn
        return {
            dn.TARGET: target,
            dn.PEER: peer if peer is not None else self.host(dn.PEER),
            dn.CONTROL: control if control is not None else self.host(dn.CONTROL),
        }


class TestVerdicts(VerdictHarness):
    def test_dns_failure_is_A(self):
        target = self.host(self.dn.TARGET, dns_ok=False)
        code, reason = self.dn.decide(self.hosts(target), {"skipped": "no key"})
        self.assertEqual(code, "A")
        self.assertIn("DNS", reason)

    def test_general_outbound_failure_is_C(self):
        target = self.host(self.dn.TARGET, tcp_ok=False)
        control = self.host(self.dn.CONTROL, tcp_ok=False)
        code, reason = self.dn.decide(self.hosts(target, control=control),
                                      {"skipped": "no key"})
        self.assertEqual(code, "C")
        self.assertIn(self.dn.CONTROL, reason)

    def test_target_only_tcp_timeout_is_B(self):
        """이번 사고의 형태: control은 되는데 대상만 TCP가 안 붙는다."""
        target = self.host(self.dn.TARGET, ipv4=("27.0.0.1",), tcp_ok=False)
        code, reason = self.dn.decide(self.hosts(target), {"skipped": "no key"})
        self.assertEqual(code, "B")
        self.assertIn("27.0.0.1", reason)
        self.assertIn(self.dn.CONTROL, reason)

    def test_B_reason_does_not_overclaim_selective_blocking(self):
        """5번 요구: peer 성공을 '선별 차단 증명'으로 말하지 않는다."""
        target = self.host(self.dn.TARGET, tcp_ok=False)
        _, reason = self.dn.decide(self.hosts(target), {"skipped": "no key"})
        self.assertIn("증명하지는 않습니다", reason)

    def test_tls_failure_is_D(self):
        target = self.host(self.dn.TARGET, tls_ok=False, http_ok=False)
        code, _ = self.dn.decide(self.hosts(target), {"skipped": "no key"})
        self.assertEqual(code, "D")

    def test_api_layer_failure_is_E(self):
        target = self.host(self.dn.TARGET)
        code, reason = self.dn.decide(
            self.hosts(target), {"ok": False, "error": "ReadTimeout: x"})
        self.assertEqual(code, "E")
        self.assertIn("API", reason)

    def test_family_asymmetry_is_F(self):
        target = self.host(self.dn.TARGET, ipv4=("1.2.3.4",), ipv6=("::1",),
                           tcp_ok=True, tcp_ok_v6=False)
        code, _ = self.dn.decide(self.hosts(target), {"skipped": "no key"})
        self.assertEqual(code, "F")

    def test_all_healthy_is_G_with_endpoint_detail(self):
        target = self.host(self.dn.TARGET)
        code, reason = self.dn.decide(
            self.hosts(target),
            {"ok": True, "status": 200, "result_code": "00"})
        self.assertEqual(code, "G")
        self.assertIn("200", reason)

    def test_every_verdict_has_a_human_reason(self):
        """9번 요구: 코드 한 글자만 남기지 않는다."""
        cases = [
            self.host(self.dn.TARGET, dns_ok=False),
            self.host(self.dn.TARGET, tcp_ok=False),
            self.host(self.dn.TARGET, tls_ok=False, http_ok=False),
            self.host(self.dn.TARGET),
        ]
        for target in cases:
            code, reason = self.dn.decide(self.hosts(target), {"skipped": "no key"})
            self.assertIn(code, "ABCDEFG")
            self.assertGreater(len(reason), 40, f"{code} 판정의 설명이 너무 짧다")


class TestClassificationDetails(VerdictHarness):
    def test_absent_aaaa_is_not_an_ipv6_failure(self):
        """2번 요구: AAAA 부재를 IPv6 장애로 적지 않는다."""
        target = self.host(self.dn.TARGET, ipv4=("1.2.3.4",), ipv6=())
        self.assertEqual(target["dns"]["ipv6_status"], "AAAA record absent")

        code, _ = self.dn.decide(self.hosts(target), {"skipped": "no key"})
        self.assertNotEqual(code, "F", "AAAA가 없을 뿐인데 경로 비대칭으로 보면 안 된다")
        self.assertEqual(self.dn._cell(target, "IPv6", "tcp"), "AAAA 없음")

    def test_tls_and_http_are_skipped_when_tcp_fails(self):
        """4번 요구: TCP 실패를 TLS 실패로 오인하지 않는다."""
        target = self.host(self.dn.TARGET, tcp_ok=False)
        for kind in ("tls", "http"):
            entry = next(x for x in target[kind] if x["family"] == "IPv4")
            self.assertIsNone(entry["ok"])
            self.assertIn("skipped", entry)
            self.assertEqual(self.dn._cell(target, "IPv4", kind), "skipped")

    def test_tcp_probe_runs_three_attempts(self):
        """3번 요구: IP마다 3회."""
        self.assertEqual(self.dn.TCP_ATTEMPTS, 3)
        self.assertEqual(self.dn.CONNECT_TIMEOUT, 10)

    def test_no_hardcoded_target_ip(self):
        """3번 요구: 관측된 IP를 코드에 박지 않는다."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("27.101.236.63", source)


class TestEndToEndIsolated(unittest.TestCase):
    """네트워크를 전부 막고 실행해도 스크립트가 정상 종료하는지."""

    def run_isolated(self, extra_env=None):
        """socket.getaddrinfo를 막아 모든 probe가 실패하게 만든 뒤 실행한다."""
        tmp = Path(tempfile.mkdtemp(prefix="netdiag_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        summary = tmp / "summary.md"
        summary.touch()

        runner = tmp / "run_isolated.py"
        runner.write_text(f'''# -*- coding: utf-8 -*-
import socket, sys, runpy
sys.path.insert(0, {str(SCRIPTS)!r})

def blocked(*a, **k):
    raise socket.gaierror("[Errno -3] Temporary failure in name resolution")

socket.getaddrinfo = blocked
socket.create_connection = blocked
try:
    import requests
    requests.get = blocked
except Exception:
    pass

runpy.run_path({str(SCRIPT)!r}, run_name="__main__")
''', encoding="utf-8")

        env = dict(os.environ,
                   GITHUB_STEP_SUMMARY=str(summary),
                   GITHUB_RUN_ID="test-run", GITHUB_RUN_ATTEMPT="1",
                   RUNNER_OS="Linux", RUNNER_ARCH="X64",
                   DATA_GO_KR_API_KEY=FAKE_KEY)
        env.pop("CHECK_EGRESS_IP", None)
        env.update(extra_env or {})

        result = subprocess.run([sys.executable, str(runner)], cwd=str(tmp),
                                capture_output=True, text=True, env=env,
                                encoding="utf-8", errors="replace", timeout=300)
        return result, tmp, summary

    def test_exits_zero_when_every_probe_fails(self):
        """10번 요구: 진단 실패와 연결 실패를 혼동하지 않는다."""
        result, tmp, summary = self.run_isolated()
        self.assertEqual(result.returncode, 0,
                         (result.stdout or "") + (result.stderr or ""))

        report = json.loads((tmp / "network_diagnostic.json").read_text(encoding="utf-8"))
        self.assertIn(report["verdict"], "ABCDEFG")
        self.assertTrue(report["verdict_reason"])
        self.assertTrue(summary.read_text(encoding="utf-8").strip(),
                        "Step Summary가 비어 있으면 안 된다")

    def test_no_secret_anywhere_in_outputs(self):
        """12번 요구: stdout/stderr/JSON/Summary 어디에도 키가 없다."""
        result, tmp, summary = self.run_isolated()
        blobs = {
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "json": (tmp / "network_diagnostic.json").read_text(encoding="utf-8"),
            "summary": summary.read_text(encoding="utf-8"),
        }
        for label, blob in blobs.items():
            for needle, kind in ((FAKE_KEY, "원문"), (ENCODED_KEY, "URL 인코딩")):
                if needle in blob:
                    self.fail(f"{label}에 서비스 키({kind})가 남았다")

    def test_report_contains_required_fields(self):
        """8번 요구: JSON 필수 항목."""
        _, tmp, _ = self.run_isolated()
        report = json.loads((tmp / "network_diagnostic.json").read_text(encoding="utf-8"))
        for key in ("timestamp", "run_id", "run_attempt", "runner_os", "runner_arch",
                    "hosts", "endpoint", "egress_ip", "verdict", "verdict_reason"):
            self.assertIn(key, report)
        self.assertIn("apis.data.go.kr", report["hosts"])
        target = report["hosts"]["apis.data.go.kr"]
        for key in ("dns", "tcp", "tls", "http"):
            self.assertIn(key, target)

    def test_report_holds_no_response_body_or_key(self):
        _, tmp, _ = self.run_isolated()
        raw = (tmp / "network_diagnostic.json").read_text(encoding="utf-8")
        self.assertNotIn("serviceKey=" + FAKE_KEY, raw)
        report = json.loads(raw)
        self.assertNotIn("body", json.dumps(report))

    def test_egress_probe_is_off_unless_requested(self):
        _, tmp, _ = self.run_isolated()
        report = json.loads((tmp / "network_diagnostic.json").read_text(encoding="utf-8"))
        self.assertFalse(report["egress_ip"]["enabled"])

        _, tmp2, _ = self.run_isolated({"CHECK_EGRESS_IP": "true"})
        report2 = json.loads((tmp2 / "network_diagnostic.json").read_text(encoding="utf-8"))
        self.assertTrue(report2["egress_ip"]["enabled"])
        self.assertIn("ip", report2["egress_ip"])


class TestExistingWorkflowsUntouched(unittest.TestCase):
    """13번 요구: 운영 workflow와 수집 코드에 손대지 않는다."""

    def test_operational_files_are_not_in_this_change(self):
        base = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            cwd=str(REPO), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if base.returncode != 0:
            self.skipTest("origin/main과 비교할 수 없다")
        changed = [f for f in base.stdout.split() if f]
        for guarded in (".github/workflows/capital-daily-refresh.yml",
                        ".github/workflows/nationwide-weekly-refresh.yml",
                        "scripts/collect_trades.py"):
            self.assertNotIn(guarded, changed, f"{guarded}는 이 변경에 포함되면 안 된다")


if __name__ == "__main__":
    unittest.main()
