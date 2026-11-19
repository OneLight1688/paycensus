#!/usr/bin/env python3
"""PayCensus dogfood — our own agent buys from top-ranked merchants via x402.

Subcommands:
  wallet            create (once) / show the dogfood wallet address + USDC balance
  probe [N]         real verification WITHOUT spending: take top-N ranked
                    real_commerce merchants, live-GET their resources, record
                    the 402 payment requirements, cross-check payTo against
                    our census attribution, write /tmp/dogfood_probe.json
  pay <resource>    execute a real x402 payment for <resource> (requires the
                    wallet to hold USDC on Base); self-limits to <= $0.50
Private key lives only in /opt/paycensus/.dogfood_wallet (0600). Never logged.
"""
import base64, json, os, secrets, sys, time
import urllib.request, urllib.error
from eth_keys import keys
from eth_utils import keccak

WALLET = "/opt/paycensus/.dogfood_wallet"
RANK = "/www/wwwroot/paycensus/data/rank.json"
BAZAAR = "/var/lib/paycensus/bazaar_resources.json"
CENSUS = "/var/lib/paycensus/census.db"
OUT = "/www/wwwroot/paycensus/data/dogfood_probe.json"
BASE_RPC = "https://mainnet.base.org"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CHAIN_ID = 8453
MAX_SPEND_USD = 0.50
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) PayCensusDogfood/0.1"}

def wallet():
    if os.path.exists(WALLET):
        raw = open(WALLET).read().strip().removeprefix("0x")
        pk = keys.PrivateKey(bytes.fromhex(raw))
    else:
        pk = keys.PrivateKey(secrets.token_bytes(32))
        with open(WALLET, "w") as f:
            f.write(pk.to_hex().removeprefix("0x"))
        os.chmod(WALLET, 0o600)
    return pk, pk.public_key.to_checksum_address()

RPCS = ["https://mainnet.base.org", "https://base.publicnode.com",
        "https://base.drpc.org", "https://1rpc.io/base"]

def rpc(method, params):
    last = None
    for u in RPCS:
        try:
            req = urllib.request.Request(u, data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": method,
                 "params": params}).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": UA["User-Agent"]})
            return json.loads(urllib.request.urlopen(req, timeout=15).read())
        except Exception as ex:  # noqa: BLE001
            last = ex
    raise last

def usdc_balance(addr):
    data = "0x70a08231" + addr.lower()[2:].rjust(64, "0")
    r = rpc("eth_call", [{"to": USDC_BASE, "data": data}, "latest"])
    return int(r.get("result", "0x0"), 16) / 1e6

def fetch(url, headers=None, timeout=20):
    h = dict(UA)
    h["Accept-Encoding"] = "identity"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()[:65536]
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()[:65536]
    except Exception as e:
        return -1, {}, repr(e).encode()

def cmd_wallet():
    pk, addr = wallet()
    bal = usdc_balance(addr)
    eth = int(rpc("eth_getBalance", [addr, "latest"])["result"], 16) / 1e18
    print("dogfood wallet address:", addr)
    print("USDC(base): $%.6f   ETH: %.8f" % (bal, eth))
    if bal == 0:
        print("-> fund this address with a few USDC on Base to enable 'pay'")
    # self-test: EIP-191 recovery round-trip proves the key material is sane
    m = keccak(b"paycensus-dogfood-selftest")
    sig = pk.sign_msg_hash(m)
    rec = keys.Signature(sig[:65]).recover_public_key_from_msg_hash(m)
    assert rec.to_checksum_address() == addr, "selftest failed"
    print("key selftest: OK (recovery round-trip)")

def pick_targets(n):
    # Triple-evidence targets: Bazaar-listed (has resource URL) AND
    # on-chain observed in our census AND classified real_commerce.
    rank = {r["addr"].lower(): r for r in json.load(open(RANK))
            .get("ranked", [])}
    bazaar = json.load(open(BAZAAR))
    import sqlite3
    con = sqlite3.connect("file:%s?mode=ro" % CENSUS, uri=True)
    seen = {a for (a,) in con.execute("SELECT addr FROM sellers")}
    con.close()
    cands = []
    for res in bazaar:
        if not res.get("resource"):
            continue
        for a in res.get("payTo", []):
            if a not in seen:
                continue
            rk = rank.get(a.lower())
            cands.append({
                "addr": a, "score": rk.get("score") if rk else None,
                "cls": rk.get("class") if rk else None,
                "verified": rk.get("verified") if rk else None,
                "addr_proven": rk.get("addr_proven") if rk else None,
                "volume": rk.get("volume") if rk else None,
                "buyers": rk.get("buyers") if rk else None,
                "resource": res.get("resource"),
                "bazaar_payers30": res.get("payers30", 0),
                "onchain_observed": True})
    cands.sort(key=lambda c: (
        c["cls"] != "real_commerce",
        c["score"] is None,
        -(c["score"] or 0),
        -c["bazaar_payers30"]))
    return cands[:int(n)]

