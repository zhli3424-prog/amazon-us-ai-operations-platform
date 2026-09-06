ALTER TABLE notification_deliveries ADD COLUMN next_attempt_at TEXT;
ALTER TABLE notification_deliveries ADD COLUMN response_code INTEGER;
ALTER TABLE privacy_requests ADD COLUMN subject_ref TEXT;

CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK(status IN ('success','failed')),
    backup_path TEXT,
    replica_path TEXT,
    sha256 TEXT,
    schema_version INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS channel_sync_cursors (
    channel TEXT NOT NULL,
    sync_type TEXT NOT NULL,
    cursor TEXT,
    watermark TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(channel,sync_type)
);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL REFERENCES products(sku),
    quantity_delta INTEGER NOT NULL CHECK(quantity_delta <> 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested','approved','applied','rejected')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    applied_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS service_action_reversals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_action_id INTEGER NOT NULL UNIQUE REFERENCES service_actions(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested','approved','applied','rejected')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS purchase_order_cancellations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_order_id INTEGER NOT NULL UNIQUE REFERENCES purchase_orders(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested','approved','applied','rejected')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role TEXT NOT NULL,
    permission TEXT NOT NULL,
    PRIMARY KEY(role,permission)
);

INSERT OR IGNORE INTO role_permissions(role,permission) VALUES
('viewer','read'),
('operator','read'),('operator','catalog.write'),('operator','ticket.write'),('operator','listing.write'),('operator','channel.sync'),('operator','procurement.write'),
('approver','read'),('approver','catalog.write'),('approver','ticket.write'),('approver','listing.write'),
('approver','approve'),('approver','channel.sync'),('approver','procurement.write'),('approver','inventory.receive'),('approver','inventory.adjust'),
('admin','*');

CREATE INDEX IF NOT EXISTS idx_notification_deliveries_ready ON notification_deliveries(status,next_attempt_at,id);
CREATE INDEX IF NOT EXISTS idx_inventory_adjustments_status ON inventory_adjustments(status,requested_at);

CREATE TRIGGER IF NOT EXISTS products_money_guard_insert BEFORE INSERT ON products
WHEN NEW.price_cents<>CAST(ROUND(NEW.price*100) AS INTEGER) OR NEW.cost_cents<>CAST(ROUND(NEW.cost*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'product money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS products_money_guard_update BEFORE UPDATE OF price,cost,price_cents,cost_cents ON products
WHEN NEW.price_cents<>CAST(ROUND(NEW.price*100) AS INTEGER) OR NEW.cost_cents<>CAST(ROUND(NEW.cost*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'product money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS financial_money_guard_insert BEFORE INSERT ON financial_transactions
WHEN NEW.amount_cents<>CAST(ROUND(NEW.amount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'financial money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS financial_money_guard_update BEFORE UPDATE OF amount,amount_cents ON financial_transactions
WHEN NEW.amount_cents<>CAST(ROUND(NEW.amount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'financial money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS service_action_money_guard_insert BEFORE INSERT ON service_actions
WHEN NEW.amount_cents<>CAST(ROUND(NEW.amount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'service action money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS service_action_money_guard_update BEFORE UPDATE OF amount,amount_cents ON service_actions
WHEN NEW.amount_cents<>CAST(ROUND(NEW.amount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'service action money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS purchase_order_money_guard_insert BEFORE INSERT ON purchase_orders
WHEN NEW.total_amount_cents<>CAST(ROUND(NEW.total_amount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'purchase order money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS purchase_order_money_guard_update BEFORE UPDATE OF total_amount,total_amount_cents ON purchase_orders
WHEN NEW.total_amount_cents<>CAST(ROUND(NEW.total_amount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'purchase order money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS orders_money_guard_insert BEFORE INSERT ON orders
WHEN NEW.item_total_cents<>CAST(ROUND(NEW.item_total*100) AS INTEGER)
 OR NEW.shipping_total_cents<>CAST(ROUND(NEW.shipping_total*100) AS INTEGER)
 OR NEW.tax_total_cents<>CAST(ROUND(NEW.tax_total*100) AS INTEGER)
 OR NEW.promotion_total_cents<>CAST(ROUND(NEW.promotion_total*100) AS INTEGER)
 OR NEW.refund_total_cents<>CAST(ROUND(NEW.refund_total*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'order money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS orders_money_guard_update BEFORE UPDATE OF item_total,shipping_total,tax_total,promotion_total,refund_total,item_total_cents,shipping_total_cents,tax_total_cents,promotion_total_cents,refund_total_cents ON orders
WHEN NEW.item_total_cents<>CAST(ROUND(NEW.item_total*100) AS INTEGER)
 OR NEW.shipping_total_cents<>CAST(ROUND(NEW.shipping_total*100) AS INTEGER)
 OR NEW.tax_total_cents<>CAST(ROUND(NEW.tax_total*100) AS INTEGER)
 OR NEW.promotion_total_cents<>CAST(ROUND(NEW.promotion_total*100) AS INTEGER)
 OR NEW.refund_total_cents<>CAST(ROUND(NEW.refund_total*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'order money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS order_items_money_guard_insert BEFORE INSERT ON order_items
WHEN NEW.item_price_cents<>CAST(ROUND(NEW.item_price*100) AS INTEGER)
 OR NEW.item_tax_cents<>CAST(ROUND(NEW.item_tax*100) AS INTEGER)
 OR NEW.promotion_discount_cents<>CAST(ROUND(NEW.promotion_discount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'order item money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS order_items_money_guard_update BEFORE UPDATE OF item_price,item_tax,promotion_discount,item_price_cents,item_tax_cents,promotion_discount_cents ON order_items
WHEN NEW.item_price_cents<>CAST(ROUND(NEW.item_price*100) AS INTEGER)
 OR NEW.item_tax_cents<>CAST(ROUND(NEW.item_tax*100) AS INTEGER)
 OR NEW.promotion_discount_cents<>CAST(ROUND(NEW.promotion_discount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'order item money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS returns_money_guard_insert BEFORE INSERT ON returns
WHEN NEW.refund_amount_cents<>CAST(ROUND(NEW.refund_amount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'return money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS returns_money_guard_update BEFORE UPDATE OF refund_amount,refund_amount_cents ON returns
WHEN NEW.refund_amount_cents<>CAST(ROUND(NEW.refund_amount*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'return money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS purchase_order_items_money_guard_insert BEFORE INSERT ON purchase_order_items
WHEN NEW.unit_cost_cents<>CAST(ROUND(NEW.unit_cost*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'purchase order item money columns disagree'); END;
CREATE TRIGGER IF NOT EXISTS purchase_order_items_money_guard_update BEFORE UPDATE OF unit_cost,unit_cost_cents ON purchase_order_items
WHEN NEW.unit_cost_cents<>CAST(ROUND(NEW.unit_cost*100) AS INTEGER)
BEGIN SELECT RAISE(ABORT,'purchase order item money columns disagree'); END;
