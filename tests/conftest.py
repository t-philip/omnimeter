import os
import sys
from pathlib import Path

import pytest

# Ensure `src` is importable when running `pytest tests/` from the app root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import credentials  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_credential_source():
    """src.credentials keeps one process-wide CredentialSource instance,
    rebuilt only via configure()/_reset(). A test that calls configure()
    with its own extra_dir must not leak that into the next test."""
    credentials._reset()
    yield
    credentials._reset()


# Pin the suite's timezone before anything under src/ is imported --
# src.localtime resolves OMNIMETER_TIMEZONE once, at import time.
#
# Several tests assert real Amsterdam DST offsets (e.g. +02:00 in July), and
# until now they passed only because the code carried a hardcoded
# Europe/Amsterdam default AND the dev machine happened to run that zone.
# Both are accidents: the default is now UTC (see src/localtime.py), and CI or
# a differently-configured laptop would have failed. Setting it explicitly
# makes the suite deterministic on any host, and makes it obvious that a test
# asserting "+02:00" is asserting DST handling, not a default.
os.environ.setdefault("OMNIMETER_TIMEZONE", "Europe/Amsterdam")
