#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Enrichment v2 booster — hit-rate improvement over enrich_v1.
  Pass A (--pass a): address-scan every origin whose root returned 402:
      fetch /.well-known/x402 (origin + registrable root) and registrable root HTML,
      extract EVM addresses, intersect with on-chain sellers -> new entity matches.
      Non-seller addresses are kept as addr_hints (catalog, no entity row).
  Pass B (--pass b): retry once every origin whose v1 status was transient failure
      (err:*, 5xx, 429). New paytos -> entities. Then republish entities.json.
State-compatible with enrich_v1 (same state file; extra keys are additive).
Run: python3 enrich_v2.py --pass a   (sync, ~3 min)
     nohup python3 enrich_v2.py --pass b > /var/lib/paycensus/v2_passb.log 2>&1 &
"""
import base64
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
STATE = "/var/lib/paycensus/enrich_state.json"
OUT = "/www/wwwroot/paycensus/data/entities.json"
UA = {"User-Agent": "Mozilla/5.0 (PayCensus indexer; +https://paycensus.com)", "Accept": "*/*"}
ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")
MULTI_LABEL_TLDS = {"co.uk", "com.br", "com.au", "co.jp", "org.uk", "net.au", "co.nz", "com.cn"}


def log(s):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {s}", flush=True)


def fetch(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(300000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(300000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body
    except Exception as e:  # noqa: BLE001
        return None, f"err:{str(e)[:40]}"


def registrable(host):
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def parse_xpayment(xp):
    paytos = []
    try:
        d = json.loads(base64.b64decode(xp + "=" * (-len(xp) % 4)))
        for acc in d.get("accepts", []):
            pt = acc.get("payTo")
            if pt:
                paytos.append(pt.lower())
    except Exception:  # noqa: BLE001
        pass
    return paytos


def probe_payto(origin):
    """Same semantics as enrich_v1.probe_payto (cap 5 addrs)."""
    try:
        req = urllib.request.Request(origin + "/", headers=UA)
        urllib.request.urlopen(req, timeout=10)
        return [], 200
    except urllib.error.HTTPError as e:
        if e.code != 402:
            return [], e.code
        paytos = parse_xpayment(e.headers.get("X-PAYMENT", ""))
        if not paytos:
            try:
                body = e.read(20000).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            paytos = [m.lower() for m in ADDR_RE.findall(body)][:5]
        return paytos, 402
    except Exception as e:  # noqa: BLE001
        return [], f"err:{str(e)[:40]}"


def insert_hits(conn, addrs, sellers, origin, o, source):
    n = 0
    for pt in addrs:
        if pt in sellers:
            conn.execute(
                "INSERT OR REPLACE INTO entities(addr,domain,title,description,source,first_seen) "
                "VALUES(?,?,?,?,?,?)",
                (pt, origin, (o.get("title") or "")[:200], (o.get("description") or "")[:1500],
                 source, datetime.now(timezone.utc).strftime("%Y-%m-%d")))
            n += 1
    return n


def publish(conn):
    entities = [dict(zip(["addr", "domain", "title", "description", "source", "first_seen"], r))
                for r in conn.execute(
                    "SELECT addr,domain,title,description,source,first_seen FROM entities "
                    "WHERE addr IN (SELECT addr FROM sellers) ORDER BY first_seen DESC LIMIT 2000")]
    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
               "note": "payTo -> domain/title/description enrichment (x402scan origins catalog + "
                       "402 probing + well-known/address-scan v2); only entities matched to "
                       "on-chain sellers are published here",
               "count": len(entities), "entities": entities},
              open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
    log(f"published entities.json ({len(entities)})")


def pass_a(conn, state, sellers):
    targets = {oid: v for oid, v in state["probed"].items() if v.get("status") == "402"}
    log(f"pass A: address-scan {len(targets)} 402-origins")
    hints = state.setdefault("addr_hints", {})
    hits = scanned = 0
    for oid, v in sorted(targets.items()):
        if v.get("scanned"):  # idempotent
            continue
        origin = v["origin"].rstrip("/")
        host = origin.split("://", 1)[-1].split("/")[0].lower()
        rd = registrable(host)
        o = {"title": None, "description": None}  # catalog title/desc only in catalog file
        addrs = set()
        for url in [origin + "/.well-known/x402", f"https://{rd}/.well-known/x402", f"https://{rd}/"]:
            _, body = fetch(url)
            if body and not body.startswith("err:"):
                addrs.update(m.lower() for m in ADDR_RE.findall(body))
        scanned += 1
        hits += insert_hits(conn, addrs, sellers, origin, o, "wellknown-scan")
        for a in addrs:
            if a not in sellers and len(hints) < 5000:
                hints[a] = origin
        v["scanned"] = True
        if scanned % 25 == 0:
            json.dump(state, open(STATE, "w"))
            conn.commit()
            log(f"pass A {scanned}/{len(targets)}: new seller-hits so far {hits}")
        time.sleep(0.25)
    json.dump(state, open(STATE, "w"))
    conn.commit()
    log(f"pass A done: scanned={scanned} new_seller_hits={hits}")


def pass_b(conn, state, sellers):
    def transient(st):
        return st.startswith("err:") or (st[:1] == "5") or st == "429"
    targets = {oid: v for oid, v in state["probed"].items()
               if transient(v.get("status", "")) and not v.get("retried")}
    log(f"pass B: retrying {len(targets)} transient-failure origins")
    done = hits = 0
    for oid, v in sorted(targets.items()):
        origin = v["origin"].rstrip("/")
        paytos, st = probe_payto(origin)
        v["retried"] = f"v2:{st}"
        if paytos:
            old = set(v.get("paytos") or [])
            if set(paytos) - old:
                v["paytos"] = sorted(set(paytos) | old)
                v["status"] = "402"
                hits += insert_hits(conn, paytos, sellers, origin, {"title": None, "description": None},
                                    "retry-probe")
                v["matched_seller"] = bool(set(paytos) & sellers)
        done += 1
        if done % 25 == 0:
            json.dump(state, open(STATE, "w"))
            conn.commit()
            log(f"pass B {done}/{len(targets)}: new seller-hits {hits}")
        time.sleep(0.3)
    json.dump(state, open(STATE, "w"))
    conn.commit()
    log(f"pass B done: retried={done} new_seller_hits={hits}")


def main():
    which = sys.argv[sys.argv.index("--pass") + 1] if "--pass" in sys.argv else "a"
    conn = sqlite3.connect(DB, timeout=30)
    state = json.load(open(STATE))
    sellers = {r[0].lower() for r in conn.execute("SELECT addr FROM sellers")}
    if which == "a":
        pass_a(conn, state, sellers)
    elif which == "b":
        pass_b(conn, state, sellers)
    else:
        log("unknown pass"); return
    nhits = conn.execute(
        "SELECT COUNT(*) FROM entities e WHERE EXISTS(SELECT 1 FROM sellers s WHERE s.addr=e.addr)"
    ).fetchone()[0]
    publish(conn)
    conn.close()
    log(f"total matched sellers now: {nhits}")


if __name__ == "__main__":
    main()
