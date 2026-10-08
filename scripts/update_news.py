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

import datetime
import email.utils
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
# Article text lives beside the listing, not inside it. index.json is what every
# visitor downloads to draw the news grid, and it needs headlines only. bodies.json
# is the bot's own working copy: keeping it separate is what allows translation to
# resume across runs instead of restarting on the freshly fetched articles.
BODIES_PATH = NEWS_DIR / "bodies.json"
PAGES_DIR = PROJECT_DIR / "news"
FEED_PATH = PROJECT_DIR / "feed.xml"
# Absolute base for the feed: readers resolve relative links against the feed's
# own location, so the link has to be the public address, not a local path.
SITE_URL = "https://nikstopka.github.io/f1-calendar/"

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
BODY_FETCH_PER_RUN = 8
# Ceiling on stored blocks per article (paragraphs plus photos and videos).
BLOCKS_LIMIT = 90
# Rough ceiling on the stored archive; oldest entries fall off the end.
MAX_STORED = 500

# ── translation limits ──────────────────────────────────────────────────────
# Anonymous MyMemory access allows a few thousand words a day. A run spends at
# most this many translation calls, headlines before bodies.
# The cap existed because MyMemory's anonymous tier allows only a few thousand
# words a day. Google, tried first, has no such quota, so this is now a guard
# against runaway loops rather than a quota limit.
TRANSLATE_CALLS_PER_RUN = 150
# How many of the newest articles get their body translated. It is an article
# cap, not a paragraph budget — the comment above it once said the opposite, and
# that is how the archive grew past it: with the limit at 40 and 52 articles on
# file, the twelve oldest were silently never translated, block after block,
# while every run reported success.
BODY_TRANSLATE_LIMIT = 200
# Which extractor produced an article's stored blocks. Bump it whenever
# _article_blocks() learns to read something new; every article whose stamp is
# older is rebuilt on the next run.
BLOCKS_VERSION = 2
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
# A bare "$37" is the RSC protocol's way of pointing at another chunk. It looks
# like text to a string scan but carries no words of its own.
_FLIGHT_REF_RE = re.compile(r"^\$[0-9a-fA-F]{1,4}$")

TYPE_RU = {
    "News": "Новость", "Opinion": "Мнение", "Feature": "Фича",
    "Technical": "Технический", "Technical Article": "Технический",
    "Interview": "Интервью", "Video": "Видео", "Gallery": "Галерея",
    "Image Gallery": "Галерея", "Report": "Репортаж", "Live Blog": "Блог",
    "Podcast": "Подкаст", "Quiz": "Викторина", "Live": "Эфир",
    "Preview": "Превью", "Review": "Обзор", "Analysis": "Разбор",
    "Story": "История", "Guide": "Гид",
}


_FLIGHT_PUSH = "self.__next_f.push([1,"
# The article body carries markdown-ish inline links: [Name](https://url).
_MD_LINK_RE = re.compile(r"\[([^\]]{1,120})\]\([^)]*\)")


def _flight_text(page_html: str) -> str:
    """Join every decoded Next.js RSC flight chunk, in order."""
    decoder = json.JSONDecoder()
    chunks = []
    pos = 0
    while True:
        i = page_html.find(_FLIGHT_PUSH, pos)
        if i < 0:
            return "".join(chunks)
        j = i + len(_FLIGHT_PUSH)
        while j < len(page_html) and page_html[j] in " \t":
            j += 1
        try:
            chunk, end = decoder.raw_decode(page_html, j)
        except ValueError:
            pos = i + 1          # malformed chunk: skip and keep scanning
            continue
        if isinstance(chunk, str):
            chunks.append(chunk)
        pos = end


def _clean_text(raw: str) -> str:
    """One payload string -> plain prose, paragraph breaks preserved.

    The '#' is deliberately left in place: it marks an in-article sub-heading
    ("### Title"), and stripping it here would hide the heading from the code
    that recognises it further down.
    """
    body = raw.replace("\\_", "_").replace("\\`", "`")
    body = re.sub(r"(?:\\n)+", "\n\n", body)
    body = _MD_LINK_RE.sub(r"\1", body)
    return re.sub(r"[*_`>]", "", body)


def _flight_text_nodes(flight: str) -> list:
    """Every `\"text\":\"…\"` value with its offset in the stream."""
    decoder = json.JSONDecoder()
    nodes = []
    pos = 0
    while True:
        i = flight.find('"text"', pos)
        if i < 0:
            return nodes
        pos = i + 6
        colon = flight.find(":", i, i + 12)
        if colon == -1:
            continue
        j = colon + 1
        while j < len(flight) and flight[j] in " \t":
            j += 1
        if flight[j] != '"':
            continue
        try:
            value, end = decoder.raw_decode(flight, j)
        except ValueError:
            continue
        pos = end
        if isinstance(value, str) and value.strip():
            nodes.append((j, value))


