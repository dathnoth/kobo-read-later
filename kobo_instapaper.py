#!/usr/bin/env python3
"""Fetch The Sizzle + The Verge feeds, publish each NEW item as a page on
GitHub Pages, and save it to Instapaper. Kobo's native Instapaper integration
then syncs it with read/archive support.

Design notes:
- Instapaper only saves URLs and its Kobo integration ignores folders, so we
  keep everything in the main list and put source/section into the visible
  description line (the API's `selection` field).
- The feeds have no usable per-article link (or a paywalled one), so we host
  each item's full text ourselves on GitHub Pages and hand Instapaper that URL.
- Pages are ephemeral: each run expires pages older than RETAIN_DAYS and
  force-amends a single commit, so old article content doesn't pile up in
  public git history. They stay live for a week because the Kobo re-fetches
  the original URL when an article is downloaded, not just at save time.
"""

import base64
import glob
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4

HOME = os.path.expanduser("~")
SITE = os.path.join(HOME, "koboRss")
PAGES_BASE = "https://dathnoth.github.io/kobo-read-later"
STATE = os.path.join(HOME, ".the-kobo-instapaper/added.txt")
CONFIG = os.path.join(HOME, ".config/feeds-to-instapaper/config.toml")
UA = "Mozilla/5.0 the-kobo-instapaper/1"
WINDOW_DAYS = 2  # only ever save items published within the last N days
RETAIN_DAYS = 7  # keep published pages live this long (Kobo fetches at download time)

C_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"
ATOM = "{http://www.w3.org/2005/Atom}"

SIZZLE_FEED = ("https://rss.thesizzle.com.au/"
               "1b6df89e6efa6ae84cf536dd52f21b718ecd3c8beae2a3dc3b0a3992ee4caafe.xml")

# Scope: Top Stories + the newsletters only. Category feeds (Tech, Reviews,
# Science, Entertainment, Transportation, Quick Posts) are intentionally left
# out — read those on desktop. Listed in de-dup priority order (earlier wins).
VERGE_FEEDS = [
    ("Notepad", "https://www.theverge.com/rss/partner/subscriber-only-notepad/rss.xml"),
    ("Regulator", "https://www.theverge.com/rss/partner/subscriber-only-regulator/rss.xml"),
    ("The Stepback", "https://www.theverge.com/rss/partner/subscriber-only-the-stepback/rss.xml"),
    ("Installer", "https://www.theverge.com/rss/partner/subscriber-only-installer/rss.xml"),
    ("Optimizer", "https://www.theverge.com/rss/partner/subscriber-only-optimizer-newsletter/rss.xml"),
    ("Top Stories", "https://www.theverge.com/rss/partner/subscriber-only-full-feed/rss.xml"),
]

# atmo.io (Mo Bitar's Substack). Unlike Sizzle/Verge, its posts have real,
# public, non-paywalled per-article links, so we hand Instapaper the article
# URL directly (it crawls the full post) instead of self-hosting a page — the
# feed's content:encoded is only a truncated preview.
ATMOIO_FEED = "https://atmoio.substack.com/feed"


def log(msg):
    print(msg, flush=True)


def creds():
    t = open(CONFIG, encoding="utf-8").read()
    return (re.search(r'username\s*=\s*"([^"]*)"', t).group(1),
            re.search(r'password\s*=\s*"([^"]*)"', t).group(1))


def substack_sid():
    """Session cookie for authenticated Substack fetches (full paid-post text).
    Optional — returns None if not configured. Treat like a password."""
    try:
        t = open(CONFIG, encoding="utf-8").read()
    except FileNotFoundError:
        return None
    m = re.search(r'substack_sid\s*=\s*"([^"]*)"', t)
    return m.group(1) if m else None


def fetch(url, cookie=None):
    headers = {"User-Agent": UA}
    if cookie:
        headers["Cookie"] = cookie
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=60).read()


def parse_date(text):
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None


def sha(*parts):
    return hashlib.sha1("|".join(p for p in parts if p).encode("utf-8")).hexdigest()


def ordinal(n):
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1:'st',2:'nd',3:'rd'}.get(n % 10,'th') }"


def first_image(body):
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', body)
    return html.unescape(m.group(1)) if m else ""


# --- feed parsing -> list of items ------------------------------------------
# item = {id, page_title, body, ip_title, ip_selection}

