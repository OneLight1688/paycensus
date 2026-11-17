#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Indexer — machine-payment census
- Base USDC x402/EIP-3009 settlements: counted (this script, cron every minute)
- MPP/Tempo: sampled hourly by tempo_probe.py (estimates in stats.json tempo_mpp / mpp.json)
- Incremental: scans blocks from last_cursor to head, 50-block chunks (pokt free limit)
- Event pairing: AuthorizationUsed(authorizer, nonce) marks a settlement tx;
  paired Transfer(from=authorizer) in same tx gives value/from/to
- Aggregate-first SQLite: daily rollups, seller/buyer totals, amount buckets
- Emits stats.json for the static panel
Run via cron every minute (lockfile-guarded). Cost: $0.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ---- config ----
RPC = "https://base-pokt.nodies.app"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TOPIC_AUTH = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"  # AuthorizationUsed(address,bytes32)
TOPIC_TRF = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # Transfer(address,address,uint256)
DB_PATH = os.environ.get("PAYCENSUS_DB", "/var/lib/paycensus/census.db")
STATS_OUT = os.environ.get("PAYCENSUS_STATS", "/www/wwwroot/paycensus/data/stats.json")
AUDIT_OUT = os.environ.get("PAYCENSUS_AUDIT", "/www/wwwroot/paycensus/data/audit.json")
CAL_OUT = os.environ.get("PAYCENSUS_CAL", "/www/wwwroot/paycensus/data/calendar.json")
LOCK_PATH = os.environ.get("PAYCENSUS_LOCK", "/tmp/paycensus_indexer.lock")
CHUNK = 50          # pokt free plan max block range
MAX_CHUNKS_PER_RUN = 24  # ~2h of chain per run; cron re-triggers, catches up gradually
BURN_IN = 6         # ignore head-6 blocks (reorg safety)
START_BLOCK_ENV = "PAYCENSUS_START_BLOCK"

DAY = os.environ.get("PAYCENSUS_DAY", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

# ---- rpc ----
def rpc(method, params, tries=5, timeout=45):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                RPC,
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError(json.dumps(d["error"])[:200])
            return d["result"]
        except Exception as e:  # noqa: BLE001
            last = str(e)[:200]
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"rpc exhausted: {last}")


def head():
    return int(rpc("eth_blockNumber", []), 16)


