#!/usr/bin/env python3
"""
F1 Calendar — News archive builder

Harvests F1 articles from formula1.com into static files so the browser never
talks to formula1.com: no CORS, and no scraping from visitors' IPs.

Generates:
  data/news/index.json   — the archive: every article found, plus translations
  news/<id>.html         — one standalone page per article, translation included

Why article pages and not links
-------------------------------
The official article pages are English-only, so following a link threw away the
translation. Each article is therefore copied to its own small HTML file on this
site, where the Russian text sits above the English original. The original
formula1.com URL is kept as a "read on F1.com" link.

Source
------
https://www.formula1.com/en/latest renders its list on the server, and the
Next.js payload in the HTML carries a clean JSON object per article: id, title,
slug, articleType, metaDescription, realUpdatedAt and a ready-made image URL.
Verified against the live page — the objects match the headlines the official F1
app shows. Article bodies are server-rendered <p> tags on the article page.

There is no documented F1 news API: api.f1.com does not resolve and the RSS
paths 404. Scraping the rendered pages is the only route to this content.

Growing the archive, and why it is capped
-----------------------------------------
The archive runs to hundreds of pages (page 100 was already May 2026), so every
run cannot walk all of it. Instead the walk starts at page 1 and stops after a
couple of consecutive pages that contain nothing new, so a quiet day costs two
requests. New articles per run are capped, and each one is stored permanently,
so the archive accumulates over weeks.

Translation
-----------
MyMemory, free and without an API key, and genuinely a neural model. The
anonymous quota is a few thousand words per day, which cannot cover a full
season of article bodies — nobody can, on a free tier. So each run has a word
budget: headlines first, then the newest article bodies. Translations are cached
in index.json, so nothing is ever paid for twice. Untranslated text keeps its
English original rather than showing a blank.

Personal, non-commercial use, same terms as the rest of this project.
"""

import gzip
import html
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
PAGES_DIR = PROJECT_DIR / "news"

SOURCE_URL = "https://www.formula1.com/en/latest"
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
UA = "F1-Calendar-Bot/1.3 (+github pages static site; personal, non-commercial)"

# ── harvest limits ──────────────────────────────────────────────────────────
# Never walk further than this in one run, however much is new.
MAX_PAGES_PER_RUN = 12
# Stop once this many pages in a row bring nothing we do not already have.
STOP_AFTER_EMPTY_PAGES = 2
# Most articles admitted per run. Kept modest on purpose: a busy weekend adds
# around a dozen, and anything beyond that can wait for the next run.
MAX_NEW_PER_RUN = 40
# Article bodies are a second request per article, so only a few are fetched per
# run. The newest articles are the ones worth opening.
BODY_FETCH_PER_RUN = 6
# Rough ceiling on the stored archive; oldest entries fall off the end.
MAX_STORED = 500

# ── translation limits ──────────────────────────────────────────────────────
# Anonymous MyMemory access allows a few thousand words a day. A run spends at
# most this many translation calls, headlines before bodies.
TRANSLATE_CALLS_PER_RUN = 25
BODY_TRANSLATE_LIMIT = 3
# MyMemory answers with HTTP 429 once the anonymous day quota is gone. Hammering
# it for the rest of the run achieves nothing, so three failures in a row stop
# translation for this run and the next one picks up where this left off.
TRANSLATE_FAIL_STREAK = 3

TRANSLATE_TIMEOUT = 20
PAUSE_BETWEEN_TRANSLATIONS = 0.3
PAUSE_BETWEEN_PAGES = 1.5
PAUSE_BETWEEN_ARTICLES = 1.0


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


# ── listing extraction ──────────────────────────────────────────────────────

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
    """Canonical F1 link. The id suffix is mandatory: without it F1 404s."""
    return f"https://www.formula1.com/en/latest/article/{slug}.{article_id}"


def entry_from_object(o: dict) -> dict:
    image = (o.get("thumbnail") or {}).get("image") or {}
    aid = o.get("id") or ""
    return {
        "id": aid,
        "slug": o.get("slug") or "",
        "title": clean_text(o.get("title")),
        "title_ru": "",
        "description": clean_text(o.get("metaDescription"))[:300],
        "description_ru": "",
        "type": (o.get("articleType") or "News").strip(),
        "published": o.get("realUpdatedAt") or o.get("updatedAt") or "",
        "url": article_url(o.get("slug") or "", aid),
        "image": image.get("url") or "",
        "image_alt": clean_text(image.get("title"))[:160],
        "page": f"news/{aid}.html" if aid else "",
        "body": [],
        "body_ru": [],
        "fetched_body": False,
    }


