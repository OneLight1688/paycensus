#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Outreach sender — CSV + template merge, G1-gated, resumable.

Usage (on main server):
  python3 outreach_sender.py --tier t3 --dry-run   # preview 3 mails, no send
  python3 outreach_sender.py --tier t3 --send      # real send, batch cap
  python3 outreach_sender.py --tier t2 --send --limit 20

Safety rails:
- G1 gate: refuses to send unless /opt/paycensus/.g1_armed exists (chmod 600,
  created ONLY after mail-tester >=8.5 + seed-inbox spot check).
- Daily cap per run (--limit, default 20), resumable via sent log.
- Suppression list: /opt/paycensus/outreach_suppression.txt (one email/line).
- Personalization strictly from the target's own census row.
"""
import argparse
import csv
import json
import os
import smtplib
import sqlite3
import ssl
import sys
import time
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, "/opt/paycensus")
from trust_api import LOGO_CID, LOGO_PATH, _mail_html  # noqa: E402

TIER_FILES = {
    "t3": "/opt/paycensus/outreach_t3.csv",
    "t2": "/opt/paycensus/outreach_t2.csv",
    "t1": "/opt/paycensus/outreach_t1.csv",
}
#: Mega-brand / free-mail email domains: zero conversion value, reputation
#: risk. Filtered by the EMAIL's own domain (enrichment sometimes attaches a
#: docs-site contact to an unrelated entity).
BRAND_EMAIL_DOMAINS = {
    "anthropic.com", "github.com", "openai.com", "google.com", "microsoft.com",
    "apple.com", "amazon.com", "cloudflare.com", "coinbase.com", "binance.com",
    "stripe.com", "vercel.com", "netlify.com", "mozilla.org", "gitlab.com",
    "x.com", "twitter.com", "meta.com", "facebook.com", "coingecko.com",
    "coinmarketcap.com", "alchemy.com", "infura.io", "quicknode.com",
    "tether.to", "gmail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "qq.com", "163.com", "126.com", "icloud.com", "proton.me",
    "protonmail.com",
}
GATE = "/opt/paycensus/.g1_armed"
SUPPRESS = "/opt/paycensus/outreach_suppression.txt"
SENT_LOG = "/var/lib/paycensus/outreach_sent.json"
CRED = "/opt/paycensus/.smtp_hello"
HOST, PORT = "smtp.qiye.aliyun.com", 465
FROM = "PayCensus <hello@paycensus.com>"

SUBJECTS = {
    "t3": "Machine-payment data: {{domain}} cited in a Bitcoin-anchored census",
    "t2": "Agents paid {{domain}} {{vol_usd}} USDC — but they see an anonymous address",
    "t1": "pip install paycensus — the machine-payment census, now importable",
}

FOOTER = ("\n---\nOne-click opt-out: https://trust.paycensus.com/api/outreach/"
          "unsub?e={{email}} (or reply \"stop\")\n"
          "Follow: https://x.com/PayCensusLabs · "
          "https://github.com/OneLight1688/paycensus\n"
          "PayCensus Labs · paycensus.com")


def live_stats(addr):
    """Live txs/volume/first_day from census.db — CSV numbers are stale
    snapshots; recipients cross-check the seller page, so send-time truth
    is mandatory."""
    try:
        c = sqlite3.connect("file:/var/lib/paycensus/census.db?mode=ro",
                            uri=True)
        r = c.execute("SELECT txs, volume, first_day FROM sellers "
                      "WHERE addr=?", (addr,)).fetchone()
        c.close()
        if r:
            return {"txs": r[0], "vol_usd": round(r[1] or 0, 2),
                    "first_day": r[2]}
    except Exception:  # noqa: BLE001
        pass
    return {}


def gateway_html(body):
    """Branded HTML for gateway mails: remote logo (CID impossible via
    gateway), auto-linked URLs, share buttons. Gateway rewrites links to
    track.<domain> and appends its own compliance footer."""
    import html as _h
    import re
    from trust_api import _BTN, _share_links, SOCIAL_GITHUB  # noqa: E402
    esc = _h.escape(body)
    esc = re.sub(r"(https?://[^\s<>\)\]]+)",
                 r'<a href="\1" style="color:#e8730c">\1</a>', esc)
    esc = esc.replace("\n", "<br>")
    s = _share_links()
    btns = (_BTN.format(url=s["x"], bg="#3a3f4a", label="Share on X")
            + _BTN.format(url=s["linkedin"], bg="#0A66C2", label="LinkedIn")
            + _BTN.format(url=s["telegram"], bg="#229ED9", label="Telegram")
            + _BTN.format(url=SOCIAL_GITHUB, bg="#24292f", label="GitHub"))
    logo = ("<img src='https://paycensus.com/logo.png' alt='PayCensus' "
            "width='44' height='44' style='width:44px;height:44px;"
            "border-radius:10px;display:block;margin-bottom:12px'>")
    return ("<div style='font:15px/1.6 -apple-system,Segoe UI,Arial,"
            "sans-serif;color:#1d212a;max-width:560px'>" + logo + esc +
            "<hr style='border:none;border-top:1px solid #e2e4e9;"
            "margin:18px 0 12px'>"
            "<div style='font:600 13px/1.4 -apple-system,Segoe UI,Arial,"
            "sans-serif;color:#1d212a;margin-bottom:8px'>PayCensus — "
            "the census of the machine economy</div>" + btns + "</div>")


def fmt_usd(v):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"${v:,.2f}" if v < 10000 else f"${v:,.0f}"


def merge(tpl, row):
    out = tpl
    for k, v in {
        "domain": row.get("domain") or row.get("addr", "")[:14],
        "email": row.get("email", ""),
        "addr": row.get("addr", ""),
        "vol_usd": fmt_usd(row.get("vol_usd")),
        "txs": row.get("txs") or "0",
        "seller_page": f"https://paycensus.com/s/{row.get('addr','')}.html",
        "first_day": row.get("first_day") or "the census start",
    }.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def load_template(tier):
    """Extract the template body from outreach-templates-v1.md (no footer)."""
    md = open("/opt/paycensus/outreach-templates-v1.md", encoding="utf-8").read()
    letter = {"t3": "A", "t2": "B", "t1": "C"}[tier]
    marker = f"## Template {letter} "
    i = md.find(marker)
    j = md.find("```", i)
    j2 = md.find("```", j + 3)
    body = md[j + 3:j2].strip("\n")
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True, choices=["t3", "t2", "t1"])
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--via", default="gateway", choices=["gateway", "hello"])
    a = ap.parse_args()
    if a.dry_run:
        a.send = False

    if a.send and a.via == "hello":
        # tier-scoped early-batch gate (.g1_armed_<tier>) or full G1 gate
        tier_gate = f"{GATE}_{a.tier}"
        if not (os.path.exists(GATE) or os.path.exists(tier_gate)):
            print(f"REFUSED: no gate armed ({GATE} / {tier_gate} missing).")
            raise SystemExit(1)

    supp = set()
    if os.path.exists(SUPPRESS):
        supp = {ln.strip().lower() for ln in open(SUPPRESS) if ln.strip()}
    sent = json.load(open(SENT_LOG)) if os.path.exists(SENT_LOG) else {}
    done = set(sent.get(a.tier, []))

    rows = list(csv.DictReader(open(TIER_FILES[a.tier])))
    # one mail per domain: prefer friendly role boxes (hello/contact/support
    # > admin/developers > the rest) — never double-mail the same team
    PREF = ("hello", "contact", "support", "team", "info", "admin", "dev")
    by_dom = {}
    for r in rows:
        d = (r.get("domain") or "").lower()
        cur = by_dom.get(d)
        if cur is None:
            by_dom[d] = r
            continue
        lp = r["email"].lower().split("@")[0]
        cp = cur["email"].lower().split("@")[0]
        cr = next((i for i, p in enumerate(PREF) if cp.startswith(p)), 99)
        nr = next((i for i, p in enumerate(PREF) if lp.startswith(p)), 99)
        if nr < cr:
            by_dom[d] = r
    rows = list(by_dom.values())
    n_pre = len(rows)
    rows = [r for r in rows
            if r["email"].lower().split("@")[-1] not in BRAND_EMAIL_DOMAINS]
    if len(rows) < n_pre:
        print(f"  brand-domain filter: {n_pre - len(rows)} removed")
    user, pw = open(CRED).read().strip().split(":")[:2]
    body_tpl = load_template(a.tier)
    subj_tpl = SUBJECTS[a.tier]

    todo = [r for r in rows
            if r["email"].lower() not in supp and r["email"].lower() not in done]
    print(f"tier {a.tier}: {len(rows)} targets, {len(done)} sent, "
          f"{len(rows) - len(todo)} suppressed, {len(todo)} queued, "
          f"cap {a.limit}")

    batch = []
    for r in todo[:a.limit]:
        r2 = dict(r)
        r2.update(live_stats(r.get("addr", "")))
        batch.append(r2)
    for i, r in enumerate(batch):
        subj = merge(subj_tpl, r)
        body = merge(body_tpl, r)
        msg = MIMEMultipart("alternative")
        msg["From"] = FROM
        msg["To"] = r["email"]
        msg["Subject"] = subj
        msg["List-Id"] = "PayCensus Outreach <outreach.paycensus.com>"
        msg["List-Unsubscribe"] = (
            "<https://trust.paycensus.com/api/outreach/unsub?e=" +
            r["email"] + ">, <mailto:hello@paycensus.com?subject=stop>")
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        # plain part: body + compact footer
        msg.attach(MIMEText(body + "\n" + merge(FOOTER, r), "plain"))
        # html part: branded (CID logo + share buttons), reuse trust_api
        html_body = merge(body + "\n\nOne-click opt-out: "
                          "https://trust.paycensus.com/api/outreach/unsub?e="
                          + r["email"], r)
        msg.attach(MIMEText(_mail_html(html_body), "html"))
        logo = MIMEImage(open(LOGO_PATH, "rb").read())
        logo.add_header("Content-ID", "<" + LOGO_CID + ">")
        logo.add_header("Content-Disposition", "inline")
        msg.attach(logo)
        if a.dry_run:
            if a.via == "gateway":
                print(f"--- DRY {i+1} [gateway]: {r['email']}\nSUBJ: {subj}\n"
                      f"[text payload {len(body)} chars, no our-footer, "
                      f"reply_to hello@]\n{body[:280]}…\n")
            else:
                raw = msg.as_string()
                print(f"--- DRY {i+1} [hello]: {r['email']}\nSUBJ: {subj}\n"
                      f"[message built OK: {len(raw)} bytes]\n{body[:280]}…\n")
            if i >= 2:
                break
            continue
        if a.via == "gateway":
            from coldmail_pool import send_one
            res = send_one(r["email"], subj, body,
                           html=gateway_html(body),
                           campaign=f"{a.tier}-2026-11",
                           reply_to="hello@paycensus.com")
            if res.get("queued"):
                sent.setdefault(a.tier, []).append(r["email"])
                json.dump(sent, open(SENT_LOG, "w"))
                print(f"  sent {i+1}/{len(batch)}: {r['email']} "
                      f"via {res.get('mailbox')} (send_id {res.get('send_id')})")
            else:
                reason = res.get("reason", "?")
                print(f"  SKIP {r['email']}: {reason}")
                if reason in ("no_quota", "pool_paused", "pool_limit_paused"):
                    print("  quota exhausted — stopping this run")
                    break
            time.sleep(1)
            continue
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(HOST, PORT, context=ctx, timeout=30) as s:
                s.login(user, pw)
                s.sendmail(user, [r["email"]], msg.as_string())
            sent.setdefault(a.tier, []).append(r["email"])
            json.dump(sent, open(SENT_LOG, "w"))
            print(f"  sent {i+1}/{len(batch)}: {r['email']}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERR {r['email']}: {str(e)[:100]}")
        time.sleep(12)  # polite pacing
    print("done")


if __name__ == "__main__":
    main()
