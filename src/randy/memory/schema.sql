CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    user_id     TEXT PRIMARY KEY REFERENCES users(user_id),
    profile_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    topic       TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    cost_usd    REAL DEFAULT 0
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
