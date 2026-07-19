import json
import glob
import sys
import os

from shard_schema import validate_item

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

    items = data.get("items", [])
    
    for i, item in enumerate(items):
        validate_item(item, i, included_sidos, path)

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
