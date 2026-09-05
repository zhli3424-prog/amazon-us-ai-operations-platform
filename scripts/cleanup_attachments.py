from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def cleanup(database: Path, private_root: Path) -> int:
    removed = 0
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id,stored_name FROM ticket_attachments WHERE deleted_at IS NULL AND expires_at IS NOT NULL AND expires_at<=?", (timestamp,)).fetchall()
        for row in rows:
            target = (private_root / row["stored_name"]).resolve()
            if target.is_relative_to(private_root.resolve()):
                target.unlink(missing_ok=True)
            conn.execute("UPDATE ticket_attachments SET deleted_at=? WHERE id=?", (timestamp, row["id"]))
            removed += 1
        conn.commit()
    return removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清理超过保留期的客服附件")
    parser.add_argument("--database", type=Path, default=Path("data/app.db"))
    parser.add_argument("--private-root", type=Path, default=Path("data/private"))
    args = parser.parse_args()
    print(f"removed={cleanup(args.database, args.private_root)}")
