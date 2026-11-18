#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PayCensus X auto-reply, email-driven path (free API tier workaround).

X's free API tier blocks all read endpoints (mentions/search), but X emails
reply/mention notifications to the account's registered address
(hello@paycensus.com). This pipeline reads the inbox over IMAP (free),
extracts reply tweets from X notification mails, drafts a reply with the
free-LLM machinery (same red lines as x_comment), and posts it via the
working write endpoint.

Actionable subjects: "replied to your post" / "mentioned you" /
"responded to your reply". Like/follow notifications are ignored.
Our own tweet ids (from x_posts.json) are excluded, and each status is
answered once (state: x_reply_state.json, shared with the reply design).

Aliyun IMAP quirks (learned 2026-11): SEARCH SUBJECT returns BAD -> use
UID SEARCH ALL + local filtering; body extraction prefers text/plain.

Run: python3 x_reply_email.py [--dry-run]
Cron: 25,55 * * * *
"""
import email
import email.header
import imaplib
import json
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/opt/paycensus")
sys.path.insert(0, "/opt/paycensus/pylibs")

from x_comment import llm_tweet, pick_models, openrouter_token, census_facts  # noqa: E402
from x_comment import SYSTEM as COMMENT_SYSTEM  # noqa: E402

SMTP_CRED = "/opt/paycensus/.smtp_hello"
IMAP_HOST = "imap.qiye.aliyun.com"
STATE = "/var/lib/paycensus/x_reply_state.json"
X_POSTS = "/var/lib/paycensus/x_posts.json"
HANDLE = "PayCensusLabs"
MAX_PER_DAY = 5
MIN_GAP_S = 15 * 60
FETCH_UIDS = 60  # recent mails to inspect each wake

ACTIONABLE = re.compile(r"replied|mentioned|responded", re.I)
STATUS_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,20})/status/(\d+)")
X_FROM_RE = re.compile(r"(x\.com|twitter\.com|x\.email|notify@x|info@x)", re.I)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"{ts} [email-reply] {msg}", flush=True)


def creds():
    user, pw = open(SMTP_CRED).read().strip().split(":")[:2]
    return user, pw


def decode_hdr(v):
    """Header fields can arrive as str, Header objects or None — converge to str."""
    try:
        v = str(v) if v else ""
        parts = email.header.decode_header(v)
        return "".join(
            p.decode(cs or "utf-8", "replace") if isinstance(p, bytes) else str(p)
            for p, cs in parts)
    except Exception:  # noqa: BLE001
        return str(v) if v else ""


def body_text(msg):
    """Prefer text/plain; fall back to stripped HTML."""
    plain, html = "", ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        txt = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ct == "text/plain" and not plain:
            plain = txt
        elif ct == "text/html" and not html:
            html = txt
    if plain:
        return plain
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return html


def our_tweet_ids():
    try:
        return {str(r["tweet_id"]) for r in json.load(open(X_POSTS))}
    except Exception:  # noqa: BLE001
        return set()


def actionable_mails():
    """[(status_id, author_handle, snippet)] from recent X notification mails."""
    user, pw = creds()
    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    imap.login(user, pw)
    imap.select("INBOX", readonly=True)
    typ, data = imap.uid("SEARCH", None, "ALL")
    uids = data[0].split()[-FETCH_UIDS:]
    out = []
    for uid in reversed(uids):
        _, md = imap.uid("FETCH", uid, "(RFC822)")
        if not md or not md[0]:
            continue
        msg = email.message_from_bytes(md[0][1])
        frm = decode_hdr(msg.get("From", "")) or ""
        subj = decode_hdr(msg.get("Subject", "")) or ""
        if not X_FROM_RE.search(frm) and not X_FROM_RE.search(subj):
            continue
        if not ACTIONABLE.search(subj):
            continue
        text = body_text(msg)
        # pick the status link that is NOT one of our own posts
        ours = our_tweet_ids()
        for m in STATUS_RE.finditer(text):
            sid, handle = m.group(2), m.group(1)
            if sid in ours or handle.lower() == HANDLE.lower():
                continue
            # snippet: text around the mention of the reply — best effort
            pos = text.find("replied")
            snippet = ""
            for line in text.splitlines():
                ln = line.strip()
                if 20 <= len(ln) <= 400 and "http" not in ln and \
                        "unsubscribe" not in ln.lower() and "x.com" not in ln:
                    snippet = ln
                    break
            out.append({"uid": uid.decode(), "status_id": sid,
                        "author": handle, "snippet": snippet,
                        "subject": subj[:120]})
            break
    imap.logout()
    return out


SYSTEM = COMMENT_SYSTEM + (
    " You are writing a REPLY to someone who answered our post. Keep it "
    "conversational, address their point (even a like-level short reply is "
    "fine), max 230 characters. Never invent facts; use only the given "
    "material. If their reply is a question about our data, answer from the "
    "census facts. If hostile/bait: one calm factual sentence.")


def main():
    dry = "--dry-run" in sys.argv
    token = openrouter_token()
    if not token:
        log("openrouter token not configured — skip")
        return
    st = json.load(open(STATE)) if _ok(STATE) else {}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("reply_date") != today:
        st["reply_date"], st["replies_today"] = today, 0
    if st.get("replies_today", 0) >= MAX_PER_DAY:
        log(f"daily cap {MAX_PER_DAY} reached — skip")
        return
    if time.time() - (st.get("last_reply_epoch") or 0) < MIN_GAP_S:
        log("min gap not elapsed — skip")

    mails = actionable_mails()
    if not mails:
        log("no actionable X notification mails")
        return
    log(f"actionable notifications: {len(mails)}")
    done = st.setdefault("replied_ids", [])
    facts = census_facts()
    posted = 0
    for m in mails:
        sid = m["status_id"]
        if sid in done or st.get("replies_today", 0) >= MAX_PER_DAY:
            continue
        user = (f"X user @{m['author']} replied/mentioned our post "
                f"(notification subject: \"{m['subject']}\").\n"
                f"Extract of the mail body (their words, if present): "
                f"\"{m['snippet'][:280]}\"\n"
                f"Census facts you may cite: {facts}\n"
                f"Write the reply now.")
        text = ""
        for i, model in enumerate(pick_models(token)[:4]):
            if i:
                time.sleep(8)
            text = llm_tweet(token, model, SYSTEM, user)
            if text and len(text) <= 260:
                break
            text = ""
        st.setdefault("attempted_ids", []).append(sid)
        st["attempted_ids"] = st["attempted_ids"][-300:]
        if not text:
            log(f"draft fail for {sid} — marked attempted")
            continue
        if dry:
            print(f"DRY reply to status {sid} (@{m['author']}):\n  {text}")
            continue
        try:
            from x_post import load_creds, record_post  # noqa: E402
            import tweepy
            c = load_creds()
            client = tweepy.Client(consumer_key=c["api_key"],
                                   consumer_secret=c["api_secret"],
                                   access_token=c["access_token"],
                                   access_token_secret=c["access_secret"])
            resp = client.create_tweet(text=text[:280],
                                       in_reply_to_tweet_id=sid)
            rid = str(resp.data["id"])
            log(f"replied to {sid} (@{m['author']}) -> {rid}: {text[:70]!r}")
            record_post(rid, "email_reply", f"(re {sid}) {text}")
            done.append(sid)
            done[:] = done[-300:]
            st["replies_today"] = st.get("replies_today", 0) + 1
            st["last_reply_epoch"] = time.time()
            posted += 1
        except Exception as exc:  # noqa: BLE001
            log(f"reply post fail {sid}: {str(exc)[:160]}")
        if posted:
            time.sleep(random.randint(20, 60))
    st["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    json.dump(st, open(STATE, "w"))
    log(f"done: {posted} replies posted")


def _ok(p):
    import os
    return os.path.exists(p)


if __name__ == "__main__":
    main()
