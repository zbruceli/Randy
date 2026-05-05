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


class MemoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        schema = (Path(__file__).parent / "schema.sql").read_text()
        with self._conn() as conn:
            conn.executescript(schema)

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

    def start_session(self, session_id: str, user_id: str, topic: str | None) -> None:
        self.ensure_user(user_id)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions(session_id, user_id, topic, started_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, user_id, topic, _now()),
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
                SELECT session_id, user_id, topic, started_at, ended_at, cost_usd
                FROM sessions WHERE user_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [SessionRow(**dict(r)) for r in rows]

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
