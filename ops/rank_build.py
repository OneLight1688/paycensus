#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent Trust Rank builder — merge census classification, identity
verification and (optionally) Bazaar demand into a single ranked list of
real machine-payable endpoints.

Score (0-100), fully documented for transparency:
  volume_score   40%  log-scaled 30d-equivalent settlement volume
  buyers_score   25%  log-scaled distinct buyer count
  ticket_score   15%  avg ticket sanity (higher = more real commerce)
  identity       20%  domain verified via trust.paycensus.com (1.0) /
                      attributed but unverified (0.5) / unknown (0)
Only real_commerce and agent_chain classes are ranked — noise is excluded
by construction, which is the whole point.

Writes /www/wwwroot/paycensus/data/rank.json
Run: python3 rank_build.py   (after agent_classifier + bazaar_harvest)
Cron: daily 03:45 local
"""
import json
import math
import sqlite3
from datetime import datetime, timezone

MAP = "/www/wwwroot/paycensus/data/agent_map.json"
BAZAAR = "/var/lib/paycensus/bazaar_resources.json"
TRUST = "/var/lib/paycensus/trust.db"
OUT = "/www/wwwroot/paycensus/data/rank.json"


def main():
    amap = json.load(open(MAP))
    verified = set(); proven = set()
    try:
        con = sqlite3.connect("file:" + TRUST + "?mode=ro", uri=True)
        for d, a, p in con.execute(
            "SELECT DISTINCT domain, addr, addr_proven FROM verifications "
            "WHERE status='verified'"):
            if d:
                verified.add(d.lower())
                if a and p:
                    proven.add((a.lower(), d.lower()))
        con.close()
    except Exception:  # noqa: BLE001
        pass

    # bazaar demand per payTo address (30d payers)
    demand = {}
    try:
        for r in json.load(open(BAZAAR)):
            for a in r.get("payTo", []):
                d = demand.get(a)
                cur = r.get("payers30", 0) or 0
                demand[a] = max(cur, d or 0)
    except Exception:  # noqa: BLE001
        pass

    # entity domain per addr (identity)
    ent = {}
    try:
        con = sqlite3.connect("file:/var/lib/paycensus/census.db?mode=ro", uri=True)
        ent = {a: (dom or "") for a, dom in con.execute("SELECT addr, domain FROM entities")}
        con.close()
    except Exception:  # noqa: BLE001
        pass

    pool = (amap.get("top_real") or []) + (amap.get("top_agent_chains") or [])
    # re-rank from the full class sets would need the classifier; we rank the
    # published tops (12+12) plus hidden_commerce — enough for the public list
    pool += amap.get("hidden_commerce") or []
    seen, rows = set(), []
    for r in pool:
        if r["addr"] in seen:
            continue
        seen.add(r["addr"])
        vol = max(1.0, float(r.get("volume") or 0))
        buyers = max(1, int(r.get("buyers") or 1))
        ticket = float(r.get("avg_ticket") or 0)
        dom = (ent.get(r["addr"]) or "").lower()
        ident = 1.0 if dom and dom in verified else (0.5 if dom else 0.0)
        prov = bool(dom and (r["addr"].lower(), dom) in proven)
        score = (40 * min(1.0, math.log10(vol) / 6) +
                 25 * min(1.0, math.log10(buyers * 10) / 4) +
                 15 * min(1.0, math.log10(max(0.05, ticket) * 100) / 5) +
                 20 * ident)
        rows.append({"addr": r["addr"], "domain": dom or None,
                     "class": r.get("class"), "txs": r.get("txs"),
                     "volume": r.get("volume"), "buyers": r.get("buyers"),
                     "avg_ticket": r.get("avg_ticket"),
                     "verified": ident == 1.0, "identity": ident,
                     "addr_proven": prov,
                     "bazaar_payers30": demand.get(r["addr"], 0),
                     "score": round(score, 1)})
    rows.sort(key=lambda r: -r["score"])
    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "method": {"volume_score": "40% log10(volume)/6",
                   "buyers_score": "25% log10(buyers*10)/4",
                   "ticket_score": "15% log10(ticket*100)/5",
                   "identity": "20% domain-verified=1.0, attributed=0.5, unknown=0; addr_proven=payment-key signature proof",
                   "addr_proven": "payment address signed paycensus-addr-proof=<nonce> (EIP-191 personal_sign)",
                   "classes": "real_commerce + agent_chain only; noise excluded"},
        "count": len(rows), "ranked": rows[:100],
    }
    json.dump(out, open(OUT, "w"))
    print(f"rank.json: {len(rows)} endpoints scored, top score "
          f"{rows[0]['score'] if rows else '-'}")


if __name__ == "__main__":
    main()
