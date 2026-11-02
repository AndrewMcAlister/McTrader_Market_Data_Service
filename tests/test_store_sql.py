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
from datetime import date, datetime, timedelta, timezone
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
        "raw_close": 100.6,
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


def test_the_daily_merge_carries_the_raw_close_both_ways():
    columns, values = insert_shape(_MERGE_DAILY_FROM_STAGE)
    assert "RawClose" in columns
    assert "source.[RawClose]" in values


def test_a_raw_less_source_cannot_erase_a_known_raw_close():
    """The BarHourly seed and the write benchmark rewrite rows without a raw close. The
    UPDATE must keep the collected value rather than nulling it."""
    assert ("[RawClose] = COALESCE(source.[RawClose], target.[RawClose])"
            in _MERGE_DAILY_FROM_STAGE)


def test_upsert_daily_binds_one_parameter_per_stage_column():
    connection = FakeConnection()
    store = store_against(connection)

    assert store.upsert_daily([row_for_daily()]) == 1

    insert_sql, payload = connection.executed[1]
    assert insert_sql.count("?") == len(payload[0]) == 11
    assert payload[0][0:2] == ("CBA", "AU")
    assert payload[0][7] == 100.6          # RawClose, the unadjusted close
    assert connection.committed


def test_upsert_daily_refuses_a_row_that_forgot_its_venue():
    """Defaulting a missing venue to US is how an ASX row becomes indistinguishable from a
    US one -- the exact failure the column was added to stop."""
    store = store_against(FakeConnection())
    row = row_for_daily()
    del row["exchange"]

    with pytest.raises(ValueError, match="venue"):
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


# ---------------------------------------------------------------------------
# every write states its venue: no row is filed as US by omission
# ---------------------------------------------------------------------------

def test_upsert_indicators_refuses_a_row_that_forgot_its_venue():
    store = store_against(FakeConnection())

    with pytest.raises(ValueError, match="venue"):
        store.upsert_indicators([{
            "symbol": "CBA",
            "values": {14: 1.0},
            "reference_price": 100.5, "sessions_used": 30,
            "as_of_session": "2026-10-30", "method": "tr", "smoothing": "simple",
        }])


def test_upsert_prices_refuses_a_row_that_forgot_its_venue():
    """(symbol, venue) is the key: a venue-less row would overwrite another venue's price."""
    store = store_against(FakeConnection())

    with pytest.raises(ValueError, match="venue"):
        store.upsert_prices([{"symbol": "AAPL", "price": 231.4}])


def test_the_database_does_not_default_the_venue():
    """The second line of defence: with no DEFAULT, a write that bypasses this module and
    omits the venue is refused by SQL Server instead of being filed as US."""
    text = (Path(__file__).resolve().parent.parent / "sql" / "001_schema.sql").read_text(
        encoding="utf-8")
    venue_columns = [line.strip() for line in text.splitlines()
                     if line.strip().startswith(("Exchange ", "Venue "))]
    assert len(venue_columns) == 3, f"expected three venue columns, found {venue_columns}"
    for line in venue_columns:
        assert "DEFAULT" not in line.upper(), f"the venue is defaulted: {line}"


# ---------------------------------------------------------------------------
# the price universe: a broad session first, then dollar volume
# ---------------------------------------------------------------------------

def test_the_universe_binds_venue_breadth_and_limit_in_order():
    connection = FakeConnection(rows=[("BHP",), ("CBA",), ("COL",)])
    store = store_against(connection)

    symbols = store.top_symbols_by_dollar_volume("AU", limit=3, min_breadth=50)

    assert symbols == ["BHP", "CBA", "COL"]
    sql, params = connection.executed[0]
    assert "HAVING COUNT(*) >= ?" in sql
    assert params == ("AU", 50, 3, "AU")


def test_the_universe_is_ranked_by_dollar_volume():
    connection = FakeConnection()
    store = store_against(connection)

    store.top_symbols_by_dollar_volume("AU", limit=10)

    sql, _ = connection.executed[0]
    assert "d.[Close] * d.Volume DESC" in sql
    assert "d.[Close] > 0" in sql and "d.Volume > 0" in sql


# ---------------------------------------------------------------------------
# history depth: how much of a symbol's series is already stored
# ---------------------------------------------------------------------------

def test_the_depth_count_binds_the_venue_the_symbols_and_the_horizon():
    connection = FakeConnection(rows=[("cba", 560), ("bhp", 12)])
    store = store_against(connection)

    depths = store.daily_bar_depth(["cba", "bhp"], "AU", since=date(2023, 1, 1))

    sql, params = connection.executed[0]
    assert "GROUP BY SymbolCode" in sql
    assert "SnapshotDate >= ?" in sql
    assert params == ("AU", "CBA", "BHP", date(2023, 1, 1))
    assert depths == {"CBA": 560, "BHP": 12}


def test_a_symbol_with_no_stored_bars_has_depth_zero():
    """Absent from the grouped result means none, not "unknown" -- the caller deepens."""
    connection = FakeConnection(rows=[("cba", 40)])
    store = store_against(connection)

    assert store.daily_bar_depth(["CBA", "BHP"], "AU") == {"CBA": 40, "BHP": 0}


def test_a_depth_count_with_no_symbols_asks_the_database_nothing():
    connection = FakeConnection()
    store = store_against(connection)

    assert store.daily_bar_depth([], "AU") == {}
    assert connection.executed == []


def test_a_non_positive_limit_prices_no_universe():
    """'No universe' is a named outcome, not an empty POST /prices that raises."""
    connection = FakeConnection(rows=[("BHP",)])
    store = store_against(connection)

    assert store.top_symbols_by_dollar_volume("AU", limit=0) == []
    assert connection.executed == []


# ---------------------------------------------------------------------------
# movers: "today" is an observation floor, not a cache-wide read
# ---------------------------------------------------------------------------

def test_big_movers_excludes_rows_observed_before_the_session_open():
    """A name that dropped out of today's universe still carries yesterday's quote; it
    must not be ranked as a mover today just because another row is fresh."""
    connection = FakeConnection()
    store = store_against(connection)
    opened = datetime(2026, 11, 1, 23, 0)

    store.big_movers("AU", limit=50, since=opened)

    assert connection.executed, "no query was run"
    for sql, params in connection.executed:
        assert "COALESCE(AsOfUtc, LastUpdatedUtc) >= ?" in sql
        assert params[-1] == opened


def test_big_movers_normalises_an_aware_session_open_to_naive_utc():
    """The column is naive UTC; an aware value from the clock must not reach pyodbc."""
    connection = FakeConnection()
    store = store_against(connection)
    opened_sydney = datetime(2026, 11, 2, 10, 0,
                             tzinfo=timezone(timedelta(hours=11)))

    store.big_movers("AU", since=opened_sydney)

    assert connection.executed[0][1][-1] == datetime(2026, 11, 1, 23, 0)


def test_big_movers_without_a_session_floor_reads_the_whole_cache():
    """The old contract is still available for a caller that wants it."""
    connection = FakeConnection()
    store = store_against(connection)

    store.big_movers("AU", limit=5)

    sql, params = connection.executed[0]
    assert "COALESCE(AsOfUtc, LastUpdatedUtc) >=" not in sql
    assert params == (5, "AU", 1.0, 0.0)
