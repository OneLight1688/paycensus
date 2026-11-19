#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trust hygiene: abandon stale pending verifications.

A pending nonce older than 48h can never complete (token validity is 48h).
If the seller retries, they get a NEW vid — the old row just clutters the
admin anomaly list forever (cf. relayshield vid5/vid6, 2026-11-16).
Run daily after trust_api.py reverify: pending + expired >48h -> 'abandoned'.
"""
import sqlite3

DB = "/var/lib/paycensus/trust.db"


def main():
    con = sqlite3.connect(DB)
    n = con.execute(
        "UPDATE verifications SET status='abandoned' "
        "WHERE status='pending' AND expires_at < datetime('now', '-48 hours')"
    ).rowcount
    con.commit()
    con.close()
    print(f"trust_cleanup: {n} stale pending -> abandoned")


if __name__ == "__main__":
    main()
