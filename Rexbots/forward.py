# Rexbots - Don't Remove Credit - @RexBots_Official
#
# Forward tool — ported/simplified from the fwdbot project's core
# source->target forwarding engine (fwdbot's ftm_manager.py / regix.py),
# rebuilt to fit Rexbots as a single bot-token client (fwdbot's clone-bot /
# multi-tenant / subscription-tier machinery is intentionally NOT ported —
# it doesn't apply here).
#
# Uses copy_message (not forward_messages) for every message, same choice
# fwdbot makes, because copy_message re-sends the content instead of
# relaying it, so it also works on chats with forwarding restricted.
#
# USERBOT FALLBACK: the bot token alone can only touch chats it has been
# added to. For a private chat the bot isn't (and can't be) added to,
# /setsource and /settarget fall back to the user's own account — reusing
# the exact session string already stored by Rexbots/session.py's /login
# flow (db.get_session), same pattern start.py already uses for restricted
# saves (Client(session_string=..., in_memory=True)). No new login system
# was built here; if the person hasn't run /login yet, they're told to.
#
# A single forward job needs ONE client that can read the source AND write
# the target, so at launch time we work out whether the bot alone can do
# both, or whether the personal account (userbot) has to do both — mixing
# (bot for one side, userbot for the other) isn't possible for copy_message
# and is reported back to the user as a clear limitation rather than
# silently failing.
#
# Commands:
#   /setsource <chat_id or @username>
#   /settarget <chat_id or @username>
#   /fwd <start_msg_id> <end_msg_id>   — forwards that id range, source->target
#   /fwdresume <end_msg_id>            — continues from where /fwd last left off
#   /fwdstatus                         — show source/target/progress
#   /fwdcancel                         — stop the running forward job

import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import FloodWait, RPCError
from config import API_ID, API_HASH
from database.db import db

from Rexbots.direct_utils import E_CHECK, E_CROSS, E_INFO, E_BOLT, E_ROCKET
from Rexbots import task_manager
from Rexbots import titanium

E_ARROW = '➜'
E_LOCK = '🔒'
MAX_RANGE = 5000          # hard cap per /fwd call, matches fwdbot-style batch limits
DELAY_SECS = 1.2          # gap between copies — keeps well under Bot API flood limits
PROGRESS_EVERY = 15       # edit status message every N messages
SAVE_EVERY = 5            # persist resume-checkpoint every N messages

# user_id -> asyncio.Task, so /fwdcancel can stop just the forward job
# (separate from task_manager's global /cancel_all, though it's registered
# there too for visibility/consistency with the rest of the bot).
_RUNNING = {}


def _parse_chat(raw: str):
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else f"@{raw}"


async def _make_userbot(user_id: int):
    """Spins up a connected Client from the person's stored /login session
    string, or returns None if they haven't logged in / it's expired.
    Caller owns the connection and must disconnect() it when done."""
    session_str = await db.get_session(user_id)
    if not session_str:
        return None
    acc = Client(
        f"fwd_userbot_{user_id}",
        session_string=session_str,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        max_concurrent_transmissions=10,
    )
    try:
        await acc.connect()
        return acc
    except Exception:
        return None


async def _resolve_chat(bot_client: Client, user_id: int, chat_ref):
    """Tries the bot client first, then the person's userbot session.
    Returns (chat, via, userbot_or_None). userbot is left connected if it
    was the one that worked, so the caller can reuse/disconnect it."""
    try:
        chat = await bot_client.get_chat(chat_ref)
        return chat, "bot", None
    except RPCError:
        pass

    acc = await _make_userbot(user_id)
    if acc is None:
        return None, None, None
    try:
        chat = await acc.get_chat(chat_ref)
        return chat, "user", acc
    except RPCError:
        await acc.disconnect()
        return None, None, None




