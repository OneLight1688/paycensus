#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PayCensus X commentary — human-voice takes between the daily index posts.

User rules (2026-11-17, refined same day):
  1. FREE LLMs only — model id must end with ":free"; paid models are never
     called even as fallback. Token is the user's OpenRouter account,
     editable in the admin panel (Trust Admin -> OpenRouter API token).
  2. 2-4 posts/day at RANDOM times, text-only, human-like rhythm. The 07:05
     UTC census digest (with chart) is a separate script and keeps its image.

Scheduling: on the first wake of each UTC day we draw a random schedule of
2-4 times inside 06:30-23:00 UTC (>=2h apart). A wake cron (every 30 min)
fires a post when a scheduled slot comes due (with small natural jitter and
a 90-min anti-burst gap). Missed slots (>40 min late) are dropped, not
posted late — real humans don't post at exact clock times either.

Grounding rule: the LLM may only comment on REAL material we feed it —
fresh Hacker News stories (Algolia API, no key) and our own census numbers.
It is forbidden to invent events, numbers, or announcements. No price
predictions, no financial advice (identity/promise red lines).

Run: python3 x_comment.py --auto   # cron wake call
     python3 x_comment.py --dry-run
     python3 x_comment.py          # manual: post now
"""
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/opt/paycensus")
sys.path.insert(0, "/opt/paycensus/pylibs")

TOKEN_FILE = "/opt/paycensus/.openrouter_token"
STATE = "/var/lib/paycensus/x_comment_state.json"
LOG = "/var/lib/paycensus/x.log"
PLACEHOLDER = "PASTE-YOUR-OPENROUTER-TOKEN-HERE"

PREFERRED_MODELS = [
    # spread across upstream providers — free pool 429s are per-provider
    "deepseek/deepseek-chat-v3-0324:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "google/gemma-4-26b-a4b-it:free",
]
HN_QUERIES = ["AI agents", "stablecoin", "crypto payments", "x402",
              "agentic AI", "machine payments", "microtransactions"]
WINDOW_START_MIN = int(6.5 * 60)   # 06:30 UTC
WINDOW_END_MIN = int(23.0 * 60)    # 23:00 UTC
MIN_GAP_MIN = 120                  # between scheduled slots
MIN_BURST_GAP_S = 90 * 60          # anti-burst: 90 min between real posts


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


def pick_models(token):
    """Ordered FREE-model candidates only. Paid models are never selected."""
    d = http_json("https://openrouter.ai/api/v1/models") or {}
    free_ids = {m.get("id") for m in d.get("data", [])
                if str(m.get("id", "")).endswith(":free")}
    cands = [m for m in PREFERRED_MODELS if m in free_ids]
    cands += sorted(free_ids - set(cands))
    return cands[:6]


def llm_tweet(token, model, system, user):
    assert model.endswith(":free"), "paid model refused by policy"
    # reasoning-style free models burn tokens on hidden thinking — give room
    # and ask to exclude reasoning where supported
    body = json.dumps({"model": model,
                       "max_tokens": 800,
                       "temperature": 1.0,
                       "reasoning": {"exclude": True},
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://paycensus.com",
                 "X-Title": "PayCensus"})
    for attempt in range(2):  # free tier: ~20 req/min — back off on 429
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            if d.get("error"):
                log(f"llm error ({model}): {str(d['error'])[:140]}")
                return ""
            txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if not (txt or "").strip():
                ch = (d.get("choices") or [{}])[0]
                log(f"llm empty content ({model}): finish={ch.get('finish_reason')} "
                    f"usage={json.dumps(d.get('usage') or {})[:120]}")
            return (txt or "").strip().strip('"')
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                log(f"429 on {model} — backing off 55s (free-tier rpm)")
                time.sleep(55)
                continue
            log(f"llm fail ({model}): http_{exc.code}")
            return ""
        except Exception as exc:  # noqa: BLE001
            log(f"llm fail ({model}): {str(exc)[:120]}")
            return ""
    return ""


def hn_stories():
    """Fresh real stories (past 10 days) a take can anchor on."""
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
                            "points": h.get("points") or 0})
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


def ensure_schedule(state):
    """Once per UTC day: draw 2-4 random slots in the active window."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sch = state.get("schedule") or {}
    if sch.get("date") == today:
        return sch
    n = random.randint(2, 4)
    for _ in range(200):
        mins = sorted(random.sample(range(WINDOW_START_MIN, WINDOW_END_MIN), n))
        if all(b - a >= MIN_GAP_MIN for a, b in zip(mins, mins[1:])):
            break
    sch = {"date": today, "times": [f"{m // 60:02d}:{m % 60:02d}" for m in mins],
           "done": 0}
    state["schedule"] = sch
    log(f"today's schedule drawn: {sch['times']}")
    return sch


