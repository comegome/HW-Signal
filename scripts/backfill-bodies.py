#!/usr/bin/env python3
"""回填历史条目正文：在 Actions 上跑（机房 IP 抓不了 VideoCardz）。

用法：python3 scripts/backfill-bodies.py '<json array of {id,url}>'
产物：data/backfill-bodies.json {"extract_ver":N,"bodies":{id:[paragraphs]}}
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fetch as F  # noqa: E402

raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
items = json.loads(raw)
bodies, ok, fail = {}, 0, 0
for it in items:
    paras = F.article_lead(it["url"])
    if paras:
        bodies[it["id"]] = paras
        ok += 1
    else:
        fail += 1
        print("  FAIL", it["id"], it["url"][:80])
    time.sleep(1.5)
print("backfill: %d ok, %d failed, %d total" % (ok, fail, len(items)))
(ROOT / "data").mkdir(parents=True, exist_ok=True)  # data/ 在仓库里是空的，checkout 后不存在
(ROOT / "data" / "backfill-bodies.json").write_text(
    json.dumps({"extract_ver": F.ARTICLE_VER, "bodies": bodies}, ensure_ascii=False),
    encoding="utf-8")
