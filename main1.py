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
# TikTok часто отдаёт капчу вместо video-detail JSON, если IP «засвечен»
# (в т.ч. домашний после активного парсинга). Прокси это лечит.
# Формат: "http://user:pass@host:port" или "socks5://user:pass@host:port"
# (для socks5 нужен пакет requests[socks]). Пусто ("") — ходить напрямую.
PROXY = "http://kQ7AIo0kf4:79mu5s2F9h@23.152.200.36:38194"

if PROXY:
    SESSION.proxies.update({"http": PROXY, "https": PROXY})

# Пробовать ли мобильный app-API (обогащение списка гиров через подпись X-Gorgon).
# Сейчас app-API требует ещё и X-Argus (нативный libsscronet.so), которого у нас
# нет -> запрос отдаёт пустой 200 и только тормозит каждый чек ожиданием. Держим
# ВЫКЛ; когда появится Argus — поставить True, остальной код уже готов.
USE_APP_API = False

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
    "refresh":   ("5877410604225924969", "🔄"),
}


def em(slot: str) -> str:
    """Возвращает премиум-эмодзи (tg-emoji) или обычный юникод в зависимости от настройки."""
    cid, fallback = EMOJI.get(slot, ("", "•"))
    if USE_PREMIUM_EMOJI and cid:
        return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>'
    return fallback


def emj(slot: str) -> str:
    """Только обычный юникод-символ (для текста кнопок — там HTML/премиум не парсится)."""
    return EMOJI.get(slot, ("", "•"))[1]


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


# ───────────────────── МОБИЛЬНЫЙ APP-API (подпись X-Gorgon) ─────────────────────
# Веб-JSON отдаёт максимум 5 гиров и не содержит «сырых»/HD-гиров и оригинала.
# Мобильный app-API TikTok богаче (bit_rate с fps/codec/is_bytevc1, чистые
# tiktokcdn.com-ссылки без 403). Он требует подписи запроса — считаем её на
# чистом Python через tiktok_signer (X-Gorgon/X-Khronos). Если API недоступен
# (rate-limit / смена схемы подписи) — тихо возвращаем None и работаем на веб-JSON.
try:
    from tiktok_signer import sign as _tt_sign
except Exception:  # signer недоступен — просто отключаем этот источник
    _tt_sign = None

# Хосты app-API: пробуем по очереди (часть отвечает пустым 200 при rate-limit).
_APP_HOSTS = ["api-normal.tiktokv.com", "api16-normal-c-useast1a.tiktokv.com",
              "api22-normal-c-useast2a.tiktokv.com", "api19-normal-c-useast1a.tiktokv.com"]
# Версия приложения, при которой app-API отдаёт полный bit_rate и принимает X-Gorgon.
_APP = {"app_name": "musical_ly", "aid": "1233", "vn": "26.1.3",
        "vc": "260103", "manifest": "2022601030"}
_APP_UA = (f"com.zhiliaoapp.musically/{_APP['manifest']} (Linux; U; Android 13; "
           "en_US; Pixel 7; Build/TD1A.220804.031; Cronet/58.0.2991.0)")

import time as _time, uuid as _uuid, random as _random, string as _string
from urllib.parse import urlencode as _urlencode


def _mstoken(n=107):
    return "".join(_random.choices(_string.ascii_letters + _string.digits + "-_", k=n))


def _app_query(aweme_id):
    n = int(_time.time())
    return {
        "aweme_id": aweme_id, "device_platform": "android", "os": "android", "ssmix": "a",
        "_rticket": n * 1000, "cdid": str(_uuid.uuid4()), "channel": "googleplay",
        "aid": _APP["aid"], "app_name": _APP["app_name"], "version_code": _APP["vc"],
        "version_name": _APP["vn"], "manifest_version_code": _APP["manifest"],
        "update_version_code": _APP["manifest"], "ab_version": _APP["vn"],
        "resolution": "1080*2400", "dpi": 420, "device_type": "Pixel 7",
        "device_brand": "Google", "language": "en", "os_api": "29", "os_version": "13",
        "ac": "wifi", "is_pad": "0", "current_region": "US", "app_type": "normal",
        "sys_region": "US", "last_install_time": n - _random.randint(86400, 1123200),
        "timezone_name": "America/New_York", "residence": "US", "app_language": "en",
        "timezone_offset": "-14400", "host_abi": "armeabi-v7a", "locale": "en",
        "ac2": "wifi5g", "uoo": "1", "carrier_region": "US", "op_region": "US",
        "build_number": _APP["vn"], "region": "US", "ts": n, "msToken": _mstoken(),
        "device_id": str(_random.randint(7250000000000000000, 7351147085025500000)),
        "openudid": "".join(_random.choices("0123456789abcdef", k=16)),
    }


