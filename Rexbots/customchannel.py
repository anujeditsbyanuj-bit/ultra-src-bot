# Rexbots
# Custom Channel — lets a user redirect every file the bot delivers them
# into a channel/group of their own (in addition to their private chat).
#
# Storage is shared with the existing /setchat "dump chat" field
# (database.db.set_dump_chat / get_dump_chat) so both commands and the
# Settings menu always agree on the same value. The actual forwarding
# happens in Rexbots/direct_utils.py -> _forward_to_custom_channel(),
# which runs once per delivered file.
#
# Don't Remove Credit
# Telegram Channel @RexBots_Official

from pyrogram import Client, filters, enums
from pyrogram.types import Message
from database.db import db
from logger import LOGGER

logger = LOGGER(__name__)

E_CHECK = '<emoji id=5206607081334906820>✔️</emoji>'
E_CROSS = '<emoji id=5210952531676504517>❌</emoji>'
E_INFO  = '<emoji id=5334544901428229844>ℹ️</emoji>'
E_WARN  = '<emoji id=5447644880824181073>⚠️</emoji>'

USAGE_TXT = (
    f"<blockquote>{E_WARN} <b>Usage:</b> <code>/set_channel_id -100xxxxxxxxxx</code>\n"
    f"<b>Example:</b> <code>/set_channel_id -1001234567890</code>\n\n"
    f"<b>Note:</b> The ID must start with <code>-100</code> and you must make me "
    f"an admin in that channel/group. Get your Channel ID by forwarding any "
    f"message from the channel to @MissRose_bot.</blockquote>"
)

NOT_SET_TXT = (
    f"<blockquote>{E_CROSS} <b>No custom channel set!</b>\n\n"
    f"You don't have any custom channel/group configured.\n"
    f"To set one: /set_channel_id</blockquote>"
)


@Client.on_message(filters.command(["set_channel_id", "set_channel"]) & filters.private)
async def set_channel_command(client: Client, message: Message):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    if len(message.command) < 2:
        return await message.reply_text(USAGE_TXT, parse_mode=enums.ParseMode.HTML)

    raw = message.command[1].strip()
    try:
        chat_id = int(raw)
    except ValueError:
        return await message.reply_text(
            f"<b>{E_CROSS} Invalid Channel ID</b>\n\n{USAGE_TXT}",
            parse_mode=enums.ParseMode.HTML,
        )

    if not raw.startswith("-100"):
        return await message.reply_text(
            f"<b>{E_CROSS} Invalid Channel ID</b>\n\n{USAGE_TXT}",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        chat = await client.get_chat(chat_id)
    except Exception as e:
        logger.debug(f"set_channel_id: get_chat({chat_id}) failed: {e}")
        return await message.reply_text(
            f"<b>{E_CROSS} Unable to Access That Chat</b>\n\n"
            f"<i>Make sure the bot has already been added to the channel/group "
            f"before linking it.</i>\n\n{USAGE_TXT}",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        await client.send_message(
            chat_id,
            f"{E_CHECK} <b>Channel Linked</b>\n"
            f"This channel/group has been set by {message.from_user.mention} "
            f"to receive downloaded files.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        logger.debug(f"set_channel_id: test send to {chat_id} failed: {e}")
        return await message.reply_text(
            f"<b>{E_CROSS} Bot Can't Post There</b>\n\n"
            f"<i>Please make me an admin (with permission to post messages) "
            f"in that channel/group, then try again.</i>\n\n{USAGE_TXT}",
            parse_mode=enums.ParseMode.HTML,
        )

    await db.set_dump_chat(user_id, chat_id)

    channel_title = chat.title or "Private Chat"
    username_line = f" @{chat.username}" if getattr(chat, "username", None) else ""

    await message.reply_text(
        f"<blockquote>{E_CHECK} <b>Custom Channel Set Successfully!</b>\n\n"
        f"<b>Channel Name:</b> {channel_title}{username_line}\n"
        f"<b>Channel ID:</b> <code>{chat_id}</code>\n\n"
        f"All downloaded files will now be sent to this channel/group.\n\n"
        f"<b>Note:</b> Files won't be auto-deleted from channel/group.\n\n"
        f"To remove: /del_channel_id</blockquote>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("del_channel_id") & filters.private)
async def del_channel_command(client: Client, message: Message):
    user_id = message.from_user.id
    current = await db.get_dump_chat(user_id)

    if not current:
        return await message.reply_text(NOT_SET_TXT, parse_mode=enums.ParseMode.HTML)

    await db.set_dump_chat(user_id, None)
    await message.reply_text(
        f"<blockquote>{E_CHECK} <b>Custom Channel Removed</b>\n\n"
        f"{E_WARN} <i>Downloaded files will no longer be sent to that "
        f"channel/group. Files already delivered there were not deleted.</i></blockquote>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command(["channel_id", "get_channel_id", "channel_status"]) & filters.private)
async def channel_status_command(client: Client, message: Message):
    """Shows the currently configured custom channel, or the NOT_SET_TXT
    prompt if none has been linked yet."""
    user_id = message.from_user.id
    current = await db.get_dump_chat(user_id)

    if not current:
        return await message.reply_text(NOT_SET_TXT, parse_mode=enums.ParseMode.HTML)

    try:
        chat = await client.get_chat(current)
        channel_title = chat.title or "Private Chat"
        username_line = f" @{chat.username}" if getattr(chat, "username", None) else ""
    except Exception:
        channel_title, username_line = "Unknown", ""

    await message.reply_text(
        f"<blockquote>{E_INFO} <b>Custom Channel</b>\n\n"
        f"<b>Channel Name:</b> {channel_title}{username_line}\n"
        f"<b>Channel ID:</b> <code>{current}</code>\n\n"
        f"To remove: /del_channel_id</blockquote>",
        parse_mode=enums.ParseMode.HTML,
    )
