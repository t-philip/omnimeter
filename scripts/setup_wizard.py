"""Interactive first-run setup for a self-hosted OmniMeter install.

Run via `docker compose run --rm setup`. Walks the user through each meter
role (P1/Battery/Watermeter), pairs v2 devices directly (no bot, no
secrets-manager dependency -- the author's own reference deployment's
pairing flow needs both), and writes devices.json + .env.

Pairing reuses homewizard_api_client's SSLContext builder rather than
duplicating the CN-fallback TLS logic the reference deployment's own pairing
script has its own copy of (see that script's docstring for why a plain
custom SSLContext is required at all).

verify_hostname (the identity HomeWizard's local API v2 cert is checked
against) is not derivable from product_type+serial -- P1 carries it in the
Subject CN only, Battery carries a *different* DNS SAN instead (see
homewizard_api_client.py's module docstring, "REAL GOTCHA #2"). Rather than
hardcode either scheme, this wizard fetches the device's real certificate
unverified, shows its identity to the user for a one-time confirmation --
same trust model as an SSH host-key prompt, and the same MITM exposure any
first-connection TOFU flow has (acceptable here: LAN-local, one-time, no
weaker than HomeWizard's own official pairing, which has no cert pinning
before the first pair either) -- then pins that confirmed identity for the
actual pairing request. The Subject CN also encodes the device's serial
(`appliance/{product_type}/{serial}`), so v2 devices never need the user to
manually transcribe a MAC address; only the v1 Watermeter (plain HTTP, no
cert to inspect) still asks for it directly, matching today's documented
manual process.
"""

import json
import secrets
import socket
import ssl
import sys
import time
from pathlib import Path

import urllib3
from cryptography import x509
from cryptography.x509.oid import NameOID

from src import homewizard_api_client as hwc

ENV_FILE_PATH = Path("/opt/omnimeter/.env")
PAIR_TIMEOUT_SECONDS = 180
PAIR_RETRY_INTERVAL_SECONDS = 2
PAIR_NAME = "omnimeter-setup-wizard"

ROLES = (
    ("p1", "P1 meter", "p1dongle"),
    ("battery", "Plug-in Battery", "battery"),
    ("watermeter", "Watermeter", "watermeter"),
)


def _ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    resp = input(prompt + suffix).strip().lower()
    if not resp:
        return default
    return resp in ("y", "yes")


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]: " if default else ": "
    resp = input(prompt + suffix).strip()
    return resp or default


