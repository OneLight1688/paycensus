#!/usr/bin/env python3
"""PayCensus Agent MCP server — public read-only tools over daily snapshots."""
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = "/www/wwwroot/paycensus/data"
PORT = 8790

def load(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": repr(e)}

def tool_agent_rank(args):
    d = load("rank.json")
    rows = d.get("ranked", []) if isinstance(d, dict) else []
    mn = float(args.get("min_score") or 0)
    cls = args.get("cls"); ver = args.get("verified")
    try: lim = int(args.get("limit") or 25)
    except Exception: lim = 25
    lim = max(1, min(lim, 200))
    out = []
    for r in rows:
        if (r.get("score") or 0) < mn: continue
        if cls and r.get("class") != cls: continue
        if ver is not None and bool(r.get("verified")) != bool(ver): continue
        out.append(r)
    return {"generated_at": d.get("generated_at"), "method": d.get("method"),
            "count": len(out), "limit": lim, "ranked": out[:lim]}

TOOLS = [
 {"name": "agent_rank",
  "description": "PayCensus Agent Trust Rank: machine-payable x402 endpoints scored from real settlement behaviour (40% volume + 25% buyer diversity + 15% ticket sanity + 20% identity). Noise classes excluded by construction.",
  "inputSchema": {"type": "object", "properties": {
      "min_score": {"type": "number", "description": "minimum Trust Rank score (0-100)"},
      "cls": {"type": "string", "description": "behaviour class filter, e.g. real_commerce"},
      "verified": {"type": "boolean", "description": "only domain-verified endpoints"},
      "limit": {"type": "integer", "description": "max rows (1-200, default 25)"}},
      "additionalProperties": False}},
 {"name": "agent_map",
  "description": "Full behaviour taxonomy snapshot of 23k+ selling addresses in the machine-payment economy (real_commerce / agent_chain / self_probe / dust_noise / aggregator / low_signal).",
  "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
 {"name": "bazaar_stats",
  "description": "Coinbase x402 Bazaar indexing coverage and 30-day demand signals (unique payers, calls per endpoint).",
  "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]

def handle_rpc(body):
    method = body.get("method"); rid = body.get("id")
    def res(result): return {"jsonrpc": "2.0", "id": rid, "result": result}
    def err(code, msg): return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}
    if method == "initialize":
        return res({"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "paycensus-agent", "version": "0.1.0"}})
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return res({})
    if method == "tools/list":
        return res({"tools": TOOLS})
    if method == "tools/call":
        p = body.get("params") or {}
        name = p.get("name"); args = p.get("arguments") or {}
        if name == "agent_rank":
            data = tool_agent_rank(args)
        elif name == "agent_map":
            data = load("agent_map.json")
        elif name == "bazaar_stats":
            data = load("bazaar_stats.json")
        else:
            return err(-32602, "unknown tool: %s" % name)
        return res({"content": [{"type": "text", "text": json.dumps(data)[:60000]}], "isError": False})
    return err(-32601, "method not found: %s" % method)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, payload, ctype="application/json"):
        b = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()
    def do_POST(self):
        if self.path.rstrip("/") not in ("/mcp", "/"):
            self._send(404, {"error": "not found"}); return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error: %r" % e}}); return
        if isinstance(body, list):
            outs = [o for o in (handle_rpc(x) for x in body) if o is not None]
            self._send(200, outs if outs else {}); return
        out = handle_rpc(body)
        if out is None:
            self._send(202, b"", ctype="text/plain"); return
        self._send(200, out)

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print("paycensus-agent-mcp listening on 127.0.0.1:%d" % PORT, flush=True)
    srv.serve_forever()
