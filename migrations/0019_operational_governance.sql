CREATE TABLE IF NOT EXISTS data_retention_policies (
    data_type TEXT PRIMARY KEY,
    retention_days INTEGER NOT NULL CHECK(retention_days > 0),
    action TEXT NOT NULL CHECK(action IN ('anonymize','delete')),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS privacy_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_type TEXT NOT NULL CHECK(request_type IN ('export','anonymize')),
    customer_alias TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested','completed','rejected')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_by TEXT,
    completed_at TEXT,
    result_summary TEXT
);
CREATE TABLE IF NOT EXISTS legal_holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT,
    UNIQUE(entity_type,entity_key)
);
CREATE TABLE IF NOT EXISTS audit_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    last_event_id INTEGER NOT NULL,
    event_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL REFERENCES operational_notifications(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(notification_id,channel)
);
