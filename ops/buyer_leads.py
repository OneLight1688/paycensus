#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Buyer-side intelligence pipeline (Gate GTM prep) — v0.26.0.

Buyers of machine payments = enterprises/dev-teams running purchasing
agents — the exact ICP for Gate (agent spend governance). This pipeline
collects, attributes, cleans and stores buyer intelligence:

  Stage A  DB-only: score + tier every buyer wallet (spend, merchant
           diversity from edges, recency). No network.
  Stage B  paced network: ENS reverse lookup + GitHub code search for
           top unattributed buyers (resumable; caps per run).
  Stage C  contact discovery: GitHub profile (email/blog) of the owner,
           role-email scrape on attributed domains.
  Stage D  清洗闸: coldmail_pool.clean() verdict for every new email.

Storage: census.db buyer_enrich (internal; emails NEVER published or
sold). Public output /data/buyer_leads.json contains aggregates and
public on-chain facts only — no emails, no contact rows.

Run: python3 buyer_leads.py [ens_cap] [gh_cap] [scrape_cap]   (cron daily)
"""
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
OUT = "/www/wwwroot/paycensus/data/buyer_leads.json"
TOKEN_FILE = "/opt/paycensus/.github_token"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126",
      "Accept": "application/json"}

ENS_CAP = int(sys.argv[1]) if len(sys.argv) > 1 else 400
GH_CAP = int(sys.argv[2]) if len(sys.argv) > 2 else 60
SCRAPE_CAP = int(sys.argv[3]) if len(sys.argv) > 3 else 30
ENS_GAP = 0.6

ROLE_LOCALS = {"info", "hello", "contact", "support", "team", "admin",
               "sales", "business", "dev", "developer", "hi", "mail",
               "founders", "partnerships"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CONTACT_PATHS = ["", "/contact", "/about", "/team", "/contact.html",
                 "/about.html", "/impressum"]


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg):
    print("%s %s" % (ts(), msg), flush=True)


def con():
    c = sqlite3.connect(DB, timeout=120)
    c.row_factory = sqlite3.Row
    return c


# ---------------- Stage A: score + tier (DB only) ----------------
def init_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS buyer_enrich(
        addr TEXT PRIMARY KEY, ens TEXT, gh_owner TEXT, gh_repo TEXT,
        domain TEXT, email TEXT, email_cls TEXT, clean_verdict TEXT,
        tier TEXT, score REAL, merchants INTEGER, volume REAL,
        txs INTEGER, first_day TEXT, last_day TEXT, updated_at TEXT)""")


def stage_a(conn):
    today_minus6 = conn.execute("SELECT date('now','-6 day')").fetchone()[0]
    # set-based: one pass over edges for merchant diversity (no per-row query)
    mer = dict(conn.execute(
        "SELECT buyer, COUNT(DISTINCT seller) FROM edges GROUP BY buyer")
        .fetchall())
    rows = conn.execute(
        "SELECT b.addr, b.volume, b.txs, b.first_day, b.last_day, "
        "COALESCE(e.domain,'') edom FROM buyers b "
        "LEFT JOIN buyer_enrich be ON be.addr = b.addr "
        "LEFT JOIN entities e ON e.addr = b.addr "
        "WHERE be.addr IS NULL").fetchall()
    ins = []
    for r in rows:
        m = mer.get(r["addr"], 0)
        vol = float(r["volume"] or 0)
        if vol >= 10000:
            tier = "whale"
        elif vol >= 100 and r["last_day"] >= today_minus6:
            tier = "active"
        elif vol >= 1:
            tier = "growth"
        else:
            tier = "noise"
        score = round(min(100, (vol / 1000.0) + m * 2
                          + (10 if r["last_day"] >= today_minus6 else 0)), 1)
        ins.append((r["addr"], None, None, None, r["edom"] or None, None,
                    None, None, tier, score, m, vol, r["txs"],
                    r["first_day"], r["last_day"], ts()))
    conn.executemany(
        "INSERT OR REPLACE INTO buyer_enrich(addr, ens, gh_owner, gh_repo,"
        " domain, email, email_cls, clean_verdict, tier, score, merchants,"
        " volume, txs, first_day, last_day, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ins)
    conn.commit()
    log("stage A: %d new buyer rows scored (total %d)"
        % (len(ins), conn.execute("SELECT COUNT(*) FROM buyer_enrich")
           .fetchone()[0]))
    return len(ins)


# ---------------- Stage B: ENS + GitHub attribution (paced) ----------------
_ens_next = [0.0]


def _ens_slot():
    while True:
        now = time.time()
        if now >= _ens_next[0]:
            _ens_next[0] = now + ENS_GAP
            return
        time.sleep(_ens_next[0] - now)


def _get_json(url, timeout=12):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode(errors="replace"))


