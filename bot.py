import asyncio
import html
import logging
import os
import re
import tempfile
import time
import uuid
from functools import partial
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
import static_ffmpeg
from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

static_ffmpeg.add_paths()  # додає ffmpeg/ffprobe в PATH якщо не знайдено системного

load_dotenv(override=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Встановіть TELEGRAM_TOKEN у змінних оточення або .env файлі")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

URL_RE = re.compile(r"https?://\S+")
TIKTOK_PHOTO_RE = re.compile(r"tiktok\.com/@[^/?#]+/photo/(\d+)")
TIKTOK_SHORT_RE = re.compile(r"(?:vt|vm)\.tiktok\.com/\w+")
MAX_FILE_SIZE = 49 * 1024 * 1024
TOKEN_TTL = 600  # секунд до видалення невикористаного токена
DOWNLOAD_TOKENS: dict[str, tuple[str, dict | None, float]] = {}  # token -> (url, info, timestamp)

QUALITY_FORMATS = {
    "360":  "best[height<=360][ext=mp4]/best[height<=360]/worst[ext=mp4]/worst",
    "720":  "best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best",
    "1080": "best[height<=1080][ext=mp4]/best[height<=1080]/best[ext=mp4]/best",
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
}

try:
    import curl_cffi  # noqa: F401
    _IMPERSONATE = {"impersonate": ImpersonateTarget("chrome", "116")}
    logger.info("curl_cffi available — TikTok impersonation enabled")
except ImportError:
    _IMPERSONATE = {}
    logger.warning("curl_cffi not installed — TikTok may fail with 403/429")

YDL_COMMON = {
    "quiet": True,
    "no_warnings": True,
    **_IMPERSONATE,
    # iOS client bypasses YouTube bot-detection on most videos
    "extractor_args": {"youtube": {"player_client": ["ios"]}},
}

_BOT_DIR = Path(__file__).parent

# Підтримка cookies через env-змінну COOKIES_B64 (base64) — для Railway/Docker-деплоїв
def _init_env_cookies() -> None:
    raw = os.getenv("COOKIES_B64", "").strip()
    if not raw:
        return
    import base64
    dest = _BOT_DIR / "env_cookies.txt"
    try:
        decoded = base64.b64decode("".join(raw.split()))  # strip whitespace Railway might add
        # Decode to text with multiple fallbacks, then write clean UTF-8 for yt-dlp
        for enc in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
            try:
                content = decoded.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dest.write_text(content, encoding="utf-8")
        logger.info("Cookies loaded from COOKIES_B64 env var → %s (%d bytes)", dest.name, len(decoded))
    except Exception as exc:
        logger.warning("Failed to decode COOKIES_B64: %s", exc)

_init_env_cookies()

# Cookies читаються один раз при запуску; якщо є кілька файлів — об'єднуються в один
def _load_cookie_opts() -> dict:
    files = [p for p in _BOT_DIR.glob("*cookies*.txt") if not p.name.startswith("_combined")]
    if not files:
        return {}
    if len(files) == 1:
        logger.info("Using cookies file: %s", files[0].name)
        return {"cookiefile": str(files[0])}

    combined = _BOT_DIR / "_combined_cookies.txt"
    lines = ["# Netscape HTTP Cookie File\n"]
    for p in files:
        logger.info("Merging cookies file: %s", p.name)
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#"):
                    lines.append(line + "\n")
        except Exception as exc:
            logger.warning("Could not read %s: %s", p.name, exc)
    combined.write_text("".join(lines), encoding="utf-8")
    logger.info("Combined %d cookie files → %s", len(files), combined.name)
    return {"cookiefile": str(combined)}

_COOKIE_OPTS = _load_cookie_opts()


def _read_cookies_for_domain(domain: str) -> str:
    """Return Cookie header string for the given domain from the loaded cookie file."""
    cookiefile = _COOKIE_OPTS.get("cookiefile")
    if not cookiefile:
        return ""
    try:
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(cookiefile)
        jar.load(ignore_discard=True, ignore_expires=True)
        cookies = {c.name: c.value for c in jar if domain in (c.domain or "")}
        logger.debug("Cookies for %s: %d found", domain, len(cookies))
        return "; ".join(f"{k}={v}" for k, v in cookies.items())
    except Exception as exc:
        logger.warning("_read_cookies_for_domain(%s) failed: %s", domain, exc)
        return ""

YOUTUBE_BOT_MSG = (
    "YouTube заблокував запит через захист від ботів.\n\n"
    "<b>Як виправити:</b>\n"
    "1. Встанови розширення <b>«Get cookies.txt LOCALLY»</b> у Chrome/Edge\n"
    "2. Залогінься на youtube.com\n"
    "3. Натисни розширення → Export → збережи файл як <code>cookies.txt</code> "
    "у папку бота (<code>C:\\Users\\Roman\\bob_downloader\\</code>)\n"
    "4. Перезапусти бота"
)

RATE_LIMIT_MSG = (
    "TikTok тимчасово заблокував запит (429 Too Many Requests).\n\n"
    "<b>Як виправити:</b>\n"
    "1. Встанови розширення <b>«Get cookies.txt LOCALLY»</b> у Chrome/Edge\n"
    "2. Відкрий tiktok.com (залогінься якщо треба)\n"
    "3. Натисни розширення → Export → збережи файл як <code>cookies.txt</code> "
    "у папку бота (<code>C:\\Users\\Roman\\bob_downloader\\</code>)\n"
    "4. Перезапусти бота\n\n"
    "Або просто зачекай кілька хвилин і спробуй ще раз."
)


def _is_rate_limit(exc: Exception) -> bool:
    return "429" in str(exc) or "Too Many Requests" in str(exc)


def _is_youtube_bot_error(exc: Exception) -> bool:
    msg = str(exc)
    return "Sign in to confirm" in msg or ("bot" in msg.lower() and "youtube" in msg.lower())


def _resolve_url(url: str) -> str:
    """Follow HTTP redirects and return the final URL (best-effort)."""
    try:
        if _IMPERSONATE:
            from curl_cffi import requests as cffi_req
            resp = cffi_req.get(url, impersonate="chrome116", allow_redirects=True, timeout=10)
            return resp.url
        else:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible)"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.url
    except Exception as exc:
        logger.warning("URL resolution failed for %s: %s", url, exc)
        return url