def sizzle_items():
    root = ET.fromstring(fetch(SIZZLE_FEED))
    items = []
    for it in root.find("channel").findall("item"):
        guid = (it.findtext("guid") or it.findtext("title") or "").strip()
        body = it.findtext("description") or ""
        dt = parse_date(it.findtext("pubDate"))
        num = (re.search(r"(\d+)", it.findtext("title") or "") or [None])[0]
        if dt:
            full = f"The Sizzle - {dt.strftime('%A')} {ordinal(dt.day)} {dt.strftime('%B')}"
            short = f"The Sizzle - {dt.strftime('%A')} {dt.day}/{dt.month}"
        else:
            full = short = (it.findtext("title") or "The Sizzle").strip()
        if num:
            full += f" - Issue {num}"
        items.append({"id": sha(guid), "page_title": full, "body": body,
                      "ip_title": short, "site_name": "The Sizzle", "dt": dt})
    return items


def verge_items(claimed):
    items = []
    for section, url in VERGE_FEEDS:
        try:
            root = ET.fromstring(fetch(url))
        except Exception as exc:  # noqa: BLE001
            log(f"[Verge:{section}] feed error: {exc}")
            continue
        entries = root.findall(".//item")
        atom = not entries
        entries = entries or root.findall(f".//{ATOM}entry")
        for it in entries:
            if atom:
                title = (it.findtext(f"{ATOM}title") or "").strip()
                ident = (it.findtext(f"{ATOM}id") or "").strip()
                link_el = it.find(f"{ATOM}link")
                link = link_el.get("href") if link_el is not None else ""
                body = it.findtext(f"{ATOM}content") or it.findtext(f"{ATOM}summary") or ""
                dt = parse_date(it.findtext(f"{ATOM}published")
                                or it.findtext(f"{ATOM}updated"))
            else:
                title = (it.findtext("title") or "").strip()
                ident = (it.findtext("guid") or "").strip()
                link = (it.findtext("link") or "").strip()
                body = it.findtext(C_ENCODED) or it.findtext("description") or ""
                dt = parse_date(it.findtext("pubDate"))
            aid = sha(ident, link, title)
            if aid in claimed:
                continue
            claimed.add(aid)
            # Verge pages serve full article text to anonymous fetches (the
            # paywall is cookie-metered client-side; verified 2026-07-17), so
            # prefer the real link — it never expires. Self-host only if the
            # item has no link.
            items.append({"id": aid, "page_title": title, "body": body,
                          "link": link,
                          "ip_title": f"{title} | Verge:{section}",
                          "site_name": "The Verge", "dt": dt})
    return items


def atmoio_items():
    """atmo.io posts are paid-subscriber gated, so the article URLs are useless
    to Instapaper's crawler and the public feed only carries previews. With a
    Substack session cookie we fetch full post text and self-host it (like the
    Sizzle). Without the cookie — or once it expires — the body is a truncated
    preview; we detect that and skip rather than save stubs."""
    sid = substack_sid()
    cookie = f"substack.sid={sid}" if sid else None
    root = ET.fromstring(fetch(ATMOIO_FEED, cookie=cookie))
    # atmo.io posts have no per-post cover art, so use the publication avatar as
    # a uniform list thumbnail. Read it from the feed (survives avatar changes)
    # and bump the Substack CDN width for a crisper image.
    chan_img = root.findtext("channel/image/url") or ""
    avatar = re.sub(r"([?,])w_\d+", r"\g<1>w_512", chan_img) if chan_img else ""
    items, stubbed = [], 0
    for it in root.find("channel").findall("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        body = it.findtext(C_ENCODED) or it.findtext("description") or ""
        dt = parse_date(it.findtext("pubDate"))
        # Preview stubs end with a trailing "Read more" <a> link, or carry a
        # paid-subscriber notice. Match only the end-of-body paywall link so an
        # embedded-post card ("<span…>Read more</span>" mid-article) isn't a
        # false positive.
        if ("paid subscribers" in body
                or re.search(r"Read more\s*</a>\s*</p>\s*$", body.strip())):
            stubbed += 1
            continue
        # Some atmo.io posts are video-only (YouTube embeds) with little or no
        # article text — a blank page is useless on a Kobo, so skip them.
        if len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()) < 200:
            continue
        items.append({"id": sha(guid), "page_title": title, "body": body,
                      "ip_title": f"{title} | atmo.io", "og_image": avatar,
                      "site_name": "atmo.io", "dt": dt})
    if stubbed:
        log(f"[atmo.io] {stubbed} item(s) came back as previews — "
            f"{'sid cookie likely expired; refresh substack_sid in config' if sid else 'no substack_sid configured'}.")
    return items


# --- page + publish ---------------------------------------------------------

def make_page(item):
    t = html.escape(item["page_title"])
    site = html.escape(item.get("site_name", ""), quote=True)
    # Explicit per-item thumbnail wins (e.g. atmo.io's uniform avatar); else
    # fall back to the first image in the body.
    og_img = item.get("og_image") or first_image(item["body"])
    og = (f"<meta property='og:image' content=\"{html.escape(og_img, quote=True)}\">"
          if og_img else "")
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{t}</title>"
            f"<meta name='robots' content='noindex, nofollow, noarchive'>"
            f"<meta property='og:type' content='article'>"
            f"<meta property='og:site_name' content=\"{site}\">"
            f"<meta property='og:title' content=\"{t}\">{og}"
            f"</head><body><article><h1>{t}</h1>{item['body']}</article></body></html>")


