"""
Video Vault - a private Telegram bot that turns a chat into a video archive.

Send it a link (TikTok, YouTube, X, Instagram, Reddit, ... anything yt-dlp
supports); it downloads the video and posts it back into the chat with the
title and the original link. Uses MTProto (Telethon) so uploads can reach 2 GB
instead of the Bot API's 50 MB.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yt_dlp
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MAX_BYTES = int(float(os.environ.get("MAX_FILE_MB", "1950")) * 1024 * 1024)
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "1080"))
COOKIES_FILE = DATA_DIR / "cookies.txt"  # optional, Netscape format (helps with Instagram)
MAX_ITEMS_PER_LINK = 10  # posts with several videos (e.g. X, Instagram carousels)

URL_RE = re.compile(r"https?://[^\s<>\"']+")

MESSAGES = {
    "start": "👋 Send me a video link (TikTok, YouTube, X, Instagram, ...) and I'll download it and post it here.",
    "setup": "🔧 Setup mode.\nYour user ID: `{uid}`\nPut it in ALLOWED_USER_IDS in .env and restart the bot.",
    "denied": "⛔ This bot is private.",
    "queued": "🕒 Queued... ({n} ahead)",
    "downloading": "⏳ Downloading...",
    "uploading": "📤 Uploading... {pct}%",
    "too_big": "⚠️ The video is larger than the limit ({mb} MB).",
    "failed": "❌ Download failed:\n{err}",
    "no_video": "⚠️ No video found at this link.",
}
# Optional translations: put a JSON file with the same keys at DATA_DIR/messages.json.
MESSAGES_FILE = DATA_DIR / "messages.json"
if MESSAGES_FILE.exists():
    MESSAGES.update(json.loads(MESSAGES_FILE.read_text(encoding="utf-8")))
T = MESSAGES

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("video-vault")
logging.getLogger("telethon").setLevel(logging.WARNING)

client = TelegramClient(str(DATA_DIR / "bot"), API_ID, API_HASH)
job_lock = asyncio.Lock()  # one download at a time keeps the small server responsive
waiting = 0


# ---------------------------------------------------------------------------
# Download and media helpers (blocking - run in a worker thread)
# ---------------------------------------------------------------------------

class TooBig(Exception):
    pass


def download(url: str, workdir: Path) -> list[dict]:
    """Download every video behind a link. Returns [{path, title, duration, ...}]."""
    opts = {
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "format_sort": [f"res:{MAX_HEIGHT}", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": f"1:{MAX_ITEMS_PER_LINK}",
        "max_filesize": MAX_BYTES,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    entries = info.get("entries") or [info]
    results = []
    for e in entries:
        if not e:
            continue
        for d in e.get("requested_downloads") or []:
            path = Path(d.get("filepath") or "")
            if path.is_file():
                if path.stat().st_size > MAX_BYTES:
                    raise TooBig()
                results.append({
                    "path": path,
                    "title": e.get("title") or e.get("description") or "",
                    "uploader": e.get("uploader") or e.get("channel") or "",
                    "site": e.get("extractor_key") or "",
                })
    return results


def probe(path: Path) -> tuple[int, int, int]:
    """Return (duration_seconds, width, height) using ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        j = json.loads(out.stdout or "{}")
        s = (j.get("streams") or [{}])[0]
        return int(float(j.get("format", {}).get("duration") or 0)), int(s.get("width") or 0), int(s.get("height") or 0)
    except (ValueError, KeyError, IndexError):
        return 0, 0, 0


def make_thumb(path: Path, duration: int) -> Path | None:
    """Grab one frame as a small JPEG so the video shows a preview in the chat."""
    thumb = path.with_suffix(".thumb.jpg")
    at = "1" if duration > 2 else "0"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", at, "-i", str(path),
         "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(thumb)],
        capture_output=True, timeout=60,
    )
    return thumb if thumb.is_file() else None