def hot_news():
    """Trending headlines (past 36h) from free RSS feeds — the 蹭热点 flavor.

    Sources: Google News RSS (broad: crypto/stablecoin/AI-agents) +
    Cointelegraph RSS (crypto-native). Returns [{title, link, source, ts}].
    """
    out = []
    feeds = [
        ("GoogleNews", "https://news.google.com/rss/search?q="
         + urllib.parse.quote("cryptocurrency OR stablecoin OR \"AI agents\" OR \"agentic payments\"")
         + "&hl=en-US&gl=US&ceid=US:en"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
    ]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    for source, url in feeds:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (PayCensus; +https://paycensus.com)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                root = ET.fromstring(r.read(500000))
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = item.findtext("pubDate") or ""
                # Google News titles end with " - Outlet" — strip the suffix
                clean = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()
                if not clean or len(clean) < 25:
                    continue
                try:
                    dt = datetime.strptime(pub[:25].strip(),
                                           "%a, %d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    dt = datetime.now(timezone.utc)  # undated (CT feed): keep
                if dt < cutoff:
                    continue
                out.append({"title": clean, "link": link, "source": source})
        except Exception as exc:  # noqa: BLE001
            log(f"hot_news feed {source} fail: {str(exc)[:80]}")
    seen = set()
    uniq = []
    for n in out:
        key = n["title"].lower()[:60]
        if key not in seen:
            seen.add(key)
            uniq.append(n)
    return uniq


def build_anchor(state, stories, facts):
    """Flavor lottery (user 2026-11-17: 蹭热点新闻模块):
    ~45% HN niche story, ~35% trending mainstream/crypto news, ~20% census
    data observation — so the timeline reads like a real analyst's mix."""
    news = hot_news()
    roll = random.random()
    if news and roll < 0.35:
        n = random.choice(news[:8])
        anchor_text = (f"Trending right now ({n['source']}): '{n['title']}'")
        return anchor_text, None, n["link"]
    if stories and roll < 0.80:
        s = random.choice(stories[:4])
        return (f"Real recent story being discussed (Hacker News, {s['points']} points): "
                f"'{s['title']}'", s["id"], None)
    if facts:
        return facts, None, None
    if news:
        n = news[0]
        return f"Trending right now ({n['source']}): '{n['title']}'", None, n["link"]
    if stories:
        s = stories[0]
        return (f"Real recent story being discussed (Hacker News): '{s['title']}'",
                s["id"], None)
    return "", None, None


def do_post(state, dry=False):
    token = openrouter_token()
    if not token:
        log("openrouter token not configured — skip")
        return False
    used = set(state.get("used_story_ids", []))
    stories = [s for s in hn_stories() if s["id"] not in used]
    facts = census_facts()
    anchor, anchor_id, news_link = build_anchor(state, stories, facts)
    if not anchor:
        log("no anchor material — skip")
        return False

    user = (anchor + "\n\nWrite ONE tweet: your take on what this says about "
            "where machine payments / the agentic economy is heading. Ground "
            "every claim in the material above; opinions are welcome, "
            "invented facts are not. If this is a headline you may react as "
            "someone who just saw the news — do not pretend to have read the "
            "full article or cite details beyond the headline.")
    models = pick_models(token)
    if not models:
        log("no free model available — skip (policy: never use paid models)")
        return False
    text = ""
    used_model = ""
    for model in models[:4]:  # empty/429 -> try next free model, spaced out
        if used_model:
            time.sleep(8)  # free-tier rpm: don't self-inflict 429s
        text = llm_tweet(token, model, SYSTEM, user)
        used_model = model
        if text and len(text) <= 280:
            break
        text = ""
    if not text or len(text) > 280:
        log(f"llm output unusable across {len(models)} free models — skip")
        return False
    # news flavor: humans share the link ~60% of the time (X counts any URL
    # as 23 chars, so reserve that budget)
    if news_link and random.random() < 0.6:
        if len(text) + 1 + 23 <= 280:
            text = text + " " + news_link
    log(f"model={used_model} -> {text[:110]!r}")
    if dry:
        print("DRY-RUN tweet:", text)
        return True

    from x_post import post_tweet, load_creds, record_post  # noqa: E402
    try:
        res = post_tweet(text, load_creds())
        tid = res["data"]["id"]
        record_post(tid, "comment", text)
        log(f"posted comment {tid}")
    except Exception as exc:  # noqa: BLE001
        log(f"post fail: {str(exc)[:140]}")
        return False
    if anchor_id:
        state.setdefault("used_story_ids", [])
        state["used_story_ids"] = (state["used_story_ids"] + [anchor_id])[-200:]
    state["last_model"] = model
    return True


def main():
    auto = "--auto" in sys.argv
    dry = "--dry-run" in sys.argv
    state = {}
    try:
        state = json.load(open(STATE))
    except Exception:  # noqa: BLE001
        pass

    if not auto:
        ok = do_post(state, dry=dry)
        if ok and not dry:
            state["last_post_epoch"] = time.time()
            sch = state.get("schedule") or {}
            if sch.get("date") == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                sch["done"] = sch.get("done", 0) + 1
        json.dump(state, open(STATE, "w"))
        return

    # ---- wake mode: check today's random schedule ----
    sch = ensure_schedule(state)
    times = sch.get("times", [])
    done = sch.get("done", 0)
    if done >= len(times):
        sys.exit(0)  # daily quota reached — silent, no log spam
    now = datetime.now(timezone.utc)
    now_min = now.hour * 60 + now.minute
    h, m = map(int, times[done].split(":"))
    due = h * 60 + m
    if now_min < due:
        sys.exit(0)
    if now_min - due > 40:
        # slot stale (cron gap) — drop it, humans don't post hours late
        log(f"slot {times[done]} dropped (woke {now_min - due} min late)")
        sch["done"] += 1
        json.dump(state, open(STATE, "w"))
        sys.exit(0)
    last = state.get("last_post_epoch") or 0
    if time.time() - last < MIN_BURST_GAP_S:
        log("anti-burst gap — slot skipped")
        sch["done"] += 1
        json.dump(state, open(STATE, "w"))
        sys.exit(0)

    if do_post(state, dry=dry):
        sch["done"] = done + 1
        state["last_post_epoch"] = time.time()
    json.dump(state, open(STATE, "w"))


if __name__ == "__main__":
    main()
