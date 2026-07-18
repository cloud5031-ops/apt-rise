import sqlite3
import json
conn = sqlite3.connect('data/apt.sqlite')
conn.row_factory = sqlite3.Row

# 상위 20위 아파트와 >= 30% 이상인 것들 조회
rows = conn.execute("""
    SELECT apartment_key, apt_name, umd_name, area_group, current_median_price, baseline_median_price, rise_amount, rise_rate, current_trade_count, baseline_trade_count
    FROM apartment_monthly_metrics 
    WHERE reference_month = '202505'
      AND current_trade_count >= 2 AND baseline_trade_count >= 2
      AND rise_amount >= 30000000 AND rise_rate >= 3.0
    ORDER BY rise_rate DESC
""").fetchall()

print(f"Total valid apartments in ranking criteria: {len(rows)}")
for i, r in enumerate(rows[:20]):
    print(f"{i+1}위: {r['umd_name']} {r['apt_name']} {r['area_group']}㎡ | 상승률 {r['rise_rate']}% | 상승액 {r['rise_amount']}원 ({r['baseline_median_price']} -> {r['current_median_price']}) | 건수 {r['baseline_trade_count']}->{r['current_trade_count']}")

print("--- 30% 이상 결과 상세 ---")
outliers = [r for r in rows if r['rise_rate'] >= 30.0]
for r in outliers:
    print(f"\n[{r['apt_name']} {r['area_group']}㎡]")
    print(f"계산 결과: 상승률 {r['rise_rate']}% (기준가 {r['baseline_median_price']} -> 현재가 {r['current_median_price']})")
    
    # 해당 아파트의 실제 거래 내역 조회
    trades = conn.execute("""
        SELECT deal_month, deal_amount, is_cancelled, cancel_date 
        FROM apartment_trades 
        WHERE apartment_key = ? AND area_group = ? 
          AND deal_month IN ('202502', '202503', '202504', '202505')
          AND is_outlier = 0 AND is_cancelled = 0
        ORDER BY deal_month ASC
    """, (r['apartment_key'], r['area_group'])).fetchall()
    
    print("사용된 거래 내역:")
    for t in trades:
        print(f"  - {t['deal_month']}: {t['deal_amount']}원 (취소: {t['is_cancelled']})")
        
    cancelled_trades = conn.execute("""
        SELECT deal_month, deal_amount, is_cancelled, cancel_date 
        FROM apartment_trades 
        WHERE apartment_key = ? AND area_group = ? 
          AND deal_month IN ('202502', '202503', '202504', '202505')
          AND is_cancelled = 1
        ORDER BY deal_month ASC
    """, (r['apartment_key'], r['area_group'])).fetchall()
    if cancelled_trades:
        print("해제된 거래 (제외됨):")
        for t in cancelled_trades:
            print(f"  - {t['deal_month']}: {t['deal_amount']}원 (취소일: {t['cancel_date']})")
