from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import shutil
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(source: Path, target_dir: Path, retention: int = 14, replica_dir: Path | None = None) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"数据库不存在: {source}")
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"app-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}.db"
    replica_target = None
    try:
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性校验失败")
            schema_version = dst.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
        checksum = file_sha256(target)
        manifest = target.with_suffix(".json")
        manifest.write_text(json.dumps({"database": target.name, "sha256": checksum, "schema_version": schema_version, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, ensure_ascii=False, indent=2), encoding="utf-8")
        if replica_dir:
            if replica_dir.resolve() == target_dir.resolve() or replica_dir.resolve() == source.parent.resolve():
                raise ValueError("副本目录必须与数据库和主备份目录处于不同路径")
            replica_dir.mkdir(parents=True, exist_ok=True)
            replica_target = replica_dir / target.name
            shutil.copy2(target, replica_target)
            shutil.copy2(manifest, replica_dir / manifest.name)
            if file_sha256(replica_target) != checksum:
                raise RuntimeError("异地副本校验失败")
        recovery_source = replica_target or target
        with tempfile.TemporaryDirectory() as recovery_dir:
            recovery_copy = Path(recovery_dir) / "restored.db"
            shutil.copy2(recovery_source, recovery_copy)
            with closing(sqlite3.connect(recovery_copy)) as recovered:
                if recovered.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or recovered.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0] != schema_version:
                    raise RuntimeError("备份恢复演练校验失败")
        backups = sorted(target_dir.glob("app-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
        for expired in backups[max(1, retention):]:
            expired.unlink()
            expired.with_suffix(".json").unlink(missing_ok=True)
        record_backup_run(source, "success", target, replica_target, checksum, schema_version, started_at, None)
        return target
    except Exception as exc:
        target.unlink(missing_ok=True)
        target.with_suffix(".json").unlink(missing_ok=True)
        if replica_target:
            replica_target.unlink(missing_ok=True)
            replica_target.with_suffix(".json").unlink(missing_ok=True)
        record_backup_run(source, "failed", None, None, None, None, started_at, str(exc)[:500])
        raise


def record_backup_run(source: Path, status: str, target: Path | None, replica: Path | None, checksum: str | None, schema_version: int | None, started_at: str, error: str | None) -> None:
    try:
        with closing(sqlite3.connect(source)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_runs'").fetchone():
                conn.execute("INSERT INTO backup_runs(status,backup_path,replica_path,sha256,schema_version,started_at,completed_at,error) VALUES(?,?,?,?,?,?,?,?)", (status, str(target.resolve()) if target else None, str(replica.resolve()) if replica else None, checksum, schema_version, started_at, datetime.now(timezone.utc).isoformat(timespec="seconds"), error))
                conn.commit()
    except sqlite3.Error:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="在线备份 SQLite 数据库并执行完整性校验")
    parser.add_argument("--source", type=Path, default=Path("data/app.db"))
    parser.add_argument("--target-dir", type=Path, default=Path("backups"))
    parser.add_argument("--retention", type=int, default=14, help="保留最近多少份备份")
    parser.add_argument("--replica-dir", type=Path, help="复制到独立磁盘或远程挂载目录")
    args = parser.parse_args()
    if args.retention < 1:
        raise SystemExit("retention 必须至少为 1")
    print(backup(args.source, args.target_dir, args.retention, args.replica_dir).resolve())
