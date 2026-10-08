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
    ENGINE_DATA_DIR,
    ETL_PORT,
    ensure_workspace_folders,
    server_is_running,
    start_python_server,
    stop_python_server,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--port", type=int, default=ETL_PORT)
    args = parser.parse_args()
    ensure_workspace_folders()
    log_path = ENGINE_DATA_DIR / "logs" / "flask_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")

    time.sleep(max(0.0, args.delay))
    log(f"restart: stopping port {args.port}")
    stop_python_server(args.port)
    deadline = time.time() + 20
    while server_is_running(args.port) and time.time() < deadline:
        time.sleep(0.4)
    if server_is_running(args.port):
        log("restart: port still in use; start skipped")
        return 1
    log("restart: starting")
    start_python_server(args.port)
    deadline = time.time() + 40
    while not server_is_running(args.port) and time.time() < deadline:
        time.sleep(0.5)
    running = server_is_running(args.port)
    log(f"restart: running={running}")
    return 0 if running else 1


if __name__ == "__main__":
    raise SystemExit(main())
