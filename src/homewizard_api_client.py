"""Low-level HTTP(S) client for HomeWizard's local API, one device at a time.

Each device (P1 dongle, Plug-in Battery, Watermeter) runs its own local API
instance -- own IP, own protocol version, own pairing/Bearer token if on v2.
Not one integration, three.

Two protocols coexist here, per device, tracked in DEVICES[*]["protocol"]:
- 'v2': HTTPS + Bearer token (P1, Battery -- confirmed live 2026-07-24 that
  both have API v2 enabled).
- 'v1': plain HTTP, no auth, no pairing (Watermeter -- confirmed live the
  same day that this specific unit, HWE-WTR-13 firmware 3.01, only exposes
  a generic "Local API" toggle in the app, not "API v2 (experimental)";
  v2 apparently hasn't rolled out to this model/firmware yet). Consistent
  with this app's existing LAN-only security boundary (see
  README's Write authentication section) -- v1 has always been
  unauthenticated by design, same risk profile as any other
  unauthenticated local-only IoT API already tolerated on this network.
  If a future firmware update adds v2 support, flip this device's
  protocol back to 'v2' and pair it through the normal flow -- ip/serial
  are already on file.

API v2 is HTTPS with a self-signed cert per device, verified against
HomeWizard's own CA (src/certs/homewizard-ca-cert.pem, downloaded from
api-documentation.homewizard.com/docs/v2/authorization/) plus hostname
verification. `requests`'s `verify=` kwarg only checks a cert against the
connection's own host (here, a bare LAN IP), which doesn't match either of
the identity schemes below -- using urllib3 directly instead, since
HTTPSConnectionPool's `assert_hostname` lets the hostname check target
something other than the connection host. Cert verification is never
disabled here, even as a fallback.

REAL GOTCHA #1 (confirmed live against the P1 meter, 2026-07-24): its cert
carries an `appliance/{type}/{serial}` identity in the Subject *Common
Name*, with no Subject Alternative Name extension at all. Modern hostname
verification (RFC 6125) checks SAN only; CN-based fallback was dropped from
Python's own `ssl` module years ago. urllib3 keeps CN-fallback support for
exactly this kind of legacy cert, but only if you hand it your own
`ssl.SSLContext` with `hostname_checks_common_name = True` -- the simpler
`ca_certs=`/`cert_reqs=` kwargs (tried first) make urllib3 build its own
default context internally, which hardcodes CN-fallback to `False`
regardless, and every request fails with "no appropriate subjectAltName
fields were found". `_build_ssl_context()` below constructs that context
explicitly for this reason.

REAL GOTCHA #2 (confirmed live against the Plug-in Battery, same day):
its cert has a *different* identity scheme entirely -- Subject CN is still
`appliance/battery/{serial}` (matching P1's pattern), but it also carries a
DNS SAN of `{serial}.battery.device.homewizard.energy`. Once a cert has any
SAN, standard hostname matching uses the SAN exclusively and never
consults the CN, so asserting the `appliance/...` string here fails outright
even with CN-fallback enabled -- these devices are not consistent with each
other, HomeWizard's docs describe the P1's scheme as if it were universal,
and it isn't. There is no formula that derives the right value from
product_type+serial across device types -- DEVICES[*]["verify_hostname"]
below is therefore set per device from the certificate's own actual
Subject/SAN (inspected via `openssl s_client -connect {ip}:443 | openssl
x509 -noout -text`), not computed. Check any newly-added device's real
certificate the same way before assuming either scheme applies.

Rate-limit guidance from HomeWizard's docs: no faster than one request every
500ms. meter_poller.py's ~20s poll loop is nowhere near that.
"""

import json
import os
import ssl
from pathlib import Path

import requests
import urllib3

from .meter_device import MeterDeviceAuthError, MeterDeviceConnectionError

CA_CERT_PATH = Path(__file__).parent / "certs" / "homewizard-ca-cert.pem"

