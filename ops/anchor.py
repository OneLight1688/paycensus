#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Proof-of-Integrity anchorer — OpenTimestamps (Bitcoin) + hash chaining
- Daily: snapshot the three public data files -> chained digest
         (digest = SHA256(prev_digest + snapshot)) -> `ots stamp` -> receipt published
- Upgrade mode: `ots upgrade` pending receipts once Bitcoin confirms; refresh statuses
- State: /var/lib/paycensus/anchoring (snapshots+receipts), /data/anchoring.json (public feed)
- No account, no private key, $0 cost. Receipts are independently verifiable with `ots verify`
  or https://verify.opentimestamps.org
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SNAP_DIR = "/var/lib/paycensus/anchoring"
DATA_DIR = "/www/wwwroot/paycensus/data"
PUB_OTS = os.path.join(DATA_DIR, "ots")
META_PATH = os.path.join(SNAP_DIR, "meta.json")
ANCHOR_OUT = os.path.join(DATA_DIR, "anchoring.json")
FILES = ["stats.json", "calendar.json", "audit.json", "indices.json"]
GENESIS = "GENESIS:2026-10-31"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
STATS_OUT = os.environ.get("PAYCENSUS_STATS", os.path.join(DATA_DIR, "stats.json"))



def _log(s):
    from datetime import datetime as _dt
    print(f"{_dt.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} {s}", flush=True)

def ots_cli():
    p = shutil.which("ots")
    if not p:
        cand = os.path.expanduser("~/.local/bin/ots")
        if os.path.exists(cand):
            p = cand
    if not p:
        raise RuntimeError("ots CLI not found (pip install opentimestamps-client)")
    return p


def run(cmd, timeout=180):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def load_meta():
    if os.path.exists(META_PATH):
        with open(META_PATH) as f:
            return json.load(f)
    return {"prev_digest": GENESIS, "prev_label": None}


def save_meta(m):
    os.makedirs(SNAP_DIR, exist_ok=True)
    with open(META_PATH, "w") as f:
        json.dump(m, f)


def load_anchors():
    if os.path.exists(ANCHOR_OUT):
        with open(ANCHOR_OUT) as f:
            return json.load(f)
    return {"note": "Daily snapshots are Bitcoin-anchored (hash-chained, OpenTimestamps). "
                    "digest = SHA256(prev_digest + snapshot_bytes). Verify: ots verify -f snapshot ots_receipt "
                    "or `paycensus-verify --ots` from the paycensus client.",
            "chain_head": GENESIS, "anchors": []}


def save_anchors(a):
    tmp = ANCHOR_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(a, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, ANCHOR_OUT)


def ots_status(ots_path):
    """Return (status, btc_block) from `ots info`.
    A complete timestamp prints `verify BitcoinBlockHeaderAttestation(<height>)`."""
    code, out = run(f"{ots_cli()} info {ots_path}", timeout=60)
    m = re.search(r"BitcoinBlockHeaderAttestation\((\d+)\)", out)
    if m:
        return "confirmed", int(m.group(1))
    return "pending", None