def git(*args):
    return subprocess.run(["git", *args], cwd=SITE, capture_output=True, text=True)


def publish(pages):
    """Add `pages` (list of (fname, html)) to the repo, drop pages older than
    RETAIN_DAYS (by local file mtime — the site clone is persistent), and
    force-amend a single commit so expired content leaves public history.
    Pages must stay live for a while because the Kobo re-fetches the original
    URL at download time, not just Instapaper's parsed copy."""
    cutoff = time.time() - RETAIN_DAYS * 86400
    for f in glob.glob(os.path.join(SITE, "*.html")):
        if os.path.basename(f) != "index.html" and os.path.getmtime(f) < cutoff:
            os.remove(f)
    for fname, doc in pages:
        with open(os.path.join(SITE, fname), "w", encoding="utf-8") as fh:
            fh.write(doc)
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        return True  # nothing changed
    git("commit", "--amend", "-m", "pages", "--allow-empty")
    push = git("push", "--force", "origin", "main")
    if push.returncode != 0:
        log(f"git push failed: {push.stderr.strip()[:160]}")
        return False
    return True


def wait_live(url, timeout=150):
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(5)
        try:
            if urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": UA}),
                    timeout=15).status == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def instapaper_add(user, pw, url, title):
    data = urllib.parse.urlencode({"url": url, "title": title[:250]}).encode()
    req = urllib.request.Request("https://www.instapaper.com/api/add", data=data)
    req.add_header("Authorization",
                   "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    req.add_header("User-Agent", UA)
    return urllib.request.urlopen(req, timeout=40).status


def load_added():
    try:
        return set(l.strip() for l in open(STATE, encoding="utf-8") if l.strip())
    except FileNotFoundError:
        return set()


def record_added(aid):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "a", encoding="utf-8") as fh:
        fh.write(aid + "\n")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap new items (testing)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    user, pw = creds()
    added = load_added()

    claimed = set(added)  # don't re-claim already-saved ids across Verge feeds
    all_items = sizzle_items() + verge_items(claimed) + atmoio_items()

    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    def recent(dt):
        if dt is None:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff

    new = [it for it in all_items if it["id"] not in added and recent(it["dt"])]
    new.sort(key=lambda it: it["dt"], reverse=True)
    if args.limit:
        new = new[:args.limit]

    if not new:
        log("No new items. Up to date.")
        # still refresh the repo to expire pages past RETAIN_DAYS
        if not args.dry_run:
            publish([])
        return 0

    if args.dry_run:
        for it in new:
            log(f"  [dry-run] {it['ip_title'][:90]}")
        return 0

    # Items with a real public `link` are saved by that URL directly; the rest
    # are self-hosted. Build any pages and publish them all in one push.
    staged = []  # (item, url)
    pages = []   # (fname, html) for self-hosted items only
    for it in new:
        if it.get("link"):
            staged.append((it, it["link"]))
            continue
        fname = f"{uuid4().hex}.html"
        pages.append((fname, make_page(it)))
        staged.append((it, f"{PAGES_BASE}/{fname}"))
    # A publish/deploy failure only blocks the self-hosted items; direct-link
    # items don't depend on GitHub Pages, so save them regardless.
    log(f"Publishing {len(pages)} page(s) to GitHub Pages...")
    hosted_ok = publish(pages)  # also expires pages past RETAIN_DAYS
    if not hosted_ok:
        log("publish failed; hosted items will retry next run.")
    else:
        hosted = [url for it, url in staged if not it.get("link")]
        if hosted and not wait_live(hosted[0]):
            log("Pages did not go live in time; hosted items will retry next run.")
            hosted_ok = False
    if not hosted_ok:
        staged = [(it, url) for it, url in staged if it.get("link")]
        if not staged:
            return 1
    log("Saving to Instapaper...")

    saved = 0
    for it, url in staged:
        try:
            status = instapaper_add(user, pw, url, it["ip_title"])
            if status in (200, 201):
                record_added(it["id"])
                saved += 1
                log(f"  saved {it['ip_title'][:70]}")
            else:
                log(f"  add returned HTTP {status}: {it['ip_title'][:50]}")
        except Exception as exc:  # noqa: BLE001
            log(f"  add FAILED: {it['ip_title'][:50]} - {exc}")

    log(f"Done. {saved}/{len(staged)} saved to Instapaper.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
