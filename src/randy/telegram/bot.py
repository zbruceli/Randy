"""Telegram bot — Phase 3 (multi-expert + memory).

Each /ask convenes the full committee and persists the session. The user's
profile is loaded into the brief, and updated asynchronously after synthesis.
"""

import asyncio
import io
import logging

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

COMMAND_MENU = [
    BotCommand("ask", "Pose a question (uses your profile)"),
    BotCommand("new", "Pose a stateless question (ignores profile)"),
    BotCommand("profile", "What I remember about you"),
    BotCommand("recap", "Your recent sessions"),
    BotCommand("cost", "Today / month / lifetime spend"),
    BotCommand("r2", "Toggle round-2 (forced disagreement)"),
    BotCommand("forget", "Wipe my memory of you"),
    BotCommand("help", "Show help"),
]

from ..config import settings
from ..memory import MemoryStore
from ..orchestrator import ConsultationResult, run_consultation
from ..personas import PERSONAS

logger = logging.getLogger("randy.telegram")

WELCOME = (
    "Hi — I'm Randy, your personal advisory committee.\n\n"
    "Ask /ask <question> or just send a message. I convene three experts:\n"
    "  • The Strategist (Claude)\n"
    "  • The Contrarian (GPT-5.5)\n"
    "  • The Operator (DeepSeek)\n"
    "and synthesize with the Facilitator (Gemini).\n\n"
    "I'll remember durable things you tell me — goals, decisions, what you've tried — "
    "so future sessions get sharper. See /profile.\n\n"
    "Commands (also available via the / menu):\n"
    "  /ask <question> — pose a question (uses your profile)\n"
    "  /new <question> — stateless question (ignores profile, won't update it)\n"
    "  /profile — what I remember about you\n"
    "  /recap — your recent sessions\n"
    "  /cost — today / this month / lifetime spend\n"
    "  /r2 [on|off] — enable a 2nd 'forced-disagreement' round (slower, ~3× cost)\n"
    "  /forget — wipe my memory of you (sessions stay logged)\n"
    "  /help — this message"
)


def _parse_allowed(raw: str) -> set[str]:
    return {u.strip().lstrip("@").lower() for u in raw.split(",") if u.strip()}


def _is_allowed(update: Update, allowed: set[str]) -> bool:
    if not allowed:
        return True
    user = update.effective_user
    if user is None:
        return False
    if str(user.id) in allowed:
        return True
    if user.username and user.username.lower() in allowed:
        return True
    return False


async def _gate(update: Update, allowed: set[str]) -> bool:
    if _is_allowed(update, allowed):
        return True
    user = update.effective_user
    logger.warning(
        "Rejected user id=%s username=%s",
        getattr(user, "id", None),
        getattr(user, "username", None),
    )
    if update.message:
        await update.message.reply_text("Sorry, this bot is private.")
    return False


async def _safe_reply(msg, text: str, **kwargs) -> None:
    """reply_text with a fallback when Markdown parsing fails on Telegram's side.

    Gemini occasionally emits unmatched `*`/`_`/`` ` `` that the legacy Markdown
    parser rejects with BadRequest. We strip parse_mode and retry plain.
    """
    try:
        await msg.reply_text(text, **kwargs)
    except BadRequest as e:
        if "parse" in str(e).lower() or "entit" in str(e).lower():
            kwargs.pop("parse_mode", None)
            await msg.reply_text(text, **kwargs)
        else:
            raise


