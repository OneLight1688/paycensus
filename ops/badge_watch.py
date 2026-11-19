#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Badge install watcher: for each verified domain, check whether the site
actually embeds the PayCensus badge (badge.js / any paycensus reference).
Result -> /var/lib/paycensus/badge_watch.json (history per domain, so the
admin can see installs over time and nudge laggards)."""
import json, re, sqlite3, urllib.request
from datetime import datetime, timezone

DB = "/var/lib/paycensus/trust.db"
OUTS = ["/var/lib/paycensus/badge_watch.json",
        "/www/wwwroot/paycensus/data/badge_watch.json"]
UA = {"User-Agent": "Mozilla/5.0 (PayCensus badge monitor; +https://paycensus.com)"}


def site_has_badge(domain):
    for url in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
                html = r.read(150000).decode("utf-8", "replace")
            return ("badge.js" in html.lower()) or ("paycensus" in html.lower()), url
        except Exception:
            continue
    return None, None


def main():
    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    rows = con.execute("SELECT DISTINCT domain, addr FROM verifications "
                       "WHERE status='verified'").fetchall()
    con.close()
    try:
        hist = json.load(open(OUT))
    except Exception:
        hist = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for domain, addr in rows:
        installed, url = site_has_badge(domain)
        hist[domain] = {"addr": addr, "installed": installed, "checked_url": url,
                        "checked_at": today}
        print(f"{domain}: installed={installed} ({url})")
    for _o in OUTS:
        json.dump(hist, open(_o, "w"))


if __name__ == "__main__":
    main()
