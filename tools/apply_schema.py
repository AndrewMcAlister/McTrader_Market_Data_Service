"""Apply the schema and report what landed. Run from the project root."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import config
from data_service.store import Store

settings = config.load()
print(f"database : {settings.db_database} @ {settings.db_server}")
print(f"conn     : {settings.connection_string}")
print(f"key      : {'present len=' + str(len(settings.eodhd_api_key)) if settings.eodhd_api_key else 'ABSENT'}")
print()

store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)
applied = store.ensure_schema()
print(f"applied {applied} batch(es)")
print()
print("stats:", store.stats())
print()
print("sessions in DailySnapshot:", store.session_symbol_counts(5))

# Prove the doc rows landed.
import pyodbc
conn = pyodbc.connect(settings.connection_string, timeout=30)
cur = conn.cursor()
cur.execute("""
    SELECT TableName FROM dbo.TableDoc
    WHERE TableName IN ('SymbolPriceCache','DailySnapshot','IndicatorCache')
    ORDER BY TableName
""")
print("TableDoc rows:", [r[0] for r in cur.fetchall()])
cur.execute("""
    SELECT TableName, ColumnName FROM dbo.ColumnDoc
    WHERE TableName IN ('SymbolPriceCache','DailySnapshot','IndicatorCache')
    ORDER BY TableName, ColumnName
""")
print("ColumnDoc rows:", [f"{a}.{b}" for a, b in cur.fetchall()])
conn.close()
