#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase C merge — runs on the MAIN server.

For sellers with github repo URLs in gh_hits and no entity: fetch repo metadata
via api.github.com (60 req/h unauthenticated, paced), extract `homepage` and
`description`; if homepage is a real URL (not github itself), create an entity
row -> new attribution. Resumable via sellers.gh_done flag ('1').
"""
import json
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126",
      "Accept": "application/vnd.github+json"}
REPO = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
BAD_HOME = ("github.com", "github.io")  # github.io IS a real site — keep it


def api_repo(owner, name):
    r = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{name}", headers=UA)
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read().decode(errors="replace"))


def main():
    conn = sqlite3.connect(DB, timeout=60)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(sellers)")]
    if "gh_done" not in cols:
        conn.execute("ALTER TABLE sellers ADD COLUMN gh_done TEXT")
        conn.commit()
    rows = conn.execute(
        "SELECT s.addr, s.gh_hits FROM sellers s "
        "LEFT JOIN entities e ON e.addr = s.addr "
        "WHERE s.gh_done IS NULL AND s.gh_hits IS NOT NULL AND s.gh_hits != '[]' "
        "AND e.addr IS NULL ORDER BY s.volume DESC").fetchall()
    print(f"gh_home: {len(rows)} sellers with github hits to resolve", flush=True)
    ok = miss = 0
    for addr, hits in rows:
        try:
            urls = json.loads(hits)
        except Exception:  # noqa: BLE001
            urls = []
        repos = []
        for u in urls:
            owner = name = None
            if isinstance(u, dict):          # Phase C v2 records
                full = u.get("repo") or ""
                if "/" in full:
                    owner, name = full.split("/", 1)
            else:                            # Phase C v1 records (URL strings)
                m = REPO.search(u)
                if m:
                    owner, name = m.group(1), m.group(2)
            if owner and owner.lower() not in ("topics", "search", "features",
                                               "marketplace", "sponsors"):
                r = (owner, (name or "").rstrip("."))
                if r[1] and r not in repos:
                    repos.append(r)
        home, desc, repo_name = None, None, None
        for owner, name in repos[:2]:
            try:
                d = api_repo(owner, name)
                h = (d.get("homepage") or "").strip()
                if h and not h.lower().rstrip("/").endswith("github.com"):
                    if not h.startswith("http"):
                        h = "https://" + h
                    home, desc, repo_name = h, d.get("description"), f"{owner}/{name}"
                    break
                if desc and not desc != desc:
                    desc, repo_name = d.get("description"), f"{owner}/{name}"
                time.sleep(2)  # 60 req/h unauthenticated
            except Exception as e:  # noqa: BLE001
                if "403" in str(e) or "429" in str(e):
                    print("  api rate limit, sleeping 600s", flush=True)
                    time.sleep(600)
                time.sleep(2)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if home:
            conn.execute(
                "INSERT OR IGNORE INTO entities(addr, domain, title, description, "
                "source, first_seen) VALUES(?,?,?,?,?,?)",
                (addr, home, repo_name or home, desc or "", "github-homepage", today))
            ok += 1
        else:
            miss += 1
        conn.execute("UPDATE sellers SET gh_done='1' WHERE addr=?", (addr,))
        conn.commit()
    c2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    n = c2.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    c2.close()
    print(f"DONE attributed={ok} no-homepage={miss} entities_now={n}", flush=True)


if __name__ == "__main__":
    main()
