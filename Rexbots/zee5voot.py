# Rexbots - Don't Remove Credit - @RexBots_Official
#
# Zee5 & Voot — dedicated commands for two named streaming platforms.
#
# yt-dlp already ships native "Zee5" and "Voot" extractors, and neither
# domain is in ytdl.py's _EXCLUDED_DOMAINS list, so a bare zee5.com/voot.com
# link already gets picked up automatically by the generic auto-detect
# handler in ytdl.py (Tier 1 there recognises them by name and routes
# exclusively to yt-dlp, same as Twitch/TikTok/Vimeo). These two commands
# just give an explicit, branded entry point for people who'd rather type
# /zee5 or /voot than paste a bare link — and a clear error up front if the
# link isn't actually from that site, instead of falling through generic
# auto-detect's silent per-domain checks.
#
# Both platforms gate most content behind a login — for anything beyond
# free/trailer content you'll need to add that site's cookies via
# /setcookies zee5.com (or voot.com), same as any other site in the
# per-domain cookie store (see cookies_manager.py).

import re
from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Rexbots.ytdl import _show_quality_picker

E_WARN = '<emoji id=5447644880824181073>⚠️</emoji>'

_ZEE5_RE = re.compile(r"https?://(?:www\.)?zee5\.com/\S+", re.IGNORECASE)
_VOOT_RE = re.compile(r"https?://(?:www\.)?voot\.com/\S+", re.IGNORECASE)


@Client.on_message(filters.private & filters.command("zee5"))
async def zee5_cmd(client: Client, message: Message):
    text = message.text.split(None, 1)
    m = _ZEE5_RE.search(text[1]) if len(text) > 1 else None
    if not m:
        return await message.reply_text(
            f"<b>{E_WARN} Usage:</b> <code>/zee5 https://www.zee5.com/...</code>\n\n"
            f"<i>Premium shows need Zee5 login cookies first — see /cookie.</i>",
            parse_mode=enums.ParseMode.HTML
        )
    await _show_quality_picker(client, message, m.group(0))


# --- Bare-link auto-detect --------------------------------------------
# Registered at group=1 (same tier as terabox.py, vk.py, etc.) so a plain
# zee5.com/voot.com link pasted without /zee5 or /voot is caught directly
# here — no need to fall through to ytdl.py's generic Tier-1 detection.
@Client.on_message(
    filters.text & filters.private & filters.regex(_ZEE5_RE) & ~filters.regex(r"^/"),
    group=1,
)
async def zee5_auto_detect(client: Client, message: Message):
    m = _ZEE5_RE.search(message.text)
    if m:
        await _show_quality_picker(client, message, m.group(0))


@Client.on_message(
    filters.text & filters.private & filters.regex(_VOOT_RE) & ~filters.regex(r"^/"),
    group=1,
)
async def voot_auto_detect(client: Client, message: Message):
    m = _VOOT_RE.search(message.text)
    if m:
        await _show_quality_picker(client, message, m.group(0))
async def voot_cmd(client: Client, message: Message):
    text = message.text.split(None, 1)
    m = _VOOT_RE.search(text[1]) if len(text) > 1 else None
    if not m:
        return await message.reply_text(
            f"<b>{E_WARN} Usage:</b> <code>/voot https://www.voot.com/...</code>\n\n"
            f"<i>Premium shows need Voot login cookies first — see /cookie.</i>",
            parse_mode=enums.ParseMode.HTML
        )
    await _show_quality_picker(client, message, m.group(0))