def _json_value(flight: str, key: str, start: int = 0) -> str:
    """The string value of `key` at or after `start`, escapes decoded.

    A regex like `"([^"]{3,300})"` cannot do this: the payload is JSON, so a
    quote inside a caption arrives escaped as `\\"`, and the character class stops
    at it. The regex consumes the backslash first and hands back a caption ending
    in a stray `\\` — which is how three captions in the archive were cut off
    mid-sentence.
    """
    k = flight.find('"' + key + '"', start)
    if k < 0:
        return ""
    colon = flight.find(":", k + len(key) + 2)
    if colon < 0:
        return ""
    j = colon + 1
    while j < len(flight) and flight[j] in " \t":
        j += 1
    if j >= len(flight) or flight[j] != '"':
        return ""
    try:
        value, _ = json.JSONDecoder().raw_decode(flight, j)
    except ValueError:
        return ""
    return value if isinstance(value, str) else ""


def _dom_gallery(page_html: str) -> dict:
    """Photos from a mid-article carousel, with the captions written for them.

    The carousel is not in the React payload at all — it is server-rendered in
    the DOM, so it has to be read from there. Every slide pairs an image with a
    figcaption written by F1's editors; the alt text is mostly a filename, so
    the caption is the part worth showing and translating.

    The anchor is the body paragraph the carousel follows, which is how the
    gallery gets its place among the blocks.
    """
    first = page_html.find("carousel-child")
    if first < 0:
        return None
    last = page_html.rfind("carousel-child")
    seg = page_html[first:last + 2000]
    srcs = re.findall(r'<img[^>]*src="([^"]+)"[^>]*alt="([^"]*)"', seg)
    caps = [html.unescape(_TAG_RE.sub("", c)).strip()
            for c in re.findall(r'<figcaption[^>]*>(.*?)</figcaption>', seg, re.S)]
    items = []
    for i, (src, alt) in enumerate(srcs):
        if not src.startswith("http"):
            continue
        items.append({"url": src,
                      "alt": html.unescape(alt).strip(),
                      "caption": caps[i] if i < len(caps) else ""})
    if len(items) < 2:
        return None
    before = page_html[:first]
    prev_ps = _BODY_P_RE.findall(before)
    anchor = ""
    if prev_ps:
        anchor = html.unescape(_TAG_RE.sub("", prev_ps[-1])).strip()[:60]

    # The paragraph before the carousel is often rendered only in the DOM and
    # never reaches the payload, so it cannot be looked up among the blocks.
    # The paragraphs after it usually do, so the gallery is placed before the
    # first of those that is recognised.
    after_ps = _BODY_P_RE.findall(page_html[last:])
    after = []
    for p in after_ps:
        text = html.unescape(_TAG_RE.sub("", p)).strip()[:60]
        if text:
            after.append(text)
    return {"anchor": anchor, "after": after, "items": items}


