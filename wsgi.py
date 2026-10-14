import os

from src.credentials import configure as _configure_credentials

# Must run before src.app (create_app() resolves OMNIMETER_WRITE_API_TOKEN at
# call time) — optional hardening for a native systemd install;
# a plain-env deployment is unaffected (extra_dir=None falls through to
# os.environ, same as before this existed).
_configure_credentials(extra_dir=os.environ.get("OMNIMETER_CREDENTIALS_DIR"))

from src.app import create_app  # noqa: E402

app = create_app()
