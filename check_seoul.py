import sqlite3
conn = sqlite3.connect('data/apt.sqlite')
codes = [r[0] for r in conn.execute("SELECT sgg_code FROM region_codes WHERE sido_name = '서울특별시'").fetchall()]
print(codes)