def _fetch_app_item(aweme_id: str):
    """Подписанный запрос к app-API. Возвращает aweme_detail (dict) или None."""
    if not _tt_sign or not aweme_id or aweme_id == "N/A":
        return None
    for host in _APP_HOSTS:
        try:
            url = f"https://{host}/aweme/v1/aweme/detail/?" + _urlencode(_app_query(aweme_id))
            cookie = "odin_tt=" + "".join(_random.choices("0123456789abcdef", k=160))
            headers = _tt_sign(url, body=None, cookie=cookie)
            headers.update({"User-Agent": _APP_UA, "Accept": "application/json",
                            "Cookie": cookie})
            r = SESSION.get(url, headers=headers, timeout=15)
            if r.status_code != 200 or len(r.content) < 50:
                continue  # пустой 200 = rate-limit этого хоста, пробуем следующий
            j = r.json()
            aw = j.get("aweme_detail") or (j.get("aweme_list") or [None])[0]
            if aw and aw.get("video"):
                return aw
        except Exception as e:
            logging.warning(f"fetch_app_item {host}: {e}")
    return None


def _app_formats(aweme):
    """Разбирает bit_rate из app-API в тот же формат, что и веб-гиры.
    Возвращает (formats_list, best, orig_gear) или ([], None, None)."""
    video = (aweme or {}).get("video", {}) or {}
    bit_rate = video.get("bit_rate") or []
    if not bit_rate:
        return [], None, None
    formats, best, orig_gear = [], None, None
    for b in bit_rate:
        pa = b.get("play_addr", {}) or {}
        w, h = pa.get("width"), pa.get("height")
        fps = b.get("fps")
        br = b.get("bit_rate")
        codec = "hevc" if b.get("is_bytevc1") else "h264"
        short = min(w, h) if (w and h) else (w or h or 0)
        res_class = {2160: "4K", 1440: "2K", 1080: "1080p", 720: "720p", 576: "576p",
                     540: "540p", 480: "480p", 360: "360p", 240: "240p"}.get(
                         short, f"{short}p" if short else "")
        gear_label = f"{res_class}{fps}" if fps else res_class
        urls = list(pa.get("url_list", []) or [])
        fmt = {
            "gear": b.get("gear_name"), "label": gear_label,
            "resolution": f"{w}x{h}" if w and h else "N/A", "res_class": res_class,
            "height_num": h or 0, "fps": fps, "area": (w or 0) * (h or 0),
            "bitrate": f"{br / 1_000_000:.1f} Mbps" if br else None,
            "codec": codec, "size": _fmt_size(pa.get("data_size")),
            "size_bytes": pa.get("data_size"),
            # у app-API ссылки уже прямые (tiktokcdn.com), берём первую как основную
            "aweme_url": next((u for u in urls if "aweme/v1/play" in u), urls[0] if urls else None),
            "urls": urls,
        }
        formats.append(fmt)
        if best is None or fmt["height_num"] > best["height_num"]:
            best = fmt
        if orig_gear is None or fmt["area"] > orig_gear["area"]:
            orig_gear = fmt
    formats.sort(key=lambda x: x["height_num"], reverse=True)
    return formats, best, orig_gear


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


def _download_bytes(urls, referer="https://www.tiktok.com/"):
    """Пробует по очереди список URL, возвращает (bytes, content_type) первого,
    который отдал реальный медиа-контент (video/audio/image). None если все упали."""
    if isinstance(urls, str):
        urls = [urls]
    for u in urls:
        if not u:
            continue
        try:
            r = SESSION.get(u, headers={"Referer": referer}, timeout=40, stream=True)
            ct = r.headers.get("Content-Type", "")
            if r.status_code == 200 and ("video" in ct or "audio" in ct
                                         or "image" in ct or "octet-stream" in ct):
                return r.content, ct
            r.close()
        except Exception as e:
            logging.warning(f"download_bytes {u[:40]}: {e}")
    return None, None


# Флаг-эмодзи из кода страны (US -> 🇺🇸)
def _flag(code: str) -> str:
    if not code or len(code) != 2 or not code.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code.upper())


