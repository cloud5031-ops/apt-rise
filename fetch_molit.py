import os
import requests
from urllib.parse import unquote

with open('.env', 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#'):
            parts = line.split('=', 1)
            if len(parts) == 2:
                os.environ[parts[0]] = parts[1].strip()

import sys
sys.path.append('scripts')
import config

LAWD_CD = "11680"
DEAL_YMD = "202605"
key = config.DATA_GO_KR_API_KEY
decoded_key = unquote(key)

os.makedirs('data/raw', exist_ok=True)
url = config.MOLIT_ENDPOINT
params = {
    "serviceKey": decoded_key,
    "LAWD_CD": LAWD_CD,
    "DEAL_YMD": DEAL_YMD,
    "numOfRows": "1000",
    "pageNo": "1",
}
resp = requests.get(url, params=params)
with open(f"data/raw/molit_{LAWD_CD}_{DEAL_YMD}.xml", "w", encoding="utf-8") as f:
    f.write(resp.text)

print(f"Saved XML. Length: {len(resp.text)}")
