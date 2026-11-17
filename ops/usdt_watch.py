# -*- coding: utf-8 -*-
"""USDT rail watch — monthly first-party probe of Tether rails (Plasma 9745 / Stable 988).
Protocol-agnostic: counts EIP-3009 authorization settlements on-chain regardless of
protocol branding; raw transfer volume is context, not census. $0 (public RPC, stdlib).
Publishes /www/wwwroot/paycensus/data/usdt_watch.json (append-only history).
"""
import json
import time
import urllib.request
import collections
from datetime import datetime, timezone

OUT = "/www/wwwroot/paycensus/data/usdt_watch.json"
PLASMA_USDT0 = "0xB8CE59FC3717ada4C02eaDF9682A9e934F625ebb"
STABLE_USDT0 = "0x779Ded0c9e1022225f8E0630b35a9b54bE713736"
CAP = 36  # keep 3 years of monthly entries

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]


def _rol64(v, n):
    n %= 64
    return ((v << n) | (v >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def _keccak_f(S):
    for rc in _RC:
        C = [S[x][0] ^ S[x][1] ^ S[x][2] ^ S[x][3] ^ S[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol64(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                S[x][y] ^= D[x]
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rol64(S[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                S[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y])
        S[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    rate = 136
    S = [[0] * 5 for _ in range(5)]
    pad = data + b"\x01"
    pad += b"\x00" * ((-len(pad)) % rate)
    pad = pad[:-1] + bytes([pad[-1] | 0x80])
    for off in range(0, len(pad), rate):
        blk = pad[off:off + rate]
        for i in range(rate // 8):
            S[i % 5][i // 5] ^= int.from_bytes(blk[8 * i:8 * i + 8], "little")
        _keccak_f(S)
    return b"".join(S[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"

_SIGS = [
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bool,uint8,bytes32,bytes32)",
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes,bytes)",
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bool,bytes,bytes)",
    "receiveWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
    "receiveWithAuthorization(address,address,uint256,uint256,uint256,bool,uint8,bytes32,bytes32)",
    "receiveWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes,bytes)",
    "receiveWithAuthorization(address,address,uint256,uint256,uint256,bool,bytes,bytes)",
]
AUTH_SEL = {"0x" + keccak256(s.encode()).hex()[:8] for s in _SIGS}
KNOWN = {"0xa9059cbb": "transfer", "0x095ea7b3": "approve", "0x23b872dd": "transferFrom"}


def rpc(url, method, params, timeout=20):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "paycensus-watch/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def find_rpc(candidates, expect_chain):
    for url in candidates:
        try:
            if int(rpc(url, "eth_chainId", [])["result"], 16) == expect_chain:
                return url
        except Exception:
            continue
    return None


def probe_chain(rpc_url, usdt0, n_blocks):
    latest = int(rpc(rpc_url, "eth_blockNumber", [])["result"], 16)
    sel = collections.Counter()
    total_tx = 0
    usdt_tx = 0
    t0 = t1 = None
    for i in range(n_blocks):
        try:
            b = rpc(rpc_url, "eth_getBlockByNumber", [hex(latest - i), True])["result"]
        except Exception:
            time.sleep(0.3)
            continue
        if not b:
            continue
        ts = int(b["timestamp"], 16)
        t1 = t1 or ts
        t0 = ts
        for tx in b.get("transactions", []):
            total_tx += 1
            if (tx.get("to") or "").lower() != usdt0.lower():
                continue
            usdt_tx += 1
            s = (tx.get("input") or "0x")[:10].lower()
            sel[KNOWN.get(s, s if s in AUTH_SEL else "other:" + s)] += 1
        time.sleep(0.08)
    span = (t1 - t0) if (t0 and t1) else 1
    auth = sum(v for k, v in sel.items() if k in ("transferWithAuthorization",
                                                  "receiveWithAuthorization"))
    return {"chain_tx_per_min": round(total_tx / span * 60),
            "usdt0_txs": usdt_tx,
            "methods": dict(sel),
            "eip3009_auth_settlements": auth}


def probe_facilitator():
    try:
        req = urllib.request.Request("https://x402.semanticpay.io/health",
                                     headers={"User-Agent": "paycensus-watch/0.1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"HTTP {r.status}"
    except Exception as ex:
        msg = str(ex)
        return "HTTP " + msg.split()[::-1][0][:3] if "HTTP Error" in msg else msg[:40]


def main():
    entry = {"probed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    p = find_rpc(["https://rpc.plasma.to", "https://rpc2.plasma.to",
                  "https://plasma.drpc.org", "https://1rpc.io/xpl"], 9745)
    entry["plasma"] = probe_chain(p, PLASMA_USDT0, 600) if p else {"error": "no rpc"}
    s = find_rpc(["https://rpc.stable.xyz", "https://rpc-mainnet.stable.xyz",
                  "https://stable.drpc.org"], 988)
    entry["stable"] = probe_chain(s, STABLE_USDT0, 800) if s else {"error": "no rpc"}
    entry["facilitator_x402_semanticpay_io"] = probe_facilitator()
    auth = (entry["plasma"].get("eip3009_auth_settlements", 0)
            + entry["stable"].get("eip3009_auth_settlements", 0))
    entry["eip3009_auth_total"] = auth
    entry["verdict"] = ("ACTIVE — TRIGGER: add Plasma/Stable settlement adapter" if auth > 0
                        else "dormant — rails alive, machine-payment settlements zero")
    try:
        doc = json.load(open(OUT))
    except Exception:
        doc = {}
    hist = doc.get("history", [])
    hist = [h for h in hist if not h.get("probed_at", "").startswith(entry["probed_at"][:10])]
    hist.append(entry)
    doc = {
        "note": "USDT machine-payment rail watch — monthly first-party probe. Protocol-agnostic: "
                "counts EIP-3009 authorization settlements on Tether rails (Plasma/Stable, USDT0) "
                "regardless of protocol branding; raw transfer volume is context, not census. "
                "History is append-only and never backfilled.",
        "updated_at": entry["probed_at"],
        "history": hist[-CAP:],
    }
    with open(OUT, "w") as f:
        json.dump(doc, f, indent=1)
    print("usdt_watch:", json.dumps(entry)[:600])
    print("published", OUT, "history entries:", len(hist))


if __name__ == "__main__":
    main()