def ens_reverse(addr):
    """'' = checked-no-name, ERR* = source trouble."""
    for attempt in range(2):
        _ens_slot()
        try:
            d = _get_json("https://enstate.rs/u/" + addr)
            return (d.get("name") or "").strip().lower()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pass
            elif e.code in (429, 403):
                if attempt == 1:
                    return "ERR429"
                time.sleep(25)
                continue
            else:
                return "ERR%d" % e.code
        except Exception:  # noqa: BLE001
            if attempt == 1:
                return "ERRNET"
            time.sleep(5)
    try:
        _ens_slot()
        d = _get_json("https://api.ensideas.com/ens/resolve/" + addr)
        return (d.get("name") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return "ERRSRC"


def gh_search(tok, addr):
    q = urllib.request.quote('"%s"' % addr)
    r = urllib.request.Request(
        "https://api.github.com/search/code?q=%s&per_page=5" % q,
        headers={"Authorization": "Bearer " + tok,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "paycensus-attribution"})
    with urllib.request.urlopen(r, timeout=25) as resp:
        d = json.loads(resp.read().decode(errors="replace"))
    owners = {}
    for it in d.get("items", [])[:5]:
        full = (it.get("repository") or {}).get("full_name")
        if full and "/" in full:
            owners[full.split("/")[0]] = owners.get(full.split("/")[0], 0) + 1
    if not owners:
        return "", ""
    owner = max(owners, key=owners.get)
    return owner, owner  # repo owner as first-pass attribution


def stage_b(conn):
    tok = None
    try:
        tok = open(TOKEN_FILE).read().strip()
    except OSError:
        log("stage B: no github token — ENS only")
    rows = conn.execute(
        "SELECT b.addr, b.volume FROM buyers b JOIN buyer_enrich be "
        "ON be.addr = b.addr WHERE be.ens IS NULL "
        "AND (be.gh_owner IS NULL) ORDER BY be.volume DESC LIMIT ?",
        (max(ENS_CAP, GH_CAP),)).fetchall()
    n = e_ok = g_ok = 0
    for r in rows:
        addr = r["addr"]
        cur = conn.execute(
            "SELECT ens, gh_owner FROM buyer_enrich WHERE addr=?",
            (addr,)).fetchone()
        if cur["ens"] is None and n < ENS_CAP:
            ens = ens_reverse(addr)
            conn.execute("UPDATE buyer_enrich SET ens=?, updated_at=? "
                         "WHERE addr=?", (ens, ts(), addr))
            n += 1
            if ens and not ens.startswith("ERR") and "." not in ens:
                e_ok += 1
            time.sleep(0.1)
        if tok and cur["gh_owner"] is None and g_ok < GH_CAP:
            try:
                owner, repo = gh_search(tok, addr)
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    log("  gh rate limited, sleep 65s")
                    time.sleep(65)
                    try:
                        owner, repo = gh_search(tok, addr)
                    except Exception:  # noqa: BLE001
                        owner, repo = "", ""
                else:
                    owner, repo = "", ""
            except Exception:  # noqa: BLE001
                owner, repo = "", ""
            conn.execute("UPDATE buyer_enrich SET gh_owner=?, gh_repo=?, "
                         "updated_at=? WHERE addr=?",
                         (owner or "", "", ts(), addr))
            g_ok += 1
            time.sleep(8)
        conn.commit()
    log("stage B: ens checked %d (named %d), gh checked %d"
        % (n, e_ok, g_ok))


# ---------------- Stage C: contact discovery ----------------
def _http_text(url, timeout=10):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read().decode(errors="replace")[:200000]


def gh_profile(tok, owner):
    r = urllib.request.Request(
        "https://api.github.com/users/" + owner,
        headers={"Authorization": "Bearer " + tok,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "paycensus-attribution"})
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read().decode(errors="replace"))


def scrape_emails(domain):
    """role emails found on the site (contact pages). ''-safe."""
    found = []
    base = domain if domain.startswith("http") else "https://" + domain
    base = base.rstrip("/")
    for path in CONTACT_PATHS:
        try:
            html = _http_text(base + path)
        except Exception:  # noqa: BLE001
            continue
        for m in EMAIL_RE.findall(html):
            loc = m.split("@")[0].lower()
            if loc in ROLE_LOCALS:
                found.append(m.lower())
        time.sleep(0.4)
        if found:
            break
    return sorted(set(found))