def _article_blocks(page_html: str) -> list:
    """Article as an ordered list of text, photo and video blocks.

    Order is the point: the payload streams the body in the sequence the page
    renders it, so sorting every node by its offset reproduces the original
    layout — a photo appears between the two paragraphs it separates.

    Media is confined to the span of the article's own text nodes. The stream
    also carries navigation logos and related-article cards, which live outside
    that span and would otherwise end up inside the story.
    """
    flight = _flight_text(page_html)
    if not flight:
        return []
    text_nodes = _flight_text_nodes(flight)
    if not text_nodes:
        return []
    lo = min(at for at, _ in text_nodes)
    hi = max(at for at, _ in text_nodes)

    found = []            # (offset, sequence, kind, payload)

    # In-article sub-headings arrive as a markdown "### Title" inside the very
    # same text node as the prose that follows it. They used to be thrown away:
    # the blank line splits the heading off as its own chunk, and then the
    # 40-character minimum for a paragraph threw it out — a heading is short by
    # nature. Six articles in fourteen were losing their headings this way.
    for at, raw in text_nodes:
        seq = 0
        for chunk in _clean_text(raw).split("\n\n"):
            text = re.sub(r"\s+", " ", chunk).strip()
            if not text:
                continue
            heading = re.match(r"#{1,4}\s*(\S.*)$", text)
            if heading:
                found.append((at, seq, "heading", heading.group(1).strip()))
            else:
                found.append((at, seq, "text", text))
            seq += 1

    # Inline photographs arrive as ImageCard blocks: the file in "src", the
    # description in "alt", and the written caption in the card footer.
    for m in re.finditer(r"ImageCard-module_imagecard", flight):
        if not (lo <= m.start() <= hi):
            continue
        seg = flight[m.start():m.start() + 1600]
        src = re.search(r'"src":"(https://media\.formula1\.com[^"]+)"', seg)
        if not src:
            continue
        # Two card variants are in use and both are live:
        #   - one puts the written caption in a footer element inside the card;
        #   - the other has no footer at all and the caption follows the card as
        #     its next sibling.
        # The footer is the reliable one, so it is read first and its presence
        # stops the sibling fallback from mistaking the next paragraph — which
        # in a feature article is often a pull quote, not a caption — for one.
        foot = seg.find("ImageCard-module_footer")
        cap = _json_value(seg, "children", foot) if foot >= 0 else ""
        found.append((m.start(), 0, "image", {
            "kind": "image",
            "url": src.group(1),
            "alt": _json_value(seg, "alt"),
            "caption": cap,
            "has_footer": foot >= 0,
            # The hero shot is followed by the article's opening paragraph, not by
            # a caption, and the two are told apart only by this flag.
            "hero": "f1-article-hero" in seg,
        }))

    # Videos and other embeds: a caption plus a CloudFront still. The site runs
    # no JavaScript, so the still is shown and the link opens the original —
    # embedding X's own player would mean shipping a third-party script.
    for m in re.finditer(r'"contentType":"(atomVideo|atomWidget)"', flight):
        if not (lo <= m.start() <= hi):
            continue
        seg = flight[m.start():m.start() + 1800]
        cap = _json_value(seg, "caption")
        thumb = re.search(r'"thumbnail":\{.*?"(?:path|url)":"(https://[^"]+)"', seg)
        vid = re.search(r'"videoId":"([^"]+)"', seg)
        if not thumb and not vid:
            continue
        found.append((m.start(), 0, "video", {
            "kind": "video",
            "url": thumb.group(1) if thumb else "",
            "caption": cap,
            "video_id": vid.group(1) if vid else "",
        }))

    found.sort(key=lambda item: (item[0], item[1]))

    # Caption fallback for the card variant that has no footer.
    #
    # In that variant F1 renders the caption as the card's next sibling, in the
    # same content-rich-text wrapper body paragraphs use — which is why 79 photos
    # across 62 articles came out with an empty caption while their text sat in
    # the article as an ordinary paragraph.
    #
    # Only used where the footer is absent, and with three guards, because in a
    # feature article the paragraph after a photo is often a pull quote:
    #   - the hero photo is followed by the article's opening paragraph;
    #   - a caption is a direct sibling, so no Container-module boundary stands
    #     between the card and its text;
    #   - `"text":"$37"` points into another RSC chunk and carries no words.
    drop_text = set()
    for i, (at, _, kind, payload) in enumerate(found):
        if kind != "image" or payload.get("hero") or payload.get("has_footer"):
            continue
        j = i + 1
        while j < len(found) and j in drop_text:
            j += 1
        if j >= len(found) or found[j][2] != "text":
            continue
        if "Container-module" in flight[at:found[j][0]]:
            continue
            continue
        cap = _clean_text(found[j][3]).strip()
        # `"text":"$37"` is a link into another chunk of the RSC stream, not
        # prose: the real string lives in a chunk this scanner never resolves.
        # Taking one as a caption prints "$37" under the photo, which is how
        # several captions came out as bare references.
        if _FLIGHT_REF_RE.match(cap) or len(cap) < 20 or _JUNK_RE.search(cap):
            continue
        payload["caption"] = cap
        drop_text.add(j)

    blocks = []
    seen = set()
    seen_media = set()
    for idx, (_, _, kind, payload) in enumerate(found):
        if kind == "text":
            if idx in drop_text:
                # Already used as the caption of the photo above it; repeating it
                # as body text would say the same thing twice.
                continue
            if len(payload) < 40 or _JUNK_RE.search(payload):
                continue
            if payload.startswith(_SKIP_PREFIXES):
                continue
            key = payload[:80].lower()
            if key in seen:        # several nodes repeat the opening
                continue
            seen.add(key)
            blocks.append({"kind": "text", "en": payload})
        elif kind == "heading":
            # Short by nature, so the paragraph minimum must not apply.
            if len(payload) < 4:
                continue
            key = "h:" + payload[:60].lower()
            if key in seen:
                continue
            seen.add(key)
            blocks.append({"kind": "heading", "en": payload})
        else:
            if not payload.get("caption") and not payload.get("url"):
                continue
            # Media and cross-link cards are emitted once per responsive
            # variant, so the same card arrives four times. Keep the first.
            media_key = (payload.get("url"), payload.get("caption"))
            if media_key in seen_media:
                continue
            seen_media.add(media_key)
            blocks.append(payload)

    gallery = _dom_gallery(page_html)
    if gallery:
        node = {"kind": "gallery", "items": gallery["items"],
                "alt": "", "caption": "", "video_id": ""}
        anchor = gallery["anchor"].lower()
        after = [t.lower() for t in gallery["after"]]
        at = None
        # Preferred: straight after the paragraph the carousel follows.
        for i, blk in enumerate(blocks):
            if (blk["kind"] == "text" and anchor
                    and blk["en"][:len(anchor)].lower() == anchor):
                at = i + 1
                break
        if at is None and after:
            # Otherwise before the first paragraph after it, stepping back over
            # the heading of the next section: a heading opens what follows it,
            # so the carousel has to land before that heading, not after it.
            for i, blk in enumerate(blocks):
                if blk["kind"] == "text" and blk["en"][:len(after[0])].lower() in after:
                    at = i
                    break
            while at is not None and at > 0 and blocks[at - 1]["kind"] == "heading":
                at -= 1
        if at is None:
            # Neither anchor matched — the photos still belong on the page, so
            # they go to the end rather than being dropped.
            at = len(blocks)
        blocks.insert(at, node)
    return blocks


