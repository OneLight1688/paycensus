#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PayCensus X auto-reply — respond to mentions and replies to our posts.

User brief (2026-11-18): 让管线自动拟回复.

Sources of inbound:
  a) recent mentions: search "@PayCensusLabs -is:retweet -from:us" (7d window)
  b) replies to our recent posts that omit the handle: per recent tweet,
     search "conversation_id:<tid> -from:us -is:retweet"

Guardrails (identity/promise red lines, user-approved):
  - if asked whether we're a bot: answer honestly ("yes — automated voice of
    PayCensus Labs"); never fake a human identity
  - data claims ONLY from the census facts fed to the prompt; no invented
    numbers/events; no price predictions, no financial advice
  - no arguments, no engagement-bait; skip hostile/bait tweets quietly

Rate hygiene: free-LLM machinery shared with x_comment (candidate models,
backoff); max 5 replies/day (UTC), >=15 min between replies, reply each
mention once. LLM failure -> skip silently, next wake retries nothing (the
mention is marked attempted to avoid loops).

Run: python3 x_reply.py [--dry-run]
Cron: 23,53 * * * * (offset from the posting wake at :17/:47)
"""
import json
import random
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/opt/paycensus")
sys.path.insert(0, "/opt/paycensus/pylibs")

from x_comment import llm_tweet, pick_models, openrouter_token, census_facts, fmt_usd  # noqa: E402

STATE = "/var/lib/paycensus/x_reply_state.json"
HANDLE = "PayCensusLabs"
MAX_PER_DAY = 5
MIN_GAP_S = 15 * 60

SYSTEM = (
    "You are @PayCensusLabs on X — 'the census of the machine economy'. "
    "PayCensus counts machine-to-machine payments on-chain daily (x402 / "
    "Machine Payments Protocol, HTTP-402 flows, Bitcoin-anchored). "
    "Write a REPLY to the user's tweet. Voice: friendly analyst-human, "
    "concrete, warm, brief. HARD RULES: max 230 characters, English, plain "
    "text (no quotes wrapping, no signature). Use ONLY facts given to you — "
    "never invent numbers, events, names. No price predictions, no "
    "financial advice, no promises about outcomes. If the tweet is hostile "
    "or pure bait, respond with one calm factual sentence, never defensive. "
    "If asked whether you are a bot/AI: say yes honestly — you are the "
    "automated voice of PayCensus Labs — and offer the census link. At most "
    "one emoji, at most one hashtag."
)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"{ts} [reply] {msg}"
    print(line, flush=True)


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:  # noqa: BLE001
        return {}


def client():
    from x_post import load_creds  # noqa: E402
    import tweepy
    c = load_creds()
    return tweepy.Client(consumer_key=c["api_key"], consumer_secret=c["api_secret"],
                         access_token=c["access_token"],
                         access_token_secret=c["access_secret"],
                         wait_on_rate_limit=True)


def collect_inbound(api):
    """(mention tweet, parent tweet or None) candidates not yet answered."""
    st = load_state()
    replied = set(st.get("replied_ids", []))
    attempted = set(st.get("attempted_ids", []))
    cands = {}
    queries = [f"@{HANDLE} -is:retweet -from:{HANDLE}"]
    try:
        me_tweets = api.get_users_tweets(f"me", max_results=15).data or []
        for t in me_tweets[:10]:
            queries.append(f"conversation_id:{t.id} -from:{HANDLE} -is:retweet")
    except Exception as ex:  # noqa: BLE001
        log(f"user_timeline fail: {str(ex)[:120]}")
    for q in queries:
        try:
            res = api.search_recent_tweets(q, max_results=20,
                                           tweet_fields=["in_reply_to_user_id",
                                                         "in_reply_to_status_id",
                                                         "created_at", "author_id"])
            for tw in (res.data or []):
                if str(tw.id) in replied or str(tw.id) in attempted:
                    continue
                if tw.text.startswith("RT @"):
                    continue
                cands[str(tw.id)] = tw
        except Exception as ex:  # noqa: BLE001
            log(f"search '{q[:40]}' fail: {str(ex)[:120]}")
    return list(cands.values()), replied


def parent_text(api, tw):
    """Text of our tweet being replied to (if any)."""
    pid = getattr(tw, "in_reply_to_status_id", None) if tw else None
    if not pid:
        return ""
    try:
        r = api.get_tweet(pid)
        return (r.data.text or "")[:280] if r.data else ""
    except Exception:  # noqa: BLE001
        return ""


def draft_reply(api, token, tw, parent, facts):
    user = (f"Tweet to reply to (author @? unknown handle, don't address by "
            f"name): \"{tw.text[:280]}\"\n")
    if parent:
        user += f"(It replies to our post: \"{parent[:200]}\")\n"
    user += "\nCensus facts you may cite: " + facts
    user += "\nWrite the reply now."
    models = pick_models(token)
    for i, model in enumerate(models[:4]):
        if i:
            time.sleep(8)
        text = llm_tweet(token, model, SYSTEM, user)
        if text and len(text) <= 260:
            return text
    return ""


def main():
    dry = "--dry-run" in sys.argv
    token = openrouter_token()
    if not token:
        log("openrouter token not configured — skip")
        return
    api = client()
    st = load_state()

    # daily cap + gap
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("reply_date") != today:
        st["reply_date"] = today
        st["replies_today"] = 0
    if st.get("replies_today", 0) >= MAX_PER_DAY:
        log(f"daily cap {MAX_PER_DAY} reached — skip")
        return
    last = st.get("last_reply_epoch") or 0
    if time.time() - last < MIN_GAP_S:
        log("min gap not elapsed — skip")
        return

    inbound, replied = collect_inbound(api)
    if not inbound:
        log("no new inbound mentions/replies")
        return
    random.shuffle(inbound)  # human-like: not always newest first
    facts = census_facts()
    replied_now = 0
    for tw in inbound:
        if st.get("replies_today", 0) + replied_now >= MAX_PER_DAY:
            break
        parent = parent_text(api, tw)
        text = draft_reply(api, token, tw, parent, facts)
        st.setdefault("attempted_ids", []).append(str(tw.id))
        st["attempted_ids"] = st["attempted_ids"][-300:]
        if not text:
            log(f"draft fail for {tw.id} — marked attempted (no retry loop)")
            continue
        if dry:
            print(f"DRY reply to {tw.id} ({tw.text[:60]!r}):\n  {text}")
            continue
        try:
            resp = api.create_tweet(text=text[:280],
                                    in_reply_to_tweet_id=str(tw.id))
            rid = str(resp.data["id"])
            log(f"replied {tw.id} -> {rid}: {text[:80]!r}")
            from x_post import record_post  # noqa: E402
            record_post(rid, "reply", f"(re {tw.id}) {text}")
            st.setdefault("replied_ids", []).append(str(tw.id))
            st["replied_ids"] = st["replied_ids"][-300:]
            st["replies_today"] = st.get("replies_today", 0) + 1
            st["last_reply_epoch"] = time.time()
            replied_now += 1
        except Exception as exc:  # noqa: BLE001
            log(f"reply post fail {tw.id}: {str(exc)[:160]}")
        if replied_now:
            time.sleep(random.randint(20, 60))  # human-ish spacing
    json.dump(st, open(STATE, "w"))
    log(f"done: {replied_now} replies posted")


if __name__ == "__main__":
    main()
