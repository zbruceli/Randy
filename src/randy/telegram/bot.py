"""Telegram bot — auto-thread chat UX.

Each /ask starts a fresh thread; plain messages continue the chat's active
thread (creating one if none exists); /new starts a stateless thread that
ignores the profile.
"""

import asyncio
import html
import io
import logging
import uuid

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
    BotCommand("ask", "Pose a question — starts a new thread"),
    BotCommand("new", "Stateless question — new thread, ignores profile"),
    BotCommand("threads", "List your pinned threads"),
    BotCommand("here", "Show the current active thread"),
    BotCommand("end", "End the current thread"),
    BotCommand("profile", "What I remember about you"),
    BotCommand("recap", "Your recent sessions"),
    BotCommand("cost", "Today / month / lifetime spend"),
    BotCommand("r2", "Toggle round-2 (forced disagreement)"),
    BotCommand("forget", "Wipe my memory of you"),
    BotCommand("help", "Show help"),
]

from ..config import settings
from ..memory import MemoryStore
from ..orchestrator import ConsultationResult, ConsultationRunner
from ..personas import PERSONAS

logger = logging.getLogger("randy.telegram")

WELCOME = (
    "<b>Randy</b> — your personal advisory committee.\n\n"
    "Three experts (Claude · GPT-5.5 · DeepSeek), one facilitator (Gemini), "
    "synthesized into one verdict.\n\n"
    "<b>How threads work</b>\n"
    "  • <code>/ask</code> — starts a new thread\n"
    "  • plain message — continues the current thread\n"
    "  • <code>/new</code> — new thread, ignores your profile\n"
    "  • <code>/end</code> — leave the current thread\n\n"
    "<b>Browsing</b>\n"
    "  • <code>/threads</code> — your pinned threads\n"
    "  • <code>/here</code> — what thread you're in now\n\n"
    "<b>Memory</b>\n"
    "  • <code>/profile</code> — what I remember about you\n"
    "  • <code>/recap</code> — recent sessions\n"
    "  • <code>/cost</code> — today / month / lifetime spend\n"
    "  • <code>/r2 on|off</code> — toggle a 2nd forced-disagreement round\n"
    "  • <code>/forget</code> — wipe profile (session log kept)\n"
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
        await _safe_reply(update.message, WELCOME, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


def _title_from_question(q: str, n: int = 60) -> str:
    q = (q or "").strip().replace("\n", " ")
    return q if len(q) <= n else q[: n - 1] + "…"


def _result_keyboard(conv_id: str, *, pinned: bool, in_active_thread: bool) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if pinned:
        row.append(InlineKeyboardButton("📍 Unpin", callback_data=f"unpin:{conv_id}"))
    else:
        row.append(InlineKeyboardButton("📌 Pin", callback_data=f"pin:{conv_id}"))
    if in_active_thread:
        row.append(InlineKeyboardButton("🗂 End thread", callback_data=f"end:{conv_id}"))
    return InlineKeyboardMarkup([row])


async def cmd_threads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if not update.message:
        return
    store: MemoryStore = context.bot_data["store"]
    user_id = _user_id_of(update)
    pinned = store.list_conversations(user_id, pinned_only=True)
    if not pinned:
        await _safe_reply(
            update.message,
            "No pinned threads yet.\n\nAfter a session, tap 📌 <b>Pin</b> under the "
            "synthesis to keep a thread.",
            parse_mode="HTML",
        )
        return
    rows = [
        [InlineKeyboardButton(
            f"📌 {c.title[:40]}",
            callback_data=f"switch:{c.conversation_id}",
        )]
        for c in pinned[:12]
    ]
    await _safe_reply(
        update.message,
        "<b>Your pinned threads</b>\n\nTap one to make it the active thread for this chat.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_here(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if not update.message or update.effective_chat is None:
        return
    store: MemoryStore = context.bot_data["store"]
    chat_id = update.effective_chat.id
    active = store.get_active_thread(chat_id)
    if not active:
        await update.message.reply_text(
            "No active thread.\n\nNext message will start one automatically."
        )
        return
    conv = store.get_conversation(active)
    if not conv:
        store.set_active_thread(chat_id, None)
        await update.message.reply_text("Active thread was missing; cleared.")
        return
    sessions = store.sessions_in_conversation(active)
    pin_marker = "📌 " if conv.pinned else ""
    body = (
        f"{pin_marker}<b>{html.escape(conv.title)}</b>\n"
        f"<i>{len(sessions)} session{'s' if len(sessions) != 1 else ''} · "
        f"started {conv.created_at[:10]}</i>"
    )
    kb = _result_keyboard(active, pinned=conv.pinned, in_active_thread=True)
    await _safe_reply(update.message, body, parse_mode="HTML", reply_markup=kb)


async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[str] = context.bot_data["allowed"]
    if not await _gate(update, allowed):
        return
    if not update.message or update.effective_chat is None:
        return
    store: MemoryStore = context.bot_data["store"]
    chat_id = update.effective_chat.id
    active = store.get_active_thread(chat_id)
    if not active:
        await update.message.reply_text("No active thread to end.")
        return
    store.set_active_thread(chat_id, None)
    await update.message.reply_text("Thread ended. Next message starts fresh.")


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
    if result.research and not result.research.is_empty():
        parts.append("## Research brief")
        parts.append("")
        parts.append(result.research.markdown)
        parts.append("")
        if result.research.sources:
            parts.append("**Sources:**")
            for s in result.research.sources:
                parts.append(f"- [{s.title}]({s.url})")
            parts.append("")
        if result.research.market_snapshots:
            parts.append("**Market data:**")
            for m in result.research.market_snapshots:
                parts.append(f"- {m.summary}")
            parts.append("")
        parts.append("---")
        parts.append("")
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
    *,
    command: str = "plain",
) -> None:
    """command:
       'ask'   — always start a new thread; uses profile.
       'new'   — always start a new thread; ignores profile.
       'plain' — continue active thread (auto-create if none); uses profile.
    """
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
    runner: ConsultationRunner = context.bot_data["runner"]
    user_id = _user_id_of(update)

    # Per-chat task_id tracking so the cancel button can map chat → task.
    chat_tasks: dict[int, str] = context.bot_data.setdefault("chat_tasks", {})
    existing = chat_tasks.get(chat.id)
    if existing:
        snap = runner.get_progress(existing)
        if snap and snap.status == "running":
            await msg.reply_text("Already running a consultation here. Cancel it first.")
            return

    # Resolve the conversation_id and use_profile flag based on command.
    if command in ("ask", "new"):
        conversation_id = uuid.uuid4().hex[:12]
        store.create_conversation(conversation_id, user_id, _title_from_question(question))
        store.set_active_thread(chat.id, conversation_id)
        use_profile = command == "ask"
    else:  # plain
        active = store.get_active_thread(chat.id)
        if active and store.get_conversation(active):
            conversation_id = active
        else:
            conversation_id = uuid.uuid4().hex[:12]
            store.create_conversation(
                conversation_id, user_id, _title_from_question(question)
            )
            store.set_active_thread(chat.id, conversation_id)
        use_profile = True

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
    task_id = await runner.start(
        user_id=user_id,
        question=question,
        round2=round2,
        use_profile=use_profile,
        conversation_id=conversation_id,
        on_progress=on_progress,
    )
    chat_tasks[chat.id] = task_id
    try:
        result = await runner.wait(task_id)
    except asyncio.CancelledError:
        logger.info("consultation cancelled for chat=%s task=%s", chat.id, task_id)
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
        chat_tasks.pop(chat.id, None)
        try:
            await progress_msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    conv = store.get_conversation(conversation_id)
    is_pinned = bool(conv and conv.pinned)
    is_active = store.get_active_thread(chat.id) == conversation_id
    result_kb = _result_keyboard(conversation_id, pinned=is_pinned, in_active_thread=is_active)

    research_bits = ""
    if result.research and not result.research.is_empty():
        rsrc_count = len(result.research.sources) + len(result.research.market_snapshots)
        if rsrc_count:
            research_bits = f" · 🔎 {rsrc_count} source{'s' if rsrc_count != 1 else ''}"
    footer = (
        f"\n\n_session {result.session_id} · ${result.total_cost_usd:.4f} total"
        + research_bits
        + (f" · {len(result.failures)} failure(s)" if result.failures else "")
        + "_"
    )
    body = result.synthesis + footer
    if len(body) <= 4096:
        await _safe_reply(msg, body, parse_mode="Markdown", reply_markup=result_kb)
    else:
        for i in range(0, len(result.synthesis), 3800):
            await _safe_reply(msg, result.synthesis[i : i + 3800])
        await _safe_reply(msg, footer.strip(), parse_mode="Markdown", reply_markup=result_kb)

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
    await _consult(update, context, question, command="ask")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(context.args or [])
    await _consult(update, context, question, command="new")


async def on_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Cancelling…")
    chat_id = query.message.chat.id if query.message else None
    if chat_id is None:
        return
    runner: ConsultationRunner = context.bot_data["runner"]
    chat_tasks: dict[int, str] = context.bot_data.get("chat_tasks", {})
    task_id = chat_tasks.get(chat_id)
    if task_id and runner.cancel(task_id):
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def on_thread_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles pin / unpin / end / switch callbacks on result + /here messages."""
    query = update.callback_query
    if not query or not query.data:
        return
    op, _, conv_id = query.data.partition(":")
    if not conv_id:
        await query.answer()
        return
    store: MemoryStore = context.bot_data["store"]
    chat_id = query.message.chat.id if query.message else None

    if op == "pin":
        store.set_conversation_pinned(conv_id, True)
        await query.answer("Pinned")
    elif op == "unpin":
        store.set_conversation_pinned(conv_id, False)
        await query.answer("Unpinned")
    elif op == "end":
        if chat_id is not None:
            store.set_active_thread(chat_id, None)
        await query.answer("Thread ended")
    elif op == "switch":
        if chat_id is not None:
            store.set_active_thread(chat_id, conv_id)
        conv = store.get_conversation(conv_id)
        title = conv.title[:40] if conv else conv_id
        await query.answer(f"Now in: {title}")
    else:
        await query.answer()
        return

    # Update the keyboard to reflect new state.
    is_active = chat_id is not None and store.get_active_thread(chat_id) == conv_id
    conv = store.get_conversation(conv_id)
    if conv is None:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if op == "end":
        # Remove keyboard since this message is no longer in the active thread.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    new_kb = _result_keyboard(conv_id, pinned=conv.pinned, in_active_thread=is_active)
    try:
        await query.edit_message_reply_markup(reply_markup=new_kb)
    except Exception:
        pass


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await _consult(update, context, update.message.text, command="plain")


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
    store = MemoryStore(settings.db_path)
    app.bot_data["store"] = store
    app.bot_data["runner"] = ConsultationRunner(store)

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
    app.add_handler(CommandHandler("threads", cmd_threads))
    app.add_handler(CommandHandler("here", cmd_here))
    app.add_handler(CommandHandler("end", cmd_end))
    app.add_handler(CallbackQueryHandler(on_cancel_callback, pattern=r"^cancel:"))
    app.add_handler(
        CallbackQueryHandler(on_thread_callback, pattern=r"^(pin|unpin|end|switch):")
    )
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
