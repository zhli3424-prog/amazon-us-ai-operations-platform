from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(source: Path, target_dir: Path, retention: int = 14) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"数据库不存在: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"app-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份完整性校验失败")
        schema_version = dst.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
    checksum = file_sha256(target)
    target.with_suffix(".json").write_text(json.dumps({"database": target.name, "sha256": checksum, "schema_version": schema_version, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, ensure_ascii=False, indent=2), encoding="utf-8")
    backups = sorted(target_dir.glob("app-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    for expired in backups[max(1, retention):]:
        expired.unlink()
        expired.with_suffix(".json").unlink(missing_ok=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="在线备份 SQLite 数据库并执行完整性校验")
    parser.add_argument("--source", type=Path, default=Path("data/app.db"))
    parser.add_argument("--target-dir", type=Path, default=Path("backups"))
    parser.add_argument("--retention", type=int, default=14, help="保留最近多少份备份")
    args = parser.parse_args()
    if args.retention < 1:
        raise SystemExit("retention 必须至少为 1")
    print(backup(args.source, args.target_dir, args.retention).resolve())
