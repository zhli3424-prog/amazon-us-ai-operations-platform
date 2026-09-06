ALTER TABLE notification_deliveries ADD COLUMN claim_token TEXT;
ALTER TABLE notification_deliveries ADD COLUMN claimed_at TEXT;
ALTER TABLE sync_failures ADD COLUMN status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','retrying'));
ALTER TABLE sync_failures ADD COLUMN resolved_by TEXT;
ALTER TABLE sync_failures ADD COLUMN resolved_at TEXT;
ALTER TABLE sync_failures ADD COLUMN resolution_note TEXT;
ALTER TABLE sync_failures ADD COLUMN retry_job_id INTEGER REFERENCES jobs(id);
ALTER TABLE ticket_attachments ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0 CHECK(encrypted IN (0,1));

INSERT OR IGNORE INTO role_permissions(role,permission) VALUES
('operator','sync_failure.manage'),('approver','sync_failure.manage');

CREATE INDEX IF NOT EXISTS idx_notification_deliveries_claim ON notification_deliveries(status,claimed_at,next_attempt_at,id);
CREATE INDEX IF NOT EXISTS idx_sync_failures_status ON sync_failures(status,created_at DESC);