def cmd_probe(n=5):
    targets = pick_targets(int(n))
    results = []
    for t in targets:
        st, hdrs, body = fetch(t["resource"])
        entry = dict(t)
        entry["http_status"] = st
        entry["serves_402"] = st == 402
        entry["price_usd"] = None
        entry["payTo"] = None
        entry["network"] = None
        entry["payto_matches_our_addr"] = None
        if st == 402:
            entry["body_preview"] = body.decode("utf-8", "replace")[:200]
            try:
                bodyj = json.loads(body.decode("utf-8", "replace"))
                accs = bodyj.get("accepts")
                pr = bodyj.get("paymentRequired")
                if not isinstance(accs, list) or not accs:
                    if isinstance(pr, dict):
                        accs = [pr]
                    elif isinstance(pr, list):
                        accs = pr
                    else:
                        xv = bodyj.get("x402Versions")
                        accs = xv if isinstance(xv, list) else []
                if accs:
                    a = accs[0]
                    entry["x402_version"] = bodyj.get("x402Version")
                    entry["payTo"] = a.get("payTo")
                    entry["network"] = a.get("network")
                    mx = a.get("maxAmountRequired") or a.get("amount")
                    if mx:
                        entry["price_usd"] = round(int(mx) / 1e6, 4)
                    entry["asset"] = a.get("asset") or a.get("extra")
                    entry["payto_matches_our_addr"] = (
                        (a.get("payTo") or "").lower() == t["addr"].lower())
                else:
                    entry["parse_error"] = "no accepts; error=%s" % str(
                        bodyj.get("error"))[:80]
            except Exception as e:
                entry["parse_error"] = repr(e)[:120]
        results.append(entry)
        time.sleep(1)
    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M UTC",
                                            time.gmtime()),
              "count": len(results), "results": results}
    with open(OUT, "w") as f:
        json.dump(report, f, indent=1)
    for r in results:
        print("%-60s -> %s price=%s payTo_match=%s" % (
            (r.get("resource") or "")[:60], r["http_status"],
            r.get("price_usd"), r.get("payto_matches_our_addr")))
    ok = sum(1 for r in results if r.get("serves_402"))
    match = sum(1 for r in results if r.get("payto_matches_our_addr"))
    print("probe: %d/%d serve real 402; %d/%d payTo == our census addr"
          % (ok, len(results), match, len(results)))
    print("report ->", OUT)

# ---- EIP-3009 (TransferWithAuthorization) for USDC on Base ----

DOMAIN_TYPEHASH = keccak(b"EIP712Domain(string name,string version,"
                         b"uint256 chainId,address verifyingContract)")
AUTH_TYPEHASH = keccak(b"TransferWithAuthorization(address from,address to,"
                       b"uint256 value,uint256 validAfter,uint256 validBefore,"
                       b"bytes32 nonce)")
DOMAIN_SEP = keccak(DOMAIN_TYPEHASH + keccak(b"USD Coin") + keccak(b"2")
                    + CHAIN_ID.to_bytes(32, "big")
                    + bytes.fromhex(USDC_BASE[2:].rjust(64, "0")))

def build_payment(pk, from_addr, to_addr, value_units, nonce=None):
    now = int(time.time())
    nonce = nonce or secrets.token_bytes(32)
    sh = (AUTH_TYPEHASH
          + bytes.fromhex(from_addr[2:].lower().rjust(64, "0"))
          + bytes.fromhex(to_addr[2:].lower().rjust(64, "0"))
          + int(value_units).to_bytes(32, "big")
          + int(now - 60).to_bytes(32, "big")
          + int(now + 600).to_bytes(32, "big")
          + nonce)
    digest = keccak(b"\x19\x01" + DOMAIN_SEP + sh)
    sig = pk.sign_msg_hash(digest)
    r, s, v = sig[:32], sig[32:64], sig[64] + 27
    payload = {
        "x402Version": 1, "scheme": "exact", "network": "base",
        "payload": {
            "signature": "0x" + (r + s + bytes([v])).hex(),
            "authorization": {
                "from": from_addr, "to": to_addr,
                "value": str(int(value_units)),
                "validAfter": str(now - 60),
                "validBefore": str(now + 600),
                "nonce": "0x" + nonce.hex()}}}
    return payload

def cmd_pay(resource):
    pk, addr = wallet()
    bal = usdc_balance(addr)
    print("wallet %s USDC=$%.6f" % (addr, bal))
    if bal <= 0:
        print("wallet has no USDC — fund it first; aborting (no real spend)")
        return
    st, hdrs, body = fetch(resource)
    if st != 402:
        print("resource did not answer 402 (got %s) — abort" % st)
        return
    accs = json.loads(body.decode("utf-8", "replace")).get("accepts") or []
    if not accs:
        print("no accepts in 402 — abort")
        return
    a = accs[0]
    value = int(a["maxAmountRequired"])
    if value / 1e6 > MAX_SPEND_USD:
        print("price $%.4f exceeds self-limit $%.2f — abort"
              % (value / 1e6, MAX_SPEND_USD))
        return
    pay_payload = build_payment(pk, addr, a["payTo"], value)
    hdr = {"X-PAYMENT": base64.b64encode(
        json.dumps(pay_payload).encode()).decode()}
    st2, hdrs2, body2 = fetch(resource, headers=hdr, timeout=60)
    print("paid GET -> HTTP", st2)
    resp_hdr = hdrs2.get("X-PAYMENT-RESPONSE") or hdrs2.get("x-payment-response")
    if resp_hdr:
        try:
            settle = json.loads(base64.b64decode(resp_hdr))
            print("settlement:", json.dumps(settle)[:300])
        except Exception:
            print("settlement header unparseable")
    print("body preview:", body2.decode("utf-8", "replace")[:300])

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "wallet"
    if cmd == "wallet":
        cmd_wallet()
    elif cmd == "probe":
        cmd_probe(sys.argv[2] if len(sys.argv) > 2 else 5)
    elif cmd == "pay":
        if len(sys.argv) < 3:
            sys.exit("usage: dogfood.py pay <resource-url>")
        cmd_pay(sys.argv[2])
    else:
        sys.exit(__doc__)
