"""Vendor-agnostic device contract for the ingest layer.

The polling-loop/error-isolation shape in meter_ingest.ingest_all() needs to
know nothing vendor-specific (TLS cert quirks, protocol versions, Bearer
tokens) -- only "is this device ready to poll" and "go get its current
measurement," with a way to tell a credential problem apart from a
device/network problem.

Two implementations:
  HomeWizardDevice  (homewizard_api_client.py) -- that vendor's own local API
  GenericHttpDevice (generic_http_device.py)   -- any JSON-over-HTTP meter,
                                                  driven by a configured
                                                  field_map
"""

from typing import Protocol, runtime_checkable


class MeterDeviceAuthError(Exception):
    """Credential missing, invalid, or revoked -- re-auth/re-pair required."""


class MeterDeviceConnectionError(Exception):
    """Device unreachable, transport failure, or unexpected response."""


@runtime_checkable
class MeterDevice(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    def fetch_measurement(self) -> dict: ...


# Optional attribute an implementation may set to True to declare that
# fetch_measurement() already returns this app's canonical *_readings column
# names, so the ingest layer must NOT put a vendor mapper in front of it.
#
# HomeWizardDevice returns its vendor's raw response and relies on the
# per-device mappers in meter_ingest. GenericHttpDevice instead
# carries its own configured field_map and applies it itself, because that
# map IS its per-device configuration -- there is nowhere else it could live.
# Read via getattr(device, RETURNS_CANONICAL_FIELDS, False) so the attribute
# stays genuinely optional and the Protocol above keeps its three-member
# minimum.
RETURNS_CANONICAL_FIELDS = "returns_canonical_fields"
