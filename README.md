# HW Signal

硬件新闻站，内容来自 VideoCardz 与 Tom's Hardware，标题与摘要由机器翻译成中文。

线上：https://hwsignal.yuzhaizu.com

## 结构

```
fetch.py                   抓取两个 RSS，清洗、类目过滤、去重 → data/items-raw.json
generate.py                入库、翻译、渲染静态站 → out/
config.json                站点名、域名、类目白名单、翻译参数
templates/style.css        全站样式表，渲染时写成 /style.css?v=<内容哈希>
templates/                 HTML 模板
.github/workflows/fetch.yml  在 GitHub Actions 上跑 fetch.py，结果推到 data 分支
scripts/install.sh         在新服务器上装脚本与定时任务（唯一安装入口）
scripts/hwsignal-dispatch.sh  触发 Actions 并等它结束后渲染
scripts/hwsignal-db-backup.py 每日恢复包
```

抓取放在 GitHub Actions，因为 VideoCardz 会拦截多数机房的 IP；渲染和翻译放在自己的服务器上，译文缓存留在本地 SQLite。

## 数据流

```
服务器   :05/:35        hwsignal-dispatch.sh → workflow_dispatch → 等 run 结束
Actions  run 结束       fetch.py → data/items-raw.json → 无父提交强推 data 分支
服务器   run 之后       run.sh：git show origin/data:items-raw.json → 翻译 → 渲染到 /var/www/.hwsignal-new → 校验 → 上线
服务器   :23 每小时     /etc/cron.d/hwsignal-render 再跑一轮 run.sh（拿不到新快照就只重渲染）
```

## 定时任务（sl 上）

三条 `/etc/cron.d`（root crontab 里不要再放 `run.sh`，会和 hwsignal-render 重复跑）：

```
# /etc/cron.d/hwsignal-dispatch —— 每 30 分钟触发一次 Actions，并等它结束后渲染
5,35 * * * * root /usr/local/bin/hwsignal-dispatch.sh >> /var/log/hwsignal-dispatch.log 2>&1

# /etc/cron.d/hwsignal-render —— 本机渲染，每小时 :23
23 * * * * root /opt/hwsignal/run.sh >> /var/log/hwsignal.log 2>&1

# /etc/cron.d/hwsignal-db-backup —— 每日恢复包
```

- `hwsignal-dispatch.sh` 触发一次 GitHub Actions 的 `fetch` 工作流，等它结束后调用 `run.sh` 拉取并渲染。它由 `scripts/install.sh` 装到 `/usr/local/bin/`，需要 `/etc/hwsignal/curl-auth`（内含只读/写 Actions 权限的 token，不进仓库）。
- GitHub 自带的 `schedule` 触发不可靠（2026-11-20 观察到停止触发约 7 小时），所以靠 dispatch 兜底。
- `run.sh` 用 flock 防止两次运行重叠。

日志：`/var/log/hwsignal.log`（渲染）、`/var/log/hwsignal-dispatch.log`（触发）。

## 部署到新服务器

```bash
sudo GIT_SSH_COMMAND="ssh -i /root/.ssh/hwsignal_deploy -o IdentitiesOnly=yes" \
  git clone git@github.com:comegome/HW-Signal.git /opt/hwsignal
sudo /opt/hwsignal/scripts/install.sh       # 脚本、cron、/etc/hwsignal 占位、/var/www/hwsignal
sudo vi /etc/hwsignal/env                   # 填 MAGPIE_KEY
cd /opt/hwsignal && sudo HW_OUT=/var/www/hwsignal python3 generate.py --rebuild
```

`scripts/install.sh` 是唯一的安装/更新入口（幂等）；仓库里没有第二个安装脚本。

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
- 摘要不截断存储（上限 800 字只是防跑飞），列表把全文放进 DOM、只用 CSS 裁两行，详情页给全文
- 详情页保留英文原标题，`isBasedOn` 指向原文
- 超过 `retention_days` 的条目不再进 sitemap，但页面保留、加 `noindex,follow`
