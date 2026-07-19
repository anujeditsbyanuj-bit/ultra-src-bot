# Rexbots
# Spotify music downloader — ported from the standalone Spotify-Music-Bot
# (its spotify_music_bot.py, which scrapes spotidown.app since Spotify's
# own API doesn't serve audio). Supports:
#   - Pasting an open.spotify.com track/playlist/album link — auto-detected,
#     same as every other link-based plugin in this bot
#   - /spotify <link> as the explicit command form
#   - No text search here (spotidown.app only resolves URLs, not queries —
#     use /saavn or /search for name-based search instead)
#
# Don't Remove Credit
# Telegram Channel @RexBots_Official

import os
import re
import json
import base64
import asyncio
import requests
import aiohttp
from bs4 import BeautifulSoup
from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from Rexbots.direct_utils import (
    make_output_folder, safe_filename, make_upload_progress, stream_download,
    E_CHECK, E_CROSS, E_INFO, E_ROCKET, E_BOLT,
)
from Rexbots.link_cache import try_send_cached, store as _cache_store

SPOTI_BASE = "https://spotidown.app"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

# intl-en/, intl-hi/, etc. show up on links shared from the mobile app —
# the original bot's regex didn't account for that locale segment.
SPOTIFY_PATTERN = re.compile(
    r"https?://open\.spotify\.com/(?:intl-\w+/)?(track|playlist|album)/[A-Za-z0-9]+",
    re.IGNORECASE,
)

# session_id -> {"kind": "playlist"|"album", "url": str, "chat_id": int, "message": Message}
_SESSIONS = {}


def _extract_url(text: str):
    m = SPOTIFY_PATTERN.search(text)
    return m.group(0) if m else None


def _spotify_kind(url: str) -> str:
    if "/track/" in url:
        return "track"
    if "/playlist/" in url:
        return "playlist"
    if "/album/" in url:
        return "album"
    return "unknown"


