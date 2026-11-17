#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily cold-mail firer: burns the gateway's full daily quota across tiers.

v2 (2026-11-16): rebuilds the queues via ammo_builder.py before firing, so
newly enriched contacts flow in automatically. Runs TWICE daily (10:05 +
22:05 UTC cron) — whichever run sees leftover quota burns it; as the gateway
raises our daily quota, volume scales with zero code change (budget is
always min(sendable_now(), queue)).

Priority: T3 (institutions) -> T1 (developers) -> T2 (sellers). Stops the
moment the pool reports sendable_now == 0 (never slams /send into 429).
"""
import subprocess
import sys

sys.path.insert(0, "/opt/paycensus")
from coldmail_pool import sendable_now  # noqa: E402

TIERS = ["t3", "t1", "t2"]


def rebuild_ammo():
    r = subprocess.run("cd /opt/paycensus && python3 ammo_builder.py",
                       shell=True, capture_output=True, text=True)
    print((r.stdout or r.stderr).strip()[-600:])


def main():
    print(f"=== fire_daily {sys.argv[1:]} ===")
    rebuild_ammo()
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 999
    budget = min(sendable_now(), cap)
    print(f"fire_daily: sendable_now={budget}")
    for tier in TIERS:
        if budget <= 0:
            print("  quota exhausted before", tier)
            break
        r = subprocess.run(
            f"cd /opt/paycensus && python3 outreach_sender.py --tier {tier} "
            f"--send --via gateway --limit {budget}",
            shell=True, capture_output=True, text=True)
        print(f"--- {tier} ---")
        print((r.stdout or r.stderr).strip()[-900:])
        # recount from the response of the last run: cheapest is re-check
        budget = min(sendable_now(), budget)


if __name__ == "__main__":
    main()
