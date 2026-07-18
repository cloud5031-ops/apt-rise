import sqlite3
conn = sqlite3.connect('data/apt.sqlite')
conn.row_factory = sqlite3.Row

# Get overall stats
total = conn.execute("SELECT count(*) FROM apartment_monthly_metrics WHERE reference_month='202505' AND current_trade_count>=2 AND baseline_trade_count>=2 AND rise_amount>=30000000 AND rise_rate>=3.0").fetchone()[0]

over20 = conn.execute("SELECT count(*) FROM apartment_monthly_metrics WHERE reference_month='202505' AND current_trade_count>=2 AND baseline_trade_count>=2 AND rise_amount>=30000000 AND rise_rate>=20.0").fetchone()[0]

print(f"Total results: {total} (previously 387)")
print(f"Results >= 20%: {over20}")

# Get Gaepo info
rows = conn.execute("SELECT area_group, current_trade_count, baseline_trade_count, rise_rate, confidence FROM apartment_monthly_metrics WHERE apartment_key='11680-5235'").fetchall()
print("\n[Gaepo]")
for r in rows:
    print(dict(r))
