#!/usr/bin/env python3
"""Render the HW Signal static site from data/items-raw.json.

Usage:
    python3 generate.py            incremental render
    python3 generate.py --rebuild  wipe output and re-render everything

Env:
    MAGPIE_KEY   API key for the translation endpoint (required for new items)
    HW_OUT       output directory (default: ./out)
    HW_DB        sqlite path (default: ./data/site.db)
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TPL_DIR = ROOT / "templates"
OUT = Path(os.environ.get("HW_OUT", ROOT / "out"))
DB_PATH = Path(os.environ.get("HW_DB", ROOT / "data" / "site.db"))
TZ = ZoneInfo(CFG["timezone"])
SITE = CFG["site_name"]
BASE = CFG["base_url"].rstrip("/")


# ---------------------------------------------------------------- helpers
def esc(s):
    return html.escape(s or "", quote=True)


def render(name, **kw):
    tpl = (TPL_DIR / name).read_text(encoding="utf-8")
    for k, v in kw.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return tpl


def to_local(iso):
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def fmt_day(iso):
    d = to_local(iso)
    return d.strftime("%Y-%m-%d") if d else ""


def fmt_time(iso):
    """列表上的时间：固定 MM-DD HH:MM，不随“今天/昨天”变化。"""
    d = to_local(iso)
    return d.strftime("%m-%d %H:%M") if d else ""


def fmt_full(iso):
    d = to_local(iso)
    return d.strftime("%Y年%m月%d日 %H:%M") if d else ""


def tr_hash(title, summary):
    return hashlib.sha1((title + "\x00" + summary).encode("utf-8")).hexdigest()


def month_key(iso):
    d = to_local(iso)
    return d.strftime("%Y-%m") if d else "1970-01"


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id           TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  source_name  TEXT NOT NULL,
  guid         TEXT,
  url          TEXT NOT NULL,
  title_en     TEXT NOT NULL,
  summary_en   TEXT,
  title_zh     TEXT,
  summary_zh   TEXT,
  tr_hash      TEXT,
  tr_model     TEXT,
  categories   TEXT,
  published_at TEXT,
  first_seen   TEXT NOT NULL,
  noindex      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pub ON items(published_at DESC);
"""


def db_open():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert(con, items):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    added = 0
    for it in items:
        cur = con.execute(
            """INSERT INTO items (id, source, source_name, guid, url, title_en, summary_en,
                                  categories, published_at, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title_en=excluded.title_en,
                 summary_en=excluded.summary_en,
                 categories=excluded.categories
               WHERE items.title_en IS NOT excluded.title_en
                  OR items.summary_en IS NOT excluded.summary_en""",
            (it["id"], it["source"], it["source_name"], it["guid"], it["url"],
             it["title"], it.get("summary") or "", json.dumps(it.get("categories") or [], ensure_ascii=False),
             it.get("published_at"), now))
        added += cur.rowcount
    con.commit()
    return added


# ---------------------------------------------------------------- translation
SYS_PROMPT = """你是硬件科技新闻的翻译。把 JSON 数组里每条的 t（标题）和 s（摘要）译成简体中文。

规则：
- 保留原文的产品型号、公司名、数字、单位、货币、专有名词（如 RTX 5090、Ryzen 9 9950X3D、PCIe 5.0、24GB、$999、GTA VI、CUDA）
- 地名译成中文（Ohio → 俄亥俄州，Taiwan → 台湾，Singapore → 新加坡）
- 不补充原文没有的信息，不合并或拆分句子，不加评论
- 标题不加句号；摘要保持一句话的节奏
- 只输出 JSON 数组，每项 {"id":"...","t":"...","s":"..."}，不要 markdown 代码块，不要解释"""


