#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus mailbox warmup — hello@paycensus.com reputation building (G1 gate).
Sends a genuine daily data digest to a rotating pool of self-controlled seed
mailboxes across 4 providers. No marketing, no third parties, honest content.
Schedule (state-driven, from warmup_state.json):
  day 1-3: 5/day | day 4-7: 10/day | day 8-14: 15/day | day 15-21: 25/day
  day 22-28: 40/day | day 29+: 60/day (cap; review G1 before any external send)
G1 gate (delivery >=95%) is evaluated from bounce/error log — external sending
is NOT part of this script and stays blocked until user signs off.
Cron: daily 09:30 UTC.
"""
import json
import os
import sqlite3
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen

CRED = "/opt/paycensus/.smtp_hello"     # user:app_password (chmod 600)
STATE = "/var/lib/paycensus/warmup_state.json"
LOGDB = "/var/lib/paycensus/census.db"
LOG = "/var/lib/paycensus/warmup.log"

# multi-identity: separate cred/state per identity, plus its SMTP host/port
ALIYUN = ("smtp.qiye.aliyun.com", 465)
ZOHO = ("smtp.zoho.com", 465)
IDENTITIES = {
    "hello": (CRED, STATE) + ALIYUN,
    "bd": ("/opt/paycensus/.smtp_bd", "/var/lib/paycensus/warmup_state_bd.json") + ALIYUN,
    "foxreply": ("/opt/paycensus/.smtp_foxreply",
                 "/var/lib/paycensus/warmup_state_foxreply.json") + ZOHO,
}

SEEDS = [
    "hello@nowcite.com", "support@nowcite.com", "sales@nowcite.com",
    "scan@nowcite.com", "billing@nowcite.com", "bd@nowcite.com",
    "leo@speedcite.com", "19951951688@189.cn", "onelight1688@gmail.com",
]

QUOTA = [(3, 5), (7, 10), (14, 15), (21, 25), (28, 40), (9999, 60)]


def log(s):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} {s}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def quota_for(day):
    for dmax, q in QUOTA:
        if day <= dmax:
            return q
    return 60


def daily_digest():
    """Real numbers from our own public stats — honest content, varies daily."""
    def fetch(url):
        r = Request(url, headers={"User-Agent": "paycensus/0.5"})
        return json.loads(urlopen(r, timeout=15).read())

    try:
        d = fetch("https://paycensus.com/data/stats.json")
        t = d.get("totals", {})
        cal = fetch("https://paycensus.com/data/calendar.json")
        days = cal.get("days") or cal.get("calendar") or []
        last = days[-1] if isinstance(days, list) and days else {}
        ent = fetch("https://paycensus.com/data/entities.json")
        return (f"txs {t.get('txs', '?')} | volume ${t.get('volume_usdc', '?')} | "
                f"sellers {t.get('sellers', '?')} | buyers {t.get('buyers', '?')} | "
                f"yesterday: {last.get('txs', '?')} txs / ${last.get('volume', last.get('volume_usdc', '?'))} | "
                f"attributed entities {ent.get('count', '?')}")
    except Exception as e:  # noqa: BLE001
        return f"digest fetch failed: {str(e)[:60]}"


def main():
    ident = "hello"
    if len(sys.argv) > 2 and sys.argv[1] == "--identity":
        ident = sys.argv[2]
    cred_path, state_path, smtp_host, smtp_port = IDENTITIES.get(
        ident, IDENTITIES["hello"])
    state = {"started": None, "day": 0, "sent_total": 0, "errors": 0}
    if os.path.exists(state_path):
        state = json.load(open(state_path))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_day") == today:
        log(f"[{ident}] already sent today, skip")
        return
    state["day"] += 1
    if not state.get("started"):
        state["started"] = today
    state["last_day"] = today

    creds = open(cred_path).read().strip().split(":")
    user, pwd = creds[0], creds[1]
    n = quota_for(state["day"])
    recipients = [SEEDS[(state["day"] * 3 + i) % len(SEEDS)] for i in range(n)]
    recipients = list(dict.fromkeys(recipients))  # dedupe, keep order

    body_line = daily_digest()
    subject = f"PayCensus daily digest — day {state['day']}"
    ok = err = 0
    for to in recipients:
        msg = MIMEMultipart("alternative")
        msg["From"] = user
        msg["To"] = to
        msg["Subject"] = subject
        text = (f"PayCensus internal digest (warmup day {state['day']})\n\n"
                f"{body_line}\n\n"
                f"Data: https://paycensus.com | Explorer: https://paycensus.com/explorer.html\n"
                f"You receive this as a controlled seed mailbox for sender-reputation warmup.")
        msg.attach(MIMEText(text, "plain", "utf-8"))
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=30) as s:
                s.login(user, pwd)
                s.sendmail(user, [to], msg.as_string())
            ok += 1
            state["sent_total"] += 1
        except Exception as e:  # noqa: BLE001
            err += 1
            state["errors"] += 1
            log(f"  SEND-FAIL to {to}: {str(e)[:80]}")
        time.sleep(2)

    # audit trail in SQLite
    try:
        conn = sqlite3.connect(LOGDB, timeout=30)
        conn.execute("""CREATE TABLE IF NOT EXISTS warmup_sends (
            day INTEGER, sent_on TEXT, recipients TEXT, ok INTEGER, err INTEGER)""")
        conn.execute("INSERT INTO warmup_sends VALUES(?,?,?,?,?)",
                     (state["day"], today, ",".join(recipients), ok, err))
        conn.commit()
        conn.close()
    except Exception as e:  # noqa: BLE001
        log(f"  db log fail: {str(e)[:60]}")

    json.dump(state, open(state_path, "w"))
    log(f"[{ident}] day {state['day']}: quota={n} ok={ok} err={err} sent_total={state['sent_total']}")


import time  # noqa: E402

if __name__ == "__main__":
    main()
