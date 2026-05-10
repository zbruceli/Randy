# Randy

A personal multi-LLM advisory committee delivered over Telegram **and** a local web dashboard. Three "experts" from different vendors, each with a distinct persona, plus a facilitator that runs the meeting and synthesizes the verdict. Modeled on RAND Corp; scoped for one person's career and startup planning.

The original idea is in [`Randy.md`](Randy.md). Architecture in [`ARCHITECTURE.md`](ARCHITECTURE.md). Phased plan and status in [`PROJECT_PLAN.md`](PROJECT_PLAN.md).

## What it does

Two delivery channels, one orchestrator:

- **Telegram bot** — fast capture-and-go. DM a question, get a synthesized recommendation back in ~30-90s, with the full expert drafts as a `.md` attachment.
- **Web dashboard** (`127.0.0.1:8000`) — for review and threading. Pinned conversations, follow-ups that auto-include prior synthesis, an editable profile, and a spend rail. HTMX-powered, server-rendered, no JS framework.

Both share the same SQLite database, the same `ConsultationRunner`, and the same persona/orchestrator stack. A session started in Telegram is visible in the web app, and vice versa.

After each session, Randy auto-extracts durable goals, decisions, and constraints into a profile that grounds future answers.

**Research grounding.** Before round 1, a Researcher (Gemini Flash + Brave Search + URL fetch + yfinance) extracts entities from the question, gathers verified facts, and injects them into every persona's brief. All three experts argue from the same ground truth instead of hallucinating company numbers, dates, or quotes. Time-boxed (default 30s); facts persist to a `facts` table and `data/research/<session>/` for browse + reuse.

The committee:

| Role | Vendor / model | Job |
|---|---|---|
| **The Strategist** | Anthropic — `claude-sonnet-4-6` | Frame the problem, map options, name trade-offs |
| **The Contrarian** | OpenAI — `gpt-5.5` | Find the load-bearing assumption, red-team the plan |
| **The Operator** | DeepSeek — `deepseek-v4-pro` | Compress strategy into Monday's actions |
| **The Facilitator** | Google — `gemini-3-pro-preview` | Run the meeting, synthesize, update the profile |

Personas are defined as full system prompts in `src/randy/personas/prompts/`. They're explicit about what each role is *not*, to avoid convergence.

## Web dashboard

Run alongside the bot (it's a separate process — they share the SQLite DB in WAL mode):

```bash
python -m randy.web   # listens on 127.0.0.1:8000
```

Pages:
- `/` — ask form + pinned conversations + recent threads + spend rail.
- `/c/<id>` — conversation thread view; follow-up form prepends prior synthesis automatically. Pin / archive / rename. Per-session research panel shows the verified facts the experts saw.
- `/facts` — browse all researched facts by topic; click a topic for the full claim list with sources, volatility, and confidence tags.
- `/profile` — inline-editable profile (save on blur for goals, constraints, open questions, notes; ✕ buttons remove auto-populated decisions or things-tried entries).

In-flight consultations show a progress card that polls `/progress/<task_id>` every 2s and swaps in the result when done. The same ⏹ Cancel button as the Telegram bot.

## Telegram commands

Registered with Telegram so the autocomplete menu pops on `/`. Threads are **automatic** — `/ask` always starts a fresh thread, plain text continues the active one (auto-creating if none), `/new` starts a stateless thread that ignores the profile.

Threads:
- `/ask <question>` — pose a question, *starts a new thread*
- `/new <question>` — stateless question; new thread, ignores profile, won't update it
- `/threads` — list pinned threads as buttons; tap to make active
- `/here` — show the current active thread
- `/end` — leave the current thread; next message starts fresh

Memory:
- `/profile` — what Randy remembers about you
- `/recap` — recent sessions + topics + cost
- `/cost` — today / this month / lifetime spend
- `/r2 [on|off]` — toggle round 2 (forced disagreement; ~3× cost, 2-3× latency)
- `/forget` — wipe profile (session log retained)
- `/help` — show help

After every consultation the synthesis carries inline buttons: **📌 Pin** to keep the thread, **🗂 End thread** to leave it. Pinning sticks the thread to the top of `/threads` and the web home page.

## Quickstart

Prerequisites: Python 3.11+, API keys for Anthropic, OpenAI, Google AI Studio, DeepSeek, and a Telegram bot token from `@BotFather`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env  # fill in keys + telegram token
pytest                # unit tests, no network
python scripts/smoke.py   # one tiny call per vendor; verifies API + cost
python -m randy           # start the bot
```

In Telegram, find your bot, send `/start`, then ask anything.

### Restricting the bot to yourself

Set `TELEGRAM_ALLOWED_USER_IDS` in `.env` to a comma-separated list of numeric user IDs or `@usernames` (case-insensitive). Empty = open to anyone — only safe with a private bot token.

### Cost caps

`SESSION_COST_CAP_USD` and `PER_MODEL_COST_CAP_USD` (in `.env`) hard-stop a runaway consultation. Defaults: `$25` per session, `$2` per model per session.

## Layout

```
src/randy/
  config.py              env-driven settings
  providers/             vendor adapters + cost meter + price table
    base.py
    anthropic_provider.py
    openai_provider.py   chat + responses API paths
    google_provider.py
    deepseek_provider.py (subclasses OpenAI, /v1 base override)
    cost_meter.py        per-model + per-session hard caps
    pricing.py           $/MTok per model + cache-aware pricing
  personas/              persona registry + Markdown prompts
    prompts/
      strategist.md
      contrarian.md
      operator.md
      facilitator.md
  experts/expert.py      persona + provider wrapper; handles R2 prompt
  orchestrator/
    pipeline.py          channel-agnostic: R1 parallel → optional R2 → synth
    runner.py            ConsultationRunner: tasks, progress, cancel — used by all channels
    profile_updater.py   async post-session profile extraction
  memory/
    schema.sql           users / profile / sessions / turns / user_settings / conversations
    store.py             SQLite CRUD + cost summary + idempotent migrations
    profile.py           UserProfile dataclass + conservative merge
  telegram/bot.py        Telegram channel: bot + commands + slash menu
  web/                   Web channel: FastAPI + Jinja + HTMX
    app.py               routes + handlers
    templates/           home / conversation / profile + HTMX partials
    static/style.css
prompts/                 (placeholders for future tunable templates)
scripts/smoke.py         cross-vendor smoke test
tests/                   pytest (cost meter, pricing, providers, personas)
```

## Notes

- **Local-only**: SQLite at `./randy.sqlite`. Single user-friendly. No vector DB; the long-context Claude/Gemini calls absorb history fine at this scale.
- **Async profile updater** runs after the user gets their answer, so it never adds visible latency.
- **Round 2** is opt-in per user. Stored in `user_settings`. Default off.
- **Reasoning models** (DeepSeek V4 Pro, Gemini 3 Pro) consume thought tokens. Provider adapters fall back to `reasoning_content` when `content` is empty, and we use generous `max_tokens` (3072 for experts) so the visible answer survives.
- **OpenAI Pro models** (`gpt-X.X-pro`) require `/v1/responses`, not `/v1/chat/completions`. The OpenAI adapter routes by model-name heuristic.

## Deploy

For always-on Docker, systemd, or other deployment paths see [`DEPLOY.md`](DEPLOY.md).

Quick docker:

```bash
docker compose up -d --build
```

## Status

See [`PROJECT_PLAN.md`](PROJECT_PLAN.md). All phases shipped through Phase 4 polish. Open: prompt caching, deploy automation, mid-consultation cancel.
