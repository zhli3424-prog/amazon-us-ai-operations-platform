from __future__ import annotations

import logging
import time

from app.main import init_db, run_next_job, run_operational_maintenance, update_worker_heartbeat, validate_runtime_config


def run_cycle(last_maintenance: float) -> float:
    current = time.monotonic()
    if current - last_maintenance >= 30:
        run_operational_maintenance()
        last_maintenance = current
    if run_next_job() is None:
        time.sleep(2)
    return last_maintenance


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    validate_runtime_config()
    init_db()
    update_worker_heartbeat("starting")
    last_maintenance = 0.0
    while True:
        try:
            last_maintenance = run_cycle(last_maintenance)
        except Exception:
            logging.exception("sync job failed")
            time.sleep(2)


if __name__ == "__main__":
    main()