def call_llm(model, payload_items, cfg):
    key = os.environ.get("MAGPIE_KEY", "").strip()
    if not key:
        raise RuntimeError("MAGPIE_KEY not set")
    t = cfg["translate"]
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": json.dumps(payload_items, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": t.get("max_tokens", 12000),
    }
    if t.get("reasoning_effort"):
        body["reasoning_effort"] = t["reasoning_effort"]
    req = urllib.request.Request(
        t["endpoint"], data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.load(r)
    content = (data["choices"][0]["message"]["content"] or "").strip()
    if not content:
        # reasoning models can burn the whole max_tokens budget on thinking
        usage = data.get("usage") or {}
        raise RuntimeError("empty content (reasoning=%s, completion=%s)" % (
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            usage.get("completion_tokens")))
    return content


def parse_json_array(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


def translate_chunk(chunk, cfg):
    """Translate a list of (id, title, summary, hash); returns (dict, model) or (None, None)."""
    payload = [{"id": c[0], "t": c[1], "s": c[2]} for c in chunk]
    for model in (cfg["translate"]["model"], cfg["translate"].get("fallback_model")):
        if not model:
            continue
        try:
            raw = call_llm(model, payload, cfg)
            parsed = parse_json_array(raw)
            if parsed:
                return {str(x.get("id")): x for x in parsed if isinstance(x, dict)}, model
            print("translate: %s returned unparseable output" % model)
        except Exception as e:  # noqa: BLE001
            print("translate: %s failed: %s" % (model, e))
        time.sleep(2)
    return None, None


def translate_pending(con):
    cfg = CFG
    rows = con.execute(
        "SELECT id, title_en, summary_en, tr_hash, title_zh FROM items").fetchall()
    todo = []
    for r in rows:
        h = tr_hash(r["title_en"], r["summary_en"] or "")
        if not r["title_zh"] or r["tr_hash"] != h:
            todo.append((r["id"], r["title_en"], r["summary_en"] or "", h))
    if not todo:
        print("translate: nothing to do")
        return 0

    batch = cfg["translate"].get("batch", 5)
    queue = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    done = 0
    failed = 0

    while queue:
        chunk = queue.pop(0)
        by_id, model = translate_chunk(chunk, cfg)
        if by_id is None:
            if len(chunk) > 1:
                half = len(chunk) // 2
                queue[:0] = [chunk[:half], chunk[half:]]
                print("translate: batch failed, splitting %d -> %d+%d" % (len(chunk), half, len(chunk) - half))
            else:
                failed += 1
                print("translate: item %s left in English" % chunk[0][0])
            continue

        for cid, ten, sen, h in chunk:
            got = by_id.get(str(cid))
            if not got or not got.get("t"):
                continue
            con.execute(
                "UPDATE items SET title_zh=?, summary_zh=?, tr_hash=?, tr_model=? WHERE id=?",
                (str(got["t"]).strip(), str(got.get("s") or "").strip(), h, model, cid))
            done += 1
        con.commit()
        print("translate: +%d (%s)" % (len(chunk), model))

    if failed:
        print("translate: %d item(s) kept in English, will retry next run" % failed)
    return done


# ---------------------------------------------------------------- render
def nav(active):
    links = [("/", "今天", "index"), ("/archive/", "归档", "archive"), ("/about/", "关于", "about")]
    out = []
    for href, label, key in links:
        cls = ' class="on"' if key == active else ""
        out.append('<a href="%s"%s>%s</a>' % (href, cls, label))
    return "".join(out)


def page(path, title, description, content, active="", jsonld=None, noindex=False):
    canonical = BASE + path
    head_extra = ""
    if jsonld:
        head_extra += '<script type="application/ld+json">%s</script>' % json.dumps(jsonld, ensure_ascii=False)
    if noindex:
        head_extra += '<meta name="robots" content="noindex,follow">'
    body = render("layout.html",
                  site=SITE, title=esc(title), description=esc(description),
                  canonical=canonical, canonical_root=BASE, nav=nav(active), content=content,
                  head_extra=head_extra, tagline=esc(CFG["tagline"]),
                  year=datetime.now(TZ).year)
    target = OUT / path.strip("/") / "index.html" if path.endswith("/") and path != "/" else (
        OUT / "index.html" if path == "/" else OUT / path.lstrip("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def source_badge(it):
    return '<span class="src">%s</span>' % esc(it["source_name"])


def clip(s, n):
    """Shorten for list display only, preferring a sentence/clause boundary."""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    m = None
    for ch in "。！？!?；;，、,":
        idx = cut.rfind(ch)
        if idx > n * 0.55 and (m is None or idx > m):
            m = idx
    if m is not None:
        return cut[:m + 1].rstrip()
    return cut.rstrip() + "…"


def row_html(it):
    href = "/item/%s/%s.html" % (month_key(it["published_at"]), it["id"])
    title = it["title_zh"] or it["title_en"]
    summ = it["summary_zh"] or it["summary_en"]
    cats = json.loads(it["categories"] or "[]")
    cat = cats[0] if cats else ""
    return render("row.html",
                  href=href, title=esc(title), summary=esc(clip(summ, CFG["summary_list_chars"])),
                  source=esc(it["source_name"]), time=esc(fmt_time(it["published_at"])),
                  category_html=('<span class="cat">%s</span>' % esc(cat)) if cat else "")


def build_index(con):
    # 首页只放「北京时间当天」的条目，index_count 只作上限
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    recent = con.execute(
        "SELECT * FROM items ORDER BY published_at DESC LIMIT 800").fetchall()
    rows = [r for r in recent if fmt_day(r["published_at"]) == today][:CFG["index_count"]]
    weekday = "一二三四五六日"[now.weekday()]
    head = render("list_head.html",
                  day=now.strftime("%Y年%m月%d日"),
                  weekday="星期" + weekday,
                  count=str(len(rows)),
                  checked=fmt_time(json.loads((ROOT / "data" / "items-raw.json").read_text(encoding="utf-8"))["generated_at"]))
    body = head + "".join(row_html(r) for r in rows)
    page("/", "%s — 硬件新闻" % SITE,
         CFG["description"], '<section class="list">%s</section>' % body, active="index",
         jsonld={"@context": "https://schema.org", "@type": "WebSite",
                 "name": SITE, "url": BASE,
                 "potentialAction": {"@type": "SearchAction", "target": BASE + "/?q={q}"}})


def build_items(con):
    rows = con.execute("SELECT * FROM items").fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(days=CFG["retention_days"])
    n = 0
    for r in rows:
        d = to_local(r["published_at"])
        noindex = bool(d and d < cutoff)
        cats = json.loads(r["categories"] or "[]")
        title = r["title_zh"] or r["title_en"]
        summ = r["summary_zh"] or r["summary_en"]
        ld = {
            "@context": "https://schema.org", "@type": "NewsArticle",
            "headline": title, "inLanguage": "zh-CN",
            "datePublished": r["published_at"],
            "dateModified": r["published_at"],
            "articleSection": cats[0] if cats else "",
            "isBasedOn": r["url"],
            "url": "%s/item/%s/%s.html" % (BASE, month_key(r["published_at"]), r["id"]),
            "publisher": {"@type": "Organization", "name": SITE, "url": BASE},
            "description": summ,
        }
        content = render("item.html",
                         title=esc(title), summary=esc(summ),
                         source=esc(r["source_name"]), url=esc(r["url"]),
                         published=esc(fmt_full(r["published_at"])),
                         category_html=('<span class="cat">%s</span>' % esc(cats[0])) if cats else "",
                         title_en=esc(r["title_en"]),
                         home=esc(CFG["sources"].get(r["source"], {}).get("home", "#")))
        page("/item/%s/%s.html" % (month_key(r["published_at"]), r["id"]),
             title, summ, content, active="", jsonld=ld, noindex=noindex)
        n += 1
    return n


def day_label(day):
    """2026-11-19 -> 2026-11-19 周四（保留年份，跨年也不歧义）"""
    dt = datetime.strptime(day, "%Y-%m-%d")
    return "%s %s" % (day, "周" + "一二三四五六日"[dt.weekday()])


def build_archive(con):
    """归档按天：/archive/ 列出每一天，/archive/YYYY-MM-DD/ 是当天全部条目。"""
    rows = con.execute("SELECT * FROM items ORDER BY published_at DESC").fetchall()
    days = {}
    for r in rows:
        days.setdefault(fmt_day(r["published_at"]), []).append(r)
    ordered = sorted(days.items(), reverse=True)

    cards = "".join(
        render("archive_card.html", day=d, label=day_label(d), count=str(len(v)))
        for d, v in ordered)
    page("/archive/", "归档 — %s" % SITE, "按天浏览全部条目",
         '<section class="list"><h1 class="page-h1">归档</h1><div class="days">%s</div></section>' % cards,
         active="archive")

    for d, v in ordered:
        body = ('<h1 class="page-h1">%s <span class="sub">%d 条</span></h1>' % (day_label(d), len(v))
                + "".join(row_html(r) for r in v))
        page("/archive/%s/" % d, "%s 归档 — %s" % (d, SITE),
             "%s 的全部条目" % d, '<section class="list">%s</section>' % body, active="archive")
    return ordered


def build_about():
    srcs = "".join('<li><a href="%s" rel="noopener">%s</a></li>' % (esc(s["home"]), esc(s["name"]))
                   for s in CFG["sources"].values())
    content = render("about.html", sources=srcs, site=SITE, retention=str(CFG["retention_days"]))
    page("/about/", "关于 — %s" % SITE, "关于 %s 的来源与翻译说明" % SITE, content, active="about")


def build_sitemap(con):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CFG["retention_days"])).isoformat()
    rows = con.execute(
        "SELECT id, published_at FROM items WHERE published_at >= ? ORDER BY published_at DESC",
        (cutoff,)).fetchall()
    by_month = {}
    for r in rows:
        by_month.setdefault(r["published_at"][:7], []).append(r)

    urls = []
    for m, items in by_month.items():
        body = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for r in items:
            body.append("<url><loc>%s/item/%s/%s.html</loc><lastmod>%s</lastmod></url>"
                        % (BASE, m, r["id"], r["published_at"][:10]))
        body.append("</urlset>")
        p = OUT / ("sitemap-%s.xml" % m)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(body), encoding="utf-8")
        urls.append((m, items[0]["published_at"][:10]))

    idx = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for m, last in sorted(urls, reverse=True):
        idx.append("<sitemap><loc>%s/sitemap-%s.xml</loc><lastmod>%s</lastmod></sitemap>" % (BASE, m, last))
    idx.append("</sitemapindex>")
    (OUT / "sitemap.xml").write_text("\n".join(idx), encoding="utf-8")

    (OUT / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % BASE, encoding="utf-8")


def build_feed(con):
    rows = con.execute("SELECT * FROM items ORDER BY published_at DESC LIMIT 30").fetchall()
    entries = []
    for r in rows:
        title = r["title_zh"] or r["title_en"]
        summ = r["summary_zh"] or r["summary_en"]
        link = "%s/item/%s/%s.html" % (BASE, month_key(r["published_at"]), r["id"])
        entries.append(
            "<item><title>%s</title><link>%s</link><guid>%s</guid>"
            "<pubDate>%s</pubDate><description>%s</description></item>"
            % (esc(title), link, link,
               to_local(r["published_at"]).strftime("%a, %d %b %Y %H:%M:%S %z"),
               esc(summ)))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
           "<title>%s</title><link>%s</link><description>%s</description>"
           "<language>zh-CN</language>%s</channel></rss>"
           % (esc(SITE), BASE, esc(CFG["description"]), "".join(entries)))
    (OUT / "feed.xml").write_text(xml, encoding="utf-8")


