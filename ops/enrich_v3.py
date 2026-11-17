#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Entity Enrichment v3 — address-side attribution via x402scan resources.
Channel: public tRPC `public.resources.list.byAddress` (bare-string input) —
  for each on-chain seller address, x402scan's payto_origin_map gives its
  resource URLs (with originId). Domain extracted from resource URL; title/
  description joined from our origins catalog by domain.
Coverage measured: 9.0% of sellers (vs 0.14% via origin-side probing).
Idempotent: only processes sellers that have no entity row yet.
Run: nohup python3 enrich_v3.py > /var/lib/paycensus/v3.log 2>&1 &
"""
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

DB = "/var/lib/paycensus/census.db"
CATALOG = "/var/lib/paycensus/origins_catalog.json"
OUT = "/www/wwwroot/paycensus/data/entities.json"
TRPC = "https://www.x402scan.com/api/trpc/public.resources.list.byAddress"
UA = {"User-Agent": "Mozilla/5.0 (PayCensus indexer; +https://paycensus.com)", "Accept": "*/*"}
THROTTLE = 1.0
SKIP_HOSTS = set()  # none: even platform subdomains are the seller's own endpoint


def log(s):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {s}", flush=True)


MULTI_LABEL_TLDS = {"co.uk", "com.br", "com.au", "co.jp", "org.uk", "net.au", "co.nz", "com.cn"}


def registrable(host):
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def load_catalog_index():
    """domain -> (title, description) from the origins catalog (prefer first seen)."""
    idx = {}
    try:
        cat = json.load(open(CATALOG))
        for o in cat.get("origins", []):
            host = urlparse(o.get("origin", "")).netloc.lower()
            if not host:
                continue
            key = host
            if key not in idx:
                idx[key] = ((o.get("title") or "")[:200], (o.get("description") or "")[:1500])
    except Exception as e:  # noqa: BLE001
        log(f"catalog load failed: {e}")
    return idx


def by_address(addr):
    url = TRPC + "?input=" + urllib.parse.quote(json.dumps({"json": addr}))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        return d.get("result", {}).get("data", {}).get("json", []) or []
    except Exception as e:  # noqa: BLE001
        log(f"  err {addr[:12]}: {str(e)[:60]}")
        return None  # None = transport error (retryable), [] = confirmed miss


def publish(conn):
    entities = [dict(zip(["addr", "domain", "title", "description", "source", "first_seen",
                           "category", "contact_channel"], r))
                for r in conn.execute(
                    "SELECT addr,domain,title,description,source,first_seen,e.category, "
                    "EXISTS(SELECT 1 FROM contacts c WHERE c.addr=e.addr AND c.cls='role') "
                    "FROM entities e WHERE e.addr IN (SELECT addr FROM sellers) "
                    "ORDER BY first_seen DESC LIMIT 5000")]
    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
               "note": "payTo -> domain/title/description enrichment. Sources: x402scan-resources "
                       "(address-side, public tRPC), x402scan-probe (origin-side 402 probing), "
                       "wellknown-scan. Only entities matched to on-chain sellers are published.",
               "count": len(entities), "entities": entities},
              open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
    log(f"published entities.json ({len(entities)})")


def main():
    conn = sqlite3.connect(DB, timeout=30)
    catalog_idx = load_catalog_index()
    sellers = [r[0].lower() for r in conn.execute("SELECT addr FROM sellers")]
    have = {r[0].lower() for r in conn.execute("SELECT addr FROM entities")}
    todo = [a for a in sellers if a not in have]
    log(f"v3: {len(todo)} sellers without entity (of {len(sellers)})")

    hits = 0
    for i, a in enumerate(todo, 1):
        data = by_address(a)
        if data:
            for res in data:
                res_url = res.get("resource") or ""
                host = urlparse(res_url).netloc.lower()
                if not host:
                    continue
                title, desc = catalog_idx.get(host, ("", ""))
                if not title:
                    title, desc = catalog_idx.get(registrable(host), ("", ""))
                conn.execute(
                    "INSERT OR REPLACE INTO entities(addr,domain,title,description,source,first_seen) "
                    "VALUES(?,?,?,?,?,?)",
                    (a, host, title, desc, "x402scan-resources",
                     datetime.now(timezone.utc).strftime("%Y-%m-%d")))
                hits += 1
                break  # primary resource only
        if i % 50 == 0:
            conn.commit()
            log(f"[{i}/{len(todo)}] attributed so far: {hits}")
        time.sleep(THROTTLE)

    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) FROM entities e WHERE EXISTS(SELECT 1 FROM sellers s WHERE s.addr=e.addr)"
    ).fetchone()[0]
    publish(conn)
    conn.close()
    log(f"v3 DONE: attributed={hits} total_matched_sellers={n} coverage={n/len(sellers)*100:.1f}%")


if __name__ == "__main__":
    main()
