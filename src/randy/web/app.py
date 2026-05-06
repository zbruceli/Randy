"""Local web dashboard for Randy.

Listens on 127.0.0.1 only — no auth, no TLS. Pulls from the same SQLite
the bot writes; uses ConsultationRunner so the bot's task model and the
web's task model are the same primitives.
"""

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..memory import MemoryStore
from ..orchestrator import ConsultationRunner

logger = logging.getLogger("randy.web")

WEB_USER_ID = "web-local"  # single-user web app — no per-user identity needed locally

_BASE_DIR = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


def _title_from_question(q: str, n: int = 60) -> str:
    q = q.strip().replace("\n", " ")
    return q if len(q) <= n else q[: n - 1] + "…"


def create_app() -> FastAPI:
    store = MemoryStore(settings.db_path)
    runner = ConsultationRunner(store)

    app = FastAPI(title="Randy Dashboard")
    app.state.store = store
    app.state.runner = runner

    static_dir = _BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        pinned = store.list_conversations(WEB_USER_ID, pinned_only=True)
        all_convs = store.list_conversations(WEB_USER_ID)
        unpinned = [c for c in all_convs if not c.pinned]
        recent = store.recent_sessions(WEB_USER_ID, limit=10)
        profile = store.get_profile(WEB_USER_ID)
        cost = store.cost_summary(WEB_USER_ID)
        round2_default = store.get_round2_enabled(WEB_USER_ID)
        return _TEMPLATES.TemplateResponse(
            request,
            "home.html",
            {
                "pinned": pinned,
                "unpinned": unpinned[:10],
                "recent": recent,
                "profile": profile,
                "cost": cost,
                "round2_default": round2_default,
            },
        )

    @app.post("/consult", response_class=HTMLResponse)
    async def consult(
        request: Request,
        question: str = Form(...),
        use_profile: str | None = Form(None),
        round2: str | None = Form(None),
        conversation_id: str | None = Form(None),
    ):
        question = (question or "").strip()
        if not question:
            raise HTTPException(400, "empty question")

        # Auto-create a conversation if none was passed.
        if not conversation_id:
            conversation_id = uuid.uuid4().hex[:12]
            store.create_conversation(
                conversation_id=conversation_id,
                user_id=WEB_USER_ID,
                title=_title_from_question(question),
            )

        # Build prior-context preface for follow-ups in an existing conversation.
        prior_sessions = store.sessions_in_conversation(conversation_id)
        question_with_context = question
        if prior_sessions:
            preface_lines = ["# Prior conversation in this thread"]
            for s in prior_sessions[-3:]:  # last 3 turns to keep cost bounded
                turns = store.session_turns(s.session_id)
                synth = next(
                    (t["content"] for t in turns if t["role"] == "facilitator"),
                    None,
                )
                preface_lines.append(f"\n## {s.started_at[:10]} — {s.topic}\n")
                if synth:
                    preface_lines.append(synth)
            preface_lines.append("\n# Current follow-up\n")
            question_with_context = "\n".join(preface_lines) + question

        async def _store_session_link(line: str) -> None:
            # We need to associate the runner's freshly-created session with this
            # conversation. The runner doesn't know about conversations; we patch
            # the latest session row after start_session has been called inside
            # the orchestrator. Simpler: after task completion, reconcile.
            return None

        task_id = await runner.start(
            user_id=WEB_USER_ID,
            question=question_with_context,
            round2=(round2 == "on"),
            use_profile=(use_profile == "on"),
            on_progress=_store_session_link,
        )
        # Stash the conversation_id so the polling endpoint can wire it up after the
        # consultation completes.
        request.app.state.pending_links = getattr(request.app.state, "pending_links", {})
        request.app.state.pending_links[task_id] = conversation_id
        return _TEMPLATES.TemplateResponse(
            request,
            "_progress_card.html",
            {
                "task_id": task_id,
                "question": question,
                "conversation_id": conversation_id,
                "progress_lines": [],
                "status": "running",
            },
        )

    @app.get("/progress/{task_id}", response_class=HTMLResponse)
    async def progress(request: Request, task_id: str):
        snap = runner.get_progress(task_id)
        if snap is None:
            raise HTTPException(404, "unknown task")

        # When the consultation is done, link its session to the conversation
        # (the runner created the session row but with no conversation_id).
        if snap.status == "done" and snap.result:
            pending = getattr(request.app.state, "pending_links", {})
            conversation_id = pending.pop(task_id, None)
            if conversation_id:
                # SessionRow stored by orchestrator has conversation_id NULL; patch it.
                import sqlite3
                with sqlite3.connect(store.db_path) as conn:
                    conn.execute(
                        "UPDATE sessions SET conversation_id = ? WHERE session_id = ? AND conversation_id IS NULL",
                        (conversation_id, snap.result.session_id),
                    )

        template = (
            "_result_card.html"
            if snap.status in ("done", "failed", "cancelled")
            else "_progress_card.html"
        )
        return _TEMPLATES.TemplateResponse(
            request,
            template,
            {
                "task_id": task_id,
                "status": snap.status,
                "progress_lines": snap.progress_lines,
                "result": snap.result,
                "error": snap.error,
                "conversation_id": getattr(
                    request.app.state, "pending_links", {}
                ).get(task_id),
            },
        )

    @app.post("/cancel/{task_id}", response_class=HTMLResponse)
    async def cancel(request: Request, task_id: str):
        runner.cancel(task_id)
        return HTMLResponse('<div class="muted">Cancelling…</div>')

    @app.get("/c/{conversation_id}", response_class=HTMLResponse)
    async def conversation(request: Request, conversation_id: str):
        conv = store.get_conversation(conversation_id)
        if not conv:
            raise HTTPException(404, "conversation not found")
        sessions = store.sessions_in_conversation(conversation_id)
        # Hydrate each session with its turns.
        rendered = []
        for s in sessions:
            turns = store.session_turns(s.session_id)
            synth = next((t for t in turns if t["role"] == "facilitator"), None)
            experts = [t for t in turns if t["role"] in ("expert_r1", "expert_r2", "expert")]
            rendered.append(
                {
                    "session": s,
                    "synthesis": synth["content"] if synth else None,
                    "experts": experts,
                }
            )
        round2_default = store.get_round2_enabled(WEB_USER_ID)
        return _TEMPLATES.TemplateResponse(
            request,
            "conversation.html",
            {
                "conv": conv,
                "sessions": rendered,
                "round2_default": round2_default,
            },
        )

    @app.post("/c/{conversation_id}/pin", response_class=HTMLResponse)
    async def toggle_pin(request: Request, conversation_id: str):
        conv = store.get_conversation(conversation_id)
        if not conv:
            raise HTTPException(404)
        store.set_conversation_pinned(conversation_id, not conv.pinned)
        return RedirectResponse(f"/c/{conversation_id}", status_code=303)

    @app.post("/c/{conversation_id}/archive", response_class=HTMLResponse)
    async def archive(request: Request, conversation_id: str):
        store.archive_conversation(conversation_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/c/{conversation_id}/title", response_class=HTMLResponse)
    async def rename(conversation_id: str, title: str = Form(...)):
        store.update_conversation_title(conversation_id, title.strip() or "Untitled")
        return HTMLResponse(f'<span class="title-display">{title}</span>')

    @app.get("/profile", response_class=HTMLResponse)
    async def profile_page(request: Request):
        profile = store.get_profile(WEB_USER_ID)
        return _TEMPLATES.TemplateResponse(request, "profile.html", {"profile": profile})

    EDITABLE_LIST_FIELDS = {"goals", "constraints", "open_questions"}

    def _split_lines(raw: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for line in (raw or "").splitlines():
            s = line.strip()
            if s and s not in seen:
                out.append(s)
                seen.add(s)
        return out

    @app.post("/profile/list/{field}", response_class=HTMLResponse)
    async def update_list_field(request: Request, field: str, lines: str = Form("")):
        if field not in EDITABLE_LIST_FIELDS:
            raise HTTPException(400, f"field {field!r} is not editable")
        profile = store.get_profile(WEB_USER_ID)
        setattr(profile, field, _split_lines(lines))
        store.save_profile(profile)
        return _TEMPLATES.TemplateResponse(
            request,
            "_profile_list_section.html",
            {"profile": profile, "field": field, "label": field.replace("_", " ").title()},
        )

    @app.post("/profile/notes", response_class=HTMLResponse)
    async def update_notes(request: Request, notes: str = Form("")):
        profile = store.get_profile(WEB_USER_ID)
        profile.notes = (notes or "").strip()
        store.save_profile(profile)
        return _TEMPLATES.TemplateResponse(
            request, "_profile_notes_section.html", {"profile": profile}
        )

    @app.post("/profile/decisions/delete/{idx}", response_class=HTMLResponse)
    async def delete_decision(request: Request, idx: int):
        profile = store.get_profile(WEB_USER_ID)
        if 0 <= idx < len(profile.decisions):
            profile.decisions.pop(idx)
            store.save_profile(profile)
        return _TEMPLATES.TemplateResponse(
            request, "_profile_decisions_section.html", {"profile": profile}
        )

    @app.post("/profile/things-tried/delete/{idx}", response_class=HTMLResponse)
    async def delete_thing_tried(request: Request, idx: int):
        profile = store.get_profile(WEB_USER_ID)
        if 0 <= idx < len(profile.things_tried):
            profile.things_tried.pop(idx)
            store.save_profile(profile)
        return _TEMPLATES.TemplateResponse(
            request, "_profile_things_tried_section.html", {"profile": profile}
        )

    @app.post("/profile/forget", response_class=HTMLResponse)
    async def forget(request: Request):
        store.delete_profile(WEB_USER_ID)
        return RedirectResponse("/profile", status_code=303)

    return app


def run_web() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    uvicorn.run(
        "randy.web.app:create_app",
        host="127.0.0.1",
        port=8000,
        factory=True,
        log_level="info",
    )


if __name__ == "__main__":
    run_web()