def build_404():
    content = render("404.html", site=SITE)
    body = render("layout.html", site=SITE, title="页面不存在 — " + SITE,
                  description="页面不存在", canonical=BASE + "/404.html",
                  nav=nav(""), content=content, head_extra='<meta name="robots" content="noindex">',
                  tagline=esc(CFG["tagline"]), year=datetime.now(TZ).year)
    (OUT / "404.html").write_text(body, encoding="utf-8")


def copy_assets():
    src = ROOT / "assets"
    if not src.exists():
        return
    dst = OUT / "assets"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--no-translate", action="store_true")
    args = ap.parse_args()

    raw_path = ROOT / "data" / "items-raw.json"
    if not raw_path.exists():
        sys.exit("missing data/items-raw.json — run fetch.py first")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    if args.rebuild and OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    con = db_open()
    changed = upsert(con, raw["items"])
    print("upsert: %d changed, %d in feed" % (changed, len(raw["items"])))

    if not args.no_translate:
        translate_pending(con)

    build_index(con)
    n = build_items(con)
    build_archive(con)
    build_about()
    build_sitemap(con)
    build_feed(con)
    build_404()
    copy_assets()
    con.close()
    print("rendered %d item pages into %s" % (n, OUT))


if __name__ == "__main__":
    main()
