# -*- coding: utf-8 -*-
"""Patch: append clean() to /opt/paycensus/coldmail_pool.py (idempotent)."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

FUNC = '''

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
'''

PATH = "/opt/paycensus/coldmail_pool.py"
src = open(PATH).read()
if "def clean(" in src:
    print("clean() already present")
else:
    src = src.rstrip() + "\n" + FUNC
    open(PATH, "w").write(src)
    print("clean() appended")
import py_compile
py_compile.compile(PATH, doraise=True)
print("compiled OK")
