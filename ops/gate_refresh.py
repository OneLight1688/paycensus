
import json, os, shutil, sys
sys.path.insert(0, "/opt/paycensus")
import gate_policy as gp
DST = "/www/wwwroot/gate/data"
os.makedirs(DST, exist_ok=True)
try:
    shutil.copyfile(gp.AUDIT_PATH, DST + "/audit.jsonl")
    ok, msg = gp.verify_chain()
    n = sum(1 for _ in open(gp.AUDIT_PATH))
except FileNotFoundError:
    ok, msg, n = True, "no audit yet", 0
json.dump({"events": n, "chainOk": bool(ok), "chainMsg": msg,
           "refreshedAt": gp._now()},
          open(DST + "/status.json", "w"), indent=1)
print("refreshed:", n, "events, chainOk:", ok)
