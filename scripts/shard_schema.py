import sys

REQUIRED_ITEM_FIELDS = [
    "apartmentKey",
    "aptSeq",
    "apartmentName",
    "sidoCode",
    "sggCode",
    "areaGroup",
    "referenceMonth",
    "baselineMedian",
    "currentMedian",
    "riseAmount",
    "riseRate",
    "baselineTradeCount",
    "currentTradeCount",
    "baselineFloorMedian",
    "currentFloorMedian",
    "confidence",
    "warnings"
]

def normalize_code(code: str, length: int) -> str:
    """코드를 문자열로 변환하고 길이가 부족하면 앞에 0을 채웁니다."""
    if not code:
        return ""
    s = str(code).strip()
    return s.zfill(length)

def validate_item(item: dict, index: int, included_sidos: list[str], path: str):
    # 필수 필드 누락 검사
    missing = [f for f in REQUIRED_ITEM_FIELDS if f not in item]
    if missing:
        print(f"오류: {path} 의 {index}번째 item에 필수 필드가 누락되었습니다: {missing}")
        sys.exit(1)

    apt_key = item["apartmentKey"]
    if not apt_key:
        print(f"오류: {path} 의 {index}번째 item에 apartmentKey가 비어 있습니다.")
        sys.exit(1)

    sido_code = item["sidoCode"]
    sgg_code = item["sggCode"]

    # sidoCode 검사 (정확히 2자리 숫자 문자열)
    if not isinstance(sido_code, str) or len(sido_code) != 2 or not sido_code.isdigit():
        print(f"오류: {path} 의 {index}번째 item(apartmentKey: {apt_key})의 sidoCode({repr(sido_code)})가 올바르지 않은 형식입니다.")
        sys.exit(1)

    # sggCode 검사 (정확히 5자리 숫자 문자열)
    if not isinstance(sgg_code, str) or len(sgg_code) != 5 or not sgg_code.isdigit():
        print(f"오류: {path} 의 {index}번째 item(apartmentKey: {apt_key})의 sggCode({repr(sgg_code)})가 올바르지 않은 형식입니다.")
        sys.exit(1)

    # sggCode가 sidoCode로 시작하는지
    if not sgg_code.startswith(sido_code):
        print(f"오류: {path} 의 {index}번째 item(apartmentKey: {apt_key})의 sggCode({sgg_code})가 sidoCode({sido_code})와 일치하지 않습니다.")
        sys.exit(1)

    # 해당 regionGroup에서 허용되는 sidoCode인지
    if sido_code not in included_sidos:
        print(f"오류: {path} 의 {index}번째 item(apartmentKey: {apt_key})이 포함되지 않은 sidoCode({sido_code})를 가지고 있습니다. 허용된 코드: {included_sidos}")
        sys.exit(1)

    # 기타 필수 필드 존재 확인 (빈 값도 안 됨)
    if not item.get("areaGroup"):
        print(f"오류: {path} 의 {index}번째 item(apartmentKey: {apt_key})에 areaGroup이 없습니다.")
        sys.exit(1)
    if not item.get("referenceMonth"):
        print(f"오류: {path} 의 {index}번째 item(apartmentKey: {apt_key})에 referenceMonth가 없습니다.")
        sys.exit(1)

    # validate_shard.py에서 snake_case 필드가 있으면 경고 또는 실패 처리 (옵션)
    # user instruction: "validate_shard.py가 snake_case와 camelCase를 모두 조용히 허용하게 하지 마세요. 공식 camelCase 스키마만 사용하고 잘못된 생산 결과는 생성 단계에서 바로 실패시켜 주세요."
    # We will enforce ONLY camelCase, meaning any snake_case in required fields won't match REQUIRED_ITEM_FIELDS anyway, but let's make sure no extra snake_case exists.
    import math
    for field in ["baselineFloorMedian", "currentFloorMedian"]:
        val = item.get(field)
        if val is not None:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                print(f"오류: {path} 의 {index}번째 item(apartmentKey: {apt_key})의 {field} 값이 유효한 숫자가 아닙니다: {val}")
                sys.exit(1)
            if math.isnan(val) or math.isinf(val):
                print(f"오류: {path} 의 {index}번째 item(apartmentKey: {apt_key})의 {field} 값이 NaN 또는 Infinity입니다: {val}")
                sys.exit(1)

    snake_case_fields = [k for k in item.keys() if "_" in k]
    if snake_case_fields:
        print(f"오류: {path} 의 {index}번째 item(apartmentKey: {apt_key})에 허용되지 않는 snake_case 필드가 있습니다: {snake_case_fields}")
        sys.exit(1)
