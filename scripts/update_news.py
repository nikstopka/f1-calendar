#!/usr/bin/env python3
"""
F1 Calendar — News tab builder

Scrapes the article list from formula1.com and writes static JSON the News tab
can load, so the browser never talks to formula1.com (no CORS, no scraping from
visitors' IPs).

Generates:
  data/news/index.json  — latest articles, original + machine translation

Source
------
https://www.formula1.com/en/latest renders its article list on the server, and
the Next.js payload embedded in the HTML carries a clean JSON object per
article: id, title, slug, articleType, metaDescription, updatedAt and a
thumbnail. Verified against the live page: the objects match the headlines the
official F1 app shows.

There is no documented F1 news API — `api.f1.com` does not resolve and the RSS
paths 404. Scraping the rendered page is the only route to the same content the
app shows, so that is what this uses.

Translation
-----------
MyMemory, free and without an API key. Translations are cached in the output
file and only new strings are ever sent: the anonymous quota is a few thousand
words per day, and a race weekend adds roughly a dozen short headlines. If a
call fails the article keeps its English text rather than an invented Russian
one.

Machine translation is not a human translator. F1 jargon trips it up — "Sepang"
becomes "Сепанга" instead of "Сепанг" — so the News tab shows the Russian line
with the original English underneath, always.

Personal, non-commercial use, same terms as the rest of this project.
"""

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
NEWS_DIR = PROJECT_DIR / "data" / "news"
INDEX_PATH = NEWS_DIR / "index.json"

SOURCE_URL = "https://www.formula1.com/en/latest"
TRANSLATE_URL = "https://api.mymemory.translated.net/get"
UA = "F1-Calendar-Bot/1.2 (+github pages static site; personal, non-commercial)"

MAX_ARTICLES = 60
# formula1.com serves 16 articles per page and the archive runs to at least
# 40 pages. Fetching all of it on every run would be rude and pointless: news
# older than a few days is never read here. Five pages keeps each run at ten
# requests, and the extra pages only matter right after a weekend.
PAGES_TO_FETCH = 5
# MyMemory's anonymous quota is a few thousand words per day. A cold backfill of
# 60 titles plus a dozen descriptions lands around 1 400 words, which fits; and
# because translations are cached, later runs only pay for genuinely new text.
DESC_TRANSLATE_LIMIT = 12
TRANSLATE_TIMEOUT = 20
PAUSE_BETWEEN_TRANSLATIONS = 0.35
PAUSE_BETWEEN_PAGES = 1.5


def log(msg: str) -> None:
    print(f"[news] {msg}", flush=True)


