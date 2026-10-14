# OmniMeter — Design Specification

**Stack:** Python · Flask · SQLite · Chart.js
**Author:** T. Philip — <https://github.com/t-philip>
**Licence:** AGPL-3.0

This document describes how OmniMeter is built and why, for anyone reading or
extending the code. `README.md` covers installation and day-to-day use; this
covers the shape of the system and the reasoning behind the parts that are not
self-evident.

Where it says **MUST**, the requirement is load-bearing — violating it causes
data corruption, a security gap, or a silently wrong number on the dashboard.

---

## 1. Purpose and scope

A self-hosted dashboard for household power, gas, water and battery usage,
fed either by polling a smart meter on your LAN or by importing exported
files. It shows consumption over time, cost against your tariff, an estimated
solar self-sufficiency figure, and data-quality reporting over the history.

**Not in scope**

- **Not a home-automation platform.** No device control, no automations —
  read-only visualisation plus the configuration that feeds it.
- **No multi-user accounts.** One household, one dashboard. The write token
  (§6) is the only access control and is a shared secret, not per-user.
- **No cloud dependency.** Three outbound calls exist — weather (§5), an
  opt-in GitHub release check, and an opt-in Telegram leak alert (§11) —
  all off by default and independently gated.
- **No HTTPS or reverse proxy built in.** Put your own in front of it if you
  need it reachable beyond your LAN.

### 1.1 Terminology: meter vendor vs energy supplier

Two different third parties, easy to conflate because both are "brands" and
both can arrive as files you import. They never touch the same tables.

| Term | Means | Handled by | Writes to |
|---|---|---|---|
| **Meter vendor** | The hardware that reads your meter | `src/ingest.py`, `MeterDevice` implementations | `*_readings` |
| **Energy supplier** | The company that bills you | `src/tariff_parser.py` | `rate_schedule`, `gas_rate_schedule` |

Both are multi-brand. Your supplier and your meter are independent choices.

---

## 2. Architecture

```
  Meter (LAN, JSON)  ─┐
                      ├─→  *_readings  ─→  aggregate.rebuild_all()  ─→  *_daily  ─→  Flask API ─→ dashboard
  Exported CSV files ─┘    (raw)            picks one granularity        (rollups)
                                            per date, derives usage
```

Two independent writers feed the same raw tables, each on its own schedule and
each tolerant of the other being absent:

| Writer | Runs as | Granularity |
|---|---|---|
| `meter_poller` | long-running poller, ~20 s | `api_live` |
| `ingest_cli` | timer / container, 15 min | `15min` or `daily` |

`aggregate.rebuild_all()` is where they converge: it reads every granularity
present for a date, keeps only the highest-ranked one (§4.1), and rebuilds the
`*_daily` tables the dashboard queries. **Nothing renders directly from raw
readings.**

### 2.1 Meter readings are cumulative

Every stored power/gas/water value is a **cumulative meter total**, not
per-period usage. Usage is derived by differencing consecutive readings. This
applies to both intake paths and is the single most important thing to get
right when feeding OmniMeter from a new source — a per-interval figure would
be read as a meter that resets constantly.

---

## 3. Getting data in

Any meter brand works. HomeWizard needs no configuration; every other brand
takes one setup step, stating its own field or column names once.

### 3.1 Live polling — the `MeterDevice` contract

```python
class MeterDevice(Protocol):
    name: str
    def is_configured(self) -> bool: ...
    def fetch_measurement(self) -> dict: ...
```

A `Protocol`, not a base class, so any object of the right shape qualifies.
`ingest_all()` iterates devices, calls those two methods, and catches
`MeterDeviceAuthError` / `MeterDeviceConnectionError`. Each device's failure is
isolated — one unreachable meter never blocks the others.

Two implementations, dispatched by `protocol` in `devices.json` by
`device_registry.build_devices()`:

| `protocol` | Implementation | For |
|---|---|---|
| `v1`, `v2` | `HomeWizardDevice` | HomeWizard's own local API (§7) |
| `generic_json` | `GenericHttpDevice` | Any meter serving JSON over HTTP |

`GenericHttpDevice` takes a `field_map` mapping OmniMeter's canonical column
names to whatever that vendor's JSON calls them, with dotted paths for nesting
and numeric segments for array indices (`external.0.value`). That is the whole
per-brand configuration.

**No vendor auto-detection, deliberately.** Two brands can use the same key
name for different quantities — cumulative vs instantaneous, Wh vs kWh — so
sniffing the response shape would sometimes write plausible but *wrong*
numbers instead of failing. An explicit map cannot be wrong silently.

### 3.2 File import

