import sqlite3
conn = sqlite3.connect('data/apt.sqlite')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT deal_month, deal_amount, is_cancelled FROM apartment_trades WHERE apartment_key='11680-5235' AND area_group=85").fetchall()
for r in rows:
    print(dict(r))
