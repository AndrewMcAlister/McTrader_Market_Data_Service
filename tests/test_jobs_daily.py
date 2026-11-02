"""The daily job's warehouse statements, without a database.

``fill_raw_close_from_bars`` is the one path that writes ``RawClose`` without spending a
vendor unit, so what it selects, what it refuses to overwrite and how it batches are all
worth pinning. A fake connection records the statements; nothing here executes SQL.

The claim the SQL rests on was measured against the vendor, not asserted: the last traded
``Bar5Min`` bar of a session equals ``/eod/``'s ``close`` for AAPL and NVDA across fifteen
sessions each, to the last decimal. ``BarHourly`` cannot stand in for it -- its ``Close`` is
the split-adjusted series that ``DailySnapshot.Close`` already holds.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.daily import (
    RAW_CLOSE_CANDIDATES,
    RAW_CLOSE_FROM_BARS,
    fill_raw_close_from_bars,
)


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rowcount = 0
        self._rows = []

    def execute(self, sql: str, *params):
        self.connection.executed.append((sql, params))
        if "OBJECT_ID" in sql:
            self._rows = [(self.connection.bar5min_object_id,)]
        elif "SELECT DISTINCT SymbolCode" in sql:
            self._rows = [(symbol,) for symbol in self.connection.symbols]
        else:
            self.rowcount = self.connection.updated_per_statement
            self._rows = []
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, symbols=(), bar5min_object_id=42,
                 updated_per_statement=3) -> None:
        self.executed: list = []
        self.symbols = list(symbols)
        self.bar5min_object_id = bar5min_object_id
        self.updated_per_statement = updated_per_statement
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        pass


class FakeStore:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeConnection:
        return self.connection


def _fill(connection: FakeConnection, **kwargs):
    return fill_raw_close_from_bars(FakeStore(connection), **kwargs)


def _updates(connection: FakeConnection):
    return [(sql, params) for sql, params in connection.executed
            if "UPDATE d" in sql]


def test_the_fill_reads_the_symbols_still_missing_a_raw_close():
    """Driven from DailySnapshot, not the 81M-row tape: the symbol list has to be cheap."""
    connection = FakeConnection(symbols=["AAPL"])

    _fill(connection, days=10, market="US")

    sql, params = connection.executed[1]
    assert sql == RAW_CLOSE_CANDIDATES
    assert "RawClose IS NULL" in sql
    assert params[0] == "US"
    assert params[1] <= params[2]                      # since, until
    assert params[2] - params[1] == timedelta(days=10)


def test_the_fill_takes_the_last_traded_bar_of_the_session():
    connection = FakeConnection(symbols=["AAPL"])

    _fill(connection)

    sql, _ = _updates(connection)[0]
    assert "dbo.Bar5Min" in sql
    assert "ORDER BY x.BarTimeUtc DESC" in sql
    assert "TOP (1)" in sql
    # A halt bar can print a zero; it must not become the close.
    assert "x.[Close] > 0" in sql


def test_the_fill_never_overwrites_a_known_raw_close():
    """The vendor's value wins, and re-running the fill is a no-op."""
    connection = FakeConnection(symbols=["AAPL"])

    _fill(connection)

    sql, _ = _updates(connection)[0]
    assert "d.RawClose IS NULL" in sql


def test_the_fill_batches_by_chunk_and_commits_each_batch():
    connection = FakeConnection(symbols=["A", "B", "C"])

    _fill(connection, chunk=2)

    updates = _updates(connection)
    assert len(updates) == 2
    assert updates[0][1][-2:] == ("A", "B")
    assert updates[1][1][-1:] == ("C",)
    assert connection.commits == 2


def test_the_fill_reports_what_it_wrote():
    connection = FakeConnection(symbols=["A", "B"], updated_per_statement=4)

    result = _fill(connection, chunk=1)

    assert result["updated"] == 8
    assert result["symbols"] == 2
    assert result["until"] >= result["since"]


def test_a_database_without_the_warehouse_tape_is_skipped_not_crashed():
    """Bar5Min is an estate table this repo does not create; a fresh database has none."""
    connection = FakeConnection(symbols=["AAPL"], bar5min_object_id=None)

    result = _fill(connection)

    assert result == {"updated": 0, "symbols": 0, "since": result["since"],
                      "skipped": "no-5min-tape"}
    assert _updates(connection) == []


def test_the_fill_as_sql_claims_no_vendor_units():
    """It is a warehouse read and a local update; the tariff must never see it."""
    assert "eod" not in RAW_CLOSE_FROM_BARS.lower()