def _rsc_paragraphs(page_html: str) -> list:
    """Article paragraphs only — the text blocks of _article_blocks()."""
    return [b["en"] for b in _article_blocks(page_html) if b["kind"] == "text"]


def fetch_body(entry: dict) -> bool:
    """Download the article as ordered blocks: text, photos, videos."""
    try:
        page_html = fetch(entry["url"], timeout=60)
    except Exception as exc:
        log(f"тело не получено ({type(exc).__name__}): {entry['title'][:40]}")
        return False
    # The payload holds the complete story with its media; the DOM holds only
    # the opening paragraphs.
    blocks = _article_blocks(page_html)
    # Fall back to the DOM only when the payload yielded nothing, so a change
    # of Next.js internals degrades to a partial article instead of none.
    if not blocks:
        for matcher in (_BODY_P_RE, _ANY_P_RE):
            paras = []
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
                blocks = [{"kind": "text", "en": p} for p in paras]
                break
    if not blocks:
        return False
    if len(blocks) > BLOCKS_LIMIT:
        log(f"блоков {len(blocks)}, обрезано до {BLOCKS_LIMIT}")
        blocks = blocks[:BLOCKS_LIMIT]
    entry["blocks"] = blocks
    # Same length as blocks from the start: the translator writes into it by
    # index, and a shorter list would leave the tail untranslatable.
    entry["blocks_ru"] = [""] * len(entry["blocks"])
    entry["fetched_body"] = True
    entry["media_done"] = True
    # Stamp the extractor that produced these blocks.
    #
    # Without it a fix to the extractor never reaches the articles already on
    # disk: `need_body` skips everything that looks complete, so a run would
    # report success while the 72 stored articles kept the old, broken output.
    # Bumping this number is what makes every article be rebuilt once.
    entry["blocks_version"] = BLOCKS_VERSION
    # Say what was actually found. Every silent failure in this script has been
    # the same shape: F1 changes its markup, a block type quietly comes back
    # empty, and the run still reports success. A per-article line makes that
    # visible in the bot log instead.
    parts = {}
    for b in entry["blocks"]:
        parts[b["kind"]] = parts.get(b["kind"], 0) + 1
    extra = []
    if parts.get("gallery"):
        extra.append("карусель %d фото" % len(entry["blocks"][
            next(i for i, b in enumerate(entry["blocks"])
                 if b["kind"] == "gallery")].get("items") or []))
    if parts.get("heading"):
        extra.append("подзаголовков %d" % parts["heading"])
    if parts.get("video"):
        extra.append("видео %d" % parts["video"])
    if parts.get("image"):
        extra.append("фото %d" % parts["image"])
    log(f"  {entry['title'][:38]}: текст {parts.get('text', 0)}"
        + (" + " + ", ".join(extra) if extra else ""))
    return True


def blocks_from_old(entry: dict) -> bool:
    """Migrate the pre-media body/body_ru pair into blocks.

    Articles fetched before media support have a paragraph list and no blocks.
    Converting them keeps their Russian text; the next fetch adds the photos and
    videos, and the translation cache restores the paragraphs for free.
    """
    body = entry.get("body") or []
    if not body or entry.get("blocks"):
        return False
    ru = entry.get("body_ru") or []
    blocks = [{"kind": "text", "en": p} for p in body]
    blocks_ru = [ru[i] if i < len(ru) else "" for i in range(len(blocks))]
    entry["blocks"] = blocks
    entry["blocks_ru"] = blocks_ru
    return True


# ── glossary ───────────────────────────────────────────────────────────────
# Post-processing on the translated Russian. Every rule here comes from an
# observed failure in this project's own output, not from guesswork:
# Google rendered "Formula 1" as "Формулы-1", "race day" as "Гоночный день",
# "betting odds" as "шансы", "pole" as "поул", "happy place" as "счастливом
# месте". No provider handles F1 vocabulary without a glossary.
#
# Order matters: longer phrases are replaced before shorter ones that could
# match inside them. Rules are deliberately narrow — a rewrite that fires on a
# correct sentence is worse than a slightly clunky one.

