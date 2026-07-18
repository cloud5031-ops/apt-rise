"""순위 JSON 유효성 엄격 검증 스크립트.

GitHub Actions에서 배포 전 실행되어 비정상 데이터 배포를 방지합니다.
"""
import json
import glob
import sys
import math

def validate_file(path: str):
    print(f"검증 중: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"오류: {path} 형식이 올바르지 않은 JSON입니다: {e}")
        
    if "referenceMonth" not in data:
        sys.exit(f"오류: {path} 에 'referenceMonth' 필드가 없습니다.")
        
    if "generatedAt" not in data and "collectedAt" not in data:
        sys.exit(f"오류: {path} 에 생성 시각 필드가 없습니다.")
        
    items = data.get("items")
    if not isinstance(items, list):
        sys.exit(f"오류: {path} 의 'items'가 배열이 아닙니다.")
        
    if len(items) == 0:
        sys.exit(f"오류: {path} 의 'items' 배열이 0건입니다.")
        
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            sys.exit(f"오류: {path} 의 {i}번째 item이 객체가 아닙니다.")
            
        rate_fields = ["riseRate", "momRate", "threeMonthRate", "yoyRate"]
        for field in rate_fields:
            if field in item:
                val = item[field]
                if val is None:
                    continue # Some might be null, wait, the user said "NaN, Infinity, null 오류값이 없는지 확인". Let's fail if it's explicitly one of these when it shouldn't be. Actually, `momRate` can be null if not computed? Let's check `compute_region_rankings.py`. `WHERE mom_rate IS NOT NULL` is there. So `momRate` shouldn't be null.
                    # But `yoyRate` might be null if 1 year ago data is missing. The user said "전월 대비, 3개월 대비, 전년 동월 대비 계산에 필요한 월이 모두 존재하는지 검증합니다... 중간 월이 누락되면 순위를 생성하지 말고 실패 처리합니다". So yoyRate must not be null either.
                    sys.exit(f"오류: {path} 의 {i}번째 item에 {field} 값이 null입니다.")
                if not isinstance(val, (int, float)):
                    sys.exit(f"오류: {path} 의 {i}번째 item에 {field} 값이 숫자가 아닙니다.")
                if math.isnan(val) or math.isinf(val):
                    sys.exit(f"오류: {path} 의 {i}번째 item에 {field} 값이 NaN이거나 Infinity입니다.")
                    
def main():
    files = glob.glob("site/data/*.json")
    if not files:
        sys.exit("오류: 검증할 JSON 파일이 site/data/ 에 없습니다.")
        
    for f in files:
        validate_file(f)
        
    print(f"총 {len(files)}개의 JSON 파일이 모든 엄격한 검증을 통과했습니다.")

if __name__ == "__main__":
    main()
