import sqlite3

conn = sqlite3.connect('data/apt.sqlite')
conn.row_factory = sqlite3.Row

trades = conn.execute("""
    SELECT deal_date, apt_seq, exclusive_area, area_group, floor, deal_amount, dealing_type, is_cancelled, sgg_code, umd_name, jibun
    FROM apartment_trades
    WHERE apartment_key = '11680-5235' AND area_group = 85
      AND deal_month IN ('202502', '202503', '202504', '202505')
    ORDER BY deal_date ASC
""").fetchall()

print(f"{'계약일':<12} | {'단지식별번호':<12} | {'전용면적':<8} | {'그룹':<4} | {'층':<4} | {'거래금액':<12} | {'거래유형':<6} | {'해제':<4} | {'법정동_지번'}")
print("-" * 110)
for t in trades:
    print(f"{t['deal_date']:<12} | {t['apt_seq']:<12} | {t['exclusive_area']:<8.4f} | {t['area_group']:<4} | {str(t['floor']):<4} | {t['deal_amount']:<12} | {t['dealing_type']:<6} | {t['is_cancelled']:<4} | {t['umd_name']} {t['jibun']}")
