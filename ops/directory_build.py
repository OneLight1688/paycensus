#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
directory_build.py (v0.17.0) — the registry: unified cross-protocol entity directory
+ leading indicators. Built from existing artifacts only ($0, no new collection):
  entities.json (attribution) + census.db (counted stats) + enrich_state (protocol
  sig tags, offer graph, scheme radar) + mpp.json (Tempo monitor) + stats.json.
Publishes:
  /data/directory.json   — entity directory (domain-keyed; addr-keyed fallback)
  /data/indicators.json  — leading indicators (all data, no prophecy)
Discipline: the daily census files (stats/calendar/audit) are Bitcoin-anchored;
this registry is built on top of them and re-anchored through the same chain.
"""
import json
import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

DATA = "/www/wwwroot/paycensus/data"
DB = "/var/lib/paycensus/census.db"
STATE = "/var/lib/paycensus/enrich_state.json"

ANCHORED_NOTE = ("Daily census files (stats/calendar/audit) are Bitcoin-anchored via "
                 "OpenTimestamps; this registry is derived from them.")

CAT_RULES = [
    ("ai-llm", ["llm", "gpt", "inference", "model", "openai", "claude", "prompt",
                "agent", "ai ", "ai-", "machine learning", "embedding", "chatbot"]),
    ("market-data", ["price", "market", "quote", "token", "trading", "crypto", "dex",
                     "exchange", "portfolio", "defi", "coin"]),
    ("dev-infra", ["api", "sdk", "dev", "code", "github", "status", "monitor", "uptime",
                   "hosting", "database", "storage", "ipfs", "cdn"]),
    ("search-news", ["search", "news", "digest", "feed", "index", "directory"]),
    ("web-geo", ["weather", "geo", "map", "location", "ip ", "dns", "domain", "whois"]),
    ("payments", ["payment", "pay", "wallet", "facilitator", "x402", "mpp", "settle",
                  "bridge", "audit", "risk", "compliance"]),
]


def log(s):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {s}", flush=True)


def load(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def categorize(text):
    t = (text or "").lower()
    for cat, kws in CAT_RULES:
        if any(k in t for k in kws):
            return cat
    return "other"


def offers_from_state(state):
    """addr(lower) -> list of offer dicts (normalized)."""
    out = {}
    for v in state.get("probed", {}).values():
        off = v.get("offer")
        if not off:
            continue
        addrs = set()
        x = off.get("x402") or {}
        if x.get("payto"):
            addrs.add(x["payto"].lower())
        for pt in v.get("paytos") or []:
            addrs.add(pt.lower())
        for a in addrs:
            out.setdefault(a, []).append({
                "origin": v.get("origin"), "sig": v.get("sig"),
                "offer_at": v.get("offer_at"), "mpp": off.get("mpp"), "x402": x,
            })
    return out


def offer_usd(offer):
    """Best-effort USD price of one offer (human units) or None."""
    m = offer.get("mpp") or {}
    cur = (m.get("currency") or "").lower()
    if m.get("amount") and cur in ("", "usd"):
        try:
            return float(m["amount"])
        except ValueError:  # noqa: BLE001
            pass
    x = offer.get("x402") or {}
    amt, asset = x.get("amount"), (x.get("asset") or "").upper()
    if amt and ("USD" in asset or "USDC" in asset or x.get("network", "").startswith("eip155")):
        try:
            v = float(amt)
            return v / 1e6 if v > 10000 else v  # raw 6-decimals vs human units
        except ValueError:  # noqa: BLE001
            pass
    return None


def main():
    ent_src = load(os.path.join(DATA, "entities.json"), {}).get("entities", [])
    state = load(STATE, {})
    mpp = load(os.path.join(DATA, "mpp.json"), {})
    stats = load(os.path.join(DATA, "stats.json"), {})
    addr_offers = offers_from_state(state)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    seller_rows = conn.execute(
        "SELECT addr, chain, txs, volume, first_day, last_day FROM sellers").fetchall()
    addr_stats = {}
    for r in seller_rows:
        addr_stats.setdefault(r["addr"].lower(), []).append(dict(r))
    tot = stats.get("totals", {})
    total_vol = tot.get("volume_usdc", 0) or 0

    # ---- directory: domain-keyed entities (attribution), addr-keyed fallback ----
    by_key, seen_addr = {}, set()
    for e in ent_src:
        addr = (e.get("addr") or "").lower()
        seen_addr.add(addr)
        key = e.get("domain") or addr
        rows = addr_stats.get(addr, [])
        offers = addr_offers.get(addr, [])
        text = f"{e.get('domain','')} {e.get('title','')} {e.get('description','')}"
        rec = {
            "id": key, "type": "service",
            "title": e.get("title") or key,
            "description": (e.get("description") or "")[:300],
            "domain": e.get("domain"),
            "category": categorize(text),
            "addresses": [{"addr": addr, "chains": [
                {k: r[k] for k in ("chain", "txs", "volume", "first_day", "last_day")}
                for r in rows]}],
            "protocols": {"x402": True,
                          "mpp": any(o.get("mpp") for o in offers)},
            "offers": offers[:4],
            "totals": {"txs": sum(r["txs"] for r in rows),
                       "volume_usdc": round(sum(r["volume"] for r in rows), 2)},
            "first_day": min((r["first_day"] for r in rows), default=None),
            "last_day": max((r["last_day"] for r in rows), default=None),
        }
        prev = by_key.get(key)
        if prev:  # merge second address under same domain
            prev["addresses"].extend(rec["addresses"])
            prev["totals"]["txs"] += rec["totals"]["txs"]
            prev["totals"]["volume_usdc"] = round(
                prev["totals"]["volume_usdc"] + rec["totals"]["volume_usdc"], 2)
            prev["offers"] = (prev["offers"] + rec["offers"])[:4]
            prev["protocols"]["mpp"] = prev["protocols"]["mpp"] or rec["protocols"]["mpp"]
            if rec["last_day"]:
                prev["last_day"] = max(prev["last_day"] or "", rec["last_day"])
        else:
            by_key[key] = rec
    # address-only sellers (no domain) are NOT listed in the public directory:
    # with settlement pools in the mix they rendered as null rows and polluted
    # the seller registry. They live in the internal CRM instead; Phase A-D
    # contract classification will decide what surfaces publicly later.
    entities = sorted(by_key.values(),
                      key=lambda r: -(r["totals"]["volume_usdc"] or 0))
    cat_counts = {}
    for r in entities:
        cat_counts[r["category"]] = cat_counts.get(r["category"], 0) + 1
    directory = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "note": "The registry: unified cross-protocol entity directory (x402 attribution + "
                "MPP offers). Addresses are public on-chain data; attribution is the seller's "
                "own public storefront info; contact information is never published. "
                + ANCHORED_NOTE,
        "count": len(entities),
        "categories": dict(sorted(cat_counts.items(), key=lambda kv: -kv[1])),
        "entities": entities[:1500],
    }
    _atomic(os.path.join(DATA, "directory.json"), directory)
    log(f"directory: {len(entities)} entities, {len(cat_counts)} categories")

    # ---- indicators ----
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    daily = stats.get("daily", [])
    yd = [d for d in daily if d["day"] == yesterday]
    # stats.json daily carries only day/txs/volume — new sellers must come
    # from the counted sellers table (first seen on the previous UTC day)
    new_sellers_24h = conn.execute(
        "SELECT COUNT(*) FROM (SELECT addr, MIN(first_day) fd FROM sellers "
        "GROUP BY addr) WHERE fd = date('now','-1 day')").fetchone()[0] or None
    base_daily_txs = sum(d.get("txs", 0) for d in yd) or None

    sigs = [v.get("sig") for v in state.get("probed", {}).values()
            if v.get("status") == "402"]
    dual = sigs.count("dual")
    schemes = {}
    for v in state.get("probed", {}).values():
        for sc in v.get("schemes") or []:
            schemes[sc] = schemes.get(sc, 0) + 1
    unknown_schemes = {k: c for k, c in schemes.items()
                       if k not in ("x402", "payment")}

    prices = []
    for lst in addr_offers.values():
        for o in lst:
            p = offer_usd(o)
            if p is not None and 0 < p < 1000:  # sane machine-payment band
                prices.append(p)
    prices.sort()
    med = round(statistics.median(prices), 4) if prices else None

    hhi_top10 = conn.execute(
        "SELECT COALESCE(SUM(v),0) FROM (SELECT addr, SUM(volume) v FROM sellers "
        "GROUP BY addr ORDER BY v DESC LIMIT 10)").fetchone()[0]
    # Caliber D: infra contracts (contract payTo, no attributed storefront)
    # distort concentration/churn metrics — publish corrected variants alongside.
    n_infra, infra_vol = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(s.volume),0) FROM sellers s "
        "LEFT JOIN entities e ON e.addr = s.addr "
        "WHERE s.kind='contract' AND e.addr IS NULL").fetchone()
    hhi_top10_excl = conn.execute(
        "SELECT COALESCE(SUM(v),0) FROM (SELECT s.addr, SUM(s.volume) v FROM sellers s "
        "LEFT JOIN entities e ON e.addr = s.addr "
        "WHERE NOT (s.kind='contract' AND e.addr IS NULL) "
        "GROUP BY s.addr ORDER BY v DESC LIMIT 10)").fetchone()[0]
    vol_excl = total_vol - infra_vol if total_vol else 0
    churn = conn.execute(
        "SELECT COUNT(*) FROM (SELECT addr, MAX(last_day) ld FROM sellers GROUP BY addr) "
        "WHERE ld <= date('now','-7 day')").fetchone()[0]
    n_sellers = tot.get("sellers", 0) or 0
    tempo_est = (mpp.get("today_avg_tx_per_min") or 0) * 1440
    ousd = mpp.get("ousd_activity", {}).get("transfers_in_sample", 0)

    def ind(key, label, value, unit, stage, note):
        return {"key": key, "label": label, "value": value, "unit": unit,
                "stage": stage, "note": note}

    indicators = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "note": "Leading indicators — every number is already-collected data, never a "
                "forecast. Counted (Base x402) and sampled (Tempo MPP) are never merged. "
                + ANCHORED_NOTE,
        "indicators": [
            ind("new_sellers_24h", "New sellers (24h, x402 counted)", new_sellers_24h,
                "sellers", 1, f"first seen on {yesterday} UTC, Base counted settlements"),
            ind("dual_sellers", "Dual-protocol sellers", dual, "origins", 1,
                "storefronts accepting both x402 and MPP (scheme radar) — cross-protocol "
                "migration clock"),
            ind("unknown_schemes", "Unknown protocol schemes",
                ", ".join(f"{k} ×{c}" for k, c in sorted(unknown_schemes.items()))
                or "none yet", "", 1,
                "third-protocol radar: any scheme outside {x402, payment}"),
            ind("offer_price_median", "Median listed price (offers)", med, "USD", 2,
                "machine-economy CPI seed: median quote across the offer graph, "
                "USD-quoted, sane band 0–1000"),
            ind("offers_count", "Offers with quote (offer graph)", len(prices), "offers",
                2, "402-challenge quotes parsed from public storefronts"),
            ind("hhi_top10", "Top-10 seller volume share",
                round(hhi_top10 / total_vol, 4) if total_vol else None, "share", 1,
                "is the machine economy one whale or a crowd (top-10 sellers, counted, "
                "address caliber — includes infra contracts)"),
            ind("infra_contracts", "Infra contracts (unattributed contract payTo)",
                n_infra, "addresses", 1,
                f"contract payTo with no storefront: {n_infra} addresses settle "
                f"${infra_vol:,.0f} = "
                f"{round(infra_vol / total_vol * 100, 1) if total_vol else 0}% of volume — "
                "settlement/pool infrastructure, not storefront sellers"),
            ind("hhi_top10_excl_infra", "Top-10 seller share (excl. infra contracts)",
                round(hhi_top10_excl / vol_excl, 4) if vol_excl else None, "share", 1,
                "same concentration question on the storefront caliber — infra "
                "contracts excluded from both numerator and denominator"),
            ind("churn_7d", "Sellers inactive 7d+", round(churn / n_sellers, 4)
                if n_sellers else None, "share", 1,
                f"{churn} of {n_sellers} counted sellers last active before 7d ago"),
            ind("buyer_seller_ratio", "Buyers per seller",
                round(tot.get("buyers", 0) / n_sellers, 2) if n_sellers else None,
                "ratio", 1, "supply/demand balance (counted)"),
            ind("tempo_base_ratio", "Tempo/Base activity ratio",
                round(tempo_est / base_daily_txs, 4)
                if tempo_est and base_daily_txs else None, "ratio", 1,
                "sampled Tempo est vs counted Base (yesterday) — settlement-layer migration; "
                "estimates never merged into counts"),
            ind("ousd_activity", "OUSD transfers (latest Tempo sample)", ousd,
                "transfers", 2, "Open Standard consortium stablecoin, native on Tempo — "
                "raw transfers, not all machine payments"),
        ],
    }
    _atomic(os.path.join(DATA, "indicators.json"), indicators)
    _tbr = next((i["value"] for i in indicators["indicators"]
                 if i["key"] == "tempo_base_ratio"), None)
    log(f"indicators: {len(indicators['indicators'])} published "
        f"(median offer ${med}, dual {dual}, tempo/base {_tbr})")
    conn.close()


def _atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


if __name__ == "__main__":
    main()
