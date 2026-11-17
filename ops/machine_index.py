#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus daily indices — deterministic, definition-first, $0 stdlib.

MPI — Machine Payment Index
  Machine-payment settlement volume, base-100 chained to the census start.
  MPI(d) = tracked_volume(d) / tracked_volume(base_day) * 100
  base_day = first complete UTC day of the census (2026-10-31).
  Published value = last COMPLETE UTC day (T-1); today's row is still
  accumulating and is excluded from the headline number.
  Tracked volume = census-tracked chains only (coverage honest, not an
  estimate of total market volume).

TVI — Trust Verification Index
  verified_entities / attributed_entities * 100 (2 decimals).
  Coverage of the registry by deterministic domain-control verification;
  revocations are subtracted from the numerator (a revoked record no longer
  counts). Raw counts published alongside — the ratio is never shown alone.

Output: /www/wwwroot/paycensus/data/indices.json (public, BTC-anchored via
anchor.py FILES list). Cron: hourly :55.
"""
import json
import os
import sqlite3
import datetime

CENSUS_DB = "/var/lib/paycensus/census.db"
TRUST_DB = "/var/lib/paycensus/trust.db"
OUT = "/www/wwwroot/paycensus/data/indices.json"
BASE_DAY = "2026-10-31"


def main():
    utcnow = datetime.datetime.now(datetime.timezone.utc)
    today = utcnow.strftime("%Y-%m-%d")

    c = sqlite3.connect(f"file:{CENSUS_DB}?mode=ro", uri=True)
    rows = c.execute("SELECT day, SUM(volume) v FROM daily WHERE day < ? "
                     "GROUP BY day ORDER BY day", (today,)).fetchall()
    attributed = c.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    c.close()

    series = [{"day": d, "volume_usd": round(v or 0, 2)} for d, v in rows]
    base_v = next((s["volume_usd"] for s in series if s["day"] == BASE_DAY), None)
    if not base_v:
        base_v = series[0]["volume_usd"] if series else 1.0
    for s in series:
        s["mpi"] = round(s["volume_usd"] / base_v * 100.0, 2)
    last = series[-1] if series else None
    prev = series[-2] if len(series) > 1 else None

    t = sqlite3.connect(f"file:{TRUST_DB}?mode=ro", uri=True)
    verified = t.execute(
        "SELECT COUNT(*) FROM verifications WHERE status='verified'").fetchone()[0]
    revoked = t.execute(
        "SELECT COUNT(*) FROM verifications WHERE status='revoked'").fetchone()[0]
    pending = t.execute(
        "SELECT COUNT(*) FROM verifications WHERE status='pending'").fetchone()[0]
    t.close()

    tvi_pct = round(verified / attributed * 100.0, 2) if attributed else 0.0

    out = {
        "generated_at": utcnow.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "mpi": {
            "value": last["mpi"] if last else None,
            "last_complete_day": last["day"] if last else None,
            "base_date": BASE_DAY,
            "base_volume_usd": base_v,
            "day_over_day_pct": (round((last["mpi"] - prev["mpi"])
                                       / prev["mpi"] * 100.0, 2)
                                 if last and prev and prev["mpi"] else None),
            "series_tail": series[-14:],
            "definition": "MPI = tracked machine-payment settlement volume "
                          "(census-tracked chains) / volume on the base day "
                          f"({BASE_DAY}) * 100. Headline uses the last COMPLETE "
                          "UTC day; tracked coverage only — not a market-size "
                          "estimate.",
        },
        "tvi": {
            "value_pct": tvi_pct,
            "verified": verified,
            "revoked": revoked,
            "pending": pending,
            "attributed_entities": attributed,
            "definition": "TVI = verified entities (minus revoked) / attributed "
                          "registry entities * 100. Deterministic domain-control "
                          "verification coverage of the census registry.",
        },
        "note": "Both indices are deterministic ratios of already-collected "
                "data — no weights, no forecasts. Published hourly, "
                "Bitcoin-anchored daily via PayCensus snapshot chain.",
    }
    tmp = OUT + ".tmp"
    json.dump(out, open(tmp, "w"), ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"MPI {out['mpi']['value']} (day {out['mpi']['last_complete_day']}) · "
          f"TVI {tvi_pct}% ({verified}/{attributed}, revoked {revoked})")


if __name__ == "__main__":
    main()