# ---- db ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS daily (
  day TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
  buyers INTEGER DEFAULT 0, sellers INTEGER DEFAULT 0, max_block INTEGER DEFAULT 0,
  PRIMARY KEY (day, chain)
);
CREATE TABLE IF NOT EXISTS sellers (
  addr TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
  first_day TEXT, last_day TEXT, PRIMARY KEY (addr, chain)
);
CREATE TABLE IF NOT EXISTS buyers (
  addr TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
  first_day TEXT, last_day TEXT, PRIMARY KEY (addr, chain)
);
CREATE TABLE IF NOT EXISTS buckets (
  bucket TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
  PRIMARY KEY (bucket, chain)
);
CREATE TABLE IF NOT EXISTS edges (
  buyer TEXT, seller TEXT, chain TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
  first_day TEXT, last_day TEXT, PRIMARY KEY (buyer, seller, chain)
);
CREATE TABLE IF NOT EXISTS daily_sellers (
  day TEXT, chain TEXT, addr TEXT, txs INTEGER DEFAULT 0, volume REAL DEFAULT 0,
  PRIMARY KEY (day, chain, addr)
);
CREATE TABLE IF NOT EXISTS token_owners (
  token_account TEXT PRIMARY KEY, owner TEXT, chain TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, tool TEXT, chain TEXT, status TEXT, cursor TEXT,
  scanned TEXT, new_items INTEGER, totals_txs INTEGER, totals_volume REAL,
  duration_ms INTEGER, detail TEXT
);
"""

BUCKETS = [
    ("<1", 1.0),
    ("1-10", 10.0),
    ("10-100", 100.0),
    ("100-1k", 1000.0),
    (">=1k", None),
]


def bucket_of(v):
    for name, hi in BUCKETS:
        if hi is None or v < hi:
            return name
    return ">=1k"


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.executescript(SCHEMA)
    return conn


# ---- indexing core ----
def scan_chunk(conn, start, end):
    """Scan one [start,end] chunk; return (settlements, head_block)."""
    logs = rpc("eth_getLogs", [{"address": USDC, "topics": [[TOPIC_AUTH, TOPIC_TRF]],
                                "fromBlock": hex(start), "toBlock": hex(end)}]) or []
    # group by tx
    txs = {}
    for lg in logs:
        t0 = (lg.get("topics") or ["?"])[0]
        txs.setdefault(lg["transactionHash"], []).append(lg)

    day = DAY
    settlements = []
    for txh, evs in txs.items():
        auths = [e for e in evs if e["topics"][0] == TOPIC_AUTH]
        if not auths:
            continue  # plain transfer, not a settlement
        trfs = [e for e in evs if e["topics"][0] == TOPIC_TRF]
        for a in auths:
            authorizer = "0x" + a["topics"][1][-40:]
            # find Transfer from this authorizer in same tx
            val = None
            seller = None
            for t in trfs:
                frm = "0x" + t["topics"][1][-40:]
                if frm.lower() == authorizer.lower():
                    data = t.get("data", "0x")
                    val = int(data, 16) / 1e6 if data not in (None, "0x", "") else 0.0
                    seller = "0x" + t["topics"][2][-40:]
                    break
            if val is None:
                val, seller = 0.0, None  # authorization used without observable transfer (rare)
            settlements.append((authorizer, seller, val))

    blk = end
    CH = "Base"
    for authorizer, seller, val in settlements:
        conn.execute(
            "INSERT INTO daily(day,chain,txs,volume,max_block) VALUES(?,?,?,?,?) "
            "ON CONFLICT(day,chain) DO UPDATE SET txs=txs+1, volume=volume+?, "
            "max_block=MAX(max_block,?)",
            (day, CH, 1, val, blk, val, blk),
        )
        if seller:
            conn.execute(
                "INSERT INTO sellers(addr,chain,txs,volume,first_day,last_day) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(addr,chain) DO UPDATE SET txs=txs+1, volume=volume+?, last_day=?",
                (seller.lower(), CH, 1, val, day, day, val, day),
            )
            conn.execute(
                "INSERT INTO daily_sellers(day,chain,addr,txs,volume) VALUES(?,?,?,?,?) "
                "ON CONFLICT(day,chain,addr) DO UPDATE SET txs=txs+1, volume=volume+?",
                (day, CH, seller.lower(), 1, val, val),
            )
            conn.execute(
                "INSERT INTO edges(buyer,seller,chain,txs,volume,first_day,last_day) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(buyer,seller,chain) DO UPDATE SET txs=txs+1, volume=volume+?, last_day=?",
                (authorizer.lower(), seller.lower(), CH, 1, val, day, day, val, day),
            )
        conn.execute(
            "INSERT INTO buyers(addr,chain,txs,volume,first_day,last_day) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(addr,chain) DO UPDATE SET txs=txs+1, volume=volume+?, last_day=?",
            (authorizer.lower(), CH, 1, val, day, day, val, day),
        )
        conn.execute(
            "INSERT INTO buckets(bucket,chain,txs,volume) VALUES(?,?,?,?) "
            "ON CONFLICT(bucket,chain) DO UPDATE SET txs=txs+1, volume=volume+?",
            (bucket_of(val), CH, 1, val, val),
        )
    conn.commit()
    return len(settlements), blk


def _tempo_block():
    """MPP/Tempo estimate from hourly probe samples (v0.12.0).
    Sampled estimate, not an enumerated count — never merge into counted totals."""
    try:
        with open("/var/lib/paycensus/mpp_state.json") as f:
            h = [x for x in json.load(f).get("history", []) if x.get("api_ok")]
        if not h:
            return None
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day = [x for x in h if x.get("ts", "").startswith(today)]
        use = day or h[-6:]
        avg_tpm = sum(x.get("tx_per_min", 0) for x in use) / len(use)
        latest = h[-1]
        return {
            "chain": "Tempo Mainnet",
            "chain_id": 4217,
            "protocol": "MPP",
            "latest_block": latest.get("block"),
            "last_sample_ts": latest.get("ts"),
            "samples_used": len(use),
            "est_tx_per_min": round(avg_tpm, 1),
            "est_daily_txs": int(avg_tpm * 1440),
            "method": "hourly 50-block samples via the public keyless Tempo API "
                      "(api.tempo.xyz/v1); estimate — raw chain activity, not all txs "
                      "are MPP payments; Stripe fiat rails are not observable",
        }
    except Exception:  # noqa: BLE001
        return None


def write_stats(conn):
    def one(q, *a):
        return conn.execute(q, a).fetchone()

    total_txs, total_vol = one("SELECT COALESCE(SUM(txs),0), COALESCE(SUM(volume),0) FROM daily")
    n_buyers = one("SELECT COUNT(*) FROM buyers")[0]
    n_sellers = one("SELECT COUNT(*) FROM sellers")[0]

    # Caliber D: seller-mix breakdown. Address counts stay address-level
    # (anchored history); infra = contract payTo with no attributed storefront.
    (n_eoa, n_contract, n_unclass, n_infra, infra_vol) = one(
        "SELECT SUM(CASE WHEN s.kind='eoa' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN s.kind='contract' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN s.kind IS NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN s.kind='contract' AND e.addr IS NULL THEN 1 ELSE 0 END), "
        "COALESCE(SUM(CASE WHEN s.kind='contract' AND e.addr IS NULL "
        "THEN s.volume ELSE 0 END),0) "
        "FROM sellers s LEFT JOIN entities e ON e.addr = s.addr")
    n_eoa, n_contract, n_unclass, n_infra = (int(x or 0) for x in
                                             (n_eoa, n_contract, n_unclass, n_infra))
    seller_mix = {
        "total_addr": n_sellers,
        "eoa_classified": n_eoa,
        "contract_classified": n_contract,
        "unclassified": n_unclass,
        "infra_contracts": n_infra,
        "infra_volume_share": round(infra_vol / total_vol, 4) if total_vol else None,
        "note": "Address-level counts are unchanged (anchored history). "
                "infra_contracts = payTo addresses that are on-chain contracts "
                "with no attributed storefront (settlement/pool infrastructure) — "
                "they settle volume but are not storefront sellers. "
                "Contract payTo WITH an attributed storefront counts as a "
                "contract-based seller. Storefront-seller caliber = attributed "
                "entities (see directory.json).",
    }

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
        {"addr": a[:6] + "…" + a[-4:], "addr_full": a, "chain": ch, "txs": t,
         "volume": round(v, 2), "kind": k,
         "infra": bool(k == "contract" and dom is None)}
        for a, ch, t, v, k, dom in conn.execute(
            "SELECT s.addr, s.chain, s.txs, s.volume, s.kind, e.addr FROM sellers s "
            "LEFT JOIN entities e ON e.addr = s.addr ORDER BY s.volume DESC LIMIT 20"
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
        "totals": {
            "txs": total_txs,
            "volume_usdc": round(total_vol, 2),
            "buyers": n_buyers,
            "sellers": n_sellers,
        },
        "daily": daily,
        "top_sellers": top_sellers,
        "seller_mix": seller_mix,
        "buckets": buckets,
        "tempo_mpp": _tempo_block(),
        "note": "Machine-payment census (x402/EIP-3009 and MPP), USDC-denominated, indexed per "
                "chain — Bitcoin-anchored daily (OpenTimestamps). Protocol-agnostic entity layer: protocol source is recorded at the "
                "ingest layer only. tempo_mpp section is a sampled estimate (see its method "
                "field) — never merged into counted totals. Aggregate-first: raw events are "
                "not stored.",
    }
    os.makedirs(os.path.dirname(STATS_OUT), exist_ok=True)
    tmp = STATS_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATS_OUT)
    return stats


PROCESSING_DOC = [
    {"stage": "ingest", "chain": "Base",
     "method": "eth_getLogs on USDC 0x8335…2913 filtered by topic0 AuthorizationUsed(address,bytes32) + Transfer(address,address,uint256); 50-block chunks via free public RPC (pokt nodies); cron every minute, 6-block burn-in for reorg safety"},
    {"stage": "ingest", "chain": "Tempo (MPP)",
     "method": "hourly 50-block samples from the public keyless Tempo API api.tempo.xyz/v1 (tempo_probe.py): tx-rate estimates published in stats.json tempo_mpp and mpp.json — estimates are never merged into counted tables"},
    {"stage": "offer-graph", "chain": "storefronts",
     "method": "402-challenge probing of the origin catalog (enrich_v1): protocol signature tags (x402/mpp/dual) + unified quote metadata (amount/currency/network/payTo) parsed from both protocol families' challenge payloads; published in mpp.json offers"},
    {"stage": "pair",
     "method": "one settlement = one AuthorizationUsed(authorizer,nonce) paired with Transfer(from=authorizer) in the same tx -> buyer=authorizer, seller=payTo recipient, value in USDC (6 decimals)"},
    {"stage": "aggregate",
     "method": "aggregate-first: raw events are NOT stored. Written tables: daily(day,chain) / sellers / buyers / edges(buyer x seller, the unbackfillable graph) / buckets(amount ranges)"},
    {"stage": "publish",
     "method": "stats.json + audit.json (this file) written via atomic replace after every run; each run is recorded in the runs table with cursor, counts and status"},
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
    """Daily machine-payment calendar: per-day txs/volume per chain, new entities,
    per-chain top seller. Backtest-friendly: every day is a self-contained record."""
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
        "note": "Machine-payment calendar (x402 chains, counted settlements) — Bitcoin-anchored daily. Day boundaries UTC. "
                "new_* = entities first seen that day. top_seller_by_chain = highest-volume payTo "
                "that day. MPP/Tempo volume is tracked separately as sampled estimates "
                "(stats.json tempo_mpp, mpp.json) — never merged into counted days. "
                "History starts 2026-10-31 (never backfilled).",
        "days": [days[k] for k in sorted(days)],
    }
    os.makedirs(os.path.dirname(CAL_OUT), exist_ok=True)
    tmp = CAL_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, CAL_OUT)


def main():
    # lockfile (stale > 15 min is ignored)
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
        conn = db()
        cur = None
        row = conn.execute("SELECT v FROM meta WHERE k='last_block'").fetchone()
        if row:
            cur = int(row[0])
        else:
            sb = os.environ.get(START_BLOCK_ENV)
            if sb:
                cur = int(sb) - 1
            else:
                cur = head() - 10  # first run: start from now, history begins today (per plan)
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_block',?)", (str(cur),))
            conn.commit()

        h = head()
        target = h - BURN_IN
        n_settle, chunks, err_detail = 0, 0, None
        while cur < target and chunks < MAX_CHUNKS_PER_RUN:
            start = cur + 1
            end = min(start + CHUNK - 1, target)
            try:
                n, blk = scan_chunk(conn, start, end)
            except Exception as e:  # noqa: BLE001
                err_detail = f"chunk {start}-{end}: {str(e)[:180]}"
                print(f"chunk {start}-{end} failed: {e}", flush=True)
                break
            cur = blk
            n_settle += n
            chunks += 1
            conn.execute("UPDATE meta SET v=? WHERE k='last_block'", (str(cur),))
            conn.commit()
            time.sleep(0.5)

        stats = write_stats(conn)
        dur = int((time.time() - t0) * 1000)
        status = "ok" if err_detail is None else "partial"
        conn.execute(
            "INSERT INTO runs(ts,tool,chain,status,cursor,scanned,new_items,totals_txs,totals_volume,duration_ms,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "base-indexer", "Base", status,
             str(cur), f"{chunks} chunks x {CHUNK} blocks", n_settle,
             stats["totals"]["txs"], stats["totals"]["volume_usdc"], dur, err_detail),
        )
        conn.commit()
        write_audit(conn)
        build_calendar(conn)
        print(f"scanned {chunks} chunks, +{n_settle} settlements, cursor={cur}, "
              f"total_txs={stats['totals']['txs']}, vol={stats['totals']['volume_usdc']:.2f}")
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
