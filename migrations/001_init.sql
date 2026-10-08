-- Auctions, and an append-only log of every bid attempt.
-- Money is stored as whole currency units in BIGINT: no floating point anywhere.

CREATE TABLE auctions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title           TEXT        NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    description     TEXT        NOT NULL DEFAULT '',
    starting_price  BIGINT      NOT NULL CHECK (starting_price > 0),
    min_increment   BIGINT      NOT NULL DEFAULT 1 CHECK (min_increment > 0),
    -- The current high bid. NULL until the first bid is accepted.
    current_price   BIGINT      CHECK (current_price >= starting_price),
    leader          TEXT,
    bid_count       INTEGER     NOT NULL DEFAULT 0,
    -- Bumped on every change to this row. Clients use it to ignore events
    -- that are older than the snapshot they already hold.
    version         BIGINT      NOT NULL DEFAULT 0,
    status          TEXT        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    ends_at         TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ
);

-- The closer only ever looks for open auctions past their end time.
CREATE INDEX auctions_open_by_end ON auctions (ends_at) WHERE status = 'open';

-- Every bid attempt, accepted or rejected. Rows are never updated or deleted.
CREATE TABLE bids (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auction_id  BIGINT      NOT NULL REFERENCES auctions (id),
    bidder      TEXT        NOT NULL,
    amount      BIGINT      NOT NULL CHECK (amount > 0),
    -- Client-chosen idempotency key. Resending the same request_id returns the
    -- original outcome instead of bidding twice (e.g. after a reconnect).
    request_id  TEXT        NOT NULL,
    status      TEXT        NOT NULL CHECK (status IN ('accepted', 'rejected')),
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (auction_id, request_id)
);

CREATE INDEX bids_by_auction ON bids (auction_id, id DESC);
