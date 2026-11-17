#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PayCensus Contact Deep-Scan v3 — widen the funnel beyond the homepage.

classify_v2 only scans / and /.well-known/x402 of each attributed entity.
Many sellers keep their contact address on /contact, /about, /impressum, /
support etc. v3 rescans ONLY entities that still have no role-mailbox
contact, on those deep paths, with the same collection-time 3-way rule:
  role -> contacts(cls='role') / personal domain -> dropped / unknown -> quarantine
Idempotent via classify3_state.json (re-run picks up entities that gained a
role contact elsewhere are skipped, entities scanned with zero hits are
recorded and not rescanned).

Run: python3 classify_v3.py   (cron: daily after classify_v2)
"""
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
STATE = "/var/lib/paycensus/classify3_state.json"
UA = {"User-Agent": "Mozilla/5.0 (PayCensus indexer; +https://paycensus.com)",
      "Accept": "*/*"}
THROTTLE = 0.8
DEEP_PATHS = ["/contact", "/contact-us", "/about", "/support", "/impressum",
              "/legal", "/terms"]
PLATFORM_HOSTS = (".workers.dev", ".pages.dev", ".vercel.app", ".netlify.app",
                  ".railway.app", ".onrender.com", ".fly.dev", ".glitch.me",
                  ".herokuapp.com", ".repl.co", ".deno.dev", ".ts.net",
                  ".sslip.io", ".nip.io", ".localhost", ".example")

ROLE_LOCALPARTS = {
    "info", "hello", "hi", "support", "sales", "contact", "admin", "team",
    "mail", "office", "business", "help", "service", "services", "dev",
    "developer", "developers", "api", "billing", "accounts", "account",
    "enquiry", "enquiries", "inquiry", "inquiries", "bd", "partner",
    "partners", "press", "media", "legal", "privacy", "compliance",
    "security", "data", "ops", "careers", "hr",
}
DROP_LOCALPARTS = {"noreply", "no-reply", "donotreply", "do-not-reply",
                   "postmaster", "abuse"}
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
    "hotmail.co.uk", "live.com", "msn.com", "yahoo.com", "yahoo.co.jp",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "proton.me", "pm.me", "gmx.com", "gmx.de", "gmx.net", "web.de",
    "t-online.de", "mail.com", "zoho.com", "yandex.com", "yandex.ru",
    "qq.com", "163.com", "126.com", "sina.com", "sina.cn", "foxmail.com",
    "aliyun.com", "sohu.com", "naver.com", "daum.net",
}
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net",
                       "example.edu", "yourdomain.com", "domain.com",
                       "test.com", "test.org", "email.com", "sentry.io",
                       "wixpress.com"}
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
BAD_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".js", ".css",
           ".woff", ".woff2", ".ttf", ".ico", ".mp4", ".pdf", ".svelte")


def log(s):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {s}", flush=True)


def fetch_url(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read(200000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def extract_emails(html):
    found = set()
    for m in EMAIL_RE.findall(html):
        e = m.strip(".").lower()
        if not e or e.split("@")[0] in DROP_LOCALPARTS:
            continue
        if e.split("@")[1] in PLACEHOLDER_DOMAINS:
            continue
        if e.endswith(BAD_EXT) or len(e) > 80:
            continue
        found.add(e)
    return found


def classify_email(email):
    domain = email.split("@")[1]
    if domain in PERSONAL_DOMAINS:
        return "drop-personal"
    if email.split("@")[0] in ROLE_LOCALPARTS:
        return "role"
    return "unknown"


def main():
    conn = sqlite3.connect(DB, timeout=60)
    state = {"done": {}, "stats": {"role": 0, "unknown": 0, "dropped": 0}}
    if os.path.exists(STATE):
        state = json.load(open(STATE))

    targets = conn.execute(
        "SELECT e.addr, e.domain FROM entities e "
        "WHERE e.domain IS NOT NULL AND e.domain != '' "
        "AND e.addr IN (SELECT addr FROM sellers) "
        "AND NOT EXISTS (SELECT 1 FROM contacts c "
        "                WHERE c.addr=e.addr AND c.cls='role')").fetchall()
    todo = [(a, d) for a, d in targets if a not in state["done"]]
    log(f"v3: {len(todo)} entities without role contact (of {len(targets)})")

    n_role = n_unknown = n_drop = 0
    for i, (addr, domain) in enumerate(sorted(todo), 1):
        host = domain.strip().lower().replace("https://", "").split("/")[0]
        if not host or "." not in host:
            state["done"][addr] = {}
            continue
        emails = set()
        hits = {}
        for path in DEEP_PATHS:
            html = fetch_url(f"https://{host}{path}")
            if not html:
                continue
            for e in extract_emails(html):
                cls = classify_email(e)
                if cls == "drop-personal":
                    n_drop += 1
                    state["stats"]["dropped"] += 1
                    continue
                emails.add((e, cls))
                if cls == "role":
                    hits[path] = True
            if any(c == "role" for _, c in emails):
                break  # role mailbox found — stop early
        for e, cls in sorted(emails):
            conn.execute(
                "INSERT OR IGNORE INTO contacts(addr,email,cls,source,first_seen) "
                "VALUES(?,?,?,?,?)",
                (addr, e, cls, f"entity-deep:{host}",
                 datetime.now(timezone.utc).strftime("%Y-%m-%d")))
            if cls == "role":
                n_role += 1
                state["stats"]["role"] += 1
            else:
                n_unknown += 1
                state["stats"]["unknown"] += 1
        state["done"][addr] = hits or {"_none": True}
        if i % 25 == 0:
            json.dump(state, open(STATE, "w"))
            conn.commit()
            log(f"[{i}/{len(todo)}] role={n_role} unknown={n_unknown} "
                f"dropped={n_drop}")
        time.sleep(THROTTLE)

    json.dump(state, open(STATE, "w"))
    conn.commit()
    by_cls = dict(conn.execute(
        "SELECT cls, COUNT(*) FROM contacts GROUP BY cls").fetchall())
    conn.close()
    log(f"v3 DONE: role={n_role} unknown={n_unknown} dropped={n_drop} "
        f"table={by_cls}")


if __name__ == "__main__":
    main()
