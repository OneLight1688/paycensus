#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus v0.2 Solana Adapter — x402 facilitator settlement indexer
- Programs: Meridian (meridian_x402, active), PayAI (stale, kept for completeness)
- Incremental: per-program signature cursor in meta; getSignaturesForAddress until cursor
- Settlement extraction:
    * Meridian: top-level instruction accounts layout
      [payer, from(buyer), config, usdc_mint, from_token, recipient_token, (platform), (treasury), token_program]
      -> buyer=accounts[1], seller=owner(recipient_token), value=USDC moved to recipient
    * fallback (PayAI / unknown layout): generic transferChecked pairing
      buyer=authority, seller=owner(destination), value=amount
- Token-account owners cached in token_owners table (permanent mapping)
- Writes chain='Solana' into the shared aggregate SQLite; refreshes stats.json
Cron every minute (lockfile-guarded). Cost: $0 (public RPC).
"""
import base64
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PROGRAMS = {
    "Meridian": "Ro6hz1smrm5zDh73849eDqKna9dE1EkPsWekAB5rBWm",
    "PayAI": "5FhmaXvWm1FZ3bpsE5rxkey5pNWDLkvaGAzoGkTUZfZ3",
}
DB_PATH = os.environ.get("PAYCENSUS_DB", "/var/lib/paycensus/census.db")
STATS_OUT = os.environ.get("PAYCENSUS_STATS", "/www/wwwroot/paycensus/data/stats.json")
AUDIT_OUT = os.environ.get("PAYCENSUS_AUDIT", "/www/wwwroot/paycensus/data/audit.json")
CAL_OUT = os.environ.get("PAYCENSUS_CAL", "/www/wwwroot/paycensus/data/calendar.json")
LOCK_PATH = os.environ.get("PAYCENSUS_LOCK", "/tmp/paycensus_sol.lock")
CH = "Solana"
BUCKETS = [("<1", 1.0), ("1-10", 10.0), ("10-100", 100.0), ("100-1k", 1000.0), (">=1k", None)]


def rpc(method, params, tries=6, timeout=40):
    last = None
    for i in range(tries):
        ep = RPCS[i % len(RPCS)]
        try:
            req = urllib.request.Request(
                ep,
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError(json.dumps(d["error"])[:180])
            return d["result"]
        except Exception as e:  # noqa: BLE001
            last = f"{ep[:30]}: {str(e)[:140]}"
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"rpc exhausted: {last}")


def bucket_of(v):
    for name, hi in BUCKETS:
        if hi is None or v < hi:
            return name
    return ">=1k"


def open_db():
    conn = sqlite3_connect()
    return conn


def sqlite3_connect():
    import sqlite3
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS daily (
      day TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
      buyers INTEGER DEFAULT 0, sellers INTEGER DEFAULT 0, max_block INTEGER DEFAULT 0,
      PRIMARY KEY (day, chain));
    CREATE TABLE IF NOT EXISTS sellers (
      addr TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
      first_day TEXT, last_day TEXT, PRIMARY KEY (addr, chain));
    CREATE TABLE IF NOT EXISTS buyers (
      addr TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
      first_day TEXT, last_day TEXT, PRIMARY KEY (addr, chain));
    CREATE TABLE IF NOT EXISTS buckets (
      bucket TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
      PRIMARY KEY (bucket, chain));
    CREATE TABLE IF NOT EXISTS edges (
      buyer TEXT, seller TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
      first_day TEXT, last_day TEXT, PRIMARY KEY (buyer, seller, chain));
    CREATE TABLE IF NOT EXISTS token_owners (
      token_account TEXT PRIMARY KEY, owner TEXT, chain TEXT);
    CREATE TABLE IF NOT EXISTS daily_sellers (
      day TEXT, chain TEXT, addr TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
      PRIMARY KEY (day, chain, addr));
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT, tool TEXT, chain TEXT, status TEXT, cursor TEXT,
      scanned TEXT, new_items INTEGER, totals_txs INTEGER, totals_volume REAL,
      duration_ms INTEGER, detail TEXT);
    """)
    return conn


def owner_of(conn, token_account):
    """Resolve token-account owner (cached permanently)."""
    if not token_account:
        return None
    row = conn.execute("SELECT owner FROM token_owners WHERE token_account=?", (token_account,)).fetchone()
    if row:
        return row[0]
    try:
        res = rpc("getMultipleAccounts", [[token_account], {"encoding": "jsonParsed"}]) or []
        v = (res.get("value") or [None])[0]
        owner = (v or {}).get("data", {}).get("parsed", {}).get("info", {}).get("owner")
    except Exception:  # noqa: BLE001
        owner = None
    if owner:
        conn.execute("INSERT OR REPLACE INTO token_owners VALUES(?,?,?)", (token_account, owner, CH))
        conn.commit()
    return owner


