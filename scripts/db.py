"""SQLite 초기화 및 커넥션 (설계안 10장의 PostgreSQL 스키마를 SQLite로 이식).

바이브코딩 1단계에서는 서버 없이 SQLite 파일 하나로 시작하고,
트래픽이 늘면 동일 스키마로 PostgreSQL(Supabase/Neon 등)로 이전한다.
"""
import sqlite3

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS region_codes (
  sgg_code     TEXT PRIMARY KEY,          -- 법정동코드 앞 5자리
  sido_name    TEXT NOT NULL,
  sigungu_name TEXT NOT NULL,
  full_name    TEXT NOT NULL,
  is_active    INTEGER NOT NULL DEFAULT 1,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS region_price_indices (
  region_code     TEXT NOT NULL,          -- R-ONE 분류코드(CLS_ID)
  region_name     TEXT NOT NULL,
  reference_month TEXT NOT NULL,          -- YYYYMM
  price_index     REAL NOT NULL,
  mom_rate        REAL,
  three_month_rate REAL,
  yoy_rate        REAL,
  source          TEXT NOT NULL DEFAULT 'RONE',
  collected_at    TEXT NOT NULL,
  PRIMARY KEY (region_code, reference_month)
);

CREATE TABLE IF NOT EXISTS apartment_trades (
  source_trade_key TEXT PRIMARY KEY,      -- 중복 방지용 결합키
  apt_seq          TEXT,
  apartment_key    TEXT NOT NULL,
  sgg_code         TEXT NOT NULL,
  umd_name         TEXT,
  jibun            TEXT,
  apt_name         TEXT NOT NULL,
  apt_dong         TEXT,
  exclusive_area   REAL NOT NULL,
  area_group       INTEGER NOT NULL,
  deal_amount      INTEGER NOT NULL,      -- 원 단위 정수
  deal_date        TEXT NOT NULL,         -- YYYY-MM-DD
  deal_month       TEXT NOT NULL,         -- YYYYMM (조회 최적화)
  floor            INTEGER,
  build_year       INTEGER,
  is_cancelled     INTEGER NOT NULL DEFAULT 0,
  cancel_date      TEXT,
  dealing_type     TEXT,
  registration_date TEXT,
  is_outlier       INTEGER NOT NULL DEFAULT 0,  -- 원본 보관, 통계 제외 플래그(설계안 8-4)
  collected_at     TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_group
  ON apartment_trades (apartment_key, area_group, deal_month);
CREATE INDEX IF NOT EXISTS idx_trades_month
  ON apartment_trades (deal_month);

CREATE TABLE IF NOT EXISTS apartment_monthly_metrics (
  apartment_key   TEXT NOT NULL,
  reference_month TEXT NOT NULL,
  area_group      REAL NOT NULL,
  apt_name        TEXT NOT NULL,
  sgg_code        TEXT NOT NULL,
  umd_name        TEXT,
  current_median_price  INTEGER,
  baseline_median_price INTEGER,
  current_trade_count   INTEGER NOT NULL,
  baseline_trade_count  INTEGER NOT NULL,
  rise_amount     INTEGER,
  rise_rate       REAL,
  confidence      TEXT,
  calculated_at   TEXT NOT NULL,
  PRIMARY KEY (apartment_key, reference_month, area_group)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


if __name__ == "__main__":
    connect().close()
    print(f"DB 초기화 완료: {config.DB_PATH}")
