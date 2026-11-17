#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Contact Classifier v2 — email 3-way scan for attributed entity domains.
Complements classify_v1 (which scans enrich-state payTo origins): v2 scans the
DOMAINS of attributed entities (enrich_v3 x402scan-resources etc.) so every
attributed seller passes through the same collection-time 3-way filter:
  role mailbox -> contacts(cls='role') / personal domain -> dropped, never stored
  / unknown -> contacts(cls='unknown') quarantine.
Internal only — never published. Idempotent via classify2_state.json.
Run: python3 classify_v2.py   (also cron-able after classify_v1)
"""
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
STATE = "/var/lib/paycensus/classify2_state.json"
UA = {"User-Agent": "Mozilla/5.0 (PayCensus indexer; +https://paycensus.com)", "Accept": "*/*"}
THROTTLE = 1.0

ROLE_LOCALPARTS = {
    "info", "hello", "hi", "support", "sales", "contact", "admin", "team", "mail",
    "office", "business", "help", "service", "services", "dev", "developer",
    "developers", "api", "billing", "accounts", "account", "enquiry", "enquiries",
    "inquiry", "inquiries", "bd", "partner", "partners", "press", "media",
    "legal", "privacy", "compliance", "security", "data", "ops", "careers", "hr",
}
DROP_LOCALPARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "postmaster", "abuse"}
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk",
    "live.com", "msn.com", "yahoo.com", "yahoo.co.jp", "icloud.com", "me.com",
    "mac.com", "aol.com", "protonmail.com", "proton.me", "pm.me", "gmx.com",
    "gmx.de", "gmx.net", "web.de", "t-online.de", "mail.com", "zoho.com",
    "yandex.com", "yandex.ru", "qq.com", "163.com", "126.com", "sina.com",
    "sina.cn", "foxmail.com", "aliyun.com", "sohu.com", "naver.com", "daum.net",
}
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "example.edu",
                       "yourdomain.com", "domain.com", "test.com", "test.org",
                       "email.com", "sentry.io", "wixpress.com"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
BAD_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".js", ".css",
           ".woff", ".woff2", ".ttf", ".ico", ".mp4", ".pdf", ".svelte")

SCHEMA_CONTACTS = """
CREATE TABLE IF NOT EXISTS contacts (
  addr TEXT, email TEXT, cls TEXT, source TEXT, first_seen TEXT,
  PRIMARY KEY (addr, email)
);
"""


def log(s):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {s}", flush=True)


def fetch_url(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read(300000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def extract_emails(html):
    found = set()
    for m in EMAIL_RE.findall(html[:150000]):
        e = m.strip(".").lower()
        if not e or e.split("@")[0] in DROP_LOCALPARTS:
            continue
        if e.split("@")[1] in PLACEHOLDER_DOMAINS:
            continue
        if e.endswith(BAD_EXT) or len(e) > 80:
            continue
        found.add(e)
    return list(found)[:8]


def classify_email(email):
    domain = email.split("@")[1]
    if domain in PERSONAL_DOMAINS:
        return "drop-personal"
    if email.split("@")[0] in ROLE_LOCALPARTS:
        return "role"
    return "unknown"


def main():
    conn = sqlite3.connect(DB, timeout=30)
    conn.executescript(SCHEMA_CONTACTS)
    state = {"done": {}, "stats": {"personal_dropped": 0}}
    if os.path.exists(STATE):
        state = json.load(open(STATE))

    rows = conn.execute(
        "SELECT addr, domain FROM entities WHERE domain IS NOT NULL AND domain != '' "
        "AND addr IN (SELECT addr FROM sellers)").fetchall()
    todo = [(a, d) for a, d in rows if a not in state["done"]]
    log(f"v2: {len(todo)} entity domains to scan")

    n_role = n_unknown = n_drop = 0
    for i, (addr, domain) in enumerate(sorted(todo), 1):
        host = domain.strip().lower().replace("https://", "").split("/")[0]
        emails = set()
        for url in [f"https://{host}/", f"https://{host}/.well-known/x402"]:
            html = fetch_url(url)
            if html:
                emails.update(extract_emails(html))
        res = {}
        for e in sorted(emails):
            cls = classify_email(e)
            res[e] = cls
            if cls == "drop-personal":
                n_drop += 1
                state["stats"]["personal_dropped"] += 1
                continue
            conn.execute(
                "INSERT OR IGNORE INTO contacts(addr,email,cls,source,first_seen) VALUES(?,?,?,?,?)",
                (addr, e, cls, f"entity-domain:{host}",
                 datetime.now(timezone.utc).strftime("%Y-%m-%d")))
            if cls == "role":
                n_role += 1
            else:
                n_unknown += 1
        state["done"][addr] = res
        if i % 25 == 0:
            json.dump(state, open(STATE, "w"))
            conn.commit()
            log(f"[{i}/{len(todo)}] role={n_role} unknown={n_unknown} personal_dropped={n_drop}")
        time.sleep(THROTTLE)

    json.dump(state, open(STATE, "w"))
    conn.commit()
    by_cls = dict(conn.execute("SELECT cls, COUNT(*) FROM contacts GROUP BY cls").fetchall())
    n_addr = conn.execute("SELECT COUNT(DISTINCT addr) FROM contacts").fetchone()[0]
    conn.close()
    log(f"v2 DONE: role={n_role} unknown={n_unknown} personal_dropped={n_drop} "
        f"table={by_cls} addrs_with_contacts={n_addr}")


if __name__ == "__main__":
    main()