def day_of(slot):
    try:
        ts = rpc("getBlockTime", [slot])
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def extract_settlements(conn, tx, program):
    """Return list of (buyer, seller, value) from one parsed tx."""
    if not tx:
        return []
    msg = tx.get("transaction", {}).get("message", {})
    account_keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys", [])]
    out = []

    def usdc_moves():
        moves = []
        for inner in (tx.get("meta", {}).get("innerInstructions") or []):
            pin = inner.get("parsed")
            if isinstance(pin, dict) and pin.get("instructions"):
                for ins in pin["instructions"]:
                    if ins.get("program") == "spl-token" and ins.get("parsed", {}).get("type") == "transferChecked":
                        info = ins["parsed"]["info"]
                        if info.get("mint") == USDC_MINT:
                            amt = (info.get("tokenAmount") or {}).get("uiAmount") or 0.0
                            moves.append((info.get("source"), info.get("destination"), amt, info.get("authority")))
        return moves

    if program == "Meridian":
        for ins in msg.get("instructions", []):
            pid = ins.get("programId") or ""
            if isinstance(pid, dict):
                pid = pid.get("pubkey", "")
            if pid != PROGRAMS["Meridian"]:
                continue
            accs = ins.get("accounts", [])

            def acc(i):
                if i >= len(accs):
                    return None
                a = accs[i]
                if isinstance(a, str) and len(a) > 30:
                    return a
                try:
                    return account_keys[int(a)]
                except Exception:  # noqa: BLE001
                    return None

            buyer = acc(1)
            recipient_ta = acc(5)
            seller = owner_of(conn, recipient_ta)
            # USDC actually moved to recipient token account
            val = sum(a for _, d, a, _ in usdc_moves() if d == recipient_ta)
            # guards: skip self-transfers and zero-value test calls (observed in the wild)
            if not buyer or val <= 0 or (seller and buyer.lower() == seller.lower()):
                return out
            if seller:
                out.append((buyer, seller, float(val)))
            else:
                out.append((buyer, None, float(val)))
            return out if out else []

    # generic fallback (PayAI or layout mismatch)
    moves = usdc_moves()
    for src, dst, amt, authority in moves:
        seller = owner_of(conn, dst)
        buyer = authority
        if not buyer or amt <= 0 or (seller and buyer.lower() == seller.lower()):
            continue  # guards: skip self-transfers and zero-value test calls
        out.append((buyer, seller, float(amt)))
    return out


def record(conn, settlements, day):
    for buyer, seller, val in settlements:
        conn.execute(
            "INSERT INTO daily(day,chain,txs,volume,max_block) VALUES(?,?,?,?,0) "
            "ON CONFLICT(day,chain) DO UPDATE SET txs=txs+1, volume=volume+?",
            (day, CH, 1, val, val),
        )
        conn.execute(
            "INSERT INTO buyers(addr,chain,txs,volume,first_day,last_day) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(addr,chain) DO UPDATE SET txs=txs+1, volume=volume+?, last_day=?",
            (buyer, CH, 1, val, day, day, val, day),  # base58 is case-sensitive: never lower()
        )
        if seller:
            conn.execute(
                "INSERT INTO sellers(addr,chain,txs,volume,first_day,last_day) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(addr,chain) DO UPDATE SET txs=txs+1, volume=volume+?, last_day=?",
                (seller, CH, 1, val, day, day, val, day),
            )
            conn.execute(
                "INSERT INTO daily_sellers(day,chain,addr,txs,volume) VALUES(?,?,?,?,?) "
                "ON CONFLICT(day,chain,addr) DO UPDATE SET txs=txs+1, volume=volume+?",
                (day, CH, seller, 1, val, val),
            )
            conn.execute(
                "INSERT INTO edges(buyer,seller,chain,txs,volume,first_day,last_day) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(buyer,seller,chain) DO UPDATE SET txs=txs+1, volume=volume+?, last_day=?",
                (buyer, seller, CH, 1, val, day, day, val, day),
            )
        conn.execute(
            "INSERT INTO buckets(bucket,chain,txs,volume) VALUES(?,?,?,?) "
            "ON CONFLICT(bucket,chain) DO UPDATE SET txs=txs+1, volume=volume+?",
            (bucket_of(val), CH, 1, val, val),
        )
    conn.commit()


