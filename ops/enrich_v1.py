#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Entity Enrichment v1 — payTo -> domain + commercial description
Pipeline:
  1) pull x402scan origins catalog (tRPC public.origins.list.origins, free)
  2) probe each origin root: HTTP 402 -> parse X-PAYMENT accepts[].payTo
     + protocol signature tag (x402 / mpp / dual, v0.9.0)
  3) matched payTo -> entities table (addr, domain, title, description) + reconcile with sellers
  4) publish /data/entities.json
Run: nohup python3 enrich_v1.py > enrich.log 2>&1 &   (throttled ~1.1s/req; resumable via state file)
Also cron-able for incremental runs (state file skips already-probed origins).
Backfill (one-off, tag sigs on already-probed 402 origins): python3 enrich_v1.py --backfill
"""
import base64
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TRPC = "https://www.x402scan.com/api/trpc/public.origins.list.origins"
HEADERS = {"User-Agent": "Mozilla/5.0 (PayCensus indexer; +https://paycensus.com)", "Accept": "*/*"}
DB = "/var/lib/paycensus/census.db"
STATE = "/var/lib/paycensus/enrich_state.json"
OUT = "/www/wwwroot/paycensus/data/entities.json"
THROTTLE = 1.1

SCHEMA_ENTITY = """
CREATE TABLE IF NOT EXISTS entities (
  addr TEXT PRIMARY KEY, domain TEXT, title TEXT, description TEXT,
  source TEXT DEFAULT 'x402scan-probe', first_seen TEXT
);
"""


def log(s):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {s}", flush=True)


def pull_origins():
    req = urllib.request.Request(
        TRPC + "?input=" + urllib.parse.quote(json.dumps({"json": {"limit": 100, "offset": 0}})),
        headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    data = d["result"]["data"]["json"]
    items = data if isinstance(data, list) else data.get("items", [])
    seen, uniq = set(), []
    for o in items:
        if o["id"] not in seen and o.get("origin"):
            seen.add(o["id"])
            uniq.append(o)
    log(f"origins catalog: {len(uniq)} unique")
    return uniq


def probe_payto(origin):
    """GET origin root; 402 -> parse payTo list + protocol signature + offer quote.
    Returns (paytos, http_status, sig, offer, unknown_schemes);
    sig in {'x402','mpp','dual',None}.
    Signature rules (validated 2026-11-06 MPP recon):
      x402: X-PAYMENT header, or x402/"accepts" in body, or WWW-Authenticate: X402
      MPP:  WWW-Authenticate: Payment, Payment-Receipt/Payment-Response header,
            or machine-payments mention in body
    offer: unified quote metadata (amount/currency/network/payTo/...) from both
    families' challenge payloads — the offer graph, accumulated from 2026-11-06.
    unknown_schemes: WWW-Authenticate schemes outside {x402, payment} — the
    third-protocol radar (v0.14.0); a new family announces itself here first.
    """
    try:
        req = urllib.request.Request(origin + "/", headers=HEADERS)
        urllib.request.urlopen(req, timeout=10)
        return [], 200, None, None, []
    except urllib.error.HTTPError as e:
        hdrs, hdrs_all = {}, {}
        try:
            for k, v in e.headers.items():
                k = k.lower()
                v = v or ""
                hdrs.setdefault(k, v)          # first value wins for single lookups
                hdrs_all.setdefault(k, []).append(v)  # keep every repeated header
        except Exception:  # noqa: BLE001
            pass
        try:
            body = e.read().decode("utf-8", "replace")[:20000]
        except Exception:  # noqa: BLE001
            body = ""
        bl = body.lower()
        wa_schemes = [v.strip().split(None, 1)[0].lower()
                      for v in hdrs_all.get("www-authenticate", [])]
        is_x402 = ("x-payment" in hdrs or "x402" in bl or '"accepts"' in bl
                   or "x402" in wa_schemes)
        is_mpp = ("payment" in wa_schemes
                  or "payment-receipt" in hdrs or "payment-response" in hdrs
                  or "machine-payments" in bl)
        sig = ("dual" if (is_x402 and is_mpp)
               else "x402" if is_x402
               else "mpp" if is_mpp else None)
        # third-protocol radar: record every WWW-Authenticate scheme verbatim —
        # full scheme list stored; scheme_radar splits known/unknown at publish time.
        # any scheme outside {x402, payment} announces a new protocol family
        schemes_all = sorted({s for s in wa_schemes if s})
        paytos = []
        xp = hdrs.get("x-payment", "")
        try:
            d = json.loads(base64.b64decode(xp + "=" * (-len(xp) % 4)))
            for acc in d.get("accepts", []):
                pt = acc.get("payTo")
                if pt:
                    paytos.append(pt.lower())
        except Exception:  # noqa: BLE001
            pass
        if not paytos:  # fallback: scan body for EVM addrs
            paytos = [m.lower() for m in re.findall(r"0x[a-fA-F0-9]{40}", body)][:3]
        return paytos, e.code, sig, parse_offer(hdrs_all, bl), schemes_all
    except Exception as e:  # noqa: BLE001
        return [], f"err:{str(e)[:40]}", None, None, []


def _b64json(s):
    try:
        return json.loads(base64.b64decode(s + "=" * (-len(s) % 4)))
    except Exception:  # noqa: BLE001
        return None


def _parse_www_auth(v):
    """WWW-Authenticate header -> dict(scheme, params..., request-b64 unfolded)."""
    out = {"scheme": v.strip().split(None, 1)[0].lower() if v.strip() else ""}
    for k, val in re.findall(r'([a-zA-Z-]+)="([^"]*)"', v):
        out[k.lower()] = val
    req = out.pop("request", None)
    if req:
        d = _b64json(req)
        if isinstance(d, dict):
            for k in ("amount", "currency", "network", "recipient", "asset",
                      "resource", "description", "method"):
                if k in d:
                    out[k] = d[k]
    return out


def parse_offer(hdrs_all, body_lower):
    """Unified offer/quote metadata from a 402 challenge (x402 + MPP)."""
    o = {}
    for v in hdrs_all.get("www-authenticate", []):
        p = _parse_www_auth(v)
        if p.get("scheme") == "payment" and "mpp" not in o:
            o["mpp"] = {k: val for k, val in p.items() if k != "scheme"}
    # x402 v2 challenge (Payment-Required header, base64 x402v2 JSON)
    d = _b64json(hdrs_all.get("payment-required", [""])[0])
    src = d.get("accepts") if isinstance(d, dict) else None
    # legacy x402 X-PAYMENT header
    if not src and hdrs_all.get("x-payment"):
        d = _b64json(hdrs_all["x-payment"][0])
        src = d.get("accepts") if isinstance(d, dict) else None
    if src:
        a = src[0]
        o["x402"] = {
            "network": a.get("network"),
            "amount": a.get("amount"),
            "asset": (a.get("extra") or {}).get("name") or a.get("asset"),
            "payto": a.get("payTo"),
        }
    # flat gateway-style headers
    flat = {}
    for hk, ok in (("x-payment-amount", "amount"), ("x-payment-network", "network"),
                   ("x-payment-payto", "payto"), ("x-payment-protocols", "protocols")):
        if hdrs_all.get(hk):
            flat[ok] = hdrs_all[hk][0]
    if flat:
        if "x402" in o:
            for k, v in flat.items():
                o["x402"].setdefault(k, v)
        else:
            o["x402"] = flat
    return o or None


def backfill(state, conn):
    """Re-probe already-probed 402 origins lacking sig/offer/schemes; update all."""
    sellers = {r[0] for r in conn.execute("SELECT addr FROM sellers")}
    todo = [oid for oid, v in state["probed"].items()
            if v.get("status") == "402"
            and ("sig" not in v or "offer" not in v or "schemes" not in v
                 or (v.get("sig") in ("x402", "mpp", "dual")
                     and not any(s in v.get("schemes", []) for s in ("x402", "payment"))))]
    log(f"backfill: {len(todo)} 402 origins to re-probe")
    n = 0
    for oid in todo:
        v = state["probed"][oid]
        paytos, st, sig, offer, schemes = probe_payto(v["origin"])
        v["sig"] = sig
        if str(st) == "402":
            v["paytos"] = paytos
            v["matched_seller"] = bool(set(paytos) & sellers)
            v["schemes"] = schemes
            if offer:
                v.setdefault("offer_at", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                v["offer"] = offer
        n += 1
        if n % 20 == 0:
            json.dump(state, open(STATE, "w"))
            log(f"backfill {n}/{len(todo)}: "
                f"{sum(1 for x in state['probed'].values() if x.get('offer'))} offers")
        time.sleep(THROTTLE)
    json.dump(state, open(STATE, "w"))
    sigs, offers = {}, sum(1 for x in state["probed"].values() if x.get("offer"))
    for v in state["probed"].values():
        s = v.get("sig")
        if s:
            sigs[s] = sigs.get(s, 0) + 1
    log(f"backfill DONE ({n}): signatures {sigs}, offers {offers}")


def main():
    conn = sqlite3.connect(DB, timeout=30)
    conn.executescript(SCHEMA_ENTITY)
    state = {"probed": {}}
    if os.path.exists(STATE):
        state = json.load(open(STATE))

    if "--backfill" in sys.argv:
        backfill(state, conn)
        conn.close()
        return

    sellers = {r[0] for r in conn.execute("SELECT addr FROM sellers")}

    origins = pull_origins()
    # persist raw catalog for audit
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump({"pulled_at": datetime.now(timezone.utc).isoformat(), "origins": origins},
              open("/var/lib/paycensus/origins_catalog.json", "w"), ensure_ascii=False)

    matched, probed_n, hits = 0, 0, 0
    for o in origins:
        oid = o["id"]
        if oid in state["probed"]:
            continue
        origin = o["origin"].rstrip("/")
        probed_n += 1
        paytos, st, sig, offer, schemes = probe_payto(origin)
        entry = {"origin": origin, "status": str(st), "paytos": paytos, "sig": sig,
                 "schemes": schemes}
        if offer:
            entry["offer_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry["offer"] = offer
        state["probed"][oid] = entry
        if paytos:
            matched += 1
            for pt in paytos:
                if pt in sellers:
                    hits += 1
                    conn.execute(
                        "INSERT OR REPLACE INTO entities(addr,domain,title,description,source,first_seen) "
                        "VALUES(?,?,?,?,?,?)",
                        (pt, origin, (o.get("title") or "")[:200],
                         (o.get("description") or "")[:1500], "x402scan-probe",
                         datetime.now(timezone.utc).strftime("%Y-%m-%d")))
            if paytos:
                # keep unmatched paytos too (entity catalog for future sellers)
                for pt in paytos:
                    if pt not in sellers:
                        conn.execute(
                            "INSERT OR IGNORE INTO entities(addr,domain,title,description,source,first_seen) "
                            "VALUES(?,?,?,?,?,?)",
                            (pt, origin, (o.get("title") or "")[:200],
                             (o.get("description") or "")[:1500], "x402scan-probe",
                             datetime.now(timezone.utc).strftime("%Y-%m-%d")))
        state["probed"][oid]["matched_seller"] = bool(set(paytos) & sellers)
        if probed_n % 20 == 0:
            json.dump(state, open(STATE, "w"))
            conn.commit()
            log(f"probed {probed_n}, 402-with-payto {matched}, seller-hits {hits}")
        time.sleep(THROTTLE)

    json.dump(state, open(STATE, "w"))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    nhits = conn.execute(
        "SELECT COUNT(*) FROM entities e WHERE EXISTS(SELECT 1 FROM sellers s WHERE s.addr=e.addr)"
    ).fetchone()[0]
    conn.close()

    # publish entities.json
    conn = sqlite3.connect(DB, timeout=30)
    entities = [dict(zip(["addr", "domain", "title", "description", "source", "first_seen"], r))
                for r in conn.execute(
                    "SELECT addr,domain,title,description,source,first_seen FROM entities "
                    "WHERE addr IN (SELECT addr FROM sellers) ORDER BY first_seen DESC LIMIT 2000")]
    conn.close()
    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
               "note": "payTo -> domain/title/description enrichment (x402scan origins catalog + 402 probing); only entities matched to on-chain sellers are published here",
               "count": len(entities), "entities": entities},
              open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
    log(f"DONE: probed={probed_n} catalog_entities={n} matched_sellers={nhits} -> entities.json ({len(entities)} published)")


if __name__ == "__main__":
    main()