def harvest(known_ids: set) -> list:
    """Walk pages from the newest until nothing new turns up."""
    fresh = []
    seen = set()
    empty_streak = 0
    for page in range(1, MAX_PAGES_PER_RUN + 1):
        url = SOURCE_URL if page == 1 else f"{SOURCE_URL}?page={page}"
        try:
            page_html = fetch(url, timeout=60)
        except Exception as exc:
            log(f"страница {page} недоступна ({type(exc).__name__}), стоп")
            break
        objects = _extract_objects(page_html, 40)
        added = 0
        for o in objects:
            aid = o.get("id") or ""
            if not aid or aid in known_ids or aid in seen:
                continue
            seen.add(aid)
            fresh.append(entry_from_object(o))
            added += 1
            if len(fresh) >= MAX_NEW_PER_RUN:
                break
        log(f"страница {page}: новых {added}")
        if added == 0:
            empty_streak += 1
            if empty_streak >= STOP_AFTER_EMPTY_PAGES:
                log("дальше идти незачем — новых статей нет")
                break
        else:
            empty_streak = 0
        if len(fresh) >= MAX_NEW_PER_RUN:
            log(f"достигнут потолок в {MAX_NEW_PER_RUN} новых статей за запуск")
            break
        time.sleep(PAUSE_BETWEEN_PAGES)
    return fresh


# ── article bodies ──────────────────────────────────────────────────────────

# Only paragraphs that belong to the article itself. The page also renders the
# site chrome (menu, cookie banner, "opens in a new tab" link text) inside <p>
# tags, so a bare <p> match pulls navigation junk into the article.
_BODY_P_RE = re.compile(
    r'<p class="[^"]*typography-module_body-[^"]*"[^>]*>(.*?)</p>', re.S | re.I)
_ANY_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SKIP_PREFIXES = ("Image", "Getty", "©", "All images", "Read next", "Sign In",
                  "Live like an F1 insider", "Follow us", "Download the F1 app",
                  "Membership", "Member benefits", "Curated insider")

# Site chrome sometimes lands inside <p> tags. A prefix check was not enough:
# the menu paragraph begins with "(opens in a new tab)Sign In…", and the fallback
# "any <p>" matcher grabbed that instead of the article, producing two
# paragraphs of navigation where the story should have been.
_JUNK_RE = re.compile(
    r"opens in a new tab|ScheduleResultsStandings|Open menu|Search website|"
    r"Cookie|Accept all|Sign InSubscribe|F1 Unlocked", re.I)

TYPE_RU = {
    "News": "Новость", "Opinion": "Мнение", "Feature": "Фича",
    "Technical": "Технический", "Technical Article": "Технический",
    "Interview": "Интервью", "Video": "Видео", "Gallery": "Галерея",
    "Image Gallery": "Галерея", "Report": "Репортаж", "Live Blog": "Блог",
    "Podcast": "Подкаст", "Quiz": "Викторина", "Live": "Эфир",
    "Preview": "Превью", "Review": "Обзор", "Analysis": "Разбор",
    "Story": "История", "Guide": "Гид",
}


def fetch_body(entry: dict) -> bool:
    """Download the article text and store it as a list of paragraphs."""
    try:
        page_html = fetch(entry["url"], timeout=60)
    except Exception as exc:
        log(f"тело не получено ({type(exc).__name__}): {entry['title'][:40]}")
        return False
    paras = []
    # Prefer the article's own paragraphs; fall back to any <p> only if that
    # finds nothing, so a class rename degrades instead of losing the text.
    for matcher in (_BODY_P_RE, _ANY_P_RE):
        for raw in matcher.findall(page_html):
            text = _TAG_RE.sub("", raw)
            text = html.unescape(text).replace("’", "'").strip()
            if len(text) < 40 or text.startswith(_SKIP_PREFIXES):
                continue
            if _JUNK_RE.search(text):
                continue
            if text in paras:                 # mobile + desktop copies
                continue
            paras.append(text)
        if paras:
            break
    if not paras:
        return False
    entry["body"] = paras[:40]
    # Same length as body from the start: the translator writes into it by
    # index, and a shorter list would raise once a run runs out of budget.
    entry["body_ru"] = [""] * len(entry["body"])
    entry["fetched_body"] = True
    return True


# ── translation ─────────────────────────────────────────────────────────────