Two formats, selected by filename:

- **HomeWizard export** (`Bat-`/`P1e-`/`P1g-`/`Water-` prefix) — that vendor's
  own filenames and column headers, recognised directly.
- **Vendor-neutral** (`omnimeter-<category>-*.csv`) — the header row names
  OmniMeter's own canonical columns. `GET /api/import/meter-csv/template`
  serves a commented template.

Files are re-ingested by content hash and rows keyed by `(time, granularity)`,
so re-importing a wider date range is safe and nothing duplicates.

The two formats differ deliberately in strictness. A vendor export legitimately
omits columns, so unknown headers are tolerated there. A vendor-neutral file is
written by hand against a published contract, so an unrecognised column name is
**rejected** — a typo is a mistake to report, not data to silently discard.

### 3.3 Unmapped and unrecognised values

A field OmniMeter has no reading for is stored as **NULL — never 0, never a
default**. Charts show a gap rather than a flat zero line, because "you
exported nothing" and "we don't know" are different claims.

If a response or file maps to *nothing at all*, that is a name mismatch rather
than a meter reading zero, and **a row of NULLs MUST NOT be written** — it
would be indistinguishable from a real reading of nothing. Both paths refuse:
the poller reports `field_mapping_error` listing the keys that did arrive, and
CSV import raises. A file uploaded through the web UI is then moved to
`data/imports/failed/`; one dropped into the dropzone directory stays where it
is and is reported on each scan, so fixing it in place is enough.

#### Export padding

A vendor export does not always honour that distinction: it can pad slots it
has no reading for with `0`. Because the meter columns are cumulative
counters, such a run is recognisable as padding rather than data and is
removed at ingest, at both ends of a file:

- **Leading run** — the meter had not registered anything yet (an export
  covering a period before it was installed). Collapsed to a single anchor
  row, which preserves the one genuine delta: a new meter counting up from
  zero for the first time.
- **Trailing run** — an export taken mid-period, padding the slots after its
  last real reading. Removed outright; nothing after the run has a delta that
  could depend on it, so no anchor is needed.

Both are lossless — consecutive zeros contribute no deltas — and each logs the
number of rows dropped. A run is only treated as padding when *every* value
column in the row is absent or zero, so a genuine reading of zero in one
column (an empty battery's state of charge, say) is never mistaken for it.
Zeros *between* real readings are left alone: those are a meter dropout, and
the cumulative-series glitch pass handles them.

---

## 4. Data model

| Table | Grain | Notes |
|---|---|---|
| `power_readings`, `gas_readings`, `water_readings`, `battery_readings` | `(time, granularity)` | Raw cumulative readings from every source |
| `power_daily`, `gas_daily`, `water_daily`, `battery_daily` | `date` | Derived rollups — the only thing the dashboard reads |
| `ingested_files` | filename | Content-hash ledger for idempotent import |
| `rate_schedule`, `gas_rate_schedule` | period | Tariffs; `period_end = '9999-12-31'` means open-ended |
| `pv_config`, `fiscal_year_config`, `feature_toggles` | singleton | Settings |
| `occupancy_log` | interval | Manually logged headcount; entries may overlap |
| `acknowledged_issues` | fingerprint | Dismissed data-quality findings |
| `weather_daily` | date | Open-Meteo cache |

### 4.1 Granularity ranking

```python
_GRANULARITY_RANK = {"live": 3, "15min": 2, "daily": 1}
```

For each date, only rows of the highest-ranked granularity present are used —
so a detailed source wins over a coarse one for the same day, rather than the
two being mixed or double-counted.

`api_live` is deliberately **absent** from the dict. An unranked granularity
defaults to rank 0, so live polling supplements the record without ever
overriding an imported source for a date it already covers. Promoting it is a
one-line change if you would rather trust the poller.

`live` is a legacy granularity no current writer produces; it is ranked so
that a database carrying such rows keeps resolving them correctly.

### 4.2 Composite primary keys **[MUST]**

Raw reading tables use `PRIMARY KEY (time, granularity)`, not `time` alone.
With a bare `time` key, two sources writing the same timestamp silently
overwrite each other through `INSERT OR REPLACE`, destroying data with no way
to recover it. `db.py` migrates older databases in place.

For the same reason, a column MUST mean one thing regardless of source:
`import_t1_kwh` is always tariff-1 only, and a combined figure goes in
`import_combined_kwh`. A column whose physical meaning changes with its source
produces plausible, badly wrong deltas at the boundary.

### 4.3 Timezone **[MUST]**

`src/localtime.py` resolves `OMNIMETER_TIMEZONE` once, and every other module
takes it from there. The zone decides where a day starts and ends, so it drives
reading timestamps, daily-total and chart boundaries, the weather day, and the
start date of any tariff document that states none.

The default is **UTC**, not the author's own zone — a local default buried in
shared code would silently mis-bucket every reading for a user elsewhere. An
unset value falls back to UTC with a logged warning; **an invalid name stops
the app at startup** rather than timestamping data in the wrong zone.

---

## 5. Weather-derived estimates

Two independent uses of the same daily Open-Meteo fetch (`weather_daily`),
covering different tabs. Neither adds a second network call — both are
derived from the one fetch driven by `weather_enabled`.

### 5.1 Solar estimation

Most inverters aren't reachable locally, so production is **estimated, never
measured**, and must always be labelled as such.

- **Default:** an annual figure (`kWp × 950 kWh/kWp/yr`, a typical NL specific
  yield) distributed by a fixed monthly curve. Clamped so the estimate can
  never imply less production than was verifiably exported. Its limitation is
  real: every day within a month gets the same figure.
- **With weather enabled:** the same annual total redistributed by measured
  solar radiation, giving the right day-to-day shape. Deliberately a
  redistribution rather than a physical model, so it cannot drift from a
  calibrated annual total and needs no panel orientation or shading data.

### 5.2 Gas heating-degree-day correlation

The Gas tab's chart carries a "Heating demand" rail (same visual mechanic as
the solar rail below), driven by heating-degree-days computed from
`temperature_2m_mean`: `max(0, 18°C − mean_temp)`, floored at zero on days
mild enough that heating wouldn't be running. 18°C is the NOAA/international
convention (65°F), a fixed constant rather than a per-install setting —
nothing in OmniMeter has grounds to tune it without the household's own
gas-vs-temperature data to calibrate against. Unlike solar production, this is
presented as **context alongside the real reading**, never a modelled
estimate of gas usage itself — no gas figure is derived or adjusted, the rail
only shows how cold each day was relative to what's typical for that date
(`src/weather.py`'s `typical_heating_degree_days_by_day_of_year`, the same
seasonal-median pooling section 5.1's "typical radiation" reference already
uses).

