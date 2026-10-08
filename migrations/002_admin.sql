-- Admin features.

-- A removed lot is closed and hidden from the public; its bids stay for the audit trail.
ALTER TABLE auctions ADD COLUMN removed_at TIMESTAMPTZ;

-- Each API instance reports in every few seconds, so the admin portal can show
-- which instances are alive and how many sockets each one holds.
CREATE TABLE instances (
    name              TEXT        PRIMARY KEY,
    started_at        TIMESTAMPTZ NOT NULL,
    last_seen         TIMESTAMPTZ NOT NULL,
    websocket_clients INTEGER     NOT NULL DEFAULT 0
);
