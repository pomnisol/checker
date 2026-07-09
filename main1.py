import asyncio
import logging
import html
import re
import json
from datetime import datetime

import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (LinkPreviewOptions, InlineKeyboardMarkup,
                           InlineKeyboardButton, BufferedInputFile)

# Токен вашего бота от @BotFather
TOKEN = "8458015105:AAGF4u67qXRROgwhct6l7Ovo9ng_Ny6RLtA"

bot = Bot(token=TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Referer": "https://www.tiktok.com/"})

# ─────────────────────────────── ПРОКСИ ───────────────────────────────
# TikTok часто блокирует дата-центровые IP (хостинги). Чтобы обойти — впиши сюда
# прокси (лучше резидентный/мобильный). Формат:
#   "http://user:pass@host:port"   или   "http://host:port"
#   (socks5 тоже можно: "socks5://user:pass@host:port" — нужен пакет requests[socks])
# Оставь пустым (""), чтобы ходить напрямую.
PROXY = "http://kQ7AIo0kf4:79mu5s2F9h@23.152.200.36:38194"

if PROXY:
    SESSION.proxies.update({"http": PROXY, "https": PROXY})

CODEC_MAP = {"h264": "h264", "h265_hvc1": "hevc", "h265": "hevc", "bytevc1": "hevc"}

# ─────────────────────────── PREMIUM ЭМОДЗИ ───────────────────────────
# Как это работает (см. пояснение в ответе):
#   Синтаксис в HTML:  <tg-emoji emoji-id="ID">🙂</tg-emoji>
#   emoji-id — числовой custom_emoji_id премиум-эмодзи. Символ внутри тега
#   ("🙂") — запасной, показывается там, где кастомные эмодзи недоступны.
#
# Чтобы включить свои премиум-эмодзи:
#   1) Владелец бота должен иметь Telegram Premium (или у бота куплен username на Fragment).
#   2) Возьми ID эмодзи (перешли премиум-эмодзи боту @idstickerbot / @Utagbot,
#      либо через getCustomEmojiStickers) и впиши в словарь ниже.
#   3) Поставь USE_PREMIUM_EMOJI = True.
# Если False — используются обычные юникод-иконки (fallback ниже), бот работает у всех.

USE_PREMIUM_EMOJI = True

# слот -> (custom_emoji_id, запасной юникод)
# ВАЖНО: символ-заглушка внутри <tg-emoji> должен совпадать с alt самого
# премиум-эмодзи (это поле "text" из апдейта, где ты его прислал). Если поставить
# другой символ — Telegram проигнорирует кастом-эмодзи. Поэтому заглушки взяты
# ровно из присланных апдейтов, а не по смыслу слота.
EMOJI = {
    "video":     ("5258077307985207053", "📹"),
    "user":      ("5258011929993026890", "👤"),
    "calendar":  ("5258105663359294787", "🗓"),
    "music":     ("5258289810082111221", "🎵"),
    "stats":     ("5258330865674494479", "📊"),
    "eye":       ("6037397706505195857", "👁"),
    "heart":     ("5938368005611195877", "❤️"),
    "comment":   ("6034831751308644168", "💬"),
    "bookmark":  ("6030425896546996257", "⭐️"),
    "repost":    ("6037622221625626773", "➡️"),
    "download":  ("6039802767931871481", "⬇️"),
    "info":      ("5258503720928288433", "ℹ️"),
    "id":        ("6026215106315034686", "🗂"),
    "source":    ("5899757765743615694", "🔗"),
    "region":    ("5778661935927004845", "📍"),
    "ghost":     ("5897962422169243693", "👻"),
    "star":      ("5258185631355378853", "⭐️"),
    "globe":     ("5776233299424843260", "🌐"),
    "phone":     ("6021554972309592266", "📱"),
    "quality":   ("5776233299424843260", "🌐"),
    "folder":    ("5296348778012361146", "🏷"),
    "bolt":      ("5305336095863485125", "🍉"),
}


def em(slot: str) -> str:
    """Возвращает премиум-эмодзи (tg-emoji) или обычный юникод в зависимости от настройки."""
    cid, fallback = EMOJI.get(slot, ("", "•"))
    if USE_PREMIUM_EMOJI and cid:
        return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>'
    return fallback


# ───────────────────────────── СБОР ДАННЫХ ─────────────────────────────
def _resolve_url(url: str) -> str:
    try:
        r = SESSION.get(url, allow_redirects=True, timeout=20)
        return r.url or url
    except Exception as e:
        logging.warning(f"resolve_url: {e}")
        return url


def _fetch_web_item(url: str):
    try:
        html_text = SESSION.get(url, timeout=25).text
        m = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>',
            html_text, re.S)
        if not m:
            return None
        scope = json.loads(m.group(1)).get("__DEFAULT_SCOPE__", {})
        return scope.get("webapp.video-detail", {}).get("itemInfo", {}).get("itemStruct")
    except Exception as e:
        logging.warning(f"fetch_web_item: {e}")
        return None