_GLOSSARY = [
    # Series and class names: the hyphen reads as a typo in Russian.
    (r"\bФормул(а|е|у|ы)\s*-\s*1\b", r"Формул\1 1"),
    (r"\bФормул(а|е|у|ы)\s*-\s*([23])\b", r"Формул\1 \2"),
    # Betting: "odds" is not "шансы".
    (r"\bшансы на (?:Гран-при|Grand Prix)", "коэффициенты на Гран-при"),
    (r"\bпоследние шансы\b", "последние коэффициенты"),
    (r"\bпо ставкам\b", "по ставкам"),
    # Pole position is a position, not a noun on its own.
    (r"\bзавоевал[аи]? поул\b", "занял первое место"),
    (r"\bвзял поул\b", "занял первое место"),
    (r"\bпоул-позици", "первое место"),
    # "race day" as a session name.
    (r"\bГоночный день\b", "день гонки"),
    # Idioms that translate literally and read wrong.
    (r"«не в счастливом месте»", "«не в лучшем настроении»"),
    (r"в счастливом месте", "в хорошем состоянии"),
    # F1 roles that need to stay in Russian domain speech.
    (r"\bголосование за пилота дня\b", "пилот дня"),
    (r"\bзавоевал золото\b", "стал чемпионом"),
    (r"\bподнять флаг\b", "финишировать"),
    # Driver of the day vote in headline phrasing.
    (r"получил ваш голос", "набрал больше всего голосов"),
]


def polish(text: str) -> str:
    """Apply the glossary to one translated string."""
    if not text:
        return text
    out = text
    for pattern, repl in _GLOSSARY:
        out = re.sub(pattern, repl, out)
    return out


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

    # 1) Google, no key needed.
    try:
        raw = fetch(f"{GOOGLE_URL}?client=gtx&sl=en&tl=ru&dt=t&q={quoted}",
                    timeout=TRANSLATE_TIMEOUT)
        data = json.loads(raw)
        out = "".join(part[0] for part in (data[0] or []) if part and part[0])
        if out.strip():
            return polish(out.strip())
    except Exception:
        pass

    # 2) MyMemory fallback. Free and quota-limited, so it only runs when Google
    #    is unreachable; its quota is routinely exhausted, hence the tolerance.
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
        # A description is one string, a body is a list of paragraphs. Handling
        # both as lists meant the length check `si >= len(dst_list)` always
        # skipped descriptions: their target is a string, so len() was 0 and no
        # description was ever translated.
        desc = (a.get("description") or "").strip()
        if desc and index < BODY_TRANSLATE_LIMIT + 8 and not a.get("description_ru"):
            key = "description::" + desc
            if cache.get(key):
                a["description_ru"] = cache[key]
                reused += 1
            elif spent < TRANSLATE_CALLS_PER_RUN and not out_of_quota():
                ru = translate(desc)
                spent += 1
                if ru:
                    fails = 0
                    cache[key] = ru
                    a["description_ru"] = ru
                    time.sleep(PAUSE_BETWEEN_TRANSLATIONS)
                else:
                    fails += 1

        if index >= BODY_TRANSLATE_LIMIT or not a.get("blocks"):
            continue
        blocks = a["blocks"]
        dst_list = a.get("blocks_ru") or []

        # A gallery holds many captions in one block, so its translations are a
        # list parallel to the items rather than a single string.
        for si, block in enumerate(blocks):
            if block["kind"] != "gallery" or si >= len(dst_list):
                continue
            items = block.get("items") or []
            ru_caps = dst_list[si] if isinstance(dst_list[si], list) else []
            ru_caps = list(ru_caps) + [""] * (len(items) - len(ru_caps))
            for ii, it in enumerate(items):
                src = (it.get("caption") or "").strip()
                if not src:
                    continue
                key = "body::" + src
                if cache.get(key):
                    ru_caps[ii] = cache[key]
                    reused += 1
                    continue
                if spent >= TRANSLATE_CALLS_PER_RUN or out_of_quota():
                    log("бюджет или квота исчерпаны, остальное — в следующий запуск")
                    log(f"переводов: новых {spent}, из кэша {reused}")
                    dst_list[si] = ru_caps
                    return cache
                ru = translate(src)
                spent += 1
                if ru:
                    fails = 0
                    cache[key] = ru
                    ru_caps[ii] = ru
                    time.sleep(PAUSE_BETWEEN_TRANSLATIONS)
                else:
                    fails += 1
            dst_list[si] = ru_caps

        for si, block in enumerate(blocks):
            # A text or heading block is translated whole; a photo or a video
            # contributes its written caption, and without one there is
            # nothing to translate.
            src = (block["en"] if block["kind"] in ("text", "heading")
                   else (block.get("caption") or "")).strip()
            if not src or si >= len(dst_list):
                continue
            key = "body::" + src
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
/* Photos and videos sit between the paragraphs they separate, so the figure
   keeps the article's own rhythm: full width, caption underneath. */
.body figure{margin:20px 0}
.body figure img{width:100%;border-radius:8px;display:block;margin:0}
.body figcaption{margin-top:7px;color:var(--dim);font-size:.82rem;line-height:1.4}
.body figure.video img{border:1px solid var(--border)}
/* In-article sub-headings. The source marks them with "###" inside the same
   text node as the prose, and they used to be discarded. */
