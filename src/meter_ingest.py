"""Maps HomeWizard local API v2 measurement responses into this dashboard's
raw-reading tables, at granularity='api_live'.

'api_live' is deliberately left out of aggregate.py's _GRANULARITY_RANK (which
only knows 'live'/'15min'/'daily') -- an unranked granularity defaults to rank
0 in filter_preferred_granularity, the lowest possible, so these rows never
win over anything already on the dashboard. That's the point during this
supplement/cross-check phase: the poller can run in production,
comparable against HA/CSV data by direct query, without risk of changing what
the user actually sees until they're satisfied it's reliable. Promoting it later is
a one-line edit to that dict, not a change here.

Every device is polled and ingested independently -- one device being
unreachable or its token being revoked must never block the other two.
"""

from collections.abc import Sequence
from datetime import datetime

from . import localtime
from .meter_device import (
    RETURNS_CANONICAL_FIELDS,
    MeterDevice,
    MeterDeviceAuthError,
    MeterDeviceConnectionError,
)

GRANULARITY = "api_live"
# Set OMNIMETER_TIMEZONE in .env. Resolved in src/localtime.py so every
# module agrees -- this poller is the sole live-data source (a later change
# removed the HA sync path), so a wrong zone here mis-stamps every reading taken.
_LOCAL_TZ = localtime.LOCAL_TZ


