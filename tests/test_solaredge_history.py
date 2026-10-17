"""SolarEdge daily history: month windows, idempotent/resumable backfill, daily top-up, yearly verification.

1990s fixture dates only; placeholder credentials; no network.
"""

import sqlite3
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from src import db
from src import solaredge_history as sh

TZ = ZoneInfo("Europe/Amsterdam")
NOW = datetime(1998, 7, 28, 10, 0, tzinfo=UTC)  # 12:00 local, 1998-07-28


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


class FakeClient:
    """Answers /energy DAY windows with value = day-of-month * 1000 Wh; records every call."""

    def __init__(self, none_before=None):
        self.calls, self.requests_made, self.none_before = [], 0, none_before

    def get(self, path, params=None):
        self.calls.append((path, params))
        self.requests_made += 1
        if params["resolution"] == "YEAR":
            return (
                {"values": [{"timestamp": "1997-01-01T00:00:00+01:00", "value": self.year_kwh}]}
                if hasattr(self, "year_kwh")
                else {"values": []}
            )
        start = date.fromisoformat(params["from"][:10])
        end = date.fromisoformat(params["to"][:10])
        vals = []
        d = start
        while d < end:
            v = None if (self.none_before and d < self.none_before) else d.day * 1000.0
            vals.append({"timestamp": f"{d.isoformat()}T00:00:00+0{1 if d.month in (11, 12, 1, 2, 3) else 2}:00", "value": v})
            d = date.fromordinal(d.toordinal() + 1)
        return {"unit": "WH", "resolution": "DAY", "values": vals}


def test_month_windows_clip_both_ends_and_cross_years():
    assert sh.month_windows(date(1998, 1, 20), date(1998, 3, 5)) == [
        (date(1998, 1, 20), date(1998, 2, 1)),
        (date(1998, 2, 1), date(1998, 3, 1)),
        (date(1998, 3, 1), date(1998, 3, 5)),
    ]
    assert sh.month_windows(date(1998, 12, 10), date(1999, 1, 3)) == [
        (date(1998, 12, 10), date(1999, 1, 1)),
        (date(1999, 1, 1), date(1999, 1, 3)),
    ]
    assert sh.month_windows(date(1998, 7, 28), date(1998, 7, 28)) == []


def test_request_uses_local_midnight_with_dst_offsets_and_wh(conn):
    c = FakeClient()
    sh.fetch_days(c, 1, date(1998, 7, 1), date(1998, 8, 1), TZ)
    params = c.calls[0][1]
    assert params["from"] == "1998-07-01T00:00:00+02:00" and params["to"] == "1998-08-01T00:00:00+02:00"
    assert params["resolution"] == "DAY" and params["unit"] == "WH"
    sh.fetch_days(c, 1, date(1998, 1, 1), date(1998, 2, 1), TZ)
    assert c.calls[1][1]["from"] == "1998-01-01T00:00:00+01:00"  # winter offset


def test_window_over_a_month_is_rejected():
    with pytest.raises(ValueError):
        sh.fetch_days(FakeClient(), 1, date(1998, 1, 1), date(1998, 3, 1), TZ)


def test_store_uses_the_local_date_from_the_timestamp_and_skips_nulls(conn):
    n = sh.store_daily(
        conn,
        [{"timestamp": "1998-07-01T00:00:00+02:00", "value": 5000.0}, {"timestamp": "1998-07-02T00:00:00+02:00", "value": None}],
        NOW,
    )
    assert n == 1
    assert conn.execute("SELECT date, production_wh FROM solaredge_daily").fetchall()[0][:] == ("1998-07-01", 5000.0)


def test_backfill_fills_months_and_never_stores_today(conn):
    c = FakeClient()
    out = sh.backfill(conn, c, 1, date(1998, 6, 20), date(1998, 7, 28), TZ, sleep=lambda s: None, log=lambda m: None, now_utc=NOW)
    assert out["calls"] == 2 and out["windows"] == 2
    dates = [r[0] for r in conn.execute("SELECT date FROM solaredge_daily ORDER BY date")]
    assert dates[0] == "1998-06-20" and dates[-1] == "1998-07-27"  # today (07-28) excluded


def test_backfill_is_idempotent_and_resumable(conn):
    c = FakeClient()
    args = dict(sleep=lambda s: None, log=lambda m: None, now_utc=NOW)
    sh.backfill(conn, c, 1, date(1998, 5, 1), date(1998, 7, 28), TZ, **args)
    first_calls = len(c.calls)
    again = sh.backfill(conn, c, 1, date(1998, 5, 1), date(1998, 7, 28), TZ, **args)
    assert again["calls"] == 0 and again["skipped_months"] == 3 and len(c.calls) == first_calls
    # Interrupted run: delete one month's rows -> only that month is refetched.
    conn.execute("DELETE FROM solaredge_daily WHERE date >= '1998-06-01' AND date < '1998-07-01'")
    conn.commit()
    resumed = sh.backfill(conn, c, 1, date(1998, 5, 1), date(1998, 7, 28), TZ, **args)
    assert resumed["calls"] == 1 and resumed["skipped_months"] == 2


def test_pause_only_between_calls(conn):
    slept = []
    sh.backfill(
        conn,
        FakeClient(),
        1,
        date(1998, 5, 1),
        date(1998, 7, 28),
        TZ,
        sleep=slept.append,
        log=lambda m: None,
        now_utc=NOW,
        pause_s=7.0,
    )
    assert slept == [7.0, 7.0]  # 3 calls -> 2 pauses, none before the first


def test_pre_installation_days_are_simply_absent(conn):
    c = FakeClient(none_before=date(1998, 6, 10))
    sh.backfill(conn, c, 1, date(1998, 6, 1), date(1998, 7, 1), TZ, sleep=lambda s: None, log=lambda m: None, now_utc=NOW)
    assert conn.execute("SELECT MIN(date) FROM solaredge_daily").fetchone()[0] == "1998-06-10"