### 5.3 Network and licensing

Weather (used by both 5.1 and 5.2), the update check (`src/update_check.py`,
not otherwise documented in this spec) and the Telegram leak alert (§11) are
the app's only outbound calls, each gated off by default so a user opts in
knowingly. Weather coordinates
are rounded before use (~11 km by default, against a ~9 km source grid).
Open-Meteo data is CC BY 4.0 and the credit is shown wherever any
weather-derived value appears, solar or gas alike.

---

## 6. Security model

Every state-changing request MUST carry `X-OmniMeter-Write-Api-Token` matching
`OMNIMETER_WRITE_API_TOKEN`, compared with `hmac.compare_digest()`. The check
is on **HTTP method**, not a route allowlist, so a future write route is
protected by default. `create_app()` **fails closed** — no token in the
environment means it refuses to start rather than serving unauthenticated
writes.

**What this does and does not defend against, stated plainly.** The dashboard
embeds the token client-side so its own UI can write, which means anyone who
loads the page can read it. This is *not* protection against an attacker
already on your LAN. It stops accidental and automated writes from devices that
never render the page — a port scanner, a misconfigured script. **The
application's LAN-only design is the actual security boundary**; there is no
HTTPS, no reverse proxy and no user accounts in this codebase.

---

## 7. HomeWizard hardware notes

Undocumented behaviour found by testing against real devices. Recorded because
it explains why the TLS code looks unusual, and because anything else built on
the same hardware will hit it.

- **The device identity contains a slash** (`appliance/{type}/{serial}`), so it
  cannot be a URL host or a `curl --resolve` token at all. The client uses
  `urllib3` with `assert_hostname=`, a string comparison decoupled from URL
  parsing.
- **Certificate identity is not one scheme across devices.** The P1 meter
  carries its identity in the Subject **Common Name with no SAN**, which
  modern TLS ignores unless the caller builds an `SSLContext` with
  `hostname_checks_common_name=True` explicitly. The battery carries a DNS
  **SAN**, and once any SAN exists it is matched exclusively. No formula
  derives the right value across device types — each device's real certificate
  must be inspected and its identity stored per device.
- **Not every device offers API v2.** v2 is an experimental opt-in; v1 is the
  default and some units never get v2. Both are supported, and their key names
  differ for identical quantities, so the mappers accept either spelling.
- **Gas is not a separate device.** DSMR meters relay the gas reading through
  the P1 telegram, so it arrives inside the P1 response rather than from an
  endpoint of its own.

---