def caption_for(item: dict, url: str) -> str:
    title = re.sub(r"\s+", " ", item["title"]).strip()
    if len(title) > 300:
        title = title[:297] + "..."
    parts = [p for p in (title, item["uploader"] and f"👤 {item['uploader']}") if p]
    parts.append(f"🔗 {url}")
    return "\n".join(parts)[:1024]


def short_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    return msg[:300]


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

def authorized(event) -> bool:
    return event.sender_id in ALLOWED_USER_IDS


@client.on(events.NewMessage(pattern=r"^/start"))
async def on_start(event):
    if not ALLOWED_USER_IDS:
        await event.reply(T["setup"].format(uid=event.sender_id))
        log.info("Setup mode: user %s asked for their ID", event.sender_id)
    elif authorized(event):
        await event.reply(T["start"])
    else:
        await event.reply(T["denied"])
        log.warning("Rejected /start from user %s", event.sender_id)
    raise events.StopPropagation


@client.on(events.NewMessage(func=lambda e: e.is_private and bool(e.raw_text)))
async def on_message(event):
    global waiting
    if not ALLOWED_USER_IDS:
        await event.reply(T["setup"].format(uid=event.sender_id))
        return
    if not authorized(event):
        log.warning("Ignored message from unauthorized user %s", event.sender_id)
        return

    urls = list(dict.fromkeys(URL_RE.findall(event.raw_text)))
    if not urls:
        await event.reply(T["start"])
        return

    for url in urls:
        ahead = waiting + (1 if job_lock.locked() else 0)
        status = await event.reply(T["queued"].format(n=ahead) if ahead else T["downloading"])
        waiting += 1
        async with job_lock:
            waiting -= 1
            try:
                await process(event, url, status)
            except Exception as e:  # never let one bad link kill the handler
                log.exception("Unexpected error for %s", url)
                await safe_edit(status, T["failed"].format(err=short_error(e)))


async def process(event, url: str, status):
    await safe_edit(status, T["downloading"])
    workdir = Path(tempfile.mkdtemp(prefix="vv-", dir=DATA_DIR))
    started = time.monotonic()
    try:
        try:
            items = await asyncio.to_thread(download, url, workdir)
        except TooBig:
            await safe_edit(status, T["too_big"].format(mb=MAX_BYTES // (1024 * 1024)))
            return
        except Exception as e:  # yt-dlp raises many types; report them to the user
            log.warning("Download failed for %s: %s", url, short_error(e))
            await safe_edit(status, T["failed"].format(err=short_error(e)))
            return
        if not items:
            await safe_edit(status, T["no_video"])
            return

        for item in items:
            duration, w, h = await asyncio.to_thread(probe, item["path"])
            thumb = await asyncio.to_thread(make_thumb, item["path"], duration)
            last = 0.0

            async def progress(sent, total):
                nonlocal last
                now = time.monotonic()
                if total and now - last > 3:
                    last = now
                    await safe_edit(status, T["uploading"].format(pct=int(sent * 100 / total)))

            await client.send_file(
                event.chat_id,
                item["path"],
                caption=caption_for(item, url),
                thumb=str(thumb) if thumb else None,
                supports_streaming=True,
                attributes=[DocumentAttributeVideo(duration=duration, w=w, h=h, supports_streaming=True)],
                progress_callback=progress,
            )
            log.info("Sent %s (%s, %.1f MB)", url, item["site"], item["path"].stat().st_size / 1e6)

        await status.delete()
        log.info("Done %s in %.0fs", url, time.monotonic() - started)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def safe_edit(msg, text: str):
    try:
        await msg.edit(text)
    except Exception:
        pass  # "message not modified" and similar are harmless


async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Remove temp folders left behind if the bot was killed mid-download.
    for leftover in DATA_DIR.glob("vv-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    mode = "setup mode (no ALLOWED_USER_IDS)" if not ALLOWED_USER_IDS else f"{len(ALLOWED_USER_IDS)} allowed user(s)"
    log.info("Video Vault running as @%s - %s", me.username, mode)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
