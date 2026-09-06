from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


REQUIRED_SCHEMA_VERSION = 21
REQUIRED_COLUMNS = {
    "products": {"sku", "asin", "status", "version", "moq", "price_cents", "cost_cents"},
    "listings": {"product_id", "status", "version", "evidence_snapshot", "created_by", "edited_by"},
    "tickets": {"external_event_id", "status", "assigned_to", "version"},
    "ticket_messages": {"ticket_id", "body", "status", "approved_by", "attempts", "version"},
    "financial_transactions": {"external_transaction_id", "amount_cents", "currency", "posted_at"},
    "orders": {"item_total_cents", "refund_total_cents", "currency"},
    "order_items": {"item_price_cents", "unit_cost_cents"},
    "service_actions": {"amount_cents", "currency", "requested_by", "approved_by"},
    "purchase_orders": {"total_amount_cents", "created_by", "approved_by"},
    "purchase_order_items": {"ordered_qty", "received_qty", "rejected_qty", "unit_cost_cents"},
    "warehouse_inventory": {"sku", "on_hand", "qc_hold"},
    "ticket_attachments": {"sha256", "scan_status", "expires_at", "deleted_at", "encrypted"},
    "operational_notifications": {"fingerprint", "severity", "status", "href"},
    "notification_deliveries": {"status", "attempts", "next_attempt_at", "response_code", "claim_token", "claimed_at"},
    "sync_failures": {"status", "resolved_by", "resolved_at", "resolution_note", "retry_job_id"},
    "backup_runs": {"status", "backup_path", "replica_path", "sha256", "completed_at"},
    "channel_sync_cursors": {"channel", "sync_type", "cursor", "watermark"},
    "inventory_adjustments": {"sku", "quantity_delta", "status", "requested_by", "approved_by"},
    "service_action_reversals": {"service_action_id", "status", "requested_by", "approved_by"},
    "purchase_order_cancellations": {"purchase_order_id", "status", "requested_by", "approved_by"},
    "role_permissions": {"role", "permission"},
    "audit_events": {"previous_hash", "event_hash", "request_id"},
}
REQUIRED_INDEXES = {"idx_tickets_external_event", "idx_financial_sku_time", "idx_ticket_messages_ticket", "idx_jobs_ready", "idx_notifications_status_severity", "idx_notification_deliveries_ready", "idx_notification_deliveries_claim", "idx_sync_failures_status", "idx_inventory_adjustments_status"}
REQUIRED_TRIGGERS = {
    "audit_no_update", "audit_no_delete", "tickets_product_guard", "inventory_product_guard",
    "products_money_guard_insert", "financial_money_guard_insert", "service_action_money_guard_insert",
    "purchase_order_money_guard_insert", "orders_money_guard_insert", "order_items_money_guard_insert",
    "returns_money_guard_insert", "purchase_order_items_money_guard_insert",
}


def check(database: Path) -> int:
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在: {database}")
    with sqlite3.connect(database) as conn:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        version = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
        objects = {(row[0], row[1]) for row in conn.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','index','trigger')")}
        missing_columns = []
        for table, expected in REQUIRED_COLUMNS.items():
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")} if (table, "table") in objects else set()
            missing_columns.extend(f"{table}.{column}" for column in sorted(expected - actual))
    if integrity != "ok":
        raise RuntimeError(f"数据库完整性异常: {integrity}")
    if version != REQUIRED_SCHEMA_VERSION:
        raise RuntimeError(f"迁移版本不一致: 当前 {version}，应用要求 {REQUIRED_SCHEMA_VERSION}")
    missing_indexes = sorted(name for name in REQUIRED_INDEXES if (name, "index") not in objects)
    missing_triggers = sorted(name for name in REQUIRED_TRIGGERS if (name, "trigger") not in objects)
    if missing_columns or missing_indexes or missing_triggers:
        raise RuntimeError(f"数据库结构不完整: columns={missing_columns}, indexes={missing_indexes}, triggers={missing_triggers}")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="部署前检查数据库完整性与迁移版本")
    parser.add_argument("--database", type=Path, default=Path("data/app.db"))
    args = parser.parse_args()
    print(f"schema_version={check(args.database)} integrity=ok")