async def _resolve_and_store(client: Client, message: Message, which: str):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/set{which} -1001234567890</code> or <code>/set{which} @channelusername</code>\n"
            f"<i>Tries the bot first; if the bot can't access it, falls back to your /login session.</i>",
            parse_mode=enums.ParseMode.HTML
        )

    chat_ref = _parse_chat(message.command[1])
    chat, via, acc = await _resolve_chat(client, user_id, chat_ref)
    if acc:
        await acc.disconnect()

    if not chat:
        has_session = bool(await db.get_session(user_id))
        hint = (
            "Your login session can't see it either — double check the chat id/username."
            if has_session else
            f"The bot isn't in that chat, and you haven't run /login yet — do that if it's a "
            f"private chat, so your own account can be used instead."
        )
        return await message.reply_text(
            f"<b>{E_CROSS} Can't access that chat.</b> {hint}",
            parse_mode=enums.ParseMode.HTML
        )

    if which == "source":
        await db.set_fwd_source(user_id, chat.id, via)
    else:
        await db.set_fwd_target(user_id, chat.id, via)

    via_note = f" <i>(via {'your account' if via == 'user' else 'the bot'})</i>"
    await message.reply_text(
        f"<b>{E_CHECK} {which.capitalize()} set:</b> {chat.title or chat.first_name or chat.id} "
        f"(<code>{chat.id}</code>){via_note}",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.private & filters.command("setsource"))
async def setsource_cmd(client: Client, message: Message):
    await _resolve_and_store(client, message, "source")


@Client.on_message(filters.private & filters.command("settarget"))
async def settarget_cmd(client: Client, message: Message):
    await _resolve_and_store(client, message, "target")


@Client.on_message(filters.private & filters.command("fwdstatus"))
async def fwdstatus_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    s = await db.get_fwd_settings(user_id)
    running = "🟢 running" if user_id in _RUNNING and not _RUNNING[user_id].done() else "⚪ idle"
    await message.reply_text(
        f"<b>{E_INFO} Forward status</b>\n\n"
        f"<b>Source:</b> <code>{s['source'] or 'not set'}</code> <i>({s['source_via']})</i>\n"
        f"<b>Target:</b> <code>{s['target'] or 'not set'}</code> <i>({s['target_via']})</i>\n"
        f"<b>Last forwarded id:</b> <code>{s['last_id'] or '-'}</code>\n"
        f"<b>Status:</b> {running}",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.private & filters.command("fwdcancel"))
async def fwdcancel_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    task = _RUNNING.get(user_id)
    if not task or task.done():
        return await message.reply_text(f"<b>{E_INFO} No forward job is running.</b>", parse_mode=enums.ParseMode.HTML)
    task.cancel()
    await message.reply_text(f"<b>🚫 Stopping...</b> current message will finish, then it'll halt.", parse_mode=enums.ParseMode.HTML)


async def _pick_job_client(bot_client: Client, user_id: int, source, target, source_via, target_via):
    """Works out ONE client that can both read `source` and write `target`
    for this run. Returns (client, owns_it) — owns_it=True means the caller
    must disconnect() it when the job ends (it's a fresh userbot client);
    False means it's the shared bot client, leave it alone."""
    if source_via == "bot" and target_via == "bot":
        job_client, is_clone, clone_username = await titanium.get_job_client(user_id, bot_client, source, target)
        return job_client, False  # clone bots live in titanium._CLONE_CACHE and are reused — never disconnect here

    # Either side needs the personal account — the same account has to be
    # able to reach both, since copy_message runs on a single client.
    acc = await _make_userbot(user_id)
    if acc is None:
        return None, False
    try:
        await acc.get_chat(source)
        await acc.get_chat(target)
        return acc, True
    except RPCError:
        await acc.disconnect()
        return None, False


async def _forward_loop(job_client: Client, owns_client: bool, message: Message, status: Message, user_id: int, source, target, start_id: int, end_id: int):
    done = skipped = failed = 0
    total = end_id - start_id + 1
    msg_id = start_id
    try:
        while msg_id <= end_id:
            try:
                await job_client.copy_message(chat_id=target, from_chat_id=source, message_id=msg_id)
                done += 1
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                continue  # retry same msg_id
            except RPCError:
                skipped += 1  # deleted / service message / no access to that one message
            except Exception:
                failed += 1

            if msg_id % SAVE_EVERY == 0:
                await db.set_fwd_progress(user_id, msg_id, {"source": source, "target": target})

            if (done + skipped + failed) % PROGRESS_EVERY == 0:
                processed = done + skipped + failed
                pct = processed * 100 // total
                await status.edit_text(
                    f"<b>{E_ROCKET} Forwarding...</b> {pct}%\n"
                    f"<code>{processed}/{total}</code> | ✅ {done} | ⏭ {skipped} | ❌ {failed}\n"
                    f"<i>Currently at id {msg_id}</i>",
                    parse_mode=enums.ParseMode.HTML
                )

            msg_id += 1
            await asyncio.sleep(DELAY_SECS)

        await db.set_fwd_progress(user_id, end_id, {"source": source, "target": target})
        await status.edit_text(
            f"<b>{E_CHECK} Forward complete.</b>\n"
            f"<b>Range:</b> <code>{start_id}-{end_id}</code>\n"
            f"✅ {done} sent | ⏭ {skipped} skipped | ❌ {failed} failed",
            parse_mode=enums.ParseMode.HTML
        )
    except asyncio.CancelledError:
        await db.set_fwd_progress(user_id, msg_id - 1, {"source": source, "target": target})
        await status.edit_text(
            f"<b>🚫 Forward stopped at id {msg_id - 1}.</b>\n"
            f"✅ {done} sent | ⏭ {skipped} skipped | ❌ {failed} failed\n"
            f"<i>Resume anytime with /fwdresume {end_id}</i>",
            parse_mode=enums.ParseMode.HTML
        )
        raise
    finally:
        _RUNNING.pop(user_id, None)
        if owns_client:
            try:
                await job_client.disconnect()
            except Exception:
                pass


