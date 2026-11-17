#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Leads Tagger — 三级客户分类 + 量级临界点看板
- Classifies enriched entities into tiers by transparent keyword signals:
    T3 institution: foundation/grant/ventures/research/...
    T1 developer:   sdk/open source/github/mcp/toolkit/...  (also counted from buyer profiles)
    T2 seller:      api/per call/x402/... (default for on-chain matched sellers)
- Publishes /data/leads.json: per-tier counts vs thresholds, ready flags
- Marketing rule (user-approved): tier data reaching threshold = market signal to start
  that tier's auxiliary outreach (subject to email warmup gates). Cold email is
  auxiliary; module positioning stays the primary channel.
Run: cron hourly, or after enrichment completes.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
OUT = "/www/wwwroot/paycensus/data/leads.json"

THRESHOLDS = {
    "T3": {"name": "Institutions (foundations/VCs/research)", "threshold": 50,
           "action": "monthly report + direct outreach (few, high-value)"},
    "T1": {"name": "Developers / buyers", "threshold": 1000,
           "action": "SDK launch + developer email sequence"},
    "T2": {"name": "Sellers (API providers)", "threshold": 500,
           "action": "seller revenue report email sequence + badge campaign"},
}

T3_KW = ["foundation", "grant", "ventures", "capital", "research", "institute",
         "accelerator", "ecosystem fund", "council"]
T1_KW = ["sdk", "open source", "github", "agent framework", "developer",
         "mcp", "toolkit", "boilerplate", "starter"]
T2_KW = ["api", "per call", "per request", "x402", "pay per", "usdc", "endpoint", "microservice"]


def classify(title, description):
    text = f"{title} {description}".lower()
    if any(k in text for k in T3_KW):
        return "T3"
    if any(k in text for k in T1_KW):
        return "T1"
    if any(k in text for k in T2_KW):
        return "T2"
    return "T2"  # on-chain matched seller default


def main():
    conn = sqlite3.connect(DB, timeout=30)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(entities)")]
    if "tier" not in cols:
        conn.execute("ALTER TABLE entities ADD COLUMN tier TEXT")
        conn.commit()

    rows = conn.execute(
        "SELECT addr,domain,title,description FROM entities e "
        "WHERE EXISTS(SELECT 1 FROM sellers s WHERE s.addr=e.addr)").fetchall()
    tiers = {"T1": 0, "T2": 0, "T3": 0}
    for addr, domain, title, desc in rows:
        t = classify(title or "", desc or "")
        tiers[t] += 1
        conn.execute("UPDATE entities SET tier=? WHERE addr=?", (t, addr))
    conn.commit()

    buyers_n = conn.execute("SELECT COUNT(*) FROM buyers").fetchone()[0]
    # contact 3-way collection stats (internal only; emails never published)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    contacts = dict(conn.execute("SELECT cls, COUNT(DISTINCT email) FROM contacts GROUP BY cls").fetchall()) \
        if "contacts" in tables else {}
    conn.close()
    dropped = 0
    if os.path.exists("/var/lib/paycensus/classify_state.json"):
        try:
            dropped = json.load(open("/var/lib/paycensus/classify_state.json"))["stats"]["personal_dropped"]
        except Exception:  # noqa: BLE001
            pass

    # protocol census (v0.9.0): sig tags from origin probing — x402 vs MPP vs dual
    protocols = {"x402": 0, "mpp": 0, "dual": 0, "untagged": 0, "probed_402": 0}
    if os.path.exists("/var/lib/paycensus/enrich_state.json"):
        try:
            est = json.load(open("/var/lib/paycensus/enrich_state.json"))
            for v in est.get("probed", {}).values():
                if v.get("status") != "402":
                    continue
                protocols["probed_402"] += 1
                s = v.get("sig")
                if s in ("x402", "mpp", "dual"):
                    protocols[s] += 1
                elif s is None:
                    protocols["untagged"] += 1
        except Exception:  # noqa: BLE001
            pass

    # T1 developer proxy: buyer wallets with a spend profile (contacts pending enrichment)
    t1_total = tiers["T1"] + buyers_n
    lead_data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "rule": "Tier data reaching threshold = market signal to start that tier's auxiliary "
                "outreach with pre-prepared personalization. Module positioning (open source) "
                "remains the primary sales strategy; email is auxiliary and gated by warmup (>=95% delivery).",
        "thresholds": THRESHOLDS,
        "tiers": {
            "T3": {"count": tiers["T3"], "threshold": THRESHOLDS["T3"]["threshold"],
                   "progress": round(tiers["T3"] / THRESHOLDS["T3"]["threshold"] * 100, 1),
                   "ready": tiers["T3"] >= THRESHOLDS["T3"]["threshold"]},
            "T1": {"count": t1_total, "threshold": THRESHOLDS["T1"]["threshold"],
                   "progress": round(t1_total / THRESHOLDS["T1"]["threshold"] * 100, 1),
                   "ready": t1_total >= THRESHOLDS["T1"]["threshold"],
                   "note": f"{tiers['T1']} attributed entities + {buyers_n} buyer wallets with spend profiles"},
            "T2": {"count": tiers["T2"], "threshold": THRESHOLDS["T2"]["threshold"],
                   "progress": round(tiers["T2"] / THRESHOLDS["T2"]["threshold"] * 100, 1),
                   "ready": tiers["T2"] >= THRESHOLDS["T2"]["threshold"]},
        },
        "contacts": {
            "note": "3-way classification at collection: role mailboxes stored, personal "
                    "mailboxes dropped, unknown quarantined. Internal only — never published.",
            "role_unique": contacts.get("role", 0),
            "unknown_quarantined": contacts.get("unknown", 0),
            "personal_dropped": dropped,
        },
        "protocols": {
            "note": "Origin-level payment-protocol census (HTTP 402 responses): x402 = "
                    "X-PAYMENT/accepts signature; MPP = WWW-Authenticate: Payment / "
                    "Payment-Receipt / machine-payments; dual = both families on one origin. "
                    "Cross-protocol clock starts 2026-11-06 (v0.9.0).",
            **protocols,
        },
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lead_data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(json.dumps(lead_data["tiers"], indent=1))


if __name__ == "__main__":
    main()
