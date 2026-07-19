# Generic per-domain cookies store.
#
# config.py only ever wired up cookies for three hardcoded sites (YouTube,
# Instagram, Facebook). A lot of "quality options are missing" / "site wants
# a login" problems on OTHER sites are simply because yt-dlp is fetching
# the page logged-out, and the page serves a smaller/lower-quality format
# list (or an entirely different, JS-stub page) to anonymous visitors.
#
# This lets an admin upload a Netscape-format cookies.txt for ANY domain,
# which ytdl.py's _cookies_for() then picks up automatically for every
# link from that domain (and its subdomains) - no code change needed per
# site.
#
# Usage:
#   /cookie                                 — button panel (Add / View / Delete)
#   /setcookies example.com                — then send the cookies.txt file
#   /setcookies example.com  (as a document caption, file attached directly)
#   /listcookies                            — see which domains have cookies set
#   /delcookies example.com                 — remove them

import os
import re
import time
import asyncio
from urllib.parse import urlparse
from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from config import ADMINS

E_CHECK = '<emoji id=5206607081334906820>✔️</emoji>'
E_CROSS = '<emoji id=5210952531676504517>❌</emoji>'
E_INFO  = '<emoji id=5334544901428229844>ℹ️</emoji>'

COOKIES_DIR = "cookies/custom"
os.makedirs(COOKIES_DIR, exist_ok=True)

PANEL_TEXT = (
    "🍪 <b>Cookie Control Panel</b>\n\n"
    "Upload cookies to bypass login walls and age restrictions for virtually "
    "any website, including:\n"
    "• YouTube\n• Instagram\n• TikTok\n• XHamster\n• Twitter / X\n• Zee5\n• Voot\n• Hotstar & more!\n\n"
    "🛠 <b>How to get cookies:</b>\n"
    "1. Install the <b>Cookie-Editor</b> extension in your PC/Mobile browser.\n"
    "2. Go to the website you want to download from (e.g. youtube.com) and log in.\n"
    "3. Click the Cookie-Editor extension button.\n"
    "4. Click <b>Export → Export as Netscape</b> (Format must be Netscape!).\n"
    "5. Paste the copied text into a new text file and save it as <code>cookies.txt</code>.\n\n"
    "Select an option below to manage your cookies:"
)

ADD_TIMEOUT = 60  # seconds

# user_id -> domain, set by /setcookies while waiting for the file to follow
# as the user's next message.
_pending_setcookies: dict[int, str] = {}

# user_id -> {"expires": monotonic deadline, "chat_id": int, "msg_id": int}
# set by the "Add Cookie" button while waiting for the next document.
_pending_panel: dict[int, dict] = {}


def _panel_markup(mode: str = "root") -> InlineKeyboardMarkup:
    if mode == "delete":
        files = sorted(f[:-4] for f in os.listdir(COOKIES_DIR) if f.endswith(".txt"))
        rows = [[InlineKeyboardButton(f"🗑 {d}", callback_data=f"ckpanel:del:{d}")] for d in files]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="ckpanel:root")])
        return InlineKeyboardMarkup(rows)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Cookie", callback_data="ckpanel:add"),
            InlineKeyboardButton("👁 View Cookies", callback_data="ckpanel:view"),
        ],
        [InlineKeyboardButton("🗑 Delete Cookie", callback_data="ckpanel:delmenu")],
    ])


def _detect_domain(path: str) -> str | None:
    """Best-effort parse of a Netscape cookies.txt: returns the shortest
    (most general) domain seen across all cookie lines, e.g. picks
    'youtube.com' over 'accounts.youtube.com'. Handles the '#HttpOnly_'
    prefix some exporters add to httponly cookie lines."""
    domains = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                if line.startswith("#"):
                    if not line.startswith("#HttpOnly_"):
                        continue
                    line = line[len("#HttpOnly_"):]
                cols = line.split("\t")
                if len(cols) < 7:
                    continue
                d = cols[0].strip().lower().lstrip(".")
                if d and "." in d:
                    domains.append(d)
    except Exception:
        return None
    if not domains:
        return None
    domains.sort(key=len)
    return domains[0]


def _sanitize_domain(raw: str) -> str:
    raw = raw.strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/")[0].split(":")[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return re.sub(r"[^a-z0-9.\-]", "", raw)


def _cookie_path(domain: str) -> str:
    return os.path.join(COOKIES_DIR, f"{domain}.txt")


