"""One-shot backfill: aggregate rotated nginx logs into visitors_history.json.

Rotation happens at ~00:5x local, so a local day is split across TWO adjacent
files (tail of one, head of the next). We therefore accumulate raw candidate
rows per day ACROSS all files, then aggregate once per day.
Only lines carrying ctry= (pc/pc2, live since 2026-11-05-ish) are parseable;
combined-format lines are skipped by the LINE regex.
Today's record is owned by visitors.py (hourly upsert) and never touched here.
Run as ubuntu:  python3 /opt/paycensus/visitors_backfill.py
"""
import collections
import datetime
import glob
import gzip
import json
import os
import re
import sys

sys.path.insert(0, "/opt/paycensus")
from visitors import (LINE, TRUST_LINE, BOT_UA, SCANNER_PATH, OK_STATUS,
                      jiangsu_ips, HIST, TRUST_LOG)

DAY_RE = re.compile(r"\[(\d{2})/(\w{3})/(\d{4})")
MONTHS = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05",
          "Jun": "06", "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10",
          "Nov": "11", "Dec": "12"}


def day_of(line):
    m = DAY_RE.search(line)
    if not m:
        return None
    return f"{m.group(3)}-{MONTHS.get(m.group(2), '??')}-{m.group(1)}"


def rows_of(path, acc, line_re=LINE):
    """Append (day, ctry, vip, is_bot) rows from one log file into acc."""
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", errors="replace") as f:
        for line in f:
            m = line_re.match(line)
            if not m:
                continue  # unrecognized format
            _host, _ip, _ts, req, status, _ref, ua, vip, ctry = m.groups()
            d = day_of(line)
            if not d:
                continue
            p = req.split(" ")[1] if " " in req else req
            bots = (status not in OK_STATUS or bool(BOT_UA.search(ua))
                    or bool(SCANNER_PATH.search(p)))
            acc[d].append((ctry, vip, bots))


def main():
    try:
        hist = json.load(open(HIST))
    except Exception:  # noqa: BLE001
        hist = []
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    acc = collections.defaultdict(list)
    for p in sorted(glob.glob("/var/log/nginx/access.log*")):
        if not (p.endswith(".1") or p.endswith(".gz")):
            continue  # current file belongs to visitors.py
        rows_of(p, acc)

    updated = []
    for d, rows in acc.items():
        if d == today:
            continue  # visitors.py owns today (full-day rebuild each run)
        bots_n = sum(1 for r in rows if r[2])
        cands = [(c, v) for c, v, b in rows if not b]
        self_set = jiangsu_ips({v for c, v in cands if c == "CN" and v})
        humans = [(c, v) for c, v in cands
                  if not (c == "CN" and v in self_set)]
        excluded = len(cands) - len(humans)
        rec = {"day": d, "all_requests": len(rows),
               "human_requests": len(humans),
               "human_visitors": len({v for _, v in humans if v and v != "-"}),
               "bot_or_scanner": bots_n, "self_excluded": excluded}
        updated.append(rec)

    # ---- trust fields from the separate trust access log (unrotated so far) ----
    tacc = collections.defaultdict(list)
    if os.path.exists(TRUST_LOG):
        rows_of(TRUST_LOG, tacc, line_re=TRUST_LINE)
    trust_recs = {}
    for d, rows in tacc.items():
        if d == today:
            continue  # visitors.py owns today's trust fields too
        bots_n = sum(1 for r in rows if r[2])
        cands = [(c, v) for c, v, b in rows if not b]
        self_set = jiangsu_ips({v for c, v in cands if c == "CN" and v})
        humans = [(c, v) for c, v in cands
                  if not (c == "CN" and v in self_set)]
        trust_recs[d] = {"trust_all": len(rows), "trust_human": len(humans),
                         "trust_human_visitors": len({v for _, v in humans if v and v != "-"}),
                         "trust_bots": bots_n}

    by_day = {r["day"]: r for r in hist if r.get("day") == today}
    for r in updated:
        by_day[r["day"]] = r
    for d, t in trust_recs.items():
        if d in by_day:
            by_day[d].update(t)
        else:
            by_day[d] = dict(day=d, all_requests=0, human_requests=0,
                             human_visitors=0, bot_or_scanner=0,
                             self_excluded=0, **t)
    merged = sorted(by_day.values(), key=lambda r: r["day"])
    tmp = HIST + ".tmp"
    json.dump(merged[-730:], open(tmp, "w", encoding="utf-8"), separators=(",", ":"))
    os.replace(tmp, HIST)
    for r in merged:
        print(r["day"], "all:", r["all_requests"], "human:", r.get("human_requests"),
              "uniq:", r.get("human_visitors"), "bots:", r.get("bot_or_scanner"),
              "| trust:", r.get("trust_human"), "/", r.get("trust_human_visitors"))


if __name__ == "__main__":
    main()