## 8. Frontend

Single page (`templates/index.html` + `static/js/dashboard.js`, Chart.js), one
tab per category, with optional tabs hidden via feature toggles.

- **One shared date range** across all chart tabs — presets, exact dates, or
  dragging across a chart to set the range (not a chart-only zoom), after which
  the step buttons advance by exactly that span.
- **Per-series toggles** below each multi-series chart, replacing Chart.js's
  text legend, whose click-to-toggle affordance is not obviously interactive.
  State resets on redraw, since each redraw recreates the chart.
- **Contextual help** — `HELP_TOPICS` in `dashboard.js` is the single source
  for both the "ⓘ" popovers and the Help tab, so they cannot drift apart.
- **English only, no translation layer.** With ~800 UI strings and no
  non-English users to validate against, a maintained translation risked
  shipping wrong terminology nobody could catch. Browsers' own page-translate
  handles static text for free, client-side. Chart canvases render pixels, so
  axis labels and tooltips are the one thing no translator can reach.

### 8.1 Quarters comparison

Comparison has two sub-tabs. *Date ranges* is the free two-period comparison;
*Quarters* compares **calendar quarters** (Q1 = Jan–Mar … Q4 = Oct–Dec) in two
modes — any two quarters side by side, or one quarter across every year that has
data (each year shown with its change from the previous year).

- **One implementation.** `src/quarters.py` builds every figure; the page only
  renders it. Endpoints: `/api/quarters/available`, `/api/quarters/compare?a=YYYY-Qn&b=YYYY-Qn`,
  `/api/quarters/series?q=1..4`. Deltas and percentages are computed server-side.
- **Cost is shared with the Costs tab.** `src/costs.py` holds the one per-day cost
  calculation used by both, so a quarter's cost always equals the sum of the days
  the Costs tab shows. Cost is energy only: no fixed or standing fees, and water
  has no cost.
- **Metrics:** power import/export/net, gas, water, solar production, power/gas/total
  cost, battery charge/discharge, average headcount, average temperature, heating
  degree days (`max(0, 18 °C − daily mean)`) and gas per heating degree day
  (computed only over days that have both gas and a temperature).
- **Totals and per-day.** Quarters are 90–92 days long and the current one is
  partial, so each metric also carries a per-day figure (total ÷ days that have
  data); metrics that are already averages or ratios keep their value.
- **Completeness is explicit.** Each metric reports `days_with_data`,
  `days_in_quarter`, `complete` and a list of `issues`. A metric is incomplete when
  some elapsed days have no data, the quarter is still in progress, cost days were
  priced with an expired rate, or the source is not set up (`available: false`).
  A metric with no data at all is `null`, never `0`.
- **Solar is optional.** Where the solar integration is not installed the metric
  is reported as not available rather than as zero.

**Verification.** The backend (quarter arithmetic, totals, per-day, deltas,
completeness, cost parity with the Costs tab, weather, routes) is covered by
`tests/test_quarters.py`. The page itself has no automated browser test; it was
checked by hand only.

---

## 9. Configuration

All deployment-specific values are environment-driven, with no default that
assumes a particular household, location or hardware. `.env.example` documents
each in full; `devices.json` holds per-device addresses and field maps.

The Docker Compose path reads `.env` directly and has no external secrets
dependency. A native-systemd install can source the same variable names from
elsewhere — the application neither knows nor cares which.

---

## 10. Testing and quality

`pytest` for tests, `ruff` for lint, `mypy` at baseline (not strict) settings —
all three expected to pass clean before any release. Run them with:

```
python -m pytest tests/ -q && python -m ruff check . && python -m mypy src/ scripts/ wsgi.py
```

Stated honestly: **there is no automated browser or end-to-end coverage.**
Every test exercises `src/` and `scripts/` directly or through Flask's test
client against a temporary SQLite database. UI regressions are caught by
manual review, not by the suite.

---

## 11. Runaway leak detection & alerting

> The tuning and validation data came from a single private installation and is not included in this repository, so these statements are design rationale rather than independently reproducible evidence.

A fast-path detector, independent of the outlier-report machinery in §4/§10 —
that report is deliberately informational and pull-based (a flagged-but-
legitimate day costs nothing); this is push-based and interrupts, so it
exists as its own, separately-reasoned system rather than an extension of it.
Currently implemented for **water only** — gas and power need a different
discriminator (see the design notes referenced in the module docstrings) and
are future work, not yet built.

### 11.1 Discriminator — three signals, not one number