def _fetch_open_cdn(url: str):
    """tikwm -> прямые ссылки на публичный CDN (tiktokcdn-us), открываются без 403."""
    try:
        j = SESSION.post("https://www.tikwm.com/api/",
                         data={"url": url, "hd": 1}, timeout=25).json()
        if j.get("code") != 0:
            return None
        d = j.get("data") or {}

        def _abs(u):
            if not u:
                return None
            return ("https://www.tikwm.com" + u) if u.startswith("/") else u

        music_url = (d.get("music_info") or {}).get("play") or d.get("music")
        return {"sd_url": _abs(d.get("play")), "hd_url": _abs(d.get("hdplay")),
                "sd_size": d.get("size"), "hd_size": d.get("hd_size"),
                "music_url": _abs(music_url)}
    except Exception as e:
        logging.warning(f"fetch_open_cdn: {e}")
        return None


def _fmt_size(num):
    try:
        return f"{int(num) / (1024 * 1024):.1f} MB" if num else None
    except (TypeError, ValueError):
        return None


# Флаг-эмодзи из кода страны (US -> 🇺🇸)
def _flag(code: str) -> str:
    if not code or len(code) != 2 or not code.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code.upper())


REGION_NAMES = {
    "US": "United States", "GB": "United Kingdom", "RU": "Russia", "BY": "Belarus",
    "UA": "Ukraine", "DE": "Germany", "FR": "France", "PL": "Poland", "KZ": "Kazakhstan",
}

# Русские названия месяцев (strftime %B зависит от локали ОС — делаем сами)
RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def extract_tiktok_full_analytics(url: str):
    final_url = _resolve_url(url)
    item = _fetch_web_item(final_url)
    if not item:
        return None

    author = item.get("author", {}) or {}
    stats_raw = item.get("statsV2", {}) or item.get("stats", {}) or {}
    video = item.get("video", {}) or {}
    music = item.get("music", {}) or {}

    uploader = author.get("nickname") or author.get("uniqueId") or "unknown_user"
    unique_id = author.get("uniqueId") or uploader
    uploader_url = f"https://www.tiktok.com/@{unique_id}"

    video_id = item.get("id") or video.get("id") or "N/A"
    video_url = f"https://www.tiktok.com/@{unique_id}/video/{video_id}"

    ts = item.get("createTime")
    try:
        dt = datetime.fromtimestamp(int(ts))
        date_str = f"{dt.day} {RU_MONTHS[dt.month]} {dt.year}, {dt.strftime('%H:%M:%S')}" if ts else "Неизвестно"
    except (TypeError, ValueError, KeyError):
        date_str = "Неизвестно"

    description = item.get("desc") or "Без описания"

    music_title = music.get("title") or "original sound"
    m_dur = music.get("duration")
    music_dur = f"{m_dur // 60}:{m_dur % 60:02d}" if m_dur else "0:00"

    def _stat(*keys):
        for k in keys:
            v = stats_raw.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return v
        return 0

    stats = {
        'views': _stat('playCount'), 'likes': _stat('diggCount'),
        'comments': _stat('commentCount'), 'favorites': _stat('collectCount'),
        'shares': _stat('shareCount'), 'downloads': _stat('downloadCount'),
    }

    region_code = item.get("locationCreated") or ""
    region_name = REGION_NAMES.get(region_code.upper(), region_code or "Global")

    # теневой бан — если видео не индексируется / приватное / на ревью
    shadow = (not item.get("indexEnabled", True)) or item.get("isReviewing") or item.get("privateItem")
    shadow_str = "Да" if shadow else "Нет"

    # Оригинальное разрешение и VQScore
    orig_w, orig_h = video.get("width"), video.get("height")
    orig_res = f"{orig_w}x{orig_h}" if orig_w and orig_h else "N/A"
    vq_score = video.get("VQScore") or "0"

    # Качества с настоящим FPS (BitrateFPS)
    formats_data = []
    best = None
    for b in video.get("bitrateInfo", []) or []:
        pa = b.get("PlayAddr", {}) or {}
        w, h = pa.get("Width"), pa.get("Height")
        fps = b.get("BitrateFPS")
        br = b.get("Bitrate")
        codec_raw = (b.get("CodecType") or "").lower()
        codec = CODEC_MAP.get(codec_raw, codec_raw or "N/A")
        # Ярлык качества а-ля "4K120": класс разрешения (по короткой стороне) + fps.
        # У TikTok видео вертикальное, поэтому "качество" определяет меньшая сторона.
        short = min(w, h) if (w and h) else (w or h or 0)
        res_class = {2160: "4K", 1440: "2K", 1080: "1080p", 720: "720p",
                     540: "540p", 480: "480p", 360: "360p"}.get(short, str(short or ""))
        gear_label = f"{res_class}{fps}" if fps else res_class
        # openable-ссылка на этот гир: aweme/v1/play (открывается без 403)
        aweme_url = next((u for u in pa.get("UrlList", []) if "aweme/v1/play" in u), None)
        fmt = {
            'gear': b.get("GearName"), 'label': gear_label,
            'resolution': f"{w}x{h}" if w and h else "N/A",
            'height_num': h or 0, 'fps': fps,
            'bitrate': f"{br / 1_000_000:.1f} Mbps" if br else None,
            'codec': codec, 'size': _fmt_size(pa.get("DataSize")),
            'aweme_url': aweme_url,
        }
        formats_data.append(fmt)
        if best is None or fmt['height_num'] > best['height_num']:
            best = fmt
    formats_data.sort(key=lambda x: x['height_num'], reverse=True)

    cdn = _fetch_open_cdn(final_url)
    # Ссылка на звук: приоритет CDN-us (tikwm), запас — playUrl из веб-JSON
    music_url = (cdn or {}).get("music_url") or music.get("playUrl")
    labels = item.get("diversificationLabels") or []

    return {
        'uploader': uploader, 'uploader_url': uploader_url, 'unique_id': unique_id,
        'date_str': date_str, 'final_url': final_url, 'video_url': video_url, 'description': description,
        'music_title': music_title, 'music_dur': music_dur, 'music_url': music_url,
        'duration': video.get('duration'),
        'stats': stats, 'video_id': video_id,
        'region_code': region_code, 'region_name': region_name, 'region_flag': _flag(region_code),
        'shadow_str': shadow_str, 'orig_res': orig_res, 'vq_score': vq_score,
        'formats': formats_data, 'best': best, 'cdn': cdn, 'labels': labels,
        'raw': item,  # оригинальный JSON (itemStruct) для кнопки "JSON"
    }


