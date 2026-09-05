from __future__ import annotations

import logging
import time

from app.main import init_db, run_next_job, update_worker_heartbeat, validate_runtime_config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    validate_runtime_config()
    init_db()
    update_worker_heartbeat("starting")
    while True:
        try:
            if run_next_job() is None:
                time.sleep(2)
        except Exception:
            logging.exception("sync job failed")
            time.sleep(2)


if __name__ == "__main__":
    main()
