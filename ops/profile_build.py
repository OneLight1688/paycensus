#!/usr/bin/env python3
"""Multi-chain seller profile builder (v0.25.0).

For every verified domain in trust.db, fetch /.well-known/x402.json —
the seller-declared x402 v2 protocol profile (CAIP-2 networks, per-chain
payTo addresses, resources, offers) — and store the parsed profile in
trust.db seller_profiles. Publishes a public aggregate at
/data/seller_profiles.json. Estimates/aggregates are never fabricated:
missing file or parse failure is recorded as ok=0 with the error.
"""
import datetime
import json
import os
import sqlite3
import ssl
import urllib.request

DB = "/var/lib/paycensus/trust.db"
OUT = "/www/wwwroot/paycensus/data/seller_profiles.json"
UA = "PayCensus ProfileBot (+https://paycensus.com; verified-seller metadata)"
CTX = ssl.create_default_context()
LOGO = None  # unused; keeps flake quiet


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")


def init_tables():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS seller_profiles(
        domain TEXT PRIMARY KEY, data TEXT NOT NULL,
        fetched_at TEXT, ok INTEGER DEFAULT 0, error TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS seller_addresses(
        vid INTEGER NOT NULL, chain TEXT NOT NULL, addr TEXT NOT NULL,
        proven INTEGER DEFAULT 0, created_at TEXT,
        PRIMARY KEY(vid, chain, addr))""")
    c.commit()
    c.close()


def verified_domains():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT id, domain, addr FROM verifications WHERE status='verified' "
        "ORDER BY id")]
    c.close()
    return rows


def fetch_wellknown(domain):
    url = "https://%s/.well-known/x402.json" % domain
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def parse(d):
    networks = d.get("networks") or (
        [d["network"]] if d.get("network") else [])
    addrs = {}

    def put(a, net):
        if isinstance(a, str) and a:
            key = a.lower() if a.startswith("0x") else a
            addrs.setdefault(key, set()).add(net or "")

    put(d.get("payTo"), d.get("network"))
    resources = d.get("resources") or []
    for r in resources:
        for acc in r.get("accepts") or []:
            put(acc.get("payTo"), acc.get("network"))
    return {
        "x402Version": d.get("x402Version"),
        "name": d.get("name"),
        "alias": d.get("alias"),
        "description": (d.get("description") or "")[:400],
        "website": d.get("website"),
        "facilitator": d.get("facilitator"),
        "networks": networks,
        "addresses": [{"addr": a, "networks": sorted(n for n in nets if n)}
                      for a, nets in sorted(addrs.items())],
        "resources": len(resources),
        "offers": sum(len(r.get("accepts") or []) for r in resources),
    }


def main():
    init_tables()
    out = {}
    n_ok = 0
    for v in verified_domains():
        dom = v["domain"]
        rec = {"vid": v["id"], "evm_addr": v["addr"], "ok": 0,
               "fetched_at": ts(), "error": None, "profile": None}
        try:
            rec["profile"] = parse(fetch_wellknown(dom))
            rec["ok"] = 1
            n_ok += 1
        except Exception as ex:  # noqa: BLE001
            rec["error"] = str(ex)[:160]
        out[dom] = rec
        c = sqlite3.connect(DB)
        c.execute(
            "INSERT INTO seller_profiles(domain,data,fetched_at,ok,error) "
            "VALUES(?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET "
            "data=excluded.data, fetched_at=excluded.fetched_at, "
            "ok=excluded.ok, error=excluded.error",
            (dom, json.dumps(rec), rec["fetched_at"], rec["ok"],
             rec["error"]))
        c.commit()
        c.close()
    pub = {
        "generated_at": ts(),
        "note": "Seller-declared x402 profiles (/.well-known/x402.json) for "
                "domains with proven DNS control on PayCensus Trust. "
                "Self-declared metadata; domain control and payment-address "
                "binding are verified, offer contents are not.",
        "sellers": out,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pub, f)
    os.replace(tmp, OUT)
    print("seller profiles: %d verified domains, %d with x402.json"
          % (len(out), n_ok))


if __name__ == "__main__":
    main()
