#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot: send gateway render test v4 to owner Gmail, then remove its own
cron line. Scheduled 00:10 UTC Nov 17 (quota reset +8h) so the v4 HTML path
is verified BEFORE the 10:05 fire_daily batch."""
import csv
import subprocess
import sys

sys.path.insert(0, "/opt/paycensus")
from coldmail_pool import quota, send_one  # noqa: E402
from outreach_sender import (SUBJECTS, gateway_html, load_template,  # noqa: E402
                             live_stats, merge)

q = quota(force=True)
if int(q.get("sendable_now") or 0) <= 0:
    print("quota not reset yet — aborting")
    raise SystemExit(1)

rows = list(csv.DictReader(open("/opt/paycensus/outreach_t1.csv")))
row = dict(rows[0])
row.update(live_stats(row.get("addr", "")))
body = merge(load_template("t1"), row)
subj = merge(SUBJECTS["t1"], row) + " [gateway render test v4]"
r = send_one("onelight1688@gmail.com", subj, body,
             html=gateway_html(body),
             campaign="t1-dev-gateway-test", reply_to="hello@paycensus.com")
print("render test v4:", r)

r = subprocess.run("crontab -l", shell=True, capture_output=True, text=True)
lines = [l for l in r.stdout.splitlines() if "gateway_render_test" not in l]
open("/tmp/cron_clean", "w").write("\n".join(lines) + "\n")
subprocess.run("crontab /tmp/cron_clean && rm -f /tmp/cron_clean", shell=True)
print("one-shot cron removed")
