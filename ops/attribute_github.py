#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase C (v2, GitHub Code Search API) — runs on the MAIN server.

Requires a GitHub token in /opt/paycensus/.github_token (chmod 600).
Classic PAT with NO scopes works for public code search (or fine-grained
with public repos read-only). Unauthenticated code search is not allowed.

For sellers without an entity row: exact-match search the payTo address in
public code ("0x..." quoted). Stores JSON [{repo, path, url}] in
sellers.gh_hits ('[]' = checked-no-hits, resumable). Pacing: ~8s/req
(code search limit 10 req/min). Volume-ordered; MAX_ADDRS caps a run.
Follow up with attribute_gh_home.py to turn repo hits into entity rows.
"""
import json
import sqlite3
import sys
import time
import urllib.request

DB = "/var/lib/paycensus/census.db"
TOKEN_FILE = "/opt/paycensus/.github_token"
MAX_ADDRS = int(sys.argv[1]) if len(sys.argv) > 1 else 3000


def load_token():
    try:
        return open(TOKEN_FILE).read().strip()
    except OSError:
        print(f"FATAL: no token file {TOKEN_FILE} — create a GitHub PAT "
              "(classic, no scopes needed) and save it there, chmod 600.",
              flush=True)
        sys.exit(1)


def search_code(tok, addr):
    q = urllib.request.quote(f'"{addr}"')
    r = urllib.request.Request(
        f"https://api.github.com/search/code?q={q}&per_page=5",
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "paycensus-attribution"})
    with urllib.request.urlopen(r, timeout=25) as resp:
        d = json.loads(resp.read().decode(errors="replace"))
    out = []
    for it in d.get("items", [])[:5]:
        out.append({"repo": it.get("repository", {}).get("full_name"),
                    "path": it.get("path"),
                    "url": it.get("html_url")})
    return out


def main():
    tok = load_token()
    conn = sqlite3.connect(DB, timeout=60)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(sellers)")]
    if "gh_hits" not in cols:
        conn.execute("ALTER TABLE sellers ADD COLUMN gh_hits TEXT")
        conn.commit()
    rows = conn.execute(
        "SELECT s.addr FROM sellers s LEFT JOIN entities e ON e.addr = s.addr "
        "WHERE s.gh_hits IS NULL AND e.addr IS NULL "
        "ORDER BY s.volume DESC LIMIT ?", (MAX_ADDRS,)).fetchall()
    total = len(rows)
    print(f"Phase C v2: {total} addresses queued (token loaded)", flush=True)
    hits_n = 0
    t0 = time.time()
    for i, (addr,) in enumerate(rows):
        for attempt in range(3):
            try:
                res = search_code(tok, addr)
                break
            except Exception as e:  # noqa: BLE001
                s = str(e)
                if "401" in s:
                    print("FATAL: token rejected (401)", flush=True)
                    sys.exit(1)
                if "403" in s or "429" in s:
                    print(f"  rate limited, sleep 65s ({s[:60]})", flush=True)
                    time.sleep(65)
                    continue
                time.sleep(5)
                res = None
        if res is None:
            res = []
        conn.execute("UPDATE sellers SET gh_hits=? WHERE addr=?",
                     (json.dumps(res) if res else "[]", addr))
        conn.commit()
        if res:
            hits_n += 1
            print(f"  HIT {addr[:12]}… {res[0]['repo']} {res[0]['path']}",
                  flush=True)
        if (i + 1) % 25 == 0:
            el = time.time() - t0
            eta = el / (i + 1) * (total - i - 1) / 3600
            print(f"  {i+1}/{total} hits={hits_n} elapsed={el/60:.0f}m "
                  f"eta={eta:.1f}h", flush=True)
        time.sleep(8)
    conn.close()
    print(f"DONE {total} searched, {hits_n} with code hits", flush=True)


if __name__ == "__main__":
    main()
