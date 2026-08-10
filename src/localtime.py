"""Single source of truth for the installation's local timezone.

Every module that timestamps, dates, or day-buckets anything resolves the
zone through here.

This exists because the same ``os.environ.get("OMNIMETER_TIMEZONE", ...)``
call was copy-pasted into four modules with a hardcoded Netherlands default
-- and when ``tariff_parser.py`` was added it silently got no copy at all,
dating imported rate periods in the host's UTC instead of local time
(found 2026-08-08 on a host running Etc/UTC). One import is harder
to forget than a convention, and one default is harder to disagree with than
four.

The default is **UTC**, which matches ``.env.example`` and the setup wizard.
It is deliberately *not* Europe/Amsterdam: that was an artifact of the
author's own installation, and a Dutch default buried in the code would
silently mis-bucket every reading for
a self-hosted user elsewhere in the world, who has no reason to suspect it is
there. Being wrong in UTC is at least obvious; being wrong in someone else's
timezone is not.
"""

import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ENV_VAR = "OMNIMETER_TIMEZONE"
DEFAULT_TIMEZONE = "UTC"

_log = logging.getLogger(__name__)


def resolve_timezone_name(environ=None) -> str:
    """The configured IANA zone name. Open-Meteo wants this string form, so
    it is exposed as well as the ZoneInfo object."""
    env = os.environ if environ is None else environ
    name = (env.get(ENV_VAR) or "").strip()
    if name:
        return name
    _log.warning(
        "%s is not set -- falling back to %s. Daily totals, chart day boundaries and "
        "imported rate-period dates will all be bucketed in %s, which is almost certainly "
        "wrong unless you genuinely live there. Set it in .env to your own IANA zone "
        "(e.g. Europe/Amsterdam, America/New_York, Asia/Kolkata).",
        ENV_VAR,
        DEFAULT_TIMEZONE,
        DEFAULT_TIMEZONE,
    )
    return DEFAULT_TIMEZONE


def resolve_timezone(name: str) -> ZoneInfo:
    """Fail closed on a bad zone name rather than starting up and silently
    writing every timestamp in the wrong offset -- the same reasoning as
    this app's refusal to start on an unreachable secrets backend."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError(
            f"{ENV_VAR}={name!r} is not a valid IANA timezone ({exc}). "
            f"Use a tz-database name such as Europe/Amsterdam, America/New_York or "
            f"Asia/Kolkata. Refusing to start rather than timestamping your data in "
            f"the wrong zone."
        ) from None


TIMEZONE_NAME = resolve_timezone_name()
LOCAL_TZ = resolve_timezone(TIMEZONE_NAME)
