from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


ROLE_LEVEL = {"viewer": 10, "operator": 20, "approver": 30, "admin": 40}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_session(username: str, role: str, secret: str, ttl_seconds: int = 28_800, session_id: str | None = None) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    payload = {"sub": username, "role": role, "csrf": csrf, "sid": session_id or secrets.token_urlsafe(24), "exp": int(time.time()) + ttl_seconds}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}", csrf


def read_session(token: str | None, secret: str) -> dict[str, Any] | None:
    try:
        body, signature = (token or "").rsplit(".", 1)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if int(payload["exp"]) < int(time.time()) or payload.get("role") not in ROLE_LEVEL:
            return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def required_role(method: str, path: str) -> str:
    if path in {"/health", "/health/metrics", "/api/admin/audit/verify"}:
        return "admin"
    if method == "GET":
        return "viewer"
    if path.startswith("/api/account/"):
        return "viewer"
    if path.startswith("/api/import/") or path == "/api/tickets/import":
        return "admin"
    if path.startswith("/api/admin/"):
        return "admin"
    if any(path.endswith(action) for action in ("/approve", "/reject", "/publish", "/verify")):
        return "approver"
    return "operator"


def required_permission(method: str, path: str) -> str:
    if method == "GET":
        return "read"
    if path.startswith("/api/admin/"):
        return "*"
    if any(path.endswith(action) for action in ("/approve", "/reject", "/publish", "/verify")):
        return "approve"
    if path.startswith("/api/channels/") or path.startswith("/api/jobs/"):
        return "channel.sync"
    if path.startswith("/api/sync-failures/"):
        return "sync_failure.manage"
    if path.startswith("/api/purchase-order-items/") and path.endswith("/receive"):
        return "inventory.receive"
    if path == "/api/inventory-adjustments" or path.startswith("/api/inventory-adjustments/"):
        return "inventory.adjust"
    if path.startswith("/api/purchase-order-cancellations/"):
        return "procurement.write"
    if path.startswith("/api/service-action-reversals/"):
        return "ticket.write"
    if path.startswith("/api/replenishment-plans/") or path.startswith("/api/purchase-orders/"):
        return "procurement.write"
    if path.startswith("/api/listings/"):
        return "listing.write"
    if path.startswith("/api/tickets/") or path.startswith("/api/service-actions/") or path.startswith("/api/feedback"):
        return "ticket.write"
    if path.startswith("/api/products/") or path.startswith("/api/import/"):
        return "catalog.write"
    return "read"


def role_allows(actual: str, required: str) -> bool:
    return ROLE_LEVEL.get(actual, 0) >= ROLE_LEVEL[required]


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, timestamp: int | None = None) -> str:
    counter = int(timestamp or time.time()) // 30
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def verify_totp(secret: str, code: str) -> bool:
    if not code.isdigit() or len(code) != 6:
        return False
    current = int(time.time())
    return any(hmac.compare_digest(totp_code(secret, current + offset), code) for offset in (-30, 0, 30))
