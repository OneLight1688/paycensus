# -*- coding: utf-8 -*-
"""PayCensus Trust API v0.2.0 — seller/counterparty verification for machine payments.

Fully deterministic verification (no human per-case judgment):
  verify/start  -> one-time nonce, 24-48h expiry
  verify/check  -> DNS TXT via 3 public resolvers, >=2/3 must match
  status        -> public, CORS-open (badge widget live lookup)
  report        -> triggers automatic re-examination (never a human verdict)
  revocations   -> public append-only list
Cron reverify: 72h rolling window, 3 consecutive failures -> auto-revoke.

v0.2.0 — payments & metering:
  founding      -> public founding-slot counter ($99/yr first 100, locked for life)
  checkout/start-> Stripe Checkout subscription (auto-invoice); graceful 503 until keys
  stripe/webhook-> signature-verified (HMAC-SHA256, 5-min replay guard); auto state
  keys/create   -> Trust API key (pct_live_, stored hashed)
  factors       -> metered factor lookup (10 calls/day free per key)
  usage         -> per-key usage today
  email alerts  -> verified/revoked transitions to the seller's contact address
"""
import hashlib
import hmac
import html as _html
import json
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import ssl
import struct
import sys
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = "/var/lib/paycensus/trust.db"
VERSION = "0.19.0"
LOGO_PATH = "/opt/paycensus/logo_avatar.png"
LOGO_CID = "paycensus-logo"
CENSUS_DB = "/var/lib/paycensus/census.db"
ANCHORING_JSON = "/www/wwwroot/paycensus/data/anchoring.json"
ANCHOR_SNAP_DIR = "/var/lib/paycensus/anchoring"
DIRECTORY_JSON = "/www/wwwroot/paycensus/data/directory.json"
DIRECTORY_TTL = 1800  # seconds; rebuilt hourly by directory_build.py
RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
TXT_HOST_PREFIX = "_paycensus"
REVERIFY_HOURS = 72
FAILS_TO_REVOKE = 3
NONCE_TTL_HOURS = 48

FOUNDING_TOTAL = 100
FOUNDING_PRICE_CENTS = 9900
LIST_PRICE_CENTS = 19900
FREE_CALLS_DAY = 10
AGENT_CALLS_FREE = 1000
STRIPE_API = "https://api.stripe.com/v1"
STRIPE_CFG = "/opt/paycensus/.trust_stripe"
SMTP_CRED = "/opt/paycensus/.smtp_trust"  # trust@ — transactional mail on its own identity
MAIL_FROM = "trust@paycensus.com"

ADDR_RE = re.compile(r"^(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def utcnow():
    return datetime.now(timezone.utc)


def ts(dt=None):
    return (dt or utcnow()).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------- config & email ----------

def load_cfg(path):
    cfg = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


SOCIAL_SITE = "https://paycensus.com"
SOCIAL_X = "https://x.com/PayCensusLabs"
SOCIAL_GITHUB = "https://github.com/OneLight1688/paycensus"
SOCIAL_TAGLINE = "PayCensus — The census of the machine economy"
_BTN = ('<a href="{url}" style="display:inline-block;margin:4px 6px 0 0;'
        'padding:7px 14px;border-radius:6px;background:{bg};color:#ffffff;'
        'font:600 13px/1.4 -apple-system,Segoe UI,Arial,sans-serif;'
        'text-decoration:none">{label}</a>')


def _share_links():
    """Share-intent URLs for the mail footer."""
    q = urllib.parse.quote
    return {
        "x": ("https://twitter.com/intent/tweet?text=" + q(SOCIAL_TAGLINE)
              + "&url=" + q(SOCIAL_SITE)),
        "linkedin": "https://www.linkedin.com/sharing/share-offsite/?url=" + q(SOCIAL_SITE),
        "telegram": ("https://t.me/share/url?url=" + q(SOCIAL_SITE)
                     + "&text=" + q(SOCIAL_TAGLINE)),
        "facebook": "https://www.facebook.com/sharer/sharer.php?u=" + q(SOCIAL_SITE),
    }


def _mail_footer_text():
    s = _share_links()
    return ("\n\n----\n" + SOCIAL_TAGLINE + "\n"
            "Follow: " + SOCIAL_X + "  ·  " + SOCIAL_GITHUB + "\n"
            "Share: " + s["x"] + "\n")


def _mail_html(body):
    """Branded HTML body (CID-embedded logo) + social follow/share buttons
    (inline styles only, no remote images — email-client safe)."""
    s = _share_links()
    buttons = (
        _BTN.format(url=SOCIAL_X, bg="#000000", label="Follow on X")
        + _BTN.format(url=SOCIAL_GITHUB, bg="#24292f", label="GitHub")
        + _BTN.format(url=SOCIAL_SITE, bg="#F6821F", label="paycensus.com")
        + "<br>"
        + _BTN.format(url=s["x"], bg="#3a3f4a", label="Share on X")
        + _BTN.format(url=s["linkedin"], bg="#0A66C2", label="LinkedIn")
        + _BTN.format(url=s["telegram"], bg="#229ED9", label="Telegram")
        + _BTN.format(url=s["facebook"], bg="#1877F2", label="Facebook")
    )
    logo_html = ("<img src='cid:" + LOGO_CID + "' alt='PayCensus' width='56' height='56'"
                 " style='width:56px;height:56px;border-radius:12px;"
                 "display:block;margin-bottom:14px;'>")
    esc = _html.escape(body).replace("\n", "<br>")
    return ("<!doctype html><html><body style='margin:0;background:#f4f5f7;'>"
            "<div style='max-width:560px;margin:0 auto;padding:24px;"
            "font:15px/1.6 -apple-system,Segoe UI,Arial,sans-serif;color:#1d212a;'>"
            + logo_html + esc +
            "<hr style='border:none;border-top:1px solid #e2e4e9;margin:22px 0 14px;'>"
            "<div style='font:600 13px/1.4 -apple-system,Segoe UI,Arial,sans-serif;"
            "color:#1d212a;margin-bottom:2px;'>PayCensus</div>"
            "<div style='font:12px/1.5 -apple-system,Segoe UI,Arial,sans-serif;"
            "color:#9aa0aa;margin-bottom:10px;'>The census of the machine economy"
            " · Bitcoin-anchored daily</div>"
            + buttons +
            "<div style='font:11px/1.5 -apple-system,Segoe UI,Arial,sans-serif;"
            "color:#9aa0aa;margin-top:14px;'>Sent by PayCensus Labs · "
            "deterministic, fully automated seller verification</div>"
            "</div></body></html>")


def send_mail(to, subject, body, from_addr=None, cred_path=None):
    """Best-effort transactional mail via SMTP. Never raises.
    Multipart/alternative: plain-text + branded HTML footer with social
    follow/share buttons — every customer mail gets them automatically.
    Account-flow mails override sender via from_addr/cred_path (hello@)."""
    try:
        addr = from_addr or MAIL_FROM
        display = "PayCensus" if from_addr else "PayCensus Trust"
        cred = open(cred_path or SMTP_CRED).read().strip()
        user, _, pwd = cred.partition(":")
        m = MIMEMultipart("alternative")
        m["From"] = f"{display} <{addr}>"
        m["To"] = to
        m["Subject"] = subject
        m.attach(MIMEText(body + _mail_footer_text(), "plain", "utf-8"))
        m.attach(MIMEText(_mail_html(body), "html", "utf-8"))
        if os.path.exists(LOGO_PATH):
            img = MIMEImage(open(LOGO_PATH, "rb").read(), _subtype="png")
            img.add_header("Content-ID", "<" + LOGO_CID + ">")
            img.add_header("Content-Disposition", "inline",
                           filename="paycensus-logo.png")
            m.attach(img)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.qiye.aliyun.com", 465, context=ctx, timeout=30) as s:
            s.login(user, pwd)
            s.sendmail(user, [to], m.as_string().encode("utf-8"))
        print(f"{ts()} mail sent to {to}: {subject}")
        return True
    except Exception as ex:  # noqa: BLE001
        print(f"{ts()} mail FAIL to {to}: {str(ex)[:120]}")
        return False


# ---------- minimal DNS TXT resolver (stdlib only; EDNS0 + TCP fallback) ----------

def _build_query(name, qtype=16):
    tid = secrets.randbits(16)
    q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 1)  # ARCOUNT=1 (EDNS0 OPT)
    q += b"".join(bytes([len(l)]) + l.encode() for l in name.split(".")) + b"\x00"
    q += struct.pack(">HH", qtype, 1)
    q += b"\x00" + struct.pack(">HHIH", 41, 4096, 0, 0)  # OPT: 4096-byte buffer
    return tid, q


def _parse_txt(data, tid):
    """returns list of TXT strings; None = unparseable/id mismatch."""
    if len(data) < 12:
        return None
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if rid != tid:
        return None
    if (flags & 0xF) != 0:  # non-zero rcode -> authoritative "no records"
        return []
    off = 12

    def skip_name(o):
        while True:
            ln = data[o]
            if ln == 0:
                return o + 1
            if ln & 0xC0 == 0xC0:
                return o + 2
            o += 1 + ln

    try:
        for _ in range(qd):
            off = skip_name(off) + 4
        out = []
        for _ in range(an):
            off = skip_name(off)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
            off += 10
            rd = data[off:off + rdlen]
            if rtype == 16:
                i, parts = 0, []
                while i < len(rd):
                    ln = rd[i]
                    parts.append(rd[i + 1:i + 1 + ln].decode("utf-8", "replace"))
                    i += 1 + ln
                out.append("".join(parts))
            off += rdlen
    except (IndexError, struct.error):
        return None
    return out


def _dns_query(name, server, qtype=16, timeout=4):
    tid, q = _build_query(name, qtype)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(q, (server, 53))
        data, _ = sock.recvfrom(65535)
    finally:
        sock.close()
    txts = _parse_txt(data, tid)
    if txts is not None:
        flags = struct.unpack(">H", data[2:4])[0]
        if not ((flags >> 9) & 1):  # not truncated
            return txts
    # truncated or unparseable over UDP -> TCP retry (length-prefixed)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((server, 53))
        sock.sendall(struct.pack(">H", len(q)) + q)
        hdr = b""
        while len(hdr) < 2:
            c = sock.recv(2 - len(hdr))
            if not c:
                return txts or []
            hdr += c
        ln = struct.unpack(">H", hdr)[0]
        data = b""
        while len(data) < ln:
            chunk = sock.recv(ln - len(data))
            if not chunk:
                break
            data += chunk
        sock.close()
        t2 = _parse_txt(data, tid)
        return t2 if t2 is not None else (txts or [])
    except Exception:
        return txts or []


def check_domain_nonce(domain, nonce):
    """Query _paycensus.<domain> TXT across RESOLVERS.

    Returns (state, per), state:
      pass         — >=2 conclusive resolvers carry the nonce
      fail         — >=2 conclusive resolvers answered WITHOUT the nonce
                     (record really removed/mismatched -> counts a strike)
      inconclusive — <2 resolvers answered (network loss on our side);
                     NEVER counts a strike: "could not query" != "record gone"
    Errored resolvers get one retry round before judging.
    """
    host = f"{TXT_HOST_PREFIX}.{domain}"

    def probe():
        out = {}
        for srv in RESOLVERS:
            try:
                txts = _dns_query(host, srv)
                out[srv] = "hit" if any(nonce in t for t in txts) else "miss"
            except Exception as ex:
                out[srv] = f"err:{str(ex)[:30]}"
        return out

    per = probe()
    if sum(1 for v in per.values() if v.startswith("err")) >= 2:
        time.sleep(3)
        for srv, v in probe().items():
            if not v.startswith("err"):
                per[srv] = v
    hits = sum(1 for v in per.values() if v == "hit")
    conclusive = sum(1 for v in per.values() if not v.startswith("err"))
    if hits >= 2:
        state = "pass"
    elif conclusive - hits >= 2:
        state = "fail"
    else:
        state = "inconclusive"
    return state, per


