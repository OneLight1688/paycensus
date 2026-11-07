# -*- coding: utf-8 -*-
"""paycensus.verify — independent verifier for the PayCensus Proof-of-Integrity chain.

Re-computes everything from public endpoints; trusts no server state beyond the
published, Bitcoin-anchored artifacts themselves:

  1. chain walk   : every anchor's prev_digest must resolve back to GENESIS
  2. snapshot hash: sha256(published {label}.snapshot) == published digest
  3. header checks: label + prev_digest embedded in the snapshot must match
  4. embedded size: per-file sizes inside the snapshot must match the manifest
  5. optional ots : if the `ots` CLI is installed and --ots is passed, each
                    receipt is verified against Bitcoin block headers and the
                    attested height compared to the published one

Exit code 0 = all checks passed, 1 = any failure. Suitable for cron/CI.

Usage:
  python -m paycensus.verify [--base https://paycensus.com] [--days N] [--ots]
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

DEFAULT_BASE = "https://paycensus.com"
UA = {"User-Agent": "paycensus-verifier/0.3 (+https://paycensus.com)", "Accept": "application/json"}
GENESIS_PREFIX = "GENESIS"
FILES = ["stats.json", "calendar.json", "audit.json"]


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_snapshot(raw, entry, problems):
    """Check the published snapshot against its manifest entry."""
    head = raw.split(b"\n\n", 1)[0].decode("utf-8", "replace")
    m = re.search(r"PayCensus daily snapshot (\S+)", head)
    if not m or m.group(1) != entry["label"]:
        problems.append(f"{entry['label']}: header label mismatch")
    if f"prev_digest: {entry['prev_digest']}" not in head:
        problems.append(f"{entry['label']}: header prev_digest mismatch")
    body = raw.split(b"\n\n", 1)[1] if b"\n\n" in raw else b""
    for fn in FILES:
        mm = re.search(rf"===== {re.escape(fn)} \((\d+) bytes\) =====", body.decode("utf-8", "replace"))
        want = (entry.get("files_bytes") or {}).get(fn)
        if mm and want and int(mm.group(1)) != int(want):
            problems.append(f"{entry['label']}: {fn} embedded size {mm.group(1)} != manifest {want}")


def verify(base, days=None, use_ots=False):
    base = base.rstrip("/")
    anchors = json.loads(_get(base + "/data/anchoring.json"))
    entries = anchors.get("anchors", [])
    head = anchors.get("chain_head", "")
    if not entries:
        print("FAIL: anchoring.json has no anchors yet")
        return 1

    # 1) chain walk: head must resolve back to GENESIS
    by_digest = {e["digest"]: e for e in entries}
    cur, chain_ok = head, True
    steps = 0
    while cur and not cur.startswith(GENESIS_PREFIX):
        e = by_digest.get(cur)
        if not e:
            problems.append(f"chain: prev link {cur[:16]}… does not resolve")
            chain_ok = False
            break
        cur = e["prev_digest"]
        steps += 1
    print(f"[{'PASS' if chain_ok else 'FAIL'}] chain: {steps} links from head back to "
          f"{'GENESIS' if chain_ok else 'BROKEN'} ({len(entries)} anchors published)")

    todo = entries if days is None else entries[:days]
    passed = checked = 0
    ots_bin = shutil.which("ots") if use_ots else None
    if use_ots and not ots_bin:
        print("[warn] --ots requested but the `ots` CLI is not installed "
              "(pip install opentimestamps-client)")
    problems = []
    for e in todo:
        checked += 1
        lbl = e["label"]
        p = []
        try:
            raw = _get(f"{base}/data/ots/{lbl}.snapshot")
        except Exception as ex:  # noqa: BLE001
            print(f"[FAIL] {lbl}: snapshot download failed ({ex})")
            continue
        if hashlib.sha256(raw).hexdigest() != e["digest"]:
            p.append("sha256(snapshot) != published digest")
        _parse_snapshot(raw, e, p)
        if use_ots and ots_bin:
            try:
                with tempfile.TemporaryDirectory() as td:
                    sp = os.path.join(td, f"{lbl}.snapshot")
                    op = sp + ".ots"
                    open(sp, "wb").write(raw)
                    open(op, "wb").write(_get(f"{base}/data/ots/{lbl}.ots"))
                    out = subprocess.run([ots_bin, "verify", "-f", sp, op],
                                         capture_output=True, text=True, timeout=120)
                    mm = re.search(r"BitcoinBlockHeaderAttestation\((\d+)\)", out.stdout + out.stderr)
                    if mm:
                        got = int(mm.group(1))
                        if e.get("btc_block") and got != int(e["btc_block"]):
                            p.append(f"btc block {got} != published {e.get('btc_block')}")
                    elif e.get("status") == "confirmed":
                        p.append("ots verify did not find a bitcoin attestation")
            except Exception as ex:  # noqa: BLE001
                p.append(f"ots verify failed: {ex}")
        if p:
            problems.extend(f"{lbl}: {x}" for x in p)
            print(f"[FAIL] {lbl}")
        else:
            passed += 1

    status = "OK" if chain_ok and not problems else "FAILED"
    print(f"\nverified {checked} snapshots: {passed} passed, {checked - passed} failed — chain {status}")
    for x in problems:
        print("  !", x)
    print("receipts: https://verify.opentimestamps.org (upload the .ots + .snapshot pair)")
    return 0 if status == "OK" else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="paycensus-verify",
                                 description="Verify the PayCensus Proof-of-Integrity chain")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--days", type=int, default=None, help="verify only the N most recent snapshots")
    ap.add_argument("--ots", action="store_true",
                    help="also run `ots verify` against Bitcoin (requires opentimestamps-client)")
    args = ap.parse_args(argv)
    sys.exit(verify(args.base, args.days, args.ots))


if __name__ == "__main__":
    main()
