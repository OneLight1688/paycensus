#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-seller public profile pages — structural SEO asset.

One static page per ATTRIBUTED seller (entities with a domain, ~1.5k and
growing). Incremental by design: existing files are skipped, only newly
attributed sellers get pages each run (daily cron). Pages carry an as-of
snapshot + live verification status via /api/status, and the conversion CTA
(NOT VERIFIED -> free verification). Never includes emails.

Publishes /www/wwwroot/paycensus/s/{addr}.html and sitemap-sellers.xml.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
OUT = "/www/wwwroot/paycensus/s"
SITEMAP = "/www/wwwroot/paycensus/sitemap-sellers.xml"
AS_OF = datetime.now(timezone.utc).strftime("%Y-%m-%d")

TPL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{domain} — machine-payment seller profile · PayCensus</title>
<meta name="description" content="{domain} has settled {vol_usd} across {txs} x402 machine payments on Base USDC. Verification status, settlement history and trust factors — tracked by the PayCensus census of the machine economy.">
<meta name="robots" content="index,follow">
<link rel="icon" type="image/png" href="/favicon.ico">
<style>
*{{box-sizing:border-box}}
body{{font-family:system-ui;margin:0;background:#0a0e14;color:#e5ecf5;padding:32px 20px;max-width:720px;margin:0 auto}}
a{{color:#5eead4;text-decoration:none}}
h1{{font-size:22px;margin:0 0 4px}}
.muted{{color:#8b98ab;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:22px 0}}
.card{{background:#11161f;border:1px solid #1f2937;border-radius:10px;padding:14px}}
.k{{font-size:11px;color:#8b98ab;text-transform:uppercase;letter-spacing:1px}}
.v{{font-size:20px;font-weight:700;margin-top:4px}}
.vstat{{border:1px solid #1f2937;border-radius:10px;padding:14px;margin:18px 0}}
.cta{{margin-top:18px}}
.btn{{display:inline-block;background:#5eead4;color:#000;font-weight:700;padding:9px 18px;border-radius:8px;margin-right:8px}}
.btn.o{{background:none;border:1px solid #5eead4;color:#5eead4;font-weight:600}}
#st{{font-weight:700}}
</style></head>
<body>
<p class="muted"><a href="/">← PayCensus — the census of the machine economy</a> · seller profile · as of {as_of}</p>
<h1>{domain}</h1>
<p class="muted">Machine-payment seller · attributed via on-chain payTo address <span class="mono">{addr_short}</span>…</p>
<div class="grid">
  <div class="card"><div class="k">Settlements</div><div class="v">{txs}</div></div>
  <div class="card"><div class="k">Volume (USDC)</div><div class="v">{vol_usd}</div></div>
  <div class="card"><div class="k">First seen</div><div class="v" style="font-size:14px">{first_day}</div></div>
  <div class="card"><div class="k">Last seen</div><div class="v" style="font-size:14px">{last_day}</div></div>
</div>
<div class="vstat">
  <div class="k">Verification status</div>
  <div id="st" class="muted">checking live…</div>
  <p class="muted" style="margin:6px 0 0">Status is checked live from the PayCensus Trust registry on every page view — DNS domain-control proof, re-checked every 72h, publicly revocable.</p>
</div>
<div class="cta">
  <a class="btn" href="https://trust.paycensus.com/verify.html">Verify this domain — free</a>
  <a class="btn o" href="/directory.html">Browse all sellers</a>
</div>
<p class="muted" style="margin-top:22px">Are you the owner of {domain}? Claim this profile free — get the PayCensus Verified badge, a public registry entry, and prove to AI agents that payments to your address are yours.</p>
<script>
fetch("https://trust.paycensus.com/api/status?addr={addr}").then(r=>r.json()).then(d=>{{
  const el=document.getElementById("st");
  if(d.status==="verified"){{el.textContent="✓ VERIFIED — domain control proven (since "+d.since+")";el.style.color="#4caf50";}}
  else if(d.status==="pending"){{el.textContent="◌ verification pending";el.style.color="#ffb300";}}
  else{{el.textContent="✗ NOT VERIFIED — buyers cannot prove payments reach this seller";el.style.color="#ef5350";}}
}}).catch(()=>{{document.getElementById("st").textContent="status unavailable";}});
</script>
<script src="https://paycensus.com/share.js" async></script>
</body></html>"""


def main():
    os.makedirs(OUT, exist_ok=True)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT e.addr, e.domain, e.title, s.txs, s.volume, s.first_day, "
        "s.last_day FROM entities e JOIN sellers s ON s.addr=e.addr "
        "WHERE e.domain IS NOT NULL AND e.domain!='' "
        "ORDER BY s.volume DESC").fetchall()
    conn.close()

    made = skipped = 0
    urls = []
    for r in rows:
        addr = r["addr"]
        path = os.path.join(OUT, addr + ".html")
        if os.path.exists(path):
            skipped += 1
        else:
            dom = r["domain"]
            html = TPL.format(
                domain=dom,
                vol_usd=f"{(r['volume'] or 0):,.0f}",
                txs=f"{(r['txs'] or 0):,}",
                first_day=r["first_day"] or "—",
                last_day=r["last_day"] or "—",
                addr_short=addr[:10],
                addr=addr, as_of=AS_OF)
            with open(path, "w") as f:
                f.write(html)
            made += 1
        urls.append(f"https://paycensus.com/s/{addr}.html")

    urls.sort()
    with open(SITEMAP, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for u in urls:
            f.write(f"  <url><loc>{u}</loc></url>\n")
        f.write("</urlset>\n")

    print(f"{AS_OF} seller pages: made {made}, skipped existing {skipped}, "
          f"total {len(urls)} -> sitemap-sellers.xml")


if __name__ == "__main__":
    main()
