"""Layer 2 context-aware expected-value model -- see
anomaly_model.py's own module docstring for the four
build decisions (model type, feature encoding, training cadence, residual
threshold) this implements."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from src import anomaly_model, db, telegram_notifier

_TZ = anomaly_model._LOCAL_TZ
_START = datetime(1998, 1, 1, 6, 0, 0, tzinfo=_TZ)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


def _date(i: int) -> str:
    return (_START + timedelta(days=i)).strftime("%Y-%m-%d")


def _seed_water_history(conn, n_days: int, spike_value: float | None = None) -> None:
    """n_days of water_daily + matching weather_daily, mild deterministic
    variation (195/200/205 repeating) so the training residual distribution
    has real, small, non-zero spread -- a constant target would make every
    training residual exactly 0 and trivialize the percentile threshold.
    If spike_value is given, the LAST day (index n_days - 1) gets that value
    instead of the normal pattern -- this is the day run_daily_check will
    score as "yesterday" when `now` is set to n_days days after _START."""
    for i in range(n_days):
        value = spike_value if (spike_value is not None and i == n_days - 1) else 195.0 + (i % 3) * 5.0
        conn.execute(
            "INSERT INTO water_daily (date, usage_l) VALUES (?, ?)",
            (_date(i), value),
        )
        conn.execute(
            "INSERT INTO weather_daily (date, temperature_2m_mean, shortwave_radiation_sum, latitude, longitude, "
            "source, fetched_at) VALUES (?, 15.0, 10.0, 0.0, 0.0, 'archive', ?)",
            (_date(i), _date(i)),
        )
    conn.commit()


def _now_after(n_days: int) -> datetime:
    return _START + timedelta(days=n_days)


class TestGracefulDegradation:
    def test_insufficient_history_trains_nothing(self, conn):
        _seed_water_history(conn, anomaly_model.MIN_HISTORY_DAYS - 1)
        result = anomaly_model.train_model(conn, "water", now=_now_after(anomaly_model.MIN_HISTORY_DAYS - 1))
        assert result is None
        assert conn.execute("SELECT * FROM anomaly_model_state WHERE metric = 'water'").fetchone() is None

    def test_run_daily_check_skips_metric_below_history_floor(self, conn):
        _seed_water_history(conn, anomaly_model.MIN_HISTORY_DAYS - 1)
        flagged = anomaly_model.run_daily_check(conn, now=_now_after(anomaly_model.MIN_HISTORY_DAYS - 1))
        assert flagged == []

    def test_gas_with_no_weather_never_trains(self, conn):
        # 120 days of gas_daily but zero weather_daily rows -- gas requires
        # HDD, so every day is unscoreable and feature_dicts ends up
        # empty even though gas_daily itself has plenty of rows.
        for i in range(120):
            conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES (?, 3.0)", (_date(i),))
        conn.commit()
        result = anomaly_model.train_model(conn, "gas", now=_now_after(120))
        assert result is None


class TestTraining:
    def test_trains_with_enough_history(self, conn):
        _seed_water_history(conn, 120)
        result = anomaly_model.train_model(conn, "water", now=_now_after(120))
        assert result is not None
        assert result["metric"] == "water"
        # cutoff excludes both "today" and the not-yet-scored "yesterday"
        # (see _training_data's docstring) -- 120 seeded days, day 119 (the
        # last) is excluded, so 119 rows are usable.
        assert result["training_rows"] == 119

        row = conn.execute("SELECT * FROM anomaly_model_state WHERE metric = 'water'").fetchone()
        assert row is not None
        import json

        feature_names = json.loads(row["feature_names"])
        assert feature_names == list(anomaly_model._COMMON_FEATURES) + list(
            anomaly_model._METRIC_EXTRA_FEATURES["water"]
        )

    def test_retrain_replaces_prior_fit_not_duplicates(self, conn):
        _seed_water_history(conn, 120)
        anomaly_model.train_model(conn, "water", now=_now_after(120))
        anomaly_model.train_model(conn, "water", now=_now_after(120))
        rows = conn.execute("SELECT * FROM anomaly_model_state WHERE metric = 'water'").fetchall()
        assert len(rows) == 1


class TestDetection:
    def test_anchor_style_spike_is_flagged(self, conn):
        # Shape: a single day far above a stable ~200 L/day baseline.
        _seed_water_history(conn, 120, spike_value=5000.0)
        flagged = anomaly_model.run_daily_check(conn, now=_now_after(120))
        assert len(flagged) == 1
        ep = flagged[0]
        assert ep["kind"] == "context"
        assert ep["metric"] == "water"
        assert ep["total_volume"] == pytest.approx(5000.0)
        # peak_rate is repurposed as "expected value" for kind='context'
        # (documented in anomaly_model._record_context_anomaly) -- should
        # sit close to the ~200 L/day baseline, nowhere near the actual spike.
        assert ep["peak_rate"] == pytest.approx(200.0, abs=20.0)
        assert ep["acknowledged_at"] is not None  # digest, never the persistent banner
        assert ep["ended_at"] is not None

    def test_flagged_episode_excluded_from_active_alerts_query(self, conn):
        _seed_water_history(conn, 120, spike_value=5000.0)
        anomaly_model.run_daily_check(conn, now=_now_after(120))
        active = conn.execute("SELECT * FROM alert_episodes WHERE acknowledged_at IS NULL AND kind = 'context'").fetchall()
        assert active == []  # exactly what /api/alerts/active's WHERE clause selects

    def test_normal_day_is_not_flagged(self, conn):
        # Pinned to the pattern's own mean (200.0) rather than letting the
        # 195/200/205 cycle pick the last day's value naturally -- index 119
        # would land on 205.0, which is the top third of a 3-way cycle and
        # therefore not a meaningful "ordinary day" fixture on its own.
        _seed_water_history(conn, 120, spike_value=200.0)
        flagged = anomaly_model.run_daily_check(conn, now=_now_after(120))
        assert flagged == []
        assert conn.execute("SELECT * FROM alert_episodes WHERE kind = 'context'").fetchone() is None

    def test_repeated_runs_do_not_duplicate_the_same_day(self, conn):
        _seed_water_history(conn, 120, spike_value=5000.0)
        anomaly_model.run_daily_check(conn, now=_now_after(120))
        anomaly_model.run_daily_check(conn, now=_now_after(120))
        rows = conn.execute("SELECT * FROM alert_episodes WHERE kind = 'context'").fetchall()
        assert len(rows) == 1


class TestDigestDelivery:
    def test_disabled_toggle_records_disabled_status_not_an_exception(self, conn):
        # anomaly_alerts_enabled defaults to 0 -- send_message() must return
        # 'disabled' cleanly, and run_daily_check must record that on the
        # episode rather than raising (a notification failure must never
        # stall or crash the nightly timer).
        _seed_water_history(conn, 120, spike_value=5000.0)
        flagged = anomaly_model.run_daily_check(conn, now=_now_after(120))
        ep = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (flagged[0]["id"],)).fetchone()
        assert ep["notify_status"] == "disabled"

    def test_enabled_toggle_sends_one_digest_message(self, conn, monkeypatch):
        conn.execute("UPDATE feature_toggles SET anomaly_alerts_enabled = 1 WHERE id = 1")
        conn.execute(
            "INSERT INTO telegram_config (id, chat_id) VALUES (1, '12345') "
            "ON CONFLICT(id) DO UPDATE SET chat_id = excluded.chat_id"
        )
        conn.commit()
        monkeypatch.setenv("OMNIMETER_TELEGRAM_BOT_TOKEN", "dummy-token")

        sent: list[str] = []

        def fake_send(_conn, text):
            sent.append(text)
            return "sent", None

        monkeypatch.setattr(telegram_notifier, "send_message", fake_send)

        _seed_water_history(conn, 120, spike_value=5000.0)
        flagged = anomaly_model.run_daily_check(conn, now=_now_after(120))

        assert len(sent) == 1
        assert "Water" in sent[0]
        ep = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (flagged[0]["id"],)).fetchone()
        assert ep["notify_status"] == "sent"


class TestContextFeatures:
    def test_declared_occupancy_overrides_default(self):
        ctx = anomaly_model._context_for_date(
            "water", "1998-06-15", {"1998-06-15": _fake_weather_row(20.0, 15.0)},
            {"1998-06-15": 3}, set(), set(),
        )
        assert ctx["occupant_count"] == 3.0

    def test_away_day_backfill_sets_zero_occupancy(self):
        ctx = anomaly_model._context_for_date(
            "water", "1998-02-01", {"1998-02-01": _fake_weather_row(5.0, 2.0)},
            {}, {"1998-02-01"}, set(),
        )
        assert ctx["occupant_count"] == 0.0

    def test_unknown_day_defaults_to_occupied(self):
        ctx = anomaly_model._context_for_date(
            "water", "1998-03-01", {"1998-03-01": _fake_weather_row(10.0, 5.0)},
            {}, set(), set(),
        )
        assert ctx["occupant_count"] == 1.0

    def test_metric_missing_required_weather_returns_none(self):
        assert anomaly_model._context_for_date("gas", "1998-03-01", {}, {}, set(), set()) is None
        assert anomaly_model._context_for_date("water", "1998-03-01", {}, {}, set(), set()) is None

    def test_power_ev_charging_day_flag(self):
        ctx = anomaly_model._context_for_date(
            "power", "1998-05-01", {"1998-05-01": _fake_weather_row(12.0, 18.0)},
            {}, set(), {"1998-05-01"},
        )
        assert ctx["ev_charging_day"] == 1.0


def _fake_weather_row(temp_c: float, radiation: float) -> dict:
    return {"temperature_2m_mean": temp_c, "shortwave_radiation_sum": radiation}


class TestPercentile:
    def test_known_values(self):
        assert anomaly_model._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50.0) == pytest.approx(3.0)
        assert anomaly_model._percentile([10.0], 99.0) == pytest.approx(10.0)

    def test_high_percentile_close_to_max(self):
        values = [float(i) for i in range(1, 101)]  # 1..100
        assert anomaly_model._percentile(values, 99.0) == pytest.approx(99.01, abs=0.5)