def fetch(url: str, *, timeout: int = 60, referer: str = "") -> str:
    """GET a URL and return decoded text, transparently gunzipping."""
    headers = {"User-Agent": UA, "Accept": "text/html,application/json,*/*",
               "Accept-Encoding": "gzip"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


# ── article extraction ──────────────────────────────────────────────────────
# The payload is a Next.js RSC stream: JSON with backslash-escaped quotes. The
# objects are found by brace matching from a field that only article objects
# carry, then unescaped and parsed.

_ANCHOR = "realUpdatedAt"


def _unescape(text: str) -> str:
    return (text.replace("\\\\", "\x00")
                .replace('\\"', '"')
                .replace("\\u003c", "<")
                .replace("\\u003e", ">")
                .replace("\x00", "\\"))


def _extract_objects(text: str, limit: int) -> list:
    out = []
    pos = 0
    while len(out) < limit:
        i = text.find(_ANCHOR, pos)
        if i < 0:
            break
        start = text.rfind("{", 0, i)
        if start < 0:
            break
        depth = 0
        end = None
        for j in range(start, min(len(text), start + 400_000)):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end is None:
            break
        try:
            obj = json.loads(_unescape(text[start:end]))
        except Exception:
            obj = None
        if isinstance(obj, dict) and obj.get("slug"):
            out.append(obj)
        pos = end
    return out


def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").replace("’", "'").replace("‘", "'"))
    return s.strip()


def article_url(slug: str, article_id: str) -> str:
    """The canonical article link.

    The id suffix is mandatory: formula1.com 404s on /article/<slug> without it,
    so both parts are needed.
    """
    return ("https://www.formula1.com/en/latest/article/" + slug + "." + article_id)


def scrape_articles() -> list:
    """Walk the paginated list and collect unique articles."""
    seen = set()
    out = []
    for page in range(1, PAGES_TO_FETCH + 1):
        url = SOURCE_URL if page == 1 else f"{SOURCE_URL}?page={page}"
        try:
            html = fetch(url, timeout=60)
        except Exception as exc:
            log(f"страница {page} не получена ({type(exc).__name__}), "
                f"останавливаюсь на {len(out)} статьях")
            break
        objects = _extract_objects(html, MAX_ARTICLES * 2)
        added = 0
        for o in objects:
            slug = o.get("slug") or ""
            title = clean_text(o.get("title"))
            if not slug or not title or slug in seen:
                continue
            seen.add(slug)
            thumb = o.get("thumbnail") or {}
            image = thumb.get("image") or {}
            # The article object carries a ready-made transformed image URL.
            # Building one from `path` does not work: the Cloudinary-style
            # transform differs per image, and a guessed prefix 404s.
            image_url = image.get("url") or ""
            aid = o.get("id") or ""
            out.append({
                "id": aid,
                "title": title,
                "title_ru": "",
                "description": clean_text(o.get("metaDescription"))[:300],
                "description_ru": "",
                "type": (o.get("articleType") or "News").strip(),
                "published": o.get("realUpdatedAt") or o.get("updatedAt") or "",
                "url": article_url(slug, aid),
                "image": image_url,
                "image_alt": clean_text(image.get("title"))[:160],
            })
            added += 1
            if len(out) >= MAX_ARTICLES * 2:
                break
        log(f"страница {page}: объектов {len(objects)}, добавлено {added}")
        if added == 0:
            break
        if page < PAGES_TO_FETCH:
            time.sleep(PAUSE_BETWEEN_PAGES)

    out.sort(key=lambda a: a["published"], reverse=True)
    out = out[:MAX_ARTICLES]
    log(f"статей уникальных: {len(seen)}, сохранено: {len(out)}")
    return out


# ── translation ─────────────────────────────────────────────────────────────

def translate(text: str) -> str:
    """Machine-translate en->ru. Returns '' on any failure, never a guess."""
    if not text:
        return ""
    q = urllib.parse.quote(text)
    url = f"{TRANSLATE_URL}?q={q}&langpair=en|ru"
    try:
        raw = fetch(url, timeout=TRANSLATE_TIMEOUT, referer="https://www.formula1.com/")
        data = json.loads(raw)
    except Exception as exc:
        log(f"перевод не удался ({type(exc).__name__})")
        return ""
    if str(data.get("responseStatus")) not in ("200", "200.0"):
        return ""
    out = (data.get("responseData") or {}).get("translatedText") or ""
    if not out or out.upper().startswith(("MYMEMORY WARNING", "INVALID", "QUERY LENGTH LIMIT")):
        return ""
    return out.strip()


def apply_translations(articles: list, cache: dict) -> dict:
    """Fill in Russian text, reusing the cache so only new strings are sent.

    Titles are translated for every article because they are short and they are
    what the tab shows. Descriptions run to ~40 words each, so only the newest
    few are sent — that keeps a cold backfill inside the free daily quota while
    still giving the top cards a lead paragraph.
    """
    cache = dict(cache or {})
    hits = 0
    misses = 0
    for index, a in enumerate(articles):
        wanted = [("title", True)]
        if index < DESC_TRANSLATE_LIMIT:
            wanted.append(("description", True))
        for field, _ in wanted:
            src = a.get(field) or ""
            dst_field = field + "_ru"
            cached = cache.get(field + "::" + src)
            if cached:
                a[dst_field] = cached
                hits += 1
                continue
            if not src:
                a[dst_field] = ""
                continue
            ru = translate(src)
            if ru:
                a[dst_field] = ru
                cache[field + "::" + src] = ru
                misses += 1
                time.sleep(PAUSE_BETWEEN_TRANSLATIONS)
            else:
                # English stays visible in the tab, so an empty translation
                # degrades gracefully instead of showing a blank line.
                a[dst_field] = ""
    log(f"переводов из кэша: {hits}, новых запросов: {misses}")
    return cache


def main() -> int:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)

    cache = {}
    if INDEX_PATH.exists():
        try:
            old = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            cache = old.get("translation_cache") or {}
            log(f"кэш переводов загружен: {len(cache)} строк")
        except Exception as exc:
            log(f"старый файл не читается ({type(exc).__name__}), начинаю с нуля")

    try:
        articles = scrape_articles()
    except Exception as exc:
        log(f"не удалось получить новости: {type(exc).__name__}: {exc}")
        return 1
    if not articles:
        log("подходящих статей не найдено — разметка сайта могла измениться")
        return 1

    cache = apply_translations(articles, cache)

    # Keep the cache bounded; it only exists to avoid re-translating known text.
    if len(cache) > 400:
        cache = dict(list(cache.items())[-400:])

    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": SOURCE_URL,
        "translation": "MyMemory (free, no key), machine translation",
        "articles": articles,
        "translation_cache": cache,
    }
    # newline="" keeps the output LF everywhere. On Windows the default would
    # write CRLF, so every local run would show a diff against the committed file
    # even when nothing changed.
    INDEX_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                          encoding="utf-8", newline="\n")
    log(f"записано {INDEX_PATH.relative_to(PROJECT_DIR)}: {len(articles)} статей")

    first = articles[0]
    log(f"первая: {first['title'][:70]}")
    if first.get("title_ru"):
        log(f"  ru: {first['title_ru'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())