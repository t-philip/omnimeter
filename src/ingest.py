"""Scan the CSV dropzone and load HomeWizard P1 export files into SQLite.

Filenames follow the HomeWizard export tool's own convention:
    Bat-<start>-<end>.csv    battery (Import/Export kWh, State of charge %)
    P1e-<start>-<end>.csv    power   (Import/Export T1/T2 kWh, L1/L2/L3 max W)
    P1g-<start>-<end>.csv    gas     (Total gas used, m3)
    Water-<start>-<end>.csv  water   (water usage dl)

Two shapes of file exist: per-year 15-minute exports, and one long-range
daily rollup spanning the household's full history (the only source for
periods before the first 15-minute export). Both are ingested as-is, tagged
with a `granularity` column; rollup queries prefer 15-minute data when both
exist for the same date (see aggregate.py).
"""

import csv
import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

FILENAME_RE = re.compile(r"^(Bat|P1e|P1g|Water)-")

CATEGORY_BY_PREFIX = {
    "Bat": "battery",
    "P1e": "power",
    "P1g": "gas",
    "Water": "water",
}

# Vendor-neutral import. The HomeWizard-format path above is keyed to one
# vendor's export tool (its filenames AND its column headers), so a meter of
# any other brand had no way into this app at all. This format is the way in:
# the caller states the category in the filename and names this app's own
# canonical columns in the header row, so no vendor-specific parsing is
# involved and no new code is needed per brand.
#
# Deliberately a separate filename shape rather than sniffing the header of
# every file: a file declares which contract it is written against, so a
# malformed generic file reports generic errors instead of being silently
# judged against HomeWizard's rules (or vice versa).
GENERIC_FILENAME_RE = re.compile(r"^omnimeter-(power|gas|water|battery)-", re.IGNORECASE)

# category -> the value columns a generic CSV may carry. 'time' is required
# in addition and handled separately. These are exactly the *_readings
# columns, so the format needs no translation layer of its own.
GENERIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "power": (
        "import_t1_kwh",
        "import_t2_kwh",
        "import_combined_kwh",
        "export_t1_kwh",
        "export_t2_kwh",
        "export_combined_kwh",
        "l1_max_w",
        "l2_max_w",
        "l3_max_w",
    ),
    "gas": ("total_gas_m3",),
    "water": ("water_usage_dl",),
    "battery": ("import_kwh", "export_kwh", "soc_pct"),
}


_GENERIC_TABLE = {
    "power": "power_readings",
    "gas": "gas_readings",
    "water": "water_readings",
    "battery": "battery_readings",
}


def generic_insert_sql(category: str) -> str:
    """Built from GENERIC_COLUMNS rather than reusing INSERT_SQL above, which
    is deliberately narrower: the HomeWizard power export has no combined
    import/export column (that meter splits everything by T1/T2 tariff), so
    its INSERT names only 7 value columns. A generic meter often has the
    opposite shape -- a single combined total and no tariff split at all,
    which is the normal case outside a dual-tariff market -- so the generic
    format must be able to write import_combined_kwh/export_combined_kwh.
    Generating the statement from the same tuple the validator checks
    against also means the two can never drift apart."""
    columns = GENERIC_COLUMNS[category]
    col_list = ", ".join(["time", *columns, "granularity"])
    placeholders = ", ".join(["?"] * (len(columns) + 2))
    return f"INSERT OR REPLACE INTO {_GENERIC_TABLE[category]} ({col_list}) VALUES ({placeholders})"


class GenericCsvError(ValueError):
    """A generic-format CSV that cannot be trusted -- rejected outright
    rather than partially imported, matching parse_tariff_csv's rule and for
    the same reason: a hand-built file is far more likely to contain a
    mistake than a vendor's own export, so partial acceptance hides typos
    instead of tolerating them."""


def detect_generic_category(filename: str) -> str | None:
    m = GENERIC_FILENAME_RE.match(filename)
    return m.group(1).lower() if m else None


def detect_category(filename: str) -> str | None:
    generic = detect_generic_category(filename)
    if generic is not None:
        return generic
    m = FILENAME_RE.match(filename)
    return CATEGORY_BY_PREFIX[m.group(1)] if m else None


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def detect_granularity(rows: list[dict]) -> str:
    """15-minute exports have consecutive rows 15 min apart; the long-range
    export has one row per day. Sampling the first two rows is enough since
    each file is uniformly one shape or the other (confirmed against every
    real export seen)."""
    times = [r["time"] for r in rows[:2] if r.get("time")]
    if len(times) < 2:
        return "15min"
    t0 = datetime.strptime(times[0], "%Y-%m-%d %H:%M")
    t1 = datetime.strptime(times[1], "%Y-%m-%d %H:%M")
    return "daily" if (t1 - t0) >= timedelta(hours=1) else "15min"


