"""Layer 1 runaway (leak) detector -- Tier 1 gates and Tier 2 background
windows.

Fixtures use synthetic values shaped like the cases the detector is
designed for. TestSustainedRunVsBathRegression pins the firing minute of a
sustained high-rate run and the silence of a short bath-like burst.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from src import db, runaway_detector, telegram_notifier

_TZ = runaway_detector._LOCAL_TZ
_START = datetime(1998, 1, 1, 12, 0, 0, tzinfo=_TZ)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    # Deterministic thresholds for the tests below, independent of the
    # fresh-install defaults seeded by db.py (which may change).
    c.execute(
        "UPDATE runaway_detector_config SET home_threshold_l = 100, away_threshold_l = 10, away_after_days = 2 "
        "WHERE metric = 'water'"
    )
    c.commit()
    return c


def _seed(conn, dl: float, t: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO water_readings (time, water_usage_dl, granularity) VALUES (?, ?, 'api_live')",
        (t.strftime("%Y-%m-%d %H:%M:%S"), dl),
    )
    conn.commit()


def _poll(conn, cumulative_l: float, t: datetime):
    _seed(conn, cumulative_l * 10, t)
    return runaway_detector.check_water(conn, now=t)


def _seed_minute(conn, dl: float, t: datetime) -> None:
    """A reading stamped to the minute -- what meter_ingest actually writes.
    Three 20s polls upsert one row, so production's api_live series is a
    1-minute series, which is the resolution every gate was validated at."""
    conn.execute(
        "INSERT OR REPLACE INTO water_readings (time, water_usage_dl, granularity) VALUES (?, ?, 'api_live')",
        (t.strftime("%Y-%m-%d %H:%M"), dl),
    )
    conn.commit()


def _drive_minutes(conn, start: datetime, deltas_l: list[float]) -> list[tuple[datetime, dict | None]]:
    """Replay a run at the production resolution: one reading and one
    check_water() per minute. Returns (time, episode) for every minute, so
    a test can assert not just *whether* a run fires but exactly when."""
    _seed_minute(conn, 0.0, start - timedelta(minutes=1))
    runaway_detector.check_water(conn, now=start - timedelta(minutes=1))
    out, cumulative = [], 0.0
    for i, delta in enumerate(deltas_l):
        t = start + timedelta(minutes=i)
        cumulative += delta
        _seed_minute(conn, cumulative * 10, t)
        out.append((t, runaway_detector.check_water(conn, now=t)))
    return out


def _first_firing(polls):
    for t, episode in polls:
        if episode is not None:
            return t, episode
    return None, None


def _seed_span(conn, start: datetime, minutes: int, litres_per_min, gaps=()) -> None:
    """One reading per minute across the span -- what the poller writes
    whether or not water is moving. `litres_per_min` may be a constant or a
    callable of the minute; `gaps` are (from, to) windows where no reading
    is written at all, modelling an outage: the meter keeps advancing but
    nothing records it."""
    cumulative = 0.0
    for i in range(minutes):
        t = start + timedelta(minutes=i)
        cumulative += litres_per_min(t) if callable(litres_per_min) else litres_per_min
        if any(a <= t < b for a, b in gaps):
            continue
        _seed_minute(conn, cumulative * 10, t)


class TestFirstSample:
    def test_seeds_state_without_firing(self, conn):
        episode = _poll(conn, 0, _START)
        assert episode is None
        state = conn.execute("SELECT * FROM detector_state WHERE metric = 'water'").fetchone()
        assert state["last_meter_value"] == 0
        assert state["run_started_at"] is None


class TestContinuousRunFires:
    # Litres per synthetic minute, shaped like a sustained high-rate run:
    # no idle minute, crosses the 100 L threshold on the 6th delta
    # at a cumulative of 110 L.
    DELTAS_L = [13, 14, 20, 15, 27, 21, 31, 19, 25, 26, 18, 54, 44]

    def test_fires_once_threshold_crossed_and_not_before(self, conn):
        t = _START
        _poll(conn, 0, t)  # seed baseline
        cumulative = 0.0
        fired_at_index = None
        for i, delta in enumerate(self.DELTAS_L):
            t += timedelta(seconds=20)
            cumulative += delta
            episode = _poll(conn, cumulative, t)
            if episode is not None:
                fired_at_index = i
                break
        assert fired_at_index == 5  # 13+14+20+15+27+21 = 110, first >= 100
        assert episode["total_volume"] == pytest.approx(110.0)
        assert episode["threshold_used"] == 100.0
        assert episode["occupancy_state"] == "home"
        assert episode["ended_at"] is None

    def test_run_continues_to_update_same_episode(self, conn):
        t = _START
        _poll(conn, 0, t)
        cumulative = 0.0
        episode = None
        for delta in self.DELTAS_L:
            t += timedelta(seconds=20)
            cumulative += delta
            episode = _poll(conn, cumulative, t)
        assert episode is not None
        assert episode["total_volume"] == pytest.approx(sum(self.DELTAS_L))
        rows = conn.execute("SELECT COUNT(*) AS n FROM alert_episodes").fetchone()
        assert rows["n"] == 1  # one continuous run -> one episode row, not one per poll

    def test_run_started_at_stable_across_polls(self, conn):
        t = _START
        _poll(conn, 0, t)
        started_ats = set()
        cumulative = 0.0
        for delta in self.DELTAS_L:
            t += timedelta(seconds=20)
            cumulative += delta
            _poll(conn, cumulative, t)
            state = conn.execute("SELECT run_started_at FROM detector_state WHERE metric = 'water'").fetchone()
            started_ats.add(state["run_started_at"])
        assert len(started_ats) == 1  # never restarted mid-run


class TestIntermittentUseDoesNotFire:
    def test_two_separated_bursts_never_cross_threshold(self, conn):
        # 120 L total across the day (ABOVE the 100 L threshold) but as two
        # separated bursts of 65 L and 55 L, neither alone reaching 100 L --
        # this is the case a flat "100 L per window"
        # rule that sums a day/window would wrongly fire on, but the
        # continuity rule (volume within one unbroken run) correctly does
        # not, because no single run ever crosses the threshold.
        t = _START
        _poll(conn, 0, t)
        cumulative = 0.0
        for delta in [20, 20, 15, 10]:  # burst 1: 65 L, continuous
            t += timedelta(seconds=20)
            cumulative += delta
            episode = _poll(conn, cumulative, t)
            assert episode is None
        t += timedelta(seconds=20)
        episode = _poll(conn, cumulative, t)  # idle poll -- ends the run
        assert episode is None
        for delta in [20, 15, 12, 8]:  # burst 2: 55 L, continuous (120 L for the "day" total)
            t += timedelta(seconds=20)
            cumulative += delta
            episode = _poll(conn, cumulative, t)
            assert episode is None

    def test_single_88l_continuous_run_below_100_does_not_fire(self, conn):
        # Close to threshold but must not fire at 100 L.
        t = _START
        _poll(conn, 0, t)
        cumulative = 0.0
        episode = None
        for delta in [10, 12, 11, 9, 13, 10, 8, 15]:  # 88 L, continuous
            t += timedelta(seconds=20)
            cumulative += delta
            episode = _poll(conn, cumulative, t)
        assert episode is None


class TestStaleGapAndRestart:
    def test_stale_gap_resets_the_run_instead_of_resuming(self, conn):
        t = _START
        _poll(conn, 0, t)
        t += timedelta(seconds=20)
        _poll(conn, 30, t)  # 30 L into a run, well under threshold
        state = conn.execute("SELECT * FROM detector_state WHERE metric = 'water'").fetchone()
        assert state["run_volume"] == 30

        # A gap far longer than STALE_GAP_SECONDS -- simulates a service
        # restart missing the run entirely.
        t += timedelta(seconds=runaway_detector.STALE_GAP_SECONDS + 30)
        _poll(conn, 45, t)  # +15 L after the gap
        state = conn.execute("SELECT * FROM detector_state WHERE metric = 'water'").fetchone()
        # The new run only carries the post-gap delta, not 30+15.
        assert state["run_volume"] == pytest.approx(15.0)

    def test_short_gap_resumes_the_run(self, conn):
        t = _START
        _poll(conn, 0, t)
        t += timedelta(seconds=20)
        _poll(conn, 30, t)
        t += timedelta(seconds=runaway_detector.STALE_GAP_SECONDS - 10)  # under the stale-gap floor
        _poll(conn, 45, t)
        state = conn.execute("SELECT * FROM detector_state WHERE metric = 'water'").fetchone()
        assert state["run_volume"] == pytest.approx(45.0)  # resumed, not reset


class TestMeterReset:
    def test_negative_delta_resets_without_negative_volume(self, conn):
        t = _START
        _poll(conn, 0, t)
        t += timedelta(seconds=20)
        _poll(conn, 30, t)
        t += timedelta(seconds=20)
        episode = _poll(conn, 5, t)  # cumulative went DOWN -- a meter reset, not a fault
        assert episode is None
        state = conn.execute("SELECT * FROM detector_state WHERE metric = 'water'").fetchone()
        assert state["run_volume"] == 0
        assert state["last_meter_value"] == 50  # 5 L in dL


class TestAwayMode:
    def _seed_away_days(self, conn, days_before_today: list[int], ref_date):
        for n in days_before_today:
            d = (ref_date - timedelta(days=n)).date().isoformat()
            conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES (?, 0.02)", (d,))
            conn.execute("INSERT INTO water_daily (date, usage_l) VALUES (?, 3)", (d,))
        conn.commit()

    def test_away_threshold_applies_after_trailing_near_zero_days(self, conn):
        self._seed_away_days(conn, [1, 2], _START)
        t = _START
        _poll(conn, 0, t)
        t += timedelta(seconds=20)
        episode = _poll(conn, 12, t)  # 12 L, below home (100) but above away (10)
        assert episode is not None
        assert episode["threshold_used"] == 10.0
        assert episode["occupancy_state"] == "away"

    def test_single_low_day_does_not_yet_trigger_away_mode(self, conn):
        self._seed_away_days(conn, [1], _START)  # only 1 of the required 2 days
        t = _START
        _poll(conn, 0, t)
        t += timedelta(seconds=20)
        episode = _poll(conn, 12, t)
        assert episode is None  # 12 L is under the home threshold (100)

    def test_declared_occupancy_overrides_away_mode(self, conn):
        self._seed_away_days(conn, [1, 2], _START)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count, notes) VALUES (?, ?, 1, 'Test Data note')",
            ((_START - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"), (_START + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        assert runaway_detector.is_away(conn, "water", _START) is False

    def test_unsettled_high_usage_today_does_not_relax_away_mode(self, conn):
        # Away-mode must never auto-relax from live consumption.
        # Today isn't in water_daily/gas_daily yet (that only happens once
        # the day settles), so a high live run today must not flip is_away()
        # back to False -- only a settled trailing day can do that.
        self._seed_away_days(conn, [1, 2], _START)
        t = _START
        _poll(conn, 0, t)
        t += timedelta(seconds=20)
        _poll(conn, 200, t)  # a large live run "today" -- not yet in water_daily
        assert runaway_detector.is_away(conn, "water", t) is True

    def test_a_settled_high_usage_trailing_day_relaxes_away_mode(self, conn):
        self._seed_away_days(conn, [1, 2], _START)
        # Yesterday's day (index 1) later settles with real usage -- no
        # longer qualifies as an away day.
        yesterday = (_START - timedelta(days=1)).date().isoformat()
        conn.execute("UPDATE water_daily SET usage_l = 150 WHERE date = ?", (yesterday,))
        conn.commit()
        assert runaway_detector.is_away(conn, "water", _START) is False

    def test_brief_visit_inside_a_long_absence_does_not_fire(self, conn):
        # A single day with a small amount of use (a brief check-in
        # visit), bracketed by zeros, must not fire even at the away
        # threshold, because the visit's flow is not continuous.
        self._seed_away_days(conn, list(range(1, 63)), _START)  # a long absence
        t = _START
        _poll(conn, 0, t)
        cumulative = 0.0
        for delta in [1.5, 2.0, 1.0, 1.5, 1.0]:  # 7 L total, spread out with idle gaps
            t += timedelta(seconds=20)
            cumulative += delta
            episode = _poll(conn, cumulative, t)
            assert episode is None
            t += timedelta(seconds=20)
            episode = _poll(conn, cumulative, t)  # idle poll between each small draw
            assert episode is None


class TestMaybeNotify:
    def _make_episode(self, conn, total_volume=110.0, occupancy_state="home", started_at=None):
        started_at = started_at or _START.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, total_volume, unit, "
            "threshold_used, occupancy_state, alert_count) VALUES "
            "('runaway', 'water', ?, ?, ?, 'L', 100, ?, 0)",
            (started_at, started_at, total_volume, occupancy_state),
        )
        conn.commit()
        return conn.execute("SELECT id FROM alert_episodes WHERE started_at = ?", (started_at,)).fetchone()["id"]

    def test_sends_immediately_on_first_alert(self, conn, monkeypatch):
        conn.execute("UPDATE feature_toggles SET anomaly_alerts_enabled = 1 WHERE id = 1")
        conn.commit()
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: (calls.append(text) or ("sent", None)))
        episode_id = self._make_episode(conn)
        runaway_detector.maybe_notify(conn, episode_id, now=_START)
        assert len(calls) == 1
        assert "alert #1" in calls[0]
        row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
        assert row["alert_count"] == 1
        assert row["notify_status"] == "sent"

    def test_backoff_schedule_15_then_30_then_hourly(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: (calls.append(1) or ("sent", None)))
        episode_id = self._make_episode(conn)

        t = _START
        runaway_detector.maybe_notify(conn, episode_id, now=t)  # #1, immediate
        assert len(calls) == 1

        runaway_detector.maybe_notify(conn, episode_id, now=t + timedelta(minutes=5))  # too soon for #2
        assert len(calls) == 1

        t2 = t + timedelta(minutes=15, seconds=1)
        runaway_detector.maybe_notify(conn, episode_id, now=t2)  # #2, at +15
        assert len(calls) == 2

        runaway_detector.maybe_notify(conn, episode_id, now=t2 + timedelta(minutes=20))  # too soon for #3 (needs 30)
        assert len(calls) == 2

        t3 = t2 + timedelta(minutes=30, seconds=1)
        runaway_detector.maybe_notify(conn, episode_id, now=t3)  # #3, at +30
        assert len(calls) == 3

        runaway_detector.maybe_notify(conn, episode_id, now=t3 + timedelta(minutes=45))  # too soon for #4 (needs 60)
        assert len(calls) == 3

        t4 = t3 + timedelta(hours=1, seconds=1)
        runaway_detector.maybe_notify(conn, episode_id, now=t4)  # #4, at +60
        assert len(calls) == 4

    def test_failed_send_does_not_advance_backoff_and_retries_next_poll(self, conn, monkeypatch):
        results = iter([("failed", "boom"), ("failed", "boom"), ("sent", None)])
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: next(results))
        episode_id = self._make_episode(conn)

        t = _START
        runaway_detector.maybe_notify(conn, episode_id, now=t)
        row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
        assert row["alert_count"] == 0  # failure never advances the count
        assert row["notify_status"] == "failed"

        # Retried on the very next poll cycle (20s later), not after a
        # backoff wait.
        runaway_detector.maybe_notify(conn, episode_id, now=t + timedelta(seconds=20))
        row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
        assert row["alert_count"] == 0

        runaway_detector.maybe_notify(conn, episode_id, now=t + timedelta(seconds=40))
        row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
        assert row["alert_count"] == 1
        assert row["notify_status"] == "sent"

    def test_acknowledged_episode_is_never_notified(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: (calls.append(1) or ("sent", None)))
        episode_id = self._make_episode(conn)
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=False, now=_START)
        runaway_detector.maybe_notify(conn, episode_id, now=_START + timedelta(hours=2))
        assert calls == []

    def test_ended_episode_is_never_notified(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: (calls.append(1) or ("sent", None)))
        episode_id = self._make_episode(conn)
        conn.execute("UPDATE alert_episodes SET ended_at = ? WHERE id = ?", (_START.strftime("%Y-%m-%d %H:%M:%S"), episode_id))
        conn.commit()
        runaway_detector.maybe_notify(conn, episode_id, now=_START + timedelta(hours=2))
        assert calls == []


class TestRecordDismissal:
    def test_plain_acknowledge_does_not_change_threshold(self, conn):
        conn.execute(
            "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, total_volume, unit, "
            "threshold_used, occupancy_state, alert_count) VALUES "
            "('runaway', 'water', ?, ?, 130, 'L', 100, 'home', 1)",
            (_START.strftime("%Y-%m-%d %H:%M:%S"), _START.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        episode_id = conn.execute("SELECT id FROM alert_episodes").fetchone()["id"]
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=False, now=_START)
        thresholds = runaway_detector.get_thresholds(conn, "water")
        assert thresholds["home_threshold_l"] == 100
        row = conn.execute("SELECT acknowledged_at FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
        assert row["acknowledged_at"] is not None

    def test_not_a_leak_nudges_threshold_past_the_firing_volume(self, conn):
        conn.execute(
            "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, total_volume, unit, "
            "threshold_used, occupancy_state, alert_count) VALUES "
            "('runaway', 'water', ?, ?, 118, 'L', 100, 'home', 1)",
            (_START.strftime("%Y-%m-%d %H:%M:%S"), _START.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        episode_id = conn.execute("SELECT id FROM alert_episodes").fetchone()["id"]
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=True, now=_START)
        thresholds = runaway_detector.get_thresholds(conn, "water")
        assert thresholds["home_threshold_l"] == pytest.approx(128.0)  # 118 + 10 margin

    def test_not_a_leak_never_lowers_an_already_higher_threshold(self, conn):
        conn.execute(
            "UPDATE runaway_detector_config SET home_threshold_l = 500 WHERE metric = 'water'"
        )
        conn.execute(
            "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, total_volume, unit, "
            "threshold_used, occupancy_state, alert_count) VALUES "
            "('runaway', 'water', ?, ?, 118, 'L', 100, 'home', 1)",
            (_START.strftime("%Y-%m-%d %H:%M:%S"), _START.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        episode_id = conn.execute("SELECT id FROM alert_episodes").fetchone()["id"]
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=True, now=_START)
        thresholds = runaway_detector.get_thresholds(conn, "water")
        assert thresholds["home_threshold_l"] == 500.0

    def test_not_a_leak_bumps_away_threshold_for_an_away_episode(self, conn):
        conn.execute(
            "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, total_volume, unit, "
            "threshold_used, occupancy_state, alert_count) VALUES "
            "('runaway', 'water', ?, ?, 12, 'L', 10, 'away', 1)",
            (_START.strftime("%Y-%m-%d %H:%M:%S"), _START.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        episode_id = conn.execute("SELECT id FROM alert_episodes").fetchone()["id"]
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=True, now=_START)
        thresholds = runaway_detector.get_thresholds(conn, "water")
        assert thresholds["away_threshold_l"] == pytest.approx(22.0)
        assert thresholds["home_threshold_l"] == 100.0  # untouched


class TestGetThresholds:
    def test_falls_back_to_fresh_install_defaults_for_unknown_metric(self, conn):
        thresholds = runaway_detector.get_thresholds(conn, "gas")
        assert thresholds == runaway_detector._DEFAULT_THRESHOLDS


class TestTierOneGates:
    """One firing test and one near-miss per gate. The near-misses are the
    load-bearing half: each one sits just inside the domestic envelope that
    gate was set outside of, so a gate loosened by accident shows up here
    as a false positive rather than as silence somewhere else."""

    @pytest.fixture
    def gated(self, conn):
        # The volume gate at its designed value; the fixture's own 100 L is
        # a leftover from the single-threshold era.
        conn.execute("UPDATE runaway_detector_config SET home_threshold_l = 250 WHERE metric = 'water'")
        conn.commit()
        return conn

    def test_g3_fires_on_extreme_rate_before_volume_piles_up(self, gated):
        # 25 L/min -- a burst pipe. G3 needs 5 minutes of it; at that point
        # only 150 L has passed, well under G1's 250 L backstop, which is
        # the entire point of having a rate gate at all.
        t, episode = _first_firing(_drive_minutes(gated, _START, [25.0] * 10))
        assert episode is not None
        assert episode["fired_via"] == "G3"
        assert t == _START + timedelta(minutes=5)
        assert episode["total_volume"] == pytest.approx(150.0)
        assert episode["threshold_used"] == 20.0  # the gate's operative value, not a volume
        assert episode["peak_rate"] == pytest.approx(25.0)

    def test_g3_does_not_fire_just_below_the_bar(self, gated):
        # 17 L/min is a legitimate sustained rate (a bath). G3's bar is 20 for exactly this reason.
        polls = _drive_minutes(gated, _START, [17.0] * 12)
        assert _first_firing(polls) == (None, None)

    def test_g2_fires_on_a_fixture_left_running(self, gated):
        # 12 L/min for 15 minutes: too slow for G3, far too slow to reach
        # 250 L in any reasonable time, but nothing in the house runs a
        # quarter of an hour at that rate.
        t, episode = _first_firing(_drive_minutes(gated, _START, [12.0] * 20))
        assert episode is not None
        assert episode["fired_via"] == "G2"
        assert t == _START + timedelta(minutes=15)
        assert episode["threshold_used"] == 10.0

    def test_g2_does_not_fire_on_a_long_slow_legitimate_run(self, gated):
        # 7.5 L/min for half an hour -- long, but under G2's rate bar, and
        # 225 L is under G1's volume bar. Both gates have to decline.
        polls = _drive_minutes(gated, _START, [7.5] * 30)
        assert _first_firing(polls) == (None, None)

    def test_g1_fires_on_volume_alone_with_no_duration_qualifier(self, gated):
        # 9 L/min: under both rate gates forever, so only the volume
        # backstop can catch it. It fires the minute 250 L is passed --
        # there is deliberately no "and it has run for N minutes" term,
        # because nothing domestic reaches 250 L at all, so a duration
        # condition could only delay a true positive.
        t, episode = _first_firing(_drive_minutes(gated, _START, [9.0] * 30))
        assert episode is not None
        assert episode["fired_via"] == "G1"
        assert t == _START + timedelta(minutes=27)  # 28 x 9 = 252 L
        assert episode["total_volume"] == pytest.approx(252.0)
        assert episode["threshold_used"] == 250.0

    def test_g1_does_not_fire_just_under_the_volume_bar(self, gated):
        polls = _drive_minutes(gated, _START, [9.0] * 27)  # 243 L
        assert _first_firing(polls) == (None, None)

    def test_short_run_cannot_claim_a_five_minute_sustained_rate(self, gated):
        # The trailing mean is never padded with zeros and never reaches
        # back past the run's start, so a 3-minute burst at 60 L/min still
        # has to wait out G3's 5-minute duration term rather than
        # borrowing quiet minutes from before it began.
        polls = _drive_minutes(gated, _START, [60.0, 60.0, 60.0])
        assert _first_firing(polls) == (None, None)


class TestSustainedRunVsBathRegression:
    """A sustained high-rate run must fire via the extreme-rate gate; a short
    bath-like burst peaking below 20 L/min must not. One is fast but short,
    the other long but slow, which is why a single number cannot separate
    them. Series are synthetic."""

    # A long run opening with a two-minute trickle at ~1 L/min.
    # That lead-in is why the rate signal is a trailing mean and not a run
    # average: a run average stays diluted by it and crosses any bar late.
    SUSTAINED_RUN_L = [
        1, 1, 13, 14, 20, 15, 27, 21, 31, 17, 25, 28, 16, 54, 46, 69, 45, 71, 43,
        59, 50, 32, 49, 45, 69, 55, 83, 53, 67, 54, 72, 83, 56, 85, 61, 84, 54, 102,
        71, 90, 82, 62, 44, 85, 98, 69, 106, 76, 100, 51, 95, 53, 89, 66, 88, 95, 61,
        99, 49, 86, 47, 63, 44, 73, 78, 54, 88, 61, 92, 50, 83, 56, 32, 60, 49, 88,
        60, 94, 74, 78, 74, 38, 62, 55, 59, 19, 6,
    ]
    # A short bath-like burst, peak trailing-5 of 17.0 L/min -- the false
    # positive a single 100 L volume threshold would produce.
    BATH_L = [9, 14, 19, 14, 19, 13, 20, 12, 19, 13, 3]

    @pytest.fixture
    def gated(self, conn):
        conn.execute("UPDATE runaway_detector_config SET home_threshold_l = 250 WHERE metric = 'water'")
        conn.commit()
        return conn

    def test_the_run_fires_via_g3_nine_minutes_in(self, gated):
        start = _START.replace(hour=13, minute=11)
        t, episode = _first_firing(_drive_minutes(gated, start, self.SUSTAINED_RUN_L))
        assert episode is not None
        assert episode["fired_via"] == "G3"
        # Eight minutes after the first non-idle minute, i.e. the ninth
        # minute of flow -- 143 L in, long before the run ends.
        assert t == start + timedelta(minutes=8)
        assert episode["total_volume"] == pytest.approx(143.0)
        assert episode["peak_rate"] == pytest.approx(22.8)

    def test_the_bath_stays_silent(self, gated):
        start = _START.replace(hour=10, minute=30)
        polls = _drive_minutes(gated, start, self.BATH_L)
        assert _first_firing(polls) == (None, None)

    def test_the_bath_peaks_below_g3s_bar(self, gated):
        # Stated as its own assertion because it is the margin the whole
        # design rests on: 17.0 observed against a 20 L/min gate.
        start = _START.replace(hour=10, minute=30)
        _drive_minutes(gated, start, self.BATH_L)
        peak = max(
            runaway_detector.trailing_rate(
                gated, start + timedelta(minutes=i), start.strftime("%Y-%m-%d %H:%M:%S")
            )
            for i in range(len(self.BATH_L))
        )
        assert peak == pytest.approx(17.0)
        assert peak < runaway_detector._DEFAULT_GATES["g3_rate_l_min"]

    def test_the_bath_would_have_fired_under_the_old_single_threshold(self, gated):
        # Guards the claim, not the code: if this ever stops being true the
        # fixture has drifted and the regression above means less than
        # it looks like it does.
        assert sum(self.BATH_L) > 100


class TestTierTwoBackgroundLeak:
    """Tier 2 exists for the band Tier 1 is structurally blind to. A run
    survives only while every poll advances the meter by a full 0.1 L tick
    (~0.3 L/min); a 0.1 L/min wall pinhole ticks about once a minute
    against three polls a minute, so two polls in three kill the run and no
    volume threshold, however low, can ever see it. 0.1 L/min is 144 L/day.

    The night in these tests starts at 00:45 rather than 01:00 because a
    bucket is only measurable when the bucket before it also has readings
    -- there is nothing to compare the first one against otherwise."""

    NIGHT_START = _START.replace(hour=0, minute=45)
    PINHOLE_L_MIN = 0.1

    def _night(self, conn, minutes, litres_per_min=PINHOLE_L_MIN, gaps=()):
        _seed_span(conn, self.NIGHT_START, minutes, litres_per_min, gaps)

    def test_a_continuous_pinhole_trips_on_its_very_first_night(self, conn):
        self._night(conn, 166)  # 00:45 -> 03:30
        episode = runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=30))
        assert episode is not None
        assert episode["fired_via"] == "T2-night"
        assert episode["threshold_used"] == 2.5  # hours, not litres
        assert episode["started_at"].endswith("01:00:00")

    def test_fires_the_moment_the_window_is_satisfied_and_not_before(self, conn):
        self._night(conn, 166)
        # 01:00 -> 03:00 is 2.25 h of flowing buckets: under the bar.
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=0)) is None
        # 01:00 -> 03:15 is exactly 2.5 h.
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=15)) is not None

    def test_a_normal_night_never_fires(self, conn):
        # Two night-time draws -- a flush, and a glass of
        # water -- then nothing. An ordinary night must never reach 2.5 h.
        def flow(t):
            if _START.replace(hour=1, minute=5) <= t < _START.replace(hour=1, minute=8):
                return 6.0
            if _START.replace(hour=3, minute=20) <= t < _START.replace(hour=3, minute=22):
                return 4.0
            return 0.0

        self._night(conn, 256, flow)  # 00:45 -> 05:00
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=5, minute=0)) is None

    def test_one_dropped_bucket_is_bridged(self, conn):
        # A leak sitting near the meter's 0.1 L bucket resolution drops the
        # odd bucket through phase drift alone. Demanding an unbroken chain
        # would let exactly the smallest leaks reset the counter forever.
        def flow(t):
            quiet = _START.replace(hour=2, minute=0) <= t < _START.replace(hour=2, minute=15)
            return 0.0 if quiet else self.PINHOLE_L_MIN

        self._night(conn, 166, flow)
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=30)) is not None

    def test_two_dropped_buckets_break_the_streak(self, conn):
        def flow(t):
            quiet = _START.replace(hour=2, minute=0) <= t < _START.replace(hour=2, minute=30)
            return 0.0 if quiet else self.PINHOLE_L_MIN

        self._night(conn, 166, flow)
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=30)) is None

    def test_the_zero_budget_is_for_the_whole_streak_not_each_gap(self, conn):
        # An appliance that draws water for a few minutes once every half
        # hour -- a softener regen, an ice maker -- alternates
        # flow/idle/flow/idle. If the tolerance reset on every flowing
        # bucket it would bound only *consecutive* idle buckets, and this
        # pattern would sustain a "near-continuous" streak forever at a 50%
        # duty cycle.
        #
        # This is not hypothetical: measured over the same 2.7 years of
        # 15-minute history the thresholds were set from, the per-gap
        # reading fires T2-away on an ordinary domestic stretch where the
        # validated rule scores zero. The backtest harness counts the
        # budget per streak; so must this.
        def flow(t):
            return self.PINHOLE_L_MIN if (t.minute // 15) % 2 == 0 else 0.0

        self._night(conn, 166, flow)
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=30)) is None

    def test_a_data_gap_is_never_bridged(self, conn):
        # An outage is not an idle bucket. Bridging one would let a service
        # restart manufacture a leak out of the single reading that
        # straddles it -- the meter kept advancing, nothing recorded it.
        self._night(
            conn, 166,
            gaps=[(_START.replace(hour=2, minute=0), _START.replace(hour=2, minute=30))],
        )
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=30)) is None

    def test_nothing_is_evaluated_outside_the_overnight_window(self, conn):
        _seed_span(conn, _START.replace(hour=8, minute=45), 166, self.PINHOLE_L_MIN)
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=11, minute=30)) is None

    def test_one_episode_per_leak_not_one_per_poll(self, conn):
        self._night(conn, 166)
        for minute in (30, 31, 32):
            runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=minute))
        rows = conn.execute("SELECT COUNT(*) AS n FROM alert_episodes WHERE fired_via = 'T2-night'").fetchone()
        assert rows["n"] == 1

    def test_the_episode_closes_once_the_flow_stops(self, conn):
        self._night(conn, 166)
        runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=30))
        # An hour of genuine silence -- well past the one-bucket tolerance.
        _seed_span(conn, _START.replace(hour=3, minute=31), 60, 0.0)
        runaway_detector.check_water_background(conn, now=_START.replace(hour=4, minute=30))
        row = conn.execute("SELECT ended_at FROM alert_episodes WHERE fired_via = 'T2-night'").fetchone()
        assert row["ended_at"] is not None

    def test_a_closed_episode_with_the_same_start_is_resumed_not_collided_with(self, conn):
        # Guards the UNIQUE(kind, metric, started_at) path. A closed episode
        # this detector owns can share a start with a recomputed streak --
        # backfilled readings are the plausible route, since a bucket with
        # no readings is unmeasurable and breaks a streak outright, then
        # becomes flowing once the gap is filled and restores the original
        # start. Re-inserting would raise IntegrityError and return None,
        # leaving a live leak behind a closed row that notify() will not
        # escalate. The state is constructed directly here rather than
        # driven, because no simple poll sequence reaches it.
        self._night(conn, 166)  # 00:45 -> 03:30, start bucket 01:00
        conn.execute(
            "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, ended_at, "
            "peak_rate, total_volume, unit, threshold_used, occupancy_state, fired_via, alert_count) "
            "VALUES ('runaway', 'water', ?, ?, ?, 0.006, 9.0, 'L', 2.5, 'home', 'T2-night', 1)",
            (_START.replace(hour=1, minute=0).strftime("%Y-%m-%d %H:%M:%S"),
             _START.replace(hour=3, minute=0).strftime("%Y-%m-%d %H:%M:%S"),
             _START.replace(hour=3, minute=0).strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()

        resumed = runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=30))
        assert resumed is not None
        assert resumed["ended_at"] is None
        assert resumed["alert_count"] == 1  # keeps its escalation position
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM alert_episodes WHERE fired_via = 'T2-night'"
        ).fetchone()["n"] == 1

    def test_tier_two_never_touches_the_run_accumulator(self, conn):
        self._night(conn, 166)
        before = dict(conn.execute("SELECT * FROM detector_state WHERE metric = 'water'").fetchone() or {})
        runaway_detector.check_water_background(conn, now=_START.replace(hour=3, minute=30))
        after = conn.execute("SELECT * FROM detector_state WHERE metric = 'water'").fetchone()
        assert before == (dict(after) if after is not None else {})


class TestTierTwoAwayWindow:
    def _seed_away_days(self, conn, ref_date):
        for n in (1, 2):
            d = (ref_date - timedelta(days=n)).date().isoformat()
            conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES (?, 0.02)", (d,))
            conn.execute("INSERT INTO water_daily (date, usage_l) VALUES (?, 3)", (d,))
        conn.commit()

    def test_two_hours_of_background_flow_in_an_empty_house_fires_at_any_hour(self, conn):
        self._seed_away_days(conn, _START)
        _seed_span(conn, _START.replace(hour=7, minute=45), 196, 0.1)  # 07:45 -> 11:00
        episode = runaway_detector.check_water_background(conn, now=_START.replace(hour=11, minute=0))
        assert episode is not None
        assert episode["fired_via"] == "T2-away"
        assert episode["occupancy_state"] == "away"
        assert episode["threshold_used"] == 2.0

    def test_the_same_flow_with_the_house_occupied_does_not_fire_by_day(self, conn):
        # No away days seeded -- the overnight window is the only Tier 2
        # rule that applies while someone is home, and 11:00 is not in it.
        _seed_span(conn, _START.replace(hour=7, minute=45), 196, 0.1)
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=11, minute=0)) is None

    def test_water_used_before_the_house_emptied_does_not_count_toward_the_streak(self, conn):
        # is_away() is date-based and normally flips at midnight, but a
        # declared-occupied occupancy_log entry ending mid-day flips it on
        # the spot. An unclipped `now - (t2_away_hours + 1)` lookback then
        # reaches back into that occupied afternoon and counts a household's
        # ordinary water use as an empty-house leak. Unclipped it fires
        # twice, clipped it is silent -- and T2-away escalates on backoff precisely because
        # nobody is home to dismiss it.
        self._seed_away_days(conn, _START)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) VALUES (?, ?, 2)",
            (_START.replace(hour=8, minute=0).strftime("%Y-%m-%d %H:%M"),
             _START.replace(hour=17, minute=30).strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()

        # Continuous flow straddling the boundary: 14:00 -> 18:15, so the
        # trailing bucket is measurable and only the clip can silence this.
        _seed_span(conn, _START.replace(hour=14, minute=0), 256, 0.1)  # 14:00 -> 18:15

        assert runaway_detector.away_since(conn, _START.replace(hour=18, minute=15)) == \
            _START.replace(hour=17, minute=30)
        assert runaway_detector.check_water_background(
            conn, now=_START.replace(hour=18, minute=15)) is None

    def test_without_an_occupancy_boundary_the_same_flow_does_fire(self, conn):
        # The contrast that proves the clip is doing the work: identical
        # readings, no declared-occupied entry to clip against, so the full
        # 3 h lookback applies and 3.25 h of flow fires T2-away.
        self._seed_away_days(conn, _START)
        _seed_span(conn, _START.replace(hour=14, minute=0), 256, 0.1)  # 14:00 -> 18:15
        assert runaway_detector.away_since(conn, _START.replace(hour=18, minute=15)) is None
        episode = runaway_detector.check_water_background(
            conn, now=_START.replace(hour=18, minute=15))
        assert episode is not None
        assert episode["fired_via"] == "T2-away"

    def test_a_genuine_leak_after_the_house_empties_still_fires(self, conn):
        # The clip must not blind the detector -- once the window has
        # actually elapsed inside the empty period, T2-away fires normally.
        self._seed_away_days(conn, _START)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) VALUES (?, ?, 2)",
            (_START.replace(hour=8, minute=0).strftime("%Y-%m-%d %H:%M"),
             _START.replace(hour=17, minute=30).strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        _seed_span(conn, _START.replace(hour=17, minute=30), 166, 0.1)  # 17:30 -> 20:15
        episode = runaway_detector.check_water_background(
            conn, now=_START.replace(hour=20, minute=15))
        assert episode is not None
        assert episode["fired_via"] == "T2-away"

    def test_a_long_quiet_stretch_starts_a_genuinely_new_episode(self, conn):
        # The counterpart to the resume case below: when the gap is long
        # enough to break the streak outright, the recovered flow is a new
        # streak with a new start, and it must get its own episode rather
        # than being folded into the old one.
        self._seed_away_days(conn, _START)
        _seed_span(conn, _START.replace(hour=7, minute=45), 196, 0.1)   # 07:45 -> 11:00
        first = runaway_detector.check_water_background(conn, now=_START.replace(hour=11, minute=0))
        assert first is not None

        _seed_span(conn, _START.replace(hour=11, minute=1), 44, 0.0)    # 45 min of silence
        assert runaway_detector.check_water_background(conn, now=_START.replace(hour=11, minute=45)) is None

        _seed_span(conn, _START.replace(hour=11, minute=46), 135, 0.1)  # -> 14:00
        second = runaway_detector.check_water_background(conn, now=_START.replace(hour=14, minute=0))
        assert second is not None
        assert second["id"] != first["id"]
        assert second["started_at"] > first["started_at"]


class TestGateAwareDismissal:
    """Dismissing an alert as "not a leak" has to nudge the parameter that
    actually fired it. Under multi-gate firing the old behaviour -- always
    raise the volume threshold -- would silently degrade G1, the backstop,
    every time a *rate* gate misfired, leaving the gate that misfired
    exactly where it was."""

    def _episode(self, conn, fired_via, volume=200.0, peak_rate=0.0,
                 occupancy_state="home", minutes=0):
        started = _START
        conn.execute(
            "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, peak_rate, "
            "total_volume, unit, threshold_used, occupancy_state, fired_via, alert_count) VALUES "
            "('runaway', 'water', ?, ?, ?, ?, 'L', 0, ?, ?, 1)",
            (started.strftime("%Y-%m-%d %H:%M:%S"),
             (started + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
             peak_rate, volume, occupancy_state, fired_via),
        )
        conn.commit()
        return conn.execute("SELECT id FROM alert_episodes ORDER BY id DESC LIMIT 1").fetchone()["id"]

    def _config(self, conn):
        return dict(conn.execute("SELECT * FROM runaway_detector_config WHERE metric = 'water'").fetchone())

    def test_dismissing_g3_moves_the_rate_gate_not_the_volume_gate(self, conn):
        episode_id = self._episode(conn, "G3", volume=200.0, peak_rate=21.5)
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=True, now=_START)
        config = self._config(conn)
        assert config["g3_rate_l_min"] == pytest.approx(22.5)   # 21.5 + 1 L/min margin
        assert config["home_threshold_l"] == 100                # untouched
        assert config["g2_rate_l_min"] == 10.0                  # and so is the other rate gate

    def test_dismissing_g2_moves_only_g2(self, conn):
        episode_id = self._episode(conn, "G2", volume=200.0, peak_rate=11.0)
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=True, now=_START)
        config = self._config(conn)
        assert config["g2_rate_l_min"] == pytest.approx(12.0)
        assert config["g3_rate_l_min"] == 20.0
        assert config["home_threshold_l"] == 100

    def test_dismissing_g1_still_moves_the_volume_threshold(self, conn):
        episode_id = self._episode(conn, "G1", volume=118.0)
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=True, now=_START)
        assert self._config(conn)["home_threshold_l"] == pytest.approx(128.0)

    def test_a_pre_upgrade_episode_with_no_gate_recorded_is_treated_as_volume(self, conn):
        # fired_via is NULL on every episode written before the gates
        # existed, and those were all fired by the volume threshold.
        episode_id = self._episode(conn, None, volume=118.0)
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=True, now=_START)
        assert self._config(conn)["home_threshold_l"] == pytest.approx(128.0)

    def test_dismissing_a_night_alert_widens_the_night_window(self, conn):
        episode_id = self._episode(conn, "T2-night", minutes=165)  # 2.75 h observed
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=True, now=_START)
        config = self._config(conn)
        assert config["t2_night_hours"] == pytest.approx(3.0)  # 2.75 + one bucket
        assert config["t2_away_hours"] == 2.0
        assert config["home_threshold_l"] == 100

    def test_a_dismissal_can_only_ever_loosen_a_gate(self, conn):
        episode_id = self._episode(conn, "G3", peak_rate=5.0)  # far below the gate
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=True, now=_START)
        assert self._config(conn)["g3_rate_l_min"] == 20.0

    def test_a_plain_acknowledge_moves_nothing(self, conn):
        episode_id = self._episode(conn, "G3", peak_rate=21.5)
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=False, now=_START)
        assert self._config(conn)["g3_rate_l_min"] == 20.0


class TestTierTwoNotificationRouting:
    def _episode(self, conn, fired_via):
        conn.execute("UPDATE feature_toggles SET anomaly_alerts_enabled = 1 WHERE id = 1")
        conn.execute(
            "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, total_volume, unit, "
            "threshold_used, occupancy_state, fired_via, alert_count) VALUES "
            "('runaway', 'water', ?, ?, 22.5, 'L', 2.5, 'home', ?, 0)",
            (_START.strftime("%Y-%m-%d %H:%M:%S"), _START.strftime("%Y-%m-%d %H:%M:%S"), fired_via),
        )
        conn.commit()
        return conn.execute("SELECT id FROM alert_episodes ORDER BY id DESC LIMIT 1").fetchone()["id"]

    def test_t2_night_sends_once_and_then_stays_silent(self, conn, monkeypatch):
        # An overnight background leak is something to look at in the
        # morning, not a reason to nag the house hourly through the night.
        # The banner is still persistent -- only the Telegram nagging stops.
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message",
                            lambda conn, text: (calls.append(text) or ("sent", None)))
        episode_id = self._episode(conn, "T2-night")
        for hours in (0, 1, 2, 4):
            runaway_detector.maybe_notify(conn, episode_id, now=_START + timedelta(hours=hours))
        assert len(calls) == 1
        assert "background leak" in calls[0]

    def test_t2_away_keeps_escalating(self, conn, monkeypatch):
        # Nobody is home to see a banner, so this one keeps nagging.
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message",
                            lambda conn, text: (calls.append(text) or ("sent", None)))
        episode_id = self._episode(conn, "T2-away")
        for minutes in (0, 16, 47, 108):
            runaway_detector.maybe_notify(conn, episode_id, now=_START + timedelta(minutes=minutes))
        assert len(calls) == 4
        assert "while the house is empty" in calls[0]

    def test_a_tier_one_alert_still_reads_as_a_run_not_a_background_leak(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message",
                            lambda conn, text: (calls.append(text) or ("sent", None)))
        episode_id = self._episode(conn, "G3")
        runaway_detector.maybe_notify(conn, episode_id, now=_START + timedelta(minutes=9))
        assert "continuous flow since" in calls[0]
        assert "9 min" in calls[0]
