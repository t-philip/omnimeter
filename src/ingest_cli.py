"""Entry point for omnimeter-ingest.service — scan the dropzone, ingest any
new/changed CSVs, and rebuild the daily rollup tables. Run via:
    python -m src.ingest_cli
"""

import os
import sys
from pathlib import Path

from . import aggregate, db, ingest

DEFAULT_IMPORTS_DIR = Path("/opt/omnimeter/data/imports")


def main() -> int:
    imports_dir = Path(os.environ.get("OMNIMETER_IMPORTS_DIR", DEFAULT_IMPORTS_DIR))
    conn = db.get_connection()
    db.init_db(conn)

    summary, errors = ingest.scan_and_ingest(conn, imports_dir)
    if summary:
        for filename, count in summary.items():
            print(f"ingested {filename}: {count} rows")
        aggregate.rebuild_all(conn)
        print("rollups rebuilt")
    else:
        print("no new or changed files")

    # Failed files don't block the others (see scan_and_ingest), but they
    # must still fail the run loudly so monitoring surfaces them.
    for filename, error in errors.items():
        print(f"ERROR ingesting {filename}: {error}", file=sys.stderr)

    conn.close()
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
