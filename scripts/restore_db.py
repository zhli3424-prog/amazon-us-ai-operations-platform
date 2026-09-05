from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path


def restore(backup: Path, target: Path) -> Path:
    if not backup.is_file():
        raise FileNotFoundError(f"备份不存在: {backup}")
    with sqlite3.connect(f"file:{backup.resolve()}?mode=ro", uri=True) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份文件完整性校验失败")
    manifest = backup.with_suffix(".json")
    if manifest.is_file():
        expected = json.loads(manifest.read_text(encoding="utf-8"))["sha256"]
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        if digest != expected:
            raise RuntimeError("备份校验和不匹配，禁止恢复")
    if target.exists():
        raise FileExistsError("目标数据库已存在；请先停止服务并将旧文件移到安全位置")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从已校验备份恢复数据库；必须先停止 Web 和 Worker")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--target", type=Path, default=Path("data/app.db"))
    args = parser.parse_args()
    print(restore(args.backup, args.target).resolve())