Not volume-per-time-window and not duration alone. A run — an unbroken chain of
consecutive non-idle polls — is described by **three** signals: its elapsed
**duration**, its accumulated **volume**, and its **sustained rate**, defined as
the mean L/min over the trailing 5 minutes. Three gates read those signals and
are **OR'd**; the first one satisfied opens the episode.

| Gate | Rule (home mode) | What it catches |
|---|---|---|
| **G1** | `volume ≥ home_threshold_l` (250 L) | Anything relentless enough to evade the other two. The backstop. |
| **G2** | `duration ≥ 15 min` **and** `trail5 ≥ 10 L/min` | A fixture left running — moderate rate, should have stopped |
| **G3** | `duration ≥ 5 min` **and** `trail5 ≥ 20 L/min` | Burst pipe, failed valve — caught before volume piles up |

**Why three and not one.** An earlier version of this detector fired on a single
volume threshold. A bath and a burst pipe have the same flow rate; what separates
them is that a bath stops. No single number can express that, and a single
volume threshold misfires on a bath. The gates instead describe different
*shapes* of event: an ordinary domestic run is either fast but
short (a bath) or long but slow (a slow-filling fixture), and each is declined
by a *different* gate's conditions.

**MUST**: the sustained rate is a **trailing** mean, never a run average. A real
event can open with a low-rate trickle before full flow begins; a run average
stays diluted by that lead-in and crosses any bar minutes late.

**MUST**: while a run is shorter than the trailing window
the mean is taken over what exists and is never padded with zeros — a 3-minute
run cannot claim a 5-minute sustained rate it has not earned.

**Two choices that look wrong and are not**, recorded here because both are the
kind of thing a later reader will want to "fix":

- **G1 carries no duration qualifier.** Adding `and duration > 20 min` looks like
  cheap insurance. It is not: no ordinary domestic run is expected to approach
  250 L, so nothing domestic reaches it. A duration term cannot prevent a
  false positive that cannot happen — it can only delay a true one, letting more
  water escape for zero change in false positives.
- **G3's bar is 20 L/min, not 30.** The bar sits above the sustained rate of
  ordinary fixtures, with margin. Raising it further
  gives away minutes of spill and catches nothing extra.

Away mode is unchanged and deliberately **not** endorsed: a single volume gate at
`away_threshold_l` (10 L). Away mode has not been exercised against an
empty-house dataset, so the rate gates have never been tested for it; the inherited threshold stays until there is data to tune it against.

Checked every poll cycle (`src/runaway_detector.check_water`, called from
`meter_poller` right after `ingest_all()`), not on a rollup — the poller already
holds the freshest reading, so detection latency is bounded by the poll interval
rather than by any aggregation settling later.

**No new state is stored for any of this.** `meter_ingest` stamps `api_live` rows
to the minute while the poller runs every 20 s, so three polls upsert the same row
and `water_readings` already *is* the 1-minute series the gates were validated
against. The trailing rate is two indexed lookups against it — `(latest −
baseline) ÷ the minutes between them` — so a restart mid-run loses nothing and
`detector_state` needs no rolling buffer.

### 11.2 Tier 2 — the slow leak Tier 1 cannot see

Tier 1 has a floor no threshold change can lower, and it is high. A run survives
only if **every** poll advances the meter by at least one 0.1 L tick — about
**0.3 L/min, or 430 L/day**. Below that most polls read zero, the run resets, and
volume never accumulates. A 0.1 L/min wall pinhole (144 L/day) ticks the meter
roughly once a minute against three polls a minute, so two polls in three kill the
run: G1's 250 L is not "slow to reach" for such a leak, it is **unreachable**.
Lowering it to 10 L would change nothing.

`check_water_background()` therefore watches a different unit entirely: fixed
**15-minute buckets**, where a bucket is *flowing* if the meter advanced at all
within it. Its floor is set by the bucket rather than the poll — 0.1 L per 15 min
≈ 0.007 L/min, reliable from about 0.02 L/min (30 L/day).

| Window | Condition |
|---|---|
| **T2-night** | ≥ 2.5 h of flowing buckets inside 01:00–06:00 |
| **T2-away** | ≥ 2 h of flowing buckets while away, at any hour |

Both tolerate **one** bridged idle bucket. That setting is what decides how small a
leak Tier 2 can see: a bucket registers only once the meter advances a full 0.1 L,
so a leak near that resolution boundary drops the odd bucket through phase drift
alone, and demanding an unbroken chain would let precisely the smallest leaks
reset the counter forever. At these window lengths bridging is free — both
settings are designed to raise no false alerts on ordinary domestic use, with the
longest legitimate runs expected to sit below both windows.

