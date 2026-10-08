"""Stop then start the local Flask ETL server. Used by the Setup screen Restart button."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from core.workspace_layout import (  # noqa: E402
    ETL_PORT,
    ensure_workspace_folders,
    start_python_server,
    stop_python_server,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--port", type=int, default=ETL_PORT)
    args = parser.parse_args()
    ensure_workspace_folders()
    time.sleep(max(0.0, args.delay))
    stop_python_server(args.port)
    time.sleep(1.0)
    start_python_server(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
