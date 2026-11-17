# -*- coding: utf-8 -*-
"""ColdMail Gateway client (autorun.trade pool) — paycensus edition.

分工口径（用户 2026-11-16 拍板）：重要联络邮件走 hello@paycensus.com 自有 SMTP；
冷邮件（T1/T2/T3 外联）走池子。每次发送前强制查 /quota，sendable_now==0 判停，
绝不靠撞 429 发现额度用尽。传给池子的正文**不带我方页脚**（池子自带合规页脚/
退订/抑制名单，双页脚会被判 warnings）。任何异常结构化返回，绝不抛出。
"""
import json
import time
import urllib.error
import urllib.request

TOKEN_FILE = "/opt/paycensus/.coldmail_token"
BASE = "https://autorun.trade"
TIMEOUT = 15
#: Cloudflare 1010 拦 python-urllib 默认 UA（leadsail 实测 2026-11-13）
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0.0.0 Safari/537.36 PayCensus-ColdMail/1.0")

_quota_cache = {"at": 0.0, "data": None}


def token():
    try:
        return open(TOKEN_FILE).read().strip()
    except OSError:
        return ""


def quota(force=False):
    """GET /api/v1/quota。force=False 时 60s 缓存（状态展示用）；发前必须 force=True。"""
    global _quota_cache
    now = time.time()
    if not force and _quota_cache["data"] and now - _quota_cache["at"] < 60:
        return dict(_quota_cache["data"])
    t = token()
    if not t:
        return {"ok": False, "error": "no_token"}
    try:
        req = urllib.request.Request(
            BASE + "/api/v1/quota",
            headers={"X-Caller-Token": t, "Accept": "application/json",
                     "User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        out = {"ok": True,
               "limit": int(data.get("limit") or 0),
               "limit_mode": str(data.get("limit_mode") or ""),
               "pool_capacity_today": int(data.get("pool_capacity_today") or 0),
               "used_today": int(data.get("used_today") or 0),
               "remaining_today": int(data.get("remaining_today") or 0),
               "pool_remaining_today": int(data.get("pool_remaining_today") or 0),
               "sendable_now": int(data.get("sendable_now") or 0),
               "status": str(data.get("status") or ""),
               "reset_at": str(data.get("reset_at") or ""),
               "domains": data.get("domains") or []}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        out = {"ok": False, "error": f"http_{exc.code}", "detail": detail}
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    _quota_cache = {"at": now, "data": out}
    return dict(out)


def sendable_now():
    q = quota(force=True)
    if not q.get("ok") or str(q.get("status") or "") != "active":
        return 0
    return max(0, int(q.get("sendable_now") or 0))


def send_one(to_addr, subject, text, html="", campaign="", reply_to=""):
    """POST /api/v1/send 单封。返回 {queued: True, send_id, mailbox} 或 {queued: False, reason}。
    发前内部强制重查 /quota。"""
    t = token()
    if not t:
        return {"queued": False, "reason": "no_token"}
    q = quota(force=True)
    if not q.get("ok"):
        return {"queued": False, "reason": f"quota_{q.get('error')}"}
    if str(q.get("status") or "") != "active":
        return {"queued": False, "reason": f"pool_{q.get('status') or 'unknown'}",
                "reset_at": q.get("reset_at", "")}
    if int(q.get("sendable_now") or 0) <= 0:
        return {"queued": False, "reason": "no_quota", "reset_at": q.get("reset_at", "")}
    payload = {"to": to_addr, "subject": subject, "text": text}
    if html:
        payload["html"] = html
    if campaign:
        payload["campaign"] = campaign
    if reply_to:
        payload["reply_to"] = reply_to
    try:
        req = urllib.request.Request(
            BASE + "/api/v1/send",
            data=json.dumps(payload).encode("utf-8"),
            headers={"X-Caller-Token": t, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": _UA},
            method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return {"queued": False, "reason": f"http_{exc.code}", "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"queued": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    results = data.get("results") or []
    first = results[0] if results else {}
    if first.get("skipped"):
        return {"queued": False, "reason": f"skipped:{first.get('skipped')}"}
    if not int(data.get("queued") or 0):
        return {"queued": False, "reason": "not_queued"}
    nq = data.get("quota") or {}
    if nq:
        _quota_cache["data"] = {"ok": True, "limit": int(nq.get("limit") or 0),
                                "used_today": int(nq.get("used_today") or 0),
                                "remaining_today": int(nq.get("remaining_today") or 0),
                                "sendable_now": int(nq.get("sendable_now") or 0),
                                "status": str(nq.get("status") or ""),
                                "reset_at": str(nq.get("reset_at") or ""),
                                "domains": []}
        _quota_cache["at"] = time.time()
    return {"queued": True, "send_id": first.get("send_id"),
            "mailbox": str(first.get("mailbox") or "")}


def clean(emails):
    """POST /api/v1/clean — 名单清洗（免费、不耗额度、不写发送记录）。
    emails: 最多 1000 个/批（超出自动分批）。返回
      {"ok": True, "verdicts": {email: "ok"|"invalid"|"skip"},
       "reasons": {email: reason}, "accepted": [ok 去重数组]}
    网关判定与 /send 入队预检同源：ok=可发；invalid=必硬退（勿重试）；
    skip=抑制名单/批内重复。调用失败时 fail-open（verdicts 为空，不缓存），
    因为 /send 内置同源预检兜底，不会把死信真正投出去。
    """
    out_all = {}
    reasons = {}
    ok_list = []
    ems = [e for e in emails if e and "@" in e]
    t = token()
    if not t:
        return {"ok": False, "error": "no_token", "verdicts": {}, "reasons": {},
                "accepted": []}
    try:
        for i in range(0, len(ems), 1000):
            body = json.dumps({"emails": ems[i:i + 1000]}).encode()
            req = urllib.request.Request(
                BASE + "/api/v1/clean", data=body, method="POST",
                headers={"X-Caller-Token": t, "Content-Type": "application/json",
                         "Accept": "application/json", "User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            for r in (data.get("results") or []):
                em = (r.get("email") or "").lower()
                v = str(r.get("verdict") or "")
                out_all[em] = v
                if r.get("reason"):
                    reasons[em] = str(r["reason"])
                if v == "ok":
                    ok_list.append(em)
        return {"ok": True, "verdicts": out_all, "reasons": reasons,
                "accepted": ok_list}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                "verdicts": {}, "reasons": {}, "accepted": []}
