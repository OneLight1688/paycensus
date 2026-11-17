#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus X (Twitter) auto-publisher.
- tweepy (installed to /opt/paycensus/pylibs via pip --target) + pillow for chart rendering.
- Modes:
    x_post.py "text"        post a tweet verbatim (text only)
    x_post.py --daily       render the 30-day settlement chart from stats.json,
                            upload as media, post the daily census digest with image
- Credentials: /opt/paycensus/.x_credentials (chmod 600, never in repo).
- Log: /var/lib/paycensus/x.log
Cron: 5 7 * * *  (07:05 UTC, after the 06:30 anchor upgrade pass)
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

import urllib.error

sys.path.insert(0, "/opt/paycensus/pylibs")

INDICES_PATH = "/www/wwwroot/paycensus/data/indices.json"
X_POSTS_PATH = "/var/lib/paycensus/x_posts.json"


def record_post(tid, mode, text):
    """Append a posted-tweet record for the admin tracking panel."""
    try:
        recs = json.load(open(X_POSTS_PATH))
    except Exception:  # noqa: BLE001
        recs = []
    recs.append({
        "posted_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "tweet_id": str(tid),
        "url": f"https://x.com/PayCensusLabs/status/{tid}",
        "mode": mode,
        "text_head": text[:140],
    })
    try:
        json.dump(recs[-500:], open(X_POSTS_PATH + ".tmp", "w"))
        os.replace(X_POSTS_PATH + ".tmp", X_POSTS_PATH)
    except Exception as ex:  # noqa: BLE001
        print(f"record_post FAIL: {str(ex)[:120]}")


def _indices_line():
    """MPI/TVI line for the daily post — empty string if indices unavailable."""
    try:
        d = json.load(open(INDICES_PATH))
        mpi = (d.get("mpi") or {}).get("value")
        tvi = (d.get("tvi") or {}).get("value_pct")
        if mpi is None:
            return ""
        line = f"\nMPI {mpi:,.2f} · TVI {tvi}% (base 10-31=100)"
        if tvi is None:
            line = f"\nMPI {mpi:,.2f} (base 10-31=100)"
        return line
    except Exception:  # noqa: BLE001
        return ""

CREDS = "/opt/paycensus/.x_credentials"
LOG = "/var/lib/paycensus/x.log"
SITE = "paycensus.com"
CHART = "/tmp/paycensus_daily_chart.png"
AVATAR = "/opt/paycensus/logo_avatar.png"
TAGLINE = "The census of the machine economy"

BG = (18, 20, 26)
CIRC = (29, 33, 42)
GRID = (38, 42, 52)
ORANGE = (246, 130, 31)
ORANGE_DIM = (160, 92, 30)
WHITE = (245, 245, 245)
MUTED = (154, 160, 170)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"{ts} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_creds():
    kv = {}
    for line in open(CREDS):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            kv[k.strip()] = v.strip()
    need = ["api_key", "api_secret", "access_token", "access_secret"]
    missing = [k for k in need if not kv.get(k)]
    if missing:
        raise SystemExit(f"credentials missing: {missing}")
    return kv


def fmt_usd(v):
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:,.0f}"


def _load_indices():
    """Return (mpi, tvi) dicts from indices.json — empty dicts if unavailable."""
    try:
        d = json.load(open(INDICES_PATH))
        return d.get("mpi") or {}, d.get("tvi") or {}
    except Exception:  # noqa: BLE001
        return {}, {}


def render_chart(stats, day):
    """Branded daily card: logo header + MPI/TVI index chips + volume bars. 1600x900."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1600, 900
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    F = lambda s: ImageFont.load_default(size=s)

    days = stats["daily"][-30:]
    # drop the in-progress day (today UTC) — charts must show complete days only,
    # matching the digest text (which uses yesterday)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if days and days[-1]["day"] == today:
        days = days[:-1]
    tot = stats["totals"]
    mpi, tvi = _load_indices()

    # --- brand header band ---
    d.rectangle([0, 0, W, 6], fill=ORANGE)
    try:
        av = Image.open(AVATAR).convert("RGBA").resize((124, 124))
        img.paste(av, (60, 42), av)
    except Exception:  # noqa: BLE001 — logo optional, card still renders
        pass
    d.text((214, 54), "PayCensus", font=F(64), fill=WHITE)
    d.text((218, 134), TAGLINE, font=F(27), fill=MUTED)
    d.text((W - 60 - d.textlength(SITE, font=F(34)), 60), SITE, font=F(34), fill=ORANGE)

    # --- index chips (MPI / TVI) ---
    chip_y, chip_h = 212, 100
    if mpi.get("value") is not None:
        dd = mpi.get("day_over_day_pct")
        dd_txt = f"  {'+' if (dd or 0) >= 0 else ''}{dd:.1f}% d/d" if dd is not None else ""
        d.rounded_rectangle([100, chip_y, 760, chip_y + chip_h], radius=14,
                            fill=CIRC, outline=ORANGE_DIM, width=2)
        d.text((130, chip_y + 14), "MACHINE PAYMENT INDEX", font=F(22), fill=MUTED)
        d.text((130, chip_y + 44), f"MPI {mpi['value']:,.2f}{dd_txt}", font=F(40), fill=ORANGE)
    if tvi.get("value_pct") is not None:
        d.rounded_rectangle([800, chip_y, 1460, chip_y + chip_h], radius=14,
                            fill=CIRC, outline=ORANGE_DIM, width=2)
        d.text((830, chip_y + 14), "TRUST VERIFICATION INDEX", font=F(22), fill=MUTED)
        d.text((830, chip_y + 44),
               f"TVI {tvi['value_pct']}%  ({tvi.get('verified', 0):,}/{tvi.get('attributed_entities', 0):,})",
               font=F(40), fill=ORANGE)

    # plot area
    x0, y0, x1, y1 = 100, 500, W - 100, 740
    vmax = max((r["volume"] for r in days), default=1) or 1
    n = len(days)
    slot = (x1 - x0) / n
    bw = max(8, int(slot * 0.55))

    # last-day callout + totals strip
    last = days[-1]
    d.text((x0, 356), f"{last['day']}  ·  {last['txs']:,} txs  ·  {fmt_usd(last['volume'])}",
           font=F(40), fill=WHITE)
    d.text((x0, 416), f"total: {tot['txs']:,} txs · {fmt_usd(tot['volume_usdc'])} · "
                      f"{tot.get('sellers', 0):,} sellers · {tot.get('buyers', 0):,} buyers",
           font=F(28), fill=MUTED)

    # gridlines
    for i in range(1, 4):
        gy = y0 + (y1 - y0) * i / 4
        d.line([x0, gy, x1, gy], fill=GRID, width=2)
        d.text((x0, gy - 26), fmt_usd(vmax * (1 - i / 4)), font=F(22), fill=MUTED)

    # bars
    for i, r in enumerate(days):
        h = (y1 - y0 - 10) * (r["volume"] / vmax)
        bx = x0 + i * slot + (slot - bw) / 2
        color = ORANGE if i == n - 1 else ORANGE_DIM
        d.rectangle([bx, y1 - h, bx + bw, y1], fill=color)
    # baseline
    d.line([x0, y1, x1, y1], fill=MUTED, width=2)

    # date labels (every ~5 bars, first & last always)
    for i, r in enumerate(days):
        if i in (0, n - 1) or i % max(1, n // 6) == 0:
            d.text((x0 + i * slot + 4, y1 + 14), r["day"][5:], font=F(22), fill=MUTED)

    d.text((x0, H - 70), "no weights, no forecasts · Bitcoin-anchored · OpenTimestamps · paycensus.com",
           font=F(25), fill=MUTED)
    img.save(CHART)
    return CHART


def post_tweet(text, creds, media_path=None):
    import tweepy
    auth = tweepy.OAuth1UserHandler(creds["api_key"], creds["api_secret"],
                                    creds["access_token"], creds["access_secret"])
    api = tweepy.API(auth)
    media_ids = []
    if media_path:
        m = api.media_upload(media_path)
        media_ids = [m.media_id]
    client = tweepy.Client(
        consumer_key=creds["api_key"],
        consumer_secret=creds["api_secret"],
        access_token=creds["access_token"],
        access_token_secret=creds["access_secret"],
    )
    resp = client.create_tweet(text=text[:280], media_ids=media_ids)
    return {"data": {"id": str(resp.data.get("id", "?"))}}


def daily_payload():
    """Return (text, chart_path or None) for the daily digest."""
    stats = json.load(open("/www/wwwroot/paycensus/data/stats.json"))
    anch = json.load(open("/www/wwwroot/paycensus/data/anchoring.json"))
    # use the last COMPLETE day (yesterday UTC) — today's row is still accumulating
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    days = stats["daily"]
    day = days[-2] if days and days[-1]["day"] == today else days[-1]
    tot = stats["totals"]
    anchor = next((a for a in anch["anchors"] if a["label"] == day["day"]), None)
    anchor_txt = ""
    if anchor:
        if anchor.get("status") == "confirmed":
            anchor_txt = f"Bitcoin-anchored (BTC block {anchor['btc_block']})"
        else:
            anchor_txt = "Bitcoin-anchored (confirming)"
    vol_tot = tot.get("volume_usdc", 0) or 0
    vol_tot_s = (f"${vol_tot/1e6:.1f}M" if vol_tot >= 1e6
                 else f"${vol_tot:,.0f}")
    est = (stats.get("tempo_mpp") or {}).get("est_daily_txs", 0)
    # greedy assembly: high-priority lines first; the tail yields when the
    # 280-char budget runs out instead of being blind-truncated.
    parts = [
        f"PayCensus daily census {day['day']} (x402):",
        f"{day['txs']:,} txs · ${day['volume']:,.0f} settled",
        f"total: {tot['txs']:,} txs · {vol_tot_s} · "
        f"{tot.get('sellers', 0):,} sellers",
        _indices_line().lstrip("\n"),
        anchor_txt,
        "🔒 Get PayCensus-Verified → trust.paycensus.com",
        f"MPP/Tempo sampled: ~{est:,}/day" if est else "",
        f"data: {SITE}/data/stats.json",
    ]
    text = ""
    for p in parts:
        if not p:
            continue
        cand = (text + "\n" + p) if text else p
        if len(cand) <= 272:
            text = cand
    text = text or parts[0]
    chart = None
    if len(days) >= 2:
        try:
            chart = render_chart(stats, day)
        except Exception as ex:  # noqa: BLE001 — chart is optional, text still posts
            log(f"chart render failed: {str(ex)[:150]}")
    return text, chart


def main():
    creds = load_creds()
    if len(sys.argv) > 1 and sys.argv[1] == "--daily":
        text, chart = daily_payload()
    else:
        text = " ".join(sys.argv[1:]).strip()
        chart = None
    if not text:
        raise SystemExit("nothing to post")
    if len(text) > 280:
        text = text[:277] + "…"
    try:
        resp = post_tweet(text, creds, media_path=chart)
        tid = resp.get("data", {}).get("id", "?")
        log(f"posted id={tid} len={len(text)} media={'y' if chart else 'n'} text={text[:80]!r}")
        record_post(tid, "daily" if chart else "text", text)
        print(f"https://x.com/PayCensusLabs/status/{tid}")
    except Exception as e:  # noqa: BLE001
        body = ""
        if isinstance(e, urllib.error.HTTPError):
            body = e.read().decode()[:300]
        log(f"FAIL {type(e).__name__}: {str(e)[:200]} {body}")
        sys.exit(1)


if __name__ == "__main__":
    main()