# Device registry (LAN IPs, serials, per-device protocol/cert-identity
# details) is external config, not source -- neither IPs nor serials are
# secret (only the per-device Bearer token is, however your deployment
# provides secrets, keyed by DEVICES[*]["token_env"]), but a self-hostable fork needs its own devices
# with its own IPs, so these can no longer be literals in this file.
#
# Defaults to OMNIMETER_DEVICES_CONFIG, or /opt/omnimeter/devices.json next
# to the app root if unset -- see devices.json.example for the schema. A
# device still on REPLACE_ME (or missing entirely, if the file doesn't exist
# yet) is treated as "not configured" and simply skipped every poll cycle
# (see is_configured() and meter_ingest.ingest_all()), not a reason
# to block the whole service from starting.
DEVICES_CONFIG_PATH = Path(os.environ.get("OMNIMETER_DEVICES_CONFIG", "/opt/omnimeter/devices.json"))

# token_env defaults are deliberately generic (OMNIMETER_*, not HOMEWIZARD_*)
# -- HomeWizard is one of several P1-meter brands, and a public self-hoster
# may not have HomeWizard hardware at all (MeterDevice is
# meant to generalize beyond one vendor). The author's own reference
# deployment keeps its real devices.json on its original HOMEWIZARD_*_API_TOKEN
# secret names untouched -- that rename is deliberately out of scope, since
# it's a purely internal naming choice with no bearing on this repo.
_UNCONFIGURED_STUB: dict[str, dict[str, str | None]] = {
    "p1": {
        "ip": "REPLACE_ME",
        "serial": "REPLACE_ME",
        "product_type": "p1dongle",
        "token_env": "OMNIMETER_P1_TOKEN",
        "protocol": "v2",
        "verify_hostname": None,
    },
    "battery": {
        "ip": "REPLACE_ME",
        "serial": "REPLACE_ME",
        "product_type": "battery",
        "token_env": "OMNIMETER_BATTERY_TOKEN",
        "protocol": "v2",
        "verify_hostname": None,
    },
    "watermeter": {
        "ip": "REPLACE_ME",
        "serial": "REPLACE_ME",
        "product_type": "watermeter",
        "token_env": "OMNIMETER_WATERMETER_TOKEN",
        "protocol": "v1",
        "verify_hostname": None,
    },
}


def load_devices(path: str | Path | None = None) -> dict[str, dict[str, str | None]]:
    """Load the device registry from a JSON config file (see
    devices.json.example for the schema). Falls back to an all-REPLACE_ME
    stub if the file doesn't exist -- is_configured() already treats
    REPLACE_ME devices as "not paired yet," so a missing config file is an
    unconfigured install, not a startup failure.
    """
    cfg_path = Path(path) if path is not None else DEVICES_CONFIG_PATH
    if not cfg_path.exists():
        return {name: dict(cfg) for name, cfg in _UNCONFIGURED_STUB.items()}
    with cfg_path.open() as f:
        return json.load(f)


DEVICES: dict[str, dict[str, str | None]] = load_devices()

DEFAULT_TIMEOUT = 10.0


class HomeWizardAuthError(MeterDeviceAuthError):
    """401/403 -- token missing, invalid, or revoked. Re-pairing required."""


class HomeWizardConnectionError(MeterDeviceConnectionError):
    """Device unreachable, TLS failure, or unexpected HTTP status -- distinct
    from an auth failure so callers can log/alert differently (a flaky
    device/network vs. a dead credential)."""


