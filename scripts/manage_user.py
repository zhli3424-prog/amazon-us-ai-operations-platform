from __future__ import annotations

import argparse
import getpass
import re

from app.main import audit, connect, init_db, now, request_actor
from app.security import ROLE_LEVEL, hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description="管理运营后台账号")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create")
    create.add_argument("username")
    create.add_argument("--role", choices=ROLE_LEVEL, required=True)
    disable = sub.add_parser("disable")
    disable.add_argument("username")
    sub.add_parser("list")
    args = parser.parse_args()
    init_db()
    token = request_actor.set("system:user-cli")
    with connect() as conn:
        if args.action == "create":
            password = getpass.getpass("新密码（至少 12 位）: ")
            if len(password) < 12 or not re.search(r"[A-Z]", password) or not re.search(r"[a-z]", password) or not re.search(r"\d", password):
                raise SystemExit("密码至少 12 位，并包含大小写字母和数字")
            conn.execute(
                "INSERT INTO users(username,password_hash,role,is_active,created_at,must_change_password,password_changed_at) VALUES(?,?,?,?,?,1,?) ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,role=excluded.role,is_active=1,must_change_password=1,password_changed_at=excluded.password_changed_at",
                (args.username.strip(), hash_password(password), args.role, 1, now(), now()),
            )
            audit(conn, "user_created_or_reset", "user", None, {"username": args.username.strip(), "role": args.role})
            print("账号已创建或重置")
        elif args.action == "disable":
            target = conn.execute("SELECT role FROM users WHERE username=? AND is_active=1", (args.username,)).fetchone()
            if target and target["role"] == "admin" and conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1").fetchone()[0] <= 1:
                raise SystemExit("不能停用最后一个有效管理员")
            if not conn.execute("UPDATE users SET is_active=0 WHERE username=?", (args.username,)).rowcount:
                raise SystemExit("账号不存在")
            conn.execute("UPDATE user_sessions SET revoked_at=? WHERE username=? AND revoked_at IS NULL", (now(), args.username))
            audit(conn, "user_disabled", "user", None, {"username": args.username})
            print("账号已停用，后续请求会立即失效")
        else:
            for row in conn.execute("SELECT username,role,is_active,created_at FROM users ORDER BY username"):
                print(f"{row['username']}\t{row['role']}\t{'active' if row['is_active'] else 'disabled'}\t{row['created_at']}")
    request_actor.reset(token)


if __name__ == "__main__":
    main()