def translate(text: str) -> str:
    """Machine-translate en->ru, Google first and MyMemory as a fallback.

    Google is markedly better on F1 wording — on the same headline it produces
    "победы Ферстаппена в Сепанге" where MyMemory produced "победы Сепанга
    Ферстаппена", which reads as if Sepang won. The endpoint needs no key.

    It is an undocumented internal endpoint, so treat it as best-effort: if it
    stops answering, the fallback takes over, and if both fail the article keeps
    its English text.
    """
    if not text:
        return ""
    quoted = urllib.parse.quote(text)

    # 1) Google, no key.
    try:
        raw = fetch(f"{GOOGLE_URL}?client=gtx&sl=en&tl=ru&dt=t&q={quoted}",
                    timeout=TRANSLATE_TIMEOUT)
        data = json.loads(raw)
        out = "".join(part[0] for part in (data[0] or []) if part and part[0])
        if out.strip():
            return out.strip()
    except Exception:
        pass

    # 2) MyMemory fallback.
    try:
        raw = fetch(f"{MYMEMORY_URL}?q={quoted}&langpair=en|ru",
                    timeout=TRANSLATE_TIMEOUT, referer="https://www.formula1.com/")
        data = json.loads(raw)
    except Exception:
        return ""
    if str(data.get("responseStatus")) not in ("200", "200.0"):
        return ""
    out = (data.get("responseData") or {}).get("translatedText") or ""
    if not out or out.upper().startswith(("MYMEMORY WARNING", "INVALID",
                                          "QUERY LENGTH LIMIT", "YOU USED ALL AVAILABLE")):
        return ""
    return out.strip()


def translate_articles(articles: list, cache: dict) -> dict:
    """Spend at most TRANSLATE_CALLS_PER_RUN requests, headlines first."""
    cache = dict(cache or {})
    spent = 0
    reused = 0
    fails = 0

    def out_of_quota() -> bool:
        """True once the quota looks spent, to stop hammering the service."""
        return fails >= TRANSLATE_FAIL_STREAK

    # Headlines for everything, newest first.
    for a in articles:
        src = a.get("title") or ""
        if src and not cache.get("title::" + src) and not a.get("title_ru"):
            if spent >= TRANSLATE_CALLS_PER_RUN or out_of_quota():
                break
            ru = translate(src)
            spent += 1
            if ru:
                fails = 0
                a["title_ru"] = ru
                cache["title::" + src] = ru
                time.sleep(PAUSE_BETWEEN_TRANSLATIONS)
            else:
                fails += 1

    # Descriptions for the newest few, then bodies for the newest few.
    for index, a in enumerate(articles):
        for field, cap in (("description", BODY_TRANSLATE_LIMIT + 8),
                           ("body", BODY_TRANSLATE_LIMIT)):
            if field == "description" and index >= cap:
                continue
            if field == "body" and (index >= cap or not a.get("body")):
                continue
            srcs = [a.get(field) or ""] if field == "description" else a.get(field) or []
            dst_list = a.get(field + "_ru") or []
            for si, src in enumerate(srcs):
                src = src.strip()
                if not src or si >= len(dst_list):
                    continue
                key = field + "::" + src
                if cache.get(key):
                    dst_list[si] = cache[key]
                    reused += 1
                    continue
                if spent >= TRANSLATE_CALLS_PER_RUN or out_of_quota():
                    log("бюджет или квота исчерпаны, остальное — в следующий запуск")
                    log(f"переводов: новых {spent}, из кэша {reused}")
                    return cache
                ru = translate(src)
                spent += 1
                if ru:
                    fails = 0
                    cache[key] = ru
                    dst_list[si] = ru
                    time.sleep(PAUSE_BETWEEN_TRANSLATIONS)
                else:
                    fails += 1

    log(f"переводов: новых {spent}, из кэша {reused}")
    return cache


# ── static article pages ────────────────────────────────────────────────────

