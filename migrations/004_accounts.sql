-- Bidder accounts (email + password), sessions, and anti-sniping.

CREATE TABLE users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         TEXT        NOT NULL,
    -- The public name on the floor. Emails are never shown to other people.
    display_name  TEXT        NOT NULL,
    -- scrypt$N$r$p$salt$hash - the password itself is never stored.
    password_hash TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- One account per address, and one person per name, whatever the letter case.
CREATE UNIQUE INDEX users_email_unique ON users (lower(email));
CREATE UNIQUE INDEX users_name_unique  ON users (lower(display_name));

CREATE TABLE sessions (
    -- SHA-256 of the cookie value. A leaked copy of this table can't be used
    -- to log in, because the cookie value itself is never stored.
    token_hash TEXT        PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX sessions_by_user ON sessions (user_id);

-- Which account placed each bid (NULL for bids from before accounts existed).
ALTER TABLE bids ADD COLUMN user_id BIGINT REFERENCES users (id);

-- Anti-sniping: how many times a late bid pushed the closing time back.
ALTER TABLE auctions ADD COLUMN extensions INTEGER NOT NULL DEFAULT 0;
