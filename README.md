# 아파트 가격 상승률 랭킹

설계안(API 설계 문서) 기반 구현 뼈대. 경제지표 대시보드와 같은 구조로 만들었다:
**Python 수집 스크립트 + GitHub Actions 자동화 + Cloudflare Pages 정적 호스팅**.
서버·DB 호스팅 비용 없이 SQLite 파일과 JSON으로 굴러간다.

## 폴더 구조

```
apt-rise/
├── scripts/
│   ├── config.py                  # API 키·통계코드·필터 기준 (전부 환경변수화)
│   ├── utils.py                   # 중위값, 면적그룹, 단지키, 신뢰도, 월 계산
│   ├── db.py                      # SQLite 스키마 (설계안 10장 이식)
│   ├── collect_region_codes.py    # ① 행안부 법정동코드 → 시군구 목록
│   ├── collect_price_index.py     # ② R-ONE 가격지수 수집 + 상승률 계산
│   ├── collect_trades.py          # ③ 국토부 실거래가 수집 (해제 반영 upsert)
│   ├── compute_region_rankings.py # ④ 시군구 순위 JSON
│   └── compute_apt_rankings.py    # ⑤ 단지 순위 JSON (중위가격·필터·신뢰도)
├── data/
│   ├── regions.json               # 시군구 코드 목록 (①이 생성)
│   └── apt.sqlite                 # 지수·거래·통계 저장소
├── site/                          # Cloudflare Pages 배포 루트
│   ├── index.html                 # 두 탭 랭킹 화면
│   └── data/*.json                # ④⑤가 생성하는 정적 데이터
└── .github/workflows/
    ├── daily-trades.yml           # 매일: 최근 3개월 실거래 재수집 (신고지연·해제 반영)
    └── monthly-index.yml          # 매월 16·20일: R-ONE 지수 수집
```

## 처음 한 번 실행 순서

```bash
pip install -r requirements.txt
cp .env.example .env               # 키 입력 후
set -a; source .env; set +a

python scripts/collect_region_codes.py          # 시군구 목록
python scripts/collect_price_index.py 202501 202607   # 지수 13개월+ 백필 (전년비 계산용)
python scripts/collect_trades.py --months 202604 202605 202606 202607
python scripts/compute_region_rankings.py
python scripts/compute_apt_rankings.py
```

로컬 확인: `cd site && python -m http.server` → http://localhost:8000

전국 백필이 부담되면 서울·경기부터 시작해도 된다:
`python scripts/collect_trades.py --sgg 11680 11710 41135 ...`

## 자동화 배포

1. GitHub repo 생성 후 push
2. repo **Settings → Secrets**에 `RONE_API_KEY`, `DATA_GO_KR_API_KEY` 등록
3. **Settings → Actions → Workflow permissions → Read and write** 허용 (커밋 푸시용)
4. Cloudflare Pages 연결, 빌드 없음, output directory = `site`
5. 워크플로가 매일 데이터를 커밋하면 Pages가 자동 재배포

## 설계 원칙 (설계안 요약)

- 시군구 상승률 = 부동산원 **가격지수**, 단지 상승률 = 국토부 **실거래가**. 역할 분리.
- 단지 비교 단위 = 단지키 + 전용면적 반올림 그룹. 상승률 = 이번 달 중위가 ÷ 직전 3개월 중위가.
- 해제 거래 제외, 거래량 필터(각 2건↑, 상승액 3천만↑, 상승률 3%↑), 신뢰도 3단계.
- 최신 월은 항상 "잠정 집계" 표시. 매일 최근 3개월을 재수집해 신고 지연·해제를 따라잡는다.

## 확인 필요 사항 (개발 시작 시)

- [ ] R-ONE 통계코드 검색에서 `A_2024_00045` 유효 여부와 항목·분류 코드 재확인 → `.env`의 `RONE_STATBL_ID`만 바꾸면 됨
- [ ] R-ONE 응답의 지역 필드명(`CLS_ID`/`CLS_FULLNM`)이 실제 응답과 맞는지 첫 호출에서 확인
- [ ] 국토부 XML 태그명(`aptNm`, `excluUseAr`, `cdealType` 등)이 실제 응답과 맞는지 확인 — `collect_trades.py`의 `text()` 호출부만 고치면 됨
- [ ] R-ONE 지역코드는 법정동코드와 체계가 달라, 지도 연동 시 이름 기준 매핑 테이블이 별도로 필요 (2단계 과제)

## 단계별 로드맵

1. **1단계 (지금 뼈대)**: 수집→계산→정적 JSON→테이블 화면
2. **2단계**: 시군구 GeoJSON 지도 색상 표시, 단지 상세 페이지(직전 거래 대비 보조지표)
3. **3단계**: 이상치 IQR 플래깅(`is_outlier` 컬럼은 이미 있음), 층 그룹 구분, KOSIS 대체 소스
4. **4단계**: SQLite → PostgreSQL 이전 + 외부 공개 API (설계안 13장 스펙 그대로)
