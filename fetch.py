#!/usr/bin/env python3
"""Fetch hardware news feeds and write data/items-raw.json.

Runs in GitHub Actions (needs a non-blocked IP for videocardz.com).
Stdlib only, no dependencies.
"""
import hashlib
import html
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError("fetch failed: %s (%s)" % (url, last))


def text_of(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def clean(raw):
    """HTML -> single-line plain text."""
    s = re.sub(r"<br\s*/?>", " ", raw or "")
    s = re.sub(r"</p\s*>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def trim_summary(s, limit):
    """Hard safety cap only; the list truncates for display, the item page shows it all."""
    s = s.strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    m = re.search(r"[.!?][^.!?]*$", cut)
    if m and m.start() > limit * 0.5:
        cut = cut[:m.start() + 1]
    else:
        cut = cut.rsplit(" ", 1)[0] + "…"
    return cut.strip()


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def item_id(guid):
    return hashlib.sha1(guid.encode("utf-8")).hexdigest()[:10]


def parse_feed(key, cfg):
    src = cfg["sources"][key]
    root = ET.fromstring(fetch(src["feed"]))
    out = []
    for it in root.findall(".//item"):
        title = clean(text_of(it.find("title")))
        link = text_of(it.find("link"))
        guid = text_of(it.find("guid")) or link
        desc = text_of(it.find("description"))
        body = desc
        enc = it.find("{http://purl.org/rss/1.0/modules/content/}encoded")
        if enc is not None and not desc.strip():
            body = text_of(enc)

        summary = clean(body)
        # VideoCardz appends a self-link trailer to every description
        summary = re.sub(r"\s*Keep on reading:.*$", "", summary, flags=re.S | re.I)
        summary = trim_summary(summary, cfg["summary_store_max_chars"])

        cats = [clean(text_of(c)) for c in it.findall("category")]
        dt = parse_date(text_of(it.find("pubDate")))

        if not title or not link:
            continue
        out.append({
            "id": item_id(guid),
            "source": key,
            "source_name": src["name"],
            "guid": guid,
            "url": link,
            "title": title,
            "summary": summary,
            "categories": cats,
            "published_at": dt.isoformat().replace("+00:00", "Z") if dt else None,
        })
    return out


def keep(item, cfg):
    cats = set(item["categories"])
    if cats & set(cfg["categories_deny"]):
        return False
    if not cats:  # VideoCardz publishes no categories
        return True
    return bool(cats & set(cfg["categories_allow"]))


def dedupe(items):
    seen_ids = set()
    kept = []
    for it in items:
        if it["id"] in seen_ids:
            continue
        seen_ids.add(it["id"])
        kept.append(it)
    # cross-source near-duplicates: keep the earlier one
    final = []
    for it in kept:
        a = re.sub(r"[^a-z0-9 ]", " ", it["title"].lower())
        dup = False
        for other in final:
            b = re.sub(r"[^a-z0-9 ]", " ", other["title"].lower())
            if SequenceMatcher(None, a, b).ratio() > 0.78:
                dup = True
                break
        if not dup:
            final.append(it)
    return final


def main():
    all_items = []
    for key in CFG["sources"]:
        try:
            got = parse_feed(key, CFG)
            kept = [x for x in got if keep(x, CFG)]
            print("%-14s %3d fetched, %3d kept" % (key, len(got), len(kept)))
            all_items.extend(kept)
        except Exception as e:  # noqa: BLE001
            print("%-14s FAILED: %s" % (key, e), file=sys.stderr)

    if not all_items:
        sys.exit("no items fetched — refusing to overwrite data")

    all_items.sort(key=lambda x: x["published_at"] or "", reverse=True)
    all_items = dedupe(all_items)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(all_items),
        "items": all_items,
    }
    out = ROOT / "data" / "items-raw.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    old = None
    if out.exists():
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = None

    if old and old.get("items") == all_items:
        print("no change (%d items)" % len(all_items))
        return

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote %s (%d items)" % (out, len(all_items)))


if __name__ == "__main__":
    main()