def _tiktok_api(item_id: str, cookie_str: str) -> tuple[list[str], dict] | None:
    """Try TikTok web API endpoint. Returns None if response has no image data."""
    import json as _json

    api_url = (
        f"https://www.tiktok.com/api/item/detail/"
        f"?itemId={item_id}&aid=1988&app_language=en&app_name=tiktok_web"
        f"&device_platform=web_pc&language=en&region=US"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.tiktok.com/",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str

    try:
        if _IMPERSONATE:
            from curl_cffi import requests as cffi_req
            resp = cffi_req.get(api_url, headers=headers, impersonate="chrome120", timeout=15)
            if not resp.content:
                return None
            data = resp.json()
        else:
            import urllib.request, json as _j
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = _j.loads(r.read().decode())
    except Exception as exc:
        logger.warning("TikTok API call failed: %s", exc)
        return None

    item = data.get("itemInfo", {}).get("itemStruct", {})
    if not item:
        logger.warning("TikTok API: empty itemStruct (statusCode=%s keys=%s)",
                       data.get("statusCode"), list(data.keys())[:6])
        return None

    title = (item.get("desc") or "TikTok фото").strip()
    uploader = (item.get("author") or {}).get("nickname") or ""

    image_urls: list[str] = []
    for raw in (item.get("imagePost") or {}).get("images") or []:
        url_list = (raw.get("imageURL") or {}).get("urlList") or []
        if url_list:
            image_urls.append(url_list[0])

    return (image_urls, {"title": title, "uploader": uploader}) if image_urls else None


def _get_tiktok_photos(url: str) -> tuple[list[str], dict]:
    """Fetch image URLs for a TikTok photo/slideshow post."""
    m = TIKTOK_PHOTO_RE.search(url)
    if not m:
        raise ValueError("Not a TikTok photo URL")
    item_id = m.group(1)

    # Try API first — works from server IPs when valid session cookies are provided
    cookie_str = _read_cookies_for_domain("tiktok.com")
    api_result = _tiktok_api(item_id, cookie_str)
    if api_result:
        logger.info("TikTok API succeeded for item %s", item_id)
        return api_result

    # Fallback: page scraping (works when TikTok serves SSR HTML)
    page_url = url.split("?")[0]

    fetch_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    if cookie_str:
        fetch_headers["Cookie"] = cookie_str

    if _IMPERSONATE:
        from curl_cffi import requests as cffi_req
        resp = cffi_req.get(page_url, headers=fetch_headers, impersonate="chrome120", timeout=20)
        html = resp.text
        logger.info("TikTok page fetch: status=%d len=%d cookies=%s",
                    resp.status_code, len(html), "yes" if cookie_str else "no")
    else:
        import urllib.request
        req = urllib.request.Request(page_url, headers=fetch_headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")

    import json as _json

    # TikTok embeds post data in a <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"> tag
    def _extract_script(script_id: str) -> dict | None:
        marker = f'id="{script_id}"'
        idx = html.find(marker)
        if idx == -1:
            return None
        content_start = html.find(">", idx) + 1
        content_end = html.find("</script>", content_start)
        try:
            return _json.loads(html[content_start:content_end].strip())
        except Exception:
            return None

    item_struct: dict = {}

    data = _extract_script("__UNIVERSAL_DATA_FOR_REHYDRATION__")
    if data:
        item_struct = (
            data.get("__DEFAULT_SCOPE__", {})
                .get("webapp.video-detail", {})
                .get("itemInfo", {})
                .get("itemStruct", {})
        )

    if not item_struct:
        data = _extract_script("SIGI_STATE")
        if data:
            item_module = data.get("ItemModule", {})
            item_struct = next(iter(item_module.values()), {}) if item_module else {}

    if not item_struct:
        logger.warning("TikTok page snippet (first 500 chars): %r", html[:500])
        raise ValueError("Не вдалося знайти дані про пост на сторінці TikTok")

    title = (item_struct.get("desc") or "TikTok фото").strip()
    uploader = (item_struct.get("author") or {}).get("nickname") or ""

    image_urls: list[str] = []
    for raw in (item_struct.get("imagePost") or {}).get("images") or []:
        url_list = (raw.get("imageURL") or {}).get("urlList") or []
        if url_list:
            image_urls.append(url_list[0])

    if not image_urls:
        raise ValueError("У цьому пості не знайдено зображень")

    return image_urls, {"title": title, "uploader": uploader}


def _download_images(image_urls: list[str], target_dir: Path) -> list[Path]:
    """Download a list of image URLs into target_dir."""
    dl_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.tiktok.com/",
    }
    paths: list[Path] = []
    for i, img_url in enumerate(image_urls):
        dest = target_dir / f"photo_{i + 1:02d}.jpg"
        if _IMPERSONATE:
            from curl_cffi import requests as cffi_req
            r = cffi_req.get(img_url, headers=dl_headers, impersonate="chrome116", timeout=30)
            dest.write_bytes(r.content)
        else:
            import urllib.request
            req = urllib.request.Request(img_url, headers=dl_headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                dest.write_bytes(r.read())
        paths.append(dest)
    return paths


def _retry(fn, *args, retries: int = 3):
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn(*args)
        except Exception as e:
            last_exc = e
            if _is_rate_limit(e) and attempt < retries - 1:
                wait = 10 * (attempt + 1)
                logger.warning("Rate limited, retry %d/%d in %ds", attempt + 1, retries, wait)
                time.sleep(wait)
            else:
                raise
    raise last_exc  # type: ignore[misc]


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def _get_info(url: str) -> dict:
    opts = {**YDL_COMMON, **_COOKIE_OPTS, "skip_download": True}

    def _extract():
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            if not entries:
                raise ValueError("Плейлист порожній або недоступний")
            return entries[0]
        return info

    return _retry(_extract)


def _download(url: str, mode: str, quality: str, target_dir: Path) -> Path:
    if mode == "audio":
        ydl_opts = {
            **YDL_COMMON,
            **_COOKIE_OPTS,
            "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": str(target_dir / "%(title).180s.%(ext)s"),
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
        }
    else:
        ydl_opts = {
            **YDL_COMMON,
            **_COOKIE_OPTS,
            "format": QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"]),
            "outtmpl": str(target_dir / "%(title).180s.%(ext)s"),
            "merge_output_format": "mp4",
        }

    def _do():
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

    _retry(_do)

    files = list(target_dir.iterdir())
    if not files:
        raise FileNotFoundError("Файл не завантажено")
    return max(files, key=lambda p: p.stat().st_size)


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _build_info_text(info: dict) -> str:
    title = html.escape(info.get("title") or "Відео")
    uploader = html.escape(info.get("uploader") or info.get("channel") or "")
    duration = _format_duration(info.get("duration"))

    lines = [f"<b>{title}</b>"]
    if uploader:
        lines.append(f"Автор: {uploader}")
    if duration:
        lines.append(f"Тривалість: {duration}")
    lines.append("\nОберіть формат:")
    return "\n".join(lines)


def _build_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Аудіо MP3", callback_data=f"dl:{token}:audio:best")],
        [
            InlineKeyboardButton(text="📹 360p",  callback_data=f"dl:{token}:video:360"),
            InlineKeyboardButton(text="📹 720p",  callback_data=f"dl:{token}:video:720"),
            InlineKeyboardButton(text="📹 1080p", callback_data=f"dl:{token}:video:1080"),
        ],
    ])