def write_stats(conn):
    def one(q, *a):
        return conn.execute(q, a).fetchone()

    total_txs, total_vol = one("SELECT COALESCE(SUM(txs),0), COALESCE(SUM(volume),0) FROM daily")
    n_buyers = one("SELECT COUNT(*) FROM buyers")[0]
    n_sellers = one("SELECT COUNT(*) FROM sellers")[0]
    chains = {}
    for ch, t, v in conn.execute("SELECT chain, SUM(txs), SUM(volume) FROM daily GROUP BY chain"):
        b = one("SELECT COUNT(*) FROM buyers WHERE chain=?", ch)[0]
        s = one("SELECT COUNT(*) FROM sellers WHERE chain=?", ch)[0]
        chains[ch] = {"txs": t, "volume_usdc": round(v, 2), "buyers": b, "sellers": s}
    daily = [
        {"day": d, "txs": t, "volume": round(v, 2)}
        for d, t, v in conn.execute("SELECT day, SUM(txs), SUM(volume) FROM daily GROUP BY day ORDER BY day")
    ]
    top_sellers = [
        {"addr": a[:6] + "…" + a[-4:], "addr_full": a, "chain": ch, "txs": t, "volume": round(v, 2)}
        for a, ch, t, v in conn.execute(
            "SELECT addr, chain, txs, volume FROM sellers ORDER BY volume DESC LIMIT 20"
        )
    ]
    buckets = [
        {"bucket": b, "txs": t, "volume": round(v or 0, 2)}
        for b, t, v in conn.execute("SELECT bucket, SUM(txs), SUM(volume) FROM buckets GROUP BY bucket")
    ]
    bmap = {x["bucket"]: x for x in buckets}
    buckets = [bmap.get(name, {"bucket": name, "txs": 0, "volume": 0}) for name, _ in BUCKETS]
    stats = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "chains": chains,
        "totals": {"txs": total_txs, "volume_usdc": round(total_vol, 2),
                   "buyers": n_buyers, "sellers": n_sellers},
        "daily": daily,
        "top_sellers": top_sellers,
        "buckets": buckets,
        "note": "Machine-payment settlements (x402/EIP-3009 and successors), USDC-denominated, indexed per chain. Aggregate-first: raw events are not stored.",
    }
    os.makedirs(os.path.dirname(STATS_OUT), exist_ok=True)
    tmp = STATS_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATS_OUT)
    return stats


PROCESSING_DOC = [
    {"stage": "ingest", "chain": "Solana",
     "method": "getSignaturesForAddress on x402 facilitator programs (Meridian meridian_x402 Ro6hz1…, PayAI 5FhmaX…) via free public RPC; per-program signature cursor; cron every minute"},
    {"stage": "pair",
     "method": "successful tx parsed (jsonParsed): Meridian layout -> buyer=account[1], seller=owner(recipient_token), value=USDC moved to recipient; fallback generic transferChecked pairing; self-transfers and zero-value test calls are filtered out"},
    {"stage": "aggregate",
     "method": "aggregate-first: raw txs are NOT stored. Written tables: daily(day,chain) / sellers / buyers / edges / buckets; token-account owners cached permanently in token_owners"},
    {"stage": "publish",
     "method": "stats.json + audit.json written via atomic replace after every run; each run recorded in runs table"},
]


