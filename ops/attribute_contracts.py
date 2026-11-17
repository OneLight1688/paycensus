#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase A: classify seller payTo addresses (txs>=21) as contract/EOA,
read EIP-1967 impl slot for proxies, attempt Basescan title for names.
Resume-capable: rows with kind already set are skipped.

Writes census.db sellers.kind ('contract'|'eoa') + sellers.impl.
Run under nohup; safe to re-run. RPC pool rotates. ~0.15s/addr.
"""
import json
import sqlite3
import time
import urllib.request

DB = "/var/lib/paycensus/census.db"
RPCS = ["https://base.llamarpc.com", "https://1rpc.io/base",
        "https://base.drpc.org", "https://mainnet.base.org"]
IMPL_SLOT = ("0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca"
             "505d382bbc")
UA = {"Content-Type": "application/json",
      "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126"}
BATCH_SLEEP = 0.12


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    last = None
    for u in RPCS:
        try:
            r = urllib.request.Request(u, data=body, headers=UA)
            with urllib.request.urlopen(r, timeout=15) as resp:
                return json.loads(resp.read()).get("result")
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def basescan_title(addr):
    """Best-effort: fetch basescan address page title (browser UA)."""
    try:
        r = urllib.request.Request(
            f"https://basescan.org/address/{addr}",
            headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(r, timeout=15) as resp:
            html = resp.read(20000).decode(errors="replace")
        i = html.find("<title>")
        j = html.find("</title>")
        if 0 <= i < j:
            t = html[i + 7:j].strip()
            return t[:120] if t else None
    except Exception:  # noqa: BLE001
        return None
    return None


def main():
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    cols = [c[1] for c in conn.execute("PRAGMA table_info(sellers)")]
    if "kind" not in cols:
        conn.execute("ALTER TABLE sellers ADD COLUMN kind TEXT")
    if "impl" not in cols:
        conn.execute("ALTER TABLE sellers ADD COLUMN impl TEXT")
    if "label" not in cols:
        conn.execute("ALTER TABLE sellers ADD COLUMN label TEXT")
    conn.commit()

    rows = conn.execute(
        "SELECT addr, txs FROM sellers WHERE txs>=21 AND "
        "(kind IS NULL OR kind='') ORDER BY volume DESC").fetchall()
    todo = len(rows)
    print(f"to classify: {todo} addresses (txs>=21)", flush=True)
    done = contracts = 0
    for r in rows:
        a = r["addr"]
        try:
            code = rpc("eth_getCode", [a, "latest"]) or "0x"
        except Exception as e:  # noqa: BLE001
            print(f"  rpc fail {a[:10]}…: {str(e)[:60]}", flush=True)
            time.sleep(1.0)
            continue
        kind, impl, label = "eoa", None, None
        if code != "0x":
            kind = "contract"
            try:
                slot = rpc("eth_getStorageAt", [a, IMPL_SLOT, "latest"])
                if slot and int(slot, 16):
                    impl = "0x" + slot[-40:]
            except Exception:  # noqa: BLE001
                pass
            label = basescan_title(a)
            contracts += 1
        conn.execute("UPDATE sellers SET kind=?, impl=?, label=? WHERE addr=?",
                     (kind, impl, label, a))
        conn.commit()
        done += 1
        if done % 25 == 0:
            print(f"  progress {done}/{todo} (contracts so far {contracts})",
                  flush=True)
        time.sleep(BATCH_SLEEP)
    print(f"DONE classified {done}/{todo}, contracts={contracts}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
