#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PayCensus Ammo Builder — rebuild outreach queues from census.db.

Replaces hand-curated outreach_t1/t2.csv with an auto-rebuilt queue so new
contacts flow into firing automatically as enrichment collects them.

Selection rules (conservative):
  - only entities matched to on-chain sellers (addr IN sellers)
  - cls='role' contacts; plus cls='unknown' ONLY when the email's registrable
    domain equals the entity's registrable domain (company's own mailbox)
  - brand/free-mail email domains removed (same list as the sender)
  - suppression list + already-sent (all tiers) + gateway bounce/complaint/
    unsub events removed
  - aggregation-page batches removed: any contact source that contributed
    >5 distinct emails is treated as a link/list page (owner mailbox dump,
    cf. 2026-11-16 blackwoodstate8 incident) and skipped entirely
  - one row per EMAIL domain across the whole file (never double-mail an
    inbox even when two entities share it); sender's PREF order applied
T3 keeps its hand-curated file (never rebuilt).

Run: python3 ammo_builder.py [--dry-run]   (cron: before each fire_daily)
"""
import csv
import json
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, "/opt/paycensus")
from coldmail_pool import clean  # noqa: E402  (gateway /api/v1/clean client)
from outreach_sender import BRAND_EMAIL_DOMAINS  # noqa: E402

#: disposable / throwaway inbox domains — zero value, bounce-prone
DISPOSABLE_DOMAINS = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com",
    "temp-mail.org", "trashmail.com", "yopmail.com", "sharklasers.com",
    "getnada.com", "dispostable.com", "maildrop.cc", "mailnesia.com",
    "throwawaymail.com", "fakeinbox.com", "tempr.email", "mohmal.com",
    "emailondeck.com", "spam4.me", "grr.la", "bugmenot.com",
}

DB = "/var/lib/paycensus/census.db"
SENT_LOG = "/var/lib/paycensus/outreach_sent.json"
EVENTS = "/var/lib/paycensus/coldmail_events.jsonl"
SUPPRESS = "/opt/paycensus/outreach_suppression.txt"
CLEAN_STATE = "/var/lib/paycensus/clean_state.json"
T1_OUT = "/opt/paycensus/outreach_t1.csv"
T2_OUT = "/opt/paycensus/outreach_t2.csv"
MAX_PER_SOURCE = 5
MULTI_LABEL_TLDS = {"co.uk", "com.br", "com.au", "co.jp", "org.uk", "net.au",
                    "co.nz", "com.cn"}
PREF = ("hello", "contact", "support", "team", "info", "admin", "dev")


def log(s):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {s}", flush=True)


def registrable(host):
    host = (host or "").lower().replace("https://", "").split("/")[0]
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def pref_rank(localpart):
    lp = (localpart or "").lower()
    return next((i for i, p in enumerate(PREF) if lp.startswith(p)), 99)


def main():
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # --- exclusion sets -------------------------------------------------
    sent = json.load(open(SENT_LOG)) if _exists(SENT_LOG) else {}
    done = {e.lower() for lst in sent.values() for e in lst}
    supp = set()
    if _exists(SUPPRESS):
        supp = {ln.strip().lower() for ln in open(SUPPRESS) if ln.strip()}
    bad_events = set()
    if _exists(EVENTS):
        for ln in open(EVENTS, encoding="utf-8", errors="replace"):
            try:
                ev = json.loads(ln)
                if ev.get("event") in ("bounce", "complaint", "unsubscribe"):
                    to = (ev.get("to") or "").lower()
                    if to:
                        bad_events.add(to)
            except Exception:  # noqa: BLE001
                pass

    # --- aggregation-page detection ------------------------------------
    src_counts = dict(conn.execute(
        "SELECT source, COUNT(DISTINCT email) FROM contacts GROUP BY source"))
    aggregators = {s for s, n in src_counts.items() if n > MAX_PER_SOURCE}

    # --- candidate rows -------------------------------------------------
    rows = conn.execute(
        "SELECT e.addr, e.domain, e.tier, e.title, c.email, c.cls, c.source, "
        "s.volume, s.txs FROM contacts c "
        "JOIN entities e ON e.addr=c.addr "
        "JOIN sellers s ON s.addr=e.addr "
        "WHERE c.cls IN ('role','unknown')").fetchall()
    conn.close()
    log(f"raw candidate rows: {len(rows)}; aggregators skipped: {len(aggregators)}")

    stats = {"brand": 0, "sent": 0, "suppressed": 0, "bounced": 0,
             "agg": 0, "cross_domain_unknown": 0, "tiers": {}}
    best = {}  # email_domain -> row
    for r in rows:
        email = r["email"].strip().lower()
        if not email or "@" not in email:
            continue
        edom = email.split("@")[-1]
        if edom in BRAND_EMAIL_DOMAINS:
            stats["brand"] += 1
            continue
        if edom in DISPOSABLE_DOMAINS:
            stats.setdefault("disposable", 0)
            stats["disposable"] += 1
            continue
        if email in done:
            stats["sent"] += 1
            continue
        if email in supp or email in bad_events:
            stats["bounced" if email in bad_events else "suppressed"] += 1
            continue
        if r["source"] in aggregators:
            stats["agg"] += 1
            continue
        if r["cls"] != "role":
            # unknown accepted only when the mailbox lives on the entity's own domain
            if registrable(edom) != registrable(r["domain"]):
                stats["cross_domain_unknown"] += 1
                continue
        ed = registrable(edom)
        cand = {"email": email, "domain": r["domain"], "addr": r["addr"],
                "vol_usd": round(r["volume"] or 0, 2), "txs": r["txs"],
                "source": r["source"], "tier": r["tier"],
                "pref": pref_rank(email.split("@")[0])}
        cur = best.get(ed)
        if cur is None or (cand["pref"], -cand["vol_usd"]) < (cur["pref"], -cur["vol_usd"]):
            best[ed] = cand

    # --- gateway clean gate (user rule 2026-11-17) -----------------------
    # Every candidate must pass POST /api/v1/clean before entering the
    # queue. verdict != 'ok' (NXDOMAIN / disposable / brand / typo domains
    # ...) is washed out permanently via the local verdict cache — a washed
    # email never re-enters any future queue. Fail-open on API outage (the
    # /send precheck is the same implementation and skips them anyway).
    cache = {}
    if _exists(CLEAN_STATE):
        try:
            cache = json.load(open(CLEAN_STATE)).get("verdicts", {})
        except Exception:  # noqa: BLE001
            cache = {}
    todo_emails = sorted({r["email"] for r in best.values() if r["email"] not in cache})
    if todo_emails:
        res = clean(todo_emails)
        if res.get("ok"):
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            reasons = res.get("reasons", {})
            for em, v in res.get("verdicts", {}).items():
                cache[em] = {"verdict": v, "reason": reasons.get(em, ""),
                             "at": today}
            with open(CLEAN_STATE, "w") as f:
                json.dump({"verdicts": cache}, f)
            log(f"clean gate: {len(todo_emails)} checked "
                f"(invalid={sum(1 for v in res['verdicts'].values() if v == 'invalid')}, "
                f"skip={sum(1 for v in res['verdicts'].values() if v == 'skip')})")
        else:
            log(f"clean API unavailable ({res.get('error')}) — fail-open, "
                f"retry next run; /send precheck still protects delivery")
    washed = []
    for ed in list(best):
        v = cache.get(best[ed]["email"], {}).get("verdict")
        if v and v != "ok":
            washed.append(f"{best[ed]['email']} ({v}: "
                          f"{cache[best[ed]['email']].get('reason', '')[:40]})")
            del best[ed]
    stats["washed"] = len(washed)
    for w in washed[:12]:
        log(f"  washed: {w}")

    # tier split: T1 (developer) -> t1, everything else -> t2
    files = {"t1": T1_OUT, "t2": T2_OUT}
    out_rows = {"t1": [], "t2": []}
    for ed, r in sorted(best.items(), key=lambda kv: -kv[1]["vol_usd"]):
        tier = "t1" if r["tier"] == "T1" else "t2"
        out_rows[tier].append(r)
    stats["tiers"] = {k: len(v) for k, v in out_rows.items()}
    log(f"selection done: {stats}")

    if dry:
        for t in ("t1", "t2"):
            for r in out_rows[t][:5]:
                print(f"  [{t}] {r['email']} | {r['domain']} | ${r['vol_usd']}")
        return

    import os
    for t, path in files.items():
        if _exists(path):
            os.replace(path, path + ".bak")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["email", "domain", "addr",
                                              "vol_usd", "txs", "source"])
            w.writeheader()
            for r in out_rows[t]:
                w.writerow({k: r[k] for k in w.fieldnames})
        log(f"wrote {path}: {len(out_rows[t])} rows (backup .bak)")


def _exists(p):
    import os
    return os.path.exists(p)


if __name__ == "__main__":
    main()
