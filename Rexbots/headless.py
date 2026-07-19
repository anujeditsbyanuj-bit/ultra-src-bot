# Headless-browser video URL discovery — last-resort fallback for JS-rendered
# players.
#
# yt-dlp's generic extractor only scans the raw HTML/JS it downloads; it
# never executes JavaScript. Most video sites without a dedicated yt-dlp
# extractor build the real video/manifest URL at runtime (signed tokens,
# player.js calling an API, etc.), so that URL simply never appears in the
# page source yt-dlp sees — no amount of smarter regex/parsing on our side
# can find a URL that isn't there.
#
# The only real fix is to actually render the page like a browser would and
# watch what it requests. This module does exactly that with Playwright:
# launch headless Chromium, load the page, nudge the player to start (many
# only fire the real media request after a play click), and collect any
# response that looks like a video file or streaming manifest.
#
# Requires: `pip install playwright` AND a one-time `playwright install
# chromium` (or `playwright install --with-deps chromium` to also grab the
# OS-level libraries) on the HOST — the pip package alone does not ship the
# browser binary. If that step hasn't been run, everything here degrades
# to returning None so callers just fall through to the next fallback.

import os
import re
import glob
import shutil
import asyncio

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

_MEDIA_EXT_RE = re.compile(r"\.(m3u8|mpd|mp4|webm|m4s)(\?|$)", re.IGNORECASE)
_MEDIA_CT_RE = re.compile(r"(mpegurl|dash\+xml|video/mp4|video/webm)", re.IGNORECASE)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_PLAY_SELECTORS = (
    "video",
    ".vjs-big-play-button",
    ".jw-icon-playback",
    "[class*='play-button']",
    "button[aria-label*='play' i]",
)


def system_chromium_path() -> str | None:
    """Chromium installed via replit.nix (nixpkgs' pkgs.chromium) is
    self-contained — all its shared libraries are pulled in through Nix
    store paths at build time, so it actually runs on Replit. Playwright's
    own `playwright install chromium` only fetches the browser binary, not
    the OS-level libraries it needs (libnss3, libgbm1, libasound2, etc.),
    which Replit's default environment doesn't have — that binary fails or
    hangs on launch there. Prefer the Nix one whenever it's present; either
    an explicit CHROMIUM_EXECUTABLE_PATH (set via replit.nix's env, or
    manually) or whatever `chromium`/`chromium-browser` is on PATH.
    """
    env_path = os.environ.get("CHROMIUM_EXECUTABLE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    for name in ("chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


# The Dockerfile runs `playwright install --with-deps chromium` at build
# time, so on a Docker deploy the browser is already there. Hosts that skip
# the Dockerfile (Procfile/buildpack-based Render/Railway deploys, Replit's
# default Nix env) never run that step, so as a safety net we self-install
# on first use here - once per process, cached after that either way. If a
# system Chromium is available (replit.nix), that's used instead and this
# whole install step is skipped.
_chromium_ensure_lock = asyncio.Lock()
_chromium_ensured = False


async def _ensure_chromium():
    global _chromium_ensured
    if _chromium_ensured:
        return
    async with _chromium_ensure_lock:
        if _chromium_ensured:
            return
        if system_chromium_path():
            _chromium_ensured = True
            return
        cache_dir = os.path.expanduser("~/.cache/ms-playwright")
        if glob.glob(os.path.join(cache_dir, "chromium-*")):
            _chromium_ensured = True
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "playwright", "install", "chromium",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=300)
        except Exception:
            pass  # best-effort - find_media_url below will just fail gracefully if this didn't work
        _chromium_ensured = True


def available() -> bool:
    return async_playwright is not None


async def find_media_url(page_url: str, timeout: int = 25) -> str | None:
    """Backward-compatible single-URL wrapper around find_media_urls()."""
    urls = await find_media_urls(page_url, timeout=timeout)
    return urls[0] if urls else None


async def find_media_urls(page_url: str, timeout: int = 25) -> list[str]:
    """Render page_url in headless Chromium and return every media-looking
    URL seen in network traffic, best guess first, or [] if nothing was
    found (or Playwright/its browser isn't installed on this host).

    Returns a ranked list rather than a single URL because the first
    "video-shaped" response isn't always the real content - some players
    fire off a small looping teaser/poster clip (a few hundred KB, same
    .mp4 extension) before the actual video request. Ranking by content-
    length (when the server sends one) and preferring HLS/DASH manifests
    (which describe the whole stream, not one small file) means callers can
    try candidates in order and fall through past a preview to the real
    one instead of only ever seeing whichever fired first."""
    if async_playwright is None:
        return []

    try:
        # _ensure_chromium's own internal install timeout is 300s - way
        # too long for a user waiting on a chat reply. On hosts where the
        # browser/its system libraries are missing or broken (e.g. Replit's
        # default Nix env, which skips the Dockerfile's `playwright install
        # --with-deps chromium` step), this used to leave the caller
        # hanging with no response for minutes. Cap it hard instead.
        await asyncio.wait_for(_ensure_chromium(), timeout=45)
    except asyncio.TimeoutError:
        return []

    candidates: list[tuple[str, int]] = []

    def on_response(response):
        try:
            url = response.url
            ctype = response.headers.get("content-type", "")
            if _MEDIA_EXT_RE.search(url) or _MEDIA_CT_RE.search(ctype):
                clen = response.headers.get("content-length")
                size = int(clen) if clen and clen.isdigit() else 0
                candidates.append((url, size))
        except Exception:
            pass

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                executable_path=system_chromium_path(),  # None -> Playwright's own bundled browser
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                context = await browser.new_context(user_agent=_UA)
                page = await context.new_page()
                page.on("response", on_response)

                try:
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                except Exception:
                    pass  # a slow/hanging page may still have fired useful requests already

                # Many players only issue the real media request after a
                # play click - try the obvious candidates, first one wins.
                for selector in _PLAY_SELECTORS:
                    try:
                        el = await page.query_selector(selector)
                        if el:
                            await el.click(timeout=2000)
                            break
                    except Exception:
                        continue

                await page.wait_for_timeout(6000)  # let the player start streaming
            finally:
                await browser.close()
    except Exception:
        return []

    if not candidates:
        return []

    # Dedupe, keeping the largest observed size per URL (some URLs get hit
    # more than once, e.g. a manifest re-requested after a seek).
    best_size = {}
    for url, size in candidates:
        if url not in best_size or size > best_size[url]:
            best_size[url] = size

    manifests = [u for u in best_size if ".m3u8" in u or ".mpd" in u]
    others = sorted(
        (u for u in best_size if u not in manifests),
        key=lambda u: best_size[u], reverse=True,
    )
    return manifests + others
