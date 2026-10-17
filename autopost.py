#!/usr/bin/env python3
"""
Автопостинг новостей DLE-сайтов в Telegram-каналы через RSS.
Запускается по расписанию в GitHub Actions, поэтому не зависит от того,
пускает ли хостинг сайта к api.telegram.org.

Вид поста: фото из og:image страницы новости, жирный заголовок-ссылка,
пустая строка, краткий текст из RSS.
"""
import html
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests
from PIL import Image

# --- Настройки сайтов -------------------------------------------------------
# start_after_id: новости с ID меньше или равным этому не публикуются никогда
# (защита от повторной публикации старых новостей при первом запуске).
#
# Токен бота: скрипт берёт секрет сайта (token_secret), а если его нет —
# общий секрет TG_TOKEN. Если один бот ведёт все каналы, достаточно TG_TOKEN.
SITES = [
    {
        "name": "nba365",
        "rss": "https://nba365.pro/rss.xml",
        "chat_id": "@nba365_pro",
        "token_secret": "TG_TOKEN_NBA365",
        "start_after_id": 355,   # публиковать только новости новее №355
    },
    {
        "name": "nhl365",
        "rss": "https://nhl365.pro/rss.xml",
        "chat_id": "@nhl365_pro",
        "token_secret": "TG_TOKEN_NHL365",
        "start_after_id": 208,   # последний пост в канале — №208, догонит №209–218
    },
    {
        "name": "f1news365",
        "rss": "https://f1news365.ru/rss.xml",
        "chat_id": "@f1news365_ru",
        "token_secret": "TG_TOKEN_F1NEWS365",
        "start_after_id": 387,   # последний пост в канале — №387, пропусков нет
    },
    {
        "name": "nfl365",
        "rss": "https://nfl365.pro/rss.xml",
        "chat_id": "@nfl365_pro",
        "token_secret": "TG_TOKEN_NFL365",
        "start_after_id": 207,   # последний пост в канале — №207, пропусков нет
    },
    {
        "name": "epl365",
        "rss": "https://epl365.pro/rss.xml",
        "chat_id": "@epl365_pro",
        "token_secret": "TG_TOKEN_EPL365",
        "start_after_id": 171,   # последний пост в канале — №171, догонит №172–175
    },
]

STATE_FILE = "posted.json"
CAPTION_LIMIT = 1000          # у Telegram лимит подписи к фото 1024 символа
MAX_ATTEMPTS = 3              # после стольких неудач новость пропускается
PAUSE_BETWEEN_POSTS = 3       # секунд, чтобы не упереться в лимиты Telegram
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tg-autopost/1.0)"}


# --- Состояние --------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# --- RSS и страница новости -------------------------------------------------
def news_id(link):
    m = re.search(r"/(\d+)-[^/]*\.html", link)
    return int(m.group(1)) if m else None


def clean_text(desc_html):
    s = desc_html or ""
    s = re.sub(r"(?i)</p>\s*<p[^>]*>", "\n\n", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def parse_rss(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        nid = news_id(link)
        if nid is None:
            continue
        items.append({
            "id": nid,
            "title": html.unescape((it.findtext("title") or "").strip()),
            "link": link,
            "text": clean_text(it.findtext("description")),
        })
    items.sort(key=lambda x: x["id"])
    return items


def find_og_image(page_html):
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)',
    ]
    for p in patterns:
        m = re.search(p, page_html, re.I)
        if m:
            return html.unescape(m.group(1))
    return None


def prepare_photo(img_bytes):
    """Любой формат (в т.ч. webp) -> JPEG, который Telegram примет как фото."""
    img = Image.open(io.BytesIO(img_bytes))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((2560, 2560))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def get_photo(link):
    page = requests.get(link, headers=HEADERS, timeout=30)
    page.raise_for_status()
    img_url = find_og_image(page.text)
    if not img_url:
        return None
    r = requests.get(img_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return prepare_photo(r.content)


# --- Текст поста ------------------------------------------------------------
def tg_len(s):
    return len(s.encode("utf-16-le")) // 2


def build_caption(title, link, text, limit=CAPTION_LIMIT):
    head = f'<b><a href="{html.escape(link)}">{html.escape(title, quote=False)}</a></b>'
    if not text:
        return head
    room = limit - tg_len(title) - 2
    if tg_len(text) > room:
        cut = text[:max(room - 1, 0)]
        if " " in cut:
            cut = cut[:cut.rfind(" ")]
        text = cut.rstrip(" ,.;:—-") + "…"
    return head + "\n\n" + html.escape(text, quote=False)


# --- Telegram ---------------------------------------------------------------
def tg_call(token, method, data, files=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    for _ in range(3):
        r = requests.post(url, data=data, files=files, timeout=60)
        resp = r.json()
        if resp.get("ok"):
            return resp
        retry = (resp.get("parameters") or {}).get("retry_after")
        if r.status_code == 429 and retry:
            time.sleep(int(retry) + 1)
            continue
        raise RuntimeError(f"{method}: {resp.get('description')}")
    raise RuntimeError(f"{method}: слишком много запросов")


def post_item(token, chat_id, item):
    caption = build_caption(item["title"], item["link"], item["text"])
    photo = None
    try:
        photo = get_photo(item["link"])
    except Exception as e:
        print(f"  картинку взять не удалось ({e}), отправлю без фото")

    if photo:
        try:
            return tg_call(token, "sendPhoto",
                           {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                           files={"photo": ("photo.jpg", photo, "image/jpeg")})
        except RuntimeError as e:
            print(f"  sendPhoto не прошёл ({e}), отправлю текстом")

    return tg_call(token, "sendMessage",
                   {"chat_id": chat_id, "text": caption, "parse_mode": "HTML"})


# --- Основной цикл ----------------------------------------------------------
def run_site(site, state):
    name = site["name"]
    token = (os.environ.get(site["token_secret"], "").strip()
             or os.environ.get("TG_TOKEN", "").strip())
    if not token:
        print(f"[{name}] нет секрета {site['token_secret']} или TG_TOKEN, пропускаю")
        return 0

    st = state.setdefault(name, {"posted": [], "failed": {}})
    posted = set(st["posted"])

    try:
        r = requests.get(site["rss"], headers=HEADERS, timeout=30)
        r.raise_for_status()
        items = parse_rss(r.content)
    except Exception as e:
        print(f"[{name}] RSS недоступна: {e}")
        return 0

    errors = 0
    for item in items:
        nid = item["id"]
        if nid <= site["start_after_id"] or nid in posted:
            continue
        print(f"[{name}] #{nid}: {item['title']}")
        try:
            post_item(token, site["chat_id"], item)
            posted.add(nid)
            st["failed"].pop(str(nid), None)
            print("  опубликовано")
        except Exception as e:
            errors += 1
            n = st["failed"].get(str(nid), 0) + 1
            st["failed"][str(nid)] = n
            print(f"  ОШИБКА ({n}/{MAX_ATTEMPTS}): {e}")
            if n >= MAX_ATTEMPTS:
                posted.add(nid)
                print("  попытки исчерпаны, новость пропущена")
        st["posted"] = sorted(posted)[-300:]
        save_state(state)
        time.sleep(PAUSE_BETWEEN_POSTS)
    return errors


def main():
    state = load_state()
    errors = sum(run_site(site, state) for site in SITES)
    save_state(state)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