def fetch_device_certificate_identity(ip: str, port: int = 443) -> tuple[str, list[str]]:
    """Returns (subject_cn, san_dns_names) from the device's real certificate,
    fetched WITHOUT verification -- there is nothing to verify against yet,
    this call's entire purpose is establishing that trust for the first time.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((ip, port), timeout=10) as sock, ctx.wrap_socket(sock, server_hostname=ip) as ssock:
        der = ssock.getpeercert(binary_form=True)
    assert der is not None  # binary_form=True on a connected socket always returns the cert
    cert = x509.load_der_x509_certificate(der)
    cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    cn = str(cn_attrs[0].value) if cn_attrs else ""
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        sans = san_ext.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        sans = []
    return cn, sans


def pair_v2_device(ip: str, verify_hostname: str) -> str:
    """POST /api/user, retrying on 403 until the physical button is pressed.
    Mirrors the reference deployment's own pairing script's pair()."""
    pool = urllib3.HTTPSConnectionPool(
        ip,
        port=443,
        ssl_context=hwc._build_ssl_context(),
        assert_hostname=verify_hostname,
        timeout=urllib3.Timeout(connect=10.0, read=10.0),
    )
    deadline = time.monotonic() + PAIR_TIMEOUT_SECONDS
    body = json.dumps({"name": PAIR_NAME}).encode()
    try:
        while time.monotonic() < deadline:
            resp = pool.request(
                "POST", "/api/user", body=body, headers={"Content-Type": "application/json", "X-Api-Version": "2"}
            )
            if resp.status == 200:
                return json.loads(resp.data)["token"]
            if resp.status == 403:
                print("  waiting for the button press...", flush=True)
                time.sleep(PAIR_RETRY_INTERVAL_SECONDS)
                continue
            raise RuntimeError(f"unexpected status {resp.status}")
    finally:
        pool.close()
    raise TimeoutError(f"no button press within {PAIR_TIMEOUT_SECONDS}s")


def _read_env_lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _get_env_value(lines: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def _set_env_value(lines: list[str], key: str, value: str) -> list[str]:
    prefix = f"{key}="
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{key}={value}"
            return lines
    return [*lines, f"{key}={value}"]


def configure_device(role: str, label: str, product_type: str, devices: dict, env_lines: list[str]) -> list[str]:
    existing = devices.get(role, {})
    already_configured = existing.get("ip") not in (None, "REPLACE_ME")
    if already_configured and not _ask_yes_no(f"{label} is already configured ({existing['ip']}). Reconfigure?"):
        return env_lines
    if not already_configured and not _ask_yes_no(f"Do you have a {label}?"):
        return env_lines

    ip = _ask(f"  {label} IP address")
    v2_prompt = "  Is 'API v2 (experimental)' enabled for this device in the HomeWizard app?"
    protocol = "v2" if _ask_yes_no(v2_prompt, default=True) else "v1"
    token_env = f"OMNIMETER_{role.upper()}_TOKEN"

    if protocol == "v1":
        serial = _ask(f"  {label} serial (its MAC address, dashes/colons stripped, lowercase)")
        devices[role] = {
            "ip": ip,
            "serial": serial,
            "product_type": product_type,
            "token_env": token_env,
            "protocol": "v1",
            "verify_hostname": None,
        }
        print(f"  {label} configured (no pairing needed for v1).")
        return env_lines

    try:
        cn, sans = fetch_device_certificate_identity(ip)
    except OSError as e:
        print(f"  Could not reach {ip}: {e} -- skipping {label}, rerun the wizard once it's reachable.")
        return env_lines

    identity = sans[0] if sans else cn
    if cn.startswith("appliance/"):
        serial = cn.rsplit("/", 1)[-1]
    else:
        serial = _ask(f"  {label} serial (couldn't derive it from the certificate)")
    print(f"  Device at {ip} presented certificate identity: {identity}")
    if not _ask_yes_no("  Does this look right? (one-time trust check, like an SSH host key)"):
        print(f"  Skipping {label} -- rerun the wizard once you've confirmed the right device/IP.")
        return env_lines

    print(f"  Ready to pair. Walk to the {label} now.")
    input("  Press Enter once you're there, then press its button when prompted: ")
    try:
        token = pair_v2_device(ip, identity)
    except (TimeoutError, RuntimeError) as e:
        print(f"  FAILED: {e}")
        return env_lines

    devices[role] = {
        "ip": ip,
        "serial": serial,
        "product_type": product_type,
        "token_env": token_env,
        "protocol": "v2",
        "verify_hostname": identity,
    }
    env_lines = _set_env_value(env_lines, token_env, token)
    del token
    print(f"  Paired -- token stored as {token_env} in .env")
    return env_lines


def main() -> int:
    print("OmniMeter setup wizard\n")

    devices_path = hwc.DEVICES_CONFIG_PATH
    devices = hwc.load_devices(devices_path)
    env_lines = _read_env_lines(ENV_FILE_PATH)

    for role, label, product_type in ROLES:
        env_lines = configure_device(role, label, product_type, devices, env_lines)

    with devices_path.open("w") as f:
        json.dump(devices, f, indent=2)
        f.write("\n")

    if not _get_env_value(env_lines, "OMNIMETER_WRITE_API_TOKEN"):
        env_lines = _set_env_value(env_lines, "OMNIMETER_WRITE_API_TOKEN", secrets.token_urlsafe(32))
        print("\nGenerated a new OMNIMETER_WRITE_API_TOKEN (protects Settings/CSV-import writes).")

    if not _get_env_value(env_lines, "OMNIMETER_TIMEZONE"):
        # Worth a sentence of explanation rather than a bare prompt: accepting
        # the UTC default while living elsewhere silently shifts readings taken
        # near midnight into the wrong day, and nothing ever errors to say so.
        print(
            "\nYour home timezone decides where each day starts and ends -- it drives reading\n"
            "timestamps, daily totals, chart day boundaries and imported tariff start dates.\n"
            "Use an IANA name, e.g. Europe/Amsterdam, America/New_York, Asia/Kolkata."
        )
        env_lines = _set_env_value(env_lines, "OMNIMETER_TIMEZONE", _ask("IANA timezone", "UTC"))

    if not _get_env_value(env_lines, "OMNIMETER_BACKUP_HOST_DIR"):
        env_lines = _set_env_value(
            env_lines, "OMNIMETER_BACKUP_HOST_DIR", _ask("Where should backups be stored on your machine?", "./backups")
        )

    ENV_FILE_PATH.write_text("\n".join(env_lines) + "\n")

    print("\nSetup complete. Start OmniMeter with: docker compose up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
