#!/usr/bin/env python3
"""Gate — ASPL v0.1 policy evaluator (dogfood build).

Evaluates a payment intent against an Agent Spend Policy and appends a
hash-linked audit event. Policy file is JSON (same fields as the YAML spec
at https://gate.paycensus.com/spec/). Never touches funds: the caller only
proceeds when decision.allow is true, then signs with ITS OWN key.
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

POLICY_PATH = os.environ.get("GATE_POLICY", "/etc/paycensus/gate_policy.json")
AUDIT_PATH = os.environ.get("GATE_AUDIT", "/var/lib/paycensus/gate_audit.jsonl")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_policy(path=POLICY_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return None


def _month_spend(audit_path, scope):
    """Sum of allowed payment amounts this month for a budget scope."""
    m = time.strftime("%Y-%m")
    total = 0.0
    try:
        with open(audit_path) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if (ev.get("type") == "payment.evaluated"
                        and ev.get("decision", {}).get("allow")
                        and ev.get("eventMonth") == m
                        and scope in (ev.get("decision", {}).get("scopes") or [])):
                    total += float(ev.get("intent", {}).get("amountUsd") or 0)
    except OSError:
        pass
    return total


def _prev_hash(audit_path):
    last = ""
    try:
        with open(audit_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        last = json.loads(line).get("hash", last)
                    except ValueError:
                        pass
    except OSError:
        pass
    return last


def evaluate(agent, resource, amount_usd, rail="x402", pay_to=None,
             policy=None, audit_path=AUDIT_PATH):
    """Evaluate one payment intent. Returns the decision dict (+eventHash)."""
    pol = policy or load_policy()
    if not pol:
        return _finish(agent, resource, amount_usd, rail, True,
                       "no policy loaded - allow (bootstrap)", [], audit_path, pay_to)
    reasons = []
    scopes = []
    ok = True

    # rail permission
    for al in pol.get("allowlists", []):
        scopes.append(al.get("scope"))
        rails = al.get("rails")
        if rails and rail not in rails:
            ok = False
            reasons.append("rail %s not permitted" % rail)

    # merchant allowlist: pinned domain/resource wins; trustRank floor wired in W2
    if ok:
        dom = urlparse(resource).netloc.lower()
        matched = None
        tr_min = None
        for al in pol.get("allowlists", []):
            for m in al.get("merchants", {}).get("pinned", []):
                if m.get("domain", "").lower() == dom or m.get("resource") == resource:
                    matched = m
                    break
            tr = al.get("merchants", {}).get("trustRank")
            if tr:
                tr_min = float(tr.get("min", 0))
            if matched:
                break
        if matched:
            reasons.append("merchant pinned: %s" % dom)
        else:
            ok = False
            reasons.append("merchant %s not in pinned allowlist (trustRank floor %.2f pending W2)"
                           % (dom, tr_min or 0))

    # budgets (month-to-date from our own audit stream)
    if ok:
        for b in pol.get("budgets", []):
            monthly = float(b.get("monthly", 0))
            if monthly <= 0:
                continue
            spent = _month_spend(audit_path, b.get("scope"))
            if spent + amount_usd > monthly:
                ok = False
                reasons.append("budget exceeded for %s: %.2f+%.4f > %.2f"
                               % (b.get("scope"), spent, amount_usd, monthly))
            else:
                reasons.append("budget ok for %s: %.4f/%.2f"
                               % (b.get("scope"), spent + amount_usd, monthly))

    # approval thresholds (dogfood: approver flow not wired yet)
    if ok:
        th = pol.get("thresholds", {})
        cap = float(th.get("autoApproveBelow", 0.01))
        if amount_usd > cap:
            ok = False
            reasons.append("amount %.4f above autoApproveBelow %.2f (approval flow pending W2)"
                           % (amount_usd, cap))
        else:
            reasons.append("amount %.4f <= autoApprove %.2f" % (amount_usd, cap))

    return _finish(agent, resource, amount_usd, rail, ok,
                   "; ".join(reasons) or "policy empty", scopes, audit_path, pay_to)


def _finish(agent, resource, amount_usd, rail, allow, reason, scopes,
            audit_path, pay_to=None):
    ev = {
        "v": "aspl/v0.1",
        "type": "payment.evaluated",
        "ts": _now(),
        "eventMonth": time.strftime("%Y-%m"),
        "agent": agent,
        "intent": {"resource": resource, "amountUsd": round(amount_usd, 6),
                   "rail": rail, "payTo": pay_to},
        "decision": {"allow": bool(allow), "reason": reason, "scopes": scopes},
    }
    ev["prevHash"] = _prev_hash(audit_path)
    canon = json.dumps(ev, sort_keys=True, separators=(",", ":"))
    ev["hash"] = hashlib.sha256(canon.encode()).hexdigest()
    os.makedirs(os.path.dirname(audit_path), exist_ok=True)
    with open(audit_path, "a") as f:
        f.write(json.dumps(ev, sort_keys=True) + "\n")
    out = dict(ev["decision"])
    out["eventHash"] = ev["hash"]
    return out


def verify_chain(audit_path=AUDIT_PATH):
    prev = ""
    n = 0
    with open(audit_path) as f:
        for line in f:
            ev = json.loads(line)
            h = ev.get("hash")
            p = ev.get("prevHash")
            canon = json.dumps({k: v for k, v in ev.items() if k != "hash"},
                               sort_keys=True, separators=(",", ":"))
            if hashlib.sha256(canon.encode()).hexdigest() != h:
                return False, "hash mismatch at event %d" % n
            if p != prev:
                return False, "chain break at event %d" % n
            prev = h
            n += 1
    return True, "chain ok: %d events" % n


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "eval":
        d = evaluate(sys.argv[2], sys.argv[3], float(sys.argv[4]),
                     sys.argv[5] if len(sys.argv) > 5 else "x402")
        print(json.dumps(d, indent=1))
    elif cmd == "verify":
        ok, msg = verify_chain()
        print(("PASS " if ok else "FAIL ") + msg)
    else:
        print("usage: gate_policy.py eval <agent> <resource> <amount_usd> [rail] | verify")
