#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Visitors stats — server-side aggregation of nginx access log.
Human heuristic: exclude known scanner paths (wp-admin/.git/.env/php probes),
bot UAs, non-2xx/3xx statuses; country from CF-IPCountry (ctry= field).
Self-visitor exclusion (2026-11-05): CN egress in Jiangsu province (operator's
own traffic) is excluded — real visitor IP from nginx vip= (CF-Connecting-IP),
province resolved via free ip-api.com batch lookup with a local cache
(only unknown CN IPs are queried; $0, no key).
Publishes /data/visitors.json (today's window). Bot noise is reported
separately so the human number is honest about its own method.
Cron: hourly :50.
"""
import collections
import json
import os
import re
import datetime
import urllib.request

LOG = "/var/log/nginx/access.log"
TRUST_LOG = "/var/log/nginx/access_trust.log"  # pc2 format: $host prefix
OUT = "/www/wwwroot/paycensus/data/visitors.json"
HIST = "/www/wwwroot/paycensus/data/visitors_history.json"
MAX_HIST_DAYS = 730
STATE = "/var/lib/paycensus/visitors_self.json"

# main log: pc (no host) or pc2 (leading $host token) — host group optional
LINE = re.compile(r'^(?:(\S+) )?(\S+) \[([^\]]+)\] "([^"]*)" (\d{3}) "([^"]*)" "([^"]*)"(?: vip=(\S+))? ctry=(\w+)')
TRUST_LINE = re.compile(r'^(\S+) (\S+) \[([^\]]+)\] "([^"]*)" (\d{3}) "([^"]*)" "([^"]*)"(?: vip=(\S+))? ctry=(\w+)')
BOT_UA = re.compile(r"(?i)bot|spider|crawl|slurp|curl|wget|python|okhttp|go-http|semrush|ahrefs|monitor|uptime|headless|scanner|siteradar|zgrab|masscan|nmap|gptbot|claude|openai|bytespider|petalbot|mj12|dotbot|yandex|bingpreview|facebookexternal|meta-external")
SCANNER_PATH = re.compile(r"(?i)wp-admin|wp-login|wp-includes|\.git|\.env|\.php|/login|/admin|/secure|/online$|/en$|/home$|\.aspx|/cgi-bin|wlwmanifest|xmlrpc|phpunit|^//")
OK_STATUS = {"200", "301", "302", "304"}
PAGE_PATHS = {"/", "/explorer.html", "/audit.html", "/index.html",
              "/directory.html", "/indicators.html"}
TRUST_PAGE_PATHS = {"/", "/verify.html", "/api.html", "/tos.html", "/revocations.html"}


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:  # noqa: BLE001
            pass
    return {"ip_region": {}}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE)


def jiangsu_ips(ips):
    """Return the subset of IPs whose province is Jiangsu (cached; new CN IPs
    resolved via free ip-api.com batch lookup — $0, no key)."""
    st = load_state()
    cache = st.setdefault("ip_region", {})
    unknown = [ip for ip in ips if ip and ip not in cache and not ip.startswith("127.")]
    for i in range(0, len(unknown), 100):
        batch = unknown[i:i + 100]
        try:
            req = urllib.request.Request(
                "http://ip-api.com/batch?fields=status,regionName,query",
                data=json.dumps(batch).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                for row in json.loads(r.read()):
                    cache[row.get("query", "?")] = row.get("regionName") or "unknown"
        except Exception:  # noqa: BLE001
            break  # network hiccup: unresolved IPs try again next run
    save_state(st)
    return {ip for ip in ips if "jiangsu" in str(cache.get(ip, "")).lower()}


def main():
    countries = collections.Counter()
    refs = collections.Counter()
    pages = collections.Counter()
    api = collections.Counter()
    total = human = bots = excluded = 0
    candidates = []  # (ctry, vip, ref, p)
    if os.path.exists(LOG):
        for line in open(LOG, errors="replace"):
            m = LINE.match(line)
            if not m:
                continue
            req, status, ref, ua, ctry = m.group(4), m.group(5), m.group(6), m.group(7), m.group(9)
            vip = m.group(8)
            path = req.split(" ")[1] if " " in req else req
            total += 1
            if status not in OK_STATUS:  # mass-404 probes wear browser UAs — never human
                bots += 1
                continue
            if BOT_UA.search(ua):
                bots += 1
                continue
            if SCANNER_PATH.search(path):
                bots += 1
                continue
            candidates.append((ctry, vip, ref, path))
    # ---- trust subdomain (pc2: leading $host token) ----
    t_countries = collections.Counter()
    t_pages = collections.Counter()
    t_api = collections.Counter()
    t_total = t_human = t_bots = t_excluded = 0
    t_candidates = []
    if os.path.exists(TRUST_LOG):
        for line in open(TRUST_LOG, errors="replace"):
            m = TRUST_LINE.match(line)
            if not m:
                continue
            req, status, ref, ua, ctry = m.group(4), m.group(5), m.group(6), m.group(7), m.group(9)
            vip = m.group(8)
            path = req.split(" ")[1] if " " in req else req
            t_total += 1
            if status not in OK_STATUS:
                t_bots += 1
                continue
            if BOT_UA.search(ua):
                t_bots += 1
                continue
            if SCANNER_PATH.search(path):
                t_bots += 1
                continue
            t_candidates.append((ctry, vip, ref, path))
    t_self = jiangsu_ips({c[1] for c in t_candidates if c[0] == "CN" and c[1]})
    for ctry, vip, ref, path in t_candidates:
        if ctry == "CN" and vip in t_self:
            t_excluded += 1
            continue
        t_human += 1
        t_countries[ctry] += 1
        p = path.split("?")[0]
        if p in TRUST_PAGE_PATHS:
            t_pages[p] += 1
        elif p.startswith("/api/"):
            t_api[p] += 1
    self_set = jiangsu_ips({c[1] for c in candidates if c[0] == "CN" and c[1]})
    for ctry, vip, ref, path in candidates:
        if ctry == "CN" and vip in self_set:
            excluded += 1  # operator's own traffic (CN Jiangsu egress)
            continue
        human += 1
        countries[ctry] += 1
        if ref and ref != "-":
            refs[re.sub(r"^https?://", "", ref)[:90]] += 1
        else:
            refs["(direct)"] += 1
        p = path.split("?")[0]
        if p in PAGE_PATHS:
            pages[p] += 1
        elif p.startswith("/data/") or p.startswith("/api/"):
            api[p] += 1
    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "window": "today (server local day, UTC+8)",
        "method": "nginx access-log aggregation; human = non-bot UA, non-scanner path, "
                  "successful (2xx/3xx) response; country from Cloudflare CF-IPCountry header; "
                  "self-traffic (CN Jiangsu egress, real IP via CF-Connecting-IP) excluded. "
                  "human_visitors = distinct CF-Connecting-IP among human requests. "
                  "Cross-check with CF Web Analytics.",
        "totals": {"all_requests": total, "human_requests": human,
                   "human_visitors": len({c[1] for c in candidates if c[1] and c[1] != "-"}),
                   "bot_or_scanner": bots, "self_excluded": excluded},
        "human_by_country": dict(countries.most_common(15)),
        "human_referrers": dict(refs.most_common(10)),
        "human_pages": dict(pages.most_common(10)),
        "data_api_consumers": {k: v for k, v in api.most_common(15)},
        "trust": {
            "totals": {"all_requests": t_total, "human_requests": t_human,
                       "human_visitors": len({c[1] for c in t_candidates if c[1] and c[1] != "-"}),
                       "bot_or_scanner": t_bots, "self_excluded": t_excluded},
            "human_by_country": dict(t_countries.most_common(15)),
            "human_pages": dict(t_pages.most_common(10)),
            "trust_api_consumers": {k: v for k, v in t_api.most_common(15)},
        },
    }
    tmp = OUT + ".tmp"
    json.dump(out, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)

    # ---- daily history: today's record upserted on every run (append-only series) ----
    day = datetime.datetime.now().strftime("%Y-%m-%d")  # server local day (UTC+8)
    rec = {"day": day,
           "all_requests": total, "human_requests": human,
           "human_visitors": len({c[1] for c in candidates if c[1] and c[1] != "-"}),
           "bot_or_scanner": bots, "self_excluded": excluded,
           "trust_all": t_total, "trust_human": t_human, "trust_bots": t_bots,
           "trust_human_visitors": len({c[1] for c in t_candidates if c[1] and c[1] != "-"})}
    try:
        hist = json.load(open(HIST))
    except Exception:  # noqa: BLE001 — first run or unreadable: start fresh
        hist = []
    hist = [d for d in hist if d.get("day") != day]
    hist.append(rec)
    hist.sort(key=lambda d: d.get("day", ""))
    tmp2 = HIST + ".tmp"
    json.dump(hist[-MAX_HIST_DAYS:], open(tmp2, "w", encoding="utf-8"), separators=(",", ":"))
    os.replace(tmp2, HIST)
    print(json.dumps({k: out[k] for k in ("totals", "human_by_country")}, ensure_ascii=False)[:500])


if __name__ == "__main__":
    main()
