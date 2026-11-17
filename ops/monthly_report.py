#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Monthly Report generator — T3 institution deliverable (Issue #1 skeleton).
Pulls ONLY public data (stats/calendar/entities/anchoring/audit) and renders a
markdown issue per month. Internal tables (contacts/addr_hints) never appear.
Data honesty rules: partial-month issues say so; methodology + boundaries included;
signed Harold Tucker / PayCensus Labs.
Run: python3 monthly_report.py [YYYY-MM]   (default: last complete month)
Cron: 05:30 UTC on day 1 — generates previous month issue.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE = "https://paycensus.com"
OUT_DIR = "/www/wwwroot/paycensus/data/reports"
UA = {"User-Agent": "PayCensus-reporter/0.1"}


def fetch(path):
    req = urllib.request.Request(BASE + path, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def fmt_usd(v):
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:,.2f}"


def build(month: str) -> str:
    stats = fetch("/data/stats.json")
    cal = fetch("/data/calendar.json")
    ent = fetch("/data/entities.json")
    anch = fetch("/data/anchoring.json")
    tot = stats.get("totals", {})
    chains = stats.get("chains", {})
    days = [d for d in cal.get("days", []) if d.get("day", "").startswith(month)]

    m_txs = sum(d.get("txs", 0) for d in days)
    m_vol = sum(d.get("volume", 0.0) for d in days)
    best = max(days, key=lambda d: d.get("volume", 0), default={})
    top_sellers = (ent.get("entities") or [])[:10]

    L = []
    L.append(f"# PayCensus Monthly — {month}")
    L.append("")
    L.append(f"*Issue skeleton | generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | "
             f"Harold Tucker, PayCensus Labs*")
    L.append("")
    L.append("> **Scope note.** PayCensus data history starts 2026-10-31 (never backfilled). "
             "If this month is partial, the numbers below cover only the observed days. "
             "All day boundaries are UTC. Methodology is public: https://paycensus.com/data/audit.json")
    L.append("")
    L.append("## 1. The month in numbers")
    L.append("")
    L.append(f"- Machine payments observed in {month}: **{m_txs:,} txs / {fmt_usd(m_vol)}** "
             f"across {len(days)} observed day(s)")
    L.append(f"- Cumulative since inception: {tot.get('txs', 0):,} txs / {fmt_usd(tot.get('volume_usdc', 0))} "
             f"| {tot.get('sellers', 0):,} sellers | {tot.get('buyers', 0):,} buyers")
    if best:
        L.append(f"- Largest observed day: **{best.get('day')}** — "
                 f"{best.get('txs', 0):,} txs / {fmt_usd(best.get('volume', 0))}")
    L.append("")
    L.append("## 2. Daily series")
    L.append("")
    L.append("| Day | Txs | Volume |")
    L.append("|---|---|---|")
    for d in days:
        L.append(f"| {d.get('day')} | {d.get('txs', 0):,} | {fmt_usd(d.get('volume', 0))} |")
    L.append("")
    L.append("## 3. Chain split (lifetime)")
    L.append("")
    L.append("| Chain | Txs | Volume | Sellers | Buyers |")
    L.append("|---|---|---|---|---|")
    for ch, v in chains.items():
        L.append(f"| {ch} | {v.get('txs', 0):,} | {fmt_usd(v.get('volume_usdc', 0))} | "
                 f"{v.get('sellers', 0):,} | {v.get('buyers', 0):,} |")
    L.append("")
    L.append("## 4. Entity layer — who is getting paid")
    L.append("")
    cov = f"{ent.get('count', 0)}"
    L.append(f"Attributed payTo entities (active sellers): **{cov}** "
             f"*(attribution = public storefront metadata matched to on-chain addresses; "
             f"contact information is never collected into the public layer)*")
    L.append("")
    if top_sellers:
        L.append("| Domain | Title | First seen |")
        L.append("|---|---|---|")
        for e in top_sellers:
            t = (e.get("title") or "").replace("|", "/")[:60]
            d = (e.get("domain") or "").replace("|", "/")[:60]
            L.append(f"| {d} | {t} | {e.get('first_seen', '')} |")
        L.append("")
    L.append("## 5. Proof of Integrity")
    L.append("")
    L.append(f"Chain head: `{anch.get('chain_head', '?')[:16]}…` — every day's snapshot is "
             f"hash-chained (digest = SHA256(prev_digest + snapshot)) and anchored to Bitcoin "
             f"via OpenTimestamps. Verify any day yourself:")
    L.append("")
    for a in anch.get("anchors", [])[:8]:
        L.append(f"- `{a['label']}` digest `{a['digest'][:16]}…` status **{a.get('status')}** — "
                 f"[receipt](https://paycensus.com/data/{a.get('ots')}) · "
                 f"[snapshot](https://paycensus.com/data/{a.get('snapshot')})")
    L.append("")
    L.append("## 6. Methodology & boundaries")
    L.append("")
    L.append("- Aggregate-first: raw events are not stored; published tables are "
             "daily/sellers/buyers/edges (the unbackfillable graph).")
    L.append("- Day boundary: UTC calendar date of the ingestion run (1-minute cron; "
             "boundary skew < 1 minute).")
    L.append("- Attribution: origin-side 402 probing + address-side public resource mapping; "
             "coverage is partial by design and every attribution source is labelled.")
    L.append("- Privacy: on-chain facts are public data; entity metadata is the seller's own "
             "storefront info; contact information is never published; 24h opt-out honored.")
    L.append("- Anchoring preserves integrity, not correctness — correctness comes from the "
             "public methodology anyone can independently re-run.")
    L.append("")
    L.append("---")
    L.append("")
    L.append("*PayCensus — The census of the machine economy. https://paycensus.com*")
    L.append("")
    return "\n".join(L)


def main():
    month = sys.argv[1] if len(sys.argv) > 1 else \
        (datetime.now(timezone.utc).replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
    md = build(month)
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{month}.md")
    tmp = out + ".tmp"
    open(tmp, "w", encoding="utf-8").write(md)
    os.replace(tmp, out)
    print(f"published {out} ({len(md)} bytes)")
    print(md[:600])


if __name__ == "__main__":
    main()
