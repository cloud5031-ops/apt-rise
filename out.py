import sqlite3
conn=sqlite3.connect('data/apt.sqlite')
conn.row_factory=sqlite3.Row
rows=conn.execute("SELECT sgg_code, umd_name, apt_name, area_group, rise_rate FROM apartment_monthly_metrics WHERE reference_month='202505' AND current_trade_count>=2 AND baseline_trade_count>=2 AND rise_amount>=30000000 AND rise_rate>=3.0 ORDER BY rise_rate DESC LIMIT 20").fetchall()
with open('out.txt', 'w', encoding='utf-8') as f:
    for i, r in enumerate(rows):
        f.write(f"{i+1}. {r['sgg_code']} {r['umd_name']} {r['apt_name']} {r['area_group']}㎡: {r['rise_rate']}%\n")