def _pop_token(token: str) -> tuple[str, dict | None] | tuple[None, None]:
    entry = DOWNLOAD_TOKENS.pop(token, None)
    return (entry[0], entry[1]) if entry else (None, None)


def _cleanup_tokens() -> None:
    cutoff = time.time() - TOKEN_TTL
    stale = [k for k, (_, _i, ts) in DOWNLOAD_TOKENS.items() if ts < cutoff]
    for k in stale:
        del DOWNLOAD_TOKENS[k]


async def run_sync(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args))


async def send_photo_album(
    message: types.Message,
    paths: list[Path],
    caption: str,
) -> None:
    """Send images as Telegram media group (≤10 per chunk)."""
    from aiogram.types import InputMediaPhoto

    await bot.send_chat_action(message.chat.id, "upload_photo")
    for chunk_start in range(0, len(paths), 10):
        chunk = paths[chunk_start : chunk_start + 10]
        media = [
            InputMediaPhoto(
                media=FSInputFile(p),
                caption=caption if i == 0 and chunk_start == 0 else None,
                parse_mode="HTML" if i == 0 and chunk_start == 0 else None,
            )
            for i, p in enumerate(chunk)
        ]
        await message.answer_media_group(media=media)


async def send_media(
    message: types.Message,
    path: Path,
    mode: str,
    info: dict | None = None,
) -> None:
    size = path.stat().st_size
    if size > MAX_FILE_SIZE:
        await message.answer(
            f"Файл {size // 1024 // 1024} МБ перевищує ліміт Telegram (49 МБ).\n"
            "Спробуйте нижчу якість або завантажте локально."
        )
        return

    if mode == "audio":
        await bot.send_chat_action(message.chat.id, "upload_voice")
        title = (info or {}).get("title") or None
        performer = (info or {}).get("artist") or (info or {}).get("uploader") or None
        await message.answer_audio(
            audio=FSInputFile(path),
            title=title,
            performer=performer,
        )
    else:
        await bot.send_chat_action(message.chat.id, "upload_video")
        try:
            await message.answer_video(video=FSInputFile(path))
        except Exception:
            # TikTok/Instagram often use HEVC — Telegram rejects it as video, send as file instead
            logger.warning("answer_video rejected, falling back to document: %s", path.suffix)
            await bot.send_chat_action(message.chat.id, "upload_document")
            await message.answer_document(
                document=FSInputFile(path),
                caption="Відео надіслано як файл (формат не підтримується відеоплеєром Telegram)",
            )