def _now_local() -> str:
    return datetime.now(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def resolve_fields(measurement: dict, key_map: dict[str, tuple[str, ...]]) -> dict:
    """Map a vendor's raw response onto this app's canonical column names.

    key_map is {canonical_column: (candidate source keys, in priority order)}.
    The first candidate actually present and non-None wins; a canonical field
    with no match resolves to None.

    Candidate *lists* rather than single keys because a vendor's own API
    versions rename fields: HomeWizard's v1 `/api/v1/data` calls a quantity
    `total_power_import_t1_kwh` that its v2 `/api/measurement` calls
    `energy_import_t1_kwh`. Both describe OBIS 1-0:1.8.1 off the same DSMR
    telegram. Before this existed the mappers read v2 names only, so a device
    on protocol='v1' -- the DEFAULT, since v2 is HomeWizard's experimental
    opt-in -- mapped every field to None and wrote rows of NULLs with no
    error anywhere. Listing both spellings fixes that and costs nothing when
    only one is present.

    The same shape is what lets a non-HomeWizard meter work at all: the
    canonical names are this app's `*_readings` columns, so a different
    vendor is a different key_map, not different code."""
    out: dict = {}
    for canonical, candidates in key_map.items():
        value = None
        for key in candidates:
            candidate = measurement.get(key)
            if candidate is not None:
                value = candidate
                break
        out[canonical] = value
    return out


# Canonical column -> candidate source keys. v2 spelling first (the richer
# API, and what a paired device uses), v1 second.
_P1_KEYS: dict[str, tuple[str, ...]] = {
    "import_t1_kwh": ("energy_import_t1_kwh", "total_power_import_t1_kwh"),
    "import_t2_kwh": ("energy_import_t2_kwh", "total_power_import_t2_kwh"),
    "import_combined_kwh": ("energy_import_kwh", "total_power_import_kwh"),
    "export_t1_kwh": ("energy_export_t1_kwh", "total_power_export_t1_kwh"),
    "export_t2_kwh": ("energy_export_t2_kwh", "total_power_export_t2_kwh"),
    "export_combined_kwh": ("energy_export_kwh", "total_power_export_kwh"),
    "l1_max_w": ("power_l1_w", "active_power_l1_w"),
    "l2_max_w": ("power_l2_w", "active_power_l2_w"),
    "l3_max_w": ("power_l3_w", "active_power_l3_w"),
}


def _map_p1(measurement: dict) -> dict:
    return resolve_fields(measurement, _P1_KEYS)


_BATTERY_KEYS: dict[str, tuple[str, ...]] = {
    "import_kwh": ("energy_import_kwh",),
    "export_kwh": ("energy_export_kwh",),
    "soc_pct": ("state_of_charge_pct",),
}

_WATER_KEYS: dict[str, tuple[str, ...]] = {"total_m3": ("total_liter_m3",)}


def _map_battery(measurement: dict) -> dict:
    # Confirmed live against the real device 2026-07-28: /api/measurement
    # does return energy_import_kwh/energy_export_kwh (cumulative), the same
    # figures HA's equivalent sensors report. An earlier version of this
    # function assumed those fields didn't exist and left both None --
    # that assumption was never checked against a live response and was
    # wrong.
    return resolve_fields(measurement, _BATTERY_KEYS)


def _map_watermeter(measurement: dict) -> dict:
    # HomeWizard's Watermeter reports a cumulative total in `total_liter_m3`
    # (misleadingly named -- the value is in m3, not liters; confirmed against
    # the v1 API docs and HA's own integration, since the public v2 docs
    # excerpt available at implementation time didn't show a Watermeter
    # measurement sample directly -- reconfirm this field name against the
    # real device's first live response before trusting it in production).
    # water_readings.water_usage_dl is itself cumulative (see aggregate.py's
    # rebuild_water_daily, which delta's it like gas/power), in deciliters,
    # matching the HomeWizard CSV export's own "water usage dl" column.
    # 1 m3 = 1000 L = 10,000 dL.
    total_m3 = resolve_fields(measurement, _WATER_KEYS)["total_m3"]
    return {"water_usage_dl": None if total_m3 is None else total_m3 * 10_000}


def _map_gas(measurement: dict) -> dict | None:
    """Gas isn't a separate HomeWizard device -- Dutch DSMR smart meters read
    the gas meter over a local wire and relay it through the P1 telegram, so
    the P1 dongle's own /api/measurement surfaces it via an `external` array
    rather than a dedicated endpoint (confirmed live 2026-07-28:
    external: [{"type": "gas_meter", "value": <m3>, "unit": "m3", ...}]).
    Returns None (not a zero-filled row) if no gas_meter entry is present --
    a household with no gas connection, or a P1 telegram with nothing wired
    to it, is "no data" not "zero usage"."""
    for entry in measurement.get("external", []) or []:
        if entry.get("type") == "gas_meter" and entry.get("value") is not None:
            return {"total_gas_m3": entry["value"]}
    return None


_GAS_TABLE = "gas_readings"
_GAS_COLUMNS: tuple[str, ...] = ("total_gas_m3",)

_GAS_KEYS: dict[str, tuple[str, ...]] = {"total_gas_m3": ("total_gas_m3",)}


def _map_gas_device(measurement: dict) -> dict:
    """For gas polled as a device in its OWN right, rather than extracted
    from a P1 response's `external` array (_map_gas below).

    Needed because gas reaches this app two structurally different ways. On a
    HomeWizard P1 the gas meter is relayed inside the P1 telegram, so there
    is no gas device to poll and _map_gas picks it out of the P1 payload as a
    side effect. Any other setup -- a separate gas endpoint, or a meter that
    simply reports a gas total alongside everything else -- has nowhere to
    put that, because _DEVICE_TARGETS is keyed by device name and had no
    'gas' entry at all. A device named "gas" in devices.json now writes
    gas_readings directly."""
    return resolve_fields(measurement, _GAS_KEYS)


# device_name -> (mapper, table, ordered columns matching the mapper's keys)
_DEVICE_TARGETS = {
    "p1": (
        _map_p1,
        "power_readings",
        (
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
    ),
    "battery": (_map_battery, "battery_readings", ("import_kwh", "export_kwh", "soc_pct")),
    "watermeter": (_map_watermeter, "water_readings", ("water_usage_dl",)),
    # No HomeWizard device is named "gas" (its gas arrives inside the P1
    # telegram, handled by the _map_gas side effect below). This entry exists
    # so a meter that DOES report gas as its own endpoint has somewhere to
    # write -- without it, a non-HomeWizard user could poll power, battery
    # and water but had no way to record gas at all.
    "gas": (_map_gas_device, _GAS_TABLE, _GAS_COLUMNS),
}


def _write_row(conn, table: str, columns: tuple[str, ...], row: dict, time_str: str) -> None:
    col_list = ", ".join(["time", *columns, "granularity"])
    placeholders = ", ".join(["?"] * (len(columns) + 2))
    values = [time_str, *[row.get(c) for c in columns], GRANULARITY]
    conn.execute(f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})", values)


def ingest_all(conn, devices: Sequence[MeterDevice]) -> dict[str, str]:
    """devices: one MeterDevice per source to poll -- vendor-agnostic.
    Only devices with an entry in _DEVICE_TARGETS have anywhere to write a
    measurement to; a name outside that table is reported, not silently
    dropped (see the config_error branch below). Returns {device_name:
    status}, status one of 'ok',
    'skipped: device not configured (unpaired, or no token set)',
    'auth_error: ...', 'connection_error: ...', or 'config_error: ...' --
    one device's failure is captured here and never raises past this
    function, so the caller keeps polling the rest."""
    time_str = _now_local()
    results: dict[str, str] = {}
    for device in devices:
        if device.name not in _DEVICE_TARGETS:
            # A device that made it this far already passed
            # device_registry.build_devices() -- it has a recognized
            # protocol and is a real, constructed MeterDevice. Landing here
            # with a name outside _DEVICE_TARGETS is always a devices.json
            # authoring mistake (a typo'd key, most likely), never a
            # legitimate no-op -- unlike build_devices()'s own skip of
            # non-dict "_comment" entries, which never reach here at all.
            # Surfacing it is the whole point: devices.json.generic.example
            # once shipped "water" instead of "watermeter" and every reading
            # was silently discarded with no error anywhere.
            results[device.name] = (
                f"config_error: device name {device.name!r} has no destination table -- "
                f"expected one of {sorted(_DEVICE_TARGETS)}, check devices.json"
            )
            continue
        mapper, table, columns = _DEVICE_TARGETS[device.name]
        if not device.is_configured():
            results[device.name] = "skipped: device not configured (unpaired, or no token set)"
            continue
        try:
            measurement = device.fetch_measurement()
        except MeterDeviceAuthError as e:
            results[device.name] = f"auth_error: {e}"
            continue
        except MeterDeviceConnectionError as e:
            results[device.name] = f"connection_error: {e}"
            continue
        # A device that already speaks canonical column names (see
        # meter_device.RETURNS_CANONICAL_FIELDS) carries its own field map as
        # configuration and must not have a vendor mapper run over it as
        # well -- doing so would look up HomeWizard's key names in an
        # already-translated dict and find nothing.
        if getattr(device, RETURNS_CANONICAL_FIELDS, False):
            row = dict(measurement)
        else:
            row = mapper(measurement)
        # A response that maps to nothing at all is a field-name mismatch,
        # not an empty meter: the device answered (fetch_measurement would
        # have raised otherwise), so something came back -- this app just
        # didn't recognize any of it. Writing it would insert a row of NULLs
        # that looks exactly like a real reading of nothing, and the
        # dashboard would silently show a gap with no error anywhere to
        # explain it. Report which keys did arrive, since that is precisely
        # what someone wiring up a new meter needs to build a key_map from.
        if all(v is None for v in row.values()):
            arrived = ", ".join(sorted(measurement)[:12]) or "(empty response)"
            results[device.name] = (
                f"field_mapping_error: none of the expected fields "
                f"({', '.join(columns)}) were found in the response. "
                f"Keys actually returned: {arrived}"
            )
            continue
        _write_row(conn, table, columns, row, time_str)
        results[device.name] = "ok"

        if device.name == "p1":
            gas_row = _map_gas(measurement)
            if gas_row is not None:
                _write_row(conn, _GAS_TABLE, _GAS_COLUMNS, gas_row, time_str)
    conn.commit()
    return results