**MUST**: a *missing* bucket is not an idle bucket. A bucket with no readings, or
whose predecessor has none, is unmeasurable and breaks a streak outright, where an
idle bucket only spends the one-bucket tolerance. Bridging a data gap would let a
service restart manufacture a leak out of the single reading that straddles it.

The two tiers are not alternatives. Tier 1 covers above ~0.3 L/min, Tier 2 covers
0.02–0.3 L/min, and a genuinely continuous leak fills the whole overnight window
and so trips on its **first night**. A daytime "≥5 h of continuous flow" backstop
was considered and **rejected**: 3 h of continuous flow at some point in the day
is a normal event in a household, and anything continuous
enough to run 5 h by day is running at night too, where T2-night already has it.

### 11.3 Occupancy gating

Two thresholds per metric (`runaway_detector_config`): a home threshold and a
much lower away threshold. Away-mode engages once a trailing run of consecutive
near-zero gas+water days (reusing `aggregate.find_away_days`, §4/§10's own absence
detector — not reimplemented) precedes today, **or** relaxes immediately given a
declared `occupancy_log` entry covering the present moment with a non-zero
headcount.

**MUST**: away-mode is derived only from *settled* daily rows and *declared*
occupancy, never from the live reading currently being evaluated. Inferring "away"
from low consumption is circular — a leak is itself consumption — and would relax
protection at exactly the moment it should tighten.

### 11.4 Persistence and delivery

Two tables besides the config above: `detector_state` (one row per metric, the
in-progress run — survives a service restart, resuming a run only if its last
sample is recent, otherwise resetting rather than resuming a possibly-dead run)
and `alert_episodes` (one row per event that satisfied a gate, `UNIQUE (kind,
metric, started_at)`). Tier 2 shares the episode table but **not**
`detector_state`: it holds no run state at all, being a query over buckets that
already exist.

`alert_episodes.fired_via` records which gate opened an episode — `'G1'`, `'G2'`,
`'G3'`, `'T2-night'`, `'T2-away'`. It is load-bearing, not cosmetic: it is what
the dismissal-nudge below dispatches on. Gates are evaluated most-specific-first
(G3, G2, then G1) so an episode is attributed to the gate that best describes it,
and `fired_via` is never rewritten on later polls — it records what *opened* the
episode. `threshold_used` holds that gate's own operative value (litres for G1,
L/min for G2/G3, hours for Tier 2), so it is only interpretable together with
`fired_via`. Episodes written before this feature existed carry `fired_via =
NULL`, which every consumer reads as "the legacy volume gate"; they are
deliberately not back-filled, since asserting a gate retroactively would be a
guess dressed up as a record.

Two delivery channels with different jobs, both fed from the same
`alert_episodes` row:

- **An in-app banner** (`GET /api/alerts/active`) — reliable, local, cannot
  silently fail. Persists until acknowledged, so an event that resolved
  unseen is still shown on return.
- **Telegram** (`src/telegram_notifier.py`) — fast and reaches you away from
  the dashboard. **MUST**: the database write happens first and always
  succeeds; Telegram is attempted after, on a short timeout, and its outcome
  (`notify_status`/`notify_error`) is recorded rather than swallowed — a
  failed send is retried on the *next poll cycle*, not lost and not deferred
  to the escalating backoff below. This is a deliberate departure from
  `update_check.py`'s "degrade silently" precedent (§5.3): silently dropping
  a leak alert is indistinguishable from "no leak," which defeats the
  feature's own purpose.

A successful send escalates its own repeat cadence (immediate, then +15 min,
+30 min, then hourly) so an unattended leak keeps nagging without spamming.
**T2-night is the one exception**: it sends **once** and then stays silent. An
overnight background leak is something to look at in the morning, not a reason to
nag the house hourly through the night — and its banner is still persistent, so it
is un-nagged, not missable. T2-away keeps the full escalation, since nobody is
home to see a banner.

Acknowledging an episode stops it. Acknowledging specifically as "not a leak"
also nudges **the parameter that fired it** just past the value that fired it —
G1 raises the volume threshold, G2/G3 raise their own rate bar, a Tier 2 gate
widens its own window. **MUST**: that nudge dispatches on `fired_via`. Always
raising the volume threshold, as the single-gate version did, would silently
degrade G1 — the backstop — every time a *rate* gate misfired, while leaving the
gate that actually misfired exactly where it was. `MAX()` means a dismissal can
only ever loosen a gate, never tighten one.

The gate parameters live in `runaway_detector_config` alongside the two volume
thresholds and are reported by `GET /api/settings/anomaly`, but unlike those two
they are **not** editable from Settings. They are self-tuning through the
dismissal path above, which adjusts whichever gate actually misfired — a better
mechanism than hand-editing a number whose safe range was established from a
deployment's own history.

