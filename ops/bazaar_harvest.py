#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PayCensus Bazaar Harvester — pull the Coinbase CDP x402 Bazaar catalog
(public discovery API, no key) and turn it into census leads + ranking signal.

Flow:
  1. paginate /platform/v2/x402/discovery/resources (limit 500/page, ~36k)
  2. snapshot  -> /var/lib/paycensus/bazaar_resources.json
  3. attribute -> any payTo address already in our sellers but with NO entity
                   gets an entity row (domain = resource host) — source='bazaar'
                   (the Bazaar hands us the attribution for free)
  4. leads     -> payTo addresses we have never seen settling (true new-lead
                   watchlist, the top of the claim funnel)
  5. stats     -> /www/wwwroot/paycensus/data/bazaar_stats.json (admin backend)

Run: python3 bazaar_harvest.py
Cron: daily 03:35 local
"""
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

BASE = ("https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources")
DB = "/var/lib/paycensus/census.db"
SNAP = "/var/lib/paycensus/bazaar_resources.json"
STATS_OUT = "/www/wwwroot/paycensus/data/bazaar_stats.json"
UA = {"User-Agent": "Mozilla/5.0 (PayCensus bazaar harvester; +https://paycensus.com)",
      "Accept": "application/json"}
PAGE = 500
THROTTLE = 0.35


def log(s):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {s}", flush=True)


def fetch_page(limit, offset):
    url = BASE + "?" + urllib.parse.urlencode({"limit": limit, "offset": offset})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            log(f"page(offset={offset}) attempt {attempt+1} fail: {str(e)[:90]}")
            time.sleep(3 * (attempt + 1))
    return None


def registrable(host):
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def parse_items(items):
    out = []
    for it in items:
        url = it.get("resource") or ""
        if not url:
            continue
        host = urlparse(url).hostname or ""
        payto = sorted({(a.get("payTo") or "").lower()
                        for a in it.get("accepts", [])} - {""})
        amt = it.get("accepts", [{}])[0].get("amount")
        q = it.get("quality") or {}
        out.append({
            "resource": url[:300],
            "domain": registrable(host.lower()) if host else "",
            "payTo": payto,
            "price": (float(amt) / 1e6 if amt else None),  # USDC 6dp
            "calls30": q.get("l30DaysTotalCalls") or 0,
            "payers30": q.get("l30DaysUniquePayers") or 0,
            "lastCalled": (q.get("lastCalledAt") or "")[:10],
            "desc": (it.get("description") or "")[:200],
            "lastUpdated": (it.get("lastUpdated") or "")[:10],
        })
    return out


def main():
    # 1) pull everything
    all_items = []
    offset, total = 0, None
    while True:
        d = fetch_page(PAGE, offset)
        if not d:
            break
        total = (d.get("pagination") or {}).get("total", total)
        items = d.get("items") or []
        all_items.extend(parse_items(items))
        offset += len(items)
        if not items or (total and offset >= total) or len(items) < PAGE:
            break
        time.sleep(THROTTLE)
    log(f"pulled {len(all_items)} listings (catalog total={total})")

    # dedupe by resource url (keep highest payers30)
    uniq = {}
    for r in all_items:
        cur = uniq.get(r["resource"])
        if cur is None or r["payers30"] > cur["payers30"]:
            uniq[r["resource"]] = r
    res = list(uniq.values())
    json.dump(res, open(SNAP, "w"))
    log(f"snapshot: {len(res)} unique resources -> {SNAP}")

    # 2) diff against census
    con = sqlite3.connect(DB, timeout=60)
    sellers = {a for (a,) in con.execute("SELECT addr FROM sellers")}
    have_entity = {a for (a,) in con.execute("SELECT addr FROM entities")}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    attributed = new_leads = 0
    for r in res:
        for addr in r["payTo"]:
            if addr not in sellers:
                continue
            if addr in have_entity:
                continue
            # free attribution: the Bazaar hands us the seller's own domain
            if not r["domain"]:
                continue
            try:
                con.execute(
                    "INSERT OR IGNORE INTO entities(addr,domain,title,description,"
                    "source,first_seen,tier) VALUES(?,?,?,?,?,?,?)",
                    (addr, r["domain"], r["desc"][:120] or r["domain"],
                     r["desc"][:200], "bazaar", today, "T2"))
                have_entity.add(addr)
                attributed += 1
            except sqlite3.Error as e:  # noqa: BLE001
                log(f"entity insert fail {addr[:10]}: {str(e)[:80]}")
            # any payTo we've never seen settling is a fresh lead
    unseen = set()
    for r in res:
        for addr in r["payTo"]:
            if addr not in sellers and addr not in have_entity:
                unseen.add(addr)
    new_leads = len(unseen)
    con.commit()

    payto_all = {a for r in res for a in r["payTo"]}
    our_covered = len(payto_all & sellers)
    domains = {r["domain"] for r in res if r["domain"]}
    demand = sorted([r for r in res if r["payers30"] > 0],
                    key=lambda r: -r["payers30"])[:120]
    stats = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "catalog_total": total, "unique_resources": len(res),
        "unique_domains": len(domains),
        "attributed_now": attributed,
        "attributed_total": con.execute(
            "SELECT COUNT(*) FROM entities WHERE source='bazaar'"
        ).fetchone()[0],
        "payto_total": len(payto_all),
        "our_sellers_covered": our_covered,
        "coverage_pct": round(100.0 * our_covered / max(len(payto_all), 1), 1),
        "new_lead_addresses": new_leads,
        "top_by_demand": demand,
    }
    try:
        tcon = sqlite3.connect("/var/lib/paycensus/trust.db")
        vaddr = {a.lower() for (a,) in tcon.execute(
            "SELECT DISTINCT addr FROM verifications WHERE status='verified'")}
        tcon.close()
    except Exception:  # noqa: BLE001
        vaddr = set()
    stats["verified_payto"] = len({a for a in payto_all
                                   if a.lower() in vaddr})
    stats["self_listed"] = any(
        r.get("resource") == "https://agent.paycensus.com/api/x402/rank"
        for r in res)
    json.dump(stats, open(STATS_OUT, "w"))
    con.close()
    log(f"attributed_now={attributed} our_sellers_covered={our_covered} "
        f"new_leads={new_leads} domains={len(domains)}")
    print(f"stats -> {STATS_OUT}")


if __name__ == "__main__":
    main()