def _build_ssl_context() -> ssl.SSLContext:
    """A custom SSLContext is required (not just ca_certs=/cert_reqs= kwargs)
    to enable CN-based hostname fallback -- see module docstring."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(CA_CERT_PATH))
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = False  # we verify manually via assert_hostname below
    context.hostname_checks_common_name = True
    return context


def is_configured(device_name: str) -> bool:
    """False if this device's ip/serial is still the REPLACE_ME placeholder --
    i.e. not yet paired/set up. Devices are configured one at a time (the
    pair/confirm flow), so this is a per-device check, not an all-or-nothing
    gate: the service should start and poll whatever's ready, not refuse to
    run at all until every device happens to be configured."""
    cfg = DEVICES[device_name]
    return cfg["ip"] != "REPLACE_ME" and cfg["serial"] != "REPLACE_ME"


def configured_devices() -> list[str]:
    return [name for name in DEVICES if is_configured(name)]


def fetch_measurement(device_name: str, token: str) -> dict:
    """GET /api/measurement from one paired device.

    Raises HomeWizardAuthError on 401/403, HomeWizardConnectionError on any
    network/TLS failure or other non-2xx status.
    """
    cfg = DEVICES[device_name]
    ip = cfg["ip"]
    assert ip is not None  # only verify_hostname is ever None (v1 devices, which never reach here)
    pool = urllib3.HTTPSConnectionPool(
        ip,
        port=443,
        ssl_context=_build_ssl_context(),
        assert_hostname=cfg["verify_hostname"],
        timeout=urllib3.Timeout(connect=DEFAULT_TIMEOUT, read=DEFAULT_TIMEOUT),
    )
    try:
        resp = pool.request(
            "GET",
            "/api/measurement",
            headers={"Authorization": f"Bearer {token}", "X-Api-Version": "2"},
        )
    except urllib3.exceptions.HTTPError as e:
        raise HomeWizardConnectionError(f"{device_name}: unreachable or TLS failure: {e}") from e
    finally:
        pool.close()

    if resp.status in (401, 403):
        raise HomeWizardAuthError(f"{device_name}: HTTP {resp.status} -- token invalid/revoked, re-pair required")
    if resp.status >= 400:
        raise HomeWizardConnectionError(f"{device_name}: unexpected HTTP {resp.status}")
    return json.loads(resp.data)


def fetch_measurement_v1(device_name: str) -> dict:
    """GET /api/v1/data over plain HTTP -- no auth, no TLS, no pairing. Only
    for devices whose DEVICES entry has protocol='v1' (see module docstring).
    """
    cfg = DEVICES[device_name]
    try:
        resp = requests.get(f"http://{cfg['ip']}/api/v1/data", timeout=DEFAULT_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise HomeWizardConnectionError(f"{device_name}: unreachable: {e}") from e
    if resp.status_code >= 400:
        raise HomeWizardConnectionError(f"{device_name}: unexpected HTTP {resp.status_code}")
    return resp.json()


class HomeWizardDevice:
    """Implements meter_device.MeterDevice for one HomeWizard device (P1,
    Battery, or Watermeter). Wraps this module's v1/v2 fetch functions and
    per-device protocol/token lookup -- everything HomeWizard-specific stays
    inside this class, so callers like meter_ingest.ingest_all() can
    work against the generic MeterDevice contract alone.

    Reads DEVICES[name] fresh on every call rather than caching config at
    construction, so it reflects live pairing/config changes (and so tests
    can monkeypatch DEVICES the same way they already do for the module-level
    functions).
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def is_configured(self) -> bool:
        """True once this device is paired (see is_configured()) *and*, for
        v2 devices, its Bearer token is actually present in the environment.
        v1 devices (no auth) are ready as soon as they're paired. Merges what
        used to be two separate skip reasons ("not paired" vs. "no token
        configured") into one readiness check -- both mean "not ready to poll
        yet," and a future non-token-based vendor may not have a credential
        concept at all, so the generic contract shouldn't assume one.
        """
        if not is_configured(self.name):
            return False
        cfg = DEVICES[self.name]
        if cfg["protocol"] == "v1":
            return True
        token_env = cfg["token_env"]
        assert token_env is not None  # only verify_hostname is ever None
        return os.environ.get(token_env) is not None

    def fetch_measurement(self) -> dict:
        cfg = DEVICES[self.name]
        if cfg["protocol"] == "v1":
            return fetch_measurement_v1(self.name)
        token_env = cfg["token_env"]
        assert token_env is not None  # only verify_hostname is ever None
        token = os.environ.get(token_env)
        if not token:
            # Defense in depth -- callers should gate on is_configured()
            # first, which already excludes this case.
            raise HomeWizardAuthError(f"{self.name}: no token configured ({token_env} not set)")
        return fetch_measurement(self.name, token)


def all_devices() -> list[HomeWizardDevice]:
    return [HomeWizardDevice(name) for name in DEVICES]
