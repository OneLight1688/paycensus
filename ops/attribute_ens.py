#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase B v2: ENS/Basename reverse lookup for census sellers.

Fixes vs v1 (which livelocked):
- 429/403 retry capped per address (3 rounds -> 'ERR429', move on)
- source circuit breaker: source 429ing 5x consecutively is disabled 10 min
- global rate limiter: >=0.5s between any two outbound requests
- faulthandler stack dump on SIGUSR1 for diagnosis
Writes sellers.ens ('' = checked-no-name, ERR* = source errors). Resumable.
"""
import faulthandler
import json
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request

DB = "/var/lib/paycensus/census.db"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126",
      "Accept": "application/json"}
THREADS = 3
MIN_GAP = 0.5

faulthandler.register(signal.SIGUSR1)
_lock = threading.Lock()
_next_ok = [0.0]           # global rate limiter
_src_down = {"ensdata": 0.0, "ensideas": 0.0}   # circuit breakers (until ts)
_src_429 = {"ensdata": 0, "ensideas": 0}


def _wait_slot():
    while True:
        with _lock:
            now = time.time()
            if now >= _next_ok[0]:
                _next_ok[0] = now + MIN_GAP
                return
            w = _next_ok[0] - now
        time.sleep(w)


def get(url, timeout=10):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode(errors="replace"))


def try_source(src, url):
    """Return (name|''|None) — None means source unavailable (skip)."""
    if src in _src_down and time.time() < _src_down[src]:
        return None
    try:
        d = get(url)
        _src_429[src] = 0
        return (d.get("name") or d.get("ens") or "").strip().lower()
    except urllib.error.HTTPError as e:
        if e.code in (429, 403):
            _src_429[src] += 1
            if _src_429[src] >= 5:
                _src_down[src] = time.time() + 600
                print(f"  [breaker] {src} down 10min ({_src_429[src]}x429)",
                      flush=True)
            raise
        if e.code == 404:
            _src_429[src] = 0
            return ""
        raise


def reverse_lookup(addr):
    for attempt in range(3):
        # enstate first (404 = no name; enstate has no breaker, it's healthy)
        _wait_slot()
        try:
            d = get(f"https://enstate.rs/u/{addr}")
            n = (d.get("name") or "").strip().lower()
            return n if n else ""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pass  # no name on enstate -> try fallbacks
            elif e.code in (429, 403):
                if attempt == 2:
                    return "ERR429"
                time.sleep(25 * (attempt + 1))
                continue
            else:
                return f"ERR{e.code}"
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return "ERRNET"
            time.sleep(5)
            continue
        # fallbacks (404 at enstate)
        got_429 = False
        for src, url in (("ensdata", f"https://ensdata.net/{addr}"),
                         ("ensideas",
                          f"https://api.ensideas.com/ens/resolve/{addr}")):
            _wait_slot()
            try:
                n = try_source(src, url)
                if n is None:
                    continue  # breaker open
                return n if n else ""
            except urllib.error.HTTPError as e:
                if e.code in (429, 403):
                    got_429 = True
                continue
            except Exception:  # noqa: BLE001
                continue
        if got_429:
            if attempt == 2:
                return "ERR429"
            time.sleep(25 * (attempt + 1))
            continue
        return ""
    return "ERR"


def worker(chunk, wid):
    conn = sqlite3.connect(DB, timeout=60)
    for i, (addr,) in enumerate(chunk):
        name = reverse_lookup(addr)
        conn.execute("UPDATE sellers SET ens=? WHERE addr=?", (name, addr))
        conn.commit()
        if (i + 1) % 25 == 0:
            print(f"  t{wid}: {i+1}/{len(chunk)}", flush=True)
    conn.close()


def main():
    conn = sqlite3.connect(DB, timeout=60)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(sellers)")]
    if "ens" not in cols:
        conn.execute("ALTER TABLE sellers ADD COLUMN ens TEXT")
        conn.commit()
    rows = conn.execute("SELECT addr FROM sellers WHERE ens IS NULL "
                        "ORDER BY volume DESC").fetchall()
    conn.close()
    total = len(rows)
    print(f"Phase B v2: {total} addresses to go (resume; ERR rows skipped: "
          f"re-run with reset later if needed)", flush=True)
    if not total:
        print("DONE", flush=True)
        return
    k = max(1, -(-total // THREADS))
    chunks = [rows[i * k:(i + 1) * k] for i in range(THREADS)]
    ts = [threading.Thread(target=worker, args=(c, i), daemon=True)
          for i, c in enumerate(chunks) if c]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    c2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    found = c2.execute("SELECT COUNT(*) FROM sellers WHERE ens IS NOT NULL "
                       "AND ens != '' AND ens NOT LIKE 'ERR%'").fetchone()[0]
    errs = c2.execute("SELECT COUNT(*) FROM sellers WHERE ens LIKE 'ERR%'").fetchone()[0]
    c2.close()
    print(f"DONE ens_found={found} errors={errs}", flush=True)


if __name__ == "__main__":
    main()
