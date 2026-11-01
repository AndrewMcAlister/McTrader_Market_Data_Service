"""The store's statements: column lists, VALUES clauses and bound parameters.

Database-free. A fake connection records what would have been sent, so a MERGE whose column
list and VALUES clause disagree is caught here instead of in production -- that mismatch is a
compile error at run time, and nothing else in the suite executes SQL.

It is not hypothetical. The first version of the exchange change added ``Exchange`` to the
INSERT column list of both MERGEs and left it out of the VALUES clause, so the daily load and
the ATR rebuild would each have failed on their first write, and the AU pull would have been
filed as US. These tests are the reason that class of mistake cannot come back quietly.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.store import (
    _CREATE_STAGE_DAILY,
    _MERGE_DAILY_FROM_STAGE,
    _MERGE_INDICATOR,
    Store,
)


# ---------------------------------------------------------------------------
# a connection that records rather than executes
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    def execute(self, sql: str, *params) -> None:
        self.connection.executed.append((sql, params))

    def executemany(self, sql: str, payload) -> None:
        rows = [tuple(row) for row in payload]
        self.connection.executed.append((sql, rows))

    def fetchall(self):
        return self.connection.rows

    def fetchone(self):
        return self.connection.rows[0] if self.connection.rows else None

    def nextset(self) -> bool:
        return False

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, rows=()) -> None:
        self.executed: list = []
        self.rows = list(rows)
        self.committed = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def store_against(connection: FakeConnection) -> Store:
    store = Store("DRIVER={fake}")
    store.connect = lambda: connection  # type: ignore[method-assign]
    return store


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

def insert_shape(sql: str):
    """(column names, VALUES expressions) from an INSERT ... VALUES statement."""
    match = re.search(
        r"INSERT\s*(?:INTO\s+[#\w.]+\s*)?\((.*?)\)\s*VALUES\s*\((.*?)\)\s*;",
        sql, re.IGNORECASE | re.DOTALL)
    assert match, f"no INSERT ... VALUES statement found:\n{sql}"
    columns = [part.strip().strip("[]") for part in match.group(1).split(",")]
    values = [part.strip() for part in match.group(2).split(",")]
    return columns, values


def create_table_columns(sql: str):
    """Column names from a single CREATE TABLE statement."""
    body = sql.split("(", 1)[1].rsplit(")", 1)[0]
    columns = []
    for line in body.splitlines():
        text = line.strip().rstrip(",")
        if text:
            columns.append(text.split()[0].strip("[]"))
    return columns


def row_for_daily() -> dict:
    return {
        "symbol": "CBA", "exchange": "AU", "session": "2026-10-30",
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "volume": 1000, "source": "eod-bulk", "adjusted": True,
    }


# ---------------------------------------------------------------------------
# the daily MERGE: a column list is a promise about the VALUES clause
# ---------------------------------------------------------------------------

def test_the_daily_merge_lists_as_many_values_as_columns():
    columns, values = insert_shape(_MERGE_DAILY_FROM_STAGE)
    assert len(columns) == len(values), (
        f"{len(columns)} columns but {len(values)} values -- SQL Server refuses this at "
        f"compile time, so the daily load would fail on its first write")


def test_the_daily_merge_carries_the_exchange_both_ways():
    """The key is (symbol, exchange, date); a MERGE that writes one but not the other is
    either a compile error or a silent mislabel."""
    columns, values = insert_shape(_MERGE_DAILY_FROM_STAGE)
    assert "Exchange" in columns
    assert "source.Exchange" in values
    assert "target.Exchange = source.Exchange" in _MERGE_DAILY_FROM_STAGE


def test_the_stage_table_has_every_column_the_merge_inserts():
    stage = create_table_columns(_CREATE_STAGE_DAILY)
    columns, _ = insert_shape(_MERGE_DAILY_FROM_STAGE)
    missing = [name for name in columns if name not in stage]
    assert not missing, f"#stage has no {missing}; the MERGE would read a column that is not there"


def test_upsert_daily_binds_one_parameter_per_stage_column():
    connection = FakeConnection()
    store = store_against(connection)

    assert store.upsert_daily([row_for_daily()]) == 1

    insert_sql, payload = connection.executed[1]
    assert insert_sql.count("?") == len(payload[0]) == 10
    assert payload[0][0:2] == ("CBA", "AU")
    assert connection.committed


def test_upsert_daily_refuses_a_row_that_forgot_its_venue():
    """Defaulting a missing venue to US is how an ASX row becomes indistinguishable from a
    US one -- the exact failure the column was added to stop."""
    store = store_against(FakeConnection())
    row = row_for_daily()
    del row["exchange"]

    with pytest.raises(ValueError, match="exchange"):
        store.upsert_daily([row])


def test_upsert_daily_normalises_the_venue_to_upper_case():
    connection = FakeConnection()
    store = store_against(connection)
    row = row_for_daily()
    row["exchange"] = " au "

    store.upsert_daily([row])

    assert connection.executed[1][1][0][1] == "AU"


# ---------------------------------------------------------------------------
# indicators: same promise, one level up
# ---------------------------------------------------------------------------

def test_the_indicator_merge_lists_as_many_values_as_columns():
    columns, values = insert_shape(_MERGE_INDICATOR)
    assert len(columns) == len(values)


def test_the_indicator_merge_is_keyed_and_inserted_with_the_exchange():
    columns, values = insert_shape(_MERGE_INDICATOR)
    assert "Exchange" in columns
    assert "source.Exchange" in values
    assert "target.Exchange = source.Exchange" in _MERGE_INDICATOR


def test_upsert_indicators_binds_one_parameter_per_column():
    connection = FakeConnection()
    store = store_against(connection)

    store.upsert_indicators([{
        "symbol": "CBA", "exchange": "AU",
        "values": {14: 1.0, 20: 1.0, 22: 1.0, 26: 1.0, 50: 1.0},
        "reference_price": 100.5, "sessions_used": 30,
        "as_of_session": "2026-10-30", "method": "tr", "smoothing": "simple",
    }])

    sql, payload = connection.executed[0]
    assert sql.count("?") == len(payload[0]) == 12
    assert payload[0][0:2] == ("CBA", "AU")


# ---------------------------------------------------------------------------
# reads: the venue is part of the question, not a default
# ---------------------------------------------------------------------------

def test_indicators_are_read_for_one_venue_only():
    connection = FakeConnection()
    store = store_against(connection)

    store.get_indicators(["aapl"], "AU")

    sql, params = connection.executed[0]
    assert "WHERE Exchange = ?" in sql
    assert params[0] == "AU"
    assert "AAPL" in params


def test_daily_bars_are_read_for_one_venue_only():
    connection = FakeConnection()
    store = store_against(connection)

    store.get_daily_bars_many(["aapl"], 15, "au")

    sql, params = connection.executed[0]
    assert "WHERE Exchange = ?" in sql
    assert params[0] == "AU"
    assert "AAPL" in params


def test_a_read_with_no_venue_means_us():
    """The API's own default, and what every row written before 2026-11-01 was."""
    connection = FakeConnection()
    store = store_against(connection)

    store.get_daily_bars_many(["msft"], 15)

    assert connection.executed[0][1][0] == "US"