async def do_download(
    reply_to: types.Message,
    url: str,
    mode: str,
    quality: str,
    info: dict | None = None,
) -> None:
    status = await reply_to.answer("⏳ Завантажую...")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = await run_sync(_download, url, mode, quality, Path(tmp))
        except Exception as e:
            logger.exception("Download failed url=%s mode=%s quality=%s", url, mode, quality)
            if _is_rate_limit(e):
                await status.edit_text(RATE_LIMIT_MSG, parse_mode="HTML")
            else:
                await status.edit_text("Помилка завантаження. Перевірте посилання і спробуйте ще раз.")
            return

        await status.edit_text("📤 Надсилаю файл...")
        try:
            await send_media(reply_to, path, mode, info)
        except Exception:
            logger.exception("Send failed")
            await status.edit_text("Не вдалося надіслати файл. Можливо він зашифрований або пошкоджений.")
            return

    await status.delete()


# ──────────────── handlers ────────────────

@dp.message(Command("start", "help"))
async def cmd_start(message: types.Message) -> None:
    await message.answer(
        "Привіт! Надішли мені посилання на YouTube, TikTok, Instagram та ін.,\n"
        "і я запропоную завантажити відео або аудіо.\n\n"
        "<b>Команди:</b>\n"
        "/audio &lt;URL&gt; — аудіо в MP3\n"
        "/video &lt;URL&gt; — відео в найкращій якості",
        parse_mode="HTML",
    )