### 11.5 Configuration and secrets

Same secret-handling pattern as every other credential in this app: the
Telegram bot token (`OMNIMETER_TELEGRAM_BOT_TOKEN`) is environment-only and
never written to the database. The chat ID is not a secret, so it is
DB-editable in Settings, falling back to `OMNIMETER_TELEGRAM_CHAT_ID` for a
self-hoster who configures everything from `.env`. The whole feature is
opt-in (`anomaly_alerts_enabled`, default off, same reasoning as §5.3) —
enabling it is this app's third outbound network call.

### 11.6 Health & staleness monitoring (Layer 3)

A second, independent detector sharing the same `alert_episodes` table and
delivery machinery (`src/alert_delivery.py`, factored out of §11.4 once
this became its second caller): `src/health_monitor.py`, run hourly by its
own timer (`scripts/health_check.py`), never from inside `meter_poller` —
a stalled poller is exactly the failure this exists to catch, so it cannot
depend on that same poller to run it.

**Rule, deliberately simple (no ML):** for each metric that has ever
produced a live (`api_live`) reading, if the gap since the most recent one
exceeds 90 minutes, that metric is stale. A metric with no live-reading
history at all (no device configured for it, or a household with no gas
connection) is never checked — there is nothing to be stale relative to,
and device configuration is deliberately not consulted directly for this
decision (`devices.json` can change; the readings table is the ground
truth for "has this ever actually worked").

`alert_episodes.kind = 'health'` reuses the schema's generic `metric`/
`unit`/`total_volume` columns with a different meaning than a Layer 1 row:
`total_volume` holds **minutes stale**, not litres, and `unit = 'min'`.
Both the API (`?kind=` filter on `/api/alerts/active` and `/api/alerts/
history`) and the frontend read `kind` to render and route each row
correctly — the Settings → Leak alerts history table filters to
`kind=runaway` only, so a health row's "minutes" figure is never shown
under a "Volume (L)" column.

**MUST:** the "not a leak" threshold-nudge (§11.4) is Layer 1-specific and
must never fire for a `kind='health'` episode — a health episode can
legitimately share the same `metric` value (e.g. `'water'`) as a Layer 1
episode, and applying the nudge there would silently corrupt the wrong
household's leak threshold. `app.py`'s acknowledge endpoint checks `kind`
before deciding whether to apply it.

### 11.7 Context-aware anomaly model (Layer 2)

`src/anomaly_model.py`, run nightly by its own timer (`scripts/anomaly.py`),
independent of both Layer 1 and Layer 3. Unlike them, Layer 2 is a
**digest, not an interrupt**: it never touches the persistent
OK-required banner.

**What it predicts:** each metric's (water/gas/power) DAILY total, as a
function of its own context — cyclic day-of-week and day-of-year, occupancy
headcount (`occupancy_log`, backfilled for training from
`aggregate.find_away_days()` when no log entry exists), and metric-specific
weather/EV context (water: mean temperature; gas: heating-degree-days; power:
heating-degree-days, solar irradiance, and an EV-charging-day flag reused
from `aggregate.find_ev_charge_days()`, never reimplemented).

**Model:** ridge regression on standardized features, fit fresh every night
from scratch (no incremental update) via a pure-Python normal-equations
solve — no numpy/scikit-learn dependency, deliberate given the app's
existing dependency footprint and a small host. A day is flagged when
its actual value exceeds the model's prediction by more than the 99th
percentile of that metric's own historical (training) residuals — a
household- and metric-relative number, not a hardcoded one, following the
same principle as every other tunable constant in this app (§11.4's runaway
thresholds, `aggregate.py`'s outlier-detector constants).

**Degrades gracefully, by design:** below 90 days of usable settled history,
or wherever a required weather field is missing for a metric that needs it,
training simply produces nothing for that metric and no detection runs —
`aggregate.find_outlier_days()`'s independent ratio-based detector is
completely unaffected either way.

**Never contaminates its own baseline:** each night's model is trained on
everything strictly before the day about to be scored (not just before
"today") — the day under judgment is deliberately excluded from its own
training set, so one anomalous day cannot inflate its own expected value
and residual threshold enough to mask itself.

**Schema note:** `kind='context'` rows in `alert_episodes` repurpose the
generic columns a third way (see §11.6's note on `kind='health'` doing this
a second time): `total_volume` is the day's actual value, `peak_rate` is the
model's expected value (otherwise meaningless for a daily total), and
`threshold_used` is the absolute cutoff (expected + residual threshold) that
triggered the flag. Every `kind='context'` row is written already
`acknowledged_at`-set — this is what keeps it out of `/api/alerts/active`
(and so out of the banner) with no frontend change, while remaining a real,
queryable row in `/api/alerts/history` for later audit or threshold
retuning.

`GET /api/anomaly/models` exposes each metric's training status (`metric`,
`trained_at`, `training_rows`, `residual_threshold`) for external monitoring
or scripting -- deliberately excludes the fitted coefficients/feature
means/stds, which are internal model parameters with no meaning outside
`predict()` itself.

