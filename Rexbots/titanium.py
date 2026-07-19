# Rexbots - Don't Remove Credit - @RexBots_Official
#
# Titanium Clone Mode — ported from fwdbot's plugins/titanium.py, trimmed
# to fit Rexbots. Two things from the original were intentionally NOT
# ported:
#   - Plan-based slot limits (Config.TITANIUM_PLAN_LIMITS) — Rexbots has no
#     subscription-tier system, so this uses one flat MAX_TITANIUM_BOTS cap
#     instead. If Rexbots ever gets tiers, gate this the same way.
#   - Bot API 9.6 "Managed Bots" auto-create (the deep-link "tap Create,
#     zero BotFather" flow) — that needs can_manage_bots enabled on the
#     main bot plus a manager-bot poller (fwdbot's utils/managed_bots.py),
#     neither of which exist here. Connecting is via a normal @BotFather
#     token instead — this was fwdbot's own fallback path when Bot
#     Management Mode isn't set up, so it's a well-tested route either way.
#
# What it does: lets a person connect their own bot(s) so their jobs run
# on a SEPARATE Telegram flood-limit pool instead of sharing the one main
# bot with every other user. Nothing about plan/features changes — it's
# purely extra rate-limit headroom.
#
# CRITICAL SAFETY NOTE (kept from the original): clone Clients below are
# started WITHOUT plugins=dict(root="Rexbots") — no command handlers of
# their own. They're only ever driven programmatically by get_job_client(),
# which is already scoped to the connecting user's own chats/jobs. Do NOT
# attach the plugin root to a clone client — that would turn every
# connected clone into its own fully public, unrestricted copy of the bot.
#
# The clone bot still has to be manually added (as member/admin) to
# whatever chats a job touches, exactly like the main bot — connecting it
# here doesn't grant it access to anything by itself.

import time
from pyrogram import Client, filters, enums
from pyrogram.errors import RPCError
from pyrogram.types import Message
from config import API_ID, API_HASH
from database.db import db
from Rexbots.direct_utils import E_CHECK, E_CROSS, E_INFO

MAX_TITANIUM_BOTS = 5  # flat cap — see module docstring re: no plan system here

_CLONE_CACHE = {}  # token -> connected Client, reused across jobs/messages


async def _get_clone_client(token: str) -> Client:
    cached = _CLONE_CACHE.get(token)
    if cached is not None and cached.is_connected:
        return cached
    client = Client(
        f"titanium_{token[:10]}",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=token,
        in_memory=True,
        max_concurrent_transmissions=10,
        # no plugins= here on purpose — see module docstring
    )
    await client.start()
    _CLONE_CACHE[token] = client
    return client


@Client.on_message(filters.private & filters.command("titanium"))
async def titanium_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    bots = await db.get_titanium_bots(user_id)
    lines = [
        "<b>⚡ Titanium Clone Mode</b>",
        "",
        "Connect your own @BotFather bot(s) so your jobs run on a separate "
        "flood-limit pool instead of sharing the main bot's with everyone else.",
        "",
        f"<b>Connected:</b> {len(bots)}/{MAX_TITANIUM_BOTS}",
    ]
    for b in bots:
        lines.append(f"  • @{b['username']}")
    lines += [
        "",
        "<code>/addbot &lt;token&gt;</code> — connect a bot (get one from @BotFather → /newbot)",
        "<code>/delbot &lt;username&gt;</code> — disconnect one",
        "",
        "<i>Add each clone as admin to whatever chats you use it for — connecting "
        "it here doesn't give it access to anything on its own.</i>",
    ]
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.private & filters.command("addbot"))
async def addbot_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/addbot 123456:ABC-your-bot-token</code>\n"
            f"<i>Create one with @BotFather (/newbot) first, then paste the token here.</i>",
            parse_mode=enums.ParseMode.HTML
        )

    token = message.command[1].strip()
    bots = await db.get_titanium_bots(user_id)
    if len(bots) >= MAX_TITANIUM_BOTS:
        return await message.reply_text(
            f"<b>{E_CROSS} Limit reached</b> ({MAX_TITANIUM_BOTS} bots). Disconnect one with /delbot first.",
            parse_mode=enums.ParseMode.HTML
        )
    if any(b["token"] == token for b in bots):
        return await message.reply_text(f"<b>{E_INFO} That bot is already connected.</b>", parse_mode=enums.ParseMode.HTML)

    status = await message.reply_text(f"<b>{E_INFO} Verifying token...</b>", parse_mode=enums.ParseMode.HTML)
    try:
        test_client = Client(
            f"titanium_verify_{user_id}_{int(time.time())}",
            api_id=API_ID, api_hash=API_HASH, bot_token=token, in_memory=True
        )
        await test_client.start()
        me = await test_client.get_me()
        await test_client.stop()
    except Exception as e:
        return await status.edit_text(f"<b>{E_CROSS} Invalid token:</b> <code>{e}</code>", parse_mode=enums.ParseMode.HTML)

    if any(b["username"] == me.username for b in bots):
        return await status.edit_text(f"<b>{E_INFO} @{me.username} is already connected.</b>", parse_mode=enums.ParseMode.HTML)

    await db.add_titanium_bot(user_id, token, me.username)
    await status.edit_text(
        f"<b>{E_CHECK} Connected @{me.username}.</b>\n"
        f"<i>Add it as admin to your chats — it'll be picked up automatically for jobs that can use it.</i>",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.private & filters.command("delbot"))
async def delbot_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if len(message.command) < 2:
        return await message.reply_text(f"<b>{E_INFO} Usage:</b> <code>/delbot username</code>", parse_mode=enums.ParseMode.HTML)
    username = message.command[1].strip().lstrip("@")
    removed = await db.remove_titanium_bot(user_id, username)
    if not removed:
        return await message.reply_text(f"<b>{E_INFO} No connected bot found with that username.</b>", parse_mode=enums.ParseMode.HTML)
    await message.reply_text(f"<b>{E_CHECK} Disconnected @{username}.</b>", parse_mode=enums.ParseMode.HTML)


async def get_job_client(user_id: int, fallback_client: Client, *chats_to_check):
    """Picks the least-recently-used client — main bot or a connected
    Titanium clone — that can access every chat in chats_to_check. Falls
    back to fallback_client if the person has no clones connected, or if
    none of them (nor the main bot) can reach every chat listed.

    Returns (client, is_clone: bool, username: str|None).

    This is the integration point other plugins call into — currently
    wired into Rexbots/forward.py's job launch. Other long-running plugins
    (ytdl.py, terabox.py, etc.) can call this the same way to get the same
    flood-pool benefit; that wasn't done for all of them in this pass to
    keep the change reviewable.
    """
    bots = await db.get_titanium_bots(user_id)
    if not bots:
        return fallback_client, False, None

    candidates = [("__main__", fallback_client, None)]
    for b in sorted(bots, key=lambda x: x.get("last_used", 0)):
        try:
            clone = await _get_clone_client(b["token"])
            candidates.append((b["token"], clone, b["username"]))
        except Exception:
            continue

    for token, cand_client, username in candidates:
        try:
            for chat in chats_to_check:
                await cand_client.get_chat(chat)
        except RPCError:
            continue
        if token != "__main__":
            await db.touch_titanium_bot(user_id, token)
        return cand_client, token != "__main__", username

    return fallback_client, False, None