def _battery_rows(rows, granularity):
    for r in rows:
        yield (
            r["time"],
            parse_float(r.get("Import kWh")),
            parse_float(r.get("Export kWh")),
            parse_float(r.get("State of charge %")),
            granularity,
        )


def _power_rows(rows, granularity):
    for r in rows:
        yield (
            r["time"],
            parse_float(r.get("Import T1 kWh")),
            parse_float(r.get("Import T2 kWh")),
            parse_float(r.get("Export T1 kWh")),
            parse_float(r.get("Export T2 kWh")),
            parse_float(r.get("L1 max W")),
            parse_float(r.get("L2 max W")),
            parse_float(r.get("L3 max W")),
            granularity,
        )


def _gas_rows(rows, granularity):
    for r in rows:
        yield (r["time"], parse_float(r.get("Total gas used")), granularity)


def _water_rows(rows, granularity):
    for r in rows:
        yield (r["time"], parse_float(r.get("water usage dl")), granularity)


def generic_rows(rows: list[dict], granularity: str, category: str) -> Iterator[tuple]:
    """Rows of a generic-format CSV, validated strictly.

    Unlike the HomeWizard-format readers above (which use .get() and let an
    absent column become None), an unrecognized header here is an ERROR. A
    generic file is hand-built or script-built by its user against a
    published contract, so a misspelled column is a mistake to report, not a
    field to silently drop -- the opposite trade-off from a vendor export,
    where a missing column genuinely means "this export doesn't include
    that". Omitting a valid column is still fine; naming an invalid one is
    not."""
    allowed = GENERIC_COLUMNS[category]
    if not rows:
        return
    header = set(rows[0].keys()) - {None}
    if "time" not in header:
        raise GenericCsvError(f"missing required 'time' column; found: {', '.join(sorted(header)) or '(none)'}")
    unknown = sorted(header - {"time"} - set(allowed))
    if unknown:
        raise GenericCsvError(
            f"unrecognized column(s) for category '{category}': {', '.join(unknown)}. "
            f"Valid columns are 'time' plus any of: {', '.join(allowed)}"
        )
    present = [c for c in allowed if c in header]
    if not present:
        raise GenericCsvError(
            f"no value columns for category '{category}' -- 'time' alone carries no reading. "
            f"Add at least one of: {', '.join(allowed)}"
        )
    for i, r in enumerate(rows, start=2):  # start=2: row 1 is the header
        time_value = (r.get("time") or "").strip()
        if not time_value:
            raise GenericCsvError(f"row {i}: blank 'time'")
        yield (time_value, *[parse_float(r.get(c)) if c in header else None for c in allowed], granularity)


RowGenerator = Callable[[list[dict], str], Iterator[tuple]]

# category -> (INSERT SQL, row-tuple generator)
INSERT_SQL: dict[str, tuple[str, RowGenerator]] = {
    "battery": (
        "INSERT OR REPLACE INTO battery_readings "
        "(time, import_kwh, export_kwh, soc_pct, granularity) VALUES (?, ?, ?, ?, ?)",
        _battery_rows,
    ),
    "power": (
        "INSERT OR REPLACE INTO power_readings "
        "(time, import_t1_kwh, import_t2_kwh, export_t1_kwh, export_t2_kwh, "
        "l1_max_w, l2_max_w, l3_max_w, granularity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _power_rows,
    ),
    "gas": (
        "INSERT OR REPLACE INTO gas_readings (time, total_gas_m3, granularity) VALUES (?, ?, ?)",
        _gas_rows,
    ),
    "water": (
        "INSERT OR REPLACE INTO water_readings (time, water_usage_dl, granularity) VALUES (?, ?, ?)",
        _water_rows,
    ),
}


def read_csv_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _all_values_none(values: list[tuple]) -> bool:
    """True if every value column, across every row, is None -- the
    signature of a HomeWizard export whose header no longer matches the
    column names this ingester expects (each r.get(...) call in the row_fn
    generators silently returns None for a renamed/missing column). Without
    this check such a file "ingests successfully" -- rows written, hash
    recorded, exit 0 -- while contributing nothing, with no error raised.
    time (first element) and granularity (last) are never None by
    construction, so only the middle elements need checking."""
    return all(v is None for row in values for v in row[1:-1])


def _is_empty_row(row: tuple) -> bool:
    """Every value column is absent or exactly zero. time (first element) and
    granularity (last) are positional, never values -- same slice convention
    as _all_values_none above."""
    return all(v is None or v == 0 for v in row[1:-1])