def _user_id_of(update: Update) -> str:
    user = update.effective_user
    if user is None:
        raise RuntimeError("no effective_user on update")
    return str(user.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if update.message:
        await update.message.reply_text(WELCOME)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if not update.message:
        return
    store: MemoryStore = context.bot_data["store"]
    user_id = _user_id_of(update)
    profile = store.get_profile(user_id)
    body = "**Your profile**\n\n" + profile.render_markdown()
    if profile.updated_at:
        body += f"\n\n_Last updated {profile.updated_at}_"
    await _safe_reply(update.message, body, parse_mode="Markdown")


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if not update.message:
        return
    store: MemoryStore = context.bot_data["store"]
    user_id = _user_id_of(update)
    s = store.cost_summary(user_id)
    cap_session = settings.session_cost_cap_usd
    body = (
        "**Spend**\n"
        f"  Today: ${s['today_cost']:.4f} ({s['today_n']} session{'s' if s['today_n'] != 1 else ''})\n"
        f"  This month: ${s['month_cost']:.4f} ({s['month_n']} sessions)\n"
        f"  Lifetime: ${s['life_cost']:.4f} ({s['life_n']} sessions)\n"
        f"  Last session: ${s['last_cost']:.4f}\n\n"
        f"_Per-session cap: ${cap_session:.0f}_"
    )
    await _safe_reply(update.message, body, parse_mode="Markdown")


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if not update.message:
        return
    store: MemoryStore = context.bot_data["store"]
    user_id = _user_id_of(update)
    sessions = store.recent_sessions(user_id, limit=10)
    if not sessions:
        await update.message.reply_text("No sessions yet.")
        return
    lines = ["**Recent sessions**", ""]
    for s in sessions:
        when = s.started_at.split("T")[0]
        topic = (s.topic or "").strip().replace("\n", " ")
        if len(topic) > 80:
            topic = topic[:77] + "…"
        lines.append(f"- `{s.session_id}` · {when} · ${s.cost_usd:.3f} · {topic}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_r2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if not update.message:
        return
    store: MemoryStore = context.bot_data["store"]
    user_id = _user_id_of(update)
    args = [a.lower() for a in (context.args or [])]

    if not args:
        current = store.get_round2_enabled(user_id)
        await _safe_reply(
            update.message,
            f"Round 2 is *{'on' if current else 'off'}*.\n\n"
            "When on: each expert sees the others' round-1 drafts, critiques them, and "
            "revises. Sharper output, ~3× cost, ~2-3× latency.\n\n"
            "Toggle with `/r2 on` or `/r2 off`.",
            parse_mode="Markdown",
        )
        return

    arg = args[0]
    truthy = {"on", "yes", "true", "1", "enable", "enabled"}
    falsy = {"off", "no", "false", "0", "disable", "disabled"}
    if arg in truthy:
        store.set_round2_enabled(user_id, True)
        await _safe_reply(update.message, "Round 2 *on*. Expect longer, sharper sessions.", parse_mode="Markdown")
    elif arg in falsy:
        store.set_round2_enabled(user_id, False)
        await _safe_reply(update.message, "Round 2 *off*. Single round only.", parse_mode="Markdown")
    else:
        await _safe_reply(update.message, "Use `/r2 on` or `/r2 off`.", parse_mode="Markdown")


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if not update.message:
        return
    store: MemoryStore = context.bot_data["store"]
    user_id = _user_id_of(update)
    store.delete_profile(user_id)
    await update.message.reply_text(
        "Profile wiped. Session logs are kept (delete `randy.sqlite` if you want everything gone)."
    )


def _format_drafts_attachment(result: ConsultationResult) -> bytes:
    parts = [
        f"# Randy consultation — session {result.session_id}",
        "",
        f"**Question:** {result.question}",
        f"**Rounds run:** {result.rounds_run}",
        "",
        "---",
        "",
        "## Synthesis (Facilitator)",
        "",
        result.synthesis,
        "",
        "---",
        "",
    ]
    for key in ("strategist", "contrarian", "operator"):
        if key not in result.expert_reports_r1 and key not in result.expert_reports_r2:
            continue
        parts.append(f"## {PERSONAS[key].title}")
        parts.append("")
        if key in result.expert_reports_r1:
            parts.append("### Round 1 (independent)")
            parts.append("")
            parts.append(result.expert_reports_r1[key])
            parts.append("")
        if key in result.expert_reports_r2:
            parts.append("### Round 2 (after critique)")
            parts.append("")
            parts.append(result.expert_reports_r2[key])
            parts.append("")
        parts.append("---")
        parts.append("")
    if result.failures:
        parts.append("## Failures")
        for k, v in result.failures.items():
            parts.append(f"- **{k}**: {v}")
        parts.append("")
    parts.append(
        f"_Cost: ${result.total_cost_usd:.4f} · "
        + " · ".join(f"{k}=${v:.4f}" for k, v in result.expert_costs.items())
        + (f" · facilitator=${result.facilitator_cost:.4f}" if result.facilitator_cost else "")
        + "_"
    )
    return "\n".join(parts).encode("utf-8")


async def _consult(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    question: str,
    use_profile: bool = True,
) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if not question.strip():
        if update.message:
            await update.message.reply_text("Send a question after /ask, or just message me directly.")
        return

    chat = update.effective_chat
    msg = update.message
    if not msg or not chat:
        return

    store: MemoryStore = context.bot_data["store"]
    user_id = _user_id_of(update)

    # Per-chat task tracking lets the cancel button find the running task.
    tasks: dict[int, asyncio.Task] = context.bot_data.setdefault("tasks", {})
    if chat.id in tasks and not tasks[chat.id].done():
        await msg.reply_text("Already running a consultation here. Cancel it first.")
        return

    progress_lines: list[str] = []
    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏹ Cancel", callback_data=f"cancel:{chat.id}")]]
    )
    progress_msg = await msg.reply_text("Working…", reply_markup=cancel_kb)

    # Telegram throttles message edits to ~1/sec/chat; bursts trigger 429
    # RetryAfter and PTB sleeps silently, blocking whoever holds the edit.
    # A lock + min-interval debounce keeps us under the limit and prevents
    # edits from queuing up behind R1's parallel completion bursts.
    edit_lock = asyncio.Lock()
    last_edit_at = 0.0
    MIN_EDIT_INTERVAL = 1.2

    async def on_progress(line: str) -> None:
        nonlocal last_edit_at
        progress_lines.append(line)
        now = asyncio.get_event_loop().time()
        if now - last_edit_at < MIN_EDIT_INTERVAL:
            return  # throttle — internal state still grows; next edit picks up the latest
        if edit_lock.locked():
            return  # an edit is already in flight; skip this one
        async with edit_lock:
            try:
                text = "\n".join(progress_lines[-12:]) + "\n⠀"
                await progress_msg.edit_text(text, reply_markup=cancel_kb)
                last_edit_at = asyncio.get_event_loop().time()
            except Exception:
                pass
            try:
                await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
            except Exception:
                pass

    round2 = store.get_round2_enabled(user_id)
    task = asyncio.create_task(
        run_consultation(
            user_id,
            question,
            store=store,
            on_progress=on_progress,
            round2=round2,
            use_profile=use_profile,
        )
    )
    tasks[chat.id] = task
    try:
        result = await task
    except asyncio.CancelledError:
        logger.info("consultation cancelled for chat=%s", chat.id)
        try:
            await progress_msg.edit_text("⏹ Cancelled.")
        except Exception:
            pass
        await msg.reply_text(
            "Cancelled. Any expert calls already returned were billed and logged."
        )
        return
    except Exception as e:
        logger.exception("orchestrator failed")
        await msg.reply_text(f"Sorry, the committee errored: {type(e).__name__}: {e}")
        return
    finally:
        tasks.pop(chat.id, None)
        # Strip the cancel button now that work is done (or failed).
        try:
            await progress_msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    footer = (
        f"\n\n_session {result.session_id} · ${result.total_cost_usd:.4f} total"
        + (f" · {len(result.failures)} failure(s)" if result.failures else "")
        + "_"
    )
    body = result.synthesis + footer
    if len(body) <= 4096:
        await _safe_reply(msg, body, parse_mode="Markdown")
    else:
        for i in range(0, len(result.synthesis), 3800):
            await _safe_reply(msg, result.synthesis[i : i + 3800])
        await _safe_reply(msg, footer.strip(), parse_mode="Markdown")

    if result.expert_reports:
        attachment = _format_drafts_attachment(result)
        bio = io.BytesIO(attachment)
        bio.name = f"randy-{result.session_id}.md"
        await context.bot.send_document(
            chat.id,
            document=bio,
            filename=bio.name,
            caption="Full expert drafts",
        )


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(context.args or [])
    await _consult(update, context, question, use_profile=True)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(context.args or [])
    await _consult(update, context, question, use_profile=False)


async def on_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Cancelling…")
    chat_id = query.message.chat.id if query.message else None
    if chat_id is None:
        return
    tasks: dict[int, asyncio.Task] = context.bot_data.get("tasks", {})
    task = tasks.get(chat_id)
    if task and not task.done():
        task.cancel()
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await _consult(update, context, update.message.text)


async def run_bot() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    # concurrent_updates lets the cancel-button callback run while a
    # consultation handler is still awaiting its task. Without this, all
    # updates serialize and the cancel button fires only after the work it
    # was meant to interrupt has already finished.
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    app.bot_data["allowed"] = _parse_allowed(settings.telegram_allowed_user_ids)
    app.bot_data["store"] = MemoryStore(settings.db_path)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("recap", cmd_recap))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("r2", cmd_r2))
    app.add_handler(CommandHandler("round2", cmd_r2))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CallbackQueryHandler(on_cancel_callback, pattern=r"^cancel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    me = await app.bot.get_me()
    await app.bot.set_my_commands(COMMAND_MENU)
    logger.info(
        "Bot @%s (id=%s) ready. Allowlist=%s. DB=%s. Slash menu registered (%d cmds).",
        me.username,
        me.id,
        sorted(app.bot_data["allowed"]) or "(open)",
        settings.db_path,
        len(COMMAND_MENU),
    )

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(run_bot())