def write_audit(conn):
    now_dt = datetime.now(timezone.utc)
    cols = ["ts", "tool", "chain", "status", "cursor", "scanned", "new_items",
            "totals_txs", "totals_volume", "duration_ms", "detail"]
    rows = list(conn.execute(
        f"SELECT {','.join(cols)} FROM runs ORDER BY id DESC LIMIT 500"))
    runs = [dict(zip(cols, r)) for r in rows]
    from datetime import timedelta
    cut1 = (now_dt - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    cut24 = (now_dt - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    collectors = {}
    for r in runs:
        c = collectors.setdefault(r["tool"], {
            "chain": r["chain"], "last_run": r["ts"], "last_status": r["status"],
            "cursor": r["cursor"], "new_1h": 0, "new_24h": 0, "runs_24h": 0, "errors_24h": 0})
        if r["ts"] >= cut1:
            c["new_1h"] += r["new_items"] or 0
        if r["ts"] >= cut24:
            c["new_24h"] += r["new_items"] or 0
            c["runs_24h"] += 1
            if r["status"] != "ok":
                c["errors_24h"] += 1
    agg = dict(conn.execute(
        "SELECT 'daily', COUNT(*) FROM daily UNION ALL SELECT 'sellers', COUNT(*) FROM sellers "
        "UNION ALL SELECT 'buyers', COUNT(*) FROM buyers UNION ALL SELECT 'edges', COUNT(*) FROM edges "
        "UNION ALL SELECT 'buckets', COUNT(*) FROM buckets"))
    tv = conn.execute("SELECT COALESCE(SUM(txs),0), COALESCE(SUM(volume),0) FROM daily").fetchone()
    audit = {
        "generated_at": now_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "collectors": [{"tool": k, **v} for k, v in collectors.items()],
        "processing": PROCESSING_DOC,
        "aggregate_counts": agg,
        "totals": {"txs": tv[0], "volume_usdc": round(tv[1], 2)},
        "runs": runs[:100],
    }
    os.makedirs(os.path.dirname(AUDIT_OUT), exist_ok=True)
    tmp = AUDIT_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, AUDIT_OUT)


def build_calendar(conn):
    """Same calendar contract as the Base indexer; kept local for standalone resilience."""
    days = {}
    for d, ch, t, v in conn.execute("SELECT day, chain, txs, volume FROM daily ORDER BY day"):
        e = days.setdefault(d, {"day": d, "chains": {}, "txs": 0, "volume": 0.0})
        e["chains"][ch] = {"txs": t, "volume": round(v, 2)}
        e["txs"] += t
        e["volume"] = round(e["volume"] + v, 2)
    for tbl, key in [("buyers", "new_buyers"), ("sellers", "new_sellers"), ("edges", "new_edges")]:
        for d, n in conn.execute(f"SELECT first_day, COUNT(*) FROM {tbl} GROUP BY first_day"):
            if d in days:
                days[d][key] = n
    for d, a, ch, t, v in conn.execute(
        "SELECT ds.day, ds.addr, ds.chain, ds.txs, ds.volume FROM daily_sellers ds "
        "JOIN (SELECT day, chain, MAX(volume) AS mv FROM daily_sellers GROUP BY day, chain) m "
        "ON ds.day=m.day AND ds.chain=m.chain AND ds.volume=m.mv"):
        days.setdefault(d, {"day": d, "chains": {}, "txs": 0, "volume": 0.0}) \
            .setdefault("top_seller_by_chain", {})[ch] = {"addr": a, "txs": t, "volume": round(v, 2)}
    for d in days.values():
        d["avg_value_usdc"] = round(d["volume"] / d["txs"], 4) if d["txs"] else 0
    cal = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "note": "Machine-payment calendar. Day boundaries UTC. new_* = entities first seen that day. "
                "top_seller_by_chain = highest-volume payTo that day. History starts 2026-10-31 (never backfilled).",
        "days": [days[k] for k in sorted(days)],
    }
    os.makedirs(os.path.dirname(CAL_OUT), exist_ok=True)
    tmp = CAL_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, CAL_OUT)


def main():
    if os.path.exists(LOCK_PATH):
        try:
            if time.time() - os.path.getmtime(LOCK_PATH) < 900:
                print("locked, skip")
                return
        except OSError:
            pass
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    try:
        t0 = time.time()
        conn = open_db()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total_new = 0
        cursor_info = {}
        err_detail = None
        for name, program in PROGRAMS.items():
            key = f"sol_lastsig_{name}"
            row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            last_sig = row[0] if row else None
            opts = {"limit": 200}
            if last_sig:
                opts["until"] = last_sig
            sigs = rpc("getSignaturesForAddress", [program, opts]) or []
            if not last_sig and sigs:
                # first run: start from now (history begins today)
                first = sigs[0]["signature"]
                conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, first))
                conn.commit()
                print(f"{name}: initialized cursor at latest signature")
                continue
            # process oldest first
            new_sigs = [s for s in reversed(sigs) if not s.get("err")]
            n_settle = 0
            for s in new_sigs:
                try:
                    tx = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                    settlements = extract_settlements(conn, tx, name)
                    d = day_of(s.get("slot")) if s.get("slot") else day
                    record(conn, settlements, d)
                    n_settle += len(settlements)
                    cursor_info[name] = s["signature"][:16]
                    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, s["signature"]))
                    conn.commit()
                    time.sleep(0.4)
                except Exception as e:  # noqa: BLE001
                    err_detail = f"{name} tx {s['signature'][:12]}: {str(e)[:140]}"
                    print(f"{name} tx {s['signature'][:12]} failed: {str(e)[:120]}")
                    break
            if not cursor_info.get(name):
                row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
                cursor_info[name] = (row[0][:16] if row else "init")
            total_new += n_settle
            print(f"{name}: +{n_settle} settlements ({len(new_sigs)} new sigs)")
        stats = write_stats(conn)
        dur = int((time.time() - t0) * 1000)
        conn.execute(
            "INSERT INTO runs(ts,tool,chain,status,cursor,scanned,new_items,totals_txs,totals_volume,duration_ms,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "solana-indexer", "Solana",
             "ok" if err_detail is None else "partial", json.dumps(cursor_info),
             f"{len(PROGRAMS)} programs polled", total_new,
             stats["totals"]["txs"], stats["totals"]["volume_usdc"], dur, err_detail),
        )
        conn.commit()
        write_audit(conn)
        build_calendar(conn)
        print(f"solana done: +{total_new}; totals={stats['totals']}; chains={list(stats['chains'].keys())}")
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