def strip_leading_empty_rows(values: list[tuple]) -> tuple[list[tuple], int]:
    """Collapse a leading run of all-zero readings down to a single anchor
    row. Returns (kept_rows, dropped_count).

    These columns are cumulative counters, so a leading zero means the meter
    had not registered anything yet -- it was absent, or not reporting. Those
    days are "no data", but stored as-is they become *_daily rows asserting
    "0 units used", which is a different and wrong claim. This came up in
    practice: a water meter installed some time after the P1 power/gas
    meter, where the long-range export padded the pre-installation period with
    zeros so both series would start on the same date -- 699 daily rows plus
    ~321 more days of 15-minute rows, all 0.0, until the counter first moved.
    Any user importing history from before their meter existed hits the same
    thing.

    This is LOSSLESS, not a deletion: consecutive zeros produce zero deltas,
    so dropping them changes no total. The single retained anchor preserves
    the one delta that *is* real -- a genuinely new meter counting up from
    zero for the first time. Without the anchor that first reading's usage
    would be silently lost, which is why the run is collapsed rather than
    removed outright."""
    first_real = next((i for i, row in enumerate(values) if not _is_empty_row(row)), None)
    if first_real is None:
        # Nothing in this file ever moved -- keep one row as a future anchor.
        return (values[-1:], max(0, len(values) - 1)) if values else ([], 0)
    if first_real == 0:
        return values, 0
    return values[first_real - 1 :], first_real - 1


def strip_trailing_empty_rows(values: list[tuple]) -> tuple[list[tuple], int]:
    """Drop a trailing run of all-zero readings. Returns (kept_rows,
    dropped_count). Mirror of strip_leading_empty_rows above, for the other
    end of the file.

    An export taken mid-day pads its remaining slots with zeros. Because
    these columns are cumulative counters, a reading of exactly 0 *after* a
    nonzero one is not a value the meter can produce -- a counter only
    returns to zero on replacement, and a replacement then counts back up,
    which would leave nonzero rows after the run and so not a trailing run
    at all. A trailing all-zero run is therefore unambiguously padding.

    Stored as-is it reads as one enormous negative delta. The rollup already
    skips negative deltas so totals stay exact, but the per-granularity
    diagnostic sees a drop with no recovery inside the series and reports a
    phantom meter reset. This came up in practice: a water export taken
    mid-day padded its remaining fifty-odd fifteen-minute slots with 0 and
    produced exactly that false positive. Any user exporting a period before
    it has fully elapsed hits the same thing.

    Unlike the leading case this needs NO anchor row. A leading run has to
    keep one, because the first real reading's delta is measured against it.
    A trailing run has nothing after it whose delta could depend on it -- the
    next real data arrives at a different granularity, and granularities are
    deliberately never paired (see find_negative_deltas). So the run is
    removed outright, and it is lossless in the same sense: consecutive
    zeros produce no deltas the rollup uses.

    Returns values untouched when no row is real, leaving the all-zero file
    to strip_leading_empty_rows' anchor branch -- this runs first, so that
    branch must still see the full run rather than an already-emptied list."""
    last_real = next((i for i in range(len(values) - 1, -1, -1) if not _is_empty_row(values[i])), None)
    if last_real is None:
        return values, 0
    return values[: last_real + 1], len(values) - 1 - last_real


_IMPORT_TOGGLE_COLUMN = {
    "power": "import_power_enabled",
    "gas": "import_gas_enabled",
    "water": "import_water_enabled",
}


def category_import_enabled(conn: sqlite3.Connection, category: str) -> bool:
    """Whether CSV import is enabled for this category (Settings tab).
    Battery has no toggle -- only Power/Gas/Water ever needed one, so it's
    always enabled."""
    column = _IMPORT_TOGGLE_COLUMN.get(category)
    if column is None:
        return True
    row = conn.execute(f"SELECT {column} AS v FROM feature_toggles WHERE id = 1").fetchone()
    return bool(row["v"]) if row else True