REGION_NAMES = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan",
    "AG": "Antigua and Barbuda", "AI": "Anguilla", "AL": "Albania", "AM": "Armenia",
    "AO": "Angola", "AQ": "Antarctica", "AR": "Argentina", "AS": "American Samoa",
    "AT": "Austria", "AU": "Australia", "AW": "Aruba", "AX": "Aland Islands",
    "AZ": "Azerbaijan", "BA": "Bosnia and Herzegovina", "BB": "Barbados",
    "BD": "Bangladesh", "BE": "Belgium", "BF": "Burkina Faso", "BG": "Bulgaria",
    "BH": "Bahrain", "BI": "Burundi", "BJ": "Benin", "BL": "Saint Barthelemy",
    "BM": "Bermuda", "BN": "Brunei", "BO": "Bolivia", "BQ": "Caribbean Netherlands",
    "BR": "Brazil", "BS": "Bahamas", "BT": "Bhutan", "BV": "Bouvet Island",
    "BW": "Botswana", "BY": "Belarus", "BZ": "Belize", "CA": "Canada",
    "CC": "Cocos (Keeling) Islands", "CD": "DR Congo", "CF": "Central African Republic",
    "CG": "Congo", "CH": "Switzerland", "CI": "Cote d'Ivoire", "CK": "Cook Islands",
    "CL": "Chile", "CM": "Cameroon", "CN": "China", "CO": "Colombia", "CR": "Costa Rica",
    "CU": "Cuba", "CV": "Cape Verde", "CW": "Curacao", "CX": "Christmas Island",
    "CY": "Cyprus", "CZ": "Czechia",
    "DE": "Germany", "DJ": "Djibouti", "DK": "Denmark", "DM": "Dominica",
    "DO": "Dominican Republic", "DZ": "Algeria", "EC": "Ecuador", "EE": "Estonia",
    "EG": "Egypt", "EH": "Western Sahara", "ER": "Eritrea", "ES": "Spain",
    "ET": "Ethiopia", "FI": "Finland", "FJ": "Fiji", "FK": "Falkland Islands",
    "FM": "Micronesia", "FO": "Faroe Islands", "FR": "France", "GA": "Gabon",
    "GB": "United Kingdom", "GD": "Grenada", "GE": "Georgia", "GF": "French Guiana",
    "GG": "Guernsey", "GH": "Ghana", "GI": "Gibraltar", "GL": "Greenland",
    "GM": "Gambia", "GN": "Guinea", "GP": "Guadeloupe", "GQ": "Equatorial Guinea",
    "GR": "Greece", "GS": "South Georgia", "GT": "Guatemala", "GU": "Guam",
    "GW": "Guinea-Bissau", "GY": "Guyana", "HK": "Hong Kong", "HM": "Heard Island",
    "HN": "Honduras", "HR": "Croatia", "HT": "Haiti", "HU": "Hungary",
    "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IM": "Isle of Man",
    "IN": "India", "IO": "British Indian Ocean Territory", "IQ": "Iraq", "IR": "Iran",
    "IS": "Iceland", "IT": "Italy",
    "JE": "Jersey", "JM": "Jamaica", "JO": "Jordan", "JP": "Japan", "KE": "Kenya",
    "KG": "Kyrgyzstan", "KH": "Cambodia", "KI": "Kiribati", "KM": "Comoros",
    "KN": "Saint Kitts and Nevis", "KP": "North Korea", "KR": "South Korea",
    "KW": "Kuwait", "KY": "Cayman Islands", "KZ": "Kazakhstan", "LA": "Laos",
    "LB": "Lebanon", "LC": "Saint Lucia", "LI": "Liechtenstein", "LK": "Sri Lanka",
    "LR": "Liberia", "LS": "Lesotho", "LT": "Lithuania", "LU": "Luxembourg",
    "LV": "Latvia", "LY": "Libya", "MA": "Morocco", "MC": "Monaco", "MD": "Moldova",
    "ME": "Montenegro", "MF": "Saint Martin", "MG": "Madagascar", "MH": "Marshall Islands",
    "MK": "North Macedonia", "ML": "Mali", "MM": "Myanmar", "MN": "Mongolia",
    "MO": "Macau", "MP": "Northern Mariana Islands", "MQ": "Martinique",
    "MR": "Mauritania", "MS": "Montserrat", "MT": "Malta", "MU": "Mauritius",
    "MV": "Maldives", "MW": "Malawi", "MX": "Mexico", "MY": "Malaysia", "MZ": "Mozambique",
    "NA": "Namibia", "NC": "New Caledonia", "NE": "Niger", "NF": "Norfolk Island",
    "NG": "Nigeria", "NI": "Nicaragua", "NL": "Netherlands", "NO": "Norway",
    "NP": "Nepal", "NR": "Nauru", "NU": "Niue", "NZ": "New Zealand", "OM": "Oman",
    "PA": "Panama", "PE": "Peru", "PF": "French Polynesia", "PG": "Papua New Guinea",
    "PH": "Philippines", "PK": "Pakistan", "PL": "Poland", "PM": "Saint Pierre and Miquelon",
    "PN": "Pitcairn Islands", "PR": "Puerto Rico", "PS": "Palestine", "PT": "Portugal",
    "PW": "Palau", "PY": "Paraguay", "QA": "Qatar", "RE": "Reunion", "RO": "Romania",
    "RS": "Serbia", "RU": "Russia", "RW": "Rwanda", "SA": "Saudi Arabia",
    "SB": "Solomon Islands", "SC": "Seychelles", "SD": "Sudan", "SE": "Sweden",
    "SG": "Singapore", "SH": "Saint Helena", "SI": "Slovenia", "SJ": "Svalbard and Jan Mayen",
    "SK": "Slovakia", "SL": "Sierra Leone", "SM": "San Marino", "SN": "Senegal",
    "SO": "Somalia", "SR": "Suriname", "SS": "South Sudan", "ST": "Sao Tome and Principe",
    "SV": "El Salvador", "SX": "Sint Maarten", "SY": "Syria", "SZ": "Eswatini",
    "TC": "Turks and Caicos Islands", "TD": "Chad", "TF": "French Southern Territories",
    "TG": "Togo", "TH": "Thailand", "TJ": "Tajikistan", "TK": "Tokelau",
    "TL": "Timor-Leste", "TM": "Turkmenistan", "TN": "Tunisia", "TO": "Tonga",
    "TR": "Turkey", "TT": "Trinidad and Tobago", "TV": "Tuvalu", "TW": "Taiwan",
    "TZ": "Tanzania", "UA": "Ukraine", "UG": "Uganda", "UM": "U.S. Minor Outlying Islands",
    "US": "United States", "UY": "Uruguay", "UZ": "Uzbekistan", "VA": "Vatican City",
    "VC": "Saint Vincent and the Grenadines", "VE": "Venezuela",
    "VG": "British Virgin Islands", "VI": "U.S. Virgin Islands", "VN": "Vietnam",
    "VU": "Vanuatu", "WF": "Wallis and Futuna", "WS": "Samoa", "XK": "Kosovo",
    "YE": "Yemen", "YT": "Mayotte", "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
}

