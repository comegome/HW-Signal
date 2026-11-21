#!/usr/bin/env python3
"""HW Signal 数据恢复包：SQLite 一致性快照 + 配置 打包推到 db-backup 分支。

用法（root）：
    /usr/local/sbin/hwsignal-db-backup.py [--keep N]
输出一行结果，cron 里直接进日志。
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

ROOT = "/opt/hwsignal"
DB = os.path.join(ROOT, "data", "site.db")
KEY = "/root/.ssh/hwsignal_deploy"
REMOTE = "git@github.com:comegome/HW-Signal.git"
BRANCH = "db-backup"
WORK = "/var/lib/hwsignal-db-backup"


def git(*args, cwd=None, check=True):
    env = dict(os.environ, GIT_SSH_COMMAND="ssh -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" % KEY)
    return subprocess.run(["git"] + list(args), cwd=cwd, env=env, check=check,
                          capture_output=True, text=True)


def snapshot(dst):
    src = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    n = out.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    out.close()
    src.close()
    return n


def build_kit(rows_total):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tmp = tempfile.mkdtemp(dir=WORK)
    db_copy = os.path.join(tmp, "site.db")
    n = snapshot(db_copy)
    tar_path = os.path.join(tmp, "restore-%s.tar.gz" % ts)
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(db_copy, arcname="site.db")
        tar.add(os.path.join(ROOT, "config.json"), arcname="config.json")
        for extra in ("run.sh", "generate.py"):
            p = os.path.join(ROOT, extra)
            if os.path.exists(p):
                tar.add(p, arcname="code/" + extra)
        readme = os.path.join(tmp, "RESTORE.md")
        with open(readme, "w", encoding="utf-8") as fh:
            fh.write(
                "# HW Signal 恢复\n\n"
                "1. `git clone git@github.com:comegome/HW-Signal.git /opt/hwsignal`\n"
                "2. 解包本 tar：`site.db` → `/opt/hwsignal/data/site.db`，`config.json` → `/opt/hwsignal/config.json`\n"
                "3. `sudo /opt/hwsignal/scripts/install.sh`（装 dispatch 脚本与 cron）\n"
                "4. 填 `/etc/hwsignal/env`（MAGPIE_KEY）与 `/etc/hwsignal/curl-auth`（GitHub token）\n"
                "5. `sudo /opt/hwsignal/run.sh`，然后 `python3 generate.py --doctor`\n")
        tar.add(readme, arcname="RESTORE.md")
    sha = hashlib.sha256(open(tar_path, "rb").read()).hexdigest()
    size_kb = os.path.getsize(tar_path) // 1024
    shutil.rmtree(tmp)
    return ts, tar_path, sha, size_kb, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=3)
    args = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    repo = os.path.join(WORK, "repo")
    if not os.path.isdir(os.path.join(repo, ".git")):
        shutil.rmtree(repo, ignore_errors=True)
        os.makedirs(repo)
        git("init", "-q", cwd=repo)
        git("remote", "add", "origin", REMOTE, cwd=repo)
    if git("fetch", "-q", "origin", BRANCH, cwd=repo, check=False).returncode == 0:
        git("checkout", "-q", "-B", BRANCH, "FETCH_HEAD", cwd=repo)
    else:
        git("checkout", "-q", "-B", BRANCH, cwd=repo, check=False)
        for f in os.listdir(repo):
            if f != ".git":
                p = os.path.join(repo, f)
                shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)

    ts, tar_path, sha, size_kb, rows = build_kit(None)
    shutil.copy(tar_path, os.path.join(repo, os.path.basename(tar_path)))
    with open(os.path.join(repo, os.path.basename(tar_path) + ".sha256"), "w") as fh:
        fh.write("%s  %s\n" % (sha, os.path.basename(tar_path)))
    os.unlink(tar_path)

    kits = sorted(f for f in os.listdir(repo) if f.startswith("restore-") and f.endswith(".tar.gz"))
    dropped = 0
    for old in kits[:-args.keep]:
        os.unlink(os.path.join(repo, old))
        side = os.path.join(repo, old + ".sha256")
        if os.path.exists(side):
            os.unlink(side)
        dropped += 1
    kits = kits[-args.keep:]

    with open(os.path.join(repo, "MANIFEST.txt"), "w") as fh:
        fh.write("updated_utc=%s\nkeep=%d\nitems=%d\nkits:\n" % (ts, args.keep, rows))
        for k in kits:
            fh.write("  %s\n" % k)

    git("add", "-A", cwd=repo)
    tree = git("write-tree", cwd=repo).stdout.strip()
    commit = git("commit-tree", tree, "-m", "backup %s" % ts, cwd=repo).stdout.strip()
    git("push", "-q", "--force", "origin", "%s:refs/heads/%s" % (commit, BRANCH), cwd=repo)

    print("ok %s items=%d size=%dKB kept=%d dropped=%d sha256=%s" %
          (ts, rows, size_kb, len(kits), dropped, sha[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