def get_cookies_for_url(url: str) -> str | None:
    """Used by ytdl.py: does this URL's domain (or a parent of it) have a
    custom cookies.txt an admin uploaded via /setcookies? Checks most
    specific to least specific (sub.example.com, then example.com)."""
    try:
        netloc = urlparse(url if "://" in url else f"https://{url}").netloc.lower().split(":")[0]
    except Exception:
        return None
    parts = netloc.split(".")
    for i in range(len(parts) - 1):  # never falls all the way to a bare TLD
        candidate = ".".join(parts[i:])
        path = _cookie_path(candidate)
        if os.path.exists(path):
            return path
    return None


async def _save_cookie_file(message: Message, domain: str):
    dest = _cookie_path(domain)
    try:
        await message.download(file_name=dest)
    except Exception as e:
        return await message.reply_text(
            f"<b>{E_CROSS} Failed to save cookies:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML
        )
    await message.reply_text(
        f"<b>{E_CHECK} Cookies saved for <code>{domain}</code></b>\n"
        f"<i>Links from this domain (and its subdomains) will now use these cookies automatically.</i>",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command("setcookies") & filters.private & filters.user(ADMINS))
async def setcookies_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/setcookies example.com</code>\n"
            f"<i>Then send the Netscape-format cookies.txt file as your next message "
            f"(or attach it directly to this command).</i>",
            parse_mode=enums.ParseMode.HTML
        )
    domain = _sanitize_domain(message.command[1])
    if not domain or "." not in domain:
        return await message.reply_text(f"<b>{E_CROSS} Invalid domain.</b>", parse_mode=enums.ParseMode.HTML)

    if message.document:
        return await _save_cookie_file(message, domain)

    _pending_setcookies[message.from_user.id] = domain
    await message.reply_text(
        f"<b>{E_INFO} Got it.</b> Now send the cookies.txt file for <code>{domain}</code>.",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command("cookie") & filters.private & filters.user(ADMINS))
async def cookie_panel_command(client: Client, message: Message):
    await message.reply_text(PANEL_TEXT, parse_mode=enums.ParseMode.HTML, reply_markup=_panel_markup())


@Client.on_callback_query(filters.regex(r"^ckpanel:") & filters.user(ADMINS))
async def cookie_panel_callback(client: Client, cq: CallbackQuery):
    action = cq.data.split(":", 1)[1]

    if action == "root":
        _pending_panel.pop(cq.from_user.id, None)
        try:
            await cq.message.edit_text(PANEL_TEXT, parse_mode=enums.ParseMode.HTML, reply_markup=_panel_markup())
        except MessageNotModified:
            pass
        return await cq.answer()

    if action == "add":
        _pending_panel[cq.from_user.id] = {
            "expires": time.monotonic() + ADD_TIMEOUT,
            "chat_id": cq.message.chat.id,
            "msg_id": cq.message.id,
        }
        await cq.message.edit_text(
            f"📁 <b>Send me your cookies.txt file now.</b>\n\n"
            f"<i>(Make sure it is in Netscape HTTP Cookie File format)</i>\n\n"
            f"You have {ADD_TIMEOUT} seconds.",
            parse_mode=enums.ParseMode.HTML
        )
        asyncio.create_task(_expire_add_prompt(cq.from_user.id, cq.message.chat.id, cq.message.id))
        return await cq.answer()

    if action == "view":
        files = sorted(f[:-4] for f in os.listdir(COOKIES_DIR) if f.endswith(".txt"))
        text = (f"<b>{E_INFO} Custom cookies set for:</b>\n" + "\n".join(f"• <code>{d}</code>" for d in files)) \
            if files else f"<b>{E_INFO} No custom cookies set.</b>"
        try:
            await cq.message.edit_text(
                text, parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="ckpanel:root")]])
            )
        except MessageNotModified:
            pass
        return await cq.answer()

    if action == "delmenu":
        if not any(f.endswith(".txt") for f in os.listdir(COOKIES_DIR)):
            return await cq.answer("No custom cookies set.", show_alert=True)
        await cq.message.edit_text(PANEL_TEXT, parse_mode=enums.ParseMode.HTML, reply_markup=_panel_markup("delete"))
        return await cq.answer()

    if action.startswith("del:"):
        domain = action.split(":", 1)[1]
        path = _cookie_path(domain)
        if os.path.exists(path):
            os.remove(path)
            await cq.answer(f"Removed cookies for {domain}", show_alert=True)
        else:
            await cq.answer("Already removed.", show_alert=True)
        if any(f.endswith(".txt") for f in os.listdir(COOKIES_DIR)):
            await cq.message.edit_text(PANEL_TEXT, parse_mode=enums.ParseMode.HTML, reply_markup=_panel_markup("delete"))
        else:
            await cq.message.edit_text(PANEL_TEXT, parse_mode=enums.ParseMode.HTML, reply_markup=_panel_markup())
        return