PAGE_CSS = """
:root{--bg:#0d0d0f;--surface:#16161a;--border:#2a2a30;--text:#f2f2f4;
--dim:#9a9aa5;--red:#e1062c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
a{color:#8ecbff}
.wrap{max-width:760px;margin:0 auto;padding:28px 18px 64px}
.back{display:inline-block;color:var(--dim);text-decoration:none;
font-size:.9rem;margin-bottom:18px}
.back:hover{color:var(--red)}
.tag{display:inline-block;background:rgba(225,6,30,.15);color:var(--red);
border:1px solid rgba(225,6,30,.35);border-radius:4px;padding:1px 8px;
font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.date{color:var(--dim);font-size:.82rem;margin-left:8px}
h1{font-size:1.7rem;line-height:1.25;margin:12px 0 6px}
.ru{font-size:1.05rem;font-weight:600;line-height:1.4;margin:10px 0}
.orig{color:var(--dim);font-size:.9rem;border-left:3px solid var(--border);
padding-left:12px;margin:10px 0 18px}
img{width:100%;border-radius:10px;margin:6px 0 18px;display:block}
.body p{margin:0 0 14px}
.note{margin-top:28px;padding-top:14px;border-top:1px solid var(--border);
color:var(--dim);font-size:.82rem}
"""


def write_pages(articles: list) -> int:
    """One standalone HTML file per article that has body text."""
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for a in articles:
        if not a.get("fetched_body") or not a.get("page"):
            continue
        ru = a.get("body_ru") or []
        paras = "".join(f"<p>{html.escape(p)}</p>" for p in (ru or a["body"]))
        ru_block = ""
        if ru:
            ru_block = (
                '<div class="body" lang="ru">' +
                "".join(f"<p>{html.escape(p)}</p>" for p in ru if p) +
                "</div>")
        orig_body = "".join(f"<p>{html.escape(p)}</p>" for p in a["body"])
        stamp = (a.get("published") or "")[:16].replace("T", " ")
        type_ru = TYPE_RU.get(a.get("type") or "", a.get("type") or "News")
        page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(a['title_ru'] or a['title'])} — Новости F1</title>
<meta name="description" content="{html.escape((a['description_ru'] or a['description'])[:180])}">
<style>{PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
<a class="back" href="../index.html">← Ко всем новостям</a>
<div><span class="tag">{html.escape(type_ru)}</span>
<span class="date">{html.escape(stamp)}</span></div>
<h1>{html.escape(a['title'])}</h1>
{f'<h1 lang="ru" style="font-size:1.25rem;color:#fff;margin:0 0 4px">{html.escape(a["title_ru"])}</h1>' if a.get('title_ru') else ''}
{ru_block}
{('<div class="orig" lang="en">' + orig_body + "</div>") if ru_block else ('<div class="body">' + orig_body + "</div>")}
<a href="{html.escape(a['url'])}" target="_blank" rel="noopener">Читать оригинал на formula1.com →</a>
<div class="note">Русский текст — машинный перевод, оригинал приведён рядом.
Источник: formula1.com. Некоммерческое личное использование.</div>
</div>
</body>
</html>
"""
        (PROJECT_DIR / a["page"]).write_text(page, encoding="utf-8", newline="\n")
        written += 1
    return written


def main() -> int:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)

    stored = []
    cache = {}
    if INDEX_PATH.exists():
        try:
            old = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            stored = old.get("articles") or []
            cache = old.get("translation_cache") or {}
            log(f"в архиве уже {len(stored)} статей, кэш переводов {len(cache)}")
        except Exception as exc:
            log(f"старый файл не читается ({type(exc).__name__})")

    known = {a["id"] for a in stored if a.get("id")}
    try:
        fresh = harvest(known)
    except Exception as exc:
        log(f"сбор не удался: {type(exc).__name__}: {exc}")
        return 1
    log(f"новых статей: {len(fresh)}")

    # Bodies first, so they are available for translation in the same run.
    need_body = [a for a in fresh][:BODY_FETCH_PER_RUN]
    got = 0
    for a in need_body:
        if fetch_body(a):
            got += 1
            time.sleep(PAUSE_BETWEEN_ARTICLES)
    log(f"тела получены для {got} из {len(need_body)}")

    merged = fresh + stored
    merged.sort(key=lambda a: a.get("published") or "", reverse=True)
    if len(merged) > MAX_STORED:
        merged = merged[:MAX_STORED]
    articles = merged

    cache = translate_articles(articles, cache)
    if len(cache) > 600:
        cache = dict(list(cache.items())[-600:])

    pages = write_pages(articles)
    log(f"страниц записано: {pages}")

    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": SOURCE_URL,
        "translation": "MyMemory (free, no key), neural machine translation",
        "articles": articles,
        "translation_cache": cache,
    }
    INDEX_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                          encoding="utf-8", newline="\n")
    with_body = sum(1 for a in articles if a.get("fetched_body"))
    log(f"готово: статей {len(articles)}, с телом {with_body}, "
        f"переведено заголовков {sum(1 for a in articles if a.get('title_ru'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())