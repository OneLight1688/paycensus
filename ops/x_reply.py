#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PayCensus X auto-reply v2 — v2 mentions endpoint, proven by NowCite (round28).

Key discovery (2026-11-18, mirrored from D:\\X-Space\\nowcite-com): X's free
tier BLOCKS v1.1 mentions_timeline and tweepy.Client v2 search, but ALLOWS
hand-signed OAuth-1.0a GET on /2/users/me and /2/users/{id}/mentions. This
module self-signs those GETs (no tweepy for reads), drafts replies with the
free-LLM machinery, and posts via tweepy create_tweet (write works free).

Anti-loop / human-likeness guards (same spirit as NowCite x_reply.py):
  · never reply to our own tweets (author_id == me)
  · each mention answered once; author cooldown 7 days
  · mentions older than 48h are not answered (late replies look robotic)
  · daily cap (5) + min gap; LLM empty/429 -> skip silently, cron retries
Identity/promise red lines in the prompt: honest about being AI, no invented
facts, no price predictions, no financial advice.

Run: python3 x_reply.py [--dry-run]
Cron: 23,53 * * * *
"""
import base64
import hashlib
import hmac
import json
import random
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/opt/paycensus")
sys.path.insert(0, "/opt/paycensus/pylibs")

from x_comment import llm_tweet, pick_models, openrouter_token, census_facts  # noqa: E402

STATE = "/var/lib/paycensus/x_reply_state.json"
CREDS = "/opt/paycensus/.x_credentials"
HANDLE = "PayCensusLabs"
MAX_PER_DAY = 5
MIN_GAP_S = 15 * 60
STALE_HOURS = 48
AUTHOR_COOLDOWN_DAYS = 7
ME_CACHE_DAYS = 7

SYSTEM = (
    "You are @PayCensusLabs on X — 'the census of the machine economy'. "
    "PayCensus counts machine-to-machine payments on-chain daily (x402 / "
    "Machine Payments Protocol, HTTP-402 flows, Bitcoin-anchored). "
    "Write a REPLY to a mention. Voice: friendly analyst-human, concrete, "
    "brief. HARD RULES: max 230 characters, English, plain text (no quote "
    "wrapping, no signature). Use ONLY the facts given — never invent "
    "numbers, events, names. No price predictions, no financial advice, no "
    "outcome promises. Hostile/bait mention -> one calm factual sentence, "
    "never defensive. If asked whether you are a bot/AI: say yes honestly — "
    "the automated voice of PayCensus Labs. At most one emoji, at most one "
    "hashtag. If the mention is spam or not worth a reply, output SKIP."
)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"{ts} [reply] {msg}", flush=True)


# ── OAuth-1.0a GET signing (proven pattern from NowCite ops/_probe_prod_mentions.py)
def _enc(t):
    return urllib.parse.quote(str(t), safe="")


def _sign(b, k):
    return base64.b64encode(hmac.new(k.encode(), b.encode(), hashlib.sha256).digest()).decode()


def oauth_header(creds, method, url, query=None):
    p = {"oauth_consumer_key": creds["api_key"], "oauth_nonce": secrets.token_urlsafe(24),
         "oauth_signature_method": "HMAC-SHA256", "oauth_timestamp": str(int(time.time())),
         "oauth_token": creds["access_token"], "oauth_version": "1.0"}
    signed = dict(p)
    signed.update({str(k): str(v) for k, v in (query or {}).items()})
    bp = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(signed.items()))
    base = "&".join([method, _enc(url), _enc(bp)])
    sk = "&".join([_enc(creds["api_secret"]), _enc(creds["access_secret"])])
    p["oauth_signature"] = _sign(base, sk)
    return "OAuth " + ", ".join(f'{_enc(k)}="{_enc(v)}"' for k, v in sorted(p.items()))


def sget(url, creds, params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), method="GET",
                                 headers={"Authorization": oauth_header(creds, "GET", url, params)})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"ok": True, "data": json.loads(r.read().decode())}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        return {"ok": False, "error": f"HTTP {e.code}", "detail": detail}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def x_creds():
    kv = {}
    for line in open(CREDS):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:  # noqa: BLE001
        return {}


def get_me_cached(st):
    """Cached me (7d) — /2/users/me burns quota too."""
    now = datetime.now(timezone.utc)
    mc = st.get("me_cache") or {}
    try:
        at = datetime.fromisoformat(mc.get("at") or "")
        if (now - at).days < ME_CACHE_DAYS and mc.get("id"):
            return mc
    except ValueError:
        pass
    r = sget("https://api.x.com/2/users/me", x_creds(), {"user.fields": "username"})
    d = (r.get("data") or {}).get("data") or {}
    if not (r.get("ok") and d.get("id")):
        log(f"users/me fail: {r.get('error')} {str(r.get('detail'))[:120]}")
        return {}
    me = {"id": str(d["id"]), "username": d.get("username") or "",
          "at": now.isoformat()}
    st["me_cache"] = me
    return me


def get_mentions(me_id):
    r = sget(f"https://api.x.com/2/users/{me_id}/mentions", x_creds(),
             {"max_results": "25", "tweet.fields": "created_at,author_id",
              "expansions": "author_id", "user.fields": "username"})
    if not r.get("ok"):
        return r
    data = (r.get("data") or {}).get("data") or []
    users = {u["id"]: u.get("username") for u in
             ((r.get("data") or {}).get("includes") or {}).get("users", [])}
    tweets = [{"id": str(t.get("id")), "text": t.get("text") or "",
               "author_id": str(t.get("author_id") or ""),
               "username": users.get(t.get("author_id"), ""),
               "created_at": t.get("created_at") or ""}
              for t in data]
    return {"ok": True, "tweets": tweets}


def main():
    dry = "--dry-run" in sys.argv
    token = openrouter_token()
    if not token:
        log("openrouter token not configured — skip")
        return
    st = load_state()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("reply_date") != today:
        st["reply_date"], st["replies_today"] = today, 0
    if st.get("replies_today", 0) >= MAX_PER_DAY:
        log(f"daily cap {MAX_PER_DAY} reached — skip")
        return
    if time.time() - (st.get("last_reply_epoch") or 0) < MIN_GAP_S:
        log("min gap not elapsed — skip")
        return

    me = get_me_cached(st)
    if not me.get("id"):
        return
    res = get_mentions(me["id"])
    if not res.get("ok"):
        log(f"mentions fail: {res.get('error')} {str(res.get('detail'))[:120]}")
        return
    tweets = res["tweets"]
    log(f"mentions fetched: {len(tweets)}")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=STALE_HOURS)
    cooldown_since = now - timedelta(days=AUTHOR_COOLDOWN_DAYS)
    seen = set(st.get("replied_ids", []))
    attempted = set(st.get("attempted_ids", []))
    cooldown = st.get("author_cooldowns") or {}

    cands = []
    for t in tweets:
        if t["id"] in seen or t["id"] in attempted:
            continue
        if t["author_id"] == me["id"]:
            continue  # never reply to ourselves
        if cooldown.get(t["author_id"], "") > cooldown_since.isoformat():
            continue
        try:
            created = datetime.fromisoformat(
                t["created_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if created < cutoff:
            continue
        cands.append(t)
    if not cands:
        log(f"no replyable mention (scanned {len(tweets)})")
        json.dump(st, open(STATE, "w"))
        return

    facts = census_facts()
    posted = 0
    for t in cands:
        if st.get("replies_today", 0) >= MAX_PER_DAY:
            break
        user = (f"@{t['username']} mentioned @{HANDLE} on X. Their message: "
                f"\"{t['text'][:280]}\"\nCensus facts you may cite: {facts}\n"
                "Write the reply now. If it is spam or not worth answering, "
                "output exactly SKIP.")
        text = ""
        for i, model in enumerate(pick_models(token)[:4]):
            if i:
                time.sleep(8)
            text = llm_tweet(token, model, SYSTEM, user)
            if text and len(text) <= 260:
                break
            text = ""
        st.setdefault("attempted_ids", []).append(t["id"])
        st["attempted_ids"] = st["attempted_ids"][-300:]
        if not text or text.strip().upper() in {"SKIP", "N/A", "NONE"}:
            log(f"no draft for {t['id']} (@{t['username']}) — skipped")
            continue
        if dry:
            print(f"DRY reply to {t['id']} (@{t['username']}): {t['text'][:60]!r}\n  -> {text}")
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
                                       in_reply_to_tweet_id=t["id"])
            rid = str(resp.data["id"])
            log(f"replied {t['id']} (@{t['username']}) -> {rid}: {text[:70]!r}")
            record_post(rid, "reply", f"(re @{t['username']}) {text}")
            st.setdefault("replied_ids", []).append(t["id"])
            st["replied_ids"] = st["replied_ids"][-300:]
            cd = st.setdefault("author_cooldowns", {})
            cd[t["author_id"]] = now.isoformat()
            st["replies_today"] = st.get("replies_today", 0) + 1
            st["last_reply_epoch"] = time.time()
            posted += 1
        except Exception as exc:  # noqa: BLE001
            log(f"reply post fail {t['id']}: {str(exc)[:160]}")
        if posted:
            time.sleep(random.randint(20, 60))
    json.dump(st, open(STATE, "w"))
    log(f"done: {posted} replies posted")


if __name__ == "__main__":
    main()
