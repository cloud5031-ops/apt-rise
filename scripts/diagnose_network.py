#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 러너에서 apis.data.go.kr로 가는 경로가 어디서 끊기는지 진단한다.

수집 코드(collect_trades.py)를 import하지 않는다. 진단은 운영 경로와 독립이어야
같은 버그를 두 번 겪지 않는다. 마스킹도 여기서 따로 구현한다.

계층을 분리해 본다: DNS → TCP 443 → TLS → HTTP. 어느 층에서 끊기는지 알아야
"연결이 안 된다"를 실제 원인으로 좁힐 수 있다.

데이터를 수집하지도, 파일을 발행하지도 않는다. 실제 endpoint는 1회만 부른다.
어떤 probe가 실패해도 스크립트는 항상 exit 0으로 끝난다 — 진단 결과가 나쁜 것과
진단이 실패한 것은 다르다.
"""
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# 현재 디렉터리 기준. workflow의 artifact 경로(network_diagnostic.json)와 같은
# 자리에 떨어져야 하고, 다른 곳에서 실행했을 때 저장소를 더럽히지 않는다.
RESULT_PATH = Path("network_diagnostic.json")

PORT = 443
CONNECT_TIMEOUT = 10          # collect_trades.py와 같은 값 (수정하지 않는다)
TCP_ATTEMPTS = 3

TARGET = "apis.data.go.kr"    # 진단 대상
PEER = "www.data.go.kr"       # 같은 기관 포털 — 보조 근거
CONTROL = "github.com"        # 대조군 — 러너 outbound HTTPS 자체 확인

EGRESS_ECHO = ("checkip.amazonaws.com", "/")   # opt-in일 때만 사용


# ── 비밀값 마스킹 ────────────────────────────────────────────────
# GitHub의 자동 시크릿 마스킹은 URL 인코딩 변형을 놓칠 수 있어 직접 지운다.
_SERVICE_KEY_RE = re.compile(r"(serviceKey=)[^&\s'\"<>]*", re.IGNORECASE)
_MIN_SECRET_LEN = 8


def _secret_variants():
    key = (os.environ.get("DATA_GO_KR_API_KEY") or "").strip()
    if len(key) < _MIN_SECRET_LEN:
        return []
    variants = {
        key,
        urllib.parse.quote(key, safe=""),
        urllib.parse.quote_plus(key),
    }
    return sorted((v for v in variants if len(v) >= _MIN_SECRET_LEN),
                  key=len, reverse=True)


def mask(text) -> str:
    s = str(text)
    s = _SERVICE_KEY_RE.sub(r"\1***", s)
    for value in _secret_variants():
        s = s.replace(value, "***")
    return s


def log(msg=""):
    """콘솔 인코딩 때문에 진단이 죽지 않게 한다.

    러너는 UTF-8이지만 로컬(Windows cp949 등)에서는 한글 출력이 그대로
    UnicodeEncodeError를 낸다. 출력 실패가 진단 자체를 중단시키면 안 된다.
    """
    text = mask(msg)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"),
              flush=True)
    except Exception:
        pass


def short(text, limit=200):
    s = mask(text).replace("\n", " ").strip()
    return s[:limit]


def describe(exc):
    return f"{type(exc).__name__}: {short(exc, 160)}"


# ── DNS ──────────────────────────────────────────────────────────

def resolve(host):
    """IPv4/IPv6를 나눠서 조회한다.

    AAAA 레코드가 없는 것은 IPv6 '장애'가 아니다. apis.data.go.kr은 실제로
    A 레코드만 갖고 있어, 이를 실패로 적으면 원인을 오해하게 된다.
    """
    out = {"host": host, "ipv4": [], "ipv6": [], "ok": False,
           "elapsed": None, "error": None, "ipv6_status": None}
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, PORT, type=socket.SOCK_STREAM)
        out["elapsed"] = round(time.monotonic() - started, 3)
        for family, _, _, _, sockaddr in infos:
            addr = sockaddr[0]
            if family == socket.AF_INET and addr not in out["ipv4"]:
                out["ipv4"].append(addr)
            elif family == socket.AF_INET6 and addr not in out["ipv6"]:
                out["ipv6"].append(addr)
        out["ok"] = bool(out["ipv4"] or out["ipv6"])
        out["ipv6_status"] = "present" if out["ipv6"] else "AAAA record absent"
    except Exception as exc:
        out["elapsed"] = round(time.monotonic() - started, 3)
        out["error"] = describe(exc)
        out["ipv6_status"] = "lookup failed"
    return out


# ── TCP ──────────────────────────────────────────────────────────

def tcp_probe(ip, family, attempts=TCP_ATTEMPTS):
    """한 IP에 대해 TCP 443을 여러 번 두드린다."""
    results = []
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        record = {"attempt": attempt, "ip": ip, "ok": False,
                  "elapsed": None, "error": None}
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect((ip, PORT))
            record["ok"] = True
        except Exception as exc:
            record["error"] = describe(exc)
        finally:
            record["elapsed"] = round(time.monotonic() - started, 3)
            try:
                sock.close()
            except Exception:
                pass
        results.append(record)
        log(f"    TCP {ip}:{PORT} 시도 {attempt}/{attempts} "
            f"-> {'성공' if record['ok'] else '실패'} "
            f"({record['elapsed']}s){'' if record['ok'] else ' ' + str(record['error'])}")
    return results


# ── TLS ──────────────────────────────────────────────────────────

def tls_probe(ip, family, server_name):
    """TCP가 붙은 IP에 대해서만 handshake를 시도한다."""
    record = {"ip": ip, "ok": False, "elapsed": None, "error": None,
              "subject": None, "issuer": None, "not_after": None}
    context = ssl.create_default_context()
    started = time.monotonic()
    try:
        with socket.socket(family, socket.SOCK_STREAM) as raw:
            raw.settimeout(CONNECT_TIMEOUT)
            raw.connect((ip, PORT))
            with context.wrap_socket(raw, server_hostname=server_name) as tls:
                cert = tls.getpeercert() or {}
                record["ok"] = True
                record["subject"] = _name_of(cert.get("subject"))
                record["issuer"] = _name_of(cert.get("issuer"))
                record["not_after"] = cert.get("notAfter")
    except Exception as exc:
        record["error"] = describe(exc)
    record["elapsed"] = round(time.monotonic() - started, 3)
    return record


def _name_of(rdn_sequence):
    if not rdn_sequence:
        return None
    parts = []
    for rdn in rdn_sequence:
        for key, value in rdn:
            if key in ("commonName", "organizationName"):
                parts.append(f"{key}={value}")
    return ", ".join(parts) or None


# ── HTTP (키 없이) ───────────────────────────────────────────────

def http_status(ip, family, server_name, path="/"):
    """TLS 위에 최소한의 GET을 보내 상태줄만 읽는다.

    IP를 직접 지정하므로 주소패밀리별 결과를 정확히 나눌 수 있다.
    목적은 '응답을 받았는가'이지 내용이 아니다.
    """
    record = {"ip": ip, "ok": False, "status": None,
              "elapsed": None, "error": None}
    context = ssl.create_default_context()
    started = time.monotonic()
    try:
        with socket.socket(family, socket.SOCK_STREAM) as raw:
            raw.settimeout(CONNECT_TIMEOUT)
            raw.connect((ip, PORT))
            with context.wrap_socket(raw, server_hostname=server_name) as tls:
                tls.settimeout(CONNECT_TIMEOUT)
                request = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {server_name}\r\n"
                    "User-Agent: apt-rise-network-diagnostic\r\n"
                    "Accept: */*\r\n"
                    "Connection: close\r\n\r\n"
                )
                tls.sendall(request.encode("ascii"))
                first = tls.recv(256).decode("latin-1", errors="replace")
                status_line = first.split("\r\n", 1)[0]
                record["status"] = status_line.strip()[:80]
                record["ok"] = status_line.upper().startswith("HTTP/")
    except Exception as exc:
        record["error"] = describe(exc)
    record["elapsed"] = round(time.monotonic() - started, 3)
    return record


# ── 호스트 한 곳 전체 검사 ───────────────────────────────────────

def probe_host(host, role):
    log(f"\n=== {host} ({role}) ===")
    entry = {"host": host, "role": role, "dns": None,
             "tcp": [], "tls": [], "http": []}

    dns = resolve(host)
    entry["dns"] = dns
    if dns["error"]:
        log(f"  DNS 실패 ({dns['elapsed']}s): {dns['error']}")
        return entry
    log(f"  DNS {dns['elapsed']}s | IPv4={dns['ipv4'] or '없음'} "
        f"| IPv6={dns['ipv6'] or dns['ipv6_status']}")

    families = [(socket.AF_INET, ip) for ip in dns["ipv4"]]
    families += [(socket.AF_INET6, ip) for ip in dns["ipv6"]]

    for family, ip in families:
        label = "IPv4" if family == socket.AF_INET else "IPv6"
        attempts = tcp_probe(ip, family)
        entry["tcp"].append({"ip": ip, "family": label, "attempts": attempts})

        if any(a["ok"] for a in attempts):
            tls = tls_probe(ip, family, host)
            tls["family"] = label
            entry["tls"].append(tls)
            log(f"    TLS {ip} -> {'성공' if tls['ok'] else '실패'} "
                f"({tls['elapsed']}s) "
                f"{tls['subject'] or ''} {tls['error'] or ''}".rstrip())

            http = http_status(ip, family, host)
            http["family"] = label
            entry["http"].append(http)
            log(f"    HTTP {ip} -> {http['status'] or http['error']} "
                f"({http['elapsed']}s)")
        else:
            entry["tls"].append({"ip": ip, "family": label, "ok": None,
                                 "skipped": "TCP 실패로 미실행"})
            entry["http"].append({"ip": ip, "family": label, "ok": None,
                                  "skipped": "TCP 실패로 미실행"})
            log(f"    TLS {ip} -> skipped (TCP 실패)")
            log(f"    HTTP {ip} -> skipped (TCP 실패)")
    return entry


# ── 실제 endpoint (1회) ──────────────────────────────────────────

def first_target_month():
    """manifest의 안정월 기준 첫 수집 대상월(stable-3)."""
    try:
        manifest = json.loads(
            (REPO / "site" / "data" / "apt_rankings_manifest.json")
            .read_text(encoding="utf-8"))
        stable = str(manifest["stableMonth"])
        year, month = int(stable[:4]), int(stable[4:6])
        total = year * 12 + (month - 1) - 3
        return f"{total // 12:04d}{total % 12 + 1:02d}"
    except Exception:
        return "202603"


def endpoint_url():
    """수집 코드가 쓰는 endpoint를 config에서 읽는다 (collect_trades는 건드리지 않음)."""
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import config
        return config.MOLIT_ENDPOINT
    except Exception:
        return ("https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/"
                "getRTMSDataSvcAptTradeDev")


def real_endpoint_probe():
    """키가 있을 때만, 딱 한 번 실제 endpoint를 부른다. 부하 테스트가 아니다."""
    record = {"ok": False, "skipped": None, "status": None, "elapsed": None,
              "result_code": None, "message": None, "error": None,
              "month": None, "lawd_cd": "11110"}

    key = (os.environ.get("DATA_GO_KR_API_KEY") or "").strip()
    if not key:
        record["skipped"] = "DATA_GO_KR_API_KEY 미설정"
        log("\n=== 실제 endpoint === skipped (시크릿 없음)")
        return record

    month = first_target_month()
    record["month"] = month
    url = endpoint_url()
    # URL 전체는 절대 출력하지 않는다. 경로까지만 남긴다.
    log(f"\n=== 실제 endpoint === {urllib.parse.urlsplit(url).path} "
        f"(LAWD_CD=11110, DEAL_YMD={month}, numOfRows=5, serviceKey=***)")

    started = time.monotonic()
    try:
        import requests
        response = requests.get(
            url,
            params={"serviceKey": key, "LAWD_CD": "11110", "DEAL_YMD": month,
                    "pageNo": 1, "numOfRows": 5},
            timeout=(CONNECT_TIMEOUT, 30),
        )
        record["elapsed"] = round(time.monotonic() - started, 3)
        record["status"] = response.status_code
        record["ok"] = True          # HTTP 응답을 받았다는 뜻
        body = response.text or ""
        for tag in ("resultCode", "returnReasonCode"):
            found = re.search(rf"<{tag}>\s*([^<]*)</{tag}>", body)
            if found:
                record["result_code"] = found.group(1).strip()
                break
        for tag in ("resultMsg", "returnAuthMsg", "errMsg"):
            found = re.search(rf"<{tag}>\s*([^<]*)</{tag}>", body)
            if found:
                record["message"] = short(found.group(1), 120)
                break
        log(f"  HTTP {record['status']} ({record['elapsed']}s) "
            f"resultCode={record['result_code']} msg={record['message']}")
    except Exception as exc:
        record["elapsed"] = round(time.monotonic() - started, 3)
        record["error"] = describe(exc)
        log(f"  실패 ({record['elapsed']}s): {record['error']}")
    return record


# ── egress IP (opt-in) ───────────────────────────────────────────

def egress_ip_probe(enabled):
    """러너의 public IPv4. 응답 본문에서 IP 한 줄만 취한다."""
    record = {"enabled": bool(enabled), "ok": False, "ip": None, "error": None}
    if not enabled:
        record["error"] = "비활성 (check_egress_ip=false)"
        return record
    host, path = EGRESS_ECHO
    log(f"\n=== egress IP === {host}")
    try:
        dns = resolve(host)
        if not dns["ipv4"]:
            raise RuntimeError("echo 호스트 A 레코드 없음")
        ip = dns["ipv4"][0]
        context = ssl.create_default_context()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
            raw.settimeout(CONNECT_TIMEOUT)
            raw.connect((ip, PORT))
            with context.wrap_socket(raw, server_hostname=host) as tls:
                tls.settimeout(CONNECT_TIMEOUT)
                tls.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                    "Connection: close\r\n\r\n".encode("ascii"))
                chunks = []
                while len(b"".join(chunks)) < 4096:
                    part = tls.recv(1024)
                    if not part:
                        break
                    chunks.append(part)
        body = b"".join(chunks).decode("latin-1", errors="replace")
        found = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", body.split("\r\n\r\n", 1)[-1])
        if found:
            record["ip"] = found.group(1)
            record["ok"] = True
            log(f"  public IPv4 = {record['ip']}")
        else:
            record["error"] = "응답에서 IPv4를 찾지 못함"
    except Exception as exc:
        record["error"] = describe(exc)
        log(f"  실패: {record['error']}")
    return record


# ── 판정 ─────────────────────────────────────────────────────────

def _tcp_ok(entry, family="IPv4"):
    for item in entry.get("tcp", []):
        if item["family"] == family and any(a["ok"] for a in item["attempts"]):
            return True
    return False


def _tcp_attempted(entry, family="IPv4"):
    return any(item["family"] == family for item in entry.get("tcp", []))


def _tls_ok(entry):
    return any(t.get("ok") for t in entry.get("tls", []))


def _http_ok(entry):
    return any(h.get("ok") for h in entry.get("http", []))


def decide(hosts, endpoint):
    """A~G 판정과 사람이 읽는 이유를 함께 만든다."""
    target = hosts[TARGET]
    peer = hosts[PEER]
    control = hosts[CONTROL]

    if not target["dns"]["ok"]:
        return "A", (f"{TARGET} DNS 조회에 실패했습니다 "
                     f"({target['dns']['error']}). TCP 이하 계층은 판단할 수 없습니다.")

    if not _http_ok(control):
        return "C", (f"대조군 {CONTROL} HTTPS도 실패했습니다. "
                     f"러너의 outbound HTTPS 전반 문제로 보이며, "
                     f"{TARGET} 고유 문제로 좁힐 수 없습니다.")

    v4_ok = _tcp_ok(target, "IPv4")
    v6_ok = _tcp_ok(target, "IPv6")
    v6_present = bool(target["dns"]["ipv6"])

    if not v4_ok and not (v6_present and v6_ok):
        peer_note = ("같은 기관의 " + PEER + "는 HTTPS 응답을 받았습니다"
                     if _http_ok(peer) else PEER + "도 함께 실패했습니다")
        return "B", (
            f"{TARGET} DNS는 정상 해석됐지만(IPv4 {target['dns']['ipv4']}), "
            f"IPv4 TCP/443 시도가 모두 실패했습니다. "
            f"대조군 {CONTROL} HTTPS는 성공했으므로 러너의 outbound 전반 문제는 아닙니다. "
            f"{peer_note} — 다만 두 호스트는 인프라·라우팅이 다를 수 있어 "
            f"'{TARGET}만 선별 차단'을 증명하지는 않습니다.")

    if v6_present and (v4_ok != v6_ok):
        return "F", (f"{TARGET}에서 IPv4와 IPv6 결과가 갈립니다 "
                     f"(IPv4 {'성공' if v4_ok else '실패'}, "
                     f"IPv6 {'성공' if v6_ok else '실패'}). 주소패밀리 경로 비대칭입니다.")

    if not _tls_ok(target):
        return "D", (f"{TARGET} TCP/443은 연결됐지만 TLS handshake가 실패했습니다. "
                     f"인증서 또는 중간 장비의 TLS 간섭을 의심할 수 있습니다.")

    if endpoint.get("skipped"):
        if _http_ok(target):
            return "G", (f"{TARGET}까지 TLS·HTTP 응답이 모두 정상입니다. "
                         f"시크릿이 없어 실제 endpoint는 확인하지 못했습니다 "
                         f"— 이 시점 이 러너에서는 경로 문제가 관측되지 않았습니다.")
        return "G", "HTTP 응답을 받지 못했지만 계층별 결과가 일관되지 않습니다."

    if not endpoint.get("ok"):
        return "E", (f"{TARGET} TLS·HTTP는 정상인데 실제 endpoint 호출이 실패했습니다 "
                     f"({endpoint.get('error')}). API 계층 문제입니다.")

    return "G", (f"{TARGET}의 DNS·TCP·TLS·HTTP와 실제 endpoint 호출 "
                 f"(HTTP {endpoint.get('status')}, resultCode={endpoint.get('result_code')})이 "
                 f"모두 성공했습니다. 이 실행 시점에는 경로 문제가 재현되지 않았습니다.")


# ── 요약 ─────────────────────────────────────────────────────────

def _cell(entry, family, kind):
    if not entry.get("dns", {}).get("ok"):
        return "n/a"
    if family == "IPv6" and not entry["dns"]["ipv6"]:
        return "AAAA 없음"
    if not _tcp_attempted(entry, family):
        return "n/a"
    if kind == "tcp":
        return "성공" if _tcp_ok(entry, family) else "실패"
    items = entry.get(kind, [])
    for item in items:
        if item.get("family") == family:
            if item.get("skipped"):
                return "skipped"
            return "성공" if item.get("ok") else "실패"
    return "n/a"


def render_summary(report):
    hosts = report["hosts"]
    target = hosts[TARGET]
    endpoint = report["endpoint"]
    lines = [
        "## data.go.kr 네트워크 진단",
        "",
        f"- run: `{report['run_id']}` / attempt `{report['run_attempt']}`",
        f"- runner: `{report['runner_os']} {report['runner_arch']}`",
        f"- UTC: `{report['timestamp']}`",
        "",
        "| Test | IPv4 | IPv6 | Result | Elapsed | Detail |",
        "|---|---|---|---|---|---|",
    ]

    dns = target["dns"]
    lines.append(
        f"| DNS ({TARGET}) | {', '.join(dns['ipv4']) or '없음'} "
        f"| {', '.join(dns['ipv6']) or dns['ipv6_status']} "
        f"| {'성공' if dns['ok'] else '실패'} | {dns['elapsed']}s "
        f"| {dns['error'] or '해석 성공'} |")

    first_tcp = next((i for i in target["tcp"] if i["family"] == "IPv4"), None)
    tcp_detail = "시도 없음"
    tcp_elapsed = "-"
    if first_tcp:
        oks = sum(1 for a in first_tcp["attempts"] if a["ok"])
        tcp_elapsed = f"{sum(a['elapsed'] for a in first_tcp['attempts']):.1f}s"
        tcp_detail = f"{oks}/{len(first_tcp['attempts'])} 성공"
        if not oks:
            tcp_detail += f" — {first_tcp['attempts'][0]['error']}"
    lines.append(
        f"| TCP 443 | {_cell(target,'IPv4','tcp')} | {_cell(target,'IPv6','tcp')} "
        f"| {'성공' if _tcp_ok(target) else '실패'} | {tcp_elapsed} | {tcp_detail} |")

    tls_entry = next((t for t in target["tls"] if t.get("family") == "IPv4"), {})
    lines.append(
        f"| TLS | {_cell(target,'IPv4','tls')} | {_cell(target,'IPv6','tls')} "
        f"| {'성공' if _tls_ok(target) else ('skipped' if tls_entry.get('skipped') else '실패')} "
        f"| {tls_entry.get('elapsed') or '-'} "
        f"| {tls_entry.get('subject') or tls_entry.get('skipped') or tls_entry.get('error') or '-'} |")

    http_entry = next((h for h in target["http"] if h.get("family") == "IPv4"), {})
    lines.append(
        f"| HTTP root | {_cell(target,'IPv4','http')} | {_cell(target,'IPv6','http')} "
        f"| {'성공' if _http_ok(target) else ('skipped' if http_entry.get('skipped') else '실패')} "
        f"| {http_entry.get('elapsed') or '-'} "
        f"| {http_entry.get('status') or http_entry.get('skipped') or http_entry.get('error') or '-'} |")

    if endpoint.get("skipped"):
        ep_result, ep_detail, ep_elapsed = "skipped", endpoint["skipped"], "-"
    elif endpoint.get("ok"):
        ep_result = "성공"
        ep_detail = f"HTTP {endpoint['status']}, resultCode={endpoint['result_code']}"
        ep_elapsed = f"{endpoint['elapsed']}s"
    else:
        ep_result, ep_detail = "실패", str(endpoint.get("error"))
        ep_elapsed = f"{endpoint['elapsed']}s"
    lines.append(f"| Actual API endpoint | - | - | {ep_result} | {ep_elapsed} | {ep_detail} |")

    control = hosts[CONTROL]
    control_http = next((h for h in control["http"] if h.get("family") == "IPv4"), {})
    lines.append(
        f"| Control HTTPS ({CONTROL}) | {_cell(control,'IPv4','http')} "
        f"| {_cell(control,'IPv6','http')} "
        f"| {'성공' if _http_ok(control) else '실패'} "
        f"| {control_http.get('elapsed') or '-'} "
        f"| {control_http.get('status') or control_http.get('error') or '-'} |")

    peer = hosts[PEER]
    peer_http = next((h for h in peer["http"] if h.get("family") == "IPv4"), {})
    lines.append(
        f"| Peer ({PEER}) | {_cell(peer,'IPv4','http')} | {_cell(peer,'IPv6','http')} "
        f"| {'성공' if _http_ok(peer) else '실패'} "
        f"| {peer_http.get('elapsed') or '-'} "
        f"| {peer_http.get('status') or peer_http.get('error') or '-'} |")

    egress = report["egress_ip"]
    if egress["enabled"]:
        lines.append(f"| Egress IPv4 | {egress['ip'] or '-'} | - "
                     f"| {'성공' if egress['ok'] else '실패'} | - "
                     f"| {egress['error'] or '러너 public IPv4'} |")

    lines += ["", f"### 판정: **{report['verdict']}**", "", report["verdict_reason"], "",
              "> 이 진단은 한 시점·한 러너의 표본입니다. TCP 타임아웃은 "
              "의도적 차단과 경로 장애를 구별하지 못합니다."]

    text = mask("\n".join(lines))
    # 파일부터 쓴다. 콘솔 출력이 막혀도 요약은 남아야 한다.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except Exception as exc:
            log(f"(Step Summary 기록 실패: {describe(exc)})")
    log("")
    log(text)
    return text


# ── main ─────────────────────────────────────────────────────────

def main():
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
        "runner_os": os.environ.get("RUNNER_OS", "unknown"),
        "runner_arch": os.environ.get("RUNNER_ARCH", "unknown"),
        "python": sys.version.split()[0],
        "connect_timeout": CONNECT_TIMEOUT,
        "tcp_attempts": TCP_ATTEMPTS,
        "hosts": {},
        "endpoint": {},
        "egress_ip": {},
        "verdict": "G",
        "verdict_reason": "",
    }

    log(f"러너 {report['runner_os']}/{report['runner_arch']} "
        f"python {report['python']} | run {report['run_id']}#{report['run_attempt']}")

    for host, role in ((TARGET, "진단 대상"),
                       (PEER, "같은 기관 포털 — 보조 근거"),
                       (CONTROL, "대조군")):
        try:
            report["hosts"][host] = probe_host(host, role)
        except Exception as exc:
            report["hosts"][host] = {"host": host, "role": role,
                                     "dns": {"ok": False, "error": describe(exc),
                                             "ipv4": [], "ipv6": [], "elapsed": None,
                                             "ipv6_status": "미확인"},
                                     "tcp": [], "tls": [], "http": []}
            log(f"  {host} 검사 중 예외: {describe(exc)}")

    try:
        report["endpoint"] = real_endpoint_probe()
    except Exception as exc:
        report["endpoint"] = {"ok": False, "error": describe(exc)}

    try:
        enabled = str(os.environ.get("CHECK_EGRESS_IP", "")).strip().lower() == "true"
        report["egress_ip"] = egress_ip_probe(enabled)
    except Exception as exc:
        report["egress_ip"] = {"enabled": False, "ok": False, "ip": None,
                               "error": describe(exc)}

    try:
        verdict, reason = decide(report["hosts"], report["endpoint"])
    except Exception as exc:
        verdict, reason = "G", f"판정 중 예외가 발생했습니다: {describe(exc)}"
    report["verdict"] = verdict
    report["verdict_reason"] = reason

    try:
        render_summary(report)
    except Exception as exc:
        log(f"요약 생성 실패: {describe(exc)}")

    try:
        RESULT_PATH.write_text(
            mask(json.dumps(report, ensure_ascii=False, indent=2)), encoding="utf-8")
        log(f"\n결과 저장: {RESULT_PATH.name}")
    except Exception as exc:
        log(f"결과 저장 실패: {describe(exc)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:          # 진단 실패와 연결 실패는 다르다
        log(f"진단 스크립트 예외: {describe(exc)}")
    sys.exit(0)
