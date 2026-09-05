from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image, UnidentifiedImageError
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.security import create_session, generate_totp_secret, hash_password, read_session, required_role, role_allows, totp_code, verify_password, verify_totp


ROOT = Path(__file__).resolve().parent.parent
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(ROOT / "data" / "media"))).resolve()
PRIVATE_ROOT = Path(os.getenv("PRIVATE_ROOT", str(ROOT / "data" / "private"))).resolve()
BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", str(ROOT / "backups"))).resolve()
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
SESSION_SECRET = os.getenv("SESSION_SECRET", "local-development-secret-change-before-deploy")
MFA_ENCRYPTION_KEY = os.getenv("MFA_ENCRYPTION_KEY", SESSION_SECRET if APP_ENV != "production" else "")
SYNC_INLINE = os.getenv("SYNC_INLINE", "true" if APP_ENV != "production" else "false").strip().lower() == "true"
ATTACHMENT_SCANNER_URL = os.getenv("ATTACHMENT_SCANNER_URL", "").strip()
ATTACHMENT_RETENTION_DAYS = max(1, int(os.getenv("ATTACHMENT_RETENTION_DAYS", "365")))
request_actor: ContextVar[str] = ContextVar("request_actor", default="system")
request_trace: ContextVar[str] = ContextVar("request_trace", default="startup")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger("cross_border_ops")
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    asin TEXT NOT NULL UNIQUE,
    source_title TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL CHECK(price >= 0),
    cost REAL NOT NULL CHECK(cost >= 0),
    monthly_sales REAL NOT NULL CHECK(monthly_sales >= 0),
    rating REAL NOT NULL CHECK(rating BETWEEN 0 AND 5),
    review_count INTEGER NOT NULL CHECK(review_count >= 0),
    competition_score REAL NOT NULL CHECK(competition_score BETWEEN 0 AND 100),
    stock_units INTEGER NOT NULL CHECK(stock_units >= 0),
    daily_sales REAL NOT NULL CHECK(daily_sales >= 0),
    lead_time_days INTEGER NOT NULL CHECK(lead_time_days >= 0),
    selection_score REAL,
    score_breakdown TEXT,
    score_basis TEXT NOT NULL DEFAULT '店内运营数据',
    brand TEXT NOT NULL DEFAULT '',
    parent_sku TEXT,
    variation_theme TEXT NOT NULL DEFAULT '',
    material TEXT NOT NULL DEFAULT '',
    dimensions_cm TEXT NOT NULL DEFAULT '',
    weight_kg REAL NOT NULL DEFAULT 0 CHECK(weight_kg >= 0),
    color TEXT NOT NULL DEFAULT '',
    package_contents TEXT NOT NULL DEFAULT '',
    supplier_name TEXT NOT NULL DEFAULT '',
    supplier_sku TEXT NOT NULL DEFAULT '',
    moq INTEGER NOT NULL DEFAULT 1 CHECK(moq >= 1),
    purchase_lead_days INTEGER NOT NULL DEFAULT 0 CHECK(purchase_lead_days >= 0),
    origin_country TEXT NOT NULL DEFAULT '',
    upc_ean TEXT NOT NULL DEFAULT '',
    compliance_tags TEXT NOT NULL DEFAULT '[]',
    evidence_notes TEXT NOT NULL DEFAULT '',
    image_path TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','inactive')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    title TEXT NOT NULL,
    bullet_points TEXT NOT NULL,
    description TEXT NOT NULL,
    search_terms TEXT NOT NULL,
    title_zh TEXT NOT NULL,
    bullet_points_zh TEXT NOT NULL,
    description_zh TEXT NOT NULL,
    search_terms_zh TEXT NOT NULL,
    compliance_warnings TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','approved','rejected','mock_published')),
    provider TEXT NOT NULL,
    review_notes_cn TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    approved_by TEXT,
    approved_at TEXT,
    rejection_reason TEXT,
    evidence_snapshot TEXT NOT NULL DEFAULT '[]',
    compliance_status TEXT NOT NULL DEFAULT 'needs_review',
    compliance_errors TEXT NOT NULL DEFAULT '[]',
    prompt_version TEXT NOT NULL DEFAULT 'listing-v1',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    entity_id INTEGER,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    input_snapshot TEXT NOT NULL,
    output_snapshot TEXT,
    error TEXT,
    prompt_version TEXT NOT NULL DEFAULT 'legacy',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    refund_requested INTEGER NOT NULL CHECK(refund_requested IN (0,1)),
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    topic TEXT NOT NULL,
    priority TEXT NOT NULL CHECK(priority IN ('P0','P1','P2')),
    sla TEXT NOT NULL,
    reply_draft TEXT,
    action_plan TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    external_event_id TEXT,
    event_type TEXT,
    source TEXT,
    order_id_masked TEXT,
    customer_alias TEXT,
    event_at TEXT,
    synced_at TEXT,
    is_archived INTEGER NOT NULL DEFAULT 0,
    assigned_to TEXT NOT NULL DEFAULT '待分配',
    due_at TEXT,
    resolution TEXT,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS publish_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL UNIQUE REFERENCES listings(id),
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status = 'mock_published'),
    snapshot TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    request_id TEXT NOT NULL DEFAULT 'legacy',
    previous_hash TEXT NOT NULL DEFAULT '',
    event_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS channel_connections (
    channel TEXT PRIMARY KEY,
    marketplace TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    last_inventory_sync_at TEXT,
    last_ticket_sync_at TEXT,
    last_feedback_sync_at TEXT,
    last_order_sync_at TEXT,
    last_finance_sync_at TEXT,
    last_market_sync_at TEXT,
    last_result TEXT
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    fetched INTEGER NOT NULL,
    inserted INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_snapshots (
    external_event_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    fulfillable INTEGER NOT NULL CHECK(fulfillable >= 0),
    reserved INTEGER NOT NULL CHECK(reserved >= 0),
    inbound INTEGER NOT NULL CHECK(inbound >= 0),
    unfulfillable INTEGER NOT NULL CHECK(unfulfillable >= 0),
    daily_sales REAL NOT NULL CHECK(daily_sales >= 0),
    lead_time_days INTEGER NOT NULL CHECK(lead_time_days >= 0),
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_insights (
    external_event_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    asin TEXT NOT NULL,
    positive_topic TEXT NOT NULL,
    negative_topic TEXT NOT NULL,
    mention_count INTEGER NOT NULL CHECK(mention_count >= 0),
    rating_impact REAL NOT NULL,
    trend_label TEXT NOT NULL,
    period TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('viewer','operator','approver','admin')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    mfa_secret TEXT,
    mfa_enabled INTEGER NOT NULL DEFAULT 0,
    password_changed_at TEXT,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS login_attempts (
    identifier TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    first_at TEXT NOT NULL,
    locked_until TEXT
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    external_event_id TEXT,
    sku TEXT,
    error_code TEXT NOT NULL,
    error_message TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    last_error TEXT,
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    current_step TEXT NOT NULL DEFAULT '等待执行',
    result TEXT,
    heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    current_job_id INTEGER,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    version INTEGER NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    UNIQUE(product_id, version)
);
CREATE TABLE IF NOT EXISTS listing_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    version INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(listing_id, version, action)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_order_id TEXT NOT NULL UNIQUE,
    order_id_masked TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','unshipped','shipped','delivered','cancelled','partially_refunded','refunded')),
    fulfillment_channel TEXT NOT NULL,
    purchase_at TEXT NOT NULL,
    latest_ship_at TEXT,
    latest_delivery_at TEXT,
    currency TEXT NOT NULL DEFAULT 'USD',
    item_total REAL NOT NULL DEFAULT 0 CHECK(item_total >= 0),
    shipping_total REAL NOT NULL DEFAULT 0 CHECK(shipping_total >= 0),
    tax_total REAL NOT NULL DEFAULT 0 CHECK(tax_total >= 0),
    promotion_total REAL NOT NULL DEFAULT 0 CHECK(promotion_total >= 0),
    refund_total REAL NOT NULL DEFAULT 0 CHECK(refund_total >= 0),
    buyer_alias TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    external_item_id TEXT NOT NULL UNIQUE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    sku TEXT NOT NULL,
    asin TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    item_price REAL NOT NULL CHECK(item_price >= 0),
    item_tax REAL NOT NULL DEFAULT 0 CHECK(item_tax >= 0),
    promotion_discount REAL NOT NULL DEFAULT 0 CHECK(promotion_discount >= 0),
    item_status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_return_id TEXT NOT NULL UNIQUE,
    order_id INTEGER REFERENCES orders(id),
    order_item_id INTEGER REFERENCES order_items(id),
    sku TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    refund_amount REAL NOT NULL DEFAULT 0 CHECK(refund_amount >= 0),
    status TEXT NOT NULL CHECK(status IN ('requested','authorized','in_transit','received','refunded','closed')),
    carrier TEXT,
    tracking_masked TEXT,
    requested_at TEXT NOT NULL,
    received_at TEXT,
    refunded_at TEXT,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    action_plan TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','approved','pending_send','sent_demo','failed')),
    created_by TEXT NOT NULL,
    edited_by TEXT,
    approved_by TEXT,
    approved_at TEXT,
    sent_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(ticket_id,version)
);
CREATE TABLE IF NOT EXISTS service_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    order_id INTEGER REFERENCES orders(id),
    action_type TEXT NOT NULL CHECK(action_type IN ('refund','reship','replacement','cancel')),
    amount REAL NOT NULL DEFAULT 0 CHECK(amount >= 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested','approved','completed','cancelled')),
    requested_by TEXT NOT NULL,
    approved_by TEXT,
    requested_at TEXT NOT NULL,
    approved_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    currency TEXT NOT NULL DEFAULT 'USD',
    payment_terms TEXT NOT NULL,
    lead_time_days INTEGER NOT NULL CHECK(lead_time_days >= 0),
    status TEXT NOT NULL CHECK(status IN ('active','inactive')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replenishment_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    sku TEXT NOT NULL,
    fulfillable_at_creation INTEGER NOT NULL,
    daily_sales_at_creation REAL NOT NULL,
    lead_time_days INTEGER NOT NULL,
    safety_days INTEGER NOT NULL,
    suggested_qty INTEGER NOT NULL CHECK(suggested_qty >= 0),
    approved_qty INTEGER CHECK(approved_qty >= 0),
    status TEXT NOT NULL CHECK(status IN ('draft','approved','rejected','converted')),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_number TEXT NOT NULL UNIQUE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    status TEXT NOT NULL CHECK(status IN ('draft','approved','sent_demo','partially_received','received','cancelled')),
    currency TEXT NOT NULL,
    total_amount REAL NOT NULL CHECK(total_amount >= 0),
    expected_at TEXT,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS purchase_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_order_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    replenishment_plan_id INTEGER NOT NULL UNIQUE REFERENCES replenishment_plans(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    sku TEXT NOT NULL,
    ordered_qty INTEGER NOT NULL CHECK(ordered_qty > 0),
    received_qty INTEGER NOT NULL DEFAULT 0 CHECK(received_qty >= 0),
    rejected_qty INTEGER NOT NULL DEFAULT 0 CHECK(rejected_qty >= 0),
    unit_cost REAL NOT NULL CHECK(unit_cost >= 0)
);
CREATE TABLE IF NOT EXISTS warehouse_inventory (
    sku TEXT PRIMARY KEY,
    on_hand INTEGER NOT NULL DEFAULT 0 CHECK(on_hand >= 0),
    qc_hold INTEGER NOT NULL DEFAULT 0 CHECK(qc_hold >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    movement_type TEXT NOT NULL CHECK(movement_type IN ('purchase_receipt','adjustment','fba_inbound')),
    quantity INTEGER NOT NULL,
    reference_type TEXT NOT NULL,
    reference_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL,
    sku TEXT NOT NULL,
    fulfillable INTEGER NOT NULL,
    reserved INTEGER NOT NULL,
    inbound INTEGER NOT NULL,
    unfulfillable INTEGER NOT NULL,
    daily_sales REAL NOT NULL,
    lead_time_days INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(batch_key, sku)
);
CREATE TABLE IF NOT EXISTS cost_profiles (
    product_id INTEGER PRIMARY KEY REFERENCES products(id),
    referral_rate REAL NOT NULL DEFAULT 0.15 CHECK(referral_rate BETWEEN 0 AND 1),
    fba_fee_per_unit REAL NOT NULL DEFAULT 0 CHECK(fba_fee_per_unit >= 0),
    inbound_cost_per_unit REAL NOT NULL DEFAULT 0 CHECK(inbound_cost_per_unit >= 0),
    ad_rate REAL NOT NULL DEFAULT 0 CHECK(ad_rate BETWEEN 0 AND 1),
    other_cost_per_unit REAL NOT NULL DEFAULT 0 CHECK(other_cost_per_unit >= 0),
    effective_from TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_transaction_id TEXT NOT NULL UNIQUE,
    order_id INTEGER REFERENCES orders(id),
    sku TEXT NOT NULL,
    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('principal','tax','promotion','refund','referral_fee','fba_fee','advertising','inbound_cost','other')),
    amount REAL NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    posted_at TEXT NOT NULL,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_research_snapshots (
    external_event_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    keyword TEXT NOT NULL,
    estimated_monthly_demand REAL NOT NULL CHECK(estimated_monthly_demand >= 0),
    median_price REAL NOT NULL CHECK(median_price >= 0),
    median_review_count INTEGER NOT NULL CHECK(median_review_count >= 0),
    competitor_count INTEGER NOT NULL CHECK(competitor_count >= 0),
    average_rating REAL NOT NULL CHECK(average_rating BETWEEN 0 AND 5),
    trend_score REAL NOT NULL CHECK(trend_score BETWEEN 0 AND 100),
    source_mode TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    field_name TEXT NOT NULL,
    value TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','verified','rejected')),
    created_by TEXT NOT NULL,
    verified_by TEXT,
    created_at TEXT NOT NULL,
    verified_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS category_compliance_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_keyword TEXT NOT NULL UNIQUE,
    prohibited_terms TEXT NOT NULL,
    required_evidence_fields TEXT NOT NULL,
    guidance_cn TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS trademark_watchlist (
    term TEXT PRIMARY KEY,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('block','review')),
    note TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS user_sessions (
    session_id TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    user_agent TEXT NOT NULL,
    ip_masked TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS ticket_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    scan_status TEXT NOT NULL DEFAULT 'clean' CHECK(scan_status IN ('clean','quarantined')),
    uploaded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_event_id TEXT NOT NULL REFERENCES feedback_insights(external_event_id),
    sku TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('listing_update','product_quality','supplier_corrective_action','packaging_change')),
    owner TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','in_progress','resolved')),
    outcome TEXT,
    linked_ticket_id INTEGER REFERENCES tickets(id),
    linked_plan_id INTEGER REFERENCES replenishment_plans(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS operational_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('critical','warning','info')),
    title TEXT NOT NULL,
    href TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('unread','read','resolved')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    read_by TEXT,
    read_at TEXT
);
"""

PRODUCT_FIELDS = (
    "sku", "asin", "source_title", "category", "price", "cost",
    "monthly_sales", "rating", "review_count", "competition_score",
    "stock_units", "daily_sales", "lead_time_days",
)
NUMBER_FIELDS = {
    "price": float, "cost": float, "monthly_sales": float, "rating": float,
    "review_count": int, "competition_score": float, "stock_units": int,
    "daily_sales": float, "lead_time_days": int,
}

OPTIONAL_PRODUCT_FIELDS = (
    "brand", "parent_sku", "variation_theme", "material", "dimensions_cm", "weight_kg",
    "color", "package_contents", "supplier_name", "supplier_sku", "moq", "purchase_lead_days",
    "origin_country", "upc_ean", "compliance_tags", "evidence_notes", "status",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def display_time(value: str | None) -> str:
    return value[:16].replace("T", " ") + " UTC" if value else "尚未同步"


def mfa_cipher() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(MFA_ENCRYPTION_KEY.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_mfa_secret(secret: str) -> str:
    return mfa_cipher().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_mfa_secret(value: str) -> str:
    if not value.startswith("gAAAA"):
        return value  # migration compatibility; next successful setup rewrites it encrypted
    try:
        return mfa_cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("MFA 密钥无法解密，请联系管理员重置 MFA") from exc


def db_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", str(ROOT / "data" / "app.db")))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def validate_runtime_config() -> None:
    if APP_ENV != "production":
        return
    errors = []
    if SESSION_SECRET == "local-development-secret-change-before-deploy" or SESSION_SECRET.startswith("replace-") or len(SESSION_SECRET) < 32:
        errors.append("SESSION_SECRET 必须是至少 32 位的独立生产密钥")
    if len(os.getenv("ADMIN_PASSWORD", "")) < 12 or os.getenv("ADMIN_PASSWORD", "").startswith("replace-"):
        errors.append("ADMIN_PASSWORD 必须至少 12 位")
    if len(MFA_ENCRYPTION_KEY) < 32 or MFA_ENCRYPTION_KEY == SESSION_SECRET:
        errors.append("MFA_ENCRYPTION_KEY 必须是独立于 SESSION_SECRET 的至少 32 位密钥")
    if not os.getenv("AI_API_KEY") and os.getenv("ALLOW_DEMO_AI", "false").lower() != "true":
        errors.append("生产环境禁止静默使用 Demo AI")
    if int(os.getenv("WEB_CONCURRENCY", "1")) != 1:
        errors.append("SQLite 部署仅支持单 Web 进程；多进程前请迁移 PostgreSQL")
    if not ATTACHMENT_SCANNER_URL:
        errors.append("生产环境必须配置 ATTACHMENT_SCANNER_URL，未通过恶意文件扫描的附件不得落盘")
    if errors:
        raise RuntimeError("生产配置校验失败: " + "; ".join(errors))


def init_db() -> None:
    with closing(connect()) as conn:
        conn.executescript(SCHEMA)
        conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        conn.execute("DROP TRIGGER IF EXISTS audit_no_delete")
        product_columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
        for name, definition in {
            "brand": "TEXT NOT NULL DEFAULT ''", "parent_sku": "TEXT",
            "variation_theme": "TEXT NOT NULL DEFAULT ''", "material": "TEXT NOT NULL DEFAULT ''",
            "dimensions_cm": "TEXT NOT NULL DEFAULT ''", "weight_kg": "REAL NOT NULL DEFAULT 0",
            "color": "TEXT NOT NULL DEFAULT ''", "package_contents": "TEXT NOT NULL DEFAULT ''",
            "supplier_name": "TEXT NOT NULL DEFAULT ''", "supplier_sku": "TEXT NOT NULL DEFAULT ''",
            "moq": "INTEGER NOT NULL DEFAULT 1", "purchase_lead_days": "INTEGER NOT NULL DEFAULT 0",
            "origin_country": "TEXT NOT NULL DEFAULT ''", "upc_ean": "TEXT NOT NULL DEFAULT ''",
            "compliance_tags": "TEXT NOT NULL DEFAULT '[]'", "evidence_notes": "TEXT NOT NULL DEFAULT ''",
            "image_path": "TEXT", "status": "TEXT NOT NULL DEFAULT 'active'",
            "version": "INTEGER NOT NULL DEFAULT 1", "updated_at": "TEXT",
            "score_basis": "TEXT NOT NULL DEFAULT '店内运营数据'",
        }.items():
            if name not in product_columns:
                conn.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
        for name, definition in {
            "external_event_id": "TEXT", "event_type": "TEXT", "source": "TEXT",
            "order_id_masked": "TEXT", "customer_alias": "TEXT", "event_at": "TEXT",
            "synced_at": "TEXT", "is_archived": "INTEGER NOT NULL DEFAULT 0",
            "assigned_to": "TEXT NOT NULL DEFAULT '待分配'", "due_at": "TEXT",
            "resolution": "TEXT", "resolved_at": "TEXT",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "order_ref_id": "INTEGER REFERENCES orders(id)", "return_ref_id": "INTEGER REFERENCES returns(id)",
            "refund_amount": "REAL NOT NULL DEFAULT 0", "conversation": "TEXT NOT NULL DEFAULT '[]'",
            "attachments": "TEXT NOT NULL DEFAULT '[]'", "escalation_level": "TEXT NOT NULL DEFAULT 'none'",
            "reopen_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {definition}")
        listing_columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
        for name, definition in {
            "review_notes_cn": "TEXT", "version": "INTEGER NOT NULL DEFAULT 1", "approved_by": "TEXT", "approved_at": "TEXT",
            "title_zh": "TEXT", "bullet_points_zh": "TEXT", "description_zh": "TEXT", "search_terms_zh": "TEXT",
            "rejection_reason": "TEXT",
            "evidence_snapshot": "TEXT NOT NULL DEFAULT '[]'", "compliance_status": "TEXT NOT NULL DEFAULT 'needs_review'",
            "compliance_errors": "TEXT NOT NULL DEFAULT '[]'",
            "prompt_version": "TEXT NOT NULL DEFAULT 'listing-v1'",
        }.items():
            if name not in listing_columns:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {definition}")
        audit_columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}
        for name, definition in {"actor": "TEXT NOT NULL DEFAULT 'system'", "request_id": "TEXT NOT NULL DEFAULT 'legacy'"}.items():
            if name not in audit_columns:
                conn.execute(f"ALTER TABLE audit_events ADD COLUMN {name} {definition}")
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        for name, definition in {
            "must_change_password": "INTEGER NOT NULL DEFAULT 0", "mfa_secret": "TEXT",
            "mfa_enabled": "INTEGER NOT NULL DEFAULT 0", "password_changed_at": "TEXT", "last_login_at": "TEXT",
        }.items():
            if name not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        connection_columns = {row[1] for row in conn.execute("PRAGMA table_info(channel_connections)")}
        for column in ("last_order_sync_at", "last_finance_sync_at", "last_market_sync_at"):
            if column not in connection_columns:
                conn.execute(f"ALTER TABLE channel_connections ADD COLUMN {column} TEXT")
        job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, definition in {
            "progress": "INTEGER NOT NULL DEFAULT 0", "current_step": "TEXT NOT NULL DEFAULT '等待执行'",
            "result": "TEXT", "heartbeat_at": "TEXT",
        }.items():
            if name not in job_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        ai_columns = {row[1] for row in conn.execute("PRAGMA table_info(ai_runs)")}
        for name, definition in {
            "prompt_version": "TEXT NOT NULL DEFAULT 'legacy'", "latency_ms": "INTEGER NOT NULL DEFAULT 0",
            "input_tokens": "INTEGER NOT NULL DEFAULT 0", "output_tokens": "INTEGER NOT NULL DEFAULT 0",
            "estimated_cost_usd": "REAL NOT NULL DEFAULT 0",
        }.items():
            if name not in ai_columns:
                conn.execute(f"ALTER TABLE ai_runs ADD COLUMN {name} {definition}")
        finance_columns = {row[1] for row in conn.execute("PRAGMA table_info(financial_transactions)")}
        if "amount_cents" not in finance_columns:
            conn.execute("ALTER TABLE financial_transactions ADD COLUMN amount_cents INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE financial_transactions SET amount_cents=CAST(ROUND(amount * 100) AS INTEGER)")
        po_item_columns = {row[1] for row in conn.execute("PRAGMA table_info(purchase_order_items)")}
        if "rejected_qty" not in po_item_columns:
            conn.execute("ALTER TABLE purchase_order_items ADD COLUMN rejected_qty INTEGER NOT NULL DEFAULT 0")
        attachment_columns = {row[1] for row in conn.execute("PRAGMA table_info(ticket_attachments)")}
        for name, definition in (("sha256", "TEXT NOT NULL DEFAULT ''"), ("scan_status", "TEXT NOT NULL DEFAULT 'clean'"), ("expires_at", "TEXT"), ("deleted_at", "TEXT")):
            if name not in attachment_columns:
                conn.execute(f"ALTER TABLE ticket_attachments ADD COLUMN {name} {definition}")
        audit_columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}
        for name in ("previous_hash", "event_hash"):
            if name not in audit_columns:
                conn.execute(f"ALTER TABLE audit_events ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        previous_hash = ""
        for event in conn.execute("SELECT id,action,entity_type,entity_id,detail,created_at,actor,request_id,event_hash FROM audit_events ORDER BY id"):
            expected_hash = audit_event_hash(previous_hash, event["action"], event["entity_type"], event["entity_id"], event["detail"], event["created_at"], event["actor"], event["request_id"])
            if not event["event_hash"]:
                conn.execute("UPDATE audit_events SET previous_hash=?,event_hash=? WHERE id=?", (previous_hash, expected_hash, event["id"]))
            previous_hash = event["event_hash"] or expected_hash
        conn.execute("UPDATE listings SET review_notes_cn='历史文案保留原样；再次发布前需确认美国站英文、规格证据和搜索词。' WHERE review_notes_cn IS NULL")
        conn.execute("UPDATE products SET updated_at=created_at WHERE updated_at IS NULL")
        for row in conn.execute("SELECT l.id AS listing_id,p.* FROM listings l JOIN products p ON p.id=l.product_id WHERE l.title_zh IS NULL OR l.bullet_points_zh IS NULL OR l.description_zh IS NULL OR l.search_terms_zh IS NULL"):
            chinese = demo_chinese_listing(dict(row))
            conn.execute(
                "UPDATE listings SET title_zh=?,bullet_points_zh=?,description_zh=?,search_terms_zh=? WHERE id=?",
                (chinese["title_zh"], json.dumps(chinese["bullet_points_zh"], ensure_ascii=False), chinese["description_zh"], json.dumps(chinese["search_terms_zh"], ensure_ascii=False), row["listing_id"]),
            )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_external_event ON tickets(external_event_id) WHERE external_event_id IS NOT NULL")
        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_tickets_active_priority ON tickets(is_archived,status,priority,event_at);
        CREATE INDEX IF NOT EXISTS idx_listings_status_updated ON listings(status,updated_at);
        CREATE INDEX IF NOT EXISTS idx_inventory_sku_synced ON inventory_snapshots(sku,synced_at);
        CREATE INDEX IF NOT EXISTS idx_feedback_asin_period ON feedback_insights(asin,period);
        CREATE INDEX IF NOT EXISTS idx_sync_runs_type_completed ON sync_runs(sync_type,completed_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status,available_at);
        CREATE INDEX IF NOT EXISTS idx_products_status_category ON products(status,category,id);
        CREATE INDEX IF NOT EXISTS idx_product_changes_product ON product_changes(product_id,version DESC);
        CREATE INDEX IF NOT EXISTS idx_listing_revisions_listing ON listing_revisions(listing_id,version DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_purchase_status ON orders(purchase_at DESC,status);
        CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
        CREATE INDEX IF NOT EXISTS idx_returns_order_status ON returns(order_id,status);
        CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id,id);
        CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket ON ticket_messages(ticket_id,id DESC);
        CREATE INDEX IF NOT EXISTS idx_service_actions_ticket ON service_actions(ticket_id,status);
        CREATE INDEX IF NOT EXISTS idx_replenishment_status ON replenishment_plans(status,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_purchase_orders_status ON purchase_orders(status,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_inventory_movements_sku ON inventory_movements(sku,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_active_type ON jobs(job_type,status,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_inventory_history_sku_time ON inventory_history(sku,observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_financial_sku_time ON financial_transactions(sku,posted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_market_research_sku_time ON market_research_snapshots(sku,captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_product_evidence_product_status ON product_evidence(product_id,status,field_name);
        CREATE INDEX IF NOT EXISTS idx_user_sessions_user_active ON user_sessions(username,revoked_at,expires_at);
        CREATE INDEX IF NOT EXISTS idx_mfa_recovery_user_unused ON mfa_recovery_codes(username,used_at);
        CREATE INDEX IF NOT EXISTS idx_ticket_attachments_ticket ON ticket_attachments(ticket_id,id);
        CREATE INDEX IF NOT EXISTS idx_feedback_actions_event ON feedback_actions(feedback_event_id,status);
        CREATE INDEX IF NOT EXISTS idx_notifications_status_severity ON operational_notifications(status,severity,last_seen_at DESC);
        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS tickets_product_guard BEFORE INSERT ON tickets WHEN NOT EXISTS(SELECT 1 FROM products WHERE sku=NEW.sku) BEGIN SELECT RAISE(ABORT,'unknown product sku'); END;
        CREATE TRIGGER IF NOT EXISTS tickets_product_update_guard BEFORE UPDATE OF sku ON tickets WHEN NOT EXISTS(SELECT 1 FROM products WHERE sku=NEW.sku) BEGIN SELECT RAISE(ABORT,'unknown product sku'); END;
        CREATE TRIGGER IF NOT EXISTS inventory_product_guard BEFORE INSERT ON inventory_snapshots WHEN NOT EXISTS(SELECT 1 FROM products WHERE sku=NEW.sku) BEGIN SELECT RAISE(ABORT,'unknown product sku'); END;
        CREATE TRIGGER IF NOT EXISTS inventory_product_update_guard BEFORE UPDATE OF sku ON inventory_snapshots WHEN NOT EXISTS(SELECT 1 FROM products WHERE sku=NEW.sku) BEGIN SELECT RAISE(ABORT,'unknown product sku'); END;
        CREATE TRIGGER IF NOT EXISTS feedback_product_guard BEFORE INSERT ON feedback_insights WHEN NOT EXISTS(SELECT 1 FROM products WHERE sku=NEW.sku) BEGIN SELECT RAISE(ABORT,'unknown product sku'); END;
        CREATE TRIGGER IF NOT EXISTS feedback_product_update_guard BEFORE UPDATE OF sku ON feedback_insights WHEN NOT EXISTS(SELECT 1 FROM products WHERE sku=NEW.sku) BEGIN SELECT RAISE(ABORT,'unknown product sku'); END;
        CREATE TRIGGER IF NOT EXISTS tickets_status_guard BEFORE UPDATE OF status ON tickets WHEN NEW.status NOT IN ('open','drafted','processing','resolved','closed') BEGIN SELECT RAISE(ABORT,'invalid ticket status'); END;
        """)
        admin_password = os.getenv("ADMIN_PASSWORD", "Admin123!ChangeMe")
        conn.execute("INSERT OR IGNORE INTO users(username,password_hash,role,is_active,created_at,must_change_password) VALUES(?,?,?,?,?,?)", (os.getenv("ADMIN_USERNAME", "admin"), hash_password(admin_password), "admin", 1, now(), 1 if APP_ENV == "production" else 0))
        if APP_ENV == "production":
            conn.execute("UPDATE users SET must_change_password=1 WHERE role='admin' AND password_changed_at IS NULL")
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(3,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(4,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(5,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(6,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(7,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(8,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(9,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(10,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(11,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(12,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(13,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(14,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(15,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(16,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(17,?)", (now(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(18,?)", (now(),))
        conn.execute("""INSERT OR IGNORE INTO cost_profiles(product_id,referral_rate,fba_fee_per_unit,inbound_cost_per_unit,ad_rate,other_cost_per_unit,effective_from,updated_by,updated_at)
            SELECT id,0.15,3.40 + MIN(price * 0.04,2.0),MAX(0.55,cost * 0.12),0.08 + competition_score / 100 * 0.08,0,created_at,'system:migration',COALESCE(updated_at,created_at) FROM products""")
        for supplier in (
            ("SUP-GENERAL-US", "演示综合供应商", "USD", "30% 预付款，70% 出货前", 24),
            ("SUP-SHENZHEN-01", "深圳优选供应链", "USD", "月结 30 天", 21),
            ("SUP-NINGBO-01", "宁波家居制造", "USD", "20% 预付款，80% 提单后", 30),
        ):
            conn.execute("INSERT OR IGNORE INTO suppliers(supplier_code,name,currency,payment_terms,lead_time_days,status,created_at) VALUES(?,?,?,?,?,'active',?)", (*supplier, now()))
        for rule in (
            ("电子", ["medical grade", "cures", "guaranteed safe"], ["material", "dimensions_cm"], "核验材料、尺寸、电气规格及适用认证。"),
            ("灯", ["fireproof", "explosion proof", "100% waterproof"], ["material", "dimensions_cm"], "核验防护等级、电池和电气认证，不得扩大防水或安全承诺。"),
            ("宠物", ["veterinarian approved", "prevents disease", "non-toxic"], ["material", "package_contents"], "健康与无毒表述必须有可追溯检测或专业证据。"),
            ("厨房", ["food grade", "bpa free", "antibacterial"], ["material", "dimensions_cm"], "食品接触、材料和抗菌表述必须有对应证明。"),
            ("default", ["best seller", "#1", "guaranteed", "lifetime warranty"], ["source_title"], "至少核验商品身份；任何规格、认证和性能承诺需有证据。"),
        ):
            conn.execute("INSERT OR IGNORE INTO category_compliance_rules(category_keyword,prohibited_terms,required_evidence_fields,guidance_cn) VALUES(?,?,?,?)", (rule[0], json.dumps(rule[1]), json.dumps(rule[2]), rule[3]))
        for term, risk, note in (("amazon", "review", "平台商标仅能在允许语境使用"), ("kindle", "block", "非授权商品不得使用 Kindle 商标"), ("prime", "block", "不得在商品文案中暗示 Prime 资格")):
            conn.execute("INSERT OR IGNORE INTO trademark_watchlist(term,risk_level,note) VALUES(?,?,?)", (term, risk, note))
        conn.execute(
            "INSERT OR IGNORE INTO channel_connections(channel,marketplace,mode,status) VALUES('amazon-us','Amazon US','demo_sp_api','connected_demo')"
        )
        legacy = conn.execute(
            "SELECT t.*,p.asin FROM tickets t LEFT JOIN products p ON p.sku=t.sku WHERE t.external_event_id IS NULL AND COALESCE(t.is_archived,0)=0"
        ).fetchall()
        for row in legacy:
            if row["asin"]:
                positive = "使用体验良好" if row["rating"] >= 4 else "暂无显著正面主题"
                negative = "暂无显著负面主题" if row["rating"] >= 4 else row["title"]
                conn.execute(
                    "INSERT OR IGNORE INTO feedback_insights VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"legacy-feedback-{row['id']}", row["sku"], row["asin"], positive, negative, 1,
                     round((row["rating"] - 3) * 0.1, 2), "历史迁移样例", "2026-07", row["created_at"]),
                )
        conn.execute(
            "UPDATE tickets SET is_archived=1,event_type='legacy_review',source='历史 CSV 评价' WHERE external_event_id IS NULL AND COALESCE(is_archived,0)=0"
        )
        conn.execute("UPDATE feedback_insights SET period='2026-07',trend_label='历史迁移样例' WHERE external_event_id LIKE 'legacy-feedback-%'")
        for row in conn.execute("SELECT id,event_at,priority FROM tickets WHERE due_at IS NULL AND event_at IS NOT NULL"):
            conn.execute("UPDATE tickets SET due_at=? WHERE id=?", (ticket_due_at(row["event_at"], row["priority"]), row["id"]))
        conn.commit()


def audit_event_hash(previous_hash: str, action: str, entity_type: str, entity_id: int | None, detail: str, created_at: str, actor: str, request_id: str) -> str:
    payload = "\x1f".join((previous_hash, action, entity_type, str(entity_id or ""), detail, created_at, actor, request_id))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_audit_chain(conn: sqlite3.Connection) -> bool:
    previous_hash = ""
    for event in conn.execute("SELECT * FROM audit_events ORDER BY id"):
        expected = audit_event_hash(previous_hash, event["action"], event["entity_type"], event["entity_id"], event["detail"], event["created_at"], event["actor"], event["request_id"])
        if event["previous_hash"] != previous_hash or not hmac.compare_digest(event["event_hash"], expected):
            return False
        previous_hash = event["event_hash"]
    return True


def verify_audit_tail(conn: sqlite3.Connection, limit: int = 100) -> bool:
    events = list(reversed(conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()))
    for event in events:
        expected = audit_event_hash(event["previous_hash"], event["action"], event["entity_type"], event["entity_id"], event["detail"], event["created_at"], event["actor"], event["request_id"])
        if not hmac.compare_digest(event["event_hash"], expected):
            return False
    for previous, current in zip(events, events[1:]):
        if current["previous_hash"] != previous["event_hash"]:
            return False
    return True


def audit(conn: sqlite3.Connection, action: str, entity_type: str, entity_id: int | None, detail: Any) -> None:
    detail_json, created_at, actor, request_id = json.dumps(detail, ensure_ascii=False), now(), request_actor.get(), request_trace.get()
    previous = conn.execute("SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
    previous_hash = previous["event_hash"] if previous else ""
    event_hash = audit_event_hash(previous_hash, action, entity_type, entity_id, detail_json, created_at, actor, request_id)
    conn.execute(
        "INSERT INTO audit_events(action,entity_type,entity_id,detail,created_at,actor,request_id,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?)",
        (action, entity_type, entity_id, detail_json, created_at, actor, request_id, previous_hash, event_hash),
    )


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def product_image_url(sku: str, image_path: str | None = None) -> str | None:
    if image_path:
        candidate = (MEDIA_ROOT / image_path).resolve()
        if candidate.is_relative_to(MEDIA_ROOT) and candidate.is_file():
            return f"/media/{image_path}"
    image_name = f"{sku}.png"
    return f"/static/products/{image_name}" if (ROOT / "app" / "static" / "products" / image_name).is_file() else None


def product_snapshot(product: dict[str, Any]) -> dict[str, Any]:
    return {key: product.get(key) for key in (
        "id", "sku", "asin", "source_title", "category", "price", "cost", "brand", "parent_sku",
        "variation_theme", "material", "dimensions_cm", "weight_kg", "color", "package_contents",
        "supplier_name", "supplier_sku", "moq", "purchase_lead_days", "origin_country", "upc_ean",
        "compliance_tags", "evidence_notes", "image_path", "status", "version", "updated_at",
    )}


def listing_snapshot(listing: dict[str, Any]) -> dict[str, Any]:
    value = dict(listing)
    for key in ("bullet_points", "search_terms", "bullet_points_zh", "search_terms_zh", "compliance_warnings"):
        if isinstance(value.get(key), str):
            value[key] = json.loads(value[key])
    return value


def record_listing_revision(conn: sqlite3.Connection, listing_id: int, action: str, reason: str | None = None) -> None:
    listing = row_dict(conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone())
    if listing:
        conn.execute(
            "INSERT OR IGNORE INTO listing_revisions(listing_id,version,action,actor,reason,snapshot,created_at) VALUES(?,?,?,?,?,?,?)",
            (listing_id, listing["version"], action, request_actor.get(), reason, json.dumps(listing_snapshot(listing), ensure_ascii=False), now()),
        )


EVIDENCE_FIELDS = {"source_title", "brand", "material", "dimensions_cm", "weight_kg", "color", "package_contents", "origin_country", "upc_ean", "compliance", "performance", "compatibility", "warranty"}


def verified_evidence(conn: sqlite3.Connection, product_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT id,field_name,value,source_name,source_reference,verified_by,verified_at,version FROM product_evidence WHERE product_id=? AND status='verified' ORDER BY id", (product_id,))]


def listing_compliance_check(conn: sqlite3.Connection, product_id: int, value: dict[str, Any], evidence: list[dict[str, Any]] | None = None) -> tuple[list[str], list[str]]:
    product = conn.execute("SELECT category FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return ["商品不存在"], []
    evidence = evidence if evidence is not None else verified_evidence(conn, product_id)
    verified_fields = {item["field_name"] for item in evidence}
    rules = [dict(row) for row in conn.execute("SELECT * FROM category_compliance_rules WHERE is_active=1 AND (category_keyword='default' OR instr(?,category_keyword)>0)", (product["category"],))]
    required_fields: set[str] = set()
    prohibited: set[str] = set()
    warnings: list[str] = []
    for rule in rules:
        required_fields.update(json.loads(rule["required_evidence_fields"]))
        prohibited.update(term.casefold() for term in json.loads(rule["prohibited_terms"]))
        warnings.append(rule["guidance_cn"])
    customer_copy = " ".join([value["title"], *value["bullet_points"], value["description"], *value["search_terms"]])
    lower_copy = customer_copy.casefold()
    errors = [f"缺少已核验证据字段：{field}" for field in sorted(required_fields - verified_fields)]
    errors.extend(f"命中类目禁限词：{term}" for term in sorted(prohibited) if term in lower_copy)
    evidence_text = " ".join(item["value"] for item in evidence).casefold()
    unsupported_numbers = sorted(set(re.findall(r"\b\d+(?:\.\d+)?\b", lower_copy)) - set(re.findall(r"\b\d+(?:\.\d+)?\b", evidence_text)))
    if unsupported_numbers:
        errors.append("文案中的数字规格缺少证据支持：" + ", ".join(unsupported_numbers))
    for trademark in conn.execute("SELECT * FROM trademark_watchlist WHERE is_active=1"):
        if re.search(rf"\b{re.escape(trademark['term'])}\b", lower_copy):
            if trademark["risk_level"] == "block":
                errors.append(f"命中禁用商标词：{trademark['term']}")
            else:
                warnings.append(f"商标复核：{trademark['term']}；{trademark['note']}")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def product_score(product: dict[str, Any]) -> tuple[float, dict[str, float]]:
    price = float(product["price"])
    margin = max(0.0, min((price - float(product["cost"])) / price if price else 0.0, 1.0))
    market = product.get("market_signal")
    if market:
        parts = {
            "market_demand": min(float(market["estimated_monthly_demand"]) / 20_000, 1) * 30,
            "margin": margin * 25,
            "competitor_density": (1 - min(float(market["competitor_count"]) / 1_200, 1)) * 20,
            "review_barrier": (1 - min(float(market["median_review_count"]) / 3_000, 1)) * 10,
            "market_trend": float(market["trend_score"]) / 100 * 15,
        }
        return round(sum(parts.values()), 2), {key: round(value, 2) for key, value in parts.items()}
    parts = {
        "demand": min(float(product["monthly_sales"]) / 1000, 1) * 30,
        "margin": margin * 30,
        "rating": float(product["rating"]) / 5 * 20,
        "competition": (100 - float(product["competition_score"])) / 100 * 20,
    }
    return round(sum(parts.values()), 2), {key: round(value, 2) for key, value in parts.items()}


def product_economics(product: dict[str, Any], profile: dict[str, Any] | None = None, transactions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    units, price, unit_cost = (Decimal(str(product[key])) for key in ("monthly_sales", "price", "cost"))
    money = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if transactions:
        totals: dict[str, Decimal] = {}
        for transaction in transactions:
            kind = transaction["transaction_type"]
            amount = Decimal(int(transaction["amount_cents"])) / 100 if "amount_cents" in transaction.keys() else Decimal(str(transaction["amount"]))
            totals[kind] = totals.get(kind, Decimal("0")) + amount
        actual_units = Decimal(str(product.get("actual_order_units", 0)))
        revenue = totals.get("principal", Decimal("0")) + totals.get("promotion", Decimal("0"))
        tax_collected = totals.get("tax", Decimal("0"))
        product_cost = actual_units * unit_cost
        referral_fee = abs(totals.get("referral_fee", Decimal("0")))
        fba_fee = abs(totals.get("fba_fee", Decimal("0")))
        inbound_cost = abs(totals.get("inbound_cost", Decimal("0")))
        ad_spend = abs(totals.get("advertising", Decimal("0")))
        refund_loss = abs(totals.get("refund", Decimal("0")))
        other = abs(totals.get("other", Decimal("0")))
        net_profit = revenue - product_cost - referral_fee - fba_fee - inbound_cost - ad_spend - refund_loss - other
        return {
            "revenue": money(revenue), "product_cost": money(product_cost), "referral_fee": money(referral_fee),
            "fba_fee": money(fba_fee), "inbound_cost": money(inbound_cost), "ad_spend": money(ad_spend),
            "refund_loss": money(refund_loss), "net_profit": money(net_profit), "tax_collected": money(tax_collected),
            "margin": (net_profit / revenue * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if revenue else Decimal("0.0"),
            "ad_rate": (ad_spend / revenue * 100).quantize(Decimal("0.1")) if revenue else Decimal("0.0"),
            "refund_rate": (refund_loss / revenue * 100).quantize(Decimal("0.1")) if revenue else Decimal("0.0"),
            "data_source": "结算流水", "transaction_count": len(transactions),
        }
    profile = profile or {}
    revenue = units * price
    referral_rate = Decimal(str(profile.get("referral_rate", 0.15)))
    referral_fee = revenue * referral_rate
    fba_fee_per_unit = Decimal(str(profile.get("fba_fee_per_unit", Decimal("3.40") + min(price * Decimal("0.04"), Decimal("2.0")))))
    inbound_per_unit = Decimal(str(profile.get("inbound_cost_per_unit", max(Decimal("0.55"), unit_cost * Decimal("0.12")))))
    fba_fee = units * fba_fee_per_unit
    inbound_cost = units * inbound_per_unit
    ad_rate = Decimal(str(profile.get("ad_rate", Decimal("0.08") + Decimal(str(product["competition_score"])) / 100 * Decimal("0.08"))))
    ad_spend = revenue * ad_rate
    refund_rate = max(Decimal("0.02"), Decimal("0.11") - Decimal(str(product["rating"])) * Decimal("0.018"))
    refund_loss = revenue * refund_rate * Decimal("0.45")
    net_profit = revenue - units * unit_cost - referral_fee - fba_fee - inbound_cost - ad_spend - refund_loss
    return {
        "revenue": money(revenue), "product_cost": money(units * unit_cost),
        "referral_fee": money(referral_fee), "fba_fee": money(fba_fee),
        "inbound_cost": money(inbound_cost), "ad_spend": money(ad_spend),
        "refund_loss": money(refund_loss), "net_profit": money(net_profit), "tax_collected": money(Decimal("0")),
        "margin": (net_profit / revenue * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if revenue else Decimal("0.0"),
        "ad_rate": (ad_rate * 100).quantize(Decimal("0.1")), "refund_rate": (refund_rate * 100).quantize(Decimal("0.1")),
        "data_source": "费率配置估算", "transaction_count": 0,
    }


def build_today_tasks(products: list[dict[str, Any]], tickets: list[dict[str, Any]], pending_listings: int) -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []
    for ticket in tickets:
        if ticket["priority"] == "P0" and ticket["status"] != "resolved":
            tasks.append({"tone": "red", "title": f"处理 {ticket['order_id_masked']} 紧急售后", "impact": f"负责人：{ticket['assigned_to']} · 截止 {ticket['due_at_display']}", "href": "/tickets"})
    risky = sorted((p for p in products if p["inventory"]["level"] == "critical"), key=lambda p: p["daily_sales"] * p["price"], reverse=True)
    for product in risky[:3]:
        loss = product["daily_sales"] * float(product["price"]) * 7
        tasks.append({"tone": "amber", "title": f"{product['sku']} 建议补货 {product['inventory']['reorder_qty']} 件", "impact": f"若缺货 7 天，预计销售损失 ${loss:,.0f}", "href": "/inventory"})
    low_margin = sorted((p for p in products if p["economics"]["margin"] < 12), key=lambda p: p["economics"]["margin"])
    for product in low_margin[:2]:
        tasks.append({"tone": "amber", "title": f"复核 {product['sku']} 低利润", "impact": f"贡献毛利率 {product['economics']['margin']:.1f}% · 广告占比 {product['economics']['ad_rate']:.1f}%", "href": "/profit"})
    if pending_listings:
        tasks.append({"tone": "blue", "title": f"审核 {pending_listings} 条美国站英文文案", "impact": "发布前核对规格证据、商标与关键词", "href": "/listings"})
    return tasks[:8]


def inventory_status(stock_units: int, daily_sales: float, lead_time_days: int, pipeline_units: int = 0, moq: int = 1) -> dict[str, Any]:
    if daily_sales <= 0:
        return {"days": None, "level": "unknown", "label": "缺少销量数据", "reorder_point": None, "reorder_qty": 0}
    days = round(stock_units / daily_sales, 1)
    if days <= lead_time_days:
        level, label = "critical", "紧急补货"
    elif days <= lead_time_days + 7:
        level, label = "warning", "需要补货"
    else:
        level, label = "healthy", "库存健康"
    raw_qty = max(0, math.ceil(daily_sales * (lead_time_days + 14) - stock_units - pipeline_units))
    reorder_qty = math.ceil(raw_qty / max(1, moq)) * max(1, moq)
    return {"days": days, "level": level, "label": label, "reorder_point": lead_time_days + 7, "reorder_qty": reorder_qty}


def ticket_rules(rating: int, refund_requested: bool) -> dict[str, str]:
    if rating <= 2 and refund_requested:
        return {"priority": "P0", "topic": "after_sales", "sla": "2小时联系，24小时给方案，48小时闭环"}
    if rating <= 2 or refund_requested:
        return {"priority": "P1", "topic": "customer_risk", "sla": "24小时确认原因，72小时闭环"}
    return {"priority": "P2", "topic": "feedback", "sla": "3个工作日内跟进"}


def event_ticket_rules(event_type: str, rating: int, refund_requested: bool) -> dict[str, str]:
    if event_type == "return_refund" and (refund_requested or rating <= 2):
        return {"priority": "P0", "topic": "return_refund", "sla": "2小时核验订单，24小时给出处理方案"}
    if event_type in {"order_exception", "buyer_cancel"} or rating <= 2:
        return {"priority": "P1", "topic": event_type, "sla": "24小时核验并完成首次响应"}
    return {"priority": "P2", "topic": "buyer_message", "sla": "2个工作日内人工回复"}


def ticket_due_at(event_at: str, priority: str) -> str:
    hours = {"P0": 2, "P1": 24, "P2": 48}.get(priority, 48)
    return (datetime.fromisoformat(event_at.replace("Z", "+00:00")) + timedelta(hours=hours)).isoformat(timespec="seconds")


def allocate_cents(total_cents: int, weights: list[int]) -> list[int]:
    """按权重分摊整数美分，并把舍入余数归入最后一行。"""
    denominator = sum(weights)
    allocated: list[int] = []
    used = 0
    for index, weight in enumerate(weights):
        share = total_cents - used if index == len(weights) - 1 else (total_cents * weight // denominator if denominator else 0)
        allocated.append(share)
        used += share
    return allocated


def load_demo_channel(name: str) -> list[dict[str, Any]]:
    path = ROOT / "samples" / f"channel_{name}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if name == "finance":
        rows: list[dict[str, Any]] = []
        for order in load_demo_channel("orders"):
            items = order["items"]
            refund_cents = int((Decimal(str(order.get("refund_total", 0))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            item_cents = [int((Decimal(str(item["item_price"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) for item in items]
            allocated_refunds = allocate_cents(refund_cents, item_cents)
            for index, item in enumerate(items):
                key = item["external_item_id"]
                price = float(item["item_price"])
                quantity = int(item["quantity"])
                values = {
                    "principal": price, "referral_fee": -round(price * 0.15, 2),
                    "fba_fee": -round(quantity * (3.40 + min(price / quantity * 0.04, 2.0)), 2),
                    "advertising": -round(price * 0.10, 2), "inbound_cost": -round(quantity * 0.80, 2),
                }
                if float(item.get("item_tax", 0)):
                    values["tax"] = round(float(item["item_tax"]), 2)
                if float(item.get("promotion_discount", 0)):
                    values["promotion"] = -round(float(item["promotion_discount"]), 2)
                if allocated_refunds[index]:
                    values["refund"] = -allocated_refunds[index] / 100
                for transaction_type, amount in values.items():
                    rows.append({"external_transaction_id": f"FIN-{key}-{transaction_type}", "order_id_masked": order["order_id_masked"], "sku": item["sku"], "transaction_type": transaction_type, "amount": amount, "currency": "USD", "posted_at": order["purchase_at"], "source": "Amazon Settlement 报告模拟数据"})
        return rows
    raise FileNotFoundError(f"缺少演示渠道数据: {name}")


def finish_sync(conn: sqlite3.Connection, sync_type: str, started_at: str, counts: dict[str, int], failures: list[dict[str, Any]]) -> dict[str, Any]:
    completed_at = now()
    result = {
        "sync_type": sync_type, "connection_mode": "demo_sp_api", "fetched": counts["fetched"],
        "inserted": counts["inserted"], "updated": counts["updated"], "skipped": counts["skipped"],
        "errors": counts["errors"], "completed_at": completed_at,
    }
    run_id = conn.execute(
        "INSERT INTO sync_runs(sync_type,channel,status,fetched,inserted,updated,skipped,errors,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sync_type, "amazon-us", "completed_with_errors" if counts["errors"] else "success", counts["fetched"],
         counts["inserted"], counts["updated"], counts["skipped"], counts["errors"], started_at, completed_at),
    ).lastrowid
    for failure in failures:
        conn.execute(
            "INSERT INTO sync_failures(sync_run_id,external_event_id,sku,error_code,error_message,raw_payload,created_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, failure.get("external_event_id"), failure.get("sku"), failure["error_code"], failure["error_message"], json.dumps(failure["raw_payload"], ensure_ascii=False), completed_at),
        )
    result["sync_run_id"] = run_id
    column = {"inventory": "last_inventory_sync_at", "tickets": "last_ticket_sync_at", "feedback": "last_feedback_sync_at", "orders": "last_order_sync_at", "finance": "last_finance_sync_at", "market": "last_market_sync_at"}[sync_type]
    conn.execute(f"UPDATE channel_connections SET {column}=?,last_result=? WHERE channel='amazon-us'", (completed_at, json.dumps(result, ensure_ascii=False)))
    audit(conn, f"{sync_type}_synced", sync_type, None, result)
    conn.commit()
    return result


def sync_order_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    started_at = now()
    counts = {"fetched": len(rows), "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    failures: list[dict[str, Any]] = []
    allowed_statuses = {"pending", "unshipped", "shipped", "delivered", "cancelled", "partially_refunded", "refunded"}
    with closing(connect()) as conn:
        for raw in rows:
            conn.execute("SAVEPOINT sync_row")
            try:
                external_id = str(raw["external_order_id"]).strip()
                masked = str(raw["order_id_masked"]).strip()
                status = str(raw["status"]).strip()
                buyer_alias = str(raw["buyer_alias"]).strip()
                items = raw.get("items")
                if not external_id or "***" not in masked or "*" not in buyer_alias or status not in allowed_statuses or not isinstance(items, list) or not items:
                    raise ValueError("订单编号、脱敏信息、状态或商品行无效")
                amounts = [float(raw.get(key, 0)) for key in ("item_total", "shipping_total", "tax_total", "promotion_total", "refund_total")]
                if min(amounts) < 0:
                    raise ValueError("订单金额不能为负数")
                order_values = (
                    masked, status, str(raw.get("fulfillment_channel", "AFN")), str(raw["purchase_at"]),
                    raw.get("latest_ship_at"), raw.get("latest_delivery_at"), str(raw.get("currency", "USD")),
                    *amounts, buyer_alias,
                )
                existing = conn.execute("SELECT * FROM orders WHERE external_order_id=?", (external_id,)).fetchone()
                comparable = ("order_id_masked", "status", "fulfillment_channel", "purchase_at", "latest_ship_at", "latest_delivery_at", "currency", "item_total", "shipping_total", "tax_total", "promotion_total", "refund_total", "buyer_alias")
                same_order = bool(existing and tuple(existing[key] for key in comparable) == order_values)
                timestamp = now()
                if existing and not same_order:
                    conn.execute(
                        """UPDATE orders SET order_id_masked=?,status=?,fulfillment_channel=?,purchase_at=?,latest_ship_at=?,latest_delivery_at=?,currency=?,
                        item_total=?,shipping_total=?,tax_total=?,promotion_total=?,refund_total=?,buyer_alias=?,synced_at=?,version=version+1 WHERE id=?""",
                        (*order_values, timestamp, existing["id"]),
                    )
                    order_id = existing["id"]
                elif existing:
                    order_id = existing["id"]
                else:
                    order_id = conn.execute(
                        """INSERT INTO orders(external_order_id,order_id_masked,status,fulfillment_channel,purchase_at,latest_ship_at,latest_delivery_at,currency,
                        item_total,shipping_total,tax_total,promotion_total,refund_total,buyer_alias,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (external_id, *order_values, timestamp),
                    ).lastrowid
                item_changes = 0
                for item in items:
                    sku = str(item["sku"]).strip()
                    product = conn.execute("SELECT id,asin FROM products WHERE sku=?", (sku,)).fetchone()
                    quantity = int(item["quantity"])
                    if not product or product["asin"] != str(item["asin"]) or quantity <= 0:
                        raise ValueError(f"订单商品 {sku} 无法匹配商品主数据")
                    item_values = (order_id, product["id"], sku, product["asin"], quantity, float(item["item_price"]), float(item.get("item_tax", 0)), float(item.get("promotion_discount", 0)), str(item.get("item_status", status)))
                    if min(item_values[4:8]) < 0:
                        raise ValueError("订单商品金额不能为负数")
                    before = conn.execute("SELECT * FROM order_items WHERE external_item_id=?", (str(item["external_item_id"]),)).fetchone()
                    normalized = (order_id, product["id"], sku, product["asin"], quantity, float(item["item_price"]), float(item.get("item_tax", 0)), float(item.get("promotion_discount", 0)), str(item.get("item_status", status)))
                    if not before or tuple(before[key] for key in ("order_id", "product_id", "sku", "asin", "quantity", "item_price", "item_tax", "promotion_discount", "item_status")) != normalized:
                        item_changes += 1
                    conn.execute(
                        """INSERT INTO order_items(order_id,external_item_id,product_id,sku,asin,quantity,item_price,item_tax,promotion_discount,item_status)
                        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(external_item_id) DO UPDATE SET order_id=excluded.order_id,product_id=excluded.product_id,sku=excluded.sku,
                        asin=excluded.asin,quantity=excluded.quantity,item_price=excluded.item_price,item_tax=excluded.item_tax,promotion_discount=excluded.promotion_discount,item_status=excluded.item_status""",
                        (order_id, str(item["external_item_id"]), *item_values[1:]),
                    )
                for returned in raw.get("returns", []):
                    item_row = conn.execute("SELECT id FROM order_items WHERE external_item_id=?", (str(returned["external_item_id"]),)).fetchone()
                    if not item_row:
                        raise ValueError("退货记录无法匹配订单商品")
                    return_values = (
                        order_id, item_row["id"], str(returned["sku"]), str(returned["reason_code"]), str(returned["reason_text"]),
                        int(returned["quantity"]), float(returned.get("refund_amount", 0)), str(returned["status"]), returned.get("carrier"),
                        returned.get("tracking_masked"), str(returned["requested_at"]), returned.get("received_at"), returned.get("refunded_at"), timestamp,
                    )
                    conn.execute(
                        """INSERT INTO returns(external_return_id,order_id,order_item_id,sku,reason_code,reason_text,quantity,refund_amount,status,carrier,tracking_masked,requested_at,received_at,refunded_at,synced_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(external_return_id) DO UPDATE SET order_id=excluded.order_id,order_item_id=excluded.order_item_id,sku=excluded.sku,
                        reason_code=excluded.reason_code,reason_text=excluded.reason_text,quantity=excluded.quantity,refund_amount=excluded.refund_amount,status=excluded.status,
                        carrier=excluded.carrier,tracking_masked=excluded.tracking_masked,requested_at=excluded.requested_at,received_at=excluded.received_at,refunded_at=excluded.refunded_at,synced_at=excluded.synced_at""",
                        (str(returned["external_return_id"]), *return_values),
                    )
                conn.execute("UPDATE tickets SET order_ref_id=? WHERE order_id_masked=?", (order_id, masked))
                conn.execute("UPDATE tickets SET return_ref_id=(SELECT id FROM returns WHERE returns.order_id=?) WHERE order_ref_id=? AND event_type='return_refund'", (order_id, order_id))
                if same_order and item_changes == 0:
                    counts["skipped"] += 1
                elif existing:
                    counts["updated"] += 1
                else:
                    counts["inserted"] += 1
                conn.execute("RELEASE SAVEPOINT sync_row")
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                conn.execute("ROLLBACK TO SAVEPOINT sync_row")
                conn.execute("RELEASE SAVEPOINT sync_row")
                counts["errors"] += 1
                failures.append({"external_event_id": raw.get("external_order_id"), "sku": None, "error_code": type(exc).__name__, "error_message": str(exc)[:500], "raw_payload": sanitize_channel_payload(raw)})
        return finish_sync(conn, "orders", started_at, counts, failures)


def sync_inventory_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    started_at = now()
    batch_key = f"inventory-{uuid.uuid4().hex}"
    counts = {"fetched": len(rows), "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    failures: list[dict[str, Any]] = []
    fields = ("fulfillable", "reserved", "inbound", "unfulfillable", "daily_sales", "lead_time_days")
    with closing(connect()) as conn:
        for raw in rows:
            conn.execute("SAVEPOINT sync_row")
            try:
                event_id, sku = str(raw["external_event_id"]).strip(), str(raw["sku"]).strip()
                product = conn.execute("SELECT daily_sales FROM products WHERE sku=?", (sku,)).fetchone()
                cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
                units = conn.execute("SELECT COALESCE(SUM(oi.quantity),0) FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE oi.sku=? AND oi.item_status!='cancelled' AND o.purchase_at>=?", (sku, cutoff)).fetchone()[0]
                daily_sales = round(float(units) / 30, 2) if units else float(product["daily_sales"] if product else 0)
                values = [int(raw[key]) for key in ("fulfillable", "reserved", "inbound", "unfulfillable")] + [daily_sales, int(raw["lead_time_days"])]
                if not event_id or min(values) < 0 or not product:
                    raise ValueError("无效事件编号、SKU 或库存数值")
                existing = conn.execute("SELECT * FROM inventory_snapshots WHERE external_event_id=?", (event_id,)).fetchone()
                payload = (sku, *values)
                conn.execute("INSERT INTO inventory_history(batch_key,sku,fulfillable,reserved,inbound,unfulfillable,daily_sales,lead_time_days,observed_at) VALUES(?,?,?,?,?,?,?,?,?)", (batch_key, *payload, now()))
                if existing and tuple(existing[key] for key in ("sku", *fields)) == payload:
                    counts["skipped"] += 1
                    conn.execute("RELEASE SAVEPOINT sync_row")
                    continue
                timestamp = now()
                if existing:
                    conn.execute(
                        "UPDATE inventory_snapshots SET sku=?,fulfillable=?,reserved=?,inbound=?,unfulfillable=?,daily_sales=?,lead_time_days=?,synced_at=? WHERE external_event_id=?",
                        (*payload, timestamp, event_id),
                    )
                    counts["updated"] += 1
                else:
                    conn.execute(
                        "INSERT INTO inventory_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
                        (event_id, *payload, timestamp),
                    )
                    counts["inserted"] += 1
                conn.execute("UPDATE products SET stock_units=?,daily_sales=?,lead_time_days=? WHERE sku=?", (values[0], values[4], values[5], sku))
                conn.execute("RELEASE SAVEPOINT sync_row")
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                conn.execute("ROLLBACK TO SAVEPOINT sync_row")
                conn.execute("RELEASE SAVEPOINT sync_row")
                counts["errors"] += 1
                failures.append({"external_event_id": raw.get("external_event_id"), "sku": raw.get("sku"), "error_code": type(exc).__name__, "error_message": str(exc)[:500], "raw_payload": raw})
        return finish_sync(conn, "inventory", started_at, counts, failures)


def sync_ticket_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    started_at = now()
    counts = {"fetched": len(rows), "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    failures: list[dict[str, Any]] = []
    allowed = {"order_exception", "buyer_cancel", "return_refund", "buyer_message"}
    with closing(connect()) as conn:
        for raw in rows:
            conn.execute("SAVEPOINT sync_row")
            try:
                event_id, sku, event_type = str(raw["external_event_id"]).strip(), str(raw["sku"]).strip(), str(raw["event_type"]).strip()
                rating, refund = int(raw.get("rating", 3)), parse_bool(raw.get("refund_requested", False))
                order_id, alias = str(raw["order_id_masked"]), str(raw["customer_alias"])
                if (not event_id or event_type not in allowed or not 1 <= rating <= 5 or "***" not in order_id
                        or "*" not in alias or not conn.execute("SELECT 1 FROM products WHERE sku=?", (sku,)).fetchone()):
                    raise ValueError("渠道工单字段无效或未脱敏")
                values = (sku, event_type, str(raw["source"]), order_id, alias, str(raw["event_at"]), rating, int(refund), str(raw["title"]), str(raw["message"]))
                existing = conn.execute("SELECT * FROM tickets WHERE external_event_id=?", (event_id,)).fetchone()
                if existing and tuple(existing[key] for key in ("sku", "event_type", "source", "order_id_masked", "customer_alias", "event_at", "rating", "refund_requested", "title", "message")) == values:
                    counts["skipped"] += 1
                    conn.execute("RELEASE SAVEPOINT sync_row")
                    continue
                rules, timestamp = event_ticket_rules(event_type, rating, refund), now()
                due_at = ticket_due_at(str(raw["event_at"]), rules["priority"])
                assignee = {"P0": "售后主管·林悦", "P1": "客服·周宁", "P2": "客服·陈思"}[rules["priority"]]
                order = conn.execute("SELECT id FROM orders WHERE order_id_masked=?", (order_id,)).fetchone()
                returned = conn.execute("SELECT id,refund_amount FROM returns WHERE order_id=? AND sku=? ORDER BY id DESC LIMIT 1", (order["id"], sku)).fetchone() if order else None
                if existing:
                    conn.execute(
                        "UPDATE tickets SET sku=?,event_type=?,source=?,order_id_masked=?,customer_alias=?,event_at=?,rating=?,refund_requested=?,title=?,message=?,topic=?,priority=?,sla=?,synced_at=?,due_at=?,order_ref_id=?,return_ref_id=?,refund_amount=?,is_archived=0,version=version+1 WHERE external_event_id=?",
                        (*values, rules["topic"], rules["priority"], rules["sla"], timestamp, due_at, order["id"] if order else None, returned["id"] if returned else None, returned["refund_amount"] if returned else 0, event_id),
                    )
                    conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (existing["id"], "channel_updated", "system:sync-worker", "渠道事件内容发生更新", "{}", timestamp))
                    counts["updated"] += 1
                else:
                    ticket_id = conn.execute(
                        "INSERT INTO tickets(sku,event_type,source,order_id_masked,customer_alias,event_at,rating,refund_requested,title,message,topic,priority,sla,synced_at,external_event_id,created_at,assigned_to,due_at,order_ref_id,return_ref_id,refund_amount,conversation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*values, rules["topic"], rules["priority"], rules["sla"], timestamp, event_id, timestamp, assignee, due_at, order["id"] if order else None, returned["id"] if returned else None, returned["refund_amount"] if returned else 0, json.dumps([{"direction": "inbound", "at": str(raw["event_at"]), "body": str(raw["message"])}], ensure_ascii=False)),
                    ).lastrowid
                    conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, "channel_created", "system:sync-worker", str(raw["message"]), json.dumps({"source": raw["source"]}, ensure_ascii=False), timestamp))
                    counts["inserted"] += 1
                conn.execute("RELEASE SAVEPOINT sync_row")
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                conn.execute("ROLLBACK TO SAVEPOINT sync_row")
                conn.execute("RELEASE SAVEPOINT sync_row")
                counts["errors"] += 1
                failures.append({"external_event_id": raw.get("external_event_id"), "sku": raw.get("sku"), "error_code": type(exc).__name__, "error_message": str(exc)[:500], "raw_payload": sanitize_channel_payload(raw)})
        return finish_sync(conn, "tickets", started_at, counts, failures)


def sync_feedback_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    started_at = now()
    counts = {"fetched": len(rows), "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    failures: list[dict[str, Any]] = []
    fields = ("sku", "asin", "positive_topic", "negative_topic", "mention_count", "rating_impact", "trend_label", "period")
    with closing(connect()) as conn:
        for raw in rows:
            conn.execute("SAVEPOINT sync_row")
            try:
                event_id = str(raw["external_event_id"]).strip()
                values = tuple(raw[key] for key in fields)
                product = conn.execute("SELECT asin FROM products WHERE sku=?", (values[0],)).fetchone()
                if not event_id or not product or product[0] != values[1] or int(values[4]) < 0 or not -1 <= float(values[5]) <= 1:
                    raise ValueError("反馈洞察字段无效")
                existing = conn.execute("SELECT * FROM feedback_insights WHERE external_event_id=?", (event_id,)).fetchone()
                normalized = (str(values[0]), str(values[1]), str(values[2]), str(values[3]), int(values[4]), float(values[5]), str(values[6]), str(values[7]))
                if existing and tuple(existing[key] for key in fields) == normalized:
                    counts["skipped"] += 1
                    conn.execute("RELEASE SAVEPOINT sync_row")
                    continue
                if existing:
                    conn.execute("UPDATE feedback_insights SET sku=?,asin=?,positive_topic=?,negative_topic=?,mention_count=?,rating_impact=?,trend_label=?,period=?,synced_at=? WHERE external_event_id=?", (*normalized, now(), event_id))
                    counts["updated"] += 1
                else:
                    conn.execute("INSERT INTO feedback_insights VALUES(?,?,?,?,?,?,?,?,?,?)", (event_id, *normalized, now()))
                    counts["inserted"] += 1
                conn.execute("RELEASE SAVEPOINT sync_row")
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                conn.execute("ROLLBACK TO SAVEPOINT sync_row")
                conn.execute("RELEASE SAVEPOINT sync_row")
                counts["errors"] += 1
                failures.append({"external_event_id": raw.get("external_event_id"), "sku": raw.get("sku"), "error_code": type(exc).__name__, "error_message": str(exc)[:500], "raw_payload": raw})
        return finish_sync(conn, "feedback", started_at, counts, failures)


def sync_finance_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    started_at = now()
    counts = {"fetched": len(rows), "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    failures: list[dict[str, Any]] = []
    allowed = {"principal", "tax", "promotion", "refund", "referral_fee", "fba_fee", "advertising", "inbound_cost", "other"}
    with closing(connect()) as conn:
        for raw in rows:
            conn.execute("SAVEPOINT sync_row")
            try:
                external_id = str(raw["external_transaction_id"]).strip()
                sku = str(raw["sku"]).strip()
                transaction_type = str(raw["transaction_type"]).strip()
                amount_decimal = Decimal(str(raw["amount"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                amount_cents = int(amount_decimal * 100)
                amount = amount_cents / 100
                currency = str(raw.get("currency", "USD")).strip().upper()
                product = conn.execute("SELECT 1 FROM products WHERE sku=?", (sku,)).fetchone()
                order = conn.execute("SELECT id FROM orders WHERE order_id_masked=?", (str(raw.get("order_id_masked", "")),)).fetchone()
                if not external_id or not product or transaction_type not in allowed or not math.isfinite(amount) or currency != "USD":
                    raise ValueError("结算流水字段无效")
                values = (order["id"] if order else None, sku, transaction_type, amount, amount_cents, currency, str(raw["posted_at"]), str(raw["source"]))
                existing = conn.execute("SELECT * FROM financial_transactions WHERE external_transaction_id=?", (external_id,)).fetchone()
                if existing and tuple(existing[key] for key in ("order_id", "sku", "transaction_type", "amount", "amount_cents", "currency", "posted_at", "source")) == values:
                    counts["skipped"] += 1
                elif existing:
                    conn.execute("UPDATE financial_transactions SET order_id=?,sku=?,transaction_type=?,amount=?,amount_cents=?,currency=?,posted_at=?,source=?,synced_at=? WHERE external_transaction_id=?", (*values, now(), external_id))
                    counts["updated"] += 1
                else:
                    conn.execute("INSERT INTO financial_transactions(external_transaction_id,order_id,sku,transaction_type,amount,amount_cents,currency,posted_at,source,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (external_id, *values, now()))
                    counts["inserted"] += 1
                conn.execute("RELEASE SAVEPOINT sync_row")
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                conn.execute("ROLLBACK TO SAVEPOINT sync_row")
                conn.execute("RELEASE SAVEPOINT sync_row")
                counts["errors"] += 1
                failures.append({"external_event_id": raw.get("external_transaction_id"), "sku": raw.get("sku"), "error_code": type(exc).__name__, "error_message": str(exc)[:500], "raw_payload": raw})
        return finish_sync(conn, "finance", started_at, counts, failures)


def sync_market_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    started_at = now()
    counts = {"fetched": len(rows), "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    failures: list[dict[str, Any]] = []
    fields = ("sku", "keyword", "estimated_monthly_demand", "median_price", "median_review_count", "competitor_count", "average_rating", "trend_score", "source_mode", "captured_at")
    with closing(connect()) as conn:
        for raw in rows:
            conn.execute("SAVEPOINT sync_row")
            try:
                external_id = str(raw["external_event_id"]).strip()
                normalized = (str(raw["sku"]), str(raw["keyword"]), float(raw["estimated_monthly_demand"]), float(raw["median_price"]), int(raw["median_review_count"]), int(raw["competitor_count"]), float(raw["average_rating"]), float(raw["trend_score"]), str(raw["source_mode"]), str(raw["captured_at"]))
                if not external_id or not conn.execute("SELECT 1 FROM products WHERE sku=?", (normalized[0],)).fetchone() or min(normalized[2:6]) < 0 or not 0 <= normalized[6] <= 5 or not 0 <= normalized[7] <= 100:
                    raise ValueError("市场研究字段无效")
                existing = conn.execute("SELECT * FROM market_research_snapshots WHERE external_event_id=?", (external_id,)).fetchone()
                if existing and tuple(existing[key] for key in fields) == normalized:
                    counts["skipped"] += 1
                elif existing:
                    conn.execute("UPDATE market_research_snapshots SET sku=?,keyword=?,estimated_monthly_demand=?,median_price=?,median_review_count=?,competitor_count=?,average_rating=?,trend_score=?,source_mode=?,captured_at=?,synced_at=? WHERE external_event_id=?", (*normalized, now(), external_id))
                    counts["updated"] += 1
                else:
                    conn.execute("INSERT INTO market_research_snapshots(external_event_id,sku,keyword,estimated_monthly_demand,median_price,median_review_count,competitor_count,average_rating,trend_score,source_mode,captured_at,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (external_id, *normalized, now()))
                    counts["inserted"] += 1
                conn.execute("RELEASE SAVEPOINT sync_row")
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                conn.execute("ROLLBACK TO SAVEPOINT sync_row")
                conn.execute("RELEASE SAVEPOINT sync_row")
                counts["errors"] += 1
                failures.append({"external_event_id": raw.get("external_event_id"), "sku": raw.get("sku"), "error_code": type(exc).__name__, "error_message": str(exc)[:500], "raw_payload": raw})
        return finish_sync(conn, "market", started_at, counts, failures)


def enqueue_sync_job(sync_type: str) -> int:
    if sync_type not in {"orders", "inventory", "tickets", "feedback", "finance", "market"}:
        raise ValueError("未知同步类型")
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT id FROM jobs WHERE job_type=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (f"sync_{sync_type}",)).fetchone()
        if existing:
            conn.commit()
            return existing["id"]
        job_id = conn.execute(
            "INSERT INTO jobs(job_type,payload,status,available_at,created_at,progress,current_step) VALUES(?,?,'queued',?,?,0,'等待 Worker')",
            (f"sync_{sync_type}", "{}", now(), now()),
        ).lastrowid
        audit(conn, "sync_job_queued", "job", job_id, {"sync_type": sync_type})
        conn.commit()
    return job_id


def update_worker_heartbeat(status: str, current_job_id: int | None = None) -> None:
    worker_id = os.getenv("WORKER_ID", os.getenv("HOSTNAME", "local-worker"))
    timestamp = now()
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO worker_heartbeats(worker_id,status,current_job_id,started_at,last_seen_at) VALUES(?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET status=excluded.status,current_job_id=excluded.current_job_id,last_seen_at=excluded.last_seen_at",
            (worker_id, status, current_job_id, timestamp, timestamp),
        )
        conn.commit()


def recover_stale_jobs() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=int(os.getenv("JOB_STALE_SECONDS", "120")))).isoformat(timespec="seconds")
    recovered = 0
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE status='running' AND COALESCE(heartbeat_at,locked_at)<?", (cutoff,)).fetchall()
        for job in rows:
            terminal = job["attempts"] >= job["max_attempts"]
            conn.execute(
                "UPDATE jobs SET status=?,available_at=?,locked_at=NULL,heartbeat_at=NULL,current_step=?,last_error=?,completed_at=? WHERE id=?",
                ("failed" if terminal else "queued", now(), "已进入失败队列" if terminal else "Worker 中断，等待重试", "stale_worker_lock", now() if terminal else None, job["id"]),
            )
            audit(conn, "stale_job_recovered", "job", job["id"], {"terminal": terminal})
            recovered += 1
        conn.commit()
    return recovered


def worker_health() -> dict[str, Any]:
    if SYNC_INLINE:
        return {"status": "inline", "healthy": True, "last_seen_at": now()}
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=int(os.getenv("WORKER_HEALTH_SECONDS", "30")))).isoformat(timespec="seconds")
    with closing(connect()) as conn:
        heartbeat = row_dict(conn.execute("SELECT * FROM worker_heartbeats ORDER BY last_seen_at DESC LIMIT 1").fetchone())
    return {"status": heartbeat["status"] if heartbeat else "missing", "healthy": bool(heartbeat and heartbeat["last_seen_at"] >= cutoff), "last_seen_at": heartbeat["last_seen_at"] if heartbeat else None, "worker_id": heartbeat["worker_id"] if heartbeat else None}


def process_sync_job(job_id: int) -> dict[str, Any] | None:
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] != "queued" or job["available_at"] > now():
            conn.rollback()
            return None
        timestamp = now()
        changed = conn.execute("UPDATE jobs SET status='running',attempts=attempts+1,locked_at=?,heartbeat_at=?,progress=10,current_step='读取渠道数据' WHERE id=? AND status='queued'", (timestamp, timestamp, job_id))
        conn.commit()
        if not changed.rowcount:
            return None
        job_type, attempts, max_attempts = job["job_type"], job["attempts"] + 1, job["max_attempts"]
    sync_type = job_type.removeprefix("sync_")
    worker_token = request_actor.set("system:sync-worker")
    try:
        update_worker_heartbeat("running", job_id)
        with closing(connect()) as conn:
            conn.execute("UPDATE jobs SET progress=25,current_step='校验并写入数据',heartbeat_at=? WHERE id=?", (now(), job_id))
            conn.commit()
        runners = {"orders": sync_order_rows, "inventory": sync_inventory_rows, "tickets": sync_ticket_rows, "feedback": sync_feedback_rows, "finance": sync_finance_rows, "market": sync_market_rows}
        result = runners[sync_type](load_demo_channel(sync_type))
        with closing(connect()) as conn:
            conn.execute("UPDATE jobs SET status='succeeded',completed_at=?,last_error=NULL,progress=100,current_step='同步完成',result=?,heartbeat_at=? WHERE id=?", (now(), json.dumps(result, ensure_ascii=False), now(), job_id))
            audit(conn, "sync_job_succeeded", "job", job_id, {"sync_type": sync_type, "sync_run_id": result["sync_run_id"]})
            conn.commit()
        return {**result, "job_id": job_id, "job_status": "succeeded"}
    except Exception as exc:
        terminal = attempts >= max_attempts
        delay = min(300, 2 ** attempts * 5)
        available_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
        with closing(connect()) as conn:
            conn.execute("UPDATE jobs SET status=?,available_at=?,locked_at=NULL,heartbeat_at=NULL,last_error=?,completed_at=?,progress=?,current_step=? WHERE id=?", ("failed" if terminal else "queued", available_at, f"{type(exc).__name__}: {str(exc)[:300]}", now() if terminal else None, 100 if terminal else 0, "已进入失败队列" if terminal else "等待自动重试", job_id))
            audit(conn, "sync_job_failed", "job", job_id, {"sync_type": sync_type, "terminal": terminal, "attempt": attempts})
            conn.commit()
        raise
    finally:
        update_worker_heartbeat("idle", None)
        request_actor.reset(worker_token)


def run_next_job() -> dict[str, Any] | None:
    update_worker_heartbeat("idle", None)
    recover_stale_jobs()
    with closing(connect()) as conn:
        row = conn.execute("SELECT id FROM jobs WHERE status='queued' AND available_at<=? ORDER BY id LIMIT 1", (now(),)).fetchone()
    return process_sync_job(row[0]) if row else None


def request_sync(sync_type: str) -> dict[str, Any] | JSONResponse:
    job_id = enqueue_sync_job(sync_type)
    if SYNC_INLINE:
        return process_sync_job(job_id) or {"job_id": job_id, "job_status": "queued"}
    return JSONResponse({"job_id": job_id, "job_status": "queued", "sync_type": sync_type}, status_code=202)


def parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "是"}:
        return True
    if normalized in {"0", "false", "no", "n", "否", ""}:
        return False
    raise ValueError("refund_requested 必须是 true/false")


def sanitize_channel_payload(raw: dict[str, Any]) -> dict[str, Any]:
    safe = dict(raw)
    if "customer_alias" in safe:
        safe["customer_alias"] = "[REDACTED]"
    if "order_id_masked" in safe:
        safe["order_id_masked"] = "[REDACTED]"
    if "message" in safe:
        safe["message"] = redact_customer_data({"message": safe["message"]})["message"]
    return safe


def read_csv(upload: UploadFile, content: bytes) -> list[dict[str, str]]:
    if not upload.filename or not upload.filename.lower().endswith(".csv"):
        raise HTTPException(400, "只接受 CSV 文件")
    if len(content) > 2_000_000:
        raise HTTPException(413, "CSV 文件不能超过 2MB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "CSV 必须使用 UTF-8 编码") from exc
    return list(csv.DictReader(io.StringIO(text)))


ENGLISH_PRODUCT_NAMES = {
    "KT-SCALE-5K-US": "Stainless Steel Digital Kitchen Scale, 11 lb Capacity",
    "KT-FROTH-01-US": "Rechargeable Milk Frother with 3 Speeds",
    "KT-SEAL-12-US": "Compact Vacuum Sealer with 12 Starter Bags",
    "HM-VAC-02-US": "Cordless Handheld Vacuum with 2 Filters",
    "HM-LIGHT-6-US": "Motion Sensor Under Cabinet Lights, Warm White, 6 Pack",
    "HM-BAG-10-US": "Heavy Duty Vacuum Storage Bags with Hand Pump, 10 Pack",
    "PET-ROLL-01-US": "Reusable Pet Hair Remover Roller",
    "PET-FILT-12-US": "Cat Water Fountain Replacement Filters, 12 Pack",
    "PET-BOWL-S-US": "Non-Slip Slow Feeder Dog Bowl, Small",
    "TR-CUBE-6-US": "Expandable Packing Cubes for Travel, 6 Piece Set",
    "TR-WASH-L-US": "Large Hanging Toiletry Bag with Wet and Dry Compartments",
    "TR-LUG-50-US": "Digital Luggage Scale, 110 lb Capacity",
    "FT-BAND-5-US": "Resistance Bands Set with 5 Levels and Carry Bag",
    "FT-STRAP-2-US": "Adjustable Yoga Mat Straps, 2 Pack",
    "FT-BALL-SET-US": "Deep Tissue Massage Ball Set with Storage Bag",
    "OF-STAND-AL-US": "Foldable Aluminum Laptop Stand",
    "OF-CABLE-16-US": "Silicone Cable Organizer Clips, 16 Pack",
    "OF-MAT-XL-US": "Reversible Vegan Leather Desk Mat, 35 x 17 Inches",
    "OD-LANT-2-US": "Rechargeable Camping Lanterns, 2 Pack",
    "OD-DRY-20-US": "20L Waterproof Dry Bag with Phone Pouch",
}


COPY_CONTEXTS = {
    "厨房秤": ("measuring ingredients and portioning recipes", "称量食材与控制配方份量"),
    "奶泡器": ("preparing coffee, cocoa, and other mixed drinks", "制作咖啡、可可与日常饮品"),
    "真空封口机": ("organizing meal-prep ingredients and pantry storage", "整理备餐食材与厨房储存"),
    "手持吸尘器": ("quick cleanup in compact household spaces", "处理家中小范围的日常清洁"),
    "橱柜灯": ("adding task lighting inside cabinets and counters", "为橱柜和操作台补充照明"),
    "真空收纳袋": ("compressing seasonal clothing and soft goods", "压缩换季衣物与柔软织物"),
    "宠物粘毛器": ("collecting loose pet hair from common fabrics", "清理常见织物表面的宠物浮毛"),
    "饮水机滤芯": ("keeping compatible pet fountain supplies organized", "整理适配宠物饮水机的替换耗材"),
    "慢食碗": ("serving measured meals to small dogs", "为小型犬安排日常进食"),
    "旅行收纳袋": ("separating clothing and essentials inside luggage", "在行李箱中分类衣物与随身用品"),
    "洗漱包": ("organizing toiletries for travel and shared bathrooms", "整理旅行或共用浴室中的洗漱用品"),
    "行李秤": ("checking packed luggage before a trip", "出行前检查已经打包的行李"),
    "阻力带": ("portable strength and mobility routines", "进行便携式力量与活动训练"),
    "瑜伽配件": ("carrying and securing a rolled yoga mat", "携带并固定卷起的瑜伽垫"),
    "按摩球": ("self-guided post-workout muscle care", "开展自主的运动后肌肉放松"),
    "笔记本支架": ("raising a laptop in flexible desk setups", "在灵活办公桌面上抬高笔记本电脑"),
    "理线器": ("routing charging and accessory cables on a desk", "整理桌面充电线与配件线缆"),
    "桌垫": ("defining a writing and computer work surface", "划分书写与电脑办公区域"),
    "露营灯": ("portable area lighting around a campsite", "为露营区域提供便携照明"),
    "防水袋": ("separating splash-prone gear during outdoor trips", "在户外出行中隔离容易沾水的物品"),
}


def stable_choice(options: tuple[str, ...], sku: str, salt: str) -> str:
    index = int(hashlib.sha256(f"{sku}:{salt}".encode()).hexdigest()[:8], 16) % len(options)
    return options[index]


def demo_chinese_listing(product: dict[str, Any]) -> dict[str, Any]:
    sku, category = str(product.get("sku") or "DEMO-SKU"), str(product.get("category") or "日常用品")
    source = str(product.get("source_title") or "").strip()
    name = source if re.search(r"[\u4e00-\u9fff]", source) else f"{sku} {category}商品"
    _, use_zh = COPY_CONTEXTS.get(category, (f"everyday {category} use", f"满足{category}的日常使用需求"))
    descriptions = (
        f"围绕{use_zh}，{name}将商品形态和主要配置直接呈现在名称中，方便运营人员逐项核对。使用前请确认尺寸、材质、兼容性与包装内容均和供应商资料一致。",
        f"{name}适合需要{use_zh}的用户。文案重点说明实际使用场景，不添加无法从商品资料验证的性能承诺；上架前仍需复核规格、配件与必要认证。",
        f"从{use_zh}这一具体场景出发，{name}强调清楚的信息和易理解的使用方式。请以最终供应商文件为准，检查型号、数量、材料以及适配范围。",
        f"如果目标是{use_zh}，{name}提供了一个信息明确的选择。当前内容保留了审慎的商品边界，发布人员应在批准前确认包装清单和所有规格描述。",
        f"{name}的内容围绕{use_zh}展开，避免使用夸大效果或无法验证的比较性措辞。运营审核时请特别确认商品配置、适用条件和页面展示一致。",
    )
    bullets = [
        f"使用场景：围绕{use_zh}组织商品信息，让用途更容易理解。",
        f"商品识别：标题明确呈现{name}，便于核对型号与配置。",
        stable_choice(("操作提示：使用前阅读商品说明，并按实际配置完成准备。", "上手说明：先核对包装内容，再按照商品资料进行设置。", "日常使用：保持步骤清晰，避免超出资料范围的使用承诺。"), sku, "zh-use"),
        stable_choice(("整理维护：使用后按商品说明清洁、收纳并检查配件。", "存放建议：根据材质和结构选择干燥、整洁的收纳位置。", "例行检查：定期查看主体与配件状态，发现异常时停止使用。"), sku, "zh-care"),
        stable_choice(("购买前确认：请核实尺寸、材质、兼容性与包装清单。", "下单前核对：确认数量、规格和适用条件符合实际需求。", "发布前复核：所有参数、认证及配件信息均以供应商文件为准。"), sku, "zh-check"),
    ]
    return {"title_zh": name[:100], "bullet_points_zh": bullets, "description_zh": stable_choice(descriptions, sku, "zh-description"), "search_terms_zh": [category, use_zh, name]}


def demo_listing(product: dict[str, Any]) -> dict[str, Any]:
    sku, category = str(product.get("sku") or "DEMO-SKU"), str(product.get("category") or "everyday product")
    name = ENGLISH_PRODUCT_NAMES.get(sku, f"{category.title()} for Everyday Use")
    use_en, _ = COPY_CONTEXTS.get(category, (f"everyday {category} use", f"满足{category}的日常使用需求"))
    descriptions = (
        f"Built around {use_en}, the {name} presents its format and included configuration in straightforward language. Before publishing, compare every dimension, material, compatibility note, and package detail with the final supplier documentation.",
        f"The {name} is intended for shoppers looking for {use_en}. This copy focuses on the supplied product identity instead of unsupported performance claims; verify specifications, included pieces, and applicable certifications before the detail page goes live.",
        f"For routines involving {use_en}, the {name} offers a clearly described option without exaggerated comparisons. The publishing reviewer should confirm the stated configuration, care guidance, and compatibility against approved product records.",
        f"Choose the {name} when the practical need is {use_en}. Its listing keeps the use case visible while avoiding assumptions about unverified results. Check materials, measurements, package contents, and usage limits before approval.",
        f"Designed with {use_en} in mind, the {name} keeps the product story specific and evidence-based. Final publication requires a line-by-line check of supplier specifications, included accessories, and any category compliance requirements.",
    )
    bullets = [
        f"PURPOSE-LED CHOICE: Product information is organized around {use_en}.",
        f"CLEAR PRODUCT IDENTITY: The {name} format is stated directly for easier comparison.",
        stable_choice(("STRAIGHTFORWARD SETUP: Review the supplied instructions and package contents before first use.", "READY FOR ROUTINE USE: Confirm each included piece, then follow the product-specific setup guidance.", "SIMPLE START: Match the received configuration to the detail page before putting the item into service."), sku, "en-use"),
        stable_choice(("CARE AND STORAGE: Follow material-specific cleaning guidance and store the item in an appropriate place.", "ROUTINE CARE: Keep the main item and accessories organized according to the supplied care instructions.", "AFTER USE: Inspect, clean, and store the product as directed by the final product documentation."), sku, "en-care"),
        stable_choice(("CHECK BEFORE ORDERING: Confirm dimensions, materials, compatibility, and package contents.", "VERIFY THE DETAILS: Review quantity, configuration, fit, and intended conditions before purchase.", "PURCHASE WITH CONTEXT: Compare the listed specifications and included components with your intended use."), sku, "en-check"),
    ]
    return {
        "title": name[:200], "bullet_points": bullets,
        "description": stable_choice(descriptions, sku, "en-description"),
        "search_terms": list(dict.fromkeys(word.lower() for word in re.findall(r"[A-Za-z]+", f"{name} {use_en}")[:14])),
        "compliance_warnings": [f"请核对 {category} 的尺寸、材质、兼容性、认证信息和包装清单；差异化文案不代表相关规格已经得到验证。"],
        "review_notes_cn": f"已按 SKU {sku} 与“{category}”使用场景生成差异化双语草稿；请人工核对所有商品事实和搜索词。",
        **demo_chinese_listing(product),
    }


def extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("模型未返回有效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("模型输出必须是 JSON 对象")
    return value


def validate_listing(value: dict[str, Any]) -> dict[str, Any]:
    required = {"title", "bullet_points", "description", "search_terms", "title_zh", "bullet_points_zh", "description_zh", "search_terms_zh", "compliance_warnings"}
    missing = required - value.keys()
    if missing:
        raise ValueError(f"Listing 缺少字段: {', '.join(sorted(missing))}")
    if not isinstance(value["title"], str) or not 1 <= len(value["title"]) <= 200:
        raise ValueError("标题长度必须为 1-200 个字符")
    if not isinstance(value["bullet_points"], list) or len(value["bullet_points"]) != 5 or not all(isinstance(x, str) and x.strip() for x in value["bullet_points"]):
        raise ValueError("bullet_points 必须包含 5 条非空文本")
    if any(len(point) > 500 for point in value["bullet_points"]):
        raise ValueError("每条 bullet point 不能超过 500 个字符")
    if not isinstance(value["description"], str) or not value["description"].strip() or len(value["description"]) > 2_000:
        raise ValueError("description 必须为 1-2000 个字符")
    for name in ("search_terms", "compliance_warnings"):
        if not isinstance(value[name], list) or not all(isinstance(x, str) for x in value[name]):
            raise ValueError(f"{name} 必须是字符串数组")
    prohibited = ("guaranteed cure", "#1 best", "100% safe", "保证治愈", "全网第一", "百分百安全")
    joined = " ".join([value["title"], *value["bullet_points"], value["description"]]).lower()
    hits = [term for term in prohibited if term in joined]
    if hits:
        raise ValueError(f"包含未经证实的高风险表述: {', '.join(hits)}")
    customer_facing = " ".join([value["title"], *value["bullet_points"], value["description"], *value["search_terms"]])
    if re.search(r"[\u4e00-\u9fff]", customer_facing):
        raise ValueError("Amazon US 面向买家的标题、五点、描述和关键词必须使用英文")
    normalized_terms = [term.strip().lower() for term in value["search_terms"] if term.strip()]
    if len(" ".join(normalized_terms).encode("utf-8")) > 249:
        raise ValueError("Search Terms 不能超过 249 字节")
    if len(normalized_terms) != len(set(normalized_terms)):
        raise ValueError("Search Terms 不能包含重复词组")
    value["search_terms"] = normalized_terms
    if not isinstance(value["title_zh"], str) or not value["title_zh"].strip() or len(value["title_zh"]) > 100:
        raise ValueError("中文标题必须为 1-100 个字符")
    if not isinstance(value["bullet_points_zh"], list) or len(value["bullet_points_zh"]) != 5 or not all(isinstance(x, str) and x.strip() for x in value["bullet_points_zh"]):
        raise ValueError("中文五点描述必须包含 5 条非空文本")
    if not isinstance(value["description_zh"], str) or not value["description_zh"].strip() or len(value["description_zh"]) > 2_000:
        raise ValueError("中文商品描述必须为 1-2000 个字符")
    if not isinstance(value["search_terms_zh"], list) or not all(isinstance(x, str) and x.strip() for x in value["search_terms_zh"]):
        raise ValueError("中文搜索词必须是非空字符串数组")
    chinese_copy = " ".join([value["title_zh"], *value["bullet_points_zh"], value["description_zh"], *value["search_terms_zh"]])
    if not re.search(r"[\u4e00-\u9fff]", chinese_copy):
        raise ValueError("中文文案必须包含中文内容")
    value["search_terms_zh"] = list(dict.fromkeys(term.strip() for term in value["search_terms_zh"] if term.strip()))
    return value


def ensure_listing_distinct(conn: sqlite3.Connection, product_id: int, value: dict[str, Any]) -> None:
    normalize = lambda text: re.sub(r"\s+", " ", str(text).strip().lower())
    for row in conn.execute("SELECT product_id,description,description_zh,bullet_points,bullet_points_zh FROM listings WHERE product_id<>?", (product_id,)):
        if normalize(row["description"]) == normalize(value["description"]) or normalize(row["description_zh"]) == normalize(value["description_zh"]):
            raise RuntimeError("生成内容与其他商品描述重复，请重新生成")
        if json.loads(row["bullet_points"]) == value["bullet_points"] or json.loads(row["bullet_points_zh"]) == value["bullet_points_zh"]:
            raise RuntimeError("生成内容与其他商品五点描述重复，请重新生成")


LISTING_PROMPT_VERSION = "listing-v2-evidence-bound"
TICKET_PROMPT_VERSION = "ticket-v2-order-aware"


def ai_config() -> dict[str, str]:
    key = os.getenv("AI_API_KEY", "").strip()
    return {
        "mode": "openai_compatible" if key else "demo",
        "key": key,
        "base_url": os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "model": os.getenv("AI_MODEL", "demo-listing-v1" if not key else "gpt-4.1-mini"),
        "timeout": os.getenv("AI_TIMEOUT_SECONDS", "30"),
        "input_cost_per_million": os.getenv("AI_INPUT_COST_PER_MILLION", "0"),
        "output_cost_per_million": os.getenv("AI_OUTPUT_COST_PER_MILLION", "0"),
    }


def ai_usage_metadata(config: dict[str, str], input_tokens: int, output_tokens: int, latency_ms: int) -> dict[str, Any]:
    cost = input_tokens / 1_000_000 * float(config["input_cost_per_million"]) + output_tokens / 1_000_000 * float(config["output_cost_per_million"])
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "latency_ms": latency_ms, "estimated_cost_usd": round(cost, 6)}


def call_listing_ai(product: dict[str, Any]) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    config = ai_config()
    safe_product = {key: product.get(key) for key in ("id", "sku", "asin", "category", "existing_copy_to_avoid")}
    safe_product["verified_facts"] = product.get("verified_facts", [])
    title_fact = next((item["value"] for item in safe_product["verified_facts"] if item["field_name"] == "source_title"), "")
    safe_product["source_title"] = title_fact
    if config["mode"] == "demo":
        value = validate_listing(demo_listing(safe_product))
        input_tokens = max(1, len(json.dumps(safe_product, ensure_ascii=False)) // 4)
        output_tokens = max(1, len(json.dumps(value, ensure_ascii=False)) // 4)
        return value, "demo", config["model"], ai_usage_metadata(config, input_tokens, output_tokens, 0)
    prompt = (
        f"Prompt version: {LISTING_PROMPT_VERSION}. Create conservative, accurate, product-specific Amazon US listing copy in English plus a faithful Simplified Chinese operations translation. Use only verified_facts for product claims. Do not reuse wording from existing_copy_to_avoid. Do not invent specifications, certifications, or performance claims. "
        "Return JSON only with title, bullet_points (exactly 5), description, search_terms (array), title_zh, bullet_points_zh (exactly 5), description_zh, search_terms_zh (array), and compliance_warnings (array).\n"
        + json.dumps(safe_product, ensure_ascii=False)
    )
    started = time.perf_counter()
    try:
        response = httpx.post(
            f"{config['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {config['key']}", "Content-Type": "application/json"},
            json={"model": config["model"], "temperature": 0.55, "messages": [{"role": "user", "content": prompt}]},
            timeout=float(config["timeout"]),
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        metadata = ai_usage_metadata(config, int(usage.get("prompt_tokens", len(prompt) // 4)), int(usage.get("completion_tokens", len(content) // 4)), int((time.perf_counter() - started) * 1000))
        return validate_listing(extract_json(content)), "openai_compatible", config["model"], metadata
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"AI Provider 调用失败: {type(exc).__name__}") from exc


def demo_ticket_reply(ticket: dict[str, Any]) -> tuple[str, str]:
    reply = (
        "Hello, we are sorry for the inconvenience. We are reviewing your order and the product details now. "
        "Once the order information is verified, we will confirm the available next steps under the applicable Amazon policy. "
        "Thank you for bringing this to our attention."
    )
    action = "核对订单与商品批次；确认退款或补发条件；在 SLA 内联系客户；关闭前记录处理结果。"
    return reply, action


def call_ticket_ai(ticket: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any]]:
    config = ai_config()
    if config["mode"] == "demo":
        reply, action = demo_ticket_reply(ticket)
        input_tokens = max(1, len(json.dumps(ticket, ensure_ascii=False)) // 4)
        output_tokens = max(1, (len(reply) + len(action)) // 4)
        return reply, action, "demo", "demo-support-v1", ai_usage_metadata(config, input_tokens, output_tokens, 0)
    prompt = (
        f"Prompt version: {TICKET_PROMPT_VERSION}. Generate a cautious, natural English buyer reply and a Chinese internal action plan. "
        "Do not promise a refund, compensation, or replacement before verification. "
        "Return JSON only with reply_draft and action_plan.\n"
        + json.dumps(ticket, ensure_ascii=False)
    )
    started = time.perf_counter()
    try:
        response = httpx.post(
            f"{config['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {config['key']}", "Content-Type": "application/json"},
            json={"model": config["model"], "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]},
            timeout=float(config["timeout"]),
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        value = extract_json(content)
        reply, action = value.get("reply_draft"), value.get("action_plan")
        if not isinstance(reply, str) or not reply.strip() or not isinstance(action, str) or not action.strip():
            raise ValueError("客服输出缺少 reply_draft 或 action_plan")
        usage = payload.get("usage", {})
        metadata = ai_usage_metadata(config, int(usage.get("prompt_tokens", len(prompt) // 4)), int(usage.get("completion_tokens", len(content) // 4)), int((time.perf_counter() - started) * 1000))
        return reply.strip(), action.strip(), "openai_compatible", config["model"], metadata
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"AI Provider 调用失败: {type(exc).__name__}") from exc


def redact_customer_data(ticket: dict[str, Any]) -> dict[str, Any]:
    safe = dict(ticket)
    text = str(safe.get("message", ""))
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL_REDACTED]", text)
    text = re.sub(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)", "[PHONE_REDACTED]", text)
    safe["message"] = text
    safe.pop("customer_alias", None)
    safe.pop("order_id_masked", None)
    return safe


def refresh_notifications(conn: sqlite3.Connection) -> None:
    timestamp = now()
    active: list[tuple[str, str, str, str, str]] = []
    for row in conn.execute("SELECT id,order_id_masked,due_at FROM tickets WHERE status NOT IN ('resolved','closed') AND COALESCE(is_archived,0)=0 AND due_at<?", (timestamp,)):
        active.append((f"ticket-overdue:{row['id']}", "客服", "critical", f"工单 {row['order_id_masked']} 已超过 SLA", "/tickets"))
    for row in conn.execute("SELECT id,job_type FROM jobs WHERE status='failed'"):
        active.append((f"job-failed:{row['id']}", "同步", "critical", f"渠道任务 #{row['id']} 执行失败", "/dashboard"))
    for row in conn.execute("SELECT id,sku,due_at FROM feedback_actions WHERE status!='resolved' AND due_at<?", (timestamp,)):
        active.append((f"feedback-overdue:{row['id']}", "反馈", "warning", f"{row['sku']} 反馈改进行动已逾期", "/feedback"))
    for row in conn.execute("SELECT sku,fulfillable,daily_sales,lead_time_days FROM inventory_snapshots WHERE daily_sales>0 AND fulfillable/daily_sales<=lead_time_days"):
        active.append((f"inventory-critical:{row['sku']}", "库存", "warning", f"{row['sku']} 可售库存低于采购交期", "/inventory"))
    fingerprints = [item[0] for item in active]
    if fingerprints:
        placeholders = ",".join("?" for _ in fingerprints)
        conn.execute(f"UPDATE operational_notifications SET status='resolved' WHERE status!='resolved' AND fingerprint NOT IN ({placeholders})", fingerprints)
    else:
        conn.execute("UPDATE operational_notifications SET status='resolved' WHERE status!='resolved'")
    for fingerprint, category, severity, title, href in active:
        conn.execute(
            "INSERT INTO operational_notifications(fingerprint,category,severity,title,href,status,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,'unread',?,?) ON CONFLICT(fingerprint) DO UPDATE SET category=excluded.category,severity=excluded.severity,title=excluded.title,href=excluded.href,last_seen_at=excluded.last_seen_at,status=CASE WHEN operational_notifications.status='resolved' THEN 'unread' ELSE operational_notifications.status END",
            (fingerprint, category, severity, title, href, timestamp, timestamp),
        )


def page_context(page: str, params: dict[str, str] | None = None, current_user: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    today = datetime.now(timezone.utc).date()
    date_from = params.get("date_from", (today - timedelta(days=29)).isoformat()).strip()
    date_to = params.get("date_to", today.isoformat()).strip()
    try:
        period_start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        period_end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        if period_start >= period_end:
            raise ValueError
    except ValueError:
        date_from, date_to = (today - timedelta(days=29)).isoformat(), today.isoformat()
        period_start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        period_end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    period_values = (period_start.isoformat(timespec="seconds"), period_end.isoformat(timespec="seconds"))
    q = params.get("q", "").strip()
    category_filter = params.get("category", "").strip()
    product_status = params.get("product_status", "").strip()
    listing_status = params.get("listing_status", "").strip()
    order_status = params.get("order_status", "").strip()
    ticket_status = params.get("ticket_status", "").strip()
    ticket_priority = params.get("ticket_priority", "").strip()
    try:
        page_no = max(1, int(params.get("page_no", "1")))
    except ValueError:
        page_no = 1
    page_size = 10
    with closing(connect()) as conn:
        if page == "dashboard":
            refresh_notifications(conn)
            conn.commit()
        sql_page_total: int | None = None
        offset = (page_no - 1) * page_size
        connection = row_dict(conn.execute("SELECT * FROM channel_connections WHERE channel='amazon-us'").fetchone()) or {}
        for key in ("last_inventory_sync_at", "last_ticket_sync_at", "last_feedback_sync_at", "last_order_sync_at", "last_finance_sync_at", "last_market_sync_at"):
            connection[f"{key}_display"] = display_time(connection.get(key))
        snapshot_by_sku = {row["sku"]: dict(row) for row in conn.execute("SELECT * FROM inventory_snapshots ORDER BY synced_at")}
        warehouse_by_sku = {row["sku"]: dict(row) for row in conn.execute("SELECT * FROM warehouse_inventory")}
        open_po_by_sku = {row["sku"]: row["remaining"] for row in conn.execute("SELECT poi.sku,SUM(poi.ordered_qty-poi.received_qty-poi.rejected_qty) remaining FROM purchase_order_items poi JOIN purchase_orders po ON po.id=poi.purchase_order_id WHERE po.status IN ('draft','approved','sent_demo','partially_received') GROUP BY poi.sku")}
        if page == "products":
            clauses, values = [], []
            if q:
                clauses.append("(sku LIKE ? OR asin LIKE ? OR source_title LIKE ? OR brand LIKE ? OR supplier_name LIKE ?)")
                values.extend([f"%{q}%"] * 5)
            if category_filter:
                clauses.append("category=?"); values.append(category_filter)
            if product_status in {"active", "inactive"}:
                clauses.append("status=?"); values.append(product_status)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            sql_page_total = conn.execute(f"SELECT COUNT(*) FROM products{where}", values).fetchone()[0]
            total_pages_sql = max(1, math.ceil(sql_page_total / page_size)); page_no = min(page_no, total_pages_sql); offset = (page_no - 1) * page_size
            products = [dict(row) for row in conn.execute(f"SELECT * FROM products{where} ORDER BY id DESC LIMIT ? OFFSET ?", (*values, page_size, offset))]
        else:
            products = [dict(row) for row in conn.execute("SELECT * FROM products ORDER BY id DESC")]
        cost_profiles = {row["product_id"]: dict(row) for row in conn.execute("SELECT * FROM cost_profiles")}
        financial_by_sku: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute("SELECT * FROM financial_transactions WHERE posted_at>=? AND posted_at<? ORDER BY posted_at", period_values):
            financial_by_sku.setdefault(row["sku"], []).append(dict(row))
        actual_units = {row["sku"]: row["units"] for row in conn.execute("SELECT oi.sku,SUM(oi.quantity) AS units FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE oi.item_status!='cancelled' AND o.purchase_at>=? AND o.purchase_at<? GROUP BY oi.sku", period_values)}
        market_by_sku = {row["sku"]: dict(row) for row in conn.execute("SELECT m.* FROM market_research_snapshots m JOIN (SELECT sku,MAX(captured_at) captured_at FROM market_research_snapshots GROUP BY sku) latest ON latest.sku=m.sku AND latest.captured_at=m.captured_at")}
        inventory_trends: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute("SELECT sku,fulfillable,reserved,inbound,unfulfillable,daily_sales,observed_at FROM inventory_history ORDER BY observed_at DESC,id DESC"):
            if len(inventory_trends.setdefault(row["sku"], [])) < 12:
                inventory_trends[row["sku"]].append(dict(row))
        evidence_by_product: dict[int, list[dict[str, Any]]] = {}
        for row in conn.execute("SELECT * FROM product_evidence ORDER BY id DESC"):
            evidence_by_product.setdefault(row["product_id"], []).append(dict(row))
        categories = [row[0] for row in conn.execute("SELECT DISTINCT category FROM products ORDER BY category")]
        for product in products:
            product["image_url"] = product_image_url(product["sku"], product.get("image_path"))
            try:
                product["compliance_tags_list"] = json.loads(product.get("compliance_tags") or "[]")
            except json.JSONDecodeError:
                product["compliance_tags_list"] = []
            snapshot = snapshot_by_sku.get(product["sku"], {})
            product.update({key: snapshot.get(key, product["stock_units"] if key == "fulfillable" else 0) for key in ("fulfillable", "reserved", "inbound", "unfulfillable")})
            product["daily_sales"] = snapshot.get("daily_sales", product["daily_sales"])
            product["lead_time_days"] = snapshot.get("lead_time_days", product["lead_time_days"])
            product["inventory_synced_at"] = snapshot.get("synced_at")
            warehouse = warehouse_by_sku.get(product["sku"], {})
            product["warehouse_on_hand"] = warehouse.get("on_hand", 0)
            product["warehouse_qc_hold"] = warehouse.get("qc_hold", 0)
            product["open_po_units"] = open_po_by_sku.get(product["sku"], 0) or 0
            pipeline_units = int(product["inbound"] + product["warehouse_on_hand"] + product["open_po_units"])
            product["inventory"] = inventory_status(product["fulfillable"], product["daily_sales"], product["lead_time_days"], pipeline_units, product.get("moq", 1))
            product["actual_order_units"] = actual_units.get(product["sku"], 0)
            product["economics"] = product_economics(product, cost_profiles.get(product["id"]), financial_by_sku.get(product["sku"]))
            product["cost_profile"] = cost_profiles.get(product["id"], {})
            product["market_signal"] = market_by_sku.get(product["sku"])
            product["inventory_trend"] = inventory_trends.get(product["sku"], [])
            product["evidence"] = evidence_by_product.get(product["id"], [])
            product["verified_evidence_count"] = sum(item["status"] == "verified" for item in product["evidence"])
            product["score_breakdown"] = json.loads(product["score_breakdown"]) if product["score_breakdown"] else None
        replenishment_plans = [dict(row) for row in conn.execute("SELECT rp.*,p.source_title,p.image_path FROM replenishment_plans rp JOIN products p ON p.id=rp.product_id ORDER BY rp.id DESC")]
        for plan in replenishment_plans:
            plan["image_url"] = product_image_url(plan["sku"], plan.get("image_path"))
        purchase_orders = [dict(row) for row in conn.execute("SELECT po.*,s.name AS supplier_name,s.payment_terms FROM purchase_orders po JOIN suppliers s ON s.id=po.supplier_id ORDER BY po.id DESC")]
        po_items: dict[int, list[dict[str, Any]]] = {}
        for row in conn.execute("SELECT poi.*,p.source_title,p.image_path FROM purchase_order_items poi JOIN products p ON p.id=poi.product_id ORDER BY poi.id"):
            item = dict(row)
            item["image_url"] = product_image_url(item["sku"], item.get("image_path"))
            po_items.setdefault(item["purchase_order_id"], []).append(item)
        for purchase_order in purchase_orders:
            purchase_order["items"] = po_items.get(purchase_order["id"], [])
            purchase_order["expected_at_display"] = display_time(purchase_order.get("expected_at"))
        listing_sql = "SELECT l.*,p.sku,p.asin,p.source_title,p.image_path FROM listings l JOIN products p ON p.id=l.product_id JOIN (SELECT product_id,MAX(id) AS latest_id FROM listings GROUP BY product_id) latest ON latest.latest_id=l.id"
        if page == "listings":
            clauses, values = [], []
            if q:
                clauses.append("(p.sku LIKE ? OR p.asin LIKE ? OR p.source_title LIKE ?)"); values.extend([f"%{q}%"] * 3)
            if listing_status in {"draft", "approved", "rejected", "mock_published"}:
                clauses.append("l.status=?"); values.append(listing_status)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            sql_page_total = conn.execute(f"SELECT COUNT(*) FROM ({listing_sql}{where})", values).fetchone()[0]
            total_pages_sql = max(1, math.ceil(sql_page_total / page_size)); page_no = min(page_no, total_pages_sql); offset = (page_no - 1) * page_size
            listings = [dict(row) for row in conn.execute(f"{listing_sql}{where} ORDER BY l.id DESC LIMIT ? OFFSET ?", (*values, page_size, offset))]
        else:
            listings = [dict(row) for row in conn.execute(f"{listing_sql} ORDER BY l.id DESC")]
        for item in listings:
            item["image_url"] = product_image_url(item["sku"], item.get("image_path"))
            item["bullet_points"] = json.loads(item["bullet_points"])
            item["search_terms"] = json.loads(item["search_terms"])
            item["bullet_points_zh"] = json.loads(item["bullet_points_zh"])
            item["search_terms_zh"] = json.loads(item["search_terms_zh"])
            item["compliance_warnings"] = json.loads(item["compliance_warnings"])
            item["evidence_snapshot_list"] = json.loads(item.get("evidence_snapshot") or "[]")
            item["compliance_errors_list"] = json.loads(item.get("compliance_errors") or "[]")
        if page == "orders":
            clauses, values = [], []
            if q:
                clauses.append("(o.order_id_masked LIKE ? OR o.buyer_alias LIKE ? OR EXISTS(SELECT 1 FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=o.id AND (oi.sku LIKE ? OR oi.asin LIKE ? OR p.source_title LIKE ?)))"); values.extend([f"%{q}%"] * 5)
            if order_status in {"pending", "unshipped", "shipped", "delivered", "cancelled", "partially_refunded", "refunded"}:
                clauses.append("o.status=?"); values.append(order_status)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            sql_page_total = conn.execute(f"SELECT COUNT(*) FROM orders o{where}", values).fetchone()[0]
            total_pages_sql = max(1, math.ceil(sql_page_total / page_size)); page_no = min(page_no, total_pages_sql); offset = (page_no - 1) * page_size
            orders = [dict(row) for row in conn.execute(f"SELECT o.* FROM orders o{where} ORDER BY o.purchase_at DESC,o.id DESC LIMIT ? OFFSET ?", (*values, page_size, offset))]
        else:
            orders = [dict(row) for row in conn.execute("SELECT * FROM orders ORDER BY purchase_at DESC,id DESC")]
        order_ids = [order["id"] for order in orders]
        order_items: dict[int, list[dict[str, Any]]] = {}
        child_where = f" WHERE oi.order_id IN ({','.join('?' * len(order_ids))})" if order_ids else " WHERE 0"
        for row in conn.execute(f"SELECT oi.*,p.source_title,p.image_path FROM order_items oi JOIN products p ON p.id=oi.product_id{child_where} ORDER BY oi.id", order_ids):
            item = dict(row)
            item["image_url"] = product_image_url(item["sku"], item.get("image_path"))
            order_items.setdefault(item["order_id"], []).append(item)
        returns_by_order: dict[int, list[dict[str, Any]]] = {}
        return_where = f" WHERE order_id IN ({','.join('?' * len(order_ids))})" if order_ids else " WHERE 0"
        for row in conn.execute(f"SELECT * FROM returns{return_where} ORDER BY requested_at DESC", order_ids):
            returns_by_order.setdefault(row["order_id"], []).append(dict(row))
        for order in orders:
            order["items"] = order_items.get(order["id"], [])
            order["returns"] = returns_by_order.get(order["id"], [])
            order["purchase_at_display"] = display_time(order["purchase_at"])
            order["latest_delivery_at_display"] = display_time(order.get("latest_delivery_at"))
        ticket_where = ["COALESCE(is_archived,0)=0"]; ticket_values: list[Any] = []
        if page == "tickets":
            if q:
                ticket_where.append("(sku LIKE ? OR title LIKE ? OR order_id_masked LIKE ? OR customer_alias LIKE ? OR assigned_to LIKE ?)"); ticket_values.extend([f"%{q}%"] * 5)
            if ticket_status in {"open", "drafted", "processing", "resolved", "closed"}:
                ticket_where.append("status=?"); ticket_values.append(ticket_status)
            if ticket_priority in {"P0", "P1", "P2"}:
                ticket_where.append("priority=?"); ticket_values.append(ticket_priority)
            where = " WHERE " + " AND ".join(ticket_where)
            sql_page_total = conn.execute(f"SELECT COUNT(*) FROM tickets{where}", ticket_values).fetchone()[0]
            total_pages_sql = max(1, math.ceil(sql_page_total / page_size)); page_no = min(page_no, total_pages_sql); offset = (page_no - 1) * page_size
            tickets = [dict(row) for row in conn.execute(f"SELECT * FROM tickets{where} ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,event_at DESC,id DESC LIMIT ? OFFSET ?", (*ticket_values, page_size, offset))]
        else:
            tickets = [dict(row) for row in conn.execute("SELECT * FROM tickets WHERE COALESCE(is_archived,0)=0 ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,event_at DESC,id DESC")]
        ticket_ids = [ticket["id"] for ticket in tickets]
        ticket_events: dict[int, list[dict[str, Any]]] = {}
        ticket_child_where = f" WHERE ticket_id IN ({','.join('?' * len(ticket_ids))})" if ticket_ids else " WHERE 0"
        for row in conn.execute(f"SELECT * FROM ticket_events{ticket_child_where} ORDER BY id DESC", ticket_ids):
            ticket_events.setdefault(row["ticket_id"], []).append(dict(row))
        service_actions: dict[int, list[dict[str, Any]]] = {}
        for row in conn.execute(f"SELECT * FROM service_actions{ticket_child_where} ORDER BY id DESC", ticket_ids):
            service_actions.setdefault(row["ticket_id"], []).append(dict(row))
        order_by_id = {order["id"]: order for order in orders}
        return_by_id = {returned["id"]: returned for values in returns_by_order.values() for returned in values}
        product_by_sku = {product["sku"]: product for product in products}
        product_titles = {sku: product["source_title"] for sku, product in product_by_sku.items()}
        for ticket in tickets:
            ticket["image_url"] = product_by_sku.get(ticket["sku"], {}).get("image_url")
            ticket["product_title"] = product_titles.get(ticket["sku"], "未匹配商品")
            ticket["event_at_display"] = display_time(ticket.get("event_at"))
            ticket["synced_at_display"] = display_time(ticket.get("synced_at"))
            ticket["due_at_display"] = display_time(ticket.get("due_at"))
            ticket["is_overdue"] = bool(ticket.get("due_at") and ticket["status"] not in {"resolved", "closed"} and ticket["due_at"] < now())
            ticket["order"] = order_by_id.get(ticket.get("order_ref_id"))
            ticket["return"] = return_by_id.get(ticket.get("return_ref_id"))
            for key in ("conversation", "attachments"):
                try:
                    ticket[f"{key}_list"] = json.loads(ticket.get(key) or "[]")
                except json.JSONDecodeError:
                    ticket[f"{key}_list"] = []
            ticket["events"] = ticket_events.get(ticket["id"], [])
            ticket["service_actions"] = service_actions.get(ticket["id"], [])
        message_map: dict[int, dict[str, Any]] = {}
        for row in conn.execute("SELECT tm.* FROM ticket_messages tm JOIN (SELECT ticket_id,MAX(id) latest_id FROM ticket_messages GROUP BY ticket_id) latest ON latest.latest_id=tm.id"):
            message_map[row["ticket_id"]] = dict(row)
        for ticket in tickets:
            ticket["reply_message"] = message_map.get(ticket["id"])
        attachment_map: dict[int, list[dict[str, Any]]] = {}
        for row in conn.execute("SELECT id,ticket_id,original_name,content_type,size_bytes,uploaded_by,created_at,expires_at FROM ticket_attachments WHERE deleted_at IS NULL AND scan_status='clean' ORDER BY id DESC"):
            attachment_map.setdefault(row["ticket_id"], []).append(dict(row))
        for ticket in tickets:
            ticket["private_attachments"] = attachment_map.get(ticket["id"], [])
        feedback = [dict(row) for row in conn.execute("SELECT * FROM feedback_insights ORDER BY period DESC,mention_count DESC")]
        feedback_action_map: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute("SELECT * FROM feedback_actions ORDER BY id DESC"):
            feedback_action_map.setdefault(row["feedback_event_id"], []).append(dict(row))
        for item in feedback:
            item["image_url"] = product_by_sku.get(item["sku"], {}).get("image_url")
            item["product_title"] = product_titles.get(item["sku"], "未匹配商品")
            item["actions"] = feedback_action_map.get(item["external_event_id"], [])
        if page == "audit":
            sql_page_total = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            total_pages_sql = max(1, math.ceil(sql_page_total / page_size)); page_no = min(page_no, total_pages_sql); offset = (page_no - 1) * page_size
            audits = [dict(row) for row in conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, offset))]
        else:
            audits = [dict(row) for row in conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 20")]
        jobs = [dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 20")]
        notifications = [dict(row) for row in conn.execute("SELECT * FROM operational_notifications WHERE status IN ('unread','read') ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,last_seen_at DESC LIMIT 20")]
        ai_usage = row_dict(conn.execute("SELECT COUNT(*) run_count,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(estimated_cost_usd),0) estimated_cost_usd,COALESCE(AVG(latency_ms),0) avg_latency_ms FROM ai_runs").fetchone()) or {}
        users = [dict(row) for row in conn.execute("SELECT username,role,is_active,must_change_password,mfa_enabled,created_at,last_login_at FROM users ORDER BY username")]
        sessions = [dict(row) for row in conn.execute("SELECT session_id,username,created_at,expires_at,last_seen_at,revoked_at,user_agent,ip_masked FROM user_sessions WHERE username=? ORDER BY created_at DESC LIMIT 20", ((current_user or {}).get("sub", ""),))]
        account = row_dict(conn.execute("SELECT username,role,must_change_password,mfa_secret,mfa_enabled,password_changed_at,last_login_at FROM users WHERE username=?", ((current_user or {}).get("sub", ""),)).fetchone())
        settled_products = [p for p in products if p["economics"]["data_source"] == "结算流水"]
        economics_products = settled_products or products
        revenue = sum(p["economics"]["revenue"] for p in economics_products)
        net_profit = sum(p["economics"]["net_profit"] for p in economics_products)
        business_totals = {
            "revenue": round(revenue, 2), "net_profit": round(net_profit, 2),
            "margin": round(net_profit / revenue * 100, 1) if revenue else 0,
            "ad_spend": round(sum(p["economics"]["ad_spend"] for p in economics_products), 2),
            "refund_loss": round(sum(p["economics"]["refund_loss"] for p in economics_products), 2),
            "data_source": "结算流水" if settled_products else "费率配置估算",
            "sku_coverage": len(settled_products), "sku_total": len(products),
        }
        metrics = {
            "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "pending": conn.execute("SELECT COUNT(*) FROM listings WHERE status='draft'").fetchone()[0],
            "published": conn.execute("SELECT COUNT(*) FROM publish_records").fetchone()[0],
            "urgent": conn.execute("SELECT COUNT(*) FROM tickets WHERE priority='P0' AND status='open' AND COALESCE(is_archived,0)=0").fetchone()[0],
            "inventory_risk": sum(p["inventory"]["level"] != "healthy" for p in products),
            "feedback": len(feedback),
        }
        today_tasks = build_today_tasks(products, tickets, metrics["pending"])
        filtered_products = products
        if q:
            needle = q.casefold()
            filtered_products = [p for p in filtered_products if needle in " ".join(str(p.get(k, "")) for k in ("sku", "asin", "source_title", "brand", "supplier_name")).casefold()]
        if category_filter:
            filtered_products = [p for p in filtered_products if p["category"] == category_filter]
        if product_status in {"active", "inactive"}:
            filtered_products = [p for p in filtered_products if p.get("status") == product_status]
        filtered_listings = listings
        if q:
            needle = q.casefold()
            filtered_listings = [item for item in filtered_listings if needle in f"{item['sku']} {item['asin']} {item['source_title']}".casefold()]
        if listing_status in {"draft", "approved", "rejected", "mock_published"}:
            filtered_listings = [item for item in filtered_listings if item["status"] == listing_status]
        filtered_orders = orders
        if q:
            needle = q.casefold()
            filtered_orders = [order for order in filtered_orders if needle in f"{order['order_id_masked']} {order['buyer_alias']} " .casefold() or any(needle in f"{item['sku']} {item['asin']} {item['source_title']}".casefold() for item in order["items"])]
        if order_status in {"pending", "unshipped", "shipped", "delivered", "cancelled", "partially_refunded", "refunded"}:
            filtered_orders = [order for order in filtered_orders if order["status"] == order_status]
        filtered_tickets = tickets
        if q:
            needle = q.casefold()
            filtered_tickets = [ticket for ticket in filtered_tickets if needle in f"{ticket['sku']} {ticket['title']} {ticket['order_id_masked']} {ticket['customer_alias']} {ticket['assigned_to']}".casefold()]
        if ticket_status in {"open", "drafted", "processing", "resolved", "closed"}:
            filtered_tickets = [ticket for ticket in filtered_tickets if ticket["status"] == ticket_status]
        if ticket_priority in {"P0", "P1", "P2"}:
            filtered_tickets = [ticket for ticket in filtered_tickets if ticket["priority"] == ticket_priority]
        selected = filtered_products if page == "products" else filtered_listings if page == "listings" else filtered_orders if page == "orders" else filtered_tickets if page == "tickets" else audits if page == "audit" else []
        total_items = sql_page_total if sql_page_total is not None else len(selected)
        total_pages = max(1, math.ceil(total_items / page_size))
        page_no = min(page_no, total_pages)
        start = (page_no - 1) * page_size
        if page == "products" and sql_page_total is None:
            page_products = filtered_products[start:start + page_size]
        else:
            page_products = products
        if page == "listings" and sql_page_total is None:
            listings = filtered_listings[start:start + page_size]
        if page == "orders" and sql_page_total is None:
            orders = filtered_orders[start:start + page_size]
        if page == "tickets" and sql_page_total is None:
            tickets = filtered_tickets[start:start + page_size]
        revision_rows = conn.execute("SELECT listing_id,version,action,actor,reason,created_at FROM listing_revisions ORDER BY id DESC").fetchall()
        revisions: dict[int, list[dict[str, Any]]] = {}
        for revision in revision_rows:
            revisions.setdefault(revision["listing_id"], []).append(dict(revision))
    status_labels = {"draft": "待审核", "approved": "已批准", "rejected": "已拒绝", "mock_published": "模拟发布成功"}
    provider_labels = {"demo": "演示模型", "openai_compatible": "兼容模型接口"}
    action_labels = {
        "products_imported": "导入商品", "inventory_imported": "导入库存", "product_analyzed": "完成选品分析",
        "listing_generated": "生成商品文案", "listing_generation_failed": "商品文案生成失败",
        "listing_approved": "批准商品文案", "listing_rejected": "拒绝商品文案", "listing_mock_published": "模拟发布商品文案",
        "tickets_imported": "导入客服工单", "ticket_reply_drafted": "生成客服回复", "ticket_reply_failed": "客服回复生成失败",
        "inventory_synced": "同步亚马逊库存", "tickets_synced": "同步客户事件", "feedback_synced": "同步评价洞察",
        "orders_synced": "同步亚马逊订单", "product_updated": "更新商品主数据", "product_image_updated": "更新商品主图",
        "replenishment_plan_created": "创建补货计划", "replenishment_plan_approved": "批准补货计划", "replenishment_plan_rejected": "拒绝补货计划",
        "purchase_order_created": "创建采购单", "purchase_order_approved": "批准采购单", "purchase_order_sent_demo": "模拟发送采购单", "purchase_order_received": "登记采购收货",
        "finance_synced": "同步结算流水", "market_synced": "同步市场研究", "cost_profile_updated": "更新成本费率",
        "ticket_updated": "更新客服工单", "ticket_attachment_uploaded": "上传客服私有附件",
        "ticket_message_saved": "保存客服回复", "ticket_message_approved": "批准客服回复",
        "ticket_message_sent_demo": "模拟发送客服回复", "ticket_message_send_failed": "客服回复发送失败",
        "feedback_action_created": "创建反馈改进行动", "feedback_action_updated": "更新反馈改进行动",
        "password_changed": "修改账号密码", "mfa_setup_started": "开始配置多因素认证", "mfa_enabled": "启用多因素认证",
        "session_revoked": "撤销登录会话", "user_created": "创建用户", "user_status_updated": "更新用户状态",
    }
    entity_labels = {"product": "商品", "inventory": "库存", "listing": "商品文案", "ticket": "客服工单", "ticket_message": "客服回复", "tickets": "客服工单", "feedback": "客户反馈", "feedback_action": "反馈改进行动", "orders": "订单", "replenishment": "补货计划", "purchase_order": "采购单", "user": "用户"}
    detail_key_labels = {"imported": "导入成功", "skipped": "跳过", "errors": "错误数", "score": "选品分", "provider": "模型", "channel": "发布通道", "error": "错误"}
    detail_value_labels = {"demo": "演示模型", "openai_compatible": "兼容模型接口", "Amazon US Demo": "亚马逊美国站模拟通道"}
    for item in audits:
        item["action_label"] = action_labels.get(item["action"], item["action"])
        item["entity_label"] = entity_labels.get(item["entity_type"], item["entity_type"])
        try:
            detail = json.loads(item["detail"])
            detail = {detail_key_labels.get(key, key): detail_value_labels.get(str(value), value) for key, value in detail.items()}
            item["detail_label"] = json.dumps(detail, ensure_ascii=False)
        except (TypeError, json.JSONDecodeError):
            item["detail_label"] = item["detail"]
    ticket_status_labels = {"open": "待处理", "drafted": "已有回复建议", "processing": "处理中", "resolved": "已解决", "closed": "已关闭"}
    message_status_labels = {"draft": "待审核", "approved": "已批准", "pending_send": "发送中", "sent_demo": "模拟发送成功", "failed": "发送失败"}
    order_status_labels = {"pending": "待确认", "unshipped": "待发货", "shipped": "已发货", "delivered": "已送达", "cancelled": "已取消", "partially_refunded": "部分退款", "refunded": "已退款"}
    return {"page": page, "products": page_products, "listings": listings, "orders": orders, "tickets": tickets, "feedback": feedback,
            "replenishment_plans": replenishment_plans, "purchase_orders": purchase_orders,
            "audits": audits, "jobs": jobs, "notifications": notifications, "metrics": metrics, "business_totals": business_totals, "today_tasks": today_tasks,
            "ai_usage": ai_usage,
            "users": users, "sessions": sessions, "account": account,
            "connection": connection, "ai_mode": ai_config()["mode"], "ticket_status_labels": ticket_status_labels, "message_status_labels": message_status_labels,
            "status_labels": status_labels, "provider_labels": provider_labels, "categories": categories,
            "filters": {"q": q, "category": category_filter, "product_status": product_status, "listing_status": listing_status, "order_status": order_status, "ticket_status": ticket_status, "ticket_priority": ticket_priority, "date_from": date_from, "date_to": date_to},
            "pagination": {"page": page_no, "pages": total_pages, "total": total_items}, "revisions": revisions,
            "order_status_labels": order_status_labels}


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_config()
    init_db()
    yield


app = FastAPI(
    title="跨境智营台", version="0.2.0", lifespan=lifespan,
    docs_url=None if APP_ENV == "production" else "/docs",
    redoc_url=None if APP_ENV == "production" else "/redoc",
    openapi_url=None if APP_ENV == "production" else "/openapi.json",
)
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
TEMPLATE_VALUE_LABELS = {
    "queued": "等待执行", "running": "执行中", "succeeded": "已成功", "failed": "失败",
    "sync_orders": "同步订单", "sync_inventory": "同步库存", "sync_tickets": "同步客户事件",
    "sync_feedback": "同步反馈", "sync_finance": "同步结算", "sync_market": "同步市场研究",
    "draft": "草稿", "approved": "已批准", "rejected": "已拒绝", "converted": "已转采购单",
    "sent_demo": "模拟发送成功", "partially_received": "部分收货", "received": "已收货", "cancelled": "已取消",
    "requested": "待批准", "completed": "已完成", "open": "待处理", "in_progress": "处理中", "resolved": "已完成",
    "refund": "退款", "reship": "重新发货", "replacement": "补发替换品", "cancel": "取消订单",
    "none": "未升级", "team_lead": "客服主管", "operations_manager": "运营经理", "compliance": "合规负责人",
    "reply_drafted": "生成回复草稿", "workflow_updated": "更新处理状态", "internal_note": "添加内部备注",
    "escalated": "升级处理", "reopened": "重新打开", "message_send_demo": "模拟发送回复", "message_send_failed": "回复发送失败",
    "service_action_requested": "提交售后动作", "admin": "管理员", "operator": "运营人员", "approver": "审批人员", "viewer": "只读人员",
}
templates.env.finalize = lambda value: "—" if value is None else TEMPLATE_VALUE_LABELS.get(value, value) if isinstance(value, str) else value
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
PRIVATE_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_ROOT)), name="media")


@app.middleware("http")
async def secure_requests(request: Request, call_next):
    trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    trace_token = request_trace.set(trace_id)
    public = request.url.path in {"/health", "/health/live", "/health/ready"} or request.url.path.startswith(("/static/", "/media/", "/login", "/docs", "/redoc", "/openapi.json"))
    user = read_session(request.cookies.get("ops_session"), SESSION_SECRET)
    if user and not public:
        try:
            with closing(connect()) as conn:
                account = conn.execute("SELECT role,must_change_password FROM users WHERE username=? AND is_active=1", (user["sub"],)).fetchone()
                session = conn.execute("SELECT * FROM user_sessions WHERE session_id=? AND username=? AND revoked_at IS NULL AND expires_at>?", (user.get("sid"), user["sub"], now())).fetchone()
                if session:
                    conn.execute("UPDATE user_sessions SET last_seen_at=? WHERE session_id=?", (now(), user["sid"]))
                    conn.commit()
            user = {**user, "role": account["role"]} if account and session else None
        except sqlite3.Error:
            user = None
    request.state.user = user
    actor_token = request_actor.set(user["sub"] if user else "anonymous")
    started = datetime.now(timezone.utc)
    response = None
    try:
        if not public:
            if not user:
                response = JSONResponse({"detail": "请先登录"}, status_code=401) if request.url.path.startswith("/api/") else RedirectResponse("/login", status_code=303)
            elif account and account["must_change_password"] and request.url.path not in {"/settings", "/logout"} and not request.url.path.startswith("/api/account/"):
                response = JSONResponse({"detail": "使用临时密码登录后必须先修改密码"}, status_code=403) if request.url.path.startswith("/api/") else RedirectResponse("/settings?must_change=1", status_code=303)
            elif not role_allows(user["role"], required_role(request.method, request.url.path)):
                response = JSONResponse({"detail": "权限不足"}, status_code=403)
            elif request.method in {"POST", "PUT", "PATCH", "DELETE"} and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), user["csrf"]):
                response = JSONResponse({"detail": "CSRF 校验失败"}, status_code=403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update({
            "X-Request-ID": trace_id, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; base-uri 'self'; form-action 'self'",
        })
        if APP_ENV == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
    finally:
        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        logger.info(json.dumps({"event": "http_request", "request_id": trace_id, "method": request.method, "path": request.url.path, "actor": request_actor.get(), "status": getattr(response, "status_code", 500), "duration_ms": elapsed_ms}, ensure_ascii=False))
        request_actor.reset(actor_token)
        request_trace.reset(trace_token)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="login.html", context={"error": request.query_params.get("error")})


@app.post("/login", include_in_schema=False)
def login(request: Request, username: str = Form(...), password: str = Form(...), otp: str = Form(default="")) -> RedirectResponse:
    identifier = f"{request.client.host if request.client else 'unknown'}:{username.strip().lower()}"
    with closing(connect()) as conn:
        attempt = conn.execute("SELECT * FROM login_attempts WHERE identifier=?", (identifier,)).fetchone()
        if attempt and attempt["first_at"] < (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds"):
            conn.execute("DELETE FROM login_attempts WHERE identifier=?", (identifier,))
            attempt = None
        if attempt and attempt["locked_until"] and attempt["locked_until"] > now():
            return RedirectResponse("/login?error=locked", status_code=303)
        user = conn.execute("SELECT * FROM users WHERE username=? AND is_active=1", (username.strip(),)).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            failures = (attempt["failures"] if attempt else 0) + 1
            locked_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="seconds") if failures >= 5 else None
            conn.execute("INSERT INTO login_attempts(identifier,failures,first_at,locked_until) VALUES(?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until", (identifier, failures, attempt["first_at"] if attempt else now(), locked_until))
            audit(conn, "login_failed", "user", None, {"username": username.strip(), "failures": failures})
            conn.commit()
            return RedirectResponse("/login?error=invalid", status_code=303)
        mfa_ok = not user["mfa_enabled"]
        if user["mfa_enabled"] and user["mfa_secret"]:
            mfa_ok = verify_totp(decrypt_mfa_secret(user["mfa_secret"]), otp.strip())
            if not mfa_ok and otp.strip():
                recovery_hash = hashlib.sha256(f"{user['username']}:{otp.strip().upper()}".encode()).hexdigest()
                recovery = conn.execute("SELECT id FROM mfa_recovery_codes WHERE username=? AND code_hash=? AND used_at IS NULL", (user["username"], recovery_hash)).fetchone()
                if recovery:
                    conn.execute("UPDATE mfa_recovery_codes SET used_at=? WHERE id=?", (now(), recovery["id"]))
                    mfa_ok = True
        if not mfa_ok:
            failures = (attempt["failures"] if attempt else 0) + 1
            locked_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="seconds") if failures >= 5 else None
            conn.execute("INSERT INTO login_attempts(identifier,failures,first_at,locked_until) VALUES(?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until", (identifier, failures, attempt["first_at"] if attempt else now(), locked_until))
            audit(conn, "login_mfa_failed", "user", None, {"username": user["username"], "failures": failures})
            conn.commit()
            return RedirectResponse("/login?error=locked" if locked_until else "/login?error=otp", status_code=303)
        conn.execute("DELETE FROM login_attempts WHERE identifier=?", (identifier,))
        audit(conn, "login_succeeded", "user", None, {"username": user["username"]})
        conn.execute("UPDATE users SET last_login_at=? WHERE username=?", (now(), user["username"]))
        conn.commit()
    session_id = uuid.uuid4().hex
    token, csrf = create_session(user["username"], user["role"], SESSION_SECRET, session_id=session_id)
    timestamp = now()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(timespec="seconds")
    ip = request.client.host if request.client else "unknown"
    ip_masked = re.sub(r"(?<=\.)\d+$", "*", ip) if "." in ip else ip[:4] + "***"
    with closing(connect()) as conn:
        conn.execute("INSERT INTO user_sessions(session_id,username,created_at,expires_at,last_seen_at,user_agent,ip_masked) VALUES(?,?,?,?,?,?,?)", (session_id, user["username"], timestamp, expires_at, timestamp, request.headers.get("user-agent", "unknown")[:300], ip_masked))
        conn.commit()
    response = RedirectResponse("/settings?must_change=1" if user["must_change_password"] else "/dashboard", status_code=303)
    response.set_cookie("ops_session", token, httponly=True, secure=APP_ENV == "production", samesite="strict", max_age=28_800)
    response.headers["X-CSRF-Token"] = csrf
    return response


@app.post("/logout", include_in_schema=False)
def logout(request: Request) -> RedirectResponse:
    if request.state.user:
        with closing(connect()) as conn:
            conn.execute("UPDATE user_sessions SET revoked_at=? WHERE session_id=?", (now(), request.state.user.get("sid")))
            conn.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("ops_session")
    return response


@app.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with closing(connect()) as conn:
            conn.execute("SELECT 1").fetchone()
            schema_version = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
            connection = row_dict(conn.execute("SELECT status,mode FROM channel_connections WHERE channel='amazon-us'").fetchone())
            failed_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0]
            queued_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
            audit_chain_ok = verify_audit_tail(conn)
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        database = "ok"
    except sqlite3.Error:
        database = "error"
        schema_version = 0
        connection, failed_jobs, queued_jobs, audit_chain_ok, integrity = None, 0, 0, False, "error"
    config = ai_config()
    worker = worker_health() if database == "ok" else {"status": "unknown", "healthy": False}
    channel_healthy = bool(connection and connection["status"] == "connected_demo")
    backup_files = sorted(BACKUP_ROOT.glob("app-*.db"), key=lambda path: path.stat().st_mtime, reverse=True) if BACKUP_ROOT.is_dir() else []
    backup = {"status": "ok" if backup_files else "missing", "latest_at": datetime.fromtimestamp(backup_files[0].stat().st_mtime, timezone.utc).isoformat(timespec="seconds") if backup_files else None}
    healthy = database == "ok" and integrity == "ok" and audit_chain_ok and schema_version >= 18 and worker["healthy"] and channel_healthy
    return {"status": "ok" if healthy else "degraded", "database": database, "schema_version": schema_version,
            "environment": APP_ENV, "ai_provider": config["mode"], "ai_configured": config["mode"] != "demo",
            "model": config["model"], "worker": worker, "channel": {"healthy": channel_healthy, "mode": connection["mode"] if connection else None},
            "jobs": {"queued": queued_jobs, "failed": failed_jobs}, "audit_chain": "ok" if audit_chain_ok else "error",
            "database_integrity": integrity, "backup": backup}


@app.post("/api/admin/audit/verify")
def verify_full_audit_log() -> dict[str, Any]:
    started = time.perf_counter()
    with closing(connect()) as conn:
        event_count = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        valid = verify_audit_chain(conn)
        audit(conn, "audit_chain_verified", "audit", None, {"event_count": event_count, "valid": valid})
        conn.commit()
    return {"valid": valid, "event_count": event_count, "duration_ms": int((time.perf_counter() - started) * 1000)}


@app.post("/api/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        result = conn.execute("UPDATE operational_notifications SET status='read',read_by=?,read_at=? WHERE id=? AND status='unread'", (request_actor.get(), now(), notification_id))
        if not result.rowcount:
            raise HTTPException(409, "通知不存在或已经处理")
        audit(conn, "notification_read", "notification", notification_id, {})
        conn.commit()
    return {"notification_id": notification_id, "status": "read"}


@app.get("/health/live", include_in_schema=False)
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
def readiness() -> JSONResponse:
    result = health()
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)


@app.get("/health/metrics", include_in_schema=False)
def operational_metrics() -> dict[str, int | float]:
    with closing(connect()) as conn:
        return {
            "jobs_queued": conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0],
            "jobs_failed": conn.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0],
            "tickets_open": conn.execute("SELECT COUNT(*) FROM tickets WHERE status NOT IN ('resolved','closed') AND COALESCE(is_archived,0)=0").fetchone()[0],
            "tickets_overdue": conn.execute("SELECT COUNT(*) FROM tickets WHERE status NOT IN ('resolved','closed') AND due_at<? AND COALESCE(is_archived,0)=0", (now(),)).fetchone()[0],
            "listings_waiting_approval": conn.execute("SELECT COUNT(*) FROM listings WHERE status='draft'").fetchone()[0],
            "feedback_actions_overdue": conn.execute("SELECT COUNT(*) FROM feedback_actions WHERE status!='resolved' AND due_at<?", (now(),)).fetchone()[0],
            "ai_cost_usd": round(conn.execute("SELECT COALESCE(SUM(estimated_cost_usd),0) FROM ai_runs").fetchone()[0], 6),
        }


@app.get("/api/channels/amazon-us/status")
def amazon_us_status() -> dict[str, Any]:
    with closing(connect()) as conn:
        value = row_dict(conn.execute("SELECT * FROM channel_connections WHERE channel='amazon-us'").fetchone())
    if not value:
        raise HTTPException(503, "演示渠道尚未初始化")
    value["connection_label"] = "Amazon US · 模拟 SP-API · 演示连接正常"
    value["last_result"] = json.loads(value["last_result"]) if value.get("last_result") else None
    return value


@app.post("/api/channels/amazon-us/sync/orders")
def sync_amazon_orders() -> dict[str, Any]:
    return request_sync("orders")


@app.post("/api/channels/amazon-us/sync/inventory")
def sync_amazon_inventory() -> dict[str, Any]:
    return request_sync("inventory")


@app.post("/api/channels/amazon-us/sync/tickets")
def sync_amazon_tickets() -> dict[str, Any]:
    return request_sync("tickets")


@app.post("/api/channels/amazon-us/sync/feedback")
def sync_amazon_feedback() -> dict[str, Any]:
    return request_sync("feedback")


@app.post("/api/channels/amazon-us/sync/finance")
def sync_amazon_finance() -> dict[str, Any]:
    return request_sync("finance")


@app.post("/api/market-research/sync-demo")
def sync_market_research_demo() -> dict[str, Any]:
    return request_sync("market")


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        job = row_dict(conn.execute("SELECT id,job_type,status,attempts,max_attempts,available_at,locked_at,last_error,progress,current_step,result,heartbeat_at,created_at,completed_at FROM jobs WHERE id=?", (job_id,)).fetchone())
    if not job:
        raise HTTPException(404, "任务不存在")
    job["result"] = json.loads(job["result"]) if job.get("result") else None
    return job


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        result = conn.execute("UPDATE jobs SET status='queued',attempts=0,available_at=?,locked_at=NULL,heartbeat_at=NULL,last_error=NULL,completed_at=NULL,progress=0,current_step='人工重试，等待 Worker' WHERE id=? AND status='failed'", (now(), job_id))
        if not result.rowcount:
            raise HTTPException(409, "只有失败队列中的任务可以人工重试")
        audit(conn, "job_retried", "job", job_id, {})
        conn.commit()
    if SYNC_INLINE:
        return process_sync_job(job_id) or {"job_id": job_id, "job_status": "queued"}
    return {"job_id": job_id, "job_status": "queued"}


@app.post("/api/import/products")
async def import_products(file: UploadFile = File(...)) -> dict[str, Any]:
    rows = read_csv(file, await file.read())
    if not rows or not set(PRODUCT_FIELDS).issubset(rows[0]):
        raise HTTPException(400, f"CSV 缺少字段，需要: {', '.join(PRODUCT_FIELDS)}")
    imported, skipped, errors = 0, 0, []
    seen_sku, seen_asin = set(), set()
    with closing(connect()) as conn:
        for line, raw in enumerate(rows, start=2):
            try:
                item = {key: str(raw.get(key, "")).strip() for key in PRODUCT_FIELDS}
                if any(not item[key] for key in ("sku", "asin", "source_title", "category")):
                    raise ValueError("文本必填字段不能为空")
                for key, parser in NUMBER_FIELDS.items():
                    item[key] = parser(item[key])
                if min(item[key] for key in NUMBER_FIELDS) < 0 or not 0 <= item["rating"] <= 5 or not 0 <= item["competition_score"] <= 100:
                    raise ValueError("数值超出允许范围")
                optional = {key: str(raw.get(key, "")).strip() for key in OPTIONAL_PRODUCT_FIELDS}
                optional["weight_kg"] = float(optional["weight_kg"] or 0)
                optional["moq"] = int(optional["moq"] or 1)
                optional["purchase_lead_days"] = int(optional["purchase_lead_days"] or item["lead_time_days"])
                optional["status"] = optional["status"] or "active"
                if optional["weight_kg"] < 0 or optional["moq"] < 1 or optional["purchase_lead_days"] < 0:
                    raise ValueError("重量、起订量或采购交期超出允许范围")
                if optional["status"] not in {"active", "inactive"}:
                    raise ValueError("status 只能是 active 或 inactive")
                tags = [tag.strip() for tag in re.split(r"[,，;；]", optional["compliance_tags"]) if tag.strip()]
                optional["compliance_tags"] = json.dumps(tags, ensure_ascii=False)
                if item["sku"] in seen_sku or item["asin"] in seen_asin:
                    skipped += 1
                    continue
                seen_sku.add(item["sku"]); seen_asin.add(item["asin"])
                timestamp = now()
                product_id = conn.execute(
                    f"INSERT INTO products({','.join(PRODUCT_FIELDS)},{','.join(OPTIONAL_PRODUCT_FIELDS)},created_at,updated_at) VALUES({','.join('?' for _ in range(len(PRODUCT_FIELDS) + len(OPTIONAL_PRODUCT_FIELDS) + 2))})",
                    (*[item[key] for key in PRODUCT_FIELDS], *[optional[key] for key in OPTIONAL_PRODUCT_FIELDS], timestamp, timestamp),
                ).lastrowid
                product = row_dict(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())
                conn.execute(
                    "INSERT INTO cost_profiles(product_id,referral_rate,fba_fee_per_unit,inbound_cost_per_unit,ad_rate,other_cost_per_unit,effective_from,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (product_id, 0.15, round(3.40 + min(item["price"] * 0.04, 2.0), 2), round(max(0.55, item["cost"] * 0.12), 2), round(0.08 + item["competition_score"] / 100 * 0.08, 4), 0, timestamp, request_actor.get(), timestamp),
                )
                for field_name, field_value in (("source_title", item["source_title"]), *[(field, optional[field]) for field in ("brand", "material", "dimensions_cm", "weight_kg", "color", "package_contents", "origin_country", "upc_ean") if str(optional[field]).strip() and str(optional[field]) != "0.0"]):
                    conn.execute("INSERT INTO product_evidence(product_id,field_name,value,source_name,source_reference,status,created_by,created_at) VALUES(?,?,?,?,?,'pending',?,?)", (product_id, field_name, str(field_value), "商品 CSV 导入", f"import-line-{line}", request_actor.get(), timestamp))
                conn.execute(
                    "INSERT INTO product_changes(product_id,version,action,reason,actor,snapshot,changed_at) VALUES(?,?,?,?,?,?,?)",
                    (product_id, 1, "imported", "CSV 导入", request_actor.get(), json.dumps(product_snapshot(product or {}), ensure_ascii=False), timestamp),
                )
                imported += 1
            except sqlite3.IntegrityError:
                skipped += 1
            except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
                errors.append({"line": line, "error": str(exc)})
        audit(conn, "products_imported", "product", None, {"imported": imported, "skipped": skipped, "errors": len(errors)})
        conn.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors}


@app.post("/api/import/inventory", include_in_schema=False)
async def import_inventory(file: UploadFile = File(...)) -> dict[str, Any]:
    rows = read_csv(file, await file.read())
    required = {"sku", "stock_units", "daily_sales", "lead_time_days"}
    if not rows or not required.issubset(rows[0]):
        raise HTTPException(400, f"CSV 缺少字段，需要: {', '.join(sorted(required))}")
    updated, errors = 0, []
    with closing(connect()) as conn:
        for line, raw in enumerate(rows, start=2):
            try:
                values = (int(raw["stock_units"]), float(raw["daily_sales"]), int(raw["lead_time_days"]), raw["sku"].strip())
                if min(values[:3]) < 0:
                    raise ValueError("库存字段不能为负数")
                result = conn.execute("UPDATE products SET stock_units=?,daily_sales=?,lead_time_days=? WHERE sku=?", values)
                if not result.rowcount:
                    raise ValueError("SKU 不存在")
                updated += 1
            except (ValueError, TypeError) as exc:
                errors.append({"line": line, "error": str(exc)})
        audit(conn, "inventory_imported", "inventory", None, {"updated": updated, "errors": len(errors)})
        conn.commit()
    return {"updated": updated, "errors": errors}


@app.post("/api/products/{product_id}/update")
def update_product(
    product_id: int,
    version: int = Form(...),
    source_title: str = Form(...), category: str = Form(...), brand: str = Form(default=""),
    price: float = Form(...), cost: float = Form(...), parent_sku: str = Form(default=""),
    variation_theme: str = Form(default=""), material: str = Form(default=""),
    dimensions_cm: str = Form(default=""), weight_kg: float = Form(default=0), color: str = Form(default=""),
    package_contents: str = Form(default=""), supplier_name: str = Form(default=""), supplier_sku: str = Form(default=""),
    moq: int = Form(default=1), purchase_lead_days: int = Form(default=0), origin_country: str = Form(default=""),
    upc_ean: str = Form(default=""), compliance_tags: str = Form(default=""), evidence_notes: str = Form(default=""),
    status: str = Form(default="active"), reason: str = Form(default="运营资料维护"),
) -> dict[str, Any]:
    source_title, category, reason = source_title.strip(), category.strip(), reason.strip()
    if not source_title or not category or not reason:
        raise HTTPException(422, "商品名、类目和修改原因不能为空")
    if min(price, cost, weight_kg, purchase_lead_days) < 0 or moq < 1:
        raise HTTPException(422, "价格、成本、重量、采购交期或起订量不合法")
    if status not in {"active", "inactive"}:
        raise HTTPException(422, "商品状态不合法")
    tags = [tag.strip() for tag in re.split(r"[,，;；]", compliance_tags) if tag.strip()]
    timestamp = now()
    values = (
        source_title, category, brand.strip(), price, cost, parent_sku.strip() or None, variation_theme.strip(),
        material.strip(), dimensions_cm.strip(), weight_kg, color.strip(), package_contents.strip(), supplier_name.strip(),
        supplier_sku.strip(), moq, purchase_lead_days, origin_country.strip(), upc_ean.strip(),
        json.dumps(tags, ensure_ascii=False), evidence_notes.strip(), status, timestamp, product_id, version,
    )
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = conn.execute(
            """UPDATE products SET source_title=?,category=?,brand=?,price=?,cost=?,parent_sku=?,variation_theme=?,
            material=?,dimensions_cm=?,weight_kg=?,color=?,package_contents=?,supplier_name=?,supplier_sku=?,moq=?,
            purchase_lead_days=?,origin_country=?,upc_ean=?,compliance_tags=?,evidence_notes=?,status=?,
            version=version+1,updated_at=? WHERE id=? AND version=?""",
            values,
        )
        if not result.rowcount:
            exists = conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone()
            raise HTTPException(409 if exists else 404, "商品已被其他人修改，请刷新后重试" if exists else "商品不存在")
        product = row_dict(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()) or {}
        conn.execute(
            "INSERT INTO product_changes(product_id,version,action,reason,actor,snapshot,changed_at) VALUES(?,?,?,?,?,?,?)",
            (product_id, product["version"], "updated", reason, request_actor.get(), json.dumps(product_snapshot(product), ensure_ascii=False), timestamp),
        )
        audit(conn, "product_updated", "product", product_id, {"version": product["version"], "reason": reason, "status": status})
        conn.commit()
    return {"product_id": product_id, "status": status, "version": version + 1}


@app.post("/api/products/{product_id}/image")
async def upload_product_image(product_id: int, version: int = Form(...), file: UploadFile = File(...)) -> dict[str, Any]:
    content = await file.read(5 * 1024 * 1024 + 1)
    if not content or len(content) > 5 * 1024 * 1024:
        raise HTTPException(422, "图片不能为空且不能超过 5 MB")
    signatures = (
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"RIFF", ".webp"),
    )
    extension = next((ext for signature, ext in signatures if content.startswith(signature)), None)
    if extension == ".webp" and content[8:12] != b"WEBP":
        extension = None
    if not extension:
        raise HTTPException(422, "仅支持真实的 PNG、JPEG 或 WebP 图片")
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        product = row_dict(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())
        if not product:
            raise HTTPException(404, "商品不存在")
        if product["version"] != version:
            raise HTTPException(409, "商品已被其他人修改，请刷新后重试")
        relative_path = f"products/{product_id}-{uuid.uuid4().hex[:12]}{extension}"
        target = MEDIA_ROOT / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        timestamp = now()
        result = conn.execute("UPDATE products SET image_path=?,version=version+1,updated_at=? WHERE id=? AND version=?", (relative_path, timestamp, product_id, version))
        if not result.rowcount:
            target.unlink(missing_ok=True)
            raise HTTPException(409, "商品已被其他人修改，请刷新后重试")
        updated = row_dict(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()) or {}
        conn.execute(
            "INSERT INTO product_changes(product_id,version,action,reason,actor,snapshot,changed_at) VALUES(?,?,?,?,?,?,?)",
            (product_id, updated["version"], "image_updated", "更新商品主图", request_actor.get(), json.dumps(product_snapshot(updated), ensure_ascii=False), timestamp),
        )
        audit(conn, "product_image_updated", "product", product_id, {"version": updated["version"], "content_type": extension[1:]})
        conn.commit()
    return {"product_id": product_id, "image_url": f"/media/{relative_path}", "version": version + 1}


@app.get("/api/products/{product_id}/history")
def product_history(product_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
            raise HTTPException(404, "商品不存在")
        changes = [dict(row) for row in conn.execute("SELECT version,action,reason,actor,snapshot,changed_at FROM product_changes WHERE product_id=? ORDER BY version DESC", (product_id,))]
    for change in changes:
        change["snapshot"] = json.loads(change["snapshot"])
    return {"product_id": product_id, "changes": changes}


@app.post("/api/products/{product_id}/evidence")
def add_product_evidence(product_id: int, field_name: str = Form(...), value: str = Form(...), source_name: str = Form(...), source_reference: str = Form(...)) -> dict[str, Any]:
    value, source_name, source_reference = value.strip(), source_name.strip(), source_reference.strip()
    if field_name not in EVIDENCE_FIELDS or not value or not source_name or not source_reference:
        raise HTTPException(422, "证据字段、值、来源和引用均为必填")
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
            raise HTTPException(404, "商品不存在")
        evidence_id = conn.execute("INSERT INTO product_evidence(product_id,field_name,value,source_name,source_reference,status,created_by,created_at) VALUES(?,?,?,?,?,'pending',?,?)", (product_id, field_name, value, source_name, source_reference, request_actor.get(), now())).lastrowid
        audit(conn, "product_evidence_added", "product", product_id, {"evidence_id": evidence_id, "field_name": field_name})
        conn.commit()
    return {"evidence_id": evidence_id, "product_id": product_id, "status": "pending", "version": 1}


@app.post("/api/evidence/{evidence_id}/verify")
def verify_product_evidence(evidence_id: int, version: int = Form(...)) -> dict[str, Any]:
    with closing(connect()) as conn:
        evidence = conn.execute("SELECT * FROM product_evidence WHERE id=?", (evidence_id,)).fetchone()
        if not evidence:
            raise HTTPException(404, "商品证据不存在")
        result = conn.execute("UPDATE product_evidence SET status='verified',verified_by=?,verified_at=?,version=version+1 WHERE id=? AND version=? AND status='pending'", (request_actor.get(), now(), evidence_id, version))
        if not result.rowcount:
            raise HTTPException(409, "证据状态或版本已变化")
        audit(conn, "product_evidence_verified", "product", evidence["product_id"], {"evidence_id": evidence_id, "field_name": evidence["field_name"]})
        conn.commit()
    return {"evidence_id": evidence_id, "status": "verified", "version": version + 1}


@app.post("/api/products/{product_id}/analyze")
def analyze_product(product_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        product = row_dict(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())
        if not product:
            raise HTTPException(404, "商品不存在")
        market = row_dict(conn.execute("SELECT * FROM market_research_snapshots WHERE sku=? ORDER BY captured_at DESC LIMIT 1", (product["sku"],)).fetchone())
        if market:
            product["market_signal"] = market
        score, breakdown = product_score(product)
        basis = "外部市场研究快照" if market else "店内运营数据（缺少市场快照）"
        conn.execute("UPDATE products SET selection_score=?,score_breakdown=?,score_basis=?,updated_at=? WHERE id=?", (score, json.dumps(breakdown), basis, now(), product_id))
        audit(conn, "product_analyzed", "product", product_id, {"score": score, "breakdown": breakdown, "basis": basis})
        conn.commit()
    return {"id": product_id, "score": score, "breakdown": breakdown, "basis": basis}


@app.post("/api/products/{product_id}/listing/generate")
def generate_listing(product_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        product = row_dict(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())
        if not product:
            raise HTTPException(404, "商品不存在")
        product["existing_copy_to_avoid"] = [
            {"description": row["description"][:240], "description_zh": (row["description_zh"] or "")[:160]}
            for row in conn.execute("SELECT description,description_zh FROM listings WHERE product_id<>? ORDER BY id DESC LIMIT 20", (product_id,))
        ]
        product["verified_facts"] = verified_evidence(conn, product_id)
        config = ai_config()
        run_id = conn.execute(
            "INSERT INTO ai_runs(run_type,entity_id,provider,model,status,input_snapshot,prompt_version,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("listing", product_id, config["mode"], config["model"], "running", json.dumps(product, ensure_ascii=False), LISTING_PROMPT_VERSION, now()),
        ).lastrowid
        conn.commit()
    try:
        value, provider, model, usage = call_listing_ai(product)
        with closing(connect()) as conn:
            ensure_listing_distinct(conn, product_id, value)
            compliance_errors, compliance_warnings = listing_compliance_check(conn, product_id, value, product["verified_facts"])
            value["compliance_warnings"] = list(dict.fromkeys([*value["compliance_warnings"], *compliance_warnings]))
            compliance_status = "passed" if not compliance_errors else "blocked"
            timestamp = now()
            review_notes = value.get("review_notes_cn") or "美国站英文草稿。请人工核对规格证据、关键词、商标和合规风险。"
            listing_id = conn.execute(
                "INSERT INTO listings(product_id,title,bullet_points,description,search_terms,title_zh,bullet_points_zh,description_zh,search_terms_zh,compliance_warnings,status,provider,review_notes_cn,evidence_snapshot,compliance_status,compliance_errors,prompt_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (product_id, value["title"], json.dumps(value["bullet_points"]), value["description"], json.dumps(value["search_terms"]), value["title_zh"], json.dumps(value["bullet_points_zh"], ensure_ascii=False), value["description_zh"], json.dumps(value["search_terms_zh"], ensure_ascii=False), json.dumps(value["compliance_warnings"], ensure_ascii=False), "draft", provider, review_notes, json.dumps(product["verified_facts"], ensure_ascii=False), compliance_status, json.dumps(compliance_errors, ensure_ascii=False), LISTING_PROMPT_VERSION, timestamp, timestamp),
            ).lastrowid
            conn.execute("UPDATE ai_runs SET provider=?,model=?,status='success',output_snapshot=?,latency_ms=?,input_tokens=?,output_tokens=?,estimated_cost_usd=? WHERE id=?", (provider, model, json.dumps(value, ensure_ascii=False), usage["latency_ms"], usage["input_tokens"], usage["output_tokens"], usage["estimated_cost_usd"], run_id))
            record_listing_revision(conn, listing_id, "generated")
            audit(conn, "listing_generated", "listing", listing_id, {"product_id": product_id, "provider": provider})
            conn.commit()
            return {"listing_id": listing_id, "status": "draft", "provider": provider, "compliance_status": compliance_status, "compliance_errors": compliance_errors, **value}
    except RuntimeError as exc:
        with closing(connect()) as conn:
            conn.execute("UPDATE ai_runs SET status='failed',error=? WHERE id=?", (str(exc), run_id))
            audit(conn, "listing_generation_failed", "product", product_id, {"error": str(exc)})
            conn.commit()
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/listings/{listing_id}/approve")
def approve_listing(
    listing_id: int,
    version: int = Form(...),
    title: str | None = Form(default=None),
    bullet_points: str | None = Form(default=None),
    description: str | None = Form(default=None),
    search_terms: str | None = Form(default=None),
    title_zh: str | None = Form(default=None),
    bullet_points_zh: str | None = Form(default=None),
    description_zh: str | None = Form(default=None),
    search_terms_zh: str | None = Form(default=None),
) -> dict[str, Any]:
    with closing(connect()) as conn:
        listing = row_dict(conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone())
        if not listing:
            raise HTTPException(404, "Listing 不存在")
        if listing["status"] == "mock_published":
            raise HTTPException(409, "已发布快照不能重新审批")
        value = {
            "title": title.strip() if title is not None else listing["title"],
            "bullet_points": [x.strip() for x in bullet_points.splitlines() if x.strip()] if bullet_points is not None else json.loads(listing["bullet_points"]),
            "description": description.strip() if description is not None else listing["description"],
            "search_terms": [x.strip() for x in search_terms.split(",") if x.strip()] if search_terms is not None else json.loads(listing["search_terms"]),
            "title_zh": title_zh.strip() if title_zh is not None else listing["title_zh"],
            "bullet_points_zh": [x.strip() for x in bullet_points_zh.splitlines() if x.strip()] if bullet_points_zh is not None else json.loads(listing["bullet_points_zh"]),
            "description_zh": description_zh.strip() if description_zh is not None else listing["description_zh"],
            "search_terms_zh": [x.strip() for x in re.split(r"[,，]", search_terms_zh) if x.strip()] if search_terms_zh is not None else json.loads(listing["search_terms_zh"]),
            "compliance_warnings": json.loads(listing["compliance_warnings"]),
        }
        try:
            validate_listing(value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        compliance_errors, compliance_warnings = listing_compliance_check(conn, listing["product_id"], value)
        if compliance_errors:
            raise HTTPException(422, "合规门禁未通过：" + "；".join(compliance_errors))
        value["compliance_warnings"] = list(dict.fromkeys([*value["compliance_warnings"], *compliance_warnings]))
        result = conn.execute(
            "UPDATE listings SET title=?,bullet_points=?,description=?,search_terms=?,title_zh=?,bullet_points_zh=?,description_zh=?,search_terms_zh=?,compliance_warnings=?,status='approved',approved_by=?,approved_at=?,rejection_reason=NULL,compliance_status='passed',compliance_errors='[]',version=version+1,updated_at=? WHERE id=? AND version=? AND status IN ('draft','rejected','approved')",
            (value["title"], json.dumps(value["bullet_points"]), value["description"], json.dumps(value["search_terms"]), value["title_zh"], json.dumps(value["bullet_points_zh"], ensure_ascii=False), value["description_zh"], json.dumps(value["search_terms_zh"], ensure_ascii=False), json.dumps(value["compliance_warnings"], ensure_ascii=False), request_actor.get(), now(), now(), listing_id, version),
        )
        if not result.rowcount:
            raise HTTPException(409, "文案已被其他人修改，请刷新后重试")
        record_listing_revision(conn, listing_id, "approved")
        audit(conn, "listing_approved", "listing", listing_id, {"edited": any(x is not None for x in (title, bullet_points, description, search_terms, title_zh, bullet_points_zh, description_zh, search_terms_zh))})
        conn.commit()
    return {"listing_id": listing_id, "status": "approved", "version": version + 1}


@app.post("/api/listings/{listing_id}/reject")
def reject_listing(listing_id: int, version: int = Form(...), reason: str = Form(...)) -> dict[str, Any]:
    reason = reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, "请填写至少 3 个字的拒绝原因")
    with closing(connect()) as conn:
        result = conn.execute("UPDATE listings SET status='rejected',approved_by=NULL,approved_at=NULL,rejection_reason=?,version=version+1,updated_at=? WHERE id=? AND version=? AND status IN ('draft','approved')", (reason, now(), listing_id, version))
        if not result.rowcount:
            raise HTTPException(409, "文案不存在、状态已变化或已被其他人修改")
        record_listing_revision(conn, listing_id, "rejected", reason)
        audit(conn, "listing_rejected", "listing", listing_id, {"reason": reason})
        conn.commit()
    return {"listing_id": listing_id, "status": "rejected", "version": version + 1}


@app.post("/api/listings/{listing_id}/publish")
def publish_listing(listing_id: int, version: int = Form(...)) -> dict[str, Any]:
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        listing = row_dict(conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone())
        if not listing:
            raise HTTPException(404, "Listing 不存在")
        if listing["status"] != "approved":
            raise HTTPException(409, "只有已批准 Listing 才能模拟发布")
        if listing.get("compliance_status") != "passed":
            raise HTTPException(409, "文案合规门禁未通过，禁止发布")
        if listing["version"] != version:
            raise HTTPException(409, "文案已被其他人修改，请刷新后重试")
        snapshot = {
            **listing,
            "bullet_points": json.loads(listing["bullet_points"]), "search_terms": json.loads(listing["search_terms"]),
            "bullet_points_zh": json.loads(listing["bullet_points_zh"]), "search_terms_zh": json.loads(listing["search_terms_zh"]),
            "compliance_warnings": json.loads(listing["compliance_warnings"]),
        }
        snapshot["amazon_us_payload"] = {key: snapshot[key] for key in ("title", "bullet_points", "description", "search_terms")}
        snapshot["internal_zh_copy"] = {key: snapshot[key] for key in ("title_zh", "bullet_points_zh", "description_zh", "search_terms_zh")}
        conn.execute("INSERT INTO publish_records(listing_id,channel,status,snapshot,published_at) VALUES(?,?,?,?,?)", (listing_id, "亚马逊美国站模拟通道", "mock_published", json.dumps(snapshot, ensure_ascii=False), now()))
        conn.execute("UPDATE listings SET status='mock_published',version=version+1,updated_at=? WHERE id=? AND version=? AND status='approved'", (now(), listing_id, version))
        record_listing_revision(conn, listing_id, "mock_published")
        audit(conn, "listing_mock_published", "listing", listing_id, {"channel": "亚马逊美国站模拟通道"})
        conn.commit()
    return {"listing_id": listing_id, "status": "mock_published", "channel": "亚马逊美国站模拟通道"}


def parse_versioned_ids(value: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    try:
        for item in value.split(","):
            if item.strip():
                entity_id, version = item.split(":", 1)
                result.append((int(entity_id), int(version)))
    except ValueError as exc:
        raise HTTPException(422, "批量选择参数不合法") from exc
    if not result or len(result) > 100:
        raise HTTPException(422, "请选择 1 至 100 条文案")
    return result


@app.post("/api/listings/bulk-approve")
def bulk_approve_listings(items: str = Form(...)) -> dict[str, Any]:
    selected = parse_versioned_ids(items)
    approved: list[int] = []
    errors: list[dict[str, Any]] = []
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for listing_id, version in selected:
            listing = row_dict(conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone())
            if not listing or listing["version"] != version or listing["status"] not in {"draft", "rejected", "approved"}:
                errors.append({"listing_id": listing_id, "error": "状态或版本已变化"})
                continue
            try:
                listing_value = validate_listing(listing_snapshot(listing))
            except ValueError as exc:
                errors.append({"listing_id": listing_id, "error": str(exc)})
                continue
            compliance_errors, _ = listing_compliance_check(conn, listing["product_id"], listing_value)
            if compliance_errors:
                errors.append({"listing_id": listing_id, "error": "；".join(compliance_errors)})
                continue
            conn.execute(
                "UPDATE listings SET status='approved',approved_by=?,approved_at=?,rejection_reason=NULL,compliance_status='passed',compliance_errors='[]',version=version+1,updated_at=? WHERE id=? AND version=?",
                (request_actor.get(), now(), now(), listing_id, version),
            )
            record_listing_revision(conn, listing_id, "bulk_approved")
            audit(conn, "listing_approved", "listing", listing_id, {"bulk": True})
            approved.append(listing_id)
        conn.commit()
    return {"approved": approved, "errors": errors}


@app.post("/api/listings/bulk-reject")
def bulk_reject_listings(items: str = Form(...), reason: str = Form(...)) -> dict[str, Any]:
    selected = parse_versioned_ids(items)
    reason = reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, "请填写至少 3 个字的拒绝原因")
    rejected: list[int] = []
    errors: list[dict[str, Any]] = []
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for listing_id, version in selected:
            result = conn.execute(
                "UPDATE listings SET status='rejected',approved_by=NULL,approved_at=NULL,rejection_reason=?,version=version+1,updated_at=? WHERE id=? AND version=? AND status IN ('draft','approved')",
                (reason, now(), listing_id, version),
            )
            if not result.rowcount:
                errors.append({"listing_id": listing_id, "error": "状态或版本已变化"})
                continue
            record_listing_revision(conn, listing_id, "bulk_rejected", reason)
            audit(conn, "listing_rejected", "listing", listing_id, {"bulk": True, "reason": reason})
            rejected.append(listing_id)
        conn.commit()
    return {"rejected": rejected, "errors": errors}


@app.get("/api/listings/{listing_id}/revisions")
def listing_revisions(listing_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM listings WHERE id=?", (listing_id,)).fetchone():
            raise HTTPException(404, "Listing 不存在")
        revisions = [dict(row) for row in conn.execute("SELECT version,action,actor,reason,snapshot,created_at FROM listing_revisions WHERE listing_id=? ORDER BY id DESC", (listing_id,))]
    for revision in revisions:
        revision["snapshot"] = json.loads(revision["snapshot"])
    return {"listing_id": listing_id, "revisions": revisions}


@app.post("/api/products/{product_id}/replenishment-plans")
def create_replenishment_plan(product_id: int, safety_days: int = Form(default=14), quantity: int = Form(default=0), reason: str = Form(default="库存覆盖不足")) -> dict[str, Any]:
    if not 0 <= safety_days <= 90 or quantity < 0 or len(reason.strip()) < 3:
        raise HTTPException(422, "安全库存天数、数量或原因无效")
    with closing(connect()) as conn:
        product = conn.execute("SELECT * FROM products WHERE id=? AND status='active'", (product_id,)).fetchone()
        if not product:
            raise HTTPException(404, "在售商品不存在")
        if quantity and quantity % product["moq"]:
            raise HTTPException(422, f"补货数量必须是 MOQ {product['moq']} 的整数倍")
        existing = conn.execute("SELECT id FROM replenishment_plans WHERE product_id=? AND status IN ('draft','approved')", (product_id,)).fetchone()
        if existing:
            raise HTTPException(409, f"商品已有待处理补货计划 #{existing['id']}")
        snapshot = conn.execute("SELECT * FROM inventory_snapshots WHERE sku=? ORDER BY synced_at DESC LIMIT 1", (product["sku"],)).fetchone()
        fulfillable = int(snapshot["fulfillable"] if snapshot else product["stock_units"])
        daily_sales = float(snapshot["daily_sales"] if snapshot else product["daily_sales"])
        lead_time = int(product["purchase_lead_days"] or (snapshot["lead_time_days"] if snapshot else product["lead_time_days"]))
        inbound = int(snapshot["inbound"] if snapshot else 0)
        warehouse = conn.execute("SELECT on_hand FROM warehouse_inventory WHERE sku=?", (product["sku"],)).fetchone()
        open_po = conn.execute("SELECT COALESCE(SUM(poi.ordered_qty-poi.received_qty-poi.rejected_qty),0) FROM purchase_order_items poi JOIN purchase_orders po ON po.id=poi.purchase_order_id WHERE poi.sku=? AND po.status IN ('draft','approved','sent_demo','partially_received')", (product["sku"],)).fetchone()[0]
        pipeline = inbound + int(warehouse["on_hand"] if warehouse else 0) + int(open_po)
        raw_suggested = max(0, math.ceil(daily_sales * (lead_time + safety_days) - fulfillable - pipeline))
        suggested = math.ceil(raw_suggested / product["moq"]) * product["moq"]
        selected_qty = quantity or suggested
        plan_id = conn.execute(
            "INSERT INTO replenishment_plans(product_id,sku,fulfillable_at_creation,daily_sales_at_creation,lead_time_days,safety_days,suggested_qty,approved_qty,status,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (product_id, product["sku"], fulfillable, daily_sales, lead_time, safety_days, suggested, selected_qty, "draft", reason.strip(), request_actor.get(), now()),
        ).lastrowid
        audit(conn, "replenishment_plan_created", "replenishment", plan_id, {"sku": product["sku"], "suggested_qty": suggested, "requested_qty": selected_qty, "pipeline_units": pipeline, "moq": product["moq"]})
        conn.commit()
    return {"plan_id": plan_id, "status": "draft", "suggested_qty": suggested, "requested_qty": selected_qty}


@app.post("/api/products/{product_id}/cost-profile")
def update_cost_profile(product_id: int, referral_rate: float = Form(...), fba_fee_per_unit: float = Form(...), inbound_cost_per_unit: float = Form(...), ad_rate: float = Form(...), other_cost_per_unit: float = Form(default=0), effective_from: str = Form(...)) -> dict[str, Any]:
    if not 0 <= referral_rate <= 1 or not 0 <= ad_rate <= 1 or min(fba_fee_per_unit, inbound_cost_per_unit, other_cost_per_unit) < 0 or not effective_from.strip():
        raise HTTPException(422, "成本费率或生效日期无效")
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
            raise HTTPException(404, "商品不存在")
        conn.execute("""INSERT INTO cost_profiles(product_id,referral_rate,fba_fee_per_unit,inbound_cost_per_unit,ad_rate,other_cost_per_unit,effective_from,updated_by,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET referral_rate=excluded.referral_rate,fba_fee_per_unit=excluded.fba_fee_per_unit,
            inbound_cost_per_unit=excluded.inbound_cost_per_unit,ad_rate=excluded.ad_rate,other_cost_per_unit=excluded.other_cost_per_unit,effective_from=excluded.effective_from,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (product_id, referral_rate, fba_fee_per_unit, inbound_cost_per_unit, ad_rate, other_cost_per_unit, effective_from.strip(), request_actor.get(), now()))
        audit(conn, "cost_profile_updated", "product", product_id, {"referral_rate": referral_rate, "ad_rate": ad_rate, "effective_from": effective_from.strip()})
        conn.commit()
    return {"product_id": product_id, "status": "updated", "effective_from": effective_from.strip()}


@app.post("/api/replenishment-plans/{plan_id}/approve")
def approve_replenishment_plan(plan_id: int, version: int = Form(...), approved_qty: int = Form(...)) -> dict[str, Any]:
    if approved_qty <= 0:
        raise HTTPException(422, "批准数量必须大于 0")
    with closing(connect()) as conn:
        plan = conn.execute("SELECT rp.*,p.moq FROM replenishment_plans rp JOIN products p ON p.id=rp.product_id WHERE rp.id=?", (plan_id,)).fetchone()
        if not plan:
            raise HTTPException(404, "补货计划不存在")
        if approved_qty % plan["moq"]:
            raise HTTPException(422, f"批准数量必须是 MOQ {plan['moq']} 的整数倍")
        result = conn.execute("UPDATE replenishment_plans SET status='approved',approved_qty=?,approved_by=?,approved_at=?,version=version+1 WHERE id=? AND version=? AND status='draft'", (approved_qty, request_actor.get(), now(), plan_id, version))
        if not result.rowcount:
            raise HTTPException(409, "补货计划状态或版本已变化")
        audit(conn, "replenishment_plan_approved", "replenishment", plan_id, {"approved_qty": approved_qty})
        conn.commit()
    return {"plan_id": plan_id, "status": "approved", "approved_qty": approved_qty, "version": version + 1}


@app.post("/api/replenishment-plans/{plan_id}/reject")
def reject_replenishment_plan(plan_id: int, version: int = Form(...), reason: str = Form(...)) -> dict[str, Any]:
    reason = reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, "请填写拒绝原因")
    with closing(connect()) as conn:
        result = conn.execute("UPDATE replenishment_plans SET status='rejected',reason=?,approved_by=?,approved_at=?,version=version+1 WHERE id=? AND version=? AND status='draft'", (reason, request_actor.get(), now(), plan_id, version))
        if not result.rowcount:
            raise HTTPException(409, "补货计划状态或版本已变化")
        audit(conn, "replenishment_plan_rejected", "replenishment", plan_id, {"reason": reason})
        conn.commit()
    return {"plan_id": plan_id, "status": "rejected", "version": version + 1}


@app.post("/api/replenishment-plans/{plan_id}/convert")
def convert_replenishment_plan(plan_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        plan = conn.execute("SELECT rp.*,p.cost,p.supplier_name FROM replenishment_plans rp JOIN products p ON p.id=rp.product_id WHERE rp.id=?", (plan_id,)).fetchone()
        if not plan or plan["status"] != "approved" or not plan["approved_qty"]:
            raise HTTPException(409, "仅已批准的补货计划可转采购单")
        supplier = conn.execute("SELECT * FROM suppliers WHERE name=? AND status='active'", (plan["supplier_name"],)).fetchone() if plan["supplier_name"] else None
        supplier = supplier or conn.execute("SELECT * FROM suppliers WHERE supplier_code='SUP-GENERAL-US'").fetchone()
        po_number = f"PO-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        total = round(plan["approved_qty"] * plan["cost"], 2)
        expected_at = (datetime.now(timezone.utc) + timedelta(days=max(plan["lead_time_days"], supplier["lead_time_days"]))).isoformat(timespec="seconds")
        po_id = conn.execute("INSERT INTO purchase_orders(po_number,supplier_id,status,currency,total_amount,expected_at,created_by,created_at) VALUES(?,?,'draft',?,?,?,?,?)", (po_number, supplier["id"], supplier["currency"], total, expected_at, request_actor.get(), now())).lastrowid
        conn.execute("INSERT INTO purchase_order_items(purchase_order_id,replenishment_plan_id,product_id,sku,ordered_qty,unit_cost) VALUES(?,?,?,?,?,?)", (po_id, plan_id, plan["product_id"], plan["sku"], plan["approved_qty"], plan["cost"]))
        conn.execute("UPDATE replenishment_plans SET status='converted',version=version+1 WHERE id=? AND status='approved'", (plan_id,))
        audit(conn, "purchase_order_created", "purchase_order", po_id, {"po_number": po_number, "plan_id": plan_id, "total": total})
        conn.commit()
    return {"purchase_order_id": po_id, "po_number": po_number, "status": "draft", "total_amount": total}


@app.post("/api/purchase-orders/{po_id}/approve")
def approve_purchase_order(po_id: int, version: int = Form(...)) -> dict[str, Any]:
    with closing(connect()) as conn:
        result = conn.execute("UPDATE purchase_orders SET status='approved',approved_by=?,approved_at=?,version=version+1 WHERE id=? AND version=? AND status='draft'", (request_actor.get(), now(), po_id, version))
        if not result.rowcount:
            raise HTTPException(409, "采购单状态或版本已变化")
        audit(conn, "purchase_order_approved", "purchase_order", po_id, {})
        conn.commit()
    return {"purchase_order_id": po_id, "status": "approved", "version": version + 1}


@app.post("/api/purchase-orders/{po_id}/send-demo")
def send_purchase_order_demo(po_id: int, version: int = Form(...)) -> dict[str, Any]:
    with closing(connect()) as conn:
        result = conn.execute("UPDATE purchase_orders SET status='sent_demo',version=version+1 WHERE id=? AND version=? AND status='approved'", (po_id, version))
        if not result.rowcount:
            raise HTTPException(409, "采购单必须先批准")
        audit(conn, "purchase_order_sent_demo", "purchase_order", po_id, {"external_execution": False})
        conn.commit()
    return {"purchase_order_id": po_id, "status": "sent_demo", "external_execution": False, "version": version + 1}


@app.post("/api/purchase-order-items/{item_id}/receive")
def receive_purchase_order_item(item_id: int, quantity: int = Form(...), rejected_quantity: int = Form(default=0), note: str = Form(default="到货验收")) -> dict[str, Any]:
    if quantity < 0 or rejected_quantity < 0 or quantity + rejected_quantity <= 0 or len(note.strip()) < 2:
        raise HTTPException(422, "收货数量或备注无效")
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT poi.*,po.status AS po_status FROM purchase_order_items poi JOIN purchase_orders po ON po.id=poi.purchase_order_id WHERE poi.id=?", (item_id,)).fetchone()
        if not item or item["po_status"] not in {"approved", "sent_demo", "partially_received"}:
            raise HTTPException(409, "采购单状态不允许收货")
        if item["received_qty"] + item["rejected_qty"] + quantity + rejected_quantity > item["ordered_qty"]:
            raise HTTPException(422, "累计验收数量不能超过采购数量")
        received = item["received_qty"] + quantity
        rejected = item["rejected_qty"] + rejected_quantity
        conn.execute("UPDATE purchase_order_items SET received_qty=?,rejected_qty=? WHERE id=?", (received, rejected, item_id))
        totals = conn.execute("SELECT SUM(ordered_qty),SUM(received_qty+rejected_qty) FROM purchase_order_items WHERE purchase_order_id=?", (item["purchase_order_id"],)).fetchone()
        po_status = "received" if totals[0] == totals[1] else "partially_received"
        conn.execute("UPDATE purchase_orders SET status=?,version=version+1 WHERE id=?", (po_status, item["purchase_order_id"]))
        timestamp = now()
        conn.execute("INSERT INTO warehouse_inventory(sku,on_hand,qc_hold,updated_at) VALUES(?,?,?,?) ON CONFLICT(sku) DO UPDATE SET on_hand=on_hand+excluded.on_hand,qc_hold=qc_hold+excluded.qc_hold,updated_at=excluded.updated_at", (item["sku"], quantity, rejected_quantity, timestamp))
        movement_id = conn.execute("INSERT INTO inventory_movements(sku,movement_type,quantity,reference_type,reference_id,note,actor,created_at) VALUES(?,'purchase_receipt',?,'purchase_order_item',?,?,?,?)", (item["sku"], quantity, item_id, note.strip(), request_actor.get(), timestamp)).lastrowid
        if rejected_quantity:
            conn.execute("INSERT INTO inventory_movements(sku,movement_type,quantity,reference_type,reference_id,note,actor,created_at) VALUES(?,'adjustment',?,'purchase_order_item',?,?,?,?)", (item["sku"], -rejected_quantity, item_id, f"质检拒收：{note.strip()}", request_actor.get(), timestamp))
        audit(conn, "purchase_order_received", "purchase_order", item["purchase_order_id"], {"item_id": item_id, "accepted_quantity": quantity, "rejected_quantity": rejected_quantity, "movement_id": movement_id, "status": po_status})
        conn.commit()
    return {"purchase_order_id": item["purchase_order_id"], "item_id": item_id, "received_qty": received, "rejected_qty": rejected, "status": po_status, "inventory_effect": "warehouse_on_hand"}


@app.post("/api/tickets/import", include_in_schema=False)
async def import_tickets(file: UploadFile = File(...)) -> dict[str, Any]:
    rows = read_csv(file, await file.read())
    required = {"sku", "rating", "refund_requested", "title", "message"}
    if not rows or not required.issubset(rows[0]):
        raise HTTPException(400, f"CSV 缺少字段，需要: {', '.join(sorted(required))}")
    imported, errors = 0, []
    with closing(connect()) as conn:
        for line, raw in enumerate(rows, start=2):
            try:
                rating = int(raw["rating"])
                if not 1 <= rating <= 5:
                    raise ValueError("rating 必须为 1-5")
                refund = parse_bool(raw["refund_requested"])
                if not raw["sku"].strip() or not raw["title"].strip() or not raw["message"].strip():
                    raise ValueError("sku、title、message 不能为空")
                rules = ticket_rules(rating, refund)
                conn.execute(
                    "INSERT INTO tickets(sku,rating,refund_requested,title,message,topic,priority,sla,created_at,event_type,source,is_archived) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                    (raw["sku"].strip(), rating, int(refund), raw["title"].strip(), raw["message"].strip(), rules["topic"], rules["priority"], rules["sla"], now(), "legacy_import", "开发测试 CSV"),
                )
                imported += 1
            except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
                errors.append({"line": line, "error": str(exc)})
        audit(conn, "tickets_imported", "ticket", None, {"imported": imported, "errors": len(errors)})
        conn.commit()
    return {"imported": imported, "errors": errors}


@app.post("/api/tickets/{ticket_id}/draft-reply")
def draft_ticket_reply(ticket_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        ticket = row_dict(conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())
        if not ticket:
            raise HTTPException(404, "工单不存在")
        if ticket["status"] in {"resolved", "closed"}:
            raise HTTPException(409, "已解决或已关闭工单不能重新生成回复")
        safe_ticket = redact_customer_data(ticket)
        config = ai_config()
        run_id = conn.execute(
            "INSERT INTO ai_runs(run_type,entity_id,provider,model,status,input_snapshot,output_snapshot,prompt_version,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("ticket_reply", ticket_id, config["mode"], config["model"], "running", json.dumps(safe_ticket, ensure_ascii=False), None, TICKET_PROMPT_VERSION, now()),
        ).lastrowid
        expected_version = ticket["version"]
        conn.commit()
    try:
        reply, action, provider, model, usage = call_ticket_ai(safe_ticket)
        with closing(connect()) as conn:
            result = conn.execute("UPDATE tickets SET reply_draft=?,action_plan=?,status=CASE WHEN status='open' THEN 'drafted' ELSE status END,version=version+1 WHERE id=? AND version=? AND status IN ('open','drafted','processing')", (reply, action, ticket_id, expected_version))
            if not result.rowcount:
                conn.execute("UPDATE ai_runs SET status='failed',error='concurrent_update' WHERE id=?", (run_id,))
                conn.commit()
                raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
            conn.execute("UPDATE ai_runs SET provider=?,model=?,status='success',output_snapshot=?,latency_ms=?,input_tokens=?,output_tokens=?,estimated_cost_usd=? WHERE id=?", (provider, model, json.dumps({"reply": reply, "action": action}, ensure_ascii=False), usage["latency_ms"], usage["input_tokens"], usage["output_tokens"], usage["estimated_cost_usd"], run_id))
            message_version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM ticket_messages WHERE ticket_id=?", (ticket_id,)).fetchone()[0]
            timestamp = now()
            message_id = conn.execute(
                "INSERT INTO ticket_messages(ticket_id,version,body,action_plan,status,created_by,created_at,updated_at) VALUES(?,?,?,?, 'draft',?,?,?)",
                (ticket_id, message_version, reply, action, f"ai:{provider}", timestamp, timestamp),
            ).lastrowid
            conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, "reply_drafted", request_actor.get(), reply, json.dumps({"provider": provider, "sent": False}, ensure_ascii=False), now()))
            audit(conn, "ticket_reply_drafted", "ticket", ticket_id, {"provider": provider})
            conn.commit()
            return {"ticket_id": ticket_id, "message_id": message_id, "message_version": message_version, "reply_draft": reply, "action_plan": action, "status": "draft", "provider": provider, "version": expected_version + 1}
    except RuntimeError as exc:
        with closing(connect()) as conn:
            conn.execute("UPDATE ai_runs SET status='failed',error=? WHERE id=?", (str(exc), run_id))
            audit(conn, "ticket_reply_failed", "ticket", ticket_id, {"error": str(exc)})
            conn.commit()
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/tickets/{ticket_id}/messages/{message_id}/save")
def save_ticket_message(ticket_id: int, message_id: int, body: str = Form(...), action_plan: str = Form(...), message_version: int = Form(...)) -> dict[str, Any]:
    body, action_plan = body.strip(), action_plan.strip()
    if not 10 <= len(body) <= 5000 or not 2 <= len(action_plan) <= 3000:
        raise HTTPException(422, "回复或行动方案长度无效")
    actor = request_actor.get()
    with closing(connect()) as conn:
        result = conn.execute(
            "UPDATE ticket_messages SET body=?,action_plan=?,status='draft',edited_by=?,approved_by=NULL,approved_at=NULL,version=version+1,updated_at=? WHERE id=? AND ticket_id=? AND version=? AND status IN ('draft','approved','failed')",
            (body, action_plan, actor, now(), message_id, ticket_id, message_version),
        )
        if not result.rowcount:
            raise HTTPException(409, "消息已发送或已被其他人修改，请刷新后重试")
        conn.execute("UPDATE tickets SET reply_draft=?,action_plan=? WHERE id=?", (body, action_plan, ticket_id))
        audit(conn, "ticket_message_saved", "ticket_message", message_id, {"ticket_id": ticket_id, "version": message_version + 1})
        conn.commit()
    return {"message_id": message_id, "status": "draft", "version": message_version + 1}


@app.post("/api/tickets/{ticket_id}/messages/{message_id}/approve")
def approve_ticket_message(ticket_id: int, message_id: int, message_version: int = Form(...)) -> dict[str, Any]:
    actor = request_actor.get()
    with closing(connect()) as conn:
        message = conn.execute("SELECT * FROM ticket_messages WHERE id=? AND ticket_id=?", (message_id, ticket_id)).fetchone()
        if not message:
            raise HTTPException(404, "回复消息不存在")
        if message["edited_by"] == actor:
            raise HTTPException(409, "编辑人与批准人不能是同一账号")
        result = conn.execute("UPDATE ticket_messages SET status='approved',approved_by=?,approved_at=?,version=version+1,updated_at=? WHERE id=? AND ticket_id=? AND version=? AND status='draft'", (actor, now(), now(), message_id, ticket_id, message_version))
        if not result.rowcount:
            raise HTTPException(409, "仅最新草稿可批准，请刷新后重试")
        audit(conn, "ticket_message_approved", "ticket_message", message_id, {"ticket_id": ticket_id, "version": message_version + 1})
        conn.commit()
    return {"message_id": message_id, "status": "approved", "version": message_version + 1, "approved_by": actor}


@app.post("/api/tickets/{ticket_id}/messages/{message_id}/send-demo")
def send_ticket_message_demo(ticket_id: int, message_id: int, message_version: int = Form(...), simulate_failure: bool = Form(default=False)) -> dict[str, Any]:
    with closing(connect()) as conn:
        message = conn.execute("SELECT * FROM ticket_messages WHERE id=? AND ticket_id=?", (message_id, ticket_id)).fetchone()
        if not message or message["version"] != message_version or message["status"] not in {"approved", "failed"}:
            raise HTTPException(409, "仅已批准或发送失败的最新消息可执行")
        if message["status"] == "failed" and not message["approved_by"]:
            raise HTTPException(409, "失败消息缺少有效批准记录")
        conn.execute("UPDATE ticket_messages SET status='pending_send',attempts=attempts+1,last_error=NULL,version=version+1,updated_at=? WHERE id=?", (now(), message_id))
        if simulate_failure:
            final_status, error, sent_at = "failed", "演示通道暂时不可用", None
        else:
            final_status, error, sent_at = "sent_demo", None, now()
        conn.execute("UPDATE ticket_messages SET status=?,last_error=?,sent_at=?,version=version+1,updated_at=? WHERE id=?", (final_status, error, sent_at, now(), message_id))
        if final_status == "sent_demo":
            conn.execute("UPDATE tickets SET status=CASE WHEN status IN ('open','drafted') THEN 'processing' ELSE status END WHERE id=?", (ticket_id,))
        conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, "message_send_demo" if not error else "message_send_failed", request_actor.get(), message["body"], json.dumps({"message_id": message_id, "status": final_status, "real_external_send": False}, ensure_ascii=False), now()))
        audit(conn, "ticket_message_sent_demo" if not error else "ticket_message_send_failed", "ticket_message", message_id, {"ticket_id": ticket_id, "real_external_send": False, "error": error})
        conn.commit()
    return {"message_id": message_id, "status": final_status, "version": message_version + 2, "real_external_send": False, "error": error}


@app.post("/api/tickets/{ticket_id}/update")
def update_ticket(
    ticket_id: int,
    assigned_to: str = Form(...),
    status: str = Form(...),
    resolution: str = Form(default=""),
    escalation_level: str | None = Form(default=None),
    version: int = Form(...),
) -> dict[str, Any]:
    assigned_to, resolution = assigned_to.strip(), resolution.strip()
    if not assigned_to or status not in {"open", "drafted", "processing", "resolved", "closed"}:
        raise HTTPException(422, "负责人或工单状态无效")
    if escalation_level is not None and escalation_level not in {"none", "team_lead", "operations_manager", "compliance"}:
        raise HTTPException(422, "升级级别无效")
    if status == "resolved" and not resolution:
        raise HTTPException(422, "关闭工单前必须填写处置结果")
    with closing(connect()) as conn:
        current = conn.execute("SELECT * FROM tickets WHERE id=? AND COALESCE(is_archived,0)=0", (ticket_id,)).fetchone()
        if not current:
            raise HTTPException(404, "工单不存在")
        transitions = {"open": {"open", "drafted", "processing"}, "drafted": {"drafted", "open", "processing"}, "processing": {"processing", "resolved"}, "resolved": {"resolved", "closed"}, "closed": {"closed"}}
        if status not in transitions.get(current["status"], set()):
            raise HTTPException(409, f"不允许从 {current['status']} 直接变更为 {status}")
        escalation_level = escalation_level or current["escalation_level"]
        resolved_at = now() if status == "resolved" and current["status"] != "resolved" else current["resolved_at"]
        result = conn.execute("UPDATE tickets SET assigned_to=?,status=?,resolution=?,resolved_at=?,escalation_level=?,version=version+1 WHERE id=? AND version=?", (assigned_to, status, resolution or current["resolution"], resolved_at, escalation_level, ticket_id, version))
        if not result.rowcount:
            raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
        conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, "workflow_updated", request_actor.get(), resolution or f"状态变更为 {status}", json.dumps({"from": current["status"], "to": status, "assigned_to": assigned_to, "escalation_level": escalation_level}, ensure_ascii=False), now()))
        audit(conn, "ticket_updated", "ticket", ticket_id, {"from": current["status"], "to": status, "assigned_to": assigned_to, "escalation_level": escalation_level})
        conn.commit()
    return {"ticket_id": ticket_id, "assigned_to": assigned_to, "status": status, "resolution": resolution or current["resolution"], "resolved_at": resolved_at, "escalation_level": escalation_level, "version": version + 1}


@app.post("/api/tickets/{ticket_id}/notes")
def add_ticket_note(ticket_id: int, body: str = Form(...), version: int = Form(...)) -> dict[str, Any]:
    body = body.strip()
    if not 2 <= len(body) <= 2000:
        raise HTTPException(422, "内部备注需为 2 至 2000 个字符")
    with closing(connect()) as conn:
        result = conn.execute("UPDATE tickets SET version=version+1 WHERE id=? AND version=? AND COALESCE(is_archived,0)=0", (ticket_id, version))
        if not result.rowcount:
            raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
        conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, "internal_note", request_actor.get(), body, "{}", now()))
        audit(conn, "ticket_note_added", "ticket", ticket_id, {})
        conn.commit()
    return {"ticket_id": ticket_id, "version": version + 1, "event_type": "internal_note"}


@app.post("/api/tickets/{ticket_id}/escalate")
def escalate_ticket(ticket_id: int, escalation_level: str = Form(...), reason: str = Form(...), version: int = Form(...)) -> dict[str, Any]:
    reason = reason.strip()
    if escalation_level not in {"none", "team_lead", "operations_manager", "compliance"} or len(reason) < 3:
        raise HTTPException(422, "升级级别或原因无效")
    with closing(connect()) as conn:
        result = conn.execute("UPDATE tickets SET escalation_level=?,version=version+1 WHERE id=? AND version=? AND COALESCE(is_archived,0)=0", (escalation_level, ticket_id, version))
        if not result.rowcount:
            raise HTTPException(409, "工单已被其他人修改，请刷新后重试")
        conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, "escalated", request_actor.get(), reason, json.dumps({"level": escalation_level}, ensure_ascii=False), now()))
        audit(conn, "ticket_escalated", "ticket", ticket_id, {"level": escalation_level, "reason": reason})
        conn.commit()
    return {"ticket_id": ticket_id, "escalation_level": escalation_level, "version": version + 1}


@app.post("/api/tickets/{ticket_id}/reopen")
def reopen_ticket(ticket_id: int, reason: str = Form(...), version: int = Form(...)) -> dict[str, Any]:
    reason = reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, "请填写重新打开原因")
    with closing(connect()) as conn:
        result = conn.execute("UPDATE tickets SET status='processing',resolved_at=NULL,reopen_count=reopen_count+1,version=version+1 WHERE id=? AND version=? AND status IN ('resolved','closed')", (ticket_id, version))
        if not result.rowcount:
            raise HTTPException(409, "只有已解决或已关闭工单可以重新打开，且版本必须最新")
        conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, "reopened", request_actor.get(), reason, "{}", now()))
        audit(conn, "ticket_reopened", "ticket", ticket_id, {"reason": reason})
        conn.commit()
    return {"ticket_id": ticket_id, "status": "processing", "version": version + 1}


@app.post("/api/tickets/{ticket_id}/service-actions")
def create_service_action(ticket_id: int, action_type: str = Form(...), amount: float = Form(default=0), reason: str = Form(...)) -> dict[str, Any]:
    reason = reason.strip()
    if action_type not in {"refund", "reship", "replacement", "cancel"} or amount < 0 or len(reason) < 3:
        raise HTTPException(422, "售后动作、金额或原因无效")
    with closing(connect()) as conn:
        ticket = conn.execute("SELECT order_ref_id FROM tickets WHERE id=? AND COALESCE(is_archived,0)=0", (ticket_id,)).fetchone()
        if not ticket:
            raise HTTPException(404, "工单不存在")
        if action_type in {"refund", "reship", "replacement", "cancel"} and not ticket["order_ref_id"]:
            raise HTTPException(409, "售后动作必须关联已同步订单")
        action_id = conn.execute("INSERT INTO service_actions(ticket_id,order_id,action_type,amount,reason,status,requested_by,requested_at) VALUES(?,?,?,?,?,'requested',?,?)", (ticket_id, ticket["order_ref_id"], action_type, amount, reason, request_actor.get(), now())).lastrowid
        conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, "service_action_requested", request_actor.get(), reason, json.dumps({"action_id": action_id, "action_type": action_type, "amount": amount}, ensure_ascii=False), now()))
        audit(conn, "service_action_requested", "ticket", ticket_id, {"action_id": action_id, "action_type": action_type, "amount": amount})
        conn.commit()
    return {"action_id": action_id, "ticket_id": ticket_id, "status": "requested"}


@app.post("/api/tickets/{ticket_id}/attachments")
async def upload_ticket_attachment(ticket_id: int, file: UploadFile = File(...)) -> dict[str, Any]:
    allowed = {
        "application/pdf": (".pdf", b"%PDF"),
        "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": (".jpg", b"\xff\xd8\xff"),
        "text/plain": (".txt", None),
    }
    if file.content_type not in allowed:
        raise HTTPException(415, "仅支持 PDF、PNG、JPEG 和纯文本附件")
    content = await file.read(10 * 1024 * 1024 + 1)
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "附件不能为空且不得超过 10MB")
    extension, signature = allowed[file.content_type]
    if signature and not content.startswith(signature):
        raise HTTPException(422, "附件内容与文件类型不一致")
    try:
        if file.content_type.startswith("image/"):
            image = Image.open(io.BytesIO(content))
            image.verify()
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError("图像格式不受支持")
        elif file.content_type == "application/pdf" and (b"%%EOF" not in content[-2048:] or b" obj" not in content):
            raise ValueError("PDF 结构不完整")
        elif file.content_type == "text/plain":
            content.decode("utf-8")
    except (UnidentifiedImageError, OSError, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "附件无法被安全解析或与声明类型不一致") from exc
    if ATTACHMENT_SCANNER_URL:
        try:
            scan = httpx.post(ATTACHMENT_SCANNER_URL, content=content, headers={"Content-Type": file.content_type}, timeout=15)
            scan.raise_for_status()
            if scan.json().get("clean") is not True:
                raise HTTPException(422, "附件未通过恶意文件扫描")
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, "附件扫描服务不可用，文件未保存") from exc
    original_name = Path(file.filename or f"attachment{extension}").name[:200]
    if Path(original_name).suffix.lower() not in ({".jpg", ".jpeg"} if extension == ".jpg" else {extension}):
        raise HTTPException(422, "附件扩展名与内容类型不一致")
    stored_name = f"{uuid.uuid4().hex}{extension}"
    digest = hashlib.sha256(content).hexdigest()
    target = PRIVATE_ROOT / stored_name
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM tickets WHERE id=? AND COALESCE(is_archived,0)=0", (ticket_id,)).fetchone():
            raise HTTPException(404, "工单不存在")
        quota = conn.execute("SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM ticket_attachments WHERE ticket_id=? AND deleted_at IS NULL", (ticket_id,)).fetchone()
        account_bytes = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM ticket_attachments WHERE uploaded_by=? AND deleted_at IS NULL", (request_actor.get(),)).fetchone()[0]
        if quota[0] >= 20 or quota[1] + len(content) > 50 * 1024 * 1024 or account_bytes + len(content) > 500 * 1024 * 1024:
            raise HTTPException(413, "附件配额已满：每工单 20 个/50MB，每账号 500MB")
        if conn.execute("SELECT 1 FROM ticket_attachments WHERE ticket_id=? AND sha256=? AND deleted_at IS NULL", (ticket_id, digest)).fetchone():
            raise HTTPException(409, "该工单已存在相同附件")
        timestamp = now()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=ATTACHMENT_RETENTION_DAYS)).isoformat(timespec="seconds")
        try:
            target.write_bytes(content)
            attachment_id = conn.execute(
                "INSERT INTO ticket_attachments(ticket_id,stored_name,original_name,content_type,size_bytes,sha256,scan_status,uploaded_by,created_at,expires_at) VALUES(?,?,?,?,?,?,'clean',?,?,?)",
                (ticket_id, stored_name, original_name, file.content_type, len(content), digest, request_actor.get(), timestamp, expires_at),
            ).lastrowid
            audit(conn, "ticket_attachment_uploaded", "ticket", ticket_id, {"attachment_id": attachment_id, "size_bytes": len(content), "sha256": digest})
            conn.commit()
        except Exception:
            target.unlink(missing_ok=True)
            raise
    return {"attachment_id": attachment_id, "ticket_id": ticket_id, "original_name": original_name, "size_bytes": len(content)}


@app.get("/api/tickets/{ticket_id}/attachments/{attachment_id}")
def download_ticket_attachment(ticket_id: int, attachment_id: int) -> FileResponse:
    with closing(connect()) as conn:
        attachment = conn.execute("SELECT * FROM ticket_attachments WHERE id=? AND ticket_id=? AND deleted_at IS NULL AND scan_status='clean' AND (expires_at IS NULL OR expires_at>?)", (attachment_id, ticket_id, now())).fetchone()
    if not attachment:
        raise HTTPException(404, "附件不存在")
    target = (PRIVATE_ROOT / attachment["stored_name"]).resolve()
    if not target.is_relative_to(PRIVATE_ROOT) or not target.is_file():
        raise HTTPException(404, "附件文件不存在")
    return FileResponse(target, media_type=attachment["content_type"], filename=attachment["original_name"])


@app.post("/api/feedback/{external_event_id}/actions")
def create_feedback_action(
    external_event_id: str,
    action_type: str = Form(...),
    owner: str = Form(...),
    due_at: str = Form(...),
) -> dict[str, Any]:
    owner = owner.strip()
    valid_types = {"listing_update", "product_quality", "supplier_corrective_action", "packaging_change"}
    try:
        due = datetime.fromisoformat(due_at).replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    except ValueError as exc:
        raise HTTPException(422, "截止时间格式无效") from exc
    if action_type not in valid_types or len(owner) < 2 or due <= now():
        raise HTTPException(422, "行动类型、负责人或截止时间无效")
    with closing(connect()) as conn:
        feedback = conn.execute("SELECT sku FROM feedback_insights WHERE external_event_id=?", (external_event_id,)).fetchone()
        if not feedback:
            raise HTTPException(404, "反馈洞察不存在")
        action_id = conn.execute(
            "INSERT INTO feedback_actions(feedback_event_id,sku,action_type,owner,due_at,status,created_by,created_at) VALUES(?,?,?,?,?,'open',?,?)",
            (external_event_id, feedback["sku"], action_type, owner, due, request_actor.get(), now()),
        ).lastrowid
        audit(conn, "feedback_action_created", "feedback_action", action_id, {"feedback_event_id": external_event_id, "owner": owner})
        conn.commit()
    return {"action_id": action_id, "status": "open", "version": 1}


@app.post("/api/feedback-actions/{action_id}/update")
def update_feedback_action(action_id: int, status: str = Form(...), outcome: str = Form(default=""), version: int = Form(...)) -> dict[str, Any]:
    outcome = outcome.strip()
    if status not in {"open", "in_progress", "resolved"} or status == "resolved" and len(outcome) < 3:
        raise HTTPException(422, "行动状态无效，完成时必须填写结果")
    with closing(connect()) as conn:
        current = conn.execute("SELECT status FROM feedback_actions WHERE id=?", (action_id,)).fetchone()
        if not current:
            raise HTTPException(404, "改进行动不存在")
        transitions = {"open": {"open", "in_progress", "resolved"}, "in_progress": {"in_progress", "resolved"}, "resolved": {"resolved", "in_progress"}}
        if status not in transitions[current["status"]]:
            raise HTTPException(409, "行动状态不能这样流转")
        resolved_at = now() if status == "resolved" else None
        result = conn.execute(
            "UPDATE feedback_actions SET status=?,outcome=?,resolved_at=?,version=version+1 WHERE id=? AND version=?",
            (status, outcome or None, resolved_at, action_id, version),
        )
        if not result.rowcount:
            raise HTTPException(409, "改进行动已被其他人修改，请刷新后重试")
        audit(conn, "feedback_action_updated", "feedback_action", action_id, {"status": status, "outcome": outcome})
        conn.commit()
    return {"action_id": action_id, "status": status, "version": version + 1, "resolved_at": resolved_at}


@app.post("/api/service-actions/{action_id}/approve")
def approve_service_action(action_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        action = conn.execute("SELECT * FROM service_actions WHERE id=?", (action_id,)).fetchone()
        if not action:
            raise HTTPException(404, "售后动作不存在")
        result = conn.execute("UPDATE service_actions SET status='approved',approved_by=?,approved_at=? WHERE id=? AND status='requested'", (request_actor.get(), now(), action_id))
        if not result.rowcount:
            raise HTTPException(409, "售后动作状态已变化")
        conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (action["ticket_id"], "service_action_approved", request_actor.get(), action["reason"], json.dumps({"action_id": action_id}, ensure_ascii=False), now()))
        audit(conn, "service_action_approved", "ticket", action["ticket_id"], {"action_id": action_id})
        conn.commit()
    return {"action_id": action_id, "status": "approved"}


@app.post("/api/service-actions/{action_id}/complete")
def complete_service_action(action_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        action = conn.execute("SELECT * FROM service_actions WHERE id=?", (action_id,)).fetchone()
        if not action:
            raise HTTPException(404, "售后动作不存在")
        result = conn.execute("UPDATE service_actions SET status='completed',completed_at=? WHERE id=? AND status='approved'", (now(), action_id))
        if not result.rowcount:
            raise HTTPException(409, "售后动作必须先批准")
        conn.execute("INSERT INTO ticket_events(ticket_id,event_type,actor,body,metadata,created_at) VALUES(?,?,?,?,?,?)", (action["ticket_id"], "service_action_completed", request_actor.get(), action["reason"], json.dumps({"action_id": action_id, "mode": "internal_record_only"}, ensure_ascii=False), now()))
        audit(conn, "service_action_completed", "ticket", action["ticket_id"], {"action_id": action_id, "mode": "internal_record_only"})
        conn.commit()
    return {"action_id": action_id, "status": "completed", "external_execution": False}


@app.post("/api/account/password")
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...)) -> dict[str, Any]:
    if len(new_password) < 12 or not re.search(r"[A-Z]", new_password) or not re.search(r"[a-z]", new_password) or not re.search(r"\d", new_password):
        raise HTTPException(422, "新密码至少 12 位，并包含大小写字母和数字")
    username = request.state.user["sub"]
    with closing(connect()) as conn:
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user or not verify_password(current_password, user["password_hash"]):
            raise HTTPException(403, "当前密码错误")
        conn.execute("UPDATE users SET password_hash=?,must_change_password=0,password_changed_at=? WHERE username=?", (hash_password(new_password), now(), username))
        conn.execute("UPDATE user_sessions SET revoked_at=? WHERE username=? AND session_id<>? AND revoked_at IS NULL", (now(), username, request.state.user["sid"]))
        audit(conn, "password_changed", "user", None, {"username": username})
        conn.commit()
    return {"username": username, "status": "password_changed", "other_sessions_revoked": True}


@app.post("/api/account/mfa/setup")
def setup_mfa(request: Request) -> dict[str, Any]:
    secret = generate_totp_secret()
    recovery_codes = [f"{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}" for _ in range(8)]
    username = request.state.user["sub"]
    with closing(connect()) as conn:
        conn.execute("UPDATE users SET mfa_secret=?,mfa_enabled=0 WHERE username=?", (encrypt_mfa_secret(secret), username))
        conn.execute("DELETE FROM mfa_recovery_codes WHERE username=?", (username,))
        conn.executemany("INSERT INTO mfa_recovery_codes(username,code_hash,created_at) VALUES(?,?,?)", [(username, hashlib.sha256(f"{username}:{code}".encode()).hexdigest(), now()) for code in recovery_codes])
        audit(conn, "mfa_setup_started", "user", None, {"username": username})
        conn.commit()
    return {"status": "pending_verification", "secret": secret, "recovery_codes": recovery_codes, "otpauth_uri": f"otpauth://totp/CrossBorderOps:{username}?secret={secret}&issuer=CrossBorderOps"}


@app.post("/api/account/mfa/enable")
def enable_mfa(request: Request, code: str = Form(...)) -> dict[str, Any]:
    username = request.state.user["sub"]
    with closing(connect()) as conn:
        user = conn.execute("SELECT mfa_secret FROM users WHERE username=?", (username,)).fetchone()
        if not user or not user["mfa_secret"] or not verify_totp(decrypt_mfa_secret(user["mfa_secret"]), code.strip()):
            raise HTTPException(422, "动态验证码无效")
        conn.execute("UPDATE users SET mfa_enabled=1 WHERE username=?", (username,))
        audit(conn, "mfa_enabled", "user", None, {"username": username})
        conn.commit()
    return {"username": username, "mfa_enabled": True}


@app.post("/api/account/sessions/{session_id}/revoke")
def revoke_session(request: Request, session_id: str) -> dict[str, Any]:
    with closing(connect()) as conn:
        result = conn.execute("UPDATE user_sessions SET revoked_at=? WHERE session_id=? AND username=? AND revoked_at IS NULL", (now(), session_id, request.state.user["sub"]))
        if not result.rowcount:
            raise HTTPException(404, "会话不存在或已撤销")
        audit(conn, "session_revoked", "user", None, {"username": request.state.user["sub"], "session_id": session_id[:8]})
        conn.commit()
    return {"session_id": session_id, "status": "revoked"}


@app.post("/api/admin/users")
def create_user(username: str = Form(...), password: str = Form(...), role: str = Form(...)) -> dict[str, Any]:
    username = username.strip()
    strong_password = len(password) >= 12 and bool(re.search(r"[A-Z]", password)) and bool(re.search(r"[a-z]", password)) and bool(re.search(r"\d", password))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username) or not strong_password or role not in {"viewer", "operator", "approver", "admin"}:
        raise HTTPException(422, "用户名、初始密码或角色无效")
    with closing(connect()) as conn:
        try:
            conn.execute("INSERT INTO users(username,password_hash,role,is_active,created_at,must_change_password,password_changed_at) VALUES(?,?,?,?,?,1,?)", (username, hash_password(password), role, 1, now(), now()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "用户名已存在") from exc
        audit(conn, "user_created", "user", None, {"username": username, "role": role})
        conn.commit()
    return {"username": username, "role": role, "must_change_password": True}


@app.post("/api/admin/users/{username}/status")
def update_user_status(request: Request, username: str, is_active: int = Form(...)) -> dict[str, Any]:
    if is_active not in {0, 1} or username == request.state.user["sub"] and not is_active:
        raise HTTPException(422, "不能停用当前登录账号")
    with closing(connect()) as conn:
        result = conn.execute("UPDATE users SET is_active=? WHERE username=?", (is_active, username))
        if not result.rowcount:
            raise HTTPException(404, "用户不存在")
        if not is_active:
            conn.execute("UPDATE user_sessions SET revoked_at=? WHERE username=? AND revoked_at IS NULL", (now(), username))
        audit(conn, "user_status_updated", "user", None, {"username": username, "is_active": bool(is_active)})
        conn.commit()
    return {"username": username, "is_active": bool(is_active)}


@app.post("/api/admin/users/{username}/reset-mfa")
def reset_user_mfa(username: str) -> dict[str, Any]:
    with closing(connect()) as conn:
        if not conn.execute("UPDATE users SET mfa_secret=NULL,mfa_enabled=0 WHERE username=?", (username,)).rowcount:
            raise HTTPException(404, "用户不存在")
        conn.execute("DELETE FROM mfa_recovery_codes WHERE username=?", (username,))
        conn.execute("UPDATE user_sessions SET revoked_at=? WHERE username=? AND revoked_at IS NULL", (now(), username))
        audit(conn, "user_mfa_reset", "user", None, {"username": username})
        conn.commit()
    return {"username": username, "mfa_enabled": False, "sessions_revoked": True}


@app.get("/{page}", response_class=HTMLResponse, include_in_schema=False)
def show_page(request: Request, page: str) -> HTMLResponse:
    if page not in {"dashboard", "orders", "products", "profit", "listings", "inventory", "procurement", "tickets", "feedback", "audit", "settings"}:
        raise HTTPException(404)
    context = page_context(page, dict(request.query_params), request.state.user)
    context["current_user"] = request.state.user
    return templates.TemplateResponse(request=request, name="app.html", context=context)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "init-db":
        init_db()
        print(f"Database initialized: {db_path()}")
    else:
        print("Usage: py -3.12 -m app.main init-db")