def ingest_file(conn: sqlite3.Connection, path: Path) -> int:
    """Ingest a single CSV. Returns rows written (0 if skipped as unchanged
    or disabled for this category in Settings). Raises ValueError for a
    filename that matches no known format -- this used to return 0 here too,
    silently: a file with perfectly valid columns but a typo'd/missing
    filename prefix produced no error and no log line anywhere, contradicting
    the README's "judged against the HomeWizard format and rejected"
    (rejected implies feedback; the old behavior gave none). Callers that
    already know the category (app.py's upload route checks before ever
    calling this) never hit this branch, so no existing behavior changes for
    them -- this only starts surfacing what scan_and_ingest's dropzone scan
    used to swallow.

    Safe to re-run: unchanged files (same content hash) are skipped; changed
    files (e.g. re-exporting a wider date range) are fully re-ingested via
    INSERT OR REPLACE, keyed by timestamp.

    The toggle check lives here (not just in app.py's upload route) since
    this is the single choke point both the web upload and
    omnimeter-ingest.timer's dropzone scan (scan_and_ingest below) go
    through -- a category disabled in Settings must not be silently
    re-enabled just because a file already sits in the dropzone."""
    category = detect_category(path.name)
    if category is None:
        raise ValueError(
            f"unrecognized filename {path.name!r} -- expected either a Bat-/P1e-/P1g-/Water- "
            "prefixed HomeWizard export, or a vendor-neutral "
            "'omnimeter-<power|gas|water|battery>-<name>.csv' file"
        )
    if not category_import_enabled(conn, category):
        return 0

    h = file_hash(path)
    existing = conn.execute(
        "SELECT file_hash FROM ingested_files WHERE filename = ?", (path.name,)
    ).fetchone()
    if existing is not None and existing["file_hash"] == h:
        return 0

    rows = read_csv_rows(path)
    if not rows:
        return 0
    granularity = detect_granularity(rows)
    sql, row_fn = INSERT_SQL[category]
    if detect_generic_category(path.name) is not None:
        # Generic format: validated by generic_rows itself, which raises on
        # an unknown/missing column rather than reaching the all-None check
        # below (that check exists to catch a vendor export whose header
        # changed underneath us -- it can't distinguish that from a
        # deliberately sparse hand-built file).
        values = list(generic_rows(rows, granularity, category))
        sql = generic_insert_sql(category)
    else:
        values = list(row_fn(rows, granularity))
        if values and _all_values_none(values):
            raise ValueError(
                f"every row parsed with no usable values for category '{category}' -- "
                "likely a HomeWizard export header change; expected columns not found"
            )
    # Runs after the header check above, not before: an all-None file is a
    # parse failure that must still raise, whereas an all-zero file is a
    # legitimate export of a period before the meter existed.
    # Trailing before leading: the leading pass collapses an all-zero file to a
    # single anchor row, and the trailing pass must not have emptied it first.
    values, dropped_tail = strip_trailing_empty_rows(values)
    if dropped_tail:
        print(
            f"{path.name}: dropped {dropped_tail} trailing all-zero row(s) -- export "
            "padding past the last real reading (totals unchanged)"
        )
    values, dropped = strip_leading_empty_rows(values)
    if dropped:
        print(
            f"{path.name}: dropped {dropped} leading all-zero row(s) -- meter had not "
            "registered anything yet; one anchor row retained (totals unchanged)"
        )
    conn.executemany(sql, values)
    conn.execute(
        "INSERT OR REPLACE INTO ingested_files "
        "(filename, category, file_hash, mtime, ingested_at, row_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            path.name,
            category,
            h,
            path.stat().st_mtime,
            datetime.now(UTC).isoformat(timespec="seconds"),
            len(values),
        ),
    )
    conn.commit()
    return len(values)


def scan_and_ingest(conn: sqlite3.Connection, imports_dir: Path) -> tuple[dict[str, int], dict[str, str]]:
    """Scan the dropzone for CSVs and ingest any new/changed ones.
    Returns ({filename: rows_ingested}, {filename: error}) — one malformed
    file must not block the rest of the dropzone (or every later run), so
    each file's failure is captured and the scan continues. The caller is
    responsible for reporting errors loudly (non-zero exit -> alerting).

    A file that raises is moved to imports_dir/failed/, mirroring
    app.py's api_import_csv() upload route (same reasoning, quoted from
    there): "a file that crashes ingest would otherwise be retried (and
    fail) on every 15-min timer run from now on." That comment described
    exactly this function's own prior behavior -- the move was only ever
    wired up for the web upload path, so a file dropped straight into the
    dropzone (the documented self-hosted method) re-failed identically,
    forever, with no way to acknowledge it short of deleting it by hand."""
    summary: dict[str, int] = {}
    errors: dict[str, str] = {}
    if not imports_dir.is_dir():
        return summary, errors
    for path in sorted(imports_dir.glob("*.csv")):
        try:
            count = ingest_file(conn, path)
        except Exception as e:  # noqa: BLE001 — deliberate poison-pill isolation
            conn.rollback()
            failed_dir = imports_dir / "failed"
            failed_dir.mkdir(parents=True, exist_ok=True)
            path.rename(failed_dir / path.name)
            errors[path.name] = f"{type(e).__name__}: {e} -- moved to {failed_dir.name}/, not retried"
            continue
        if count:
            summary[path.name] = count
    return summary, errors
