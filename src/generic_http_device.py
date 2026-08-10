"""A MeterDevice for any meter that serves its readings as JSON over HTTP.

The point of this module is that adding a meter brand should not require
adding a Python file. Most local-network energy meters expose exactly one
thing: an unauthenticated (or bearer-token) HTTP endpoint returning a flat
JSON object of current readings. What differs between brands is only the
*spelling* of the keys -- not the quantities, because a P1/DSMR meter's
available quantities are fixed by the DSMR standard itself (OBIS codes), not
by whoever built the dongle.

So this device takes the spelling as configuration. A devices.json entry:

    "p1": {
      "protocol": "generic_json",
      "url": "http://192.0.2.10/api/v1/data",
      "token_env": null,
      "field_map": {
        "import_t1_kwh": "total_power_import_t1_kwh",
        "export_t1_kwh": "total_power_export_t1_kwh",
        "import_combined_kwh": "total_power_import_kwh"
      }
    }

maps that vendor's key names onto this app's canonical *_readings column
names -- the same canonical names the vendor-neutral CSV format uses, and
the same resolve_fields() mechanism the HomeWizard mappers use.

Deliberately NOT auto-detecting the vendor from the response shape. Two
brands can use the same key name for different quantities (cumulative vs.
instantaneous, Wh vs. kWh), and guessing wrong writes plausible-looking
wrong numbers rather than failing -- the worst possible outcome for a
meter-reading app. An explicit map is a few lines of config and cannot be
wrong silently.

Nested values are addressed with dots ("data.power.import_kwh"); a literal
dot in a key is not supported, which no observed meter API needs.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from .meter_device import MeterDeviceAuthError, MeterDeviceConnectionError

DEFAULT_TIMEOUT = 10


def resolve_path(payload: dict, path: str) -> Any:
    """Fetch a possibly-nested value by dotted path. Returns None if any
    segment is missing, so a partially-matching map degrades to "that field
    wasn't found" rather than raising -- ingest_all's all-None check is what
    turns a wholly-wrong map into a reported error.

    A numeric segment indexes a list: "external.0.value" reaches into an
    array of readings. Real meters do this -- a HomeWizard P1 reports its
    relayed gas meter as `external: [{"type": "gas_meter", "value": ...}]`
    -- and without list support such a value is simply unreachable, which
    would silently limit the generic device to whatever a vendor happened to
    put at the top level. Negative indices are not supported (a meter's array
    order is not a contract worth counting backwards from)."""
    current: Any = payload
    for segment in path.split("."):
        if isinstance(current, list):
            if not segment.isdigit():
                return None
            index = int(segment)
            if index >= len(current):
                return None
            current = current[index]
        elif isinstance(current, dict):
            current = current.get(segment)
        else:
            return None
        if current is None:
            return None
    return current


class GenericHttpDevice:
    """Implements meter_device.MeterDevice for a JSON-over-HTTP meter.

    Reads its config fresh on every call (same reasoning as
    HomeWizardDevice): config can change under a running poller when a
    device is set up, and tests monkeypatch the registry.
    """

    # fetch_measurement() applies this device's own configured field_map, so
    # the ingest layer must not also run a vendor mapper over the result.
    # See meter_device.RETURNS_CANONICAL_FIELDS.
    returns_canonical_fields = True

    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        self._config = config

    @property
    def url(self) -> str | None:
        return self._config.get("url")

    def is_configured(self) -> bool:
        """Ready to poll: a URL, a non-empty field_map, and -- only if this
        device declares a token_env -- that variable actually being set.
        A device with no token_env needs no credential at all, which is the
        common case for LAN meter APIs."""
        if not self.url or "REPLACE_ME" in self.url:
            return False
        if not self._config.get("field_map"):
            return False
        token_env = self._config.get("token_env")
        if token_env and os.environ.get(token_env) is None:
            return False
        return True

    def fetch_measurement(self) -> dict:
        """GET the endpoint and return the vendor's raw JSON, with the
        configured field_map applied to produce canonical names.

        The mapping happens here rather than in the ingest layer so the
        object handed back already speaks this app's vocabulary, exactly
        like HomeWizardDevice's -- ingest_all stays vendor-agnostic and
        needs no branch for this device type."""
        url = self.url
        if not url:
            raise MeterDeviceConnectionError(f"{self.name}: no url configured")
        headers = {}
        token_env = self._config.get("token_env")
        if token_env:
            token = os.environ.get(token_env)
            if not token:
                raise MeterDeviceAuthError(f"{self.name}: no token configured ({token_env} not set)")
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise MeterDeviceConnectionError(f"{self.name}: unreachable: {e}") from e
        if resp.status_code in (401, 403):
            raise MeterDeviceAuthError(f"{self.name}: HTTP {resp.status_code} -- credential rejected")
        if resp.status_code >= 400:
            raise MeterDeviceConnectionError(f"{self.name}: unexpected HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise MeterDeviceConnectionError(f"{self.name}: response was not JSON: {e}") from None
        if not isinstance(payload, dict):
            raise MeterDeviceConnectionError(f"{self.name}: expected a JSON object, got {type(payload).__name__}")

        field_map: dict[str, str] = self._config.get("field_map") or {}
        return {canonical: resolve_path(payload, source) for canonical, source in field_map.items()}