# Русские названия месяцев (strftime %B зависит от локали ОС — делаем сами)
RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}

# С заглавной — для гибрид-режима ("11 Июня 2026")
RU_MONTHS_CAP = {
    1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля", 5: "Мая", 6: "Июня",
    7: "Июля", 8: "Августа", 9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря",
}


def humanize(n):
    """12000 -> 12.0K, 1500000 -> 1.5M (как в гибрид-режиме)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ─────────────────────────── РЕЖИМЫ / НАСТРОЙКИ ───────────────────────────
# Режим вывода на пользователя: "checker" (подробный текст) или "hybrid"
# (превью + компактная подпись + кнопки скачивания). Хранится в файле, чтобы
# переживать перезапуск. Меняется через /settings.
import os
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
DEFAULT_MODE = "hybrid"


def _load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"save_settings: {e}")


USER_MODE = _load_settings()


def get_mode(user_id) -> str:
    return USER_MODE.get(str(user_id), DEFAULT_MODE)


def set_mode(user_id, mode):
    USER_MODE[str(user_id)] = mode
    _save_settings(USER_MODE)


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

    # VQScore (оригинальное разрешение считаем ниже — по максимальному гиру,
    # т.к. video.width/height часто = размер дефолтного web-стрима, а не оригинала)
    vq_score = video.get("VQScore") or "0"

    # Качества с настоящим FPS (BitrateFPS)
    formats_data = []
    best = None          # максимум по высоте кадра (для "лучшее качество")
    orig_gear = None     # настоящий оригинал — максимум по площади (WxH)
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
        res_class = {2160: "4K", 1440: "2K", 1080: "1080p", 720: "720p", 576: "576p",
                     540: "540p", 480: "480p", 360: "360p", 240: "240p"}.get(
                         short, f"{short}p" if short else "")
        gear_label = f"{res_class}{fps}" if fps else res_class
        # openable-ссылка на этот гир: aweme/v1/play (открывается без 403)
        aweme_url = next((u for u in pa.get("UrlList", []) if "aweme/v1/play" in u), None)
        fmt = {
            'gear': b.get("GearName"), 'label': gear_label,
            'resolution': f"{w}x{h}" if w and h else "N/A",
            'res_class': res_class,
            'height_num': h or 0, 'fps': fps,
            'area': (w or 0) * (h or 0),
            'bitrate': f"{br / 1_000_000:.1f} Mbps" if br else None,
            'codec': codec, 'size': _fmt_size(pa.get("DataSize")),
            'size_bytes': pa.get("DataSize"),
            'aweme_url': aweme_url,
            'urls': list(pa.get("UrlList", [])),  # все ссылки этого гира (для скачивания)
        }
        formats_data.append(fmt)
        if best is None or fmt['height_num'] > best['height_num']:
            best = fmt
        if orig_gear is None or fmt['area'] > orig_gear['area']:
            orig_gear = fmt
    formats_data.sort(key=lambda x: x['height_num'], reverse=True)

    # Обогащение из мобильного app-API (подпись X-Gorgon): он отдаёт больше гиров
    # и чистые tiktokcdn.com-ссылки. Включается флагом USE_APP_API (см. верх файла).
    if USE_APP_API:
        app_aweme = _fetch_app_item(str(video_id))
        app_formats, app_best, app_orig = _app_formats(app_aweme)
        if app_formats:
            formats_data = app_formats
            best = app_best or best
            orig_gear = app_orig or orig_gear

    # Настоящее оригинальное разрешение: максимум среди гиров (video.width/height
    # часто = 576 из-за дефолтного web-playAddr), запас — размеры из video.
    if orig_gear and orig_gear.get('resolution') != "N/A":
        orig_res = orig_gear['resolution']
    else:
        orig_w, orig_h = video.get("width"), video.get("height")
        orig_res = f"{orig_w}x{orig_h}" if orig_w and orig_h else "N/A"

    # Качество для строки «Браузер»: дефолтный веб-стрим (h264 / normal_*),
    # для «Телефон» — максимальное (best). У примера: Браузер 576p30, Телефон 1080p59.
    browser_fmt = next((f for f in formats_data
                        if f['codec'] == 'h264' or 'normal' in (f.get('gear') or '')), None)
    browser_label = (browser_fmt or best or {}).get('label', "—")

    # Длительность видео в формате M:SS (именно видео, НЕ звука)
    _vd = video.get('duration')
    try:
        _vd = int(_vd)
        video_dur = f"{_vd // 60}:{_vd % 60:02d}"
    except (TypeError, ValueError):
        video_dur = "0:00"

    cdn = _fetch_open_cdn(final_url)
    # Ссылка на звук: приоритет CDN-us (tikwm), запас — playUrl из веб-JSON
    music_url = (cdn or {}).get("music_url") or music.get("playUrl")
    labels = item.get("diversificationLabels") or []

    # Обложка (превью) — самая качественная
    cover = (video.get("cover") or video.get("originCover")
             or video.get("dynamicCover") or "")

    # Короткая дата для гибрид-режима ("11 Июня 2026")
    try:
        dt2 = datetime.fromtimestamp(int(ts))
        date_short = f"{dt2.day} {RU_MONTHS_CAP[dt2.month]} {dt2.year}" if ts else "Неизвестно"
    except (TypeError, ValueError, KeyError):
        date_short = "Неизвестно"

    return {
        'uploader': uploader, 'uploader_url': uploader_url, 'unique_id': unique_id,
        'date_str': date_str, 'date_short': date_short,
        'final_url': final_url, 'video_url': video_url, 'description': description,
        'music_title': music_title, 'music_dur': music_dur, 'music_url': music_url,
        'duration': video.get('duration'), 'video_dur': video_dur, 'cover': cover,
        'stats': stats, 'video_id': video_id,
        'region_code': region_code, 'region_name': region_name, 'region_flag': _flag(region_code),
        'shadow_str': shadow_str, 'orig_res': orig_res, 'vq_score': vq_score,
        'formats': formats_data, 'best': best, 'orig_gear': orig_gear,
        'browser_label': browser_label, 'cdn': cdn, 'labels': labels,
        'raw': item,  # оригинальный JSON (itemStruct) для кнопки "JSON"
    }


# ───────────────────────────── ВЫВОД ─────────────────────────────
# Кэши по video_id: оригинальный JSON и полностью распарсенные данные (для кнопок).
JSON_CACHE = {}
DATA_CACHE = {}


def _uniq_download_formats(d):
    """Уникальные качества для кнопок скачивания — по одному на класс разрешения
    (берём вариант с наибольшим размером/битрейтом). Возвращает список fmt."""
    by_res = {}
    for f in d.get('formats', []) or []:
        key = f.get('res_class') or f.get('resolution')
        cur = by_res.get(key)
        if cur is None or (f.get('size_bytes') or 0) > (cur.get('size_bytes') or 0):
            by_res[key] = f
    return sorted(by_res.values(), key=lambda x: x['height_num'])


def _fmt_dl_urls(d, fmt):
    """Список URL для скачивания конкретного качества (по приоритету открываемости)."""
    cdn = d.get('cdn') or {}
    urls = []
    # aweme/v1/play обычно отдаётся напрямую
    if fmt.get('aweme_url'):
        urls.append(fmt['aweme_url'])
    # tikwm hd/sd — публичный CDN без 403
    if fmt.get('codec') == 'hevc' and cdn.get('hd_url'):
        urls.append(cdn['hd_url'])
    if cdn.get('sd_url'):
        urls.append(cdn['sd_url'])
    # прямые ссылки гира (webapp-prime) — как последний вариант
    urls += fmt.get('urls', [])
    return urls


def build_checker_text(d):
    """Собирает подробный текст режима «Чекер» + клавиатуру."""
    desc = html.escape(d['description'])
    nick = html.escape(d['uploader'])
    best = d['best']
    cdn = d.get('cdn') or {}
    vurl = d['video_url']
    vid = str(d['video_id'])

    def mbps(size_bytes):
        try:
            dur = d.get('duration')
            return f"{int(size_bytes) * 8 / dur / 1_000_000:.1f} Mbps" if size_bytes and dur else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    top_label = best['label'] if best else "—"

    lines = []
    lines.append(f"{em('video')} <b><a href='{vurl}'>ВИДЕО</a> • АНАЛИТИКА</b>\n")
    lines.append(f"{em('user')} <b><a href='{d['uploader_url']}'>{nick}</a></b> • "
                 f"{em('calendar')} <b><code>{d['date_str']}</code></b>")
    lines.append(f"<blockquote><b>{desc}</b></blockquote>")
    music_link = d.get('music_url')
    zvuk = f"<a href='{music_link}'>Звук</a>" if music_link else "Звук"
    lines.append(f"{em('music')} <b>{zvuk} • {d['music_dur']}</b>\n")

    s = d['stats']
    lines.append(f"{em('stats')} <b>Статистика</b>")
    lines.append(f"• {em('eye')} <b><code>{s['views']:,}</code> Просмотры</b>")
    lines.append(f"• {em('heart')} <b><code>{s['likes']:,}</code> Лайки</b>")
    lines.append(f"• {em('comment')} <b><code>{s['comments']:,}</code> Комментарии</b>")
    lines.append(f"• {em('bookmark')} <b><code>{s['favorites']:,}</code> Избранные</b>")
    lines.append(f"• {em('repost')} <b><code>{s['shares']:,}</code> Репосты</b>")
    lines.append(f"• {em('download')} <b><code>{s['downloads']:,}</code> Скачивания</b>\n")

    lines.append(f"{em('info')} <b>Информация</b>")
    lines.append(f"• {em('id')} <b>Айди | <code>{d['video_id']}</code></b>")
    lines.append(f"• {em('source')} <b>Источник | <a href='{vurl}'><code>Браузер</code></a></b>")
    lines.append(f"• {em('region')} <b>Регион | <code>{d['region_flag']} {html.escape(d['region_name'])}</code></b>")
    lines.append(f"• {em('ghost')} <b>Теневой бан | <code>{d['shadow_str']}</code></b>\n")

    lines.append(f"{em('star')} <b>Качество</b>")
    lines.append(f"• {em('globe')} <b>Браузер | <code>{d.get('browser_label') or top_label}</code></b>")
    lines.append(f"• {em('phone')} <b>Телефон | <code>{top_label}</code></b>")

    # ВСЕ качества, которые видит TikTok (каждый гир — отдельным блоком, через пустую строку)
    quality_entries = []
    for f in d.get('formats', []):
        link = f.get('aweme_url') or (f.get('urls') or [None])[0]
        gear = f.get('gear') or 'video'
        if link:
            head = f"{em('globe')}{em('phone')} <a href='{link}'>{gear}</a>"
        else:
            head = f"{em('globe')}{em('phone')} {gear}"
        detail = f"{f['label']} • {f.get('bitrate') or '—'} • {f['codec']} • {f.get('size') or '—'}"
        quality_entries.append(f"{head}\n{detail}")
    if quality_entries:
        lines.append("<blockquote expandable><b>" + "\n\n".join(quality_entries) + "</b></blockquote>")

    lines.append(f"<b>| Оригинал | <code>{d['orig_res']}</code></b>")
    lines.append(f"<b>| VQ Score | <code>{d['vq_score']}</code></b>\n")

    if d['labels']:
        lines.append(f"{em('folder')} <b>Категории</b>")
        chunk = d['labels']
        for i in range(0, len(chunk), 2):
            part = ", ".join(html.escape(x) for x in chunk[i:i + 2])
            lines.append(f"<b>| <code>{part}</code></b>")
        lines.append("")

    lines.append(f"{em('bolt')} <b>orbuz:TikTok Checker &amp; Downloader</b>")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{emj('refresh')} Перепроверить", callback_data=f"recheck:{vid}")],
        [InlineKeyboardButton(text="📄 JSON (оригинал)", callback_data=f"json:{vid}")],
    ])
    return "\n".join(lines), keyboard


def build_hybrid_text(d):
    """Компактная подпись режима «Гибрид» + клавиатура (превью-фото шлётся отдельно)."""
    desc = html.escape(d['description'])
    nick = html.escape(d['uploader'])
    vurl = d['video_url']
    vid = str(d['video_id'])
    s = d['stats']

    lines = []
    lines.append(f"{em('video')} <b>ВИДЕО • {d.get('video_dur') or d['music_dur']}</b>\n")
    lines.append(
        f"{em('user')}<b><a href='{d['uploader_url']}'>{nick}</a></b>  "
        f"{em('calendar')}<b><a href='{vurl}'>{d['date_short']}</a></b>  "
        f"{em('region')}<b>{d['region_flag']} {html.escape(d['region_name'])}</b>")
    lines.append(f"<blockquote><i>{desc}</i></blockquote>")
    lines.append(
        f"{em('eye')}<b>{humanize(s['views'])}</b> {em('heart')}<b>{humanize(s['likes'])}</b> "
        f"{em('comment')}<b>{humanize(s['comments'])}</b> {em('bookmark')}<b>{humanize(s['favorites'])}</b> "
        f"{em('repost')}<b>{humanize(s['shares'])}</b>\n")
    lines.append("<i>↓ Выберите действие</i>")

    # Кнопки: качества для скачивания (по 2 в ряд), Оригинал+MP3, Чекнуть, автор
    rows = []
    dls = _uniq_download_formats(d)
    row = []
    for i, f in enumerate(dls):
        idx = d['formats'].index(f)
        label = f"{emj('download')} {f['res_class']} • {f.get('size') or '—'}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"dl:{vid}:{idx}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="⚡ Оригинал", callback_data=f"orig:{vid}"),
        InlineKeyboardButton(text="🎵 MP3", callback_data=f"mp3:{vid}"),
    ])
    rows.append([InlineKeyboardButton(text=f"{emj('stats')} Чекнуть", callback_data=f"check:{vid}")])
    rows.append([InlineKeyboardButton(text=f"{emj('user')} {nick}", url=d['uploader_url'])])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    return "\n".join(lines), keyboard


async def _send_result(message, d):
    """Отправляет результат в зависимости от режима пользователя."""
    vid = str(d['video_id'])
    JSON_CACHE[vid] = d.get('raw') or {}
    DATA_CACHE[vid] = d

    mode = get_mode(message.chat.id)
    if mode == "hybrid":
        text, keyboard = build_hybrid_text(d)
        cover = d.get('cover')
        photo_bytes = None
        if cover:
            photo_bytes, _ = await asyncio.to_thread(_download_bytes, cover)
        if photo_bytes:
            await message.answer_photo(
                BufferedInputFile(photo_bytes, filename=f"{vid}.jpg"),
                caption=text, parse_mode="HTML", reply_markup=keyboard)
        else:
            # обложку не достали — шлём без фото
            await message.answer(text, parse_mode="HTML", reply_markup=keyboard,
                                 link_preview_options=LinkPreviewOptions(is_disabled=True))
    else:
        text, keyboard = build_checker_text(d)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard,
                             link_preview_options=LinkPreviewOptions(is_disabled=True))


@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer("Отправь мне любую ссылку на TikTok, и я соберу аналитику.\n"
                         "Режим вывода меняется командой /settings")


@dp.message(F.text == "/settings")
async def settings_cmd(message: types.Message):
    mode = get_mode(message.chat.id)
    mark = lambda m: "✅ " if mode == m else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{mark('hybrid')}Гибрид", callback_data="mode:hybrid")],
        [InlineKeyboardButton(text=f"{mark('checker')}Чекер", callback_data="mode:checker")],
    ])
    await message.answer(
        "<b>⚙️ Настройки — режим вывода</b>\n\n"
        "• <b>Гибрид</b> — превью видео, компактная инфо и кнопки скачивания\n"
        "• <b>Чекер</b> — подробная аналитика со всеми качествами",
        parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("mode:"))
async def switch_mode(callback: types.CallbackQuery):
    mode = callback.data.split(":", 1)[1]
    set_mode(callback.message.chat.id, mode)
    name = "Гибрид" if mode == "hybrid" else "Чекер"
    await callback.answer(f"Режим: {name}")
    mark = lambda m: "✅ " if mode == m else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{mark('hybrid')}Гибрид", callback_data="mode:hybrid")],
        [InlineKeyboardButton(text=f"{mark('checker')}Чекер", callback_data="mode:checker")],
    ])
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


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

    await _send_result(message, d)


@dp.callback_query(F.data.startswith("recheck:"))
async def recheck(callback: types.CallbackQuery):
    vid = callback.data.split(":", 1)[1]
    d = DATA_CACHE.get(vid)
    url = (d or {}).get('video_url')
    if not url:
        await callback.answer("Данные устарели, отправьте ссылку заново.", show_alert=True)
        return
    await callback.answer("Перепроверяю…")
    d2 = await asyncio.to_thread(extract_tiktok_full_analytics, url)
    if not d2:
        await callback.answer("Не удалось перепроверить.", show_alert=True)
        return
    DATA_CACHE[vid] = d2
    JSON_CACHE[vid] = d2.get('raw') or {}
    text, keyboard = build_checker_text(d2)
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception:
        # если сообщение с фото (гибрид) — редактировать текст нельзя, шлём новое
        await callback.message.answer(
            text, parse_mode="HTML", reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True))


@dp.callback_query(F.data.startswith("check:"))
async def check_action(callback: types.CallbackQuery):
    vid = callback.data.split(":", 1)[1]
    d = DATA_CACHE.get(vid)
    if not d:
        await callback.answer("Данные устарели, отправьте ссылку заново.", show_alert=True)
        return
    await callback.answer()
    text, keyboard = build_checker_text(d)
    await callback.message.answer(
        text, parse_mode="HTML", reply_markup=keyboard,
        link_preview_options=LinkPreviewOptions(is_disabled=True))


@dp.callback_query(F.data.startswith("dl:"))
async def download_quality(callback: types.CallbackQuery):
    _, vid, idx = callback.data.split(":")
    d = DATA_CACHE.get(vid)
    if not d:
        await callback.answer("Данные устарели, отправьте ссылку заново.", show_alert=True)
        return
    try:
        fmt = d['formats'][int(idx)]
    except (IndexError, ValueError):
        await callback.answer("Качество недоступно.", show_alert=True)
        return
    await callback.answer("Скачиваю…")
    data, _ = await asyncio.to_thread(_download_bytes, _fmt_dl_urls(d, fmt))
    if not data:
        await callback.answer("Не удалось скачать это качество.", show_alert=True)
        return
    fname = f"{d['unique_id']}_{fmt.get('res_class') or 'video'}.mp4"
    await callback.message.answer_video(
        BufferedInputFile(data, filename=fname),
        caption=f"{fmt['label']} • {fmt['codec']} • {fmt.get('size') or ''}")


@dp.callback_query(F.data.startswith("orig:"))
async def download_original(callback: types.CallbackQuery):
    vid = callback.data.split(":", 1)[1]
    d = DATA_CACHE.get(vid)
    if not d:
        await callback.answer("Данные устарели, отправьте ссылку заново.", show_alert=True)
        return
    await callback.answer("Скачиваю оригинал…")
    cdn = d.get('cdn') or {}
    # «Оригинал» = гир с максимальной площадью кадра (orig_gear), запас — best.
    orig = d.get('orig_gear') or d.get('best') or {}
    urls = []
    if orig.get('aweme_url'):
        urls.append(orig['aweme_url'])
    urls += (orig.get('urls') or [])
    urls += [cdn.get('hd_url'), cdn.get('sd_url')]
    data, _ = await asyncio.to_thread(_download_bytes, urls)
    if not data:
        await callback.answer("Не удалось скачать оригинал.", show_alert=True)
        return
    await callback.message.answer_document(
        BufferedInputFile(data, filename=f"{d['unique_id']}_original.mp4"),
        caption=f"Оригинал • {d.get('orig_res') or ''}")


@dp.callback_query(F.data.startswith("mp3:"))
async def download_mp3(callback: types.CallbackQuery):
    vid = callback.data.split(":", 1)[1]
    d = DATA_CACHE.get(vid)
    if not d:
        await callback.answer("Данные устарели, отправьте ссылку заново.", show_alert=True)
        return
    if not d.get('music_url'):
        await callback.answer("Ссылка на звук недоступна.", show_alert=True)
        return
    await callback.answer("Скачиваю звук…")
    data, _ = await asyncio.to_thread(_download_bytes, d['music_url'])
    if not data:
        await callback.answer("Не удалось скачать звук.", show_alert=True)
        return
    await callback.message.answer_audio(
        BufferedInputFile(data, filename=f"{d['unique_id']}.mp3"),
        title=d.get('music_title') or "TikTok audio")


@dp.callback_query(F.data.startswith("json:"))
async def show_json(callback: types.CallbackQuery):
    vid = callback.data.split(":", 1)[1]
    raw = JSON_CACHE.get(vid)
    if not raw:
        await callback.answer("JSON больше недоступен, отправьте ссылку заново.", show_alert=True)
        return
    pretty = json.dumps(raw, ensure_ascii=False, indent=2)
    await callback.answer()
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
