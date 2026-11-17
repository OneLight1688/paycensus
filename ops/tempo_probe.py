#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus MPP/Tempo Probe (v0.10.0) — keyless observation of the MPP settlement chain.
Source: Tempo public API https://api.tempo.xyz/v1 (most read endpoints need no key).
Measures: chain reachability, latest block, block time, tx rate (from a 50-block
sample), tx type mix, transfer share; keeps an hourly history and publishes
/data/mpp.json. Seller-side protocol census (x402/mpp/dual sig tags) is merged in
from the enrich state for one-stop MPP visibility.
Scope note: raw Tempo chain activity — not all txs are MPP payments; and MPP
settlement on Stripe fiat rails is not observable on any chain.
Run: python3 tempo_probe.py   (cron hourly at :20; 2 API calls per run)
"""
import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

API = "https://api.tempo.xyz/v1"
UA = {"User-Agent": "Mozilla/5.0 (PayCensus indexer; +https://paycensus.com)", "Accept": "application/json"}
STATE = "/var/lib/paycensus/mpp_state.json"
OUT = "/www/wwwroot/paycensus/data/mpp.json"
ENRICH_STATE = "/var/lib/paycensus/enrich_state.json"
HISTORY_CAP = 720  # 30 days of hourly samples
TOKEN_MAP_TTL = 7 * 24 * 3600  # refresh the address->symbol registry weekly


def log(s):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {s}", flush=True)


def get(path, timeout=20):
    req = urllib.request.Request(API + path, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def iso(ts):
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def token_map(state):
    """address -> symbol registry from /v1/tokens pagination (v0.15.0).
    Incremental crawl: max 5 pages/run (anonymous ratelimit is 20/window —
    5 + 2 regular calls stays under it), cursor persisted in state until the
    registry is exhausted, then cached for a week."""
    tm = dict(state.get("token_map") or {})
    if tm and time.time() - state.get("token_map_ts", 0) < TOKEN_MAP_TTL:
        return tm
    cursor = state.get("token_map_cursor") or ""
    pages = 0
    try:
        while pages < 5:
            d = get("/tokens?limit=50" + (f"&cursor={cursor}" if cursor else ""))
            for t in d.get("data", []):
                if t.get("address") and t.get("symbol"):
                    tm[t["address"].lower()] = t["symbol"]
            cursor = d.get("nextCursor") or ""
            pages += 1
            if not cursor:  # registry exhausted this pass
                state["token_map"] = tm
                state["token_map_cursor"] = ""
                state["token_map_ts"] = time.time()
                json.dump(state, open(STATE, "w"))
                log(f"token_map complete: {len(tm)} symbols")
                return tm
        state["token_map"] = tm
        state["token_map_cursor"] = cursor
        json.dump(state, open(STATE, "w"))
        log(f"token_map +{pages * 50} (resuming cursor, {len(tm)} total)")
    except Exception as e:  # noqa: BLE001
        log(f"token_map refresh failed: {e}")
        state["token_map"] = tm
        state["token_map_cursor"] = cursor
        json.dump(state, open(STATE, "w"))
    return tm


def sample_chain(tmap):
    """One sample: 50-block window + 50-tx type/token mix. Returns dict or None."""
    try:
        b = get("/blocks?limit=50")
        blocks = b["data"]
    except Exception as e:  # noqa: BLE001
        log(f"blocks fetch failed: {e}")
        return None
    if not blocks:
        return None
    latest = max(x["number"] for x in blocks)
    txs = sum(int(x.get("transactionCount") or 0) for x in blocks)
    try:
        t_new = datetime.fromisoformat(blocks[0]["timestamp"].replace("Z", "+00:00"))
        t_old = datetime.fromisoformat(blocks[-1]["timestamp"].replace("Z", "+00:00"))
        span_s = (t_new - t_old).total_seconds()
        block_time = span_s / (len(blocks) - 1) if len(blocks) > 1 else 1.0
        window_s = span_s or len(blocks)
        tx_per_min = txs / window_s * 60
    except Exception:  # noqa: BLE001
        block_time, tx_per_min = 1.0, txs * 1.2
    types, transfer_n, tx_sample_n = {}, 0, 0
    transfers_by_token, fee_by_token = {}, {}
    try:
        t = get("/transactions?limit=50")
        rows = t["data"]
        tx_sample_n = len(rows)
        for x in rows:
            types[x.get("type", "?")] = types.get(x.get("type", "?"), 0) + 1
            ft = ((x.get("feeToken") or {}).get("symbol")) \
                or tmap.get(((x.get("meta") or {}).get("rpc") or {}).get("feeToken") or "", "") \
                or "unlabeled"
            fee_by_token[ft or "unlabeled"] = fee_by_token.get(ft or "unlabeled", 0) + 1
            if (x.get("input") or "").startswith("0xa9059cbb"):
                transfer_n += 1
                # TIP-20 transfer: the token contract is the call target
                addr = (x.get("recipient") or "").lower()
                tok = tmap.get(addr) or addr or "0x?"
                transfers_by_token[tok] = transfers_by_token.get(tok, 0) + 1
    except Exception as e:  # noqa: BLE001
        log(f"transactions fetch failed: {e}")
    return {
        "ts": iso(None),
        "api_ok": True,
        "block": latest,
        "block_time_s": round(block_time, 3),
        "sampled_blocks": len(blocks),
        "txs_in_sample": txs,
        "tx_per_min": round(tx_per_min, 2),
        "est_daily_txs": int(tx_per_min * 1440),
        "tx_sample": tx_sample_n,
        "tx_types": types,
        "transfers_in_sample": transfer_n,
        "transfers_by_token": dict(sorted(transfers_by_token.items(),
                                          key=lambda kv: -kv[1])[:8]),
        "fee_tokens": dict(sorted(fee_by_token.items(), key=lambda kv: -kv[1])[:6]),
    }


def offer_list():
    """Offer graph from the enrich state: per-origin quote metadata (v0.11.0).
    Cap 60 rows; payTo/price are public storefront data (402 challenges)."""
    rows = []
    try:
        s = json.load(open(ENRICH_STATE))
        for v in s.get("probed", {}).values():
            off = v.get("offer")
            if not off or v.get("status") != "402":
                continue
            rows.append({
                "origin": v.get("origin"), "sig": v.get("sig"),
                "offer_at": v.get("offer_at"),
                "mpp": off.get("mpp"), "x402": off.get("x402"),
            })
    except Exception as e:  # noqa: BLE001
        log(f"offer_list failed: {e}")
    rows.sort(key=lambda r: r.get("offer_at") or "")
    return {"count": len(rows), "list": rows[:60]}


def scheme_radar():
    """Third-protocol radar (v0.14.0): histogram of every WWW-Authenticate scheme
    seen on 402 responses. x402/payment are the known families; anything else
    announces a new protocol family the day it appears."""
    hist = {}
    try:
        s = json.load(open(ENRICH_STATE))
        for v in s.get("probed", {}).values():
            for sc in v.get("schemes") or []:
                hist[sc] = hist.get(sc, 0) + 1
    except Exception as e:  # noqa: BLE001
        log(f"scheme_radar failed: {e}")
    known = {"x402": hist.pop("x402", 0), "payment": hist.pop("payment", 0)}
    return {"known": known, "unknown": dict(sorted(hist.items(), key=lambda kv: -kv[1]))}


def seller_census():
    """Protocol sig tags from the enrich state (seller-side, origin probing)."""
    out = {"probed_402": 0, "x402": 0, "mpp": 0, "dual": 0, "untagged": 0}
    try:
        s = json.load(open(ENRICH_STATE))
        for v in s.get("probed", {}).values():
            if v.get("status") != "402":
                continue
            out["probed_402"] += 1
            sig = v.get("sig")
            if sig in out:
                out[sig] += 1
            else:
                out["untagged"] += 1
    except Exception as e:  # noqa: BLE001
        log(f"enrich_state read failed: {e}")
    return out


def main():
    state = {"history": []}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:  # noqa: BLE001
            pass
    tmap = token_map(state)
    s = sample_chain(tmap)
    if s is None:
        s = {"ts": iso(None), "api_ok": False}
    state["history"] = ([x for x in state.get("history", []) if x.get("ts") != s["ts"]] + [s])[-HISTORY_CAP:]
    json.dump(state, open(STATE, "w"))

    # OUSD watch: any transfer sample touching an OUSD-family token (v0.15.0)
    tbt = s.get("transfers_by_token") or {}
    ousd = {k: v for k, v in tbt.items() if "OUSD" in k.upper()}
    h_ok = [x for x in state["history"] if x.get("api_ok")]
    day = [x for x in h_ok if x["ts"].startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d"))]
    day_avg = round(sum(x["tx_per_min"] for x in day) / len(day), 2) if day else None
    payload = {
        "generated_at": iso(None),
        "note": "MPP settlement-chain monitor (Tempo Mainnet). Data from the public keyless "
                "Tempo API api.tempo.xyz/v1; hourly samples. Raw chain activity — not all txs "
                "are MPP payments, and MPP settlement on Stripe fiat rails is not observable. "
                "Per-token split: TIP-20 transfers by token contract (registry from /v1/tokens, "
                "weekly refresh) and gas by feeToken — OUSD (Open Standard consortium, native "
                "on Tempo) is watched explicitly. Sellers section = origin-probe protocol "
                "census from the x402 catalog (see leads.json protocols). Offers section = the "
                "offer graph parsed from sellers' 402 challenges. Monitoring started 2026-11-06 "
                "(v0.10.0), offers v0.11.0, scheme radar v0.14.0, token split v0.15.0. The daily census files behind this monitor are Bitcoin-anchored (stats/calendar/audit).",
        "chain": {"name": "Tempo Mainnet", "chain_id": 4217,
                  "api": "https://api.tempo.xyz/v1 (public read, no key)"},
        "latest": s,
        "today_avg_tx_per_min": day_avg,
        "ousd_activity": {"transfers_in_sample": sum(ousd.values()), "tokens": ousd,
                          "note": "OUSD = Open Standard consortium stablecoin (Visa/Mastercard/"
                                  "Stripe/Coinbase/BlackRock et al.), native on Tempo. Raw "
                                  "transfers only — not all are machine payments."},
        "samples_24h": len(h_ok[-24:]),
        "sellers": seller_census(),
        "schemes_seen": scheme_radar(),
        "offers": offer_list(),
        "history": state["history"][-168:],
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    log(f"DONE block={s.get('block')} tx/min={s.get('tx_per_min')} est_daily={s.get('est_daily_txs')} "
        f"history={len(state['history'])}")


if __name__ == "__main__":
    main()