@dp.message(Command("audio", "video"))
async def cmd_download(message: types.Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Додайте посилання після команди: /audio <URL>")
        return

    url = extract_url(parts[1])
    if not url:
        await message.answer("Посилання не знайдено. Перевірте URL.")
        return

    mode = "audio" if message.text.startswith("/audio") else "video"
    await do_download(message, url, mode, "best")


@dp.message(F.text.regexp(r"https?://\S+"))
async def url_handler(message: types.Message) -> None:
    url = extract_url(message.text)
    if not url:
        return

    _cleanup_tokens()
    status = await message.answer("🔍 Отримую інформацію...")

    # Resolve short TikTok links (vt.tiktok.com / vm.tiktok.com) before checking URL type
    resolved_url = url
    if TIKTOK_SHORT_RE.search(url):
        resolved_url = await run_sync(_resolve_url, url)

    # TikTok photo/slideshow posts are unsupported by yt-dlp — use custom handler
    if TIKTOK_PHOTO_RE.search(resolved_url):
        try:
            image_urls, photo_info = await run_sync(_get_tiktok_photos, resolved_url)
        except Exception:
            logger.exception("TikTok photo fetch failed url=%s", resolved_url)
            await status.edit_text("Не вдалося отримати фото. Перевірте посилання.")
            return

        await status.edit_text("⏳ Завантажую фото...")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                paths = await run_sync(_download_images, image_urls, Path(tmp))
            except Exception:
                logger.exception("TikTok photo download failed url=%s", resolved_url)
                await status.edit_text("Помилка завантаження фото.")
                return

            title = html.escape(photo_info.get("title") or "TikTok фото")
            uploader = html.escape(photo_info.get("uploader") or "")
            caption = f"<b>{title}</b>" + (f"\nАвтор: {uploader}" if uploader else "")

            await status.edit_text("📤 Надсилаю фото...")
            try:
                await send_photo_album(message, paths, caption)
            except Exception:
                logger.exception("Send photo album failed url=%s", resolved_url)
                await status.edit_text("Не вдалося надіслати фото.")
                return

        await status.delete()
        return

    try:
        info = await run_sync(_get_info, url)
    except Exception as e:
        logger.exception("Info extraction failed url=%s", url)
        if _is_rate_limit(e):
            await status.edit_text(RATE_LIMIT_MSG, parse_mode="HTML")
        elif _is_youtube_bot_error(e):
            await status.edit_text(YOUTUBE_BOT_MSG, parse_mode="HTML")
        else:
            await status.edit_text("Не вдалося отримати інформацію. Перевірте посилання.")
        return

    token = uuid.uuid4().hex
    DOWNLOAD_TOKENS[token] = (url, info, time.time())

    await status.edit_text(
        _build_info_text(info),
        reply_markup=_build_keyboard(token),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("dl:"))
async def callback_download(callback: types.CallbackQuery) -> None:
    await callback.answer()

    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.message.answer("Неправильні дані. Надішліть посилання знову.")
        return

    _, token, mode, quality = parts
    url, info = _pop_token(token)
    if not url:
        await callback.message.answer("Кнопка застаріла — надішліть посилання ще раз.")
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await do_download(callback.message, url, mode, quality, info)


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