---

## 12. Known limitations

1. **No automated browser/e2e tests** (§10).
2. **`mypy` runs at baseline, not strict** — clean, but a weaker guarantee
   than a strict pass.
3. **Solar production is an estimate**, flat within a month unless weather is
   enabled, and never a measurement.
4. **Only HomeWizard is zero-configuration.** Other brands work through a
   field map or the vendor-neutral CSV, but you must state your meter's names
   once. The generic HTTP path has been verified end-to-end against a live
   meter — but that meter was a HomeWizard addressed as a generic endpoint,
   so quirks specific to another manufacturer's API remain unknown.
5. **Partial name mismatches are silent.** A wholly unrecognised file or
   response is rejected (§3.3), but one matching *some* expected names writes
   those and leaves the rest NULL. The vendor-neutral CSV format rejects
   unknown columns; the HomeWizard-format path cannot, because a real export
   legitimately omits columns.
6. **No HTTPS, reverse proxy, or user accounts** (§6).
7. **Leak detection is water-only, and the gates were tuned on a single
   installation** (§11). The specific limits are worth stating plainly,
   because the numbers look more universal than they are:
   - Gates were tuned on limited data from a single installation; no
     confirmed physical leak has been used to test them; the
     high-resolution series is short and seasonally narrow; away-mode and
     DST behaviour are not covered by the tuning data.
   - **The Tier 1 gates rest on a short series.** Only the 1-minute series
     can resolve continuous runs. The much longer set behind Tier 2 is
     15-minute data and cannot extend them.
   - **Away-mode Tier 1 is unvalidated.** The 10 L away threshold is
     inherited from the previous design, not endorsed by this one.
   - **Production is less sensitive than the tuning data at low flows**
     (§11.2) — immaterial for gates set at ≥10 L/min or 250 L, and the whole
     reason Tier 2 exists.
   - **Neither tier handles the DST transition, and T2-night brackets it.**
     Readings are keyed on a *local* timestamp (`PRIMARY KEY (time,
     granularity)`), so on the autumn fall-back the repeated 02:00–02:59
     upserts over the first pass and an hour of readings is overwritten;
     Tier 1 is separately blind for that hour, because its "latest reading"
     query orders lexicographically and keeps returning the pre-fold row.
     In spring the missing hour reads as absent buckets, which break a Tier
     2 streak. Both directions fail toward **silence**, never toward a false
     alert, so this is a once-a-year one-hour detection gap rather than a
     correctness risk — but the overnight window is 01:00–06:00, which is
     exactly where the transition lands. Fixing it properly means bucketing on
     UTC internally and rendering local.

   The away-mode day count and the dismissal-nudge margins ship as reasoned
   defaults, not re-proven against further households. Gas and power runaway
   detection are not yet built.
8. **The 90-minute health-check staleness threshold (§11.6) is a flat
   constant, not per-install tunable.** Reasonable for the live-polling
   path this is built to watch (readings normally land every ~20s), but a
   self-hoster relying only on the 15-minute CSV dropzone for a given
   metric would see it flagged "stale" as a matter of course between
   drops — Layer 3 is really scoped to "is the live poller demonstrably
   still working," not general data freshness (that is `/api/data-health`'s
   job, pull-based, no alerting).
9. **The context-aware model (§11.7) predicts DAILY totals only, not an
   hourly figure** — a deliberate v1 scope decision, since its own job (slow
   drift: a degrading boiler, a creeping standby baseline) matches a daily
   cadence and the existing daily outlier detector it extends, not an
   intra-day one. It also does not yet model tariff period as a separate
   feature (redundant with the day-of-week/season terms already present for
   a day/night tariff structure), and its residual
   percentile (99th) is a single Python constant, not yet DB-editable
   per household the way the Layer 1 runaway thresholds are — a natural
   follow-up once real digest output shows whether the default needs
   adjusting.

---

## 13. Licensing

- **Code:** [AGPL-3.0](../LICENSE) — chosen for its network clause: a modified
  version offered to others over a network must offer them its source too.
- **Weather data:** Open-Meteo, CC BY 4.0, credited wherever displayed.
