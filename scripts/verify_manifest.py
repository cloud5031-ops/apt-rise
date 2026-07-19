import json
import os
import sys
import re
from datetime import datetime
import zoneinfo

from utils import shift_month

def set_github_output(name, value):
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"::set-output name={name}::{value}")

def main():
    manifest_path = "site/data/apt_rankings_manifest.json"
    if not os.path.exists(manifest_path):
        sys.exit(f"오류: {manifest_path} 파일이 존재하지 않습니다.")

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        sys.exit(f"오류: manifest JSON 파싱 실패 ({e})")

    stable_month = manifest.get("stableMonth")
    prov_month = manifest.get("provisionalMonth")

    if not stable_month or not prov_month:
        sys.exit("오류: stableMonth 또는 provisionalMonth 누락")

    if not re.match(r"^\d{6}$", stable_month) or not re.match(r"^\d{6}$", prov_month):
        sys.exit("오류: 월 형식이 YYYYMM이 아님")

    expected_prov = shift_month(stable_month, 1)
    if prov_month != expected_prov:
        sys.exit(f"오류: provisionalMonth({prov_month})가 stableMonth({stable_month})의 정확히 다음 달이 아님")

    stable_file = manifest.get("stableFile")
    prov_file = manifest.get("provisionalFile")
    if not stable_file or not prov_file:
        sys.exit("오류: stableFile 또는 provisionalFile 누락")

    now = datetime.now(zoneinfo.ZoneInfo("Asia/Seoul"))
    current_month = f"{now.year:04d}{now.month:02d}"
    if stable_month >= current_month or prov_month >= current_month:
        sys.exit("오류: 기준월이 미래이거나 현재 월과 같습니다.")

    # 파일 존재 및 referenceMonth 일치 검증
    for month, filename in [(stable_month, stable_file), (prov_month, prov_file)]:
        filepath = os.path.join("site/data", filename)
        if not os.path.exists(filepath):
            sys.exit(f"오류: 참조된 JSON 파일({filepath})이 존재하지 않음")
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("referenceMonth") != month:
                sys.exit(f"오류: {filename}의 referenceMonth({data.get('referenceMonth')})가 {month}와 다름")

    print(f"Manifest 검증 성공: stable={stable_month}, provisional={prov_month}")
    set_github_output("stable_month", stable_month)
    set_github_output("provisional_month", prov_month)

if __name__ == "__main__":
    main()
