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
import urllib.error
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


# ---------------------------------------------------------------- article body
ARTICLE_WORDS = 180          # 只取正文前 N 词（按段取整，不切半句）
ARTICLE_MIN_WORDS = 80       # 低于这个数就当抓取失败，页面退回摘要
ARTICLE_PAUSE = 1.5          # 每篇之间停顿，别把源站当靶子
ARTICLE_VER = 2              # 抽取规则版本；改规则就 +1，让快照里的旧正文失效
JUNK_MARKS = ("{", "data-", "copy_link", "alert(", "function(", "<", ">")
PUBLISHED = re.compile(r"Published\s+\d{1,2}\s+[A-Z][a-z]+\s+\d{4}")
# 站点自带的推广/免责声明段落（TH 常见，混在 <article> 里）
BOILER = re.compile(
    r"(why you can trust|expert reviewers spend hours|download the tom'?s hardware app"
    r"|follow tom'?s hardware|add us as a preferred source|sign up (for|to)"
    r"|newsletter|find out (more )?about how we test)", re.I)


def clean_paragraphs(page):
    """<article> 里的 <p> → 纯文本段落，剔掉 JS 残留与 Tom's Hardware 的元数据头。"""
    m = re.search(r"<article.*?</article>", page, re.S)
    body = m.group(0) if m else page
    body = re.sub(r"<(script|style|figure|aside|noscript)[^>]*>.*?</\1>", "", body, flags=re.S)
    out = []
    for raw in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S):
        p = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", raw))).strip()
        if len(p.split()) < 12 or any(j in p for j in JUNK_MARKS) or BOILER.search(p):
            continue
        hit = PUBLISHED.search(p)
        if hit:  # TH 把「分类 + 标题 + By 作者 Published 日期」塞进第一个 <p>
            p = p[hit.end():].strip(" -–—|")
            if len(p.split()) < 12:
                continue
        if p in out:  # 上游会把首段再当导语重复一遍
            continue
        out.append(p)
    return out


def article_lead(url, tries=3):
    """抓正文前 ARTICLE_WORDS 词。失败重试（404/410 不重试），最终失败返回 []，由摘要兜底。"""
    page = ""
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=40) as r:
                page = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):  # 永久失效，重试没意义
                print("  article GONE %s %s" % (e.code, url[:70]))
                return []
            print("  article retry %d/%d %s: HTTP %s" % (attempt, tries, url[:60], e.code))
        except Exception as e:  # noqa: BLE001
            print("  article retry %d/%d %s: %s" % (attempt, tries, url[:60], type(e).__name__))
        if attempt < tries:
            time.sleep(2 * attempt)
    if not page:
        print("  article FAIL %s" % url[:70])
        return []
    paras = clean_paragraphs(page)
    if sum(len(p.split()) for p in paras) < ARTICLE_MIN_WORDS:
        return []
    lead, words = [], 0
    for p in paras:
        n = len(p.split())
        if words and words + n > ARTICLE_WORDS * 1.4:
            # 这一段会超出太多：按句子截到预算内，别切半句
            keep, used = [], 0
            for sent in re.split(r"(?<=[.!?])\s+", p):
                w = len(sent.split())
                if keep and used + w > ARTICLE_WORDS - words:
                    break
                keep.append(sent)
                used += w
            if keep:
                lead.append(" ".join(keep))
            break
        lead.append(p)
        words += n
        if words >= ARTICLE_WORDS:
            break
    return lead


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

    out = ROOT / "data" / "items-raw.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    old = None
    if out.exists():
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = None

    # 正文只在首次见到该条目（或摘要变了）时抓，其余复用上一份快照
    reuse = {}
    if (old or {}).get("extract_ver") == ARTICLE_VER:
        for it in old.get("items", []):
            if it.get("body"):
                reuse[(it.get("id"), it.get("summary"))] = it["body"]
    ok = fail = kept = 0
    for it in all_items:
        body = reuse.get((it["id"], it.get("summary")))
        if body:
            kept += 1
        else:
            body = article_lead(it["url"])
            if body:
                ok += 1
            else:
                fail += 1
            time.sleep(ARTICLE_PAUSE)
        it["body"] = body
    print("article     %3d fetched, %3d reused, %3d failed" % (ok, kept, fail))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "extract_ver": ARTICLE_VER,
        "count": len(all_items),
        "items": all_items,
    }
    if old and old.get("items") == all_items:
        print("no change (%d items)" % len(all_items))
        return

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote %s (%d items)" % (out, len(all_items)))


if __name__ == "__main__":
    main()
