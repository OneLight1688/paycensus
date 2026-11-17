#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus read-only query API (v0) — exposes processed commercial info:
  /api/seller?addr=   seller view: totals, daily series, buyers (source of payments)
  /api/buyer?addr=    buyer view: totals, sellers paid (destination of payments)
  /api/edges          top buyer x seller edges (the graph)
  /api/claim          POST: entity profile claim/correction (directory.html)
  /api/ping           health
Read-only SQLite, parameterized queries, CORS *. Runs on 127.0.0.1:8787 behind nginx.
"""
import json
import re
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB = "/var/lib/paycensus/census.db"
ADDR_RE = re.compile(r"^(0x[a-fA-F0-9xX]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$")
CLAIM_LIMIT = 10          # max claims per IP per window
CLAIM_WINDOW = 24 * 3600  # seconds
_claim_hits = {}          # ip -> [timestamps] (in-memory; claims themselves are in sqlite)


def ensure_claims():
    try:
        conn = sqlite3.connect(DB, timeout=10)
        conn.execute("CREATE TABLE IF NOT EXISTS claims ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, entity TEXT, "
                     "contact TEXT, message TEXT, ip TEXT)")
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def db():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_optout():
    try:
        conn = sqlite3.connect(DB, timeout=10)
        conn.execute("CREATE TABLE IF NOT EXISTS opt_out (addr TEXT PRIMARY KEY, requested TEXT)")
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


ensure_optout()
ensure_claims()


def datetime_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def json_out(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/ping":
                return self.json_out(200, {"ok": True, "service": "paycensus-api"})
            if u.path == "/api/health":
                return self.health()
            if u.path == "/api/seller":
                return self.seller(q)
            if u.path == "/api/buyer":
                return self.buyer(q)
            if u.path == "/api/edges":
                return self.edges(q)
            self.json_out(404, {"error": "unknown endpoint", "endpoints": ["/api/seller?addr=", "/api/buyer?addr=", "/api/edges?limit=&min_volume=", "/api/health"]})
        except Exception as e:  # noqa: BLE001
            self.json_out(500, {"error": str(e)[:200]})

    def do_POST(self):
        """POST /api/claim — entity profile claim/correction (directory.html form).
        Honeypot + per-IP throttle + size caps; claims stored for manual verification."""
        try:
            if urlparse(self.path).path != "/api/claim":
                return self.json_out(404, {"error": "unknown endpoint"})
            ln = int(self.headers.get("Content-Length") or 0)
            if ln > 2048:
                return self.json_out(413, {"error": "payload too large"})
            body = json.loads(self.rfile.read(ln).decode("utf-8", "replace") or "{}")
            if body.get("hp"):  # honeypot: silently accept but ignore
                return self.json_out(200, {"ok": True})
            entity = str(body.get("entity") or "")[:120]
            contact = str(body.get("contact") or "")[:120]
            message = str(body.get("message") or "")[:500]
            if not contact or not message:
                return self.json_out(400, {"error": "contact and message are required"})
            ip = self.client_address[0]
            now = time.time()
            hits = [t for t in _claim_hits.get(ip, []) if now - t < CLAIM_WINDOW]
            if len(hits) >= CLAIM_LIMIT:
                return self.json_out(429, {"error": "too many claims from this address, try tomorrow"})
            hits.append(now)
            _claim_hits[ip] = hits
            conn = sqlite3.connect(DB, timeout=10)
            conn.execute("INSERT INTO claims (ts, entity, contact, message, ip) VALUES (?,?,?,?,?)",
                         (datetime_utc(), entity, contact, message, ip))
            conn.commit()
            conn.close()
            self.json_out(200, {"ok": True,
                                "note": "verification is manual; accepted corrections publish within 24h"})
        except Exception as e:  # noqa: BLE001
            self.json_out(500, {"error": str(e)[:200]})

    def do_OPTIONS(self):  # CORS preflight for the claim form
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def health(self):
        """Run /opt/paycensus/healthcheck.py --json (read-only diagnostics) and return its report.
        NOTE: this route is basic-auth protected at the nginx layer (same .htpasswd_ops as /ops.html)."""
        try:
            r = subprocess.run(["/usr/bin/python3", "/opt/paycensus/healthcheck.py", "--json"],
                               capture_output=True, text=True, timeout=120)
            report = json.loads(r.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return self.json_out(504, {"error": "healthcheck timed out (120s)"})
        except Exception as e:  # noqa: BLE001
            return self.json_out(500, {"error": f"healthcheck failed: {str(e)[:200]}"})
        report["served_by"] = "paycensus-api"
        return self.json_out(200, report)

    def parse_addr(self, q):
        a = (q.get("addr") or [""])[0].strip()
        if not ADDR_RE.match(a):
            return None
        return a

    def seller(self, q):
        a = self.parse_addr(q)
        if not a:
            return self.json_out(400, {"error": "invalid addr"})
        conn = db()
        tot = conn.execute(
            "SELECT addr,chain,txs,volume,first_day,last_day FROM sellers WHERE addr=? OR addr=?",
            (a, a.lower() if a.startswith("0x") else a)).fetchall()
        if not tot:
            conn.close()
            return self.json_out(404, {"error": "seller not found", "addr": a})
        chains = [dict(r) for r in tot]
        daily = [{"day": d, "txs": t, "volume": round(v, 2)} for d, t, v in conn.execute(
            "SELECT day,SUM(txs),SUM(volume) FROM daily_sellers WHERE addr=? OR addr=? GROUP BY day ORDER BY day",
            (a, a.lower() if a.startswith("0x") else a))]
        addr_variants = list({r["addr"] for r in tot} | {a.lower()} if a.startswith("0x") else {r["addr"] for r in tot})
        marks = ",".join("?" * len(addr_variants))
        buyers = [dict(r) for r in conn.execute(
            f"SELECT buyer,chain,txs,volume,first_day,last_day FROM edges WHERE seller IN ({marks}) ORDER BY volume DESC LIMIT 100",
            addr_variants)]
        conn.close()
        entity = None
        try:
            econn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
            opt = {r[0] for r in econn.execute("SELECT addr FROM opt_out")}
            erow = econn.execute(
                "SELECT domain,title,description,source,first_seen,category FROM entities WHERE addr=? OR addr=?",
                (a, a.lower() if a.startswith("0x") else a)).fetchone()
            econn.close()
            if erow and a not in opt and a.lower() not in opt:
                entity = dict(zip(["domain", "title", "description", "source", "first_seen", "category"], erow))
                try:
                    cc = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
                    has_ch = cc.execute(
                        "SELECT 1 FROM contacts WHERE addr IN (?,?) AND cls='role' LIMIT 1",
                        (a, a.lower() if a.startswith("0x") else a)).fetchone()
                    cc.close()
                    entity["contact_channel"] = bool(has_ch)
                except Exception:  # noqa: BLE001
                    entity["contact_channel"] = False
        except Exception:  # noqa: BLE001
            pass
        self.json_out(200, {
            "seller": a, "entity": entity, "chains": chains, "daily": daily, "buyers": buyers,
            "note": "buyer/seller derived on-chain (AuthorizationUsed x Transfer pairing). "
                    "chains totals are lifetime; daily series starts 2026-10-31 (daily table introduced then). "
                    "entity = domain/title/description from origins-catalog + 402 probing. "
                    "Daily census files are Bitcoin-anchored (OpenTimestamps; paycensus-verify)"})

    def buyer(self, q):
        a = self.parse_addr(q)
        if not a:
            return self.json_out(400, {"error": "invalid addr"})
        conn = db()
        tot = conn.execute(
            "SELECT addr,chain,txs,volume,first_day,last_day FROM buyers WHERE addr=? OR addr=?",
            (a, a.lower() if a.startswith("0x") else a)).fetchall()
        if not tot:
            conn.close()
            return self.json_out(404, {"error": "buyer not found", "addr": a})
        chains = [dict(r) for r in tot]
        addr_variants = list({r["addr"] for r in tot} | {a.lower()} if a.startswith("0x") else {r["addr"] for r in tot})
        marks = ",".join("?" * len(addr_variants))
        sellers = [dict(r) for r in conn.execute(
            f"SELECT seller,chain,txs,volume,first_day,last_day FROM edges WHERE buyer IN ({marks}) ORDER BY volume DESC LIMIT 100",
            addr_variants)]
        conn.close()
        self.json_out(200, {"buyer": a, "chains": chains, "sellers": sellers,
                            "note": "Daily census files are Bitcoin-anchored "
                                    "(OpenTimestamps; verify with paycensus-verify)"})

    def edges(self, q):
        try:
            lim = min(max(int((q.get("limit") or ["50"])[0]), 1), 500)
            mv = float((q.get("min_volume") or ["0"])[0])
        except ValueError:
            return self.json_out(400, {"error": "bad limit/min_volume"})
        conn = db()
        rows = conn.execute(
            "SELECT buyer,seller,chain,txs,volume,first_day,last_day FROM edges "
            "WHERE volume>=? ORDER BY volume DESC LIMIT ?", (mv, lim)).fetchall()
        conn.close()
        self.json_out(200, {"edges": [dict(r) for r in rows],
                            "note": "Daily census files are Bitcoin-anchored "
                                    "(OpenTimestamps; verify with paycensus-verify)"})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 8787), Handler)
    print("paycensus-api on 127.0.0.1:8787")
    srv.serve_forever()
