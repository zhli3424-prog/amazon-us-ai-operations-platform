from __future__ import annotations

import re
import sqlite3
from pathlib import Path


MIGRATION_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
BASELINE_VERSION = 18


def apply_migrations(conn: sqlite3.Connection, directory: Path) -> int:
    current = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_PATTERN.match(path.name)
        if not match:
            continue
        version = int(match.group(1))
        if version <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{sql}\n"
            f"INSERT INTO schema_migrations(version,applied_at) VALUES({version},strftime('%Y-%m-%dT%H:%M:%SZ','now'));\n"
            "COMMIT;"
        )
        current = version
    return current
