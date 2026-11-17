#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PayCensus X commentary — human-voice takes between the daily index posts.

Design (user brief 2026-11-17): the 07:05 UTC daily census digest is a fixed
number card; this script interleaves 1-2 "human" commentary posts per day
about machine payments / crypto / AI-agent developments.

Grounding rule: the LLM may only comment on REAL material we feed it —
fresh Hacker News stories (Algolia API, no key) and our own census numbers.
It is forbidden to invent events, numbers, or announcements. No price
predictions, no financial advice (identity/promise red lines).

LLM: OpenRouter FREE models only (user's account). Token lives in
/opt/paycensus/.openrouter_token (chmod 600, user-editable). Missing or
placeholder token -> graceful skip, cron stays green.

Run: python3 x_comment.py [--dry-run]
Cron: 13:40 & 21:10 UTC (away from 07:05 digest)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/opt/paycensus")
sys.path.insert(0, "/opt/paycensus/pylibs")

TOKEN_FILE = "/opt/paycensus/.openrouter_token"
STATE = "/var/lib/paycensus/x_comment_state.json"
X_POSTS = "/var/lib/paycensus/x_posts.json"  # history via x_post.record_post
LOG = "/var/lib/paycensus/x.log"
PLACEHOLDER = "PASTE-YOUR-OPENROUTER-TOKEN-HERE"

PREFERRED_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
]
HN_QUERIES = ["AI agents", "stablecoin", "crypto payments", "x402",
              "agentic AI", "machine payments", "microtransactions"]


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"{ts} [comment] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def http_json(url, timeout=15):
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (PayCensus; +https://paycensus.com)",
                          "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def openrouter_token():
    try:
        t = open(TOKEN_FILE).read().strip()
    except OSError:
        return ""
    if not t or t == PLACEHOLDER:
        return ""
    return t


def pick_model(token):
    """First available FREE model, preferring the known-good list."""
    d = http_json("https://openrouter.ai/api/v1/models") or {}
    free_ids = {m.get("id") for m in d.get("data", [])
                if str(m.get("id", "")).endswith(":free")}
    for m in PREFERRED_MODELS:
        if m in free_ids:
            return m
    for m in sorted(free_ids):
        return m
    return None


def llm_tweet(token, model, system, user):
    body = json.dumps({"model": model,
                       "max_tokens": 200,
                       "temperature": 0.9,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://paycensus.com",
                 "X-Title": "PayCensus"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return (txt or "").strip().strip('"')
    except Exception as exc:  # noqa: BLE001
        log(f"llm fail ({model}): {str(exc)[:100]}")
        return ""


def hn_stories():
    """Fresh real stories (past 10 days) the take can anchor on."""
    cutoff = int(time.time()) - 10 * 86400
    out = []
    for q in HN_QUERIES:
        url = ("https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=6"
               "&numericFilters=created_at_i>" + str(cutoff) + ",points>3"
               "&query=" + urllib.parse.quote(q))
        d = http_json(url)
        for h in (d or {}).get("hits", []):
            title = (h.get("title") or "").strip()
            oid = h.get("objectID")
            if title and oid:
                out.append({"id": oid, "title": title,
                            "points": h.get("points") or 0,
                            "url": h.get("url") or "",
                            "query": q})
    # dedup by objectID, strongest first
    seen = {}
    for s in out:
        if s["id"] not in seen or s["points"] > seen[s["id"]]["points"]:
            seen[s["id"]] = s
    return sorted(seen.values(), key=lambda s: -s["points"])


def census_facts():
    try:
        stats = json.load(open("/www/wwwroot/paycensus/data/stats.json"))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        days = stats["daily"]
        day = days[-2] if days and days[-1]["day"] == today else days[-1]
        tot = stats.get("totals", {})
        return (f"Yesterday (UTC {day['day']}): {day.get('txs', 0):,} machine-payment "
                f"txs settling {fmt_usd(day.get('volume', 0))}; census totals: "
                f"{tot.get('txs', 0):,} txs / {fmt_usd(tot.get('volume', 0))} since 2026-10-31.")
    except Exception:  # noqa: BLE001
        return ""


def fmt_usd(v):
    v = float(v or 0)
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:,.0f}"


SYSTEM = (
    "You are @PayCensusLabs on X — 'the census of the machine economy'. "
    "PayCensus counts machine-to-machine payments on-chain daily (x402 / "
    "Machine Payments Protocol, HTTP-402 flows, Bitcoin-anchored daily "
    "attestations). Voice: a sharp, knowledgeable analyst-human — "
    "opinionated, concrete, a little dry wit; NOT corporate, NOT hype. "
    "HARD RULES: one tweet, max 260 characters, English. Use ONLY the facts "
    "given to you — never invent events, numbers, names or announcements. "
    "No price predictions, no financial advice, no buying/selling calls. "
    "No more than one hashtag, at most one emoji. Plain text, no quotes "
    "wrapping, no signature."
)


def main():
    dry = "--dry-run" in sys.argv
    token = openrouter_token()
    if not token:
        log("openrouter token not configured (placeholder/missing) — skip")
        return

    state = {}
    try:
        state = json.load(open(STATE))
    except Exception:  # noqa: BLE001
        pass
    used = set(state.get("used_story_ids", []))

    stories = [s for s in hn_stories() if s["id"] not in used]
    facts = census_facts()

    anchor = ""
    if stories:
        s = stories[0]
        anchor = (f"Real recent story being discussed (Hacker News, {s['points']} points): "
                  f"'{s['title']}'")
    if not anchor and not facts:
        log("no anchor material — skip")
        return

    user = (facts + ("\n\n" if facts and anchor else "") + anchor +
            "\n\nWrite ONE tweet: your take on what this says about where "
            "machine payments / the agentic economy is heading. Ground every "
            "claim in the material above; opinions are welcome, invented "
            "facts are not.")

    model = pick_model(token)
    if not model:
        log("no free model available on OpenRouter — skip")
        return
    text = llm_tweet(token, model, SYSTEM, user)
    if not text or len(text) > 280:
        log(f"llm output unusable (len={len(text)}) — skip")
        return
    log(f"model={model} anchor={anchor[:60]!r} -> {text[:100]!r}")

    if dry:
        print("DRY-RUN tweet:", text)
        return

    from x_post import post_tweet, load_creds, record_post  # noqa: E402
    try:
        res = post_tweet(text, load_creds())
        tid = res["data"]["id"]
        record_post(tid, "comment", text)
        log(f"posted comment {tid}")
    except Exception as exc:  # noqa: BLE001
        log(f"post fail: {str(exc)[:140]}")
        return
    if stories:
        state.setdefault("used_story_ids", [])
        state["used_story_ids"] = (state["used_story_ids"] + [stories[0]["id"]])[-200:]
        state["last_model"] = model
        json.dump(state, open(STATE, "w"))


if __name__ == "__main__":
    main()