# ---------- storage ----------

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS verifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        addr TEXT NOT NULL, domain TEXT NOT NULL, nonce TEXT NOT NULL,
        contact TEXT, status TEXT DEFAULT 'pending',
        created_at TEXT, expires_at TEXT, verified_at TEXT,
        last_checked TEXT, fail_count INTEGER DEFAULT 0,
        checks_pass INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS revocations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, vid INTEGER, revoked_at TEXT,
        reason TEXT, reinstated_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT, vid INTEGER, contact TEXT,
        message TEXT, created_at TEXT, outcome TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vid INTEGER, addr TEXT, domain TEXT,
        plan TEXT DEFAULT 'verified_yearly',
        founding INTEGER DEFAULT 0, price_cents INTEGER,
        status TEXT DEFAULT 'incomplete',
        stripe_session_id TEXT, stripe_customer_id TEXT,
        stripe_subscription_id TEXT, current_period_end TEXT,
        contact TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS api_keys(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_hash TEXT UNIQUE NOT NULL, label TEXT, plan TEXT DEFAULT 'free',
        created_at TEXT, last_used TEXT, revoked INTEGER DEFAULT 0,
        owner TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS account_plans(
        email TEXT PRIMARY KEY,
        plan TEXT, status TEXT DEFAULT 'incomplete',
        stripe_session_id TEXT, stripe_customer_id TEXT,
        stripe_subscription_id TEXT, current_period_end TEXT,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS api_usage(
        day TEXT, key_id INTEGER, calls INTEGER DEFAULT 0,
        PRIMARY KEY(day, key_id))""")
    for _col in ("stripe_invoice_id TEXT", "stripe_payment_intent TEXT"):
        try:
            c.execute(f"ALTER TABLE subscriptions ADD COLUMN {_col}")
        except sqlite3.OperationalError:
            pass  # column already exists
    try:
        c.execute("ALTER TABLE api_keys ADD COLUMN owner TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS account_auth(
        email TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
        created_at TEXT, last_login TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS email_codes(
        email TEXT, code TEXT, purpose TEXT, expires_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS customer_invoices(
        invoice_id TEXT PRIMARY KEY, stripe_customer_id TEXT,
        hosted_invoice_url TEXT, amount_paid_usd REAL, currency TEXT,
        status TEXT, created TEXT)""")
    try:
        c.execute("ALTER TABLE customer_invoices ADD COLUMN customer_email TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS api_log(
        ts TEXT, key_id INTEGER, endpoint TEXT, ip TEXT)""")
    return c


def row_pub(r):
    return {"addr": r["addr"], "domain": r["domain"], "status": r["status"],
            "verified_at": r["verified_at"], "last_checked": r["last_checked"],
            "since": (r["verified_at"] or "")[:10] or None}


# ---------- verification core ----------

def run_check(conn, vid):
    r = conn.execute("SELECT * FROM verifications WHERE id=?", (vid,)).fetchone()
    if not r:
        return None
    now = ts()
    if r["status"] == "revoked":
        # reinstatement path: a revoked record may re-verify only via a fresh nonce
        return row_pub(r)
    # Token expiry only gates PENDING verifications. A verified record's
    # health is measured solely by the CURRENT presence of its nonce in DNS
    # (checked every 72h) — its original token expiring must never strike it.
    expired = (r["status"] != "verified") and r["expires_at"] and utcnow() > datetime.strptime(
        r["expires_at"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    state, per = check_domain_nonce(r["domain"], r["nonce"])
    if state == "pass" and not expired:
        conn.execute(
            "UPDATE verifications SET status='verified', verified_at=COALESCE("
            "verified_at,?), last_checked=?, fail_count=0, checks_pass=checks_pass+1 "
            "WHERE id=?", (now, now, vid))
        conn.commit()
        if r["status"] != "verified" and r["contact"]:
            send_mail(r["contact"], "PayCensus Trust: your domain is Verified",
                      f"{r['domain']} passed deterministic domain-control "
                      f"verification ({now}).\n\n"
                      "Your record now shows: \"Domain control verified "
                      f"{now[:10]}\".\n"
                      "Embed the live badge from https://trust.paycensus.com/\n"
                      "Re-verification runs every 72h; 3 consecutive failures "
                      "auto-revoke (public list). No human is involved.\n\n"
                      "— PayCensus Trust (automated mail, replies not monitored)")
        return {"ok": True, "status": "verified", "since":
                (r["verified_at"] or now)[:10], "resolvers": per}
    if state == "inconclusive":
        # network-side failure: refresh last_checked, no strike, no mail
        conn.execute("UPDATE verifications SET last_checked=? WHERE id=?",
                     (now, vid))
        conn.commit()
        return {"ok": None, "status": r["status"],
                "fail_count": r["fail_count"], "resolvers": per,
                "note": "DNS resolvers unreachable — inconclusive, "
                        "no strike counted"}
    fc = r["fail_count"] + 1
    conn.execute("UPDATE verifications SET last_checked=?, fail_count=? WHERE id=?",
                 (now, fc, vid))
    if (r["status"] == "verified" and fc < FAILS_TO_REVOKE and r["contact"]):
        send_mail(r["contact"], "PayCensus Trust: re-verification failed — "
                  f"{FAILS_TO_REVOKE - fc} strike"
                  + ("s" if FAILS_TO_REVOKE - fc > 1 else "") + " left",
                  f"{r['domain']} failed its rolling re-verification at {now}.\n\n"
                  f"Strike {fc} of {FAILS_TO_REVOKE}. At {FAILS_TO_REVOKE} "
                  "consecutive failures the record is auto-revoked onto the "
                  "public list (https://trust.paycensus.com/revocations.html).\n"
                  "Most common cause: the TXT record "
                  f"({TXT_HOST_PREFIX}.{r['domain']}) was removed or the nonce "
                  "expired. Keep it in place; the next check passes and the "
                  "counter resets to zero.\n\n"
                  "— PayCensus Trust (automated mail, replies not monitored)")
    if r["status"] == "verified" and fc >= FAILS_TO_REVOKE:
        conn.execute("UPDATE verifications SET status='revoked' WHERE id=?", (vid,))
        conn.execute("INSERT INTO revocations(vid,revoked_at,reason) VALUES(?,?,?)",
                     (vid, now, f"reverify failed {fc} consecutive times"))
        conn.commit()
        if r["contact"]:
            send_mail(r["contact"], "PayCensus Trust: verification REVOKED",
                      f"{r['domain']} failed re-verification {fc} consecutive "
                      f"times and was auto-revoked at {now}.\n\n"
                      "The revocation is public and append-only:\n"
                      "https://trust.paycensus.com/revocations.html\n"
                      "To reinstate: start a fresh verification at "
                      "https://trust.paycensus.com/verify.html and pass once.\n\n"
                      "— PayCensus Trust (automated mail, replies not monitored)")
        return {"ok": False, "status": "revoked", "fail_count": fc, "resolvers": per}
    conn.commit()
    return {"ok": state == "pass", "status": r["status"], "fail_count": fc,
            "resolvers": per}


def reverify_all():
    """Cron entry: re-check verified records whose last_checked is older than 72h."""
    conn = db()
    cutoff = ts(utcnow() - timedelta(hours=REVERIFY_HOURS))
    rows = conn.execute(
        "SELECT id FROM verifications WHERE status='verified' AND "
        "(last_checked IS NULL OR last_checked < ?) ORDER BY last_checked",
        (cutoff,)).fetchall()
    out = []
    for r in rows:
        out.append(run_check(conn, r["id"]))
    n = len(rows)
    print(f"{ts()} reverify: {n} checked, "
          f"{sum(1 for o in out if o and o.get('ok') is False)} failed, "
          f"{sum(1 for o in out if o and o.get('ok') is None)} inconclusive")
    conn.close()
    return n


# ---------- payments (Stripe, stdlib only) ----------

def stripe_post(path, params):
    cfg = load_cfg(STRIPE_CFG)
    key = cfg.get("STRIPE_SECRET_KEY")
    if not key:
        return None
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(STRIPE_API + path, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as ex:  # noqa: BLE001
        print(f"{ts()} stripe {path} FAIL: {str(ex)[:200]}")
        return None


def stripe_get(path):
    """Returns (status, obj) — status None means no key / transport error."""
    cfg = load_cfg(STRIPE_CFG)
    key = cfg.get("STRIPE_SECRET_KEY")
    if not key:
        return None, {}
    req = urllib.request.Request(STRIPE_API + path)
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as ex:  # noqa: BLE001
        print(f"{ts()} stripe GET {path} FAIL: {str(ex)[:200]}")
        return None, {}


def webhook_sig_ok(sig_header, payload):
    """Stripe-Signature: t=...,v1=... — HMAC-SHA256 over '{t}.{payload}', 5-min window."""
    secret = load_cfg(STRIPE_CFG).get("STRIPE_WEBHOOK_SECRET")
    if not secret or not sig_header:
        return False
    t = None
    ok = False
    for part in sig_header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            t = v
        elif k == "v1" and t:
            mac = hmac.new(secret.encode(),
                           (t + ".").encode() + payload,
                           hashlib.sha256).hexdigest()
            ok = ok or hmac.compare_digest(mac, v)
    if not ok or t is None:
        return False
    try:
        return abs(time.time() - int(t)) <= 300
    except ValueError:
        return False


def founding_taken(conn):
    """Reservations (checkout started ≤48h ago, not yet paid) + active subs."""
    cutoff = ts(utcnow() - timedelta(hours=48))
    return conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE founding=1 AND "
        "(status='active' OR (status='incomplete' AND created_at > ?))",
        (cutoff,)).fetchone()[0]


def handle_webhook(conn, payload):
    try:
        event = json.loads(payload)
    except ValueError:
        return 400
    etype = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    if etype == "checkout.session.completed":
        sess_id = obj.get("id") or ""
        row = conn.execute("SELECT * FROM subscriptions WHERE stripe_session_id=?",
                           (sess_id,)).fetchone()
        if row:
            sub_id = obj.get("subscription") or ""
            cust = obj.get("customer") or ""
            period_end = None
            if sub_id:
                _sc, sub = stripe_get("/subscriptions/" + sub_id)
                if sub and sub.get("current_period_end"):
                    period_end = ts(datetime.fromtimestamp(
                        sub["current_period_end"], timezone.utc))
            conn.execute(
                "UPDATE subscriptions SET status='active', stripe_subscription_id=?,"
                "stripe_customer_id=?, current_period_end=COALESCE(?,"
                "current_period_end) WHERE id=?",
                (sub_id, cust, period_end, row["id"]))
            conn.commit()
            if cust:
                # English-only customer experience: invoices, receipts, emails
                stripe_post("/customers/" + cust,
                            {"preferred_locales[0]": "en"})
            if row["contact"]:
                send_mail(row["contact"], "PayCensus Trust: Verified subscription active",
                          f"Payment received. {row['domain']} is on the Verified plan"
                          + (" at the FOUNDER price, locked for life."
                             if row["founding"] else ".") +
                          "\n\nInvoices are issued automatically by Stripe.\n"
                          "Badge: https://trust.paycensus.com/\n\n"
                          "— PayCensus Trust (automated mail)")
        else:
            # agent API-plan subscription (no vid): activate keys by email
            meta = obj.get("metadata") or {}
            if meta.get("acct") == "1" and meta.get("email"):
                email = meta["email"]
                cust = obj.get("customer") or ""
                sub_id = obj.get("subscription") or ""
                conn.execute(
                    "UPDATE account_plans SET status='active', "
                    "stripe_customer_id=?, stripe_subscription_id=? "
                    "WHERE email=?", (cust, sub_id, email))
                conn.execute("UPDATE api_keys SET plan='plus' WHERE owner=?",
                             (email,))
                conn.commit()
                send_mail(email, "PayCensus — API plan active",
                          "Your Verified+ (API) plan is active: your API keys "
                          "now have 100,000 factor calls/day.\n\n"
                          "Manage keys: https://trust.paycensus.com/account.html\n\n"
                          "— PayCensus Trust (automated mail)")
    elif etype == "invoice.paid":
        sub_id = ((obj.get("lines") or {}).get("data") or [{}])[0].get(
            "subscription") or obj.get("subscription") or ""
        if sub_id:
            period_end = None
            li = (obj.get("lines") or {}).get("data") or [{}]
            pe = li[0].get("period", {}).get("end")
            if pe:
                period_end = ts(datetime.fromtimestamp(pe, timezone.utc))
            conn.execute(
                "UPDATE subscriptions SET status='active', "
                "current_period_end=COALESCE(?, current_period_end), "
                "stripe_invoice_id=COALESCE(?, stripe_invoice_id), "
                "stripe_payment_intent=COALESCE(?, stripe_payment_intent) "
                "WHERE stripe_subscription_id=?",
                (period_end, obj.get("id"), obj.get("payment_intent"), sub_id))
            conn.execute(
                "INSERT INTO customer_invoices(invoice_id,stripe_customer_id,"
                "hosted_invoice_url,amount_paid_usd,currency,status,created,"
                "customer_email) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(invoice_id) "
                "DO UPDATE SET status=excluded.status",
                (obj.get("id"), obj.get("customer"), obj.get("hosted_invoice_url"),
                 obj.get("amount_paid", 0) / 100.0, obj.get("currency"),
                 obj.get("status"),
                 ts(datetime.fromtimestamp(obj.get("created", 0), timezone.utc)),
                 (obj.get("customer_email") or "")[:120]))
            if sub_id:
                conn.execute(
                    "UPDATE account_plans SET status='active', "
                    "current_period_end=COALESCE(?, current_period_end) "
                    "WHERE stripe_subscription_id=?", (period_end, sub_id))
            conn.commit()
    elif etype == "customer.subscription.deleted":
        conn.execute(
            "UPDATE subscriptions SET status='canceled' "
            "WHERE stripe_subscription_id=?", (obj.get("id") or "",))
        conn.execute(
            "UPDATE account_plans SET status='canceled' "
            "WHERE stripe_subscription_id=?", (obj.get("id") or "",))
        conn.execute(
            "UPDATE api_keys SET plan='free' WHERE owner IN "
            "(SELECT email FROM account_plans WHERE stripe_subscription_id=?)",
            (obj.get("id") or "",))
        conn.commit()
    return 200


# ---------- API keys & metering ----------

def _key_auth(self, conn):
    """Returns (key_row, json_error) — exactly one is None."""
    h = self.headers.get("Authorization") or ""
    if not h.startswith("Bearer pct_"):
        return None, (401, {"error": "API key required: Authorization: Bearer pct_..."})
    kh = hashlib.sha256(h[7:].encode()).hexdigest()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash=? AND revoked=0", (kh,)).fetchone()
    if not row:
        return None, (401, {"error": "unknown or revoked key"})
    return row, None


def _usage_today(conn, key_id, endpoint="-", ip="-"):
    day = utcnow().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO api_usage(day,key_id,calls) VALUES(?,?,1) "
        "ON CONFLICT(day,key_id) DO UPDATE SET calls=calls+1", (day, key_id))
    conn.execute("INSERT INTO api_log(ts,key_id,endpoint,ip) VALUES(?,?,?,?)",
                 (ts(), key_id, endpoint, ip))
    conn.commit()
    n = conn.execute("SELECT calls FROM api_usage WHERE day=? AND key_id=?",
                     (day, key_id)).fetchone()[0]
    conn.execute("UPDATE api_keys SET last_used=? WHERE id=?", (ts(), key_id))
    conn.commit()
    return n


# ---------- admin: config management & self-test ----------

CFG_KEYS = ["STRIPE_PUBLISH_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
            "STRIPE_PRICE_FOUNDING", "STRIPE_PRICE_LIST", "STRIPE_PRICE_PLUS",
            "TRUST_SELF_ADDRS"]
CFG_REQUIRED = ["STRIPE_PUBLISH_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                "STRIPE_PRICE_FOUNDING", "STRIPE_PRICE_LIST"]


def _stripe_probe_key(key):
    """Validate a restricted key against GET /v1/products?limit=1. Returns HTTP code."""
    req = urllib.request.Request(STRIPE_API + "/products?limit=1")
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return None


X_CRED = "/opt/paycensus/.x_credentials"
X_KEYS = ["api_key", "api_secret", "access_token", "access_secret"]


def x_masked():
    cfg = load_cfg(X_CRED)
    out = {}
    for k in X_KEYS:
        v = cfg.get(k, "")
        out["X_" + k] = (v[:8] + "…" + v[-4:]) if v else "(not set)"
    out["_file"] = "readable" if cfg else "missing/unreadable"
    return out


def x_write(updates):
    """Merge provided X credential fields, preserving unknown keys. (ok, error)."""
    cfg = load_cfg(X_CRED)
    for k, v in updates.items():
        if k in X_KEYS and v:
            cfg[k] = v.strip()
    try:
        lines = [f"{k}={cfg[k]}\n" for k in cfg]
        with open(X_CRED, "w") as f:
            f.write("".join(lines))
        os.chmod(X_CRED, 0o600)
        return True, ""
    except OSError as ex:
        return False, str(ex)[:200]


def x_selfcheck():
    """Format check + live verify_credentials via tweepy (pylibs) when possible."""
    cfg = load_cfg(X_CRED)
    missing = [k for k in X_KEYS if not cfg.get(k)]
    res = {"check": "x_credentials", "ok": not missing,
           "detail": "all 4 fields present" if not missing
                     else "missing: " + ",".join(missing)}
    if missing:
        return res
    try:
        sys.path.insert(0, "/opt/paycensus/pylibs")
        for m in [m for m in list(sys.modules) if m == "tweepy"]:
            del sys.modules[m]  # fresh import in case of redeploy
        import tweepy
        auth = tweepy.OAuth1UserHandler(cfg["api_key"], cfg["api_secret"],
                                        cfg["access_token"], cfg["access_secret"])
        api = tweepy.API(auth)
        me = api.verify_credentials()
        res["ok"] = True
        res["detail"] = f"live verify OK — @{me.screen_name}"
    except Exception as ex:  # noqa: BLE001
        res["ok"] = False
        res["detail"] = f"live verify failed: {str(ex)[:150]}"
    return res


OR_TOKEN = "/opt/paycensus/.openrouter_token"
OR_PLACEHOLDER = "PASTE-YOUR-OPENROUTER-TOKEN-HERE"


def or_masked():
    try:
        t = open(OR_TOKEN).read().strip()
    except OSError:
        t = ""
    if not t:
        return {"OR_token": "(not set)", "_file": "missing", "_configured": "no"}
    if t == OR_PLACEHOLDER:
        return {"OR_token": "(placeholder)", "_file": "readable",
                "_configured": "no"}
    return {"OR_token": t[:8] + "…" + t[-4:], "_file": "readable",
            "_configured": "yes"}


def or_write(token):
    """Save the OpenRouter token (0600). (ok, error)."""
    try:
        with open(OR_TOKEN, "w") as f:
            f.write(token.strip() + "\n")
        os.chmod(OR_TOKEN, 0o600)
        return True, ""
    except OSError as ex:
        return False, str(ex)[:200]


def or_selfcheck():
    """Live check against OpenRouter /api/v1/auth/key (free, no quota use)."""
    try:
        t = open(OR_TOKEN).read().strip()
    except OSError:
        t = ""
    if not t or t == OR_PLACEHOLDER:
        return {"check": "openrouter_token", "ok": False,
                "detail": "not configured yet (placeholder/missing) — "
                          "x_comment.py will skip until a real token is saved"}
    import urllib.request as _u
    try:
        req = _u.Request("https://openrouter.ai/api/v1/auth/key",
                         headers={"Authorization": "Bearer " + t})
        with _u.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        data = d.get("data") or {}
        usage = data.get("usage")
        lim = data.get("limit")
        return {"check": "openrouter_token", "ok": True,
                "detail": (f"live key OK — usage ${usage or 0}, "
                           f"limit {lim if lim is not None else 'none (free-tier)'}")}
    except Exception as ex:  # noqa: BLE001
        return {"check": "openrouter_token", "ok": False,
                "detail": f"live check failed: {str(ex)[:150]}"}


def indices_anchor_check():
    """BTC-anchoring self-check for the indices feed (MPI/TVI).

    Deterministic, all local:
      1. anchoring.json readable and non-empty
      2. latest anchor label <= 2 days old (stamp pipeline alive)
      3. indices.json included in that anchor's snapshot (files_bytes)
      4. snapshot file sha256 == recorded digest (integrity)
      5. OTS receipt present on disk
    """
    out = {"check": "indices_anchored", "ok": False}
    try:
        anchors = json.load(open(ANCHORING_JSON)).get("anchors") or []
        if not anchors:
            out["detail"] = "no anchors in anchoring.json"
            return out
        e = anchors[0]
        lbl = e.get("label", "")
        age = (datetime.now(timezone.utc).date()
               - datetime.strptime(lbl, "%Y-%m-%d").date()).days
        if age > 2:
            out["detail"] = f"latest anchor {lbl} is {age}d old — stamp pipeline stalled"
            return out
        ib = (e.get("files_bytes") or {}).get("indices.json")
        if ib is None:
            out["detail"] = f"anchor {lbl} does not include indices.json"
            return out
        snap = os.path.join(ANCHOR_SNAP_DIR, f"{lbl}.snapshot")
        if not os.path.exists(snap):
            out["detail"] = f"snapshot file missing: {snap}"
            return out
        dg = hashlib.sha256(open(snap, "rb").read()).hexdigest()
        if dg != e.get("digest"):
            out["detail"] = f"snapshot digest mismatch for {lbl} (integrity fail)"
            return out
        ots = os.path.join(ANCHOR_SNAP_DIR, f"{lbl}.snapshot.ots")
        out["ok"] = True
        out["detail"] = (f"anchor {lbl} ({e.get('status')}) includes indices.json "
                         f"({ib}B); digest verified {dg[:12]}…; receipt "
                         f"{'ok' if os.path.exists(ots) else 'missing'}")
    except Exception as ex:  # noqa: BLE001
        out["detail"] = str(ex)[:200]
    return out


# ---------- customer account portal ----------

ACCT_SECRET_FILE = "/opt/paycensus/.account_secret"
ACCT_TTL = 7 * 86400
_acct_cache = {"secret": None}
_code_limit = {}  # ip -> [ts]


def acct_secret():
    if _acct_cache["secret"]:
        return _acct_cache["secret"]
    try:
        s = open(ACCT_SECRET_FILE).read().strip()
        if len(s) >= 64:
            _acct_cache["secret"] = s
            return s
    except Exception:  # noqa: BLE001
        pass
    s = secrets.token_hex(48)
    try:
        with open(ACCT_SECRET_FILE, "w") as f:
            f.write(s)
        os.chmod(ACCT_SECRET_FILE, 0o600)
    except OSError:  # noqa: BLE001
        pass
    _acct_cache["secret"] = s
    return s


def pw_hash(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return salt + "$" + h


def pw_ok(pw, stored):
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return hmac.compare_digest(calc, h)


def acct_token(email):
    exp = str(int(time.time()) + ACCT_TTL)
    sig = hmac.new(acct_secret().encode(), f"{email}|{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return f"acct|{email}|{exp}|{sig}"


def acct_verify(value):
    """Bearer token 'acct|email|exp|sig' -> email or None."""
    try:
        _, email, exp, sig = (value or "").split("|")
        if int(exp) < time.time():
            return None
        good = hmac.new(acct_secret().encode(), f"{email}|{exp}".encode(),
                        hashlib.sha256).hexdigest()
        return email if hmac.compare_digest(sig, good) else None
    except Exception:  # noqa: BLE001
        return None


def acct_email(self):
    h = self.headers.get("Authorization") or ""
    if h.startswith("Bearer "):
        return acct_verify(h[7:])
    return None


def send_acct_code(ip, email):
    now = time.time()
    recent = [t for t in _code_limit.get(ip, []) if now - t < 3600]
    if len(recent) >= 5:
        return False
    recent.append(now)
    _code_limit[ip] = recent
    code = f"{secrets.randbelow(1000000):06d}"
    conn = db()
    conn.execute("DELETE FROM email_codes WHERE email=? AND purpose='register'",
                 (email,))
    conn.execute(
        "INSERT INTO email_codes(email,code,purpose,expires_at) VALUES(?,?,?,?)",
        (email, code, "register",
         ts(datetime.now(timezone.utc) + timedelta(minutes=15))))
    conn.commit()
    conn.close()
    return send_mail(
        email, "PayCensus — your account code",
        f"Your PayCensus account verification code is: {code}\n\n"
        "It expires in 15 minutes. If you did not request it, ignore this mail.",
        from_addr="hello@paycensus.com",
        cred_path="/opt/paycensus/.smtp_hello")


def account_overview(conn, email):
    """Customer self-service overview: verification, subscription, invoices,
    API keys, usage, badge assets."""
    v = conn.execute(
        "SELECT * FROM verifications WHERE LOWER(contact)=? "
        "ORDER BY id DESC LIMIT 1", (email,)).fetchone()
    sub = conn.execute(
        "SELECT * FROM subscriptions WHERE LOWER(contact)=? "
        "ORDER BY id DESC LIMIT 1", (email,)).fetchone()
    cust = (sub["stripe_customer_id"] if sub and sub["stripe_customer_id"]
            else None)
    invs = []
    if cust:
        invs = [dict(r) for r in conn.execute(
            "SELECT invoice_id, hosted_invoice_url, amount_paid_usd, currency,"
            " status, created FROM customer_invoices WHERE stripe_customer_id=?"
            " OR LOWER(COALESCE(customer_email,''))=? ORDER BY created DESC",
            (cust, email))]
    elif email:
        invs = [dict(r) for r in conn.execute(
            "SELECT invoice_id, hosted_invoice_url, amount_paid_usd, currency,"
            " status, created FROM customer_invoices WHERE "
            "LOWER(COALESCE(customer_email,''))=? ORDER BY created DESC",
            (email,))]
    keys = [dict(r) for r in conn.execute(
        "SELECT id, label, created_at, last_used FROM api_keys "
        "WHERE owner=? AND revoked=0 ORDER BY id", (email,))]
    day = utcnow().strftime("%Y-%m-%d")
    for k in keys:
        u = conn.execute("SELECT calls FROM api_usage WHERE day=? AND key_id=?",
                         (day, k["id"])).fetchone()
        lt = conn.execute("SELECT SUM(calls) c FROM api_usage WHERE key_id=?",
                          (k["id"],)).fetchone()
        k["calls_today"] = u["calls"] if u else 0
        k["calls_lifetime"] = lt["c"] or 0
    log = []
    if keys:
        qmarks = ",".join("?" * len(keys))
        log = [dict(r) for r in conn.execute(
            f"SELECT ts, key_id, endpoint, ip FROM api_log WHERE key_id IN "
            f"({qmarks}) ORDER BY ts DESC LIMIT 50",
            [k["id"] for k in keys])]
    badge = None
    if v:
        badge = {
            "addr": v["addr"], "domain": v["domain"], "status": v["status"],
            "verified_at": v["verified_at"],
            "since": (v["verified_at"] or "")[:10] or None,
            "fail_count": v["fail_count"],
            "next_recheck": None,
            "badge_snippet": (f'<script src="https://trust.paycensus.com/badge.js" '
                              f'data-addr="{v["addr"]}"></script>'),
            "assets_url": "https://trust.paycensus.com/assets.html",
        }
        if v["last_checked"]:
            try:
                lc = datetime.strptime(
                    v["last_checked"].replace(" UTC", ""),
                    "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                badge["next_recheck"] = ts(lc + timedelta(hours=72))
            except Exception:  # noqa: BLE001
                badge["next_recheck"] = None
    # paid access: active subscription OR a paid invoice within the last year
    paid_active = bool(sub and sub["status"] == "active")
    expires = sub["current_period_end"] if sub else None
    if invs and not paid_active:
        try:
            last = max(i["created"] for i in invs if i.get("created"))
            ld = datetime.strptime(last, "%Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc)
            if ld >= utcnow() - timedelta(days=365):
                paid_active = True
                expires = ts(ld + timedelta(days=365))
        except Exception:  # noqa: BLE001
            pass
    api_plan = (conn.execute(
        "SELECT plan, status, current_period_end FROM account_plans "
        "WHERE email=?", (email,)).fetchone())
    api_plan = dict(api_plan) if api_plan else None
    if api_plan and api_plan["status"] == "active":
        for k in keys:
            k["plan"] = "plus"
    return {
        "email": email,
        "verification": badge,
        "subscription": (dict(sub) if sub else None),
        "paid_access": paid_active,
        "expires": expires,
        "api_plan": api_plan,
        "invoices": invs,
        "api_keys": keys,
        "api_log": log,
        "api_note": ("factor calls via your keys; Verified+ active — 100k/day"
                     if api_plan and api_plan["status"] == "active" else
                     "factor calls via your keys; 10/day free tier — upgrade "
                     "to Verified+ ($49/mo) for 100k/day"),
    }


def backfill_invoices():
    """One-shot: populate customer_invoices from Stripe (latest 100 paid)."""
    code, res = stripe_get("/invoices?status=paid&limit=100")
    if code != 200:
        print(f"stripe unreadable ({code})")
        return
    conn = db()
    n = 0
    for i in res.get("data", []):
        conn.execute(
            "INSERT INTO customer_invoices(invoice_id,stripe_customer_id,"
            "hosted_invoice_url,amount_paid_usd,currency,status,created,"
            "customer_email) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(invoice_id) "
            "DO UPDATE SET customer_email=COALESCE(NULLIF(excluded.customer_email,''), customer_email)",
            (i.get("id"), i.get("customer"), i.get("hosted_invoice_url"),
             i.get("amount_paid", 0) / 100.0, i.get("currency"),
             i.get("status"),
             ts(datetime.fromtimestamp(i.get("created", 0), timezone.utc)),
             (i.get("customer_email") or "")[:120]))
        n += 1
    conn.commit()
    conn.close()
    print(f"backfilled {n} invoices")


def cfg_masked():
    cfg = load_cfg(STRIPE_CFG)
    out = {}
    for k in CFG_KEYS:
        v = cfg.get(k, "")
        out[k] = (v[:12] + "…" + v[-4:]) if v else "(not set)"
    out["_file"] = "readable" if cfg else "missing/unreadable"
    return out


def cfg_write(updates):
    """Merge provided fields into the config file, preserving unknown keys.
    Returns (ok, error)."""
    cfg = load_cfg(STRIPE_CFG)
    for k, v in updates.items():
        if k in CFG_KEYS and v:
            cfg[k] = v.strip()
    lines = [f"{k}={cfg[k]}\n" for k in CFG_KEYS if k in cfg]
    lines += [f"{k}={v}\n" for k, v in cfg.items() if k not in CFG_KEYS]
    try:
        with open(STRIPE_CFG, "w") as f:
            f.write("".join(lines))
        os.chmod(STRIPE_CFG, 0o600)
        return True, ""
    except OSError as ex:
        return False, str(ex)[:200]


def admin_selftest(test_mail=False):
    results = []

    def add(name, ok, detail=""):
        results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:200]})

    # 1. config fields (core 5 required; PLUS/self-addrs optional)
    cfg = load_cfg(STRIPE_CFG)
    missing = [k for k in CFG_REQUIRED if not cfg.get(k)]
    add("config_fields", not missing,
        "core 5 present" + (" + verified_plus price" if cfg.get("STRIPE_PRICE_PLUS") else "")
        if not missing else "missing: " + ",".join(missing))

    # 2. restricted key valid
    code, res = stripe_get("/products?limit=1")
    add("stripe_key_valid", code == 200,
        "key accepted by Stripe" if code == 200 else f"HTTP {code}")

    # 3/4. prices exist, active, yearly, right amounts
    for label, pid, amount in (("founding_price", cfg.get("STRIPE_PRICE_FOUNDING"), 9900),
                               ("list_price", cfg.get("STRIPE_PRICE_LIST"), 19900)):
        if not pid:
            add(label, False, "not set")
            continue
        code, p = stripe_get("/prices/" + pid)
        rec = (p or {}).get("recurring") or {}
        ok = (code == 200 and p.get("active") and p.get("unit_amount") == amount
              and rec.get("interval") == "year")
        add(label, ok, f"{p.get('nickname', '')} ${p.get('unit_amount', 0)/100:.0f}/yr"
                       f" active={p.get('active')}" if code == 200 else f"HTTP {code}")

    # 5. webhook endpoint registered & enabled
    code, res = stripe_get("/webhook_endpoints?limit=20")
    if code == 200:
        eps = [e for e in res.get("data", [])
               if "trust.paycensus.com/api/stripe/webhook" in (e.get("url") or "")]
        if eps:
            add("webhook_endpoint", eps[0].get("status") == "enabled",
                f"url={eps[0]['url']} status={eps[0].get('status')} "
                f"events={len(eps[0].get('enabled_events', []))}")
        else:
            add("webhook_endpoint", False,
                "no endpoint pointing at /api/stripe/webhook — create it in "
                "the Stripe dashboard")
    else:
        add("webhook_endpoint", None,
            f"cannot verify via API (HTTP {code}) — restricted key may lack "
            "webhook read; check dashboard manually")

    # 5b. X credentials (format + live verify via tweepy)
    xres = x_selfcheck()
    results.append(xres)

    # 5c. indices (MPI/TVI) BTC anchoring: fresh anchor includes indices.json,
    # snapshot integrity + receipt on disk
    results.append(indices_anchor_check())

    # 6. mail path
    if test_mail:
        ok = send_mail(MAIL_FROM, "PayCensus Trust — admin self-test mail",
                       "Self-test mail triggered from the trust admin panel. "
                       "No reply needed.")
        add("mail_path", ok, "test mail sent to " + MAIL_FROM if ok else "send failed")
    else:
        import os as _os
        add("mail_path", _os.path.exists(SMTP_CRED),
            "credentials file present" if _os.path.exists(SMTP_CRED)
            else SMTP_CRED + " missing")

    # 7. DB / founding counter
    try:
        conn = db()
        t = founding_taken(conn)
        conn.close()
        add("db_founding_counter", True,
            f"taken={t} remaining={FOUNDING_TOTAL - t}")
    except Exception as ex:  # noqa: BLE001
        add("db_founding_counter", False, str(ex)[:120])

    # 8. DNS resolver sanity (control query on a known TXT-heavy domain)
    ok, per = check_domain_nonce("example.com", "paycensus-verify=selftest-probe")
    add("dns_resolvers", all(v in ("miss",) for v in per.values()),
        f"3 resolvers responded: {per}")

    overall = all(r["ok"] is not False for r in results)
    return {"ok": overall, "version": VERSION, "checked_at": ts(),
            "results": results}


# ---------- admin: ops panels (customers/revenue/usage/oss/visitors) ----------

def admin_customers(conn, search="", page=1, per_page=25):
    """Joined customer view (server-side pagination): verifications + latest
    subscription per seller. Adds badge/seal entitlement per row."""
    like = f"%{search.strip().lower()}%"
    where = "(v.addr LIKE ? OR v.domain LIKE ? OR v.contact LIKE ?)"
    args = (like, like, like)
    total = conn.execute(
        "SELECT COUNT(*) FROM verifications v LEFT JOIN subscriptions s "
        "ON s.vid=v.id WHERE " + where, args).fetchone()[0]
    per_page = max(1, min(int(per_page or 25), 100))
    pages = max(1, (total + per_page - 1) // per_page)
    try:
        page = min(max(1, int(page)), pages)
    except (TypeError, ValueError):
        page = 1
    rows = conn.execute(
        "SELECT v.id vid, v.addr, v.domain, v.status, v.verified_at, v.created_at,"
        " v.contact, v.fail_count, s.plan, s.status sub_status, s.founding, s.price_cents,"
        " s.current_period_end, s.stripe_subscription_id"
        " FROM verifications v LEFT JOIN subscriptions s ON s.id="
        " (SELECT id FROM subscriptions WHERE vid=v.id ORDER BY id DESC LIMIT 1)"
        " WHERE " + where +
        " ORDER BY v.id DESC LIMIT ? OFFSET ?",
        args + (per_page, (page - 1) * per_page)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["email"] = d["contact"] or "—"
        d["badge"] = ("badge ✓ (free strip)" if d["status"] == "verified"
                      else "—" if d["status"] == "none" else d["status"])
        if d["sub_status"] == "active":
            d["seal"] = ("seal ✓ (founding ring)" if d["founding"] else "seal ✓")
        else:
            d["seal"] = "—"
        alerts = []
        if d["status"] == "revoked":
            alerts.append("revoked")
        elif d["fail_count"] and d["fail_count"] > 0:
            alerts.append(f"{d['fail_count']} strike(s)")
        if d["sub_status"] in ("canceled", "past_due"):
            alerts.append("sub " + d["sub_status"])
        if not d["contact"]:
            alerts.append("no contact email")
        d["alert"] = ", ".join(alerts)
        # last payment (from shared invoice cache, matched on customer/sub ids)
        d["last_payment_usd"] = None
        d["last_payment_at"] = None
        if d.get("stripe_customer_id") or d.get("stripe_subscription_id"):
            inv = _paid_invoices()
            if inv:
                mine = [i for i in inv if i["customer"] == d.get("stripe_customer_id")
                        or i["subscription"] == d.get("stripe_subscription_id")]
                if mine:
                    last = max(mine, key=lambda x: x["created"])
                    d["last_payment_usd"] = round(
                        sum(x["paid_usd"] for x in mine), 2)
                    d["last_payment_at"] = last["created"]
        out.append(d)
    return {"total": total, "page": page, "pages": pages, "per_page": per_page,
            "count": len(out), "customers": out}


def admin_anomalies(conn):
    """Abnormal-customer watchlist: revoked, strikes, stale pending, unreachable,
    subscription trouble."""
    out = {
        "revoked": [dict(r) for r in conn.execute(
            "SELECT id vid, addr, domain, contact, verified_at FROM verifications "
            "WHERE status='revoked' ORDER BY id DESC")],
        "strikes": [dict(r) for r in conn.execute(
            "SELECT id vid, domain, fail_count, contact, last_checked FROM "
            "verifications WHERE fail_count>0 AND status='verified' "
            "ORDER BY fail_count DESC")],
        "stale_pending": [dict(r) for r in conn.execute(
            "SELECT id vid, domain, contact, created_at, expires_at FROM "
            "verifications WHERE status='pending' AND expires_at IS NOT NULL "
            "AND expires_at < ?", (ts(),))],
        "no_contact": [dict(r) for r in conn.execute(
            "SELECT id vid, domain, status, addr FROM verifications WHERE "
            "(contact IS NULL OR contact='') AND status IN ('verified','pending')")],
        "subs_attention": [dict(r) for r in conn.execute(
            "SELECT id, vid, domain, plan, status, current_period_end FROM "
            "subscriptions WHERE status IN ('canceled','past_due','incomplete')")],
    }
    return {"counts": {k: len(v) for k, v in out.items()},
            "anomalies": out,
            "note": "strikes resolve automatically on next successful "
                    "re-verification; stale pending = nonce expired, seller "
                    "must start over"}


_inv_cache = {"ts": 0.0, "data": None}


def _paid_invoices():
    """Latest 100 paid invoices, cached 120s (revenue + customers panels share)."""
    now = time.time()
    if _inv_cache["data"] is not None and now - _inv_cache["ts"] < 120:
        return _inv_cache["data"]
    code, res = stripe_get("/invoices?status=paid&limit=100")
    if code != 200:
        return None
    inv = []
    for i in res.get("data", []):
        inv.append({
            "id": i.get("id"), "customer": i.get("customer"),
            "subscription": i.get("subscription"),
            "url": i.get("hosted_invoice_url"),
            "paid_usd": i.get("amount_paid", 0) / 100.0,
            "refunded_usd": i.get("amount_refunded", 0) / 100.0,
            "created": datetime.fromtimestamp(
                i.get("created", 0), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "created_ts": i.get("created", 0),
        })
    _inv_cache["ts"] = now
    _inv_cache["data"] = inv
    return inv


def _bucket_agg(invoices, since_ts):
    paid = sum(i["paid_usd"] for i in invoices if i["created_ts"] >= since_ts)
    ref = sum(i["refunded_usd"] for i in invoices if i["created_ts"] >= since_ts)
    return {"paid_usd": round(paid, 2), "refunded_usd": round(ref, 2),
            "net_usd": round(paid - ref, 2), "count": sum(
                1 for i in invoices if i["created_ts"] >= since_ts)}


def admin_revenue(conn):
    """MRR + per-customer paid/refunded + day/week/month aggregates (latest 100)."""
    active = conn.execute(
        "SELECT plan, price_cents, COUNT(*) n FROM subscriptions "
        "WHERE status='active' GROUP BY plan, price_cents").fetchall()
    mrr = 0
    breakdown = []
    for r in active:
        monthly = (r["price_cents"] / 12.0 if "yearly" in r["plan"]
                   else r["price_cents"])
        mrr += monthly * r["n"]
        breakdown.append({"plan": r["plan"], "count": r["n"],
                          "price_usd": r["price_cents"] // 100,
                          "monthly_usd": round(monthly * r["n"] / 100, 2)})
    pending = conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE status='incomplete'").fetchone()[0]
    inv = _paid_invoices()
    out = {"mrr_usd": round(mrr / 100, 2),
           "active_by_plan": breakdown,
           "incomplete_checkouts": pending,
           "founding_taken": founding_taken(conn),
           "founding_remaining": max(FOUNDING_TOTAL - founding_taken(conn), 0),
           "window": "latest 100 paid invoices",
           "collected_usd_latest100": None,
           "refunded_usd_latest100": None,
           "buckets": None, "by_customer": None}
    if inv is None:
        out["note"] = "stripe key unreadable / invoices unreadable"
        return out
    out["collected_usd_latest100"] = round(
        sum(i["paid_usd"] for i in inv), 2)
    out["refunded_usd_latest100"] = round(
        sum(i["refunded_usd"] for i in inv), 2)
    now = time.time()
    out["buckets"] = {
        "today": _bucket_agg(inv, datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0).replace(
            tzinfo=timezone.utc).timestamp()),
        "week": _bucket_agg(inv, now - 7 * 86400),
        "month": _bucket_agg(inv, now - 30 * 86400),
        "all": _bucket_agg(inv, 0),
    }
    # per-customer rollup, mapped to our domain + email via subscriptions table
    dom = {}
    emails = {}
    vid_email = {r["id"]: r["contact"] for r in
                 conn.execute("SELECT id, contact FROM verifications")}
    for r in conn.execute(
            "SELECT id vid, domain, contact, stripe_customer_id, "
            "stripe_subscription_id FROM subscriptions WHERE "
            "stripe_customer_id IS NOT NULL OR stripe_subscription_id IS NOT NULL"):
        tgt = r["domain"]
        em = r["contact"] or vid_email.get(r["vid"]) or ""
        if r["stripe_customer_id"]:
            dom[r["stripe_customer_id"]] = tgt
            emails[r["stripe_customer_id"]] = em
        if r["stripe_subscription_id"]:
            dom[r["stripe_subscription_id"]] = tgt
            emails[r["stripe_subscription_id"]] = em
    by_cus = {}
    for i in inv:
        key = i["customer"] or i["subscription"] or "?"
        e = by_cus.setdefault(key, {"customer": key, "domain": dom.get(
            i["customer"]) or dom.get(i["subscription"]),
            "email": emails.get(i["customer"]) or emails.get(i["subscription"]) or "",
            "paid_usd": 0.0, "refunded_usd": 0.0, "count": 0,
            "last_usd": 0.0, "last_at": "", "invoices": []})
        e["paid_usd"] += i["paid_usd"]
        e["refunded_usd"] += i["refunded_usd"]
        e["count"] += 1
        e["invoices"].append({"id": i["id"], "url": i.get("url"),
                              "created": i["created"], "paid_usd": i["paid_usd"]})
        if i["created"] > e["last_at"]:
            e["last_at"] = i["created"]
            e["last_usd"] = i["paid_usd"]
    # fill missing emails from the invoices table (Stripe customer_email)
    for key, e in by_cus.items():
        if not e["email"]:
            row = conn.execute(
                "SELECT customer_email FROM customer_invoices WHERE "
                "stripe_customer_id=? AND COALESCE(customer_email,'')!='' "
                "LIMIT 1", (key,)).fetchone()
            if row:
                e["email"] = row["customer_email"]
    out["by_customer"] = sorted(by_cus.values(),
                                key=lambda x: x["last_at"], reverse=True)
    return out


def admin_apiusage(conn):
    """Metered call statistics per key."""
    day = utcnow().strftime("%Y-%m-%d")
    keys = conn.execute(
        "SELECT k.id, k.label, k.plan, k.created_at, k.last_used,"
        " COALESCE(u.calls,0) calls_today"
        " FROM api_keys k LEFT JOIN api_usage u ON u.key_id=k.id AND u.day=?"
        " WHERE k.revoked=0 ORDER BY k.id", (day,)).fetchall()
    lifetime = conn.execute(
        "SELECT key_id, SUM(calls) c FROM api_usage GROUP BY key_id").fetchall()
    life = {r["key_id"]: r["c"] for r in lifetime}
    n_keys = conn.execute("SELECT COUNT(*) FROM api_keys WHERE revoked=0").fetchone()[0]
    today_total = sum(r["calls_today"] for r in keys)
    out = {"keys_active": n_keys, "calls_today_total": today_total,
           "keys": [{"id": r["id"], "label": r["label"], "plan": r["plan"],
                     "created_at": r["created_at"], "last_used": r["last_used"],
                     "calls_today": r["calls_today"],
                     "calls_lifetime": life.get(r["id"], 0)} for r in keys]}
    return out


def admin_sellers(q_str="", tier="", geo="", page=1, per_page=25,
                  q_kind=""):
    """Sellers CRM browser over census.db (entities×sellers×contacts, read-only).
    Internal only — role emails are never published. Geo = TLD-derived rough."""
    page = max(1, int(page))
    per_page = min(100, max(5, int(per_page)))
    GEO = {"uk": "UK", "de": "DE", "fr": "FR", "nl": "NL", "es": "ES",
           "it": "IT", "ca": "CA", "au": "AU", "jp": "JP", "kr": "KR",
           "sg": "SG", "hk": "HK", "cn": "CN", "in": "IN", "br": "BR",
           "ch": "CH", "se": "SE", "no": "NO", "fi": "FI", "dk": "DK",
           "pl": "PL", "ru": "RU", "ua": "UA", "tr": "TR", "ae": "AE",
           "il": "IL", "za": "ZA", "mx": "MX", "us": "US", "eu": "EU",
           "io": "gTLD", "ai": "gTLD", "com": "gTLD", "net": "gTLD",
           "org": "gTLD", "app": "gTLD", "dev": "gTLD", "xyz": "gTLD",
           "vc": "gTLD", "co": "gTLD", "me": "gTLD", "so": "gTLD",
           "cc": "gTLD", "to": "gTLD", "gg": "gTLD", "sh": "gTLD"}
    conn = sqlite3.connect("file:/var/lib/paycensus/census.db?mode=ro",
                           uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    where, args = ["1=1"], []
    if q_str:
        like = f"%{q_str.lower()}%"
        where.append("(LOWER(e.domain) LIKE ? OR LOWER(e.addr) LIKE ? OR "
                     "LOWER(e.title) LIKE ? OR LOWER(c.email) LIKE ?)")
        args += [like] * 4
    if tier in ("T1", "T2", "T3"):
        where.append("e.tier=?")
        args.append(tier)
    if q_kind in ("contract", "eoa"):
        where.append("s.kind=?")
        args.append(q_kind)
    w = " AND ".join(where)
    rows = conn.execute(
        f"SELECT e.addr, e.domain, e.tier, e.title, e.first_seen, "
        f"s.txs, s.volume, s.first_day, s.last_day, s.kind, s.label, "
        f"(SELECT c.email FROM contacts c WHERE c.addr=e.addr AND c.cls='role' "
        f"LIMIT 1) email FROM entities e JOIN sellers s ON s.addr=e.addr "
        f"LEFT JOIN contacts c ON c.addr=e.addr AND c.cls='role' "
        f"WHERE {w} GROUP BY e.addr ORDER BY s.volume DESC", args).fetchall()
    conn.close()
    out = []
    for r in rows:
        dom = (r["domain"] or "").lower()
        g = GEO.get(dom.rsplit(".", 1)[-1], "unknown") if dom else "unknown"
        if geo and g != geo:
            continue
        out.append({"addr": r["addr"], "domain": r["domain"], "tier": r["tier"],
                    "title": r["title"], "first_seen": r["first_seen"],
                    "txs": r["txs"], "volume": round(r["volume"] or 0, 2),
                    "last_day": r["last_day"], "email": r["email"], "geo": g,
                    "kind": r["kind"] or "unclassified",
                    "label": r["label"]})
    total = len(out)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    return {"total": total, "page": page, "pages": pages, "per_page": per_page,
            "count": len(out[page and (page - 1) * per_page:
                              page * per_page] or 0),
            "geo_note": "TLD-derived rough geography — honest approximation",
            "sellers": out[(page - 1) * per_page: page * per_page]}


def admin_oss():
    """Open-source usage: PyPI downloads + GitHub engagement ($0 public APIs)."""
    out = {"pypi": None, "github": None}
    try:
        with urllib.request.urlopen(
                "https://pypistats.org/api/packages/paycensus/recent",
                timeout=30) as r:
            out["pypi"] = json.loads(r.read()).get("data")
    except Exception as ex:  # noqa: BLE001
        out["pypi"] = {"error": str(ex)[:80]}
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/OneLight1688/paycensus",
            headers={"User-Agent": "paycensus-ops", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            g = json.loads(r.read())
        out["github"] = {"stars": g.get("stargazers_count"),
                         "forks": g.get("forks_count"),
                         "open_issues": g.get("open_issues_count"),
                         "pushed_at": g.get("pushed_at")}
    except Exception as ex:  # noqa: BLE001
        out["github"] = {"error": str(ex)[:80]}
    return out


def admin_visitors():
    """Public visitors.json + daily history series (admin chart data)."""
    try:
        vis = json.load(open("/www/wwwroot/paycensus/data/visitors.json"))
    except Exception as ex:  # noqa: BLE001
        vis = {"error": str(ex)[:120]}
    try:
        vis["history"] = json.load(
            open("/www/wwwroot/paycensus/data/visitors_history.json"))
    except Exception:  # noqa: BLE001
        vis["history"] = []
    return vis


def admin_xposts():
    """Posted-tweet tracking records (newest first)."""
    try:
        recs = json.load(open("/var/lib/paycensus/x_posts.json"))
    except Exception:  # noqa: BLE001
        return {"count": 0, "posts": [],
                "note": "no records yet — records accumulate from the next "
                        "post onward"}
    return {"count": len(recs), "posts": list(reversed(recs))}


def admin_refund(conn, sub_id, amount_cents=None):
    """Full or partial refund of the latest captured payment. Admin-only."""
    row = conn.execute("SELECT * FROM subscriptions WHERE id=?",
                       (sub_id,)).fetchone()
    conn.close()
    if not row:
        return 404, {"error": "unknown sub_id"}
    pi = row["stripe_payment_intent"]
    if not pi:
        return 400, {"error": "no captured payment on record for this "
                              "subscription (refund via Stripe dashboard)"}
    params = {"payment_intent": pi}
    if amount_cents:
        params["amount"] = str(int(amount_cents))
    res = stripe_post("/refunds", params)
    if not res or res.get("error"):
        return 502, {"error": f"stripe refund failed: {str(res)[:120]}"}
    return 200, {"ok": True, "refund_id": res.get("id"),
                 "status": res.get("status"), "amount_usd": res.get("amount", 0) / 100}


# ---------- data-layer factors (census.db ro + directory.json cache) ----------

_dir_cache = {"ts": 0.0, "by_addr": {}}


def _directory_index():
    """addr -> entity subset from directory.json, refreshed at DIRECTORY_TTL."""
    now = time.time()
    if now - _dir_cache["ts"] < DIRECTORY_TTL and _dir_cache["by_addr"]:
        return _dir_cache["by_addr"]
    idx = {}
    try:
        with open(DIRECTORY_JSON) as f:
            d = json.load(f)
        for e in d.get("entities", []):
            ent = {"domain": e.get("domain"), "title": e.get("title"),
                   "first_day": e.get("first_day"),
                   "protocols": e.get("protocols") or {},
                   "offers": []}
            offers = e.get("offers") or []
            usds = []
            protos = set()
            for o in offers:
                if o.get("sig"):
                    protos.add(o["sig"])
                if isinstance(o.get("offer_usd"), (int, float)):
                    usds.append(o["offer_usd"])
            ent["offers"] = {"count": len(offers),
                             "protocols": sorted(protos),
                             "latest_usd": usds[-1] if usds else None,
                             "distinct_prices": len(set(usds))}
            for a in e.get("addresses") or []:
                ad = a.get("addr")
                if ad and ad not in idx:
                    idx[ad] = ent
    except Exception as ex:  # noqa: BLE001
        print(f"{ts()} directory index FAIL: {str(ex)[:120]}")
        if _dir_cache["by_addr"]:
            return _dir_cache["by_addr"]  # stale is better than empty
    _dir_cache["ts"] = now
    _dir_cache["by_addr"] = idx
    return idx


def seller_factors(addr):
    """Cross-data-layer factors for one address. All read-only, $0, honest."""
    out = {"attribution": None, "onchain": None, "offers": None,
           "contactability": None}
    # 1. attribution + onchain history (census.db, read-only)
    try:
        c = sqlite3.connect(f"file:{CENSUS_DB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        ent = c.execute("SELECT domain,title,source,first_seen,category "
                        "FROM entities WHERE addr=? LIMIT 1", (addr,)).fetchone()
        if ent:
            out["attribution"] = {"domain": ent["domain"], "title": ent["title"],
                                  "source": ent["source"],
                                  "first_seen": ent["first_seen"],
                                  "category": ent["category"]}
        s = c.execute("SELECT SUM(volume) v, SUM(txs) t, MIN(first_day) fs, "
                      "COUNT(DISTINCT chain) ch FROM sellers WHERE addr=?",
                      (addr,)).fetchone()
        if s and s["t"]:
            out["onchain"] = {"volume_usd": round(s["v"] or 0, 2),
                              "txs": s["t"], "chains": s["ch"],
                              "first_day": s["fs"]}
            if s["v"] and s["v"] > 0:
                below = c.execute(
                    "SELECT COUNT(*) FROM (SELECT SUM(volume) v FROM sellers "
                    "GROUP BY addr HAVING v>0 AND v<?)", (s["v"],)).fetchone()[0]
                total = c.execute(
                    "SELECT COUNT(*) FROM (SELECT SUM(volume) v FROM sellers "
                    "GROUP BY addr HAVING v>0)").fetchone()[0]
                out["onchain"]["volume_percentile"] = round(
                    100.0 * below / max(total, 1), 1)
        has_contact = c.execute(
            "SELECT 1 FROM contacts WHERE addr=? AND cls='role' LIMIT 1",
            (addr,)).fetchone()
        out["contactability"] = {"channel": bool(has_contact),
                                 "note": "public role-contact exists; content "
                                         "is never published"}
        c.close()
    except Exception as ex:  # noqa: BLE001
        print(f"{ts()} census factors FAIL: {str(ex)[:120]}")
    # 2. offers (directory.json, cached)
    ent = _directory_index().get(addr.lower()) or _directory_index().get(addr)
    if ent:
        if out["attribution"] is None:
            out["attribution"] = {"domain": ent.get("domain"),
                                  "title": ent.get("title"),
                                  "source": "directory",
                                  "first_seen": ent.get("first_day"),
                                  "category": None}
        out["offers"] = {"offer_consistency": (
            "stable" if ent["offers"]["count"] == 1
            else "multiple_offers" if ent["offers"]["count"] > 1
            else "no_offer_observed"),
            "note": "single-snapshot view; longitudinal consistency accumulates "
                    "as hourly directory rebuilds are archived",
            **ent["offers"]}
    return out


# ---------- seal badge (SVG, paid-tier visual) ----------

def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def seal_svg(domain, since, founding=False, theme="dark"):
    bg, fg, ring = (("#12141a", "#e8e8e8", "#F6821F") if theme == "dark"
                    else ("#ffffff", "#12141a", "#F6821F"))
    ym = (since or "")[:7] or "—"
    text = f"PAYCENSUS VERIFIED · {_esc(domain.upper())} · SINCE {ym} · "
    if founding:
        text = f"FOUNDING MEMBER · {text}"
    while len(text) < 64:
        text += "· "
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400" role="img" aria-label="PayCensus Verified seal for {domain}">
  <defs><path id="cp" d="M 200,200 m -156,0 a 156,156 0 1,1 312,0 a 156,156 0 1,1 -312,0"/></defs>
  <circle cx="200" cy="200" r="196" fill="{bg}" stroke="{ring}" stroke-width="6"/>
  <circle cx="200" cy="200" r="120" fill="none" stroke="{ring}" stroke-width="2" opacity="0.5"/>
  <text font-family="ui-monospace,Menlo,Consolas,monospace" font-size="21" letter-spacing="3.5" fill="{ring}">
    <textPath href="#cp">{text}</textPath>
  </text>
  <path d="M 155 205 L 190 238 L 250 168" fill="none" stroke="{ring}" stroke-width="14" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="200" y="290" text-anchor="middle" font-family="system-ui" font-size="17" fill="{fg}">{_esc(domain)}</text>
  <text x="200" y="318" text-anchor="middle" font-family="system-ui" font-size="13" fill="{fg}" opacity="0.65">domain control verified {ym}</text>
</svg>'''


def seal_allowed(conn, addr):
    """Seal = paid Verified (active subscription) or founder/self addresses."""
    self_addrs = [a.strip().lower() for a in
                  load_cfg(STRIPE_CFG).get("TRUST_SELF_ADDRS", "").split(",")
                  if a.strip()]
    if addr.lower() in self_addrs:
        return True, True  # founder: seal with founding ring
    row = conn.execute(
        "SELECT s.founding FROM subscriptions s JOIN verifications v "
        "ON v.id=s.vid WHERE v.addr=? AND s.status='active' "
        "ORDER BY s.id DESC LIMIT 1", (addr,)).fetchone()
    return (True, bool(row["founding"])) if row else (False, False)


# ---------- HTTP ----------

_hits = {}  # per-IP rate limit: (minute_bucket -> count)


def limited(ip, cap=30):
    b = int(time.time() // 60)
    key = (ip, b)
    _hits[key] = _hits.get(key, 0) + 1
    if len(_hits) > 5000:
        for k in [k for k in _hits if k[1] < b - 5]:
            _hits.pop(k, None)
    return _hits[key] <= cap


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def json_out(self, code, obj, cache=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if cache:
            self.send_header("Cache-Control", f"public, max-age={cache}")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{ts()} {self.address_string()} {fmt % args}")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        ip = self.client_address[0]
        if not limited(ip):
            return self.json_out(429, {"error": "rate limited"})
        path, _, qs = self.path.partition("?")
        q = {}
        for kv in qs.split("&"):
            if "=" in kv:
                k, _, v = kv.partition("=")
                q[k] = v
        try:
            if path == "/api/meta":
                return self.json_out(200, {
                    "service": "paycensus-trust", "version": VERSION,
                    "note": "Deterministic, fully automated seller verification. "
                            "Domain control only — not a guarantee of honesty, "
                            "solvency, or legality. Revocations are public and "
                            "append-only."})
            if path == "/api/status":
                a = q.get("addr", "")
                d = q.get("domain", "")
                conn = db()
                if a and ADDR_RE.match(a):
                    r = conn.execute(
                        "SELECT * FROM verifications WHERE addr=? "
                        "ORDER BY id DESC LIMIT 1", (a,)).fetchone()
                elif d and DOMAIN_RE.match(d.lower()):
                    r = conn.execute(
                        "SELECT * FROM verifications WHERE domain=? "
                        "ORDER BY id DESC LIMIT 1", (d.lower(),)).fetchone()
                else:
                    conn.close()
                    return self.json_out(400, {"error": "valid addr or domain required"})
                conn.close()
                if not r:
                    return self.json_out(200, {"status": "none"},
                                         cache=60)
                return self.json_out(200, row_pub(r), cache=60)
            if path == "/api/revocations":
                conn = db()
                rows = conn.execute(
                    "SELECT v.addr, v.domain, rv.revoked_at, rv.reason, "
                    "rv.reinstated_at FROM revocations rv JOIN verifications v "
                    "ON v.id=rv.vid ORDER BY rv.id DESC LIMIT 100").fetchall()
                conn.close()
                return self.json_out(200, {"revocations": [dict(x) for x in rows]})
            if path == "/api/citations":
                conn = db()
                n_verified = conn.execute(
                    "SELECT COUNT(*) FROM verifications WHERE status='verified'"
                ).fetchone()[0]
                conn.close()
                return self.json_out(200, {
                    "verified_entities": n_verified,
                    "note": "Live count — the citation counter itself is the metric.",
                    "version": VERSION})
            if path == "/api/founding":
                conn = db()
                taken = founding_taken(conn)
                conn.close()
                return self.json_out(200, {
                    "founding_total": FOUNDING_TOTAL, "taken": taken,
                    "remaining": max(FOUNDING_TOTAL - taken, 0),
                    "founding_price_usd": FOUNDING_PRICE_CENTS // 100,
                    "list_price_usd": LIST_PRICE_CENTS // 100,
                    "note": "Founding price $99/yr, locked for life, first "
                            f"{FOUNDING_TOTAL} paid subscriptions."}, cache=300)
            if path == "/api/outreach/unsub":
                # one-click unsubscribe (RFC 8058) for outreach mails:
                # appends the address to the suppression list. No auth by
                # design — worst case someone unsubscribes an address that
                # is not theirs; that address simply stops hearing from us.
                e = (q.get("e") or "").lower().strip()
                if "@" not in e or len(e) > 120:
                    return self.json_out(400, {"error": "bad email"})
                try:
                    with open("/opt/paycensus/outreach_suppression.txt",
                              "a") as f:
                        f.write(e + "\n")
                except OSError as ex:
                    return self.json_out(500, {"error": str(ex)[:120]})
                return self.json_out(200, {
                    "ok": True,
                    "note": "address added to outreach suppression list"})
            if path == "/api/agent/rank":
                conn = db()
                krow, err = _key_auth(self, conn)
                if err:
                    conn.close()
                    return self.json_out(err[0], err[1])
                day = utcnow().strftime("%Y-%m-%d")
                used = conn.execute(
                    "SELECT calls FROM api_usage WHERE day=? AND key_id=?",
                    (day, krow["id"])).fetchone()
                used = used[0] if used else 0
                limit = AGENT_CALLS_FREE if krow["plan"] == "free" else 100000
                if used >= limit:
                    conn.close()
                    return self.json_out(429, {
                        "error": "daily agent-api quota exhausted",
                        "limit": limit,
                        "upgrade": {"plan": "Verified+ $49/mo",
                                    "url": "https://trust.paycensus.com/",
                                    "note": "raises agent-api quota to 100k/day"},
                        "note": "Quota is shared across /api/agent/* and "
                                "resets 00:00 UTC."})
                try:
                    data = json.load(open(
                        "/www/wwwroot/paycensus/data/rank.json"))
                except OSError:
                    conn.close()
                    return self.json_out(503, {"error": "rank data not ready"})
                ranked = data.get("ranked") or []
                mn = q.get("min_score")
                if mn:
                    try:
                        ranked = [r for r in ranked
                                  if r.get("score", 0) >= float(mn)]
                    except ValueError:
                        pass
                cls_f = (q.get("cls") or "").strip().lower()
                if cls_f:
                    ranked = [r for r in ranked if r.get("class") == cls_f]
                vf = (q.get("verified") or "").strip().lower()
                if vf in ("true", "1"):
                    ranked = [r for r in ranked if r.get("verified")]
                elif vf in ("false", "0"):
                    ranked = [r for r in ranked if not r.get("verified")]
                try:
                    lim = min(500, max(1, int(q.get("limit") or 100)))
                except ValueError:
                    lim = 100
                calls = _usage_today(conn, krow["id"], "/api/agent", ip)
                conn.close()
                return self.json_out(200, {
                    "generated_at": data.get("generated_at"),
                    "count": len(ranked[:lim]), "plan": krow["plan"],
                    "calls_today": calls, "limit": limit,
                    "ranked": ranked[:lim]})
            if path == "/api/agent/map":
                conn = db()
                krow, err = _key_auth(self, conn)
                if err:
                    conn.close()
                    return self.json_out(err[0], err[1])
                day = utcnow().strftime("%Y-%m-%d")
                used = conn.execute(
                    "SELECT calls FROM api_usage WHERE day=? AND key_id=?",
                    (day, krow["id"])).fetchone()
                used = used[0] if used else 0
                limit = AGENT_CALLS_FREE if krow["plan"] == "free" else 100000
                if used >= limit:
                    conn.close()
                    return self.json_out(429, {
                        "error": "daily agent-api quota exhausted",
                        "limit": limit,
                        "note": "Quota is shared across /api/agent/* and "
                                "resets 00:00 UTC."})
                calls = _usage_today(conn, krow["id"], "/api/agent", ip)
                conn.close()
                try:
                    data = json.load(open(
                        "/www/wwwroot/paycensus/data/agent_map.json"))
                except OSError:
                    return self.json_out(503, {"error": "map data not ready"})
                return self.json_out(200, {"plan": krow["plan"],
                                           "calls_today": calls,
                                           "limit": limit, **data})
            if path == "/api/agent/bazaar":
                conn = db()
                krow, err = _key_auth(self, conn)
                if err:
                    conn.close()
                    return self.json_out(err[0], err[1])
                day = utcnow().strftime("%Y-%m-%d")
                used = conn.execute(
                    "SELECT calls FROM api_usage WHERE day=? AND key_id=?",
                    (day, krow["id"])).fetchone()
                used = used[0] if used else 0
                limit = AGENT_CALLS_FREE if krow["plan"] == "free" else 100000
                if used >= limit:
                    conn.close()
                    return self.json_out(429, {
                        "error": "daily agent-api quota exhausted",
                        "limit": limit,
                        "note": "Quota is shared across /api/agent/* and "
                                "resets 00:00 UTC."})
                calls = _usage_today(conn, krow["id"], "/api/agent", ip)
                conn.close()
                try:
                    data = json.load(open(
                        "/www/wwwroot/paycensus/data/bazaar_stats.json"))
                except OSError:
                    return self.json_out(503, {"error": "bazaar data not ready"})
                return self.json_out(200, {"plan": krow["plan"],
                                           "calls_today": calls,
                                           "limit": limit, **data})
            if path == "/api/factors":
                conn = db()
                krow, err = _key_auth(self, conn)
                if err:
                    conn.close()
                    return self.json_out(err[0], err[1])
                a = q.get("addr", "")
                if not ADDR_RE.match(a):
                    conn.close()
                    return self.json_out(400, {"error": "valid addr required"})
                day = utcnow().strftime("%Y-%m-%d")
                used = conn.execute(
                    "SELECT calls FROM api_usage WHERE day=? AND key_id=?",
                    (day, krow["id"])).fetchone()
                used = used[0] if used else 0
                limit = FREE_CALLS_DAY if krow["plan"] == "free" else 100000
                if used >= limit:
                    conn.close()
                    return self.json_out(429, {
                        "error": "daily free quota exhausted",
                        "limit": limit,
                        "upgrade": {"plan": "Verified+ $49/mo",
                                    "url": "https://trust.paycensus.com/",
                                    "note": "raises factor metering to 100k/day"},
                        "note": "Free tier: 10 factor calls/day per key. "
                                "Upgrade at trust.paycensus.com or retry "
                                "tomorrow (quota resets 00:00 UTC)."})
                r = conn.execute("SELECT * FROM verifications WHERE addr=? "
                                 "ORDER BY id DESC LIMIT 1", (a,)).fetchone()
                calls = _usage_today(conn, krow["id"], "/api/factors", ip)
                factors = seller_factors(a)
                conn.close()
                out = {"addr": a,
                       "status": r["status"] if r else "none",
                       "domain": r["domain"] if r else None,
                       "since": (r["verified_at"] or "")[:10] if r else None,
                       "last_checked": r["last_checked"] if r else None,
                       "plan": krow["plan"], "calls_today": calls, "limit": limit,
                       "factors": factors,
                       "note": "Deterministic facts only — factors, never a "
                               "composite score."}
                return self.json_out(200, out)
            if path == "/api/usage":
                conn = db()
                krow, err = _key_auth(self, conn)
                if err:
                    conn.close()
                    return self.json_out(err[0], err[1])
                day = utcnow().strftime("%Y-%m-%d")
                used = conn.execute(
                    "SELECT calls FROM api_usage WHERE day=? AND key_id=?",
                    (day, krow["id"])).fetchone()
                conn.close()
                used = used[0] if used else 0
                limit = FREE_CALLS_DAY if krow["plan"] == "free" else 100000
                return self.json_out(200, {"day": day, "calls": used,
                                           "limit": limit, "plan": krow["plan"]})
            if path == "/api/seal.svg":
                a = q.get("addr", "")
                if not ADDR_RE.match(a):
                    return self.json_out(400, {"error": "valid addr required"})
                conn = db()
                r = conn.execute("SELECT * FROM verifications WHERE addr=? AND "
                                 "status='verified' ORDER BY id DESC LIMIT 1",
                                 (a,)).fetchone()
                if not r:
                    conn.close()
                    return self.json_out(404, {"error": "no verified record"})
                allowed, founding = seal_allowed(conn, a)
                conn.close()
                if not allowed:
                    return self.json_out(402, {
                        "error": "seal is part of the Verified subscription",
                        "note": "https://trust.paycensus.com/",
                        "free_alternative": "badge.js embed"})
                svg = seal_svg(r["domain"], r["verified_at"], founding,
                               theme=q.get("theme", "dark"))
                body = svg.encode()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Cache-Control", "public, max-age=300")
                self._cors()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            if path == "/api/account/overview":
                email = acct_email(self)
                if not email:
                    return self.json_out(401, {"error": "login required"})
                conn = db()
                out = account_overview(conn, email)
                conn.close()
                return self.json_out(200, out)
            if path == "/api/admin/config":
                # nginx gates this path with basic auth (.htpasswd_ops);
                # direct access to 8788 is localhost-only.
                return self.json_out(200, cfg_masked())
            if path == "/api/admin/selftest":
                test_mail = q.get("mail") == "1"
                return self.json_out(200, admin_selftest(test_mail=test_mail))
            if path == "/api/admin/customers":
                conn = db()
                out = admin_customers(conn, q.get("search", ""),
                                      page=q.get("page", 1),
                                      per_page=q.get("per_page", 25))
                conn.close()
                return self.json_out(200, out)
            if path == "/api/admin/revenue":
                conn = db()
                out = admin_revenue(conn)
                conn.close()
                return self.json_out(200, out)
            if path == "/api/admin/apiusage":
                conn = db()
                out = admin_apiusage(conn)
                conn.close()
                return self.json_out(200, out)
            if path == "/api/admin/oss":
                return self.json_out(200, admin_oss())
            if path == "/api/admin/visitors":
                return self.json_out(200, admin_visitors())
            if path == "/api/admin/sellers":
                return self.json_out(200, admin_sellers(
                    q.get("q", ""), q.get("tier", ""), q.get("geo", ""),
                    page=q.get("page", 1), per_page=q.get("per", 25),
                    q_kind=q.get("kind", "")))
            if path == "/api/admin/xposts":
                return self.json_out(200, admin_xposts())
            if path == "/api/admin/xkeys":
                return self.json_out(200, x_masked())
            if path == "/api/admin/orkeys":
                return self.json_out(200, or_masked())
            if path == "/api/admin/anomalies":
                conn = db()
                out = admin_anomalies(conn)
                conn.close()
                return self.json_out(200, out)
            return self.json_out(404, {"error": "not found"})
        except Exception as ex:  # noqa: BLE001
            return self.json_out(500, {"error": str(ex)[:200]})

    def do_POST(self):
        ip = self.client_address[0]
        path = self.path
        # --- Stripe webhook: raw body, signature-verified, outside JSON/honeypot ---
        if path == "/api/stripe/webhook":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 65536:
                return self.json_out(400, {"error": "bad payload size"})
            payload = self.rfile.read(length)
            if not webhook_sig_ok(self.headers.get("Stripe-Signature"), payload):
                return self.json_out(400, {"error": "invalid signature"})
            conn = db()
            code = handle_webhook(conn, payload)
            conn.close()
            return self.json_out(code, {"received": True})
        if path == "/api/coldmail/hook":
            # ColdMail Gateway result webhook: HMAC_SHA256(caller_token,
            # ts + "." + raw_body); bounce/complaint/unsubscribe -> suppression.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 65536:
                return self.json_out(400, {"error": "bad payload size"})
            payload = self.rfile.read(length)
            tok = ""
            try:
                tok = open("/opt/paycensus/.coldmail_token").read().strip()
            except OSError:
                pass
            ts_h = self.headers.get("X-Gateway-Timestamp") or ""
            sig_h = self.headers.get("X-Gateway-Signature") or ""
            try:
                if abs(time.time() - int(ts_h)) > 300:
                    return self.json_out(400, {"error": "stale timestamp"})
                expect = "sha256=" + hmac.new(
                    tok.encode(), ts_h.encode() + b"." + payload,
                    hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expect, sig_h):
                    return self.json_out(400, {"error": "invalid signature"})
                ev = json.loads(payload)
            except Exception as ex:  # noqa: BLE001
                return self.json_out(400, {"error": str(ex)[:120]})
            etype = (self.headers.get("X-Gateway-Event")
                     or ev.get("event") or ev.get("type") or "unknown")
            recip = (ev.get("email") or ev.get("to") or
                     ev.get("recipient") or "")
            try:
                with open("/var/lib/paycensus/coldmail_events.jsonl", "a") as f:
                    f.write(json.dumps({"at": ts(), "event": etype,
                                        "to": recip, "delivery":
                                        self.headers.get("X-Gateway-Delivery"),
                                        "payload": ev}) + "\n")
                if etype in ("bounce", "complaint", "unsubscribe") and recip:
                    with open("/opt/paycensus/outreach_suppression.txt",
                              "a") as f:
                        f.write(recip.lower().strip() + "\n")
                elif etype == "reply" and recip:
                    # doc: reply = inbound lead (IMAP In-Reply-To matched) —
                    # dedicated triage file, surfaced by outreach_status.py
                    with open("/var/lib/paycensus/reply_leads.jsonl",
                              "a") as f:
                        f.write(json.dumps({"at": ts(), "to": recip,
                                            "send_id": ev.get("send_id"),
                                            "campaign": ev.get("campaign") or "",
                                            "subject": (ev.get("subject") or "")[:200],
                                            "detail": (ev.get("detail") or "")[:300]}) + "\n")
            except OSError as ex:
                return self.json_out(500, {"error": str(ex)[:120]})
            return self.json_out(200, {"ok": True, "event": etype})
        if not limited(ip, cap=10):
            return self.json_out(429, {"error": "rate limited"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 2048:
                return self.json_out(413, {"error": "body too large"})
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self.json_out(400, {"error": "invalid json"})
        if body.get("hp"):  # honeypot: silently accept, never store
            return self.json_out(200, {"ok": True})
        path = self.path
        try:
            conn = db()
            if path == "/api/verify/start":
                addr = (body.get("addr") or "").strip()
                domain = (body.get("domain") or "").strip().lower()
                contact = (body.get("contact") or "")[:120]
                if not ADDR_RE.match(addr):
                    conn.close()
                    return self.json_out(400, {"error": "invalid address"})
                if not DOMAIN_RE.match(domain) or len(domain) > 100:
                    conn.close()
                    return self.json_out(400, {"error": "invalid domain"})
                nonce = "paycensus-verify=" + secrets.token_hex(16)
                exp = ts(utcnow() + timedelta(hours=NONCE_TTL_HOURS))
                cur = conn.execute(
                    "INSERT INTO verifications(addr,domain,nonce,contact,"
                    "status,created_at,expires_at) VALUES(?,?,?,?, 'pending',?,?)",
                    (addr, domain, nonce, contact, ts(), exp))
                conn.commit()
                vid = cur.lastrowid
                conn.close()
                return self.json_out(200, {
                    "vid": vid, "status": "pending",
                    "txt_host": f"{TXT_HOST_PREFIX}.{domain}",
                    "txt_value": nonce, "expires_at": exp,
                    "howto": f"Add a TXT record at {TXT_HOST_PREFIX}.{domain} "
                             f"with value {nonce}, then call /api/verify/check "
                             f"or click Re-check on the verify page."})
            if path == "/api/verify/check":
                try:
                    vid = int(body.get("vid") or 0)
                except ValueError:
                    vid = 0
                if vid <= 0:
                    conn.close()
                    return self.json_out(400, {"error": "vid required"})
                res = run_check(conn, vid)
                conn.close()
                if res is None:
                    return self.json_out(404, {"error": "unknown vid"})
                return self.json_out(200, res)
            if path == "/api/report":
                vid = int(body.get("vid") or 0)
                msg = (body.get("message") or "")[:500]
                contact = (body.get("contact") or "")[:120]
                r = conn.execute("SELECT id FROM verifications WHERE id=?",
                                 (vid,)).fetchone() if vid else None
                if not r:
                    conn.close()
                    return self.json_out(404, {"error": "unknown vid"})
                # a report NEVER decides anything — it only triggers an
                # automatic re-examination; the deterministic result stands.
                res = run_check(conn, vid)
                conn.execute(
                    "INSERT INTO reports(vid,contact,message,created_at,outcome) "
                    "VALUES(?,?,?,?,?)",
                    (vid, contact, msg, ts(),
                     json.dumps(res.get("status") if res else "error")))
                conn.commit()
                conn.close()
                return self.json_out(200, {
                    "ok": True,
                    "note": "Report logged (append-only). It triggered an "
                            "automatic re-examination; the deterministic "
                            "result is the outcome.",
                    "result": res})
            if path == "/api/checkout/start":
                try:
                    vid = int(body.get("vid") or 0)
                except ValueError:
                    vid = 0
                r = conn.execute("SELECT * FROM verifications WHERE id=?",
                                 (vid,)).fetchone() if vid else None
                if vid and not r:
                    conn.close()
                    return self.json_out(404, {"error": "unknown vid"})
                if vid and r["status"] != "verified":
                    conn.close()
                    return self.json_out(409, {
                        "error": "verification must pass before subscribing"})
                cfg = load_cfg(STRIPE_CFG)
                plan = body.get("plan") or "verified"
                if plan not in ("verified", "verified_plus"):
                    conn.close()
                    return self.json_out(400, {"error": "plan must be verified "
                                                      "or verified_plus"})
                if not cfg.get("STRIPE_SECRET_KEY") or \
                        not cfg.get("STRIPE_PRICE_FOUNDING"):
                    conn.close()
                    return self.json_out(503, {
                        "error": "payments not enabled yet",
                        "note": "Verified subscriptions open with Trust v0.2 "
                                "billing rollout. Verification stays free."})
                if plan == "verified_plus" and not cfg.get("STRIPE_PRICE_PLUS"):
                    conn.close()
                    return self.json_out(503, {
                        "error": "Verified+ not on sale yet",
                        "note": "Verified+ monthly plan opens in a later rollout."})
                email = (body.get("email") or "").strip().lower()
                agent = bool(plan == "verified_plus" and not vid)
                if agent:
                    # API-plan subscription for agents: email + account, no vid
                    acct = conn.execute(
                        "SELECT email FROM account_auth WHERE email=?",
                        (email,)).fetchone()
                    if not acct:
                        conn.close()
                        return self.json_out(404, {
                            "error": "no account for this email",
                            "note": "register at /account.html (free), create "
                                    "an API key, then subscribe"})
                    if not cfg.get("STRIPE_PRICE_PLUS"):
                        conn.close()
                        return self.json_out(503, {
                            "error": "Verified+ not on sale yet"})
                    cur = conn.execute(
                        "INSERT INTO account_plans(email,plan,status,created_at) "
                        "VALUES(?,?,'incomplete',?)", (email, plan, ts()))
                    ref = cur.lastrowid
                    conn.commit()
                    sess = stripe_post("/checkout/sessions", {
                        "mode": "subscription",
                        "locale": "en",
                        "success_url": "https://trust.paycensus.com/account.html?paid=1",
                        "cancel_url": "https://trust.paycensus.com/account.html?canceled=1",
                        "customer_email": email,
                        "metadata[email]": email,
                        "metadata[plan]": "verified_plus",
                        "metadata[acct]": "1",
                        "line_items[0][price]": cfg["STRIPE_PRICE_PLUS"],
                        "line_items[0][quantity]": "1"})
                    if not sess or sess.get("url") is None:
                        conn.execute("DELETE FROM account_plans WHERE rowid=?",
                                     (ref,))
                        conn.commit()
                        conn.close()
                        return self.json_out(502, {"error": "stripe checkout failed"})
                    conn.execute("UPDATE account_plans SET stripe_session_id=? "
                                 "WHERE rowid=?", (sess["id"], ref))
                    conn.commit()
                    conn.close()
                    return self.json_out(200, {
                        "checkout_url": sess["url"],
                        "plan": "verified_plus",
                        "price_usd": 49,
                        "note": "On activation your account's API keys lift to "
                                "100k factor calls/day. Invoices automatic."})
                if plan == "verified_plus":
                    founding = 0
                    price = cfg["STRIPE_PRICE_PLUS"]
                    cents = 4900
                else:
                    taken = founding_taken(conn)
                    founding = 1 if taken < FOUNDING_TOTAL else 0
                    price = cfg["STRIPE_PRICE_FOUNDING"] if founding \
                        else cfg.get("STRIPE_PRICE_LIST",
                                     cfg["STRIPE_PRICE_FOUNDING"])
                    cents = FOUNDING_PRICE_CENTS if founding else LIST_PRICE_CENTS
                cur = conn.execute(
                    "INSERT INTO subscriptions(vid,addr,domain,plan,founding,"
                    "price_cents,status,contact,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (vid, r["addr"], r["domain"], plan, founding, cents,
                     "incomplete", r["contact"] or "", ts()))
                sub_id_row = cur.lastrowid
                conn.commit()
                sess = stripe_post("/checkout/sessions", {
                    "mode": "subscription",
                    "locale": "en",
                    "success_url": "https://trust.paycensus.com/verify.html?paid=1",
                    "cancel_url": "https://trust.paycensus.com/verify.html?canceled=1",
                    "client_reference_id": str(vid),
                    "metadata[vid]": str(vid),
                    "metadata[sub_row]": str(sub_id_row),
                    "line_items[0][price]": price,
                    "line_items[0][quantity]": "1",
                })
                if not sess or sess.get("url") is None:
                    conn.execute("DELETE FROM subscriptions WHERE id=?",
                                 (sub_id_row,))
                    conn.commit()
                    conn.close()
                    return self.json_out(502, {"error": "stripe checkout failed"})
                conn.execute("UPDATE subscriptions SET stripe_session_id=? "
                             "WHERE id=?", (sess["id"], sub_id_row))
                conn.commit()
                remaining = max(FOUNDING_TOTAL - founding_taken(conn) - founding, 0) \
                    if plan == "verified" else None
                conn.close()
                return self.json_out(200, {
                    "checkout_url": sess["url"],
                    "plan": plan,
                    "founding": bool(founding),
                    "price_usd": cents // 100,
                    "founding_remaining": remaining,
                    "note": "Subscription renews; invoices issued "
                            "automatically by Stripe. Founding price is locked "
                            "for life once active."})
            if path == "/api/account/code":
                email = (body.get("email") or "").strip().lower()
                send_acct_code(ip, email)
                return self.json_out(200, {
                    "ok": True,
                    "note": "a 6-digit code is on its way"})
            if path == "/api/account/register":
                email = (body.get("email") or "").strip().lower()
                code = (body.get("code") or "").strip()
                pw = body.get("password") or ""
                if len(pw) < 8:
                    return self.json_out(400, {"error": "password must be at least 8 characters"})
                row = conn.execute(
                    "SELECT code FROM email_codes WHERE email=? AND "
                    "purpose='register' AND expires_at>?", (email, ts())).fetchone()
                if not email or not row or row["code"] != code:
                    return self.json_out(400, {"error": "invalid or expired code"})
                conn.execute(
                    "DELETE FROM email_codes WHERE email=? AND purpose='register'",
                    (email,))
                conn.execute(
                    "INSERT INTO account_auth(email,password_hash,created_at,"
                    "last_login) VALUES(?,?,?,?) ON CONFLICT(email) DO UPDATE "
                    "SET password_hash=excluded.password_hash, "
                    "last_login=excluded.last_login",
                    (email, pw_hash(pw), ts(), ts()))
                conn.commit()
                return self.json_out(200, {"ok": True, "email": email,
                                           "token": acct_token(email)})
            if path == "/api/account/login":
                email = (body.get("email") or "").strip().lower()
                pw = body.get("password") or ""
                row = conn.execute(
                    "SELECT password_hash FROM account_auth WHERE email=?",
                    (email,)).fetchone()
                if not email or not row or not pw_ok(pw, row["password_hash"]):
                    return self.json_out(401, {"error": "invalid email or password"})
                conn.execute("UPDATE account_auth SET last_login=? WHERE email=?",
                             (ts(), email))
                conn.commit()
                return self.json_out(200, {"ok": True, "email": email,
                                           "token": acct_token(email)})
            if path == "/api/account/password":
                email = acct_email(self)
                if not email:
                    return self.json_out(401, {"error": "login required"})
                old = body.get("old_password") or ""
                new = body.get("new_password") or ""
                row = conn.execute(
                    "SELECT password_hash FROM account_auth WHERE email=?",
                    (email,)).fetchone()
                if not row or not pw_ok(old, row["password_hash"]):
                    return self.json_out(401, {"error": "current password incorrect"})
                if len(new) < 8:
                    return self.json_out(400, {"error": "new password must be at least 8 characters"})
                conn.execute("UPDATE account_auth SET password_hash=? WHERE email=?",
                             (pw_hash(new), email))
                conn.commit()
                return self.json_out(200, {"ok": True})
            if path == "/api/keys/create":
                label = (body.get("label") or "")[:60]
                contact = (body.get("contact") or "")[:120]
                raw = "pct_live_" + secrets.token_hex(24)
                kh = hashlib.sha256(raw.encode()).hexdigest()
                owner = acct_email(self)
                conn.execute(
                    "INSERT INTO api_keys(key_hash,label,created_at,owner) "
                    "VALUES(?,?,?,?)", (kh, label, ts(), owner))
                conn.commit()
                n_keys = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
                conn.close()
                return self.json_out(200, {
                    "key": raw,
                    "note": "Store this key now — it is shown once and stored "
                            "only as a hash. Free tier: 10 trust/factor calls/day + 1,000 agent calls/day. "
                            f"Key serial #{n_keys}."})
            if path == "/api/admin/config":
                # nginx gates this path with basic auth (.htpasswd_ops)
                alias = {"publish_key": "STRIPE_PUBLISH_KEY",
                         "restricted_key": "STRIPE_SECRET_KEY",
                         "webhook_secret": "STRIPE_WEBHOOK_SECRET",
                         "price_founding": "STRIPE_PRICE_FOUNDING",
                         "price_list": "STRIPE_PRICE_LIST"}
                updates = {alias[k]: v for k, v in body.items() if k in alias}
                if not updates:
                    conn.close()
                    return self.json_out(400, {
                        "error": "nothing to update",
                        "fields": list(alias)})
                if "STRIPE_SECRET_KEY" in updates:
                    code = _stripe_probe_key(updates["STRIPE_SECRET_KEY"])
                    if code != 200:
                        conn.close()
                        return self.json_out(400, {
                            "error": "restricted key rejected by Stripe "
                                     f"(HTTP {code}) — nothing saved"})
                ok, err = cfg_write(updates)
                conn.close()
                if not ok:
                    return self.json_out(500, {"error": err})
                return self.json_out(200, {
                    "ok": True, "updated": list(alias[k] for k in body if k in alias),
                    "config": cfg_masked(),
                    "note": "run self-test to verify the new configuration"})
            if path == "/api/admin/xkeys":
                alias = {"x_api_key": "api_key", "x_api_secret": "api_secret",
                         "x_access_token": "access_token",
                         "x_access_secret": "access_secret"}
                updates = {alias[k]: v for k, v in body.items() if k in alias}
                if not updates:
                    conn.close()
                    return self.json_out(400, {
                        "error": "nothing to update", "fields": list(alias)})
                ok, err = x_write(updates)
                conn.close()
                if not ok:
                    return self.json_out(500, {"error": err})
                xres = x_selfcheck()
                return self.json_out(200, {
                    "ok": True, "config": x_masked(),
                    "live_check": {"ok": xres["ok"], "detail": xres["detail"]},
                    "note": "live verification ran immediately — next daily "
                            "post will use the new credentials"})
            if path == "/api/admin/orkeys":
                tok = (body.get("or_token") or "").strip()
                if not tok:
                    conn.close()
                    return self.json_out(400, {"error": "or_token required"})
                ok, err = or_write(tok)
                conn.close()
                if not ok:
                    return self.json_out(500, {"error": err})
                res = or_selfcheck()
                return self.json_out(200, {
                    "ok": True, "config": or_masked(),
                    "live_check": {"ok": res["ok"], "detail": res["detail"]},
                    "note": "saved to /opt/paycensus/.openrouter_token (0600) "
                            "— next commentary slot (13:40/21:10 UTC) uses it"})
            if path == "/api/admin/refund":
                try:
                    sub_id = int(body.get("sub_id") or 0)
                except ValueError:
                    sub_id = 0
                if not sub_id:
                    conn.close()
                    return self.json_out(400, {"error": "sub_id required"})
                cents = body.get("amount_cents")
                cents = int(cents) if cents else None
                code, out = admin_refund(conn, sub_id, cents)
                return self.json_out(code, out)
            conn.close()
            return self.json_out(404, {"error": "not found"})
        except Exception as ex:  # noqa: BLE001
            return self.json_out(500, {"error": str(ex)[:200]})


if __name__ == "__main__":
    argv = __import__("sys").argv
    if len(argv) > 1 and argv[1] == "reverify":
        reverify_all()
    elif len(argv) > 1 and argv[1] == "backfill-invoices":
        backfill_invoices()
    else:
        srv = ThreadingHTTPServer(("127.0.0.1", 8788), Handler)
        print(f"paycensus-trust v{VERSION} on 127.0.0.1:8788")
        srv.serve_forever()
