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
- **No cloud dependency.** Two outbound calls exist — weather (§5) and an
  opt-in GitHub release check — both off by default and independently gated.
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

Weather (used by both 5.1 and 5.2) and the update check (`src/update_check.py`,
not otherwise documented in this spec) are the app's only two outbound calls,
each gated off by default so a user opts in knowingly. Weather coordinates
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

## 11. Known limitations

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

---

## 12. Licensing

- **Code:** [AGPL-3.0](../LICENSE) — chosen for its network clause: a modified
  version offered to others over a network must offer them its source too.
- **Weather data:** Open-Meteo, CC BY 4.0, credited wherever displayed.