def write_info(label, ots_path):
    """Publish a human-readable summary of the receipt (binary .ots is unreadable in browsers)."""
    code, out = run(f"{ots_cli()} info {ots_path}", timeout=60)
    if not out.strip():
        return
    txt = (f"PayCensus daily snapshot receipt {label}\n"
           f"This is the human-readable summary of {label}.ots (a binary OpenTimestamps proof).\n"
           f"Independent verification: ots verify -f {label}.snapshot {label}.ots\n"
           f"or upload both files at https://verify.opentimestamps.org\n\n{out}")
    tmp = os.path.join(PUB_OTS, f"{label}.info.txt.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(txt)
    os.replace(tmp, os.path.join(PUB_OTS, f"{label}.info.txt"))



GENESIS_LABEL = "2026-10-31"


def missing_labels():
    """Labels from genesis to yesterday that have no anchor entry."""
    anchors = load_anchors()
    have = {a["label"] for a in anchors["anchors"]}
    start = datetime.strptime(GENESIS_LABEL, "%Y-%m-%d").date()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    out, d = [], start
    while d <= yesterday:
        lbl = d.strftime("%Y-%m-%d")
        if lbl not in have:
            out.append(lbl)
        d += timedelta(days=1)
    return out


def do_backfill():
    missing = missing_labels()
    if not missing:
        _log("backfill: no gaps")
        return
    _log(f"backfill: missing {missing}, stamping in chronological order")
    for lbl in missing:
        do_stamp(lbl)


def verify_chain():
    """Walk prev_digest links back from chain_head; report unreachable entries.
    Label order may legitimately differ from chain order (late backfills)."""
    anchors = load_anchors()
    by_digest = {e["digest"]: e for e in anchors["anchors"]}
    head = anchors.get("chain_head")
    seen, cur = set(), head
    while cur and cur in by_digest and cur not in seen:
        seen.add(cur)
        cur = by_digest[cur].get("prev_digest")
    if cur and cur not in seen and not cur.startswith("GENESIS"):
        _log(f"CHAIN-WARNING: prev link {cur[:16]}… does not resolve to any anchor or genesis")
    unreachable = [e["label"] for d, e in by_digest.items() if d not in seen]
    if unreachable:
        _log(f"CHAIN-WARNING: {len(unreachable)} anchor(s) unreachable from head: {unreachable}")
    else:
        _log(f"chain verified: {len(seen)} links intact back to genesis")


def do_stamp(label):
    os.makedirs(SNAP_DIR, exist_ok=True)
    os.makedirs(PUB_OTS, exist_ok=True)
    meta = load_meta()
    anchors = load_anchors()
    if any(x["label"] == label for x in anchors["anchors"]):
        _log(f"{label} already anchored, skip")
        return

    # build snapshot: header + files (all public)
    parts = [f"PayCensus daily snapshot {label}\nprev_digest: {meta['prev_digest']}\n"
             f"generated: {datetime.now(timezone.utc).isoformat()}\n\n"]
    sizes = {}
    for fn in FILES:
        p = os.path.join(DATA_DIR, fn)
        if not os.path.exists(p):
            _log(f"skip missing {fn}")
            continue
        b = open(p, "rb").read()
        sizes[fn] = len(b)
        parts.append(f"===== {fn} ({len(b)} bytes) =====\n")
        parts.append(b.decode("utf-8", "replace"))
        parts.append("\n")
    snapshot = "".join(parts).encode("utf-8")
    digest = hashlib.sha256(snapshot).hexdigest()

    snap_path = os.path.join(SNAP_DIR, f"{label}.snapshot")
    with open(snap_path, "wb") as f:
        f.write(snapshot)

    ots_path = snap_path + ".ots"
    code, out = run(f"{ots_cli()} stamp {snap_path}", timeout=240)
    if not os.path.exists(ots_path):
        _log(f"ots stamp failed: {out[:300]}")
        return
    shutil.copy(ots_path, os.path.join(PUB_OTS, f"{label}.ots"))
    shutil.copy(snap_path, os.path.join(PUB_OTS, f"{label}.snapshot"))
    write_info(label, ots_path)

    status, block = ots_status(ots_path)
    entry = {
        "label": label, "digest": digest, "prev_digest": meta["prev_digest"],
        "files_bytes": sizes, "status": status, "btc_block": block,
        "ots": f"ots/{label}.ots", "snapshot": f"ots/{label}.snapshot",
        "anchored_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    lbl_date = datetime.strptime(label, "%Y-%m-%d").date()
    if lbl_date < datetime.now(timezone.utc).date() - timedelta(days=1):
        entry["late"] = True
    anchors["anchors"].insert(0, entry)
    anchors["chain_head"] = digest
    save_anchors(anchors)
    meta["prev_digest"] = digest
    meta["prev_label"] = label
    save_meta(meta)
    _log(f"anchored {label}: digest={digest[:16]}… status={status} files={sizes}")


def do_upgrade():
    anchors = load_anchors()
    changed = 0
    for e in anchors["anchors"]:
        if e["status"] == "confirmed":
            continue
        # receipts are written as {label}.snapshot.ots by do_stamp (ots appends .ots to the snapshot path)
        ots_path = os.path.join(SNAP_DIR, f"{e['label']}.snapshot.ots")
        if not os.path.exists(ots_path):
            _log(f"upgrade: receipt missing for {e['label']} ({ots_path}), skip")
            continue
        # legacy btc calendars (alice/bob/catallaxy/finney) named in old receipts are retired
        # ("Not found"); the pool calendars we actually submitted to hold the bitcoin attestations.
        run(f"{ots_cli()} upgrade -c https://a.pool.opentimestamps.org "
            f"-c https://b.pool.opentimestamps.org -c https://a.pool.eternitywall.com {ots_path}",
            timeout=180)
        status, block = ots_status(ots_path)
        if status != e["status"] or block != e.get("btc_block"):
            e["status"], e["btc_block"] = status, block
            changed += 1
            if status == "confirmed":
                # refresh the public receipt so third parties see the bitcoin attestation
                shutil.copy(ots_path, os.path.join(PUB_OTS, f"{e['label']}.ots"))
        write_info(e["label"], ots_path)
    save_anchors(anchors)
    _log(f"upgrade pass: {changed} updated, head={anchors['chain_head'][:16]}…")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "stamp"
    if mode == "upgrade":
        do_upgrade()
    else:
        label = sys.argv[2] if len(sys.argv) > 2 else \
            (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        do_stamp(label)
        do_backfill()
        verify_chain()
    sys.exit(0)


if __name__ == "__main__":
    main()