.body h2{margin:30px 0 12px;font-size:1.12rem;line-height:1.3;font-weight:700;
text-transform:uppercase;letter-spacing:.03em;color:#fff}
/* Gallery strip. Scroll-snap gives the "carousel" feel on touch devices with no
   JavaScript at all — the page just scrolls sideways. */
.body .strip{display:flex;gap:12px;overflow-x:auto;scroll-snap-type:x mandatory;
-webkit-overflow-scrolling:touch;padding-bottom:10px;margin:22px -18px;padding-left:18px;
padding-right:18px}
.body .strip figure{flex:0 0 78%;margin:0;scroll-snap-align:center}
/* No fixed aspect ratio: F1 galleries mix landscape and portrait shots, and a
   forced 16:9 crops portrait ones through the middle. */
.body .strip img{width:100%;border-radius:8px;display:block;background:#1c1c22}
.body .strip figcaption{margin-top:8px}
.note{margin-top:28px;padding-top:14px;border-top:1px solid var(--border);
color:var(--dim);font-size:.82rem}
.source{display:inline-block;margin-top:22px;padding:9px 14px;border:1px solid var(--border);
border-radius:6px;color:#8ecbff;text-decoration:none;font-size:.9rem}
.source:hover{border-color:var(--red);color:var(--red)}
"""


def _rfc822(stamp: str) -> str:
    """'2026-10-04T09:00:00Z' -> 'Sun, 04 Oct 2026 09:00:00 +0000'."""
    try:
        when = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""
    return email.utils.format_datetime(
        when.replace(tzinfo=datetime.timezone.utc), usegmt=False)


def write_feed(articles: list, limit: int = 40) -> int:
    """RSS of the headlines, Russian first, so a reader can follow in a reader app.

    This is the point of the news tab: F1 headlines go unread because there is
    no feed to subscribe to. Generated from data already collected, at no extra
    request to formula1.com.
    """
    items = []
    for a in articles[:limit]:
        ru = (a.get("title_ru") or "").strip()
        en = (a.get("title") or "").strip()
        if not ru and not en:
            continue
        link = SITE_URL + a["page"] if a.get("page") else a.get("url") or SITE_URL
        title = f"{ru} — {en}" if ru and en and ru != en else (ru or en)
        desc = (a.get("description_ru") or a.get("description") or "").strip()
        desc_ru = (a.get("description_ru") or "").strip()
        if desc_ru and a.get("description"):
            desc = f"{desc_ru} ({a['description']})"
        body = (f"<p><b>{html.escape(en)}</b></p>" if ru and en else "")
        if desc:
            body += f"<p>{html.escape(desc)}</p>"
        pub = _rfc822(a.get("published") or "")
        cat = TYPE_RU.get(a.get("type") or "", a.get("type") or "")
        items.append(
            "<item>"
            f"<title>{html.escape(title)}</title>"
            f"<link>{html.escape(link)}</link>"
            f"<guid isPermaLink=\"true\">{html.escape(link)}</guid>"
            + (f"<pubDate>{pub}</pubDate>" if pub else "")
            + (f"<category>{html.escape(cat)}</category>" if cat else "")
            + f"<description>{body}</description>"
            "</item>")

    build = _rfc822(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()))
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "<channel>"
        "<title>Новости F1</title>"
        f"<link>{SITE_URL}</link>"
        "<description>Заголовки новостей Formula 1, переведённые на русский. "
        "Источник: formula1.com.</description>"
        "<language>ru</language>"
        + (f"<lastBuildDate>{build}</lastBuildDate>" if build else "")
        + f'<atom:link href="{SITE_URL}feed.xml" rel="self" '
          'type="application/rss+xml"/>'
        + "".join(items)
        + "</channel></rss>\n")
    FEED_PATH.write_text(feed, encoding="utf-8", newline="\n")
    return len(items)


_IMG_W_RE = re.compile(r"w_\d+")


def _img_sources(url: str) -> tuple:
    """(src, srcset) with a smaller rendition for narrow screens.

    The CDN encodes the requested width in the path, and the payload always
    asks for the full 3392px width — about 550 KB per photo, which is a lot for
    a phone. The same transformation with a smaller width costs roughly a fifth
    of that. If the URL carries no width, it is used as-is.
    """
    m = _IMG_W_RE.search(url)
    if not m:
        return url, ""
    wide = int(m.group(0)[2:])
    if wide <= 1200:
        return url, ""
    small = url[:m.start()] + "w_1200" + url[m.end():]
    return small, f"{small} 1200w, {url} {wide}w"


def _render_blocks(blocks: list, ru_list: list, article_url: str) -> str:
    """Render blocks in article order, optionally with their Russian text.

    Photos and videos appear between the paragraphs they sit between, exactly
    where the original page places them. A block with no Russian text falls back
    to the English one rather than leaving a hole in the article.

    A video is shown as its still with a link to the article, because what the
    payload carries is a JPEG frame and the player itself lives in X's own
    embed — bringing that in would mean shipping a third-party script.
    """
    out = []
    for i, b in enumerate(blocks):
        ru = ru_list[i] if i < len(ru_list) else ""
        if b["kind"] == "text":
            text = ru or b.get("en") or ""
            if text:
                out.append(f"<p>{html.escape(text)}</p>")
            continue
        if b["kind"] == "gallery":
            # `ru` for a gallery is a list parallel to the items, so it must not
            # reach html.escape(), which expects a string and calls .replace().
            items = b.get("items") or []
            ru_caps = ru if isinstance(ru, list) else []
            items = b.get("items") or []
            ru_caps = ru if isinstance(ru, list) else []
            cards = []
            for n, it in enumerate(items):
                cap = html.escape((ru_caps[n] if n < len(ru_caps) else "")
                                  or it.get("caption") or "")
                src, srcset = _img_sources(it["url"])
                extra = (f' srcset="{html.escape(srcset)}" sizes="320px"'
                         if srcset else "")
                cards.append('<figure class="shot">'
                             f'<img loading="lazy" src="{html.escape(src)}"{extra} '
                             f'alt="{html.escape(it.get("alt") or "")}">'
                             + (f"<figcaption>{cap}</figcaption>" if cap else "")
                             + "</figure>")
            # Swipeable without a line of JavaScript: the strip simply scrolls.
            out.append('<div class="strip">' + "".join(cards) + "</div>")
            continue
        if b["kind"] == "heading":
            text = ru or b.get("en") or ""
            if text:
                out.append(f"<h2>{html.escape(text)}</h2>")
            continue
        if b["kind"] == "image":
            caption = html.escape(ru if isinstance(ru, str) else "")
            src, srcset = _img_sources(b["url"])
            extra = (f' srcset="{html.escape(srcset)}" '
                     'sizes="(max-width: 800px) 100vw, 760px"'
                     if srcset else "")
            out.append(
                f'<figure><img loading="lazy" src="{html.escape(src)}"{extra} '
                f'alt="{html.escape(b.get("alt") or "")}">'
                + (f"<figcaption>{caption}</figcaption>" if caption else "")
                + "</figure>")
            continue
        link = html.escape(article_url)
        caption = html.escape(ru if isinstance(ru, str) else "")
        src, srcset = _img_sources(b["url"]) if b.get("url") else ("", "")
        extra = (f' srcset="{html.escape(srcset)}" '
                 'sizes="(max-width: 800px) 100vw, 760px"' if srcset else "")
        still = (f'<a href="{link}" target="_blank" rel="noopener">'
                 f'<img loading="lazy" src="{html.escape(src)}"{extra} '
                 f'alt="{html.escape(b.get("alt") or "")}"></a>' if b.get("url") else "")
        note = (f'Видео: {caption} — <a href="{link}" target="_blank" '
                f'rel="noopener">смотреть на formula1.com</a>' if caption
                else 'Видео — <a href="{link}" target="_blank" rel="noopener">'
                     'смотреть на formula1.com</a>'.format(link=link))
        out.append('<figure class="video">' + still +
                   f"<figcaption>{note}</figcaption></figure>")
    return "".join(out)


def write_pages(articles: list) -> int:
    """One standalone HTML file per article that has body text."""
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for a in articles:
        # The block text is no longer kept in index.json, so on most runs only
        # the articles fetched in this run are in memory. Rewriting their pages
        # from absent blocks would publish an empty page over a good one.
        if not a.get("fetched_body") or not a.get("page") or not a.get("blocks"):
            continue
        blocks = a["blocks"]
        ru_list = a.get("blocks_ru") or []
        has_ru = any((ru_list[i] if i < len(ru_list) else "")
                     for i, b in enumerate(blocks) if b["kind"] == "text")
        ru_block = ""
        if has_ru:
            # Only render the Russian block when something was actually
            # translated: an empty container just looks like a missing section.
            ru_block = ('<div class="body" lang="ru">' +
                        _render_blocks(blocks, ru_list, a["url"]) + "</div>")
        else:
            # Nothing translated yet, so show the English text once rather than
            # an empty Russian section above a duplicated English copy.
            ru_list = []
            ru_block = ('<div class="body" lang="en">' +
                        _render_blocks(blocks, ru_list, a["url"]) + "</div>")
        stamp = (a.get("published") or "")[:16].replace("T", " ")
        type_ru = TYPE_RU.get(a.get("type") or "", a.get("type") or "News")
        # The English headline is not repeated: it is the title on the original
        # page, one click away, and showing both made the page look double-headed.
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
<h1>{html.escape(a['title_ru'] or a['title'])}</h1>
{ru_block}
<a class="source" href="{html.escape(a['url'])}" target="_blank" rel="noopener">Оригинал на formula1.com →</a>
<div class="note">Русский текст — машинный перевод. Оригинал на английском —
<a href="{html.escape(a['url'])}" target="_blank" rel="noopener">по ссылке выше</a>.
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
    # Re-attach the article text saved by earlier runs so translation can pick up
    # where it stopped instead of redoing the newest articles every time.
    if BODIES_PATH.exists():
        try:
            saved = json.loads(BODIES_PATH.read_text(encoding="utf-8"))
            for a in stored:
                kept = saved.get(a.get("id") or "")
                if not kept:
                    continue
                blocks = kept.get("blocks")
                if blocks:
                    a["blocks"] = blocks
                    a["blocks_ru"] = kept.get("blocks_ru") or [""] * len(blocks)
                else:
                    a["body"] = kept.get("body") or []
                    a["body_ru"] = kept.get("body_ru") or []
        except Exception as exc:
            log(f"файл тел не читается ({type(exc).__name__})")
    # Archive written before media support: paragraph lists become text blocks.
    migrated = sum(1 for a in stored if blocks_from_old(a))
    if migrated:
        log(f"перенесено в блоки: {migrated}")
    try:
        fresh = harvest(known)
    except Exception as exc:
        log(f"сбор не удался: {type(exc).__name__}: {exc}")
        return 1
    log(f"новых статей: {len(fresh)}")

    merged = fresh + stored
    merged.sort(key=lambda a: a.get("published") or "", reverse=True)
    if len(merged) > MAX_STORED:
        merged = merged[:MAX_STORED]
    articles = merged

    # Bodies before translation, so they can be translated in the same run.
    # The list must consider stored articles too: selecting only from `fresh`
    # meant that once an article was archived its body was never requested
    # again, and the archive stayed headline-only forever. The text itself is
    # checked as well, so an article flagged as fetched but left without a body
    # is retried instead of being trusted forever.
    #
    # Articles migrated from the old paragraph-only format are refetched too:
    # their Russian text survives in blocks_ru and in the translation cache, and
    # only a fresh fetch can add the photos and videos between the paragraphs.
    # The version stamp does the same for the extractor itself: a fix to how
    # blocks are read would otherwise never reach the articles already on disk,
    # and the run would report success while every stored article kept the old
    # output. Bumping BLOCKS_VERSION rebuilds each one once.
    need_body = [a for a in articles
                 if not (a.get("fetched_body") and a.get("blocks")
                         and a.get("media_done")
                         and a.get("blocks_version") == BLOCKS_VERSION)]
    need_body = need_body[:BODY_FETCH_PER_RUN]
    got = 0
    for a in need_body:
        if fetch_body(a):
            got += 1
        time.sleep(PAUSE_BETWEEN_ARTICLES)
    log(f"тела получены для {got} из {len(need_body)}")

    cache = translate_articles(articles, cache)
    # Full bodies mean ~25 strings per article, so 40 articles need well over
    # a thousand entries; a 600 cap would evict translations that are still
    # needed and translate the same paragraphs again every run.
    if len(cache) > 2500:
        cache = dict(list(cache.items())[-2500:])

    pages = write_pages(articles)
    log(f"страниц записано: {pages}")

    # The page path is assigned at harvest time, before the text is fetched, so
    # it can point at a file that was never written. Clear it in that case: the
    # card then falls back to formula1.com and says so, instead of a 404.
    for a in articles:
        if a.get("page") and not (PROJECT_DIR / a["page"]).exists():
            a["page"] = ""

    feed = write_feed(articles)
    log(f"записей в RSS: {feed}")

    # Composition of the whole archive. If F1 changes its markup, the block
    # types quietly stop appearing and every run still reports success — this
    # line is what makes that obvious in the log.
    shape = {}
    galleries = 0
    for a in articles:
        for b in a.get("blocks") or []:
            shape[b["kind"]] = shape.get(b["kind"], 0) + 1
            if b["kind"] == "gallery":
                galleries += 1
    log("состав архива: " + ", ".join(f"{k} {v}" for k, v in sorted(shape.items()))
        + f" | каруселей {galleries}")

    # The listing needs only the headline fields; full bodies live in the
    # standalone pages written just above. Keeping them here as well would
    # roughly quintuple index.json, which every visitor downloads to draw the
    # news grid. `fetched_body` stays so the text is not fetched twice.
    listing = []
    bodies = {}
    for a in articles:
        item = {k: v for k, v in a.items()
                if k not in ("body", "body_ru", "blocks", "blocks_ru")}
        blocks = a.get("blocks") or []
        item["body_paras"] = sum(1 for b in blocks if b["kind"] == "text")
        item["media"] = sum(1 for b in blocks if b["kind"] != "text")
        listing.append(item)
        if blocks:
            bodies[a["id"]] = {"blocks": blocks,
                               "blocks_ru": a.get("blocks_ru") or [""] * len(blocks)}
    BODIES_PATH.write_text(json.dumps(bodies, ensure_ascii=False),
                           encoding="utf-8", newline="\n")

    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": SOURCE_URL,
        "translation": "Google (free, no key) + словарик терминов F1",
        "articles": listing,
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