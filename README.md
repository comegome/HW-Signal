# HW Signal

硬件新闻站，内容来自 VideoCardz 与 Tom's Hardware，标题与摘要由机器翻译成中文。

线上：https://hwsignal.yuzhaizu.com

## 结构

```
fetch.py                   抓取两个 RSS，清洗、类目过滤、去重 → data/items-raw.json
generate.py                入库、翻译、渲染静态站 → out/
config.json                站点名、域名、类目白名单、翻译参数
templates/                 HTML 模板
.github/workflows/fetch.yml  每小时在 GitHub Actions 上跑 fetch.py 并提交结果
deploy.sh                  在新服务器上安装
```

抓取放在 GitHub Actions，因为 VideoCardz 会拦截多数机房的 IP；渲染和翻译放在自己的服务器上，译文缓存留在本地 SQLite。

## 数据流

```
Actions  :07 每小时   fetch.py → data/items-raw.json（提交进仓库）
服务器   :23 每小时   git pull → generate.py → 翻译新条目 → 渲染 → /var/www/hwsignal
```

## 部署到新服务器

```bash
git clone git@github.com:comegome/HWSignal.git /opt/hwsignal
cd /opt/hwsignal && ./deploy.sh /opt/hwsignal
sudo vi /etc/hwsignal/env          # 填 MAGPIE_KEY
cd /opt/hwsignal && set -a && . /etc/hwsignal/env && set +a && HW_OUT=/var/www/hwsignal python3 generate.py
```

Caddy：

```
hwsignal.yuzhaizu.com {
    root * /var/www/hwsignal
    encode gzip zstd
    file_server
    handle_errors {
        rewrite * /404.html
        file_server
    }
    header Cache-Control "public, max-age=60, must-revalidate"
}
```

依赖：Python 3.9+，只用标准库。

## 翻译

模型 `hy4flash-free`（WorkBuddy 免费档），经 magpie 调用。

这个模型是推理模型，思维链也算进 `max_tokens`，且长度在 800~5700 之间波动。参数配置必须满足：

```
max_tokens        12000    低于 4000 时批量请求会被思维链吃光，返回空回复
reasoning_effort  low      实测把 6 条的思维链从 5687 压到 2496
batch             5        失败会自动拆半重试（5 → 2+3 → 1）
```

`reasoning_effort` 关不到零：`0` 直接 400（字段是 string），`"none"` 不被识别，`thinking: disabled` 和 `enable_thinking: false` 都被忽略。

失败链路：拆半重试 → `deepseek-v4.1-flash` → 保留英文等下轮。

## 手动操作

```bash
python3 fetch.py                     # 需要能访问 videocardz 的 IP
python3 generate.py                  # 增量
python3 generate.py --rebuild        # 全量重建
python3 generate.py --no-translate   # 只渲染，不调 API
```

## 设计取舍

- 类目白名单在 `config.json`：Tom's Hardware 有一半条目是礼品导购和行业八卦，纯硬件白名单只留 22/50，硬件+算力留 35/50
- 摘要上限 320 字，按句号切；源摘要本身没有结束标点时会出现半句
- 详情页保留英文原标题，`isBasedOn` 指向原文
- 超过 `retention_days` 的条目不再进 sitemap，但页面保留、加 `noindex,follow`
