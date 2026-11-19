#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PayCensus Agent Economy Classifier — "how much of the machine-payment
economy is REAL?"

Classifies every selling address into five behaviour classes using the
payment graph (no heuristics on identity, pure behaviour):

  real_commerce   volume >= $1k, avg ticket >= $0.50, >=3 distinct buyers
  agent_chain     address both buys and sells, non-dust ticket (agents
                  delegating tasks to other agents and paying for them)
  self_probe      pays itself, or >100 txs to <=2 counterparties with <$100
                  total (health checks / endpoint tests)
  dust_noise      avg ticket < $0.01, or <$0.05 spray to 500+ buyers
                  (airdrop bots, faucets, protocol pings)
  aggregator      <=2 counterparties moving >= $10k, or >=10k txs to <=5
                  counterparties (gateway/funnel architecture)
  low_signal      everything else

Writes /www/wwwroot/paycensus/data/agent_map.json consumed by agentmap.html.
Run: python3 agent_classifier.py
Cron: hourly (pure local SQL, cheap)
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
OUT = "/www/wwwroot/paycensus/data/agent_map.json"

CLASSES = ("real_commerce", "agent_chain", "self_probe", "dust_noise",
           "aggregator", "low_signal")


def classify(s):
    """s: dict with txs, volume, buyers, self_loop, both_sides -> class key."""
    avg = (s["volume"] / s["txs"]) if s["txs"] else 0
    if s["self_loop"] and s["volume"] < 1000 and s["buyers"] <= 3:
        return "self_probe"
    if avg < 0.01:
        return "dust_noise"
    if avg < 0.05 and s["buyers"] >= 500:
        return "dust_noise"
    if s["txs"] > 100 and s["buyers"] <= 2 and s["volume"] < 100:
        return "self_probe"
    if (s["buyers"] <= 2 and s["volume"] >= 10000) or \
       (s["txs"] >= 10000 and s["buyers"] <= 5):
        return "aggregator"
    if s["volume"] >= 1000 and avg >= 0.5 and s["buyers"] >= 3:
        return "real_commerce"
    if s["both_sides"] and avg >= 0.05:
        return "agent_chain"
    return "low_signal"


def row(addr, s, attributed):
    return {"addr": addr, "txs": s["txs"], "volume": round(s["volume"], 2),
            "buyers": s["buyers"],
            "avg_ticket": round(s["volume"] / s["txs"], 4) if s["txs"] else 0,
            "attributed": attributed}


def main():
    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    c = con.cursor()
    sellers = {a: {"txs": t, "volume": v}
               for a, t, v in c.execute("SELECT addr, txs, volume FROM sellers")}
    attributed = {a for (a,) in c.execute("SELECT addr FROM entities")}
    buyers = {a for (a,) in c.execute("SELECT addr FROM buyers")}
    buyers_n = defaultdict(int)
    for a, n in c.execute("SELECT seller, COUNT(DISTINCT buyer) FROM edges GROUP BY seller"):
        buyers_n[a] = n
    self_loops = {a for (a,) in c.execute("SELECT seller FROM edges WHERE buyer=seller")}
    con.close()

    classes = defaultdict(lambda: {"count": 0, "txs": 0, "volume": 0.0})
    rows = []
    for addr, s in sellers.items():
        s = dict(s)
        s["buyers"] = buyers_n.get(addr, 0)
        s["self_loop"] = addr in self_loops
        s["both_sides"] = addr in buyers
        k = classify(s)
        classes[k]["count"] += 1
        classes[k]["txs"] += s["txs"]
        classes[k]["volume"] += s["volume"]
        r = row(addr, s, addr in attributed)
        r["class"] = k
        rows.append(r)
    rows.sort(key=lambda r: -r["volume"])

    def top(cls, n=12, unattr_only=False):
        pool = [r for r in rows if r["class"] == cls and
                (not unattr_only or not r["attributed"])]
        return pool[:n]

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "totals": {"sellers": len(sellers),
                   "txs": sum(r["txs"] for r in rows),
                   "volume": round(sum(r["volume"] for r in rows), 0)},
        "classes": {k: {"count": v["count"], "txs": v["txs"],
                        "volume": round(v["volume"], 0)} for k, v in classes.items()},
        "top_real": top("real_commerce"),
        "top_agent_chains": top("agent_chain"),
        "top_dust": top("dust_noise"),
        "top_aggregators": top("aggregator"),
        "self_pings": top("self_probe", n=10),
        "hidden_commerce": top("real_commerce", n=25, unattr_only=True),
        "hidden_count": len([r for r in rows if r["class"] == "real_commerce"
                             and not r["attributed"]]),
    }
    json.dump(out, open(OUT, "w"))
    print(f"agent_map.json written: {out['totals']}")
    for k in CLASSES:
        v = classes[k]
        pct = v["txs"] * 100.0 / max(1, out["totals"]["txs"])
        print(f"  {k:12s} count={v['count']:6d} txs={v['txs']:9d} "
              f"({pct:5.1f}%) vol=${v['volume']:,.0f}")
    print(f"  hidden commercial sellers (claimable): {out['hidden_count']}")


if __name__ == "__main__":
    main()
