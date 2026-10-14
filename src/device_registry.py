"""Builds the list of MeterDevices to poll, from devices.json.

Lives outside homewizard_api_client.py deliberately: that module is
HomeWizard-specific by design and by name, and it should not be the thing
that knows how to construct a non-HomeWizard device. This module is the one
place that maps a config entry's `protocol` onto an implementation, so
adding a future vendor means adding a branch here and nothing else.

Dispatch:
  protocol "v1" / "v2"      -> HomeWizardDevice (that vendor's own local API)
  protocol "generic_json"   -> GenericHttpDevice (any JSON-over-HTTP meter,
                               driven by the entry's own field_map)

An entry with an unrecognized protocol is skipped rather than raising: one
mis-typed device must not stop the poller from serving every other device,
the same isolation rule ingest_all() already applies to fetch failures.
"""

from __future__ import annotations

import logging

from .generic_http_device import GenericHttpDevice
from .meter_device import MeterDevice

log = logging.getLogger(__name__)

HOMEWIZARD_PROTOCOLS = ("v1", "v2")
GENERIC_JSON_PROTOCOL = "generic_json"


def build_devices(devices_config: dict[str, dict] | None = None) -> list[MeterDevice]:
    """One MeterDevice per configured entry. Defaults to the live registry
    loaded from devices.json."""
    from . import homewizard_api_client as hwc

    config = hwc.DEVICES if devices_config is None else devices_config
    devices: list[MeterDevice] = []
    for name, entry in config.items():
        # Not every value is necessarily a device. devices.json is hand-edited
        # JSON, which has no comment syntax, so adding a "_comment": "..."
        # string is the obvious thing to try -- and used to crash the whole
        # poller with a bare AttributeError on .get(). Skip anything that
        # isn't an object, quietly: an annotation is not a misconfiguration.
        if not isinstance(entry, dict):
            log.debug("devices config entry %r is not an object -- skipping", name)
            continue
        protocol = entry.get("protocol")
        if protocol in HOMEWIZARD_PROTOCOLS:
            devices.append(hwc.HomeWizardDevice(name))
        elif protocol == GENERIC_JSON_PROTOCOL:
            devices.append(GenericHttpDevice(name, entry))
        else:
            log.warning(
                "device %r has unrecognized protocol %r -- skipping. Expected one of: %s",
                name,
                protocol,
                ", ".join([*HOMEWIZARD_PROTOCOLS, GENERIC_JSON_PROTOCOL]),
            )
    return devices
