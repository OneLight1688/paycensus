#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus system self-check (read-only diagnostics).
Usage:
    python3 healthcheck.py          # human-readable output
    python3 healthcheck.py --json   # JSON output (consumed by /api/health)
Exit code: 0 = all pass, 1 = any warn, 2 = any fail.
Checks: cron jobs, nginx, api process/port, data-file freshness, anchoring chain,
        indexer cursor, public endpoints, access-log country coverage.
"""
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

DATA = "/www/wwwroot/paycensus/data"
LOGS = "/var/lib/paycensus"
EXPECTED_CRON = ["indexer.py", "solana_indexer.py", "anchor.py stamp", "anchor.py upgrade",
                 "leads.py", "enrich_v1.py", "warmup.py", "monthly_report.py",
                 "category.py", "visitors.py", "tempo_probe.py"]
# (file, pass_max_minutes, warn_max_minutes)
FRESH = [("stats.json", 10, 60), ("audit.json", 10, 60), ("calendar.json", 10, 60),
         ("leads.json", 70, 150), ("visitors.json", 70, 150), ("categories.json", 70, 150),
         ("mpp.json", 70, 150),
         ("entities.json", 60 * 30, 60 * 54), ("anchoring.json", 60 * 30, 60 * 54)]


def run(cmd, t=30):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
    return (r.stdout or "").strip(), (r.stderr or "").strip()


def check(name):
    def deco(fn):
        fn._name = name
        return fn
    return deco


@check("cron jobs (11 expected entries)")
def c_cron():
    out, _ = run("crontab -l 2>&1")
    missing = [x for x in EXPECTED_CRON if x not in out]
    n = len(out.splitlines())
    if missing:
        return "fail", f"{n} entries; MISSING: {missing}"
    return "pass", f"{n} entries, all {len(EXPECTED_CRON)} expected jobs present"


@check("nginx service & config")
def c_nginx():
    svc, _ = run("systemctl is-active nginx")
    if svc != "active":
        return "fail", f"service: {svc}"
    t_out, t_err = run("sudo -n nginx -t 2>&1", t=20)
    ok = "successful" in (t_out + t_err)
    return ("pass", "active, config test successful") if ok else \
           ("warn", f"active; config test: {(t_out + t_err)[:120]}")


@check("api process & port 8787")
def c_api():
    ps, _ = run("ps -eo cmd | grep '[a]pi.py'")
    if "/opt/paycensus/api.py" not in ps:
        return "fail", "api.py process not found"
    ss, _ = run("ss -tln | grep ':8787'")
    return ("pass", f"running ({ps.splitlines()[0].strip()[:60]})") if "8787" in ss else \
           ("fail", "process up but 8787 not listening")


@check("data files freshness")
def c_fresh():
    now = time.time()
    rows, worst = [], "pass"
    for fn, pmax, wmax in FRESH:
        p = f"{DATA}/{fn}"
        if not os.path.exists(p):
            rows.append(f"{fn}: MISSING")
            worst = "fail"
            continue
        age = (now - os.stat(p).st_mtime) / 60
        st = "pass" if age <= pmax else ("warn" if age <= wmax else "fail")
        if st == "warn" and worst == "pass":
            worst = "warn"
        if st == "fail":
            worst = "fail"
        rows.append(f"{fn}: {age:.0f}min{'(!)' if st != 'pass' else ''}")
    return worst, " · ".join(rows)


@check("BTC anchoring chain")
def c_anchor():
    try:
        a = json.load(open(f"{DATA}/anchoring.json"))
    except Exception as ex:
        return "fail", f"anchoring.json unreadable: {ex}"
    anchors = a.get("anchors", [])
    if not anchors:
        return "fail", "no anchors"
    pend = [e["label"] for e in anchors if e.get("status") != "confirmed"]
    # gap check: labels from genesis to yesterday
    have = {e["label"] for e in anchors}
    from datetime import timedelta
    start = datetime(2026, 10, 31).date()
    yday = datetime.now(timezone.utc).date() - timedelta(days=1)
    gaps, d = [], start
    while d <= yday:
        lbl = d.strftime("%Y-%m-%d")
        if lbl not in have:
            gaps.append(lbl)
        d += timedelta(days=1)
    head = a.get("chain_head", "")[:12]
    if gaps:
        # stamp cron runs in server local time (UTC+8) at 00/06/12/18 -> first run of
        # each UTC day is 04:10 UTC; before that, "yesterday" is legitimately unstamped.
        now = datetime.now(timezone.utc)
        window_end = now.replace(hour=4, minute=30, second=0, microsecond=0)
        if gaps == [yday.strftime("%Y-%m-%d")] and now < window_end:
            return ("pass",
                    f"head={head}… in stamping window — yesterday ({gaps[0]}) "
                    f"stamps at 04:10 UTC (cron is UTC+8)")
        if gaps == [yday.strftime("%Y-%m-%d")]:
            return ("warn",
                    f"head={head}… yesterday ({gaps[0]}) not stamped after "
                    "04:30 UTC — check stamp cron")
        return "fail", f"head={head}… UNANCHORED DAYS: {gaps}"
    if len(pend) >= 3:
        return "warn", f"head={head}… {len(pend)} anchors pending BTC confirmation ({pend[:3]}…)"
    return "pass", f"head={head}…, {len(anchors)} anchors, gaps=0, pending={len(pend)}"


@check("Base indexer cursor advancing")
def c_indexer():
    p = f"{LOGS}/indexer.log"
    if not os.path.exists(p):
        return "fail", "indexer.log missing"
    age = (time.time() - os.stat(p).st_mtime) / 60
    last, _ = run(f"tail -1 {p}")
    if age > 10:
        return "fail", f"log stale {age:.0f}min; last: {last[:80]}"
    return "pass", f"cursor line: {last[:80]}"


@check("public endpoints (local HTTPS)")
def c_endpoints():
    results, worst = [], "pass"
    for ep, expect in [("/api/ping", "200"), ("/data/stats.json", "200"),
                       ("/data/anchoring.json", "200"), ("/ops.html", "401"),
                       ("/", "200")]:
        code, _ = run(
            f"curl -sk -o /dev/null -w '%{{http_code}}' --resolve paycensus.com:443:127.0.0.1 "
            f"--max-time 10 'https://paycensus.com{ep}'", t=15)
        ok = code == expect
        if not ok and worst == "pass":
            worst = "warn" if code in ("000", "") else "fail"
        results.append(f"{ep}={code}{'' if ok else f'(want {expect})'}")
    return worst, " · ".join(results)


@check("access.log country tagging (CF-IPCountry)")
def c_ctry():
    out, _ = run("wc -l < /var/log/nginx/access.log; grep -c 'ctry=' /var/log/nginx/access.log 2>/dev/null || echo 0")
    lines = [x for x in out.splitlines() if x.strip().isdigit()]
    total, tagged = (int(lines[0]), int(lines[1])) if len(lines) >= 2 else (0, 0)
    if total == 0:
        return "warn", "access.log empty (just rotated?)"
    ratio = tagged / total
    if tagged == 0:
        return "fail", "0 lines carry ctry= (log_format broken?)"
    if ratio < 0.5:
        return "warn", f"ctry= on {tagged}/{total} lines ({ratio:.0%}) — old-format lines may linger"
    return "pass", f"ctry= on {tagged}/{total} lines ({ratio:.0%})"


@check("data layer quick stats")
def c_stats():
    try:
        s = json.load(open(f"{DATA}/stats.json"))
        t = s["totals"]
        e = json.load(open(f"{DATA}/entities.json"))
        ents = e["entities"] if isinstance(e, dict) and "entities" in e else e
        cat = sum(1 for x in ents if x.get("category"))
        return "pass", (f"{t['txs']:,} txs / ${t['volume_usdc']:,.0f} / {t['sellers']:,} sellers · "
                        f"entities={len(ents):,} (with category={cat:,})")
    except Exception as ex:
        return "fail", f"stats/entities unreadable: {ex}"


CHECKS = [c_cron, c_nginx, c_api, c_fresh, c_anchor, c_indexer, c_endpoints, c_ctry, c_stats]

RANK = {"pass": 0, "warn": 1, "fail": 2}


def main():
    t0 = time.time()
    results = []
    for fn in CHECKS:
        try:
            status, detail = fn()
        except Exception as ex:  # noqa: BLE001
            status, detail = "fail", f"check crashed: {ex}"
        results.append({"check": fn._name, "status": status, "detail": detail})
    overall = max((r["status"] for r in results), key=lambda s: RANK[s])
    out = {
        "service": "paycensus-healthcheck",
        "version": "v0.10.0",
        "overall": overall,
        "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "duration_s": round(time.time() - t0, 1),
        "checks": results,
    }
    if "--json" in sys.argv:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"PayCensus self-check {out['ran_at']}  =>  {overall.upper()} ({out['duration_s']}s)")
        for r in results:
            mark = {"pass": "OK  ", "warn": "WARN", "fail": "FAIL"}[r["status"]]
            print(f"  [{mark}] {r['check']}\n         {r['detail']}")
    sys.exit({"pass": 0, "warn": 1, "fail": 2}[overall])


if __name__ == "__main__":
    main()
