import json
import glob
import sys
import os

def validate_shard(path: str):
    print(f"Shard 검증 중: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"오류: {path} 형식이 올바르지 않은 JSON입니다: {e}")

    included_sidos = data.get("includedSidoCodes", [])
    if not included_sidos:
        sys.exit(f"오류: {path} 에 'includedSidoCodes' 메타데이터가 없거나 비어 있습니다.")

    region_group = data.get("regionGroup")
    items = data.get("items", [])
    
    for i, item in enumerate(items):
        sido_code = item.get("sidoCode")
        sgg_code = item.get("sggCode")
        apt_key = item.get("apartmentKey")
        
        if not sido_code or not sgg_code or not apt_key:
            sys.exit(f"오류: {path} 의 {i}번째 item에 필수 코드(sidoCode, sggCode, apartmentKey)가 누락되었습니다.")
            
        # sidoCode 검증
        if sido_code not in included_sidos:
            print(f"오류: {path} 의 item(apartmentKey: {apt_key})이 포함되지 않은 sidoCode({sido_code})를 가지고 있습니다. 허용된 코드: {included_sidos}")
            sys.exit(1)
            
        # sggCode 앞 2자리 검증
        if not sgg_code.startswith(sido_code):
            print(f"오류: {path} 의 item(apartmentKey: {apt_key})의 sggCode({sgg_code})가 sidoCode({sido_code})와 일치하지 않습니다.")
            sys.exit(1)
            
        # apartmentKey 앞부분 검증 (apartmentKey가 {sggCode}-... 형식이어야 함)
        if not apt_key.startswith(sgg_code):
            print(f"오류: {path} 의 item(apartmentKey: {apt_key})이 sggCode({sgg_code})로 시작하지 않습니다.")
            sys.exit(1)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-dir", help="검증할 shard 파일이 있는 최상위 디렉터리 경로")
    args = parser.parse_args()
    
    search_path = args.shard_dir if args.shard_dir else "data/shards"
    files = glob.glob(f"{search_path}/**/*.json", recursive=True)
    
    if not files:
        print(f"경고: 검증할 Shard JSON 파일이 {search_path} 에 없습니다.")
        return
        
    for f in files:
        validate_shard(f)
        
    print(f"총 {len(files)}개의 Shard 파일이 item 수준 엄격 검증을 통과했습니다.")

if __name__ == "__main__":
    main()
