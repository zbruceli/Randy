import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .profile import UserProfile


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionRow:
    session_id: str
    user_id: str
    topic: str | None
    started_at: str
    ended_at: str | None
    cost_usd: float
    conversation_id: str | None = None


@dataclass
class ConversationRow:
    conversation_id: str
    user_id: str
    title: str
    pinned: bool
    archived_at: str | None
    created_at: str
    updated_at: str


@dataclass
class FactRow:
    fact_id: str
    session_id: str | None
    topic: str
    claim: str
    source_url: str | None
    source_title: str | None
    raw_excerpt: str | None
    volatility: str        # 'evergreen' | 'slow' | 'volatile'
    confidence: str        # 'verified' | 'reported' | 'estimated'
    retrieved_at: str


class MemoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        schema = (Path(__file__).parent / "schema.sql").read_text()
        with self._conn() as conn:
            conn.executescript(schema)
            # Migrations for older DBs created before each column existed.
            # Each is a no-op on fresh DBs since the column is in schema.sql.
            sessions_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "conversation_id" not in sessions_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN conversation_id TEXT")
            # WAL mode lets the bot's writes and the web app's reads coexist.
            conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- users ----

    def ensure_user(self, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)",
                (user_id, _now()),
            )

    # ---- profile ----

    def get_profile(self, user_id: str) -> UserProfile:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT profile_json FROM profile WHERE user_id = ?", (user_id,)
            ).fetchone()
        return UserProfile.from_json(user_id, row["profile_json"] if row else None)

    def save_profile(self, profile: UserProfile) -> None:
        profile.updated_at = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO profile(user_id, profile_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    updated_at = excluded.updated_at
                """,
                (profile.user_id, profile.to_json(), profile.updated_at),
            )

    def delete_profile(self, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM profile WHERE user_id = ?", (user_id,))

    # ---- sessions ----

    def start_session(
        self,
        session_id: str,
        user_id: str,
        topic: str | None,
        conversation_id: str | None = None,
    ) -> None:
        self.ensure_user(user_id)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions(session_id, user_id, conversation_id, topic, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, user_id, conversation_id, topic, _now()),
            )
            if conversation_id is not None:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                    (_now(), conversation_id),
                )

    def end_session(self, session_id: str, cost_usd: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, cost_usd = ? WHERE session_id = ?",
                (_now(), cost_usd, session_id),
            )

    def cost_summary(self, user_id: str) -> dict[str, float | int]:
        """Today (UTC), this month (UTC), lifetime, plus session count + last cost."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self._conn() as conn:
            today_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS s, COUNT(*) AS n "
                "FROM sessions WHERE user_id = ? AND started_at LIKE ?",
                (user_id, today + "%"),
            ).fetchone()
            month_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS s, COUNT(*) AS n "
                "FROM sessions WHERE user_id = ? AND started_at LIKE ?",
                (user_id, month + "%"),
            ).fetchone()
            life_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS s, COUNT(*) AS n "
                "FROM sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            last_row = conn.execute(
                "SELECT cost_usd FROM sessions WHERE user_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return {
            "today_cost": today_row["s"], "today_n": today_row["n"],
            "month_cost": month_row["s"], "month_n": month_row["n"],
            "life_cost": life_row["s"], "life_n": life_row["n"],
            "last_cost": last_row["cost_usd"] if last_row else 0.0,
        }

    def recent_sessions(self, user_id: str, limit: int = 5) -> list[SessionRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT session_id, user_id, conversation_id, topic, started_at, ended_at, cost_usd
                FROM sessions WHERE user_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [SessionRow(**dict(r)) for r in rows]

    def get_session(self, session_id: str) -> SessionRow | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT session_id, user_id, conversation_id, topic, started_at, ended_at, cost_usd
                FROM sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return SessionRow(**dict(row)) if row else None

    def session_turns(self, session_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT role, persona, model, content, tokens_in, tokens_out, cost_usd, created_at
                FROM turns WHERE session_id = ?
                ORDER BY turn_id ASC
                """,
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- turns ----

    # ---- per-user settings ----

    def get_round2_enabled(self, user_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT round2_enabled FROM user_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
        return bool(row["round2_enabled"]) if row else False

    def set_round2_enabled(self, user_id: str, enabled: bool) -> None:
        self.ensure_user(user_id)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO user_settings(user_id, round2_enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    round2_enabled = excluded.round2_enabled,
                    updated_at = excluded.updated_at
                """,
                (user_id, 1 if enabled else 0, _now()),
            )

    # ---- conversations ----

    def create_conversation(self, conversation_id: str, user_id: str, title: str) -> None:
        self.ensure_user(user_id)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO conversations(conversation_id, user_id, title, pinned, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (conversation_id, user_id, title, _now(), _now()),
            )

    def get_conversation(self, conversation_id: str) -> ConversationRow | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["pinned"] = bool(d["pinned"])
        return ConversationRow(**d)

    def list_conversations(
        self, user_id: str, *, pinned_only: bool = False, include_archived: bool = False
    ) -> list[ConversationRow]:
        where = ["user_id = ?"]
        params: list = [user_id]
        if pinned_only:
            where.append("pinned = 1")
        if not include_archived:
            where.append("archived_at IS NULL")
        sql = (
            "SELECT * FROM conversations WHERE "
            + " AND ".join(where)
            + " ORDER BY pinned DESC, updated_at DESC"
        )
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["pinned"] = bool(d["pinned"])
            out.append(ConversationRow(**d))
        return out

    def update_conversation_title(self, conversation_id: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE conversation_id = ?",
                (title, _now(), conversation_id),
            )

    def set_conversation_pinned(self, conversation_id: str, pinned: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE conversations SET pinned = ?, updated_at = ? WHERE conversation_id = ?",
                (1 if pinned else 0, _now(), conversation_id),
            )

    def archive_conversation(self, conversation_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE conversations SET archived_at = ?, pinned = 0 WHERE conversation_id = ?",
                (_now(), conversation_id),
            )

    def sessions_in_conversation(self, conversation_id: str) -> list[SessionRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT session_id, user_id, conversation_id, topic, started_at, ended_at, cost_usd
                FROM sessions WHERE conversation_id = ?
                ORDER BY started_at ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [SessionRow(**dict(r)) for r in rows]

    # ---- facts ----

    def upsert_fact(
        self,
        *,
        fact_id: str,
        session_id: str | None,
        topic: str,
        claim: str,
        source_url: str | None = None,
        source_title: str | None = None,
        raw_excerpt: str | None = None,
        volatility: str = "slow",
        confidence: str = "reported",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO facts(fact_id, session_id, topic, claim, source_url,
                                  source_title, raw_excerpt, volatility, confidence, retrieved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_id) DO UPDATE SET
                    session_id   = excluded.session_id,
                    claim        = excluded.claim,
                    source_url   = excluded.source_url,
                    source_title = excluded.source_title,
                    raw_excerpt  = excluded.raw_excerpt,
                    volatility   = excluded.volatility,
                    confidence   = excluded.confidence,
                    retrieved_at = excluded.retrieved_at
                """,
                (
                    fact_id, session_id, topic, claim, source_url,
                    source_title, raw_excerpt, volatility, confidence, _now(),
                ),
            )

    def find_facts_by_topic(
        self, topic: str, *, max_age_seconds: int | None = None, limit: int = 20
    ) -> list[FactRow]:
        params: list = [topic]
        clauses = ["topic = ?"]
        if max_age_seconds is not None:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat(timespec="seconds")
            clauses.append("retrieved_at >= ?")
            params.append(cutoff)
        sql = (
            "SELECT * FROM facts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY retrieved_at DESC LIMIT ?"
        )
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [FactRow(**dict(r)) for r in rows]

    def session_facts(self, session_id: str) -> list[FactRow]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM facts WHERE session_id = ? ORDER BY topic, retrieved_at",
                (session_id,),
            ).fetchall()
        return [FactRow(**dict(r)) for r in rows]

    def recent_facts(self, *, limit: int = 50) -> list[FactRow]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM facts ORDER BY retrieved_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [FactRow(**dict(r)) for r in rows]

    def topics_summary(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT topic,
                       COUNT(*) AS fact_count,
                       MAX(retrieved_at) AS last_seen
                FROM facts
                GROUP BY topic
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- per-chat active thread ----

    def get_active_thread(self, chat_id: int) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT conversation_id FROM chat_active_thread WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row["conversation_id"] if row and row["conversation_id"] else None

    def set_active_thread(self, chat_id: int, conversation_id: str | None) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO chat_active_thread(chat_id, conversation_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, conversation_id, _now()),
            )

    def append_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        persona: str | None = None,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO turns(session_id, role, persona, model, content,
                                  tokens_in, tokens_out, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, role, persona, model, content,
                    tokens_in, tokens_out, cost_usd, _now(),
                ),
            )
