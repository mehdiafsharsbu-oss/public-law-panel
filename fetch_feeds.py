"""
Daily collector for the public-law panel.

1. Pulls new posts from law blogs / journals (RSS) and new legislation (UK feed, US Federal Register API).
2. Tags each item by topic (constitutional, administrative, labour, AI/tech, rights, international).
3. Translates title and short summary to Persian (cached, so each text is translated once).
4. Writes feeds.json, which index.html reads.

A source that fails is skipped and reported in feeds.json -> status. Nothing here needs an API key.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; public-law-panel/1.0)"}
TIMEOUT = 25

# ---- Sources: edit freely. (name, region, url). Region: intl / eu / uk / us / other
ARTICLE_FEEDS = [
    ("I·CONnect", "intl", "https://www.iconnectblog.com/feed/"),
    ("IACL-IADC Blog", "intl", "https://blog-iacl-aidc.org/feed/"),
    ("EJIL: Talk!", "intl", "https://www.ejiltalk.org/feed/"),
    ("Verfassungsblog", "eu", "https://verfassungsblog.de/feed/"),
    ("Strasbourg Observers", "eu", "https://strasbourgobservers.com/feed/"),
    ("UK Constitutional Law Association", "uk", "https://ukconstitutionallaw.org/feed/"),
    ("UK Human Rights Blog", "uk", "https://ukhumanrightsblog.com/feed/"),
    ("Oxford Human Rights Hub", "other", "https://ohrh.law.ox.ac.uk/feed/"),
    ("Just Security", "us", "https://www.justsecurity.org/feed/"),
    ("Balkinization", "us", "https://balkin.blogspot.com/feeds/posts/default"),
    ("The Regulatory Review", "us", "https://www.theregreview.org/feed/"),
    ("Notice & Comment", "us", "https://www.yalejreg.com/feed/"),
]

# New legislation feeds (kept only if they match at least one topic)
LAW_FEEDS = [
    ("legislation.gov.uk (new)", "uk", "https://www.legislation.gov.uk/new/data.feed"),
]

# US Federal Register search terms (administrative law / regulation)
FEDERAL_REGISTER_TERMS = ["artificial intelligence", "labor", "administrative procedure"]

MAX_PER_FEED = 12
MAX_ARTICLES = 90
MAX_LAWS = 60
SUMMARY_CHARS = 320

TOPICS = {
    "constitutional": [r"constitution", r"supreme court", r"constitutional court", r"referendum", r"separation of powers", r"federalism"],
    "administrative": [r"administrative", r"judicial review", r"regulation", r"rulemaking", r"agency", r"procurement", r"public authorit", r"public body", r"freedom of information"],
    "labor": [r"\blabou?r\b", r"employment", r"worker", r"\bunion", r"\bwage", r"gig economy", r"workplace"],
    "ai": [r"artificial intelligence", r"\bAI\b", r"algorithm", r"machine learning", r"automated decision", r"data protection", r"\bGDPR\b", r"surveillance", r"digital"],
    "rights": [r"human rights", r"\bECHR\b", r"\bfreedom\b", r"equality", r"privacy", r"discriminat", r"asylum", r"migration"],
    "intl": [r"international law", r"\btreat(y|ies)\b", r"\bICJ\b", r"united nations", r"\bUN\b", r"sanction"],
}
ACRONYMS = (r"\bAI\b", r"\bECHR\b", r"\bGDPR\b", r"\bICJ\b", r"\bUN\b")  # matched case-sensitively

CACHE_FILE = "translations_cache.json"


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CACHE = load_cache()
USED = set()
_fail_streak = 0
_translation_off = False


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def trim(text, n=SUMMARY_CHARS):
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut + "…"


def tag_topics(*texts):
    blob = " ".join(texts)
    found = []
    for k in TOPICS:
        # acronyms are matched case-sensitively, the rest case-insensitively
        words_ci = [w for w in TOPICS[k] if w not in ACRONYMS]
        words_cs = [w for w in TOPICS[k] if w in ACRONYMS]
        if (words_ci and re.search("|".join(words_ci), blob, re.I)) or (words_cs and re.search("|".join(words_cs), blob)):
            found.append(k)
    return found


def translate(text):
    """Translate to Persian with a cache. Returns '' on failure (page then shows the original)."""
    global _fail_streak, _translation_off
    if not text:
        return ""
    USED.add(text)
    if text in CACHE:
        return CACHE[text]
    if _translation_off:
        return ""
    try:
        from deep_translator import GoogleTranslator

        out = GoogleTranslator(source="auto", target="fa").translate(text[:4500])
        if out:
            CACHE[text] = out
            _fail_streak = 0
            time.sleep(0.25)
            return out
    except Exception as ex:  # network, rate limit, ...
        print("translate failed:", str(ex)[:120])
    _fail_streak += 1
    if _fail_streak >= 6:
        print("Too many translation failures; translation switched off for this run.")
        _translation_off = True
    return ""


def entry_date(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return time.strftime("%Y-%m-%d", t) if t else ""


def parse_feed_bytes(content, name, region):
    d = feedparser.parse(content)
    out = []
    for e in d.entries[:MAX_PER_FEED]:
        title = clean(e.get("title"))
        link = e.get("link", "")
        if not title or not link:
            continue
        summary = trim(clean(e.get("summary") or (e.get("content") or [{}])[0].get("value", "")))
        out.append({"source": name, "region": region, "title": title, "summary": summary, "link": link, "date": entry_date(e)})
    return out


def fetch_feed(name, region, url, status):
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        items = parse_feed_bytes(r.content, name, region)
        status[name] = {"ok": True, "items": len(items)}
        return items
    except Exception as ex:
        status[name] = {"ok": False, "error": str(ex)[:140]}
        print("FAILED:", name, "-", str(ex)[:140])
        return []


def fetch_federal_register(term, status):
    name = f"Federal Register: {term}"
    try:
        r = requests.get(
            "https://www.federalregister.gov/api/v1/documents.json",
            params={"per_page": 8, "order": "newest", "conditions[term]": term,
                    "fields[]": ["title", "abstract", "html_url", "publication_date"]},
            headers=UA, timeout=TIMEOUT,
        )
        r.raise_for_status()
        out = []
        for d in r.json().get("results", []):
            out.append({
                "source": "US Federal Register", "region": "us", "title": clean(d.get("title")),
                "summary": trim(clean(d.get("abstract"))), "link": d.get("html_url", ""),
                "date": d.get("publication_date", ""),
            })
        status[name] = {"ok": True, "items": len(out)}
        return out
    except Exception as ex:
        status[name] = {"ok": False, "error": str(ex)[:140]}
        print("FAILED:", name, "-", str(ex)[:140])
        return []


def finish(items, limit):
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["date"], reverse=True):
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        it["topics"] = tag_topics(it["title"], it["summary"])
        out.append(it)
        if len(out) >= limit:
            break
    for it in out:
        it["title_fa"] = translate(it["title"])
        it["summary_fa"] = translate(it["summary"])
    return out


def main():
    status = {}
    articles, laws = [], []
    for name, region, url in ARTICLE_FEEDS:
        articles += fetch_feed(name, region, url, status)
    for name, region, url in LAW_FEEDS:
        for it in fetch_feed(name, region, url, status):
            if tag_topics(it["title"], it["summary"]):
                laws.append(it)
    for term in FEDERAL_REGISTER_TERMS:
        laws += fetch_federal_register(term, status)

    if not articles and not laws:
        print("Nothing fetched; keeping the previous feeds.json.")
        sys.exit(1)

    articles = finish(articles, MAX_ARTICLES)
    laws = finish(laws, MAX_LAWS)

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "articles": articles,
        "laws": laws,
    }
    with open("feeds.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    pruned = {k: v for k, v in CACHE.items() if k in USED}
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False)
    ok = sum(1 for s in status.values() if s["ok"])
    print(f"Done: {len(articles)} articles, {len(laws)} laws, {ok}/{len(status)} sources OK.")


if __name__ == "__main__":
    main()