# ───────────────────────────── ВЫВОД ─────────────────────────────
# Кэш оригинального JSON по video_id — чтобы отдать его по нажатию кнопки.
JSON_CACHE = {}


@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer("Отправь мне любую ссылку на TikTok, и я соберу аналитику")


@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok(message: types.Message):
    words = message.text.split()
    url = next((w for w in words if "tiktok.com" in w), message.text.strip())

    status_msg = await message.answer("Извлекаю аналитику...")
    d = await asyncio.to_thread(extract_tiktok_full_analytics, url)
    await status_msg.delete()

    if not d:
        await message.answer("Не удалось обработать ссылку. Проверьте, что видео открыто для всех.")
        return

    desc = html.escape(d['description'])
    nick = html.escape(d['uploader'])          # никнейм (напр. "тгк @kitorbuz")
    best = d['best']
    cdn = d.get('cdn') or {}
    vurl = d['video_url']

    def mbps(size_bytes):
        try:
            dur = d.get('duration')
            return f"{int(size_bytes) * 8 / dur / 1_000_000:.1f} Mbps" if size_bytes and dur else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    top_label = best['label'] if best else "—"

    lines = []
    # Заголовок — слово ВИДЕО ведёт на само видео
    lines.append(f"{em('video')} <b><a href='{vurl}'>ВИДЕО</a> • АНАЛИТИКА</b>\n")
    # Автор (ник — ссылка на автора) / дата (в <code>, на русском)
    lines.append(f"{em('user')} <b><a href='{d['uploader_url']}'>{nick}</a></b> • "
                 f"{em('calendar')} <b><code>{d['date_str']}</code></b>")
    # Описание — жирным, в цитате
    lines.append(f"<blockquote><b>{desc}</b></blockquote>")
    # Звук — ссылка на аудио с CDN
    music_link = d.get('music_url')
    zvuk = f"<a href='{music_link}'>Звук</a>" if music_link else "Звук"
    lines.append(f"{em('music')} <b>{zvuk} • {d['music_dur']}</b>\n")

    # Статистика — числа в <code>
    s = d['stats']
    lines.append(f"{em('stats')} <b>Статистика</b>")
    lines.append(f"• {em('eye')} <b><code>{s['views']:,}</code> Просмотры</b>")
    lines.append(f"• {em('heart')} <b><code>{s['likes']:,}</code> Лайки</b>")
    lines.append(f"• {em('comment')} <b><code>{s['comments']:,}</code> Комментарии</b>")
    lines.append(f"• {em('bookmark')} <b><code>{s['favorites']:,}</code> Избранные</b>")
    lines.append(f"• {em('repost')} <b><code>{s['shares']:,}</code> Репосты</b>")
    lines.append(f"• {em('download')} <b><code>{s['downloads']:,}</code> Скачивания</b>\n")

    # Информация — значения в <code>
    lines.append(f"{em('info')} <b>Информация</b>")
    lines.append(f"• {em('id')} <b>Айди | <code>{d['video_id']}</code></b>")
    lines.append(f"• {em('source')} <b>Источник | <a href='{vurl}'><code>Браузер</code></a></b>")
    lines.append(f"• {em('region')} <b>Регион | <code>{d['region_flag']} {html.escape(d['region_name'])}</code></b>")
    lines.append(f"• {em('ghost')} <b>Теневой бан | <code>{d['shadow_str']}</code></b>\n")

    # Качество — значения в <code>, показываем оба кодека (hevc + h264)
    lines.append(f"{em('star')} <b>Качество</b>")
    lines.append(f"• {em('globe')} <b>Браузер | <code>{top_label}</code></b>")
    lines.append(f"• {em('phone')} <b>Телефон | <code>{top_label}</code></b>")

    gear = (best or {}).get('gear') or "original"
    # Ссылки на качества собираем в одну цитату (как раньше)
    quality_block = []
    # HEVC: CDN-ссылка (tikwm hdplay) + browser-ссылка (aweme/v1/play из веб-JSON)
    hevc_cdn = cdn.get('hd_url')
    aweme = (best or {}).get('aweme_url')
    if hevc_cdn or aweme:
        head = f"{em('globe')}{em('phone')} <a href='{hevc_cdn or aweme}'>play_addr</a>"
        if aweme:
            head += f"  {em('globe')} <a href='{aweme}'>{gear}</a>"
        hevc_size = best['size'] if best and best.get('size') else _fmt_size(cdn.get('hd_size'))
        hevc_br = (best or {}).get('bitrate') or mbps(cdn.get('hd_size'))
        quality_block.append(f"{head}")
        quality_block.append(f"{top_label} • {hevc_br or '—'} • hevc • {hevc_size or '—'}")
    # H264: CDN-ссылка (tikwm play — без водяного знака, h264)
    h264_cdn = cdn.get('sd_url')
    if h264_cdn:
        h264_size = _fmt_size(cdn.get('sd_size'))
        h264_br = mbps(cdn.get('sd_size'))
        quality_block.append(f"{em('phone')} <a href='{h264_cdn}'>{gear}</a>")
        quality_block.append(f"{top_label} • {h264_br or '—'} • h264 • {h264_size or '—'}")
    if quality_block:
        lines.append("<blockquote><b>" + "\n".join(quality_block) + "</b></blockquote>")

    lines.append(f"<b>| Оригинал | <code>{d['orig_res']}</code></b>")
    lines.append(f"<b>| VQ Score | <code>{d['vq_score']}</code></b>\n")

    # Категории — значения в <code>
    if d['labels']:
        lines.append(f"{em('folder')} <b>Категории</b>")
        chunk = d['labels']
        for i in range(0, len(chunk), 2):
            part = ", ".join(html.escape(x) for x in chunk[i:i + 2])
            lines.append(f"<b>| <code>{part}</code></b>")
        lines.append("")

    # Подпись
    lines.append(f"{em('bolt')} <b>orbuz:TikTok Checker &amp; Downloader</b>")

    # Кэшируем оригинальный JSON и вешаем кнопку, которая его покажет
    vid = str(d['video_id'])
    JSON_CACHE[vid] = d.get('raw') or {}
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📄 JSON (оригинал)", callback_data=f"json:{vid}")
    ]])

    text = "\n".join(lines)
    await message.answer(
        text, parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("json:"))
async def show_json(callback: types.CallbackQuery):
    vid = callback.data.split(":", 1)[1]
    raw = JSON_CACHE.get(vid)
    if not raw:
        # Кэш мог очиститься (перезапуск бота) — просим прислать ссылку заново
        await callback.answer("JSON больше недоступен, отправьте ссылку заново.", show_alert=True)
        return

    pretty = json.dumps(raw, ensure_ascii=False, indent=2)
    await callback.answer()  # убираем «часики» на кнопке

    # JSON почти всегда длиннее лимита сообщения (4096) — отдаём файлом.
    file = BufferedInputFile(pretty.encode("utf-8"), filename=f"tiktok_{vid}.json")
    await callback.message.answer_document(
        file, caption=f"Оригинальный JSON • <code>{vid}</code>", parse_mode="HTML"
    )


async def main():
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот выключен.")
