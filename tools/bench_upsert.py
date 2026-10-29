"""Time the daily upsert path without spending vendor units.

Re-writes an existing session's rows through the same MERGE, so the cost is the write path
only. This is the number that decides how long the daily job holds a write lock, and
therefore how long a reader can be blocked behind it.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import config
from data_service.store import Store

settings = config.load()
store = Store(settings.connection_string)

conn = store.connect()
cur = conn.cursor()
cur.execute("SELECT MAX(SnapshotDate) FROM dbo.DailySnapshot")
session = cur.fetchone()[0]
cur.execute(
    "SELECT SymbolCode, SnapshotDate, [Open], [High], [Low], [Close], Volume, Source, "
    "IsAdjusted FROM dbo.DailySnapshot WHERE SnapshotDate = ?", session)
rows = [
    {
        "symbol": r[0], "session": r[1], "open": float(r[2]), "high": float(r[3]),
        "low": float(r[4]), "close": float(r[5]), "volume": int(r[6]),
        "source": r[7], "adjusted": bool(r[8]),
    }
    for r in cur.fetchall()
]
conn.close()

print(f"session {session}: {len(rows):,} rows read back")

for label, count in (("10k rows", 10000), ("full session", len(rows))):
    sample = rows[:count]
    t0 = time.perf_counter()
    written = store.upsert_daily(sample)
    elapsed = time.perf_counter() - t0
    print(f"  {label:<14} {written:>7,} rows  {elapsed:6.2f}s  "
          f"({written / elapsed:,.0f} rows/s)")

# Prove a read is not blocked immediately afterwards.
t0 = time.perf_counter()
bars = store.get_daily_bars_many(["AAPL", "MSFT", "NVDA"], 51)
elapsed = (time.perf_counter() - t0) * 1000
print(f"\n  read after write: {elapsed:.0f} ms, "
      f"{ {k: len(v) for k, v in bars.items()} }")
print(f"  total daily rows: {store.stats()['daily_rows']:,}")
