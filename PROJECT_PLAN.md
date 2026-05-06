# Randy — Project Plan

A personal multi-LLM advisory committee delivered via Telegram.

**Status (2026-05-05):** all five MVP phases shipped end-to-end; bot is live and verified against real users with all four vendors. Phase 4 polish landed except prompt caching and deploy automation.

## North Star

A user asks a strategic question (career or startup) over Telegram and receives, within minutes, a synthesized recommendation backed by three distinct expert perspectives — with Randy remembering the user's goals, prior decisions, and follow-ups across sessions.

## Success criteria (MVP)

- One-command deploy; bot responds to `/ask` end-to-end.
- A career or startup question produces 3 distinct expert reports + 1 synthesis.
- Per-session cost ≤ $25, hard-stopped by a cost meter.
- User profile + session log persist across restarts.
- Median end-to-end latency ≤ 90s (with progress pings to mask wait).

## Non-goals (MVP)

Voice, image, web UI, multi-user auth, mobile app, fine-tuning, RAG over external docs, calendar/email integrations.

---

## Phases

### Phase 0 — Foundations ✓
- Repo skeleton, pyproject, env config, `.env.example`.
- Vendor SDKs wired with smoke tests (Anthropic, OpenAI, Google, DeepSeek).
- Cost meter primitive (token counting + per-call $ estimate, hard cap).
- SQLite schema: `users`, `sessions`, `turns`, `profile`.

**Deliverable:** `python -m randy.smoke` calls all four vendors and prints token + $ totals.

### Phase 1 — Single expert + Telegram ✓
- Telegram bot skeleton (`python-telegram-bot`), webhook or long-poll.
- `/ask <question>` → single expert (Claude) → reply.
- Session log persistence.

**Deliverable:** End-to-end Telegram round-trip with one model.

### Phase 2 — Facilitator + parallel experts ✓
- Gemini facilitator: clarify → dispatch → synthesize.
- Three experts called in parallel (`asyncio.gather`).
- Persona prompts (v1, hand-tuned) for each expert.
- Progress pings to Telegram during long calls.

**Deliverable:** `/ask` produces 3 expert reports + facilitator synthesis, under budget.

### Phase 3 — Memory + persistence ✓
- User profile auto-update at session end (Gemini extracts goals, decisions, open questions; conservative merge).
- `/profile`, `/recap`, `/forget` commands.
- SQLite schema: `users`, `profile`, `sessions`, `turns`, `user_settings`.

**Deliverable:** Multi-turn coherence; Randy references prior sessions.

### Round-2 forced disagreement ✓ (opt-in)
- Each surviving expert sees the others' R1 drafts and must (a) critique each by name, (b) revise their position, (c) name what would change their mind.
- R2 drafts replace R1 drafts in the synthesis brief.
- Toggled per user via `/r2 on|off`. Default off (cost / latency).

### Phase 4 — Polish ✓
- ✓ Cost dashboard (`/cost` — today / month / lifetime).
- ✓ Slash-command menu via `set_my_commands` (autocomplete on `/`).
- ✓ Graceful degrade if a vendor fails (orchestrator continues with surviving experts).
- ✓ Reasoning-model handling (DeepSeek V4 Pro, Gemini 3 Pro, GPT-5+ token-param differences).
- ✓ Per-session and per-model hard cost caps.
- ✓ Anthropic prompt caching with cache-aware pricing.
- ✓ Cancel button mid-consultation (inline keyboard + asyncio.Task.cancel).
- ✓ Markdown-safe reply (auto-fallback to plain when Telegram parser rejects).
- ✓ Deploy automation (Dockerfile, docker-compose, DEPLOY.md, systemd snippet).

### Phase 5 — Multi-channel ✓
- ✓ Extract `ConsultationRunner`: tasks + progress + cancel as channel-agnostic primitives. Push channels (Telegram) and pull channels (web HTMX poll) share the same surface.
- ✓ Conversations (threads): new `conversations` table, sessions linked by `conversation_id`, follow-ups auto-include last 3 syntheses in the brief.
- ✓ Web dashboard at `127.0.0.1:8000`: home with pinned + recent + ask form + spend rail, conversation page with thread view + follow-up form, profile editor with HTMX inline-edit on goals/constraints/open-questions/notes.

### Phase 6 — Telegram threading parity ✓
- ✓ `chat_active_thread` table for per-chat persistent thread state.
- ✓ Auto-thread bot logic: `/ask` starts new, plain continues, `/new` stateless+new.
- ✓ Post-result inline keyboard: 📌 Pin / 📍 Unpin / 🗂 End thread, with state-toggling callback.
- ✓ New commands: `/threads` (browse pinned), `/here` (show current), `/end` (leave thread).
- ✓ HTML parse mode for static UI strings (more reliable than legacy Markdown for our copy).

### Open
- Auto-title via Gemini Flash for nicer thread titles (currently first-60-chars truncate).
- Prompt caching beyond Anthropic (OpenAI's auto-cache is implicit; Google explicit context cache).
- Telegram Mini App pointing at the dashboard (deferred — needs public HTTPS hosting).

---

## Architecture

```
┌─────────────┐
│  Telegram   │
└──────┬──────┘
       │
┌──────▼─────────────────────────────────────┐
│  randy.telegram (bot handlers)              │
└──────┬─────────────────────────────────────┘
       │
┌──────▼─────────────────────────────────────┐
│  randy.orchestrator (Facilitator: Gemini)   │
│   1. clarify          4. round 2 (optional) │
│   2. dispatch         5. synthesize         │
│   3. round 1 parallel 6. update profile     │
└──┬─────────┬─────────┬─────────────────────┘
   │         │         │
┌──▼──┐  ┌──▼──┐  ┌──▼──┐
│Opus │  │GPT-5│  │Deep │   randy.experts.*
│Pro  │  │Pro  │  │Seek │
└─────┘  └─────┘  └─────┘
   │         │         │
┌──▼─────────▼─────────▼──┐
│ randy.providers (adapter│   normalizes I/O across vendors
│   layer + cost meter)   │
└──┬──────────────────────┘
   │
┌──▼──────────────────────┐
│ randy.memory (SQLite)   │   profile + session log
└─────────────────────────┘
```

## Key design decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Best LLM SDK ecosystem, async-native |
| Persistence | SQLite + JSON profile | Sufficient at single-user scale; zero ops |
| Async | `asyncio` + `httpx` | Parallel expert calls |
| Telegram lib | `python-telegram-bot` v21+ | Mature, async, well-documented |
| Config | `pydantic-settings` + `.env` | Type-safe, env-driven |
| Test | `pytest` + `pytest-asyncio` | Standard |
| Lint/format | `ruff` | Fast, single tool |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Experts converge to same advice | Strong distinct personas; forced-disagreement step in round 2 |
| Cost overruns | Hard-cap meter; abort orchestration on threshold |
| Vendor outage | Degrade gracefully to N-1 experts |
| Persona drift over long context | Re-inject persona block each turn (cached) |
| Telegram message length limits | Long expert reports as `.md` document attachments |
| Memory bloat / staleness | Profile is summary, not append log; explicit update step |

## Open questions before Phase 2

- Persona definitions (3 experts × 1 paragraph each) — to be drafted.
- Hosting target (local / VPS / Fly.io) — defer until Phase 4.
- Whether discussion round 2 actually beats round 1 — measure, don't assume.
