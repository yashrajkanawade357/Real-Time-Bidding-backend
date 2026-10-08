-- A second, shareable admin key for judges, and an audit trail of admin actions.

-- The judge key lives in the database (not the environment) so the owner can
-- rotate or switch it off from the admin portal without a redeploy.
CREATE TABLE access_keys (
    role       TEXT        PRIMARY KEY CHECK (role IN ('judge')),
    key        TEXT        NOT NULL,
    enabled    BOOLEAN     NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every change made through the admin API, and who made it.
CREATE TABLE admin_actions (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    role       TEXT        NOT NULL CHECK (role IN ('owner', 'judge')),
    ip         TEXT        NOT NULL,
    action     TEXT        NOT NULL,
    auction_id BIGINT,
    detail     TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX admin_actions_recent ON admin_actions (id DESC);
