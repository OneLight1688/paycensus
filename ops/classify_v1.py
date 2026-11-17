#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Contact Classifier v1 — 采集时三分（user-approved 2026-11-01）
For every origin that produced a payTo during enrichment:
  fetch origin homepage -> extract emails -> classify AT COLLECTION:
    - personal-mailbox domains (gmail/qq/163/...)  -> DROPPED, never written to DB
    - role mailboxes (info@/support@/sales@/...)    -> contacts(cls='role')
    - everything else (unknown identity)            -> contacts(cls='unknown')  [quarantine]
Hard rule: contacts table is INTERNAL ONLY — never published via API or /data/*.json.
Personal emails are dropped at collection, not stored (privacy red line).
Run: nohup python3 classify_v1.py > classify.log 2>&1 &  (resumable via state file)
"""
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
STATE = "/var/lib/paycensus/classify_state.json"
ENRICH_STATE = "/var/lib/paycensus/enrich_state.json"
UA = {"User-Agent": "Mozilla/5.0 (PayCensus indexer; +https://paycensus.com)", "Accept": "*/*"}
THROTTLE = 1.1

ROLE_LOCALPARTS = {
    "info", "hello", "hi", "support", "sales", "contact", "admin", "team", "mail",
    "office", "business", "help", "service", "services", "dev", "developer",
    "developers", "api", "billing", "accounts", "account", "enquiry", "enquiries",
    "inquiry", "inquiries", "bd", "partner", "partners", "press", "media",
    "legal", "privacy", "compliance", "security", "data", "ops", "careers", "hr",
}
DROP_LOCALPARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "postmaster", "abuse"}
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "example.edu",
                       "yourdomain.com", "domain.com", "test.com", "test.org",
                       "email.com", "sentry.io", "wixpress.com"}
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk",
    "live.com", "msn.com", "yahoo.com", "yahoo.co.jp", "icloud.com", "me.com",
    "mac.com", "aol.com", "protonmail.com", "proton.me", "pm.me", "gmx.com",
    "gmx.de", "gmx.net", "web.de", "t-online.de", "mail.com", "zoho.com",
    "yandex.com", "yandex.ru", "qq.com", "163.com", "126.com", "sina.com",
    "sina.cn", "foxmail.com", "aliyun.com", "sohu.com", "naver.com", "daum.net",
}
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


def fetch_url(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read(300000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


MULTI_LABEL_TLDS = {"co.uk", "com.br", "com.au", "co.jp", "org.uk", "net.au", "co.nz", "com.cn"}


def registrable_domain(host):
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def collect_sources(origin):
    """Yield (url, html) candidates: origin root, registrable-domain root, well-known docs."""
    host = origin.split("://", 1)[-1].split("/")[0].lower()
    rd = registrable_domain(host)
    seen = set()
    for url in [origin + "/", f"https://{rd}/",
                origin + "/.well-known/x402", f"https://{rd}/.well-known/x402"]:
        if url not in seen:
            seen.add(url)
            html = fetch_url(url)
            if html:
                yield url, html


def classify_email(email, origin_host):
    domain = email.split("@")[1]
    local = email.split("@")[0]
    if domain in PERSONAL_DOMAINS:
        return "drop-personal"
    if local in ROLE_LOCALPARTS:
        return "role"
    return "unknown"  # includes company-domain mails with unknown localpart -> quarantine


def main():
    conn = sqlite3.connect(DB, timeout=30)
    conn.executescript(SCHEMA_CONTACTS)
    state = {"processed": {}, "stats": {"personal_dropped": 0}}
    if os.path.exists(STATE):
        state = json.load(open(STATE))

    enrich = json.load(open(ENRICH_STATE))
    origin_map = {}  # origin -> [paytos]
    for v in enrich["probed"].values():
        if v.get("paytos"):
            origin_map[v["origin"].rstrip("/")] = v["paytos"]
    log(f"origins with payTo: {len(origin_map)}")

    n_role = n_unknown = n_drop = 0
    for i, (origin, paytos) in enumerate(sorted(origin_map.items()), 1):
        if origin in state["processed"]:
            continue
        host = origin.split("://", 1)[-1].split("/")[0].lower()
        emails = set()
        for url, html in collect_sources(origin):
            found = extract_emails(html)
            if found:
                emails.update(found)
        emails = sorted(emails)
        res = {"emails": {}}
        for e in emails:
            cls = classify_email(e, host)
            res["emails"][e] = cls
            if cls == "drop-personal":
                n_drop += 1
                state["stats"]["personal_dropped"] += 1
                continue  # never written to DB
            for pt in paytos:
                conn.execute(
                    "INSERT OR IGNORE INTO contacts(addr,email,cls,source,first_seen) VALUES(?,?,?,?,?)",
                    (pt, e, cls, f"origin-probe:{origin}",
                     datetime.now(timezone.utc).strftime("%Y-%m-%d")))
                if cls == "role":
                    n_role += 1
                else:
                    n_unknown += 1
        state["processed"][origin] = res
        if i % 10 == 0:
            json.dump(state, open(STATE, "w"))
            conn.commit()
            log(f"[{i}/{len(origin_map)}] role={n_role} unknown={n_unknown} personal_dropped={n_drop}")
        time.sleep(THROTTLE)

    json.dump(state, open(STATE, "w"))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    by_cls = dict(conn.execute("SELECT cls, COUNT(*) FROM contacts GROUP BY cls").fetchall())
    conn.close()
    log(f"DONE: rows={n} by_cls={by_cls} personal_dropped_total={state['stats']['personal_dropped']}")


if __name__ == "__main__":
    main()