def stage_c(conn, tok):
    rows = conn.execute(
        "SELECT addr, gh_owner, ens, domain, email FROM buyer_enrich "
        "WHERE email IS NULL AND (gh_owner != '' OR ens != '') "
        "AND (gh_owner IS NOT NULL OR ens IS NOT NULL) "
        "ORDER BY score DESC LIMIT ?", (SCRAPE_CAP,)).fetchall()
    n = e_found = 0
    for r in rows:
        email = None
        cls = None
        domain = None
        if r["gh_owner"]:
            try:
                p = gh_profile(tok, r["gh_owner"])
                email = (p.get("email") or "").strip().lower() or None
                blog = (p.get("blog") or "").strip()
                if blog and not domain:
                    d = blog.replace("http://", "").replace("https://", "")
                    d = d.split("/")[0].strip().lower()
                    if "." in d and "github" not in d:
                        domain = d
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
        if r["ens"] and not r["ens"].startswith("ERR") and "." in r["ens"]:
            domain = domain or r["ens"]
        if not email and domain:
            ems = scrape_emails(domain)
            if ems:
                email = ems[0]
        if email:
            loc = email.split("@")[0].lower()
            cls = "role" if loc in ROLE_LOCALS else "unknown"
            e_found += 1
        conn.execute(
            "UPDATE buyer_enrich SET email=?, email_cls=?, domain=?, "
            "updated_at=? WHERE addr=?",
            (email, cls, domain, ts(), r["addr"]))
        conn.commit()
        n += 1
    log("stage C: %d buyers enriched, %d emails found" % (n, e_found))


# ---------------- Stage D: 清洗闸 ----------------
def stage_d(conn):
    sys.path.insert(0, "/opt/paycensus")
    from coldmail_pool import clean
    rows = conn.execute(
        "SELECT addr, email FROM buyer_enrich WHERE email IS NOT NULL "
        "AND clean_verdict IS NULL").fetchall()
    if not rows:
        log("stage D: nothing to clean")
        return
    verdicts = clean([r["email"] for r in rows])
    n = 0
    for r in rows:
        v = (verdicts.get("verdicts") or {}).get(r["email"].lower())
        if v:
            conn.execute("UPDATE buyer_enrich SET clean_verdict=?, "
                         "updated_at=? WHERE addr=?", (v, ts(), r["addr"]))
            n += 1
    conn.commit()
    log("stage D: %d/%d emails cleaned" % (n, len(rows)))


# ---------------- public aggregate (NO emails) ----------------
def publish(conn):
    tot = conn.execute("SELECT COUNT(*) FROM buyer_enrich").fetchone()[0]
    tiers = {t: c for t, c in conn.execute(
        "SELECT tier, COUNT(*) FROM buyer_enrich GROUP BY tier")}
    attributed = conn.execute(
        "SELECT COUNT(*) FROM buyer_enrich "
        "WHERE (ens != '' AND ens IS NOT NULL AND ens NOT LIKE 'ERR%') "
        "OR (gh_owner != '' AND gh_owner IS NOT NULL)").fetchone()[0]
    with_email = conn.execute(
        "SELECT COUNT(*) FROM buyer_enrich WHERE email IS NOT NULL"
    ).fetchone()[0]
    gate_ready = conn.execute(
        "SELECT COUNT(*) FROM buyer_enrich WHERE email_cls='role' "
        "AND clean_verdict='ok' AND merchants >= 2").fetchone()[0]
    top = [dict(r) for r in conn.execute(
        "SELECT addr, ens, gh_owner, tier, score, merchants, volume, txs, "
        "last_day FROM buyer_enrich WHERE volume >= 100 "
        "ORDER BY volume DESC LIMIT 30")]
    pub = {
        "generated_at": ts(),
        "note": "Buyer-side intelligence for agent spend governance (Gate) "
                "go-to-market. Buyers of machine payments = teams running "
                "purchasing agents. Aggregate + public on-chain facts only; "
                "contact details are internal-only (never published or sold).",
        "totals": {"buyers_scored": tot, "attributed": attributed,
                   "with_contact_internal": with_email,
                   "gate_outreach_ready": gate_ready},
        "tiers": tiers,
        "top_buyers": top,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pub, f)
    os.replace(tmp, OUT)
    log("publish: %d scored, %d attributed, %d gate-ready"
        % (tot, attributed, gate_ready))


def main():
    conn = con()
    init_tables(conn)
    stage_a(conn)
    stage_b(conn)
    tok = None
    try:
        tok = open(TOKEN_FILE).read().strip()
    except OSError:
        pass
    stage_c(conn, tok)
    stage_d(conn)
    publish(conn)
    conn.close()


if __name__ == "__main__":
    main()