class TestFinaliseRecent:
    def test_empty_table_fetches_just_yesterday(self, conn):
        c = FakeClient()
        assert sh.finalise_recent(conn, c, 1, NOW, TZ) == 1
        assert [r[0] for r in conn.execute("SELECT date FROM solaredge_daily")] == ["1998-07-27"]

    def test_up_to_date_costs_nothing(self, conn):
        conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-27', 1.0, 'x')")
        c = FakeClient()
        assert sh.finalise_recent(conn, c, 1, NOW, TZ) == 0 and c.calls == []

    def test_gap_is_healed_in_one_call(self, conn):
        conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-20', 1.0, 'x')")
        c = FakeClient()
        assert sh.finalise_recent(conn, c, 1, NOW, TZ) == 1
        assert [r[0] for r in conn.execute("SELECT date FROM solaredge_daily ORDER BY date")][-1] == "1998-07-27"
        assert len(c.calls) == 1

    def test_unfillable_gap_is_asked_once_per_local_day(self, conn):
        # The API has no value for the gap days (inverter offline): nothing gets stored, so the gap stays open.
        conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-20', 1.0, 'x')")
        c = FakeClient(none_before=date(1998, 7, 28))
        assert sh.finalise_recent(conn, c, 1, NOW, TZ) == 1
        assert conn.execute("SELECT MAX(date) FROM solaredge_daily").fetchone()[0] == "1998-07-20"
        for minutes in (30, 60, 90):  # later runs the same local day must not spend a credit
            later = NOW.replace(minute=minutes % 60, hour=NOW.hour + minutes // 60)
            assert sh.finalise_recent(conn, c, 1, later, TZ) == 0
        assert len(c.calls) == 1

    def test_unfillable_gap_is_retried_the_next_day(self, conn):
        conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-20', 1.0, 'x')")
        c = FakeClient(none_before=date(1998, 7, 29))
        sh.finalise_recent(conn, c, 1, NOW, TZ)
        tomorrow = NOW.replace(day=29)
        assert sh.finalise_recent(conn, c, 1, tomorrow, TZ) == 1
        assert len(c.calls) == 2

    def test_failed_call_is_not_recorded_as_an_attempt(self, conn):
        conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-20', 1.0, 'x')")

        class Boom:
            requests_made = 0

            def get(self, path, params=None):
                raise RuntimeError("api down")

        with pytest.raises(RuntimeError):
            sh.finalise_recent(conn, Boom(), 1, NOW, TZ)
        assert sh.finalise_recent(conn, FakeClient(), 1, NOW, TZ) == 1  # retried, and it heals

    def test_long_outage_gap_is_capped_to_one_months_window(self, conn):
        conn.execute("INSERT INTO solaredge_daily VALUES ('1998-03-01', 1.0, 'x')")
        c = FakeClient()
        sh.finalise_recent(conn, c, 1, NOW, TZ)  # must not raise on the >1-month gap
        span = date.fromisoformat(c.calls[0][1]["to"][:10]) - date.fromisoformat(c.calls[0][1]["from"][:10])
        assert span.days <= sh.MAX_DAYS_PER_CALL


def test_yearly_check_flags_a_mismatch_only_beyond_tolerance(conn):
    for d in range(1, 11):
        conn.execute("INSERT INTO solaredge_daily VALUES (?, 1000.0, 'x')", (f"1997-01-{d:02d}",))
    c = FakeClient()
    c.year_kwh = 10.0  # 10 days x 1 kWh
    assert sh.yearly_check(conn, c, 1, date(1997, 1, 1), date(1998, 7, 28), TZ)[0]["ok"] is True
    c.year_kwh = 500.0
    assert sh.yearly_check(conn, c, 1, date(1997, 1, 1), date(1998, 7, 28), TZ)[0]["ok"] is False


class TestRateLimitRetry:
    class Flaky(FakeClient):
        def __init__(self, failures, credit=False):
            super().__init__()
            self.failures, self.credit = failures, credit

        def get(self, path, params=None):
            if self.failures:
                self.failures -= 1
                self.requests_made += 1
                raise sh.solaredge_client.SolarEdgeRateLimitError("429", credit_exhausted=self.credit, retry_after_s=12)
            return super().get(path, params)

    def test_per_minute_limit_waits_then_succeeds(self, conn):
        slept = []
        c = self.Flaky(failures=1)
        out = sh.backfill(conn, c, 1, date(1998, 7, 1), date(1998, 7, 28), TZ, sleep=slept.append, log=lambda m: None, now_utc=NOW)
        assert out["calls"] == 1 and 12.0 in slept
        assert conn.execute("SELECT COUNT(*) FROM solaredge_daily").fetchone()[0] == 27

    def test_persistent_per_minute_limit_gives_up_after_the_attempts(self, conn):
        with pytest.raises(sh.solaredge_client.SolarEdgeRateLimitError):
            sh.backfill(
                conn,
                self.Flaky(failures=5),
                1,
                date(1998, 7, 1),
                date(1998, 7, 28),
                TZ,
                sleep=lambda s: None,
                log=lambda m: None,
                now_utc=NOW,
            )

    def test_credit_cap_is_never_retried(self, conn):
        slept = []
        with pytest.raises(sh.solaredge_client.SolarEdgeRateLimitError):
            sh.backfill(
                conn,
                self.Flaky(failures=1, credit=True),
                1,
                date(1998, 7, 1),
                date(1998, 7, 28),
                TZ,
                sleep=slept.append,
                log=lambda m: None,
                now_utc=NOW,
            )
        assert slept == []
