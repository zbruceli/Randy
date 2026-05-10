CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    user_id     TEXT PRIMARY KEY REFERENCES users(user_id),
    profile_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    title           TEXT NOT NULL,
    pinned          INTEGER NOT NULL DEFAULT 0,
    archived_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_pinned ON conversations(user_id, pinned, updated_at);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    conversation_id TEXT REFERENCES conversations(conversation_id),
    topic           TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    cost_usd        REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    role        TEXT NOT NULL,
    persona     TEXT,
    model       TEXT,
    content     TEXT NOT NULL,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    cost_usd    REAL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id        TEXT PRIMARY KEY REFERENCES users(user_id),
    round2_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);

-- Per-Telegram-chat active thread. Lets the bot continue a thread across
-- restarts without requiring the user to re-anchor manually.
CREATE TABLE IF NOT EXISTS chat_active_thread (
    chat_id         INTEGER PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(conversation_id),
    updated_at      TEXT NOT NULL
);

-- Researched facts: ground-truth claims pulled from external sources before
-- R1, shared across all personas in a session. Volatility tags drive cache
-- staleness for cross-session reuse.
CREATE TABLE IF NOT EXISTS facts (
    fact_id         TEXT PRIMARY KEY,
    session_id      TEXT REFERENCES sessions(session_id),
    topic           TEXT NOT NULL,
    claim           TEXT NOT NULL,
    source_url      TEXT,
    source_title    TEXT,
    raw_excerpt     TEXT,
    volatility      TEXT NOT NULL DEFAULT 'slow',
    confidence      TEXT NOT NULL DEFAULT 'reported',
    retrieved_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_topic ON facts(topic);
CREATE INDEX IF NOT EXISTS idx_facts_session ON facts(session_id);
CREATE INDEX IF NOT EXISTS idx_facts_retrieved ON facts(retrieved_at);
