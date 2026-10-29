"""Remove daily rows loaded after a given session, and clear the indicator cache.

Used to undo a bad load rather than rebuilding the table: DailySnapshot is append-only by
session, so deleting the sessions after a known-good date is surgical.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import config
from data_service.store import Store

cutoff = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 10, 14)

settings = config.load()
store = Store(settings.connection_string)

conn = store.connect()
cur = conn.cursor()
cur.execute("DELETE FROM dbo.DailySnapshot WHERE SnapshotDate > ?", cutoff)
print(f"deleted daily rows after {cutoff}: {cur.rowcount}")
cur.execute("DELETE FROM dbo.IndicatorCache")
print(f"cleared IndicatorCache: {cur.rowcount}")
conn.commit()
conn.close()

print("stats:", store.stats())
for row in store.session_symbol_counts(8):
    print("  ", row)