async def _launch(client: Client, message: Message, start_id: int, end_id: int):
    user_id = message.from_user.id
    s = await db.get_fwd_settings(user_id)
    if not s["source"] or not s["target"]:
        return await message.reply_text(
            f"<b>{E_INFO} Set both first:</b> <code>/setsource</code> and <code>/settarget</code>.",
            parse_mode=enums.ParseMode.HTML
        )
    if user_id in _RUNNING and not _RUNNING[user_id].done():
        return await message.reply_text(f"<b>{E_INFO} A forward job is already running.</b> Use /fwdcancel to stop it first.", parse_mode=enums.ParseMode.HTML)
    if start_id > end_id:
        return await message.reply_text(f"<b>{E_CROSS} Start id must be ≤ end id.</b>", parse_mode=enums.ParseMode.HTML)
    if end_id - start_id + 1 > MAX_RANGE:
        return await message.reply_text(f"<b>{E_CROSS} Range too big — max {MAX_RANGE} messages per run.</b> Split it up.", parse_mode=enums.ParseMode.HTML)

    status = await message.reply_text(f"<b>{E_BOLT} Starting forward job...</b>", parse_mode=enums.ParseMode.HTML)

    job_client, owns_client = await _pick_job_client(
        client, user_id, s["source"], s["target"], s["source_via"], s["target_via"]
    )
    if job_client is None:
        via_needed = "your /login account" if "user" in (s["source_via"], s["target_via"]) else "the bot"
        return await status.edit_text(
            f"<b>{E_CROSS} Can't run this job.</b> {via_needed} can't reach both the source and "
            f"target chat at once — since source and target were set through different access "
            f"methods, no single connection can do the copy. Re-run /setsource and /settarget so "
            f"both resolve the same way (both via the bot, or both via /login).",
            parse_mode=enums.ParseMode.HTML
        )

    task = asyncio.ensure_future(_forward_loop(job_client, owns_client, message, status, user_id, s["source"], s["target"], start_id, end_id))
    _RUNNING[user_id] = task
    task_id = task_manager.register(user_id, task, f"Forward {start_id}-{end_id}")
    task.add_done_callback(lambda t: task_manager.unregister(user_id, task_id))


@Client.on_message(filters.private & filters.command("fwd"))
async def fwd_cmd(client: Client, message: Message):
    if len(message.command) < 3 or not all(p.lstrip("-").isdigit() for p in message.command[1:3]):
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/fwd &lt;start_msg_id&gt; &lt;end_msg_id&gt;</code>\n"
            f"<i>Message id = the number you see when you copy a message link, e.g. "
            f"t.me/c/12345/<b>678</b> → id is 678.</i>",
            parse_mode=enums.ParseMode.HTML
        )
    await _launch(client, message, int(message.command[1]), int(message.command[2]))


@Client.on_message(filters.private & filters.command("fwdresume"))
async def fwdresume_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    s = await db.get_fwd_settings(user_id)
    if not s["last_id"]:
        return await message.reply_text(f"<b>{E_INFO} Nothing to resume.</b> Use /fwd to start a fresh job.", parse_mode=enums.ParseMode.HTML)
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        return await message.reply_text(f"<b>{E_INFO} Usage:</b> <code>/fwdresume &lt;end_msg_id&gt;</code>", parse_mode=enums.ParseMode.HTML)
    await _launch(client, message, int(s["last_id"]) + 1, int(message.command[1]))