async def _expire_add_prompt(user_id: int, chat_id: int, msg_id: int):
    """After ADD_TIMEOUT seconds, if the user still hasn't sent a file for
    this exact 'Add Cookie' prompt, clear the pending state and let them
    know so a stray document later doesn't silently get treated as cookies."""
    await asyncio.sleep(ADD_TIMEOUT)
    pending = _pending_panel.get(user_id)
    if not pending or pending["msg_id"] != msg_id:
        return  # already resolved (file received) or superseded by a newer prompt
    _pending_panel.pop(user_id, None)
    try:
        from bot import BotInstance  # local import: bot.py imports this module, so avoid a cycle at load time
        await BotInstance.send_message(
            chat_id,
            f"<b>{E_CROSS} Cookie upload timed out.</b> Send <code>/cookie</code> again.",
            parse_mode=enums.ParseMode.HTML,
            reply_to_message_id=msg_id,
        )
    except Exception:
        pass


# group=-1 so this is checked BEFORE rename.py's group=0 catch-all document
# handler. It only ever acts (and only ever calls stop_propagation) when a
# /setcookies OR the "Add Cookie" panel button is actually pending for this
# user - any other document upload passes straight through untouched.
@Client.on_message(filters.private & filters.document, group=-1)
async def setcookies_file_receive(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        return

    # Panel-driven "Add Cookie" flow (auto-detects domain from the file).
    panel_pending = _pending_panel.get(user_id)
    if panel_pending:
        if time.monotonic() > panel_pending["expires"]:
            _pending_panel.pop(user_id, None)
            await message.reply_text(
                f"<b>{E_CROSS} Timed out.</b> Send <code>/cookie</code> again and re-upload.",
                parse_mode=enums.ParseMode.HTML
            )
            message.stop_propagation()
            return
        _pending_panel.pop(user_id, None)

        tmp_path = os.path.join(COOKIES_DIR, f".tmp_{user_id}_{int(time.time())}")
        try:
            await message.download(file_name=tmp_path)
        except Exception as e:
            await message.reply_text(f"<b>{E_CROSS} Failed to save cookies:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)
            message.stop_propagation()
            return

        domain = _detect_domain(tmp_path)
        if not domain:
            os.remove(tmp_path)
            await message.reply_text(
                f"<b>{E_CROSS} Couldn't detect a domain in that file.</b>\n"
                f"<i>Make sure it's a valid Netscape-format cookies.txt, or use "
                f"</i><code>/setcookies example.com</code><i> to set it manually.</i>",
                parse_mode=enums.ParseMode.HTML
            )
            message.stop_propagation()
            return

        os.replace(tmp_path, _cookie_path(domain))
        await message.reply_text(
            f"<b>{E_CHECK} Success!</b>\n\nCookies automatically assigned to domain: <code>{domain}</code>",
            parse_mode=enums.ParseMode.HTML
        )
        message.stop_propagation()
        return

    # Legacy /setcookies example.com flow (domain typed up front).
    domain = _pending_setcookies.pop(user_id, None)
    if not domain:
        return
    await _save_cookie_file(message, domain)
    message.stop_propagation()


@Client.on_message(filters.command("listcookies") & filters.private & filters.user(ADMINS))
async def listcookies_command(client: Client, message: Message):
    files = sorted(f[:-4] for f in os.listdir(COOKIES_DIR) if f.endswith(".txt"))
    if not files:
        return await message.reply_text(f"<b>{E_INFO} No custom cookies set.</b>", parse_mode=enums.ParseMode.HTML)
    text = f"<b>{E_INFO} Custom cookies set for:</b>\n" + "\n".join(f"• <code>{d}</code>" for d in files)
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command(["delcookies", "clearcookies"]) & filters.private & filters.user(ADMINS))
async def delcookies_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(f"<b>{E_INFO} Usage:</b> <code>/delcookies example.com</code>", parse_mode=enums.ParseMode.HTML)
    domain = _sanitize_domain(message.command[1])
    path = _cookie_path(domain)
    if os.path.exists(path):
        os.remove(path)
        await message.reply_text(f"<b>{E_CHECK} Removed cookies for <code>{domain}</code></b>", parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text(f"<b>{E_CROSS} No cookies found for <code>{domain}</code></b>", parse_mode=enums.ParseMode.HTML)