# ------------------------------------------------------------------ scraper
# All of this talks to spotidown.app over plain requests+bs4, exactly like
# the original bot — it's all blocking I/O, so every entry point below is
# only ever called through asyncio.to_thread().

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": SPOTI_BASE + "/en2",
        "X-Requested-With": "XMLHttpRequest",
    })
    r = s.get(SPOTI_BASE + "/en2", timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    hidden = soup.find("input", {"type": "hidden", "name": re.compile(r"^_")})
    s._csrf = {hidden["name"]: hidden["value"]} if hidden else {}
    return s


def _fetch_action(s: requests.Session, spotify_url: str) -> str:
    r = s.post(SPOTI_BASE + "/action", data={
        "url": spotify_url, "g-recaptcha-response": "faketoken", **s._csrf,
    }, timeout=20)
    resp = r.json()
    if resp.get("error"):
        raise RuntimeError(resp.get("message", "unknown error"))
    return resp["data"]


def _parse_forms(html: str):
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form", {"name": "submitspurl"})
    result = []
    for form in forms:
        fields = {inp["name"]: inp.get("value", "") for inp in form.find_all("input") if inp.get("name")}
        result.append(fields)
    img = soup.find("img")
    fallback_thumb = img["src"] if img else None
    return result, fallback_thumb


def _resolve_track(s: requests.Session, form_data: dict, index: int, fallback_thumb=None):
    """Resolves one track's title/artist/thumbnail + a direct download href
    (does NOT download it — that part goes through this bot's own
    stream_download() for a proper progress bar and consistent behaviour
    with every other plugin)."""
    try:
        info = json.loads(base64.b64decode(form_data.get("data", "")).decode())
        title = info.get("name", f"Track {index + 1}")
        artist = info.get("artist", "")
        name = f"{title} - {artist}" if artist else title
        thumb_url = info.get("cover") or info.get("image") or info.get("thumb") or fallback_thumb
    except Exception:
        title, artist, name, thumb_url = f"Track {index + 1}", "", f"Track {index + 1}", fallback_thumb

    r = s.post(SPOTI_BASE + "/action/track", data=form_data, timeout=30)
    resp = r.json()
    if resp.get("error"):
        return name, title, artist, None, thumb_url, resp.get("message")

    soup = BeautifulSoup(resp["data"], "html.parser")
    img = soup.find("img")
    if img and not thumb_url:
        thumb_url = img.get("src")

    href = None
    a = soup.find("a", href=re.compile(r"/dl\?token=|rapid\.spotidown"))
    if a:
        href = a["href"]
        if href.startswith("/"):
            href = SPOTI_BASE + href
    else:
        a = soup.find("a", href=re.compile(r"https?://"))
        if a:
            href = a["href"]

    if not href:
        return name, title, artist, None, thumb_url, "no download link found"
    return name, title, artist, href, thumb_url, None


def _spotify_get_track_blocking(spotify_url: str):
    s = _make_session()
    html = _fetch_action(s, spotify_url)
    forms, fallback_thumb = _parse_forms(html)
    if not forms:
        raise RuntimeError("no track found")
    name, title, artist, href, thumb_url, err = _resolve_track(s, forms[0], 0, fallback_thumb)
    if err:
        raise RuntimeError(err)
    return name, title, artist, href, thumb_url


def _spotify_get_set_blocking(spotify_url: str):
    """Playlist/album: resolves every track sequentially and returns the
    full list up front (name, title, artist, href, thumb_url, err)."""
    s = _make_session()
    html = _fetch_action(s, spotify_url)
    forms, fallback_thumb = _parse_forms(html)
    results = []
    for i, form in enumerate(forms):
        results.append(_resolve_track(s, form, i, fallback_thumb))
    return results


# ------------------------------------------------------------------ helpers

async def _download_thumb(url: str, dest: str):
    if not url or not url.startswith("http"):
        return None
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                with open(dest, "wb") as f:
                    f.write(await r.read())
        return dest if os.path.exists(dest) and os.path.getsize(dest) > 0 else None
    except Exception:
        return None


async def _send_track(client: Client, message: Message, status: Message, name: str, title: str, artist: str, href: str, thumb_url: str):
    if await try_send_cached(client, message, href, status):
        return True

    folder = make_output_folder("spotify")
    safe_title = safe_filename(name, "track")
    unique = f"{safe_title}_{abs(hash(href)) % 100000}"
    dest = os.path.join(folder, f"{unique}.mp3")
    thumb_path = os.path.join(folder, f"{unique}.jpg")

    try:
        await stream_download(
            href, dest, status, f"Downloading {name}",
            headers={"User-Agent": UA}, user_id=message.from_user.id, file_name=f"{unique}.mp3",
        )
        thumb = await _download_thumb(thumb_url, thumb_path)

        progress = make_upload_progress(status, file_name=name, quality="mp3")
        sent = await client.send_audio(
            chat_id=message.chat.id, audio=dest, thumb=thumb,
            title=title, performer=artist, caption=f"<b>🎵 {name}</b>",
            reply_to_message_id=message.id, parse_mode=enums.ParseMode.HTML, progress=progress,
        )
        try:
            from Rexbots.backup import backup_message
            await backup_message(client, sent)
        except Exception:
            pass
        try:
            await _cache_store(href, sent, caption=f"<b>🎵 {name}</b>")
        except Exception:
            pass
        return True
    except Exception as e:
        await message.reply_text(f"<b>{E_CROSS} Failed:</b> {name}\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)
        return False
    finally:
        for p in (dest, thumb_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# ------------------------------------------------------------------ flow

async def _start_track(client: Client, message: Message, url: str):
    status = await message.reply_text(f"<b>{E_INFO} Fetching track...</b>", parse_mode=enums.ParseMode.HTML)
    try:
        name, title, artist, href, thumb_url = await asyncio.to_thread(_spotify_get_track_blocking, url)
    except Exception as e:
        return await status.edit_text(f"<b>{E_CROSS} Failed:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)

    if not href:
        return await status.edit_text(f"<b>{E_CROSS} No downloadable stream found.</b>", parse_mode=enums.ParseMode.HTML)

    await status.edit_text(f"<b>{E_BOLT} Downloading...</b>", parse_mode=enums.ParseMode.HTML)
    ok = await _send_track(client, message, status, name, title, artist, href, thumb_url)
    if ok:
        try:
            await status.delete()
        except Exception:
            pass


def _confirm_kb(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Start download", callback_data=f"spotifygo#{session_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"spotifycancel#{session_id}"),
    ]])


async def _start_set(client: Client, message: Message, kind: str, url: str):
    session_id = f"{message.chat.id}_{message.id}"
    _SESSIONS[session_id] = {"kind": kind, "url": url, "chat_id": message.chat.id, "message": message}
    await message.reply_text(
        f"<b>{E_ROCKET} Spotify {kind} detected.</b>\n"
        f"<i>This will fetch and download every track — could take a while for large ones.</i>",
        reply_markup=_confirm_kb(session_id),
        parse_mode=enums.ParseMode.HTML,
    )


async def _run_set_download(client: Client, message: Message, kind: str, url: str, status: Message):
    await status.edit_text(f"<b>{E_INFO} Fetching {kind} track list...</b>", parse_mode=enums.ParseMode.HTML)
    try:
        results = await asyncio.to_thread(_spotify_get_set_blocking, url)
    except Exception as e:
        return await status.edit_text(f"<b>{E_CROSS} Failed to fetch {kind}:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)

    if not results:
        return await status.edit_text(f"<b>{E_CROSS} No tracks found.</b>", parse_mode=enums.ParseMode.HTML)

    total = len(results)
    done = failed = 0
    for i, (name, title, artist, href, thumb_url, err) in enumerate(results, start=1):
        if err or not href:
            failed += 1
            continue
        await status.edit_text(
            f"<b>{E_BOLT} Track {i}/{total}:</b> {name}\n✅ {done}   ❌ {failed}",
            parse_mode=enums.ParseMode.HTML,
        )
        ok = await _send_track(client, message, status, name, title, artist, href, thumb_url)
        if ok:
            done += 1
        else:
            failed += 1

    await status.edit_text(
        f"<b>{E_CHECK} Done — {done}/{total} track(s) sent</b>" + (f", {failed} failed." if failed else "."),
        parse_mode=enums.ParseMode.HTML,
    )


# ------------------------------------------------------------------ handlers

@Client.on_message(filters.text & filters.private & filters.regex(SPOTIFY_PATTERN) & ~filters.regex(r"^/"), group=1)
async def spotify_auto_detect(client: Client, message: Message):
    url = _extract_url(message.text)
    if not url:
        return
    kind = _spotify_kind(url)
    if kind == "track":
        await _start_track(client, message, url)
    elif kind in ("playlist", "album"):
        await _start_set(client, message, kind, url)
    else:
        await message.reply_text(f"<b>{E_CROSS} Unsupported Spotify link type.</b>", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("spotify") & filters.private)
async def spotify_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/spotify &lt;open.spotify.com track/playlist/album link&gt;</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    url = _extract_url(message.text.split(None, 1)[1].strip())
    if not url:
        return await message.reply_text(f"<b>{E_CROSS} That doesn't look like a Spotify link.</b>", parse_mode=enums.ParseMode.HTML)
    kind = _spotify_kind(url)
    if kind == "track":
        await _start_track(client, message, url)
    elif kind in ("playlist", "album"):
        await _start_set(client, message, kind, url)
    else:
        await message.reply_text(f"<b>{E_CROSS} Unsupported Spotify link type.</b>", parse_mode=enums.ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^spotifygo#"))
async def spotify_go_callback(client: Client, callback_query: CallbackQuery):
    session_id = callback_query.data.split("#", 1)[1]
    session = _SESSIONS.pop(session_id, None)
    await callback_query.answer()
    if not session:
        return await callback_query.message.edit_text(f"<b>{E_CROSS} Session expired — send the link again.</b>", parse_mode=enums.ParseMode.HTML)
    await _run_set_download(client, session["message"], session["kind"], session["url"], callback_query.message)


@Client.on_callback_query(filters.regex(r"^spotifycancel#"))
async def spotify_cancel_callback(client: Client, callback_query: CallbackQuery):
    session_id = callback_query.data.split("#", 1)[1]
    _SESSIONS.pop(session_id, None)
    await callback_query.answer("Cancelled")
    await callback_query.message.edit_text(f"<b>{E_CROSS} Cancelled.</b>", parse_mode=enums.ParseMode.HTML)
