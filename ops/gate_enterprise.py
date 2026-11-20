"""Gate Enterprise console backend — departments, agent tokens, dept policies.

Multi-department layer on top of gate_policy.evaluate (ASPL v0.1).
DB: /var/lib/paycensus/gate_enterprise.db (SQLite).
Tokens: gk_<48 hex>, stored sha256-hashed, shown once at creation.
Policy per department: monthly budget (per-agent cap / default),
autoApproveBelow / requireApprovalAbove thresholds, Trust Rank floor,
pinned merchant domains. Evaluation reuses gate_policy.evaluate(policy=...),
so every spend lands in the same hash-linked audit chain.
"""
import hashlib
import json
import os
import secrets
import sqlite3

DB = os.environ.get("GATE_ENTERPRISE_DB", "/var/lib/paycensus/gate_enterprise.db")

FIELDS = ("monthly_budget", "auto_approve_below",
          "require_approval_above", "trust_floor")


def _con():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = _con()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS departments(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      monthly_budget REAL NOT NULL DEFAULT 50.0,
      auto_approve_below REAL NOT NULL DEFAULT 0.01,
      require_approval_above REAL NOT NULL DEFAULT 0.05,
      trust_floor REAL NOT NULL DEFAULT 0.0,
      pinned TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS agents(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      dept_id INTEGER NOT NULL REFERENCES departments(id),
      monthly_budget REAL,
      token_hash TEXT NOT NULL,
      token_prefix TEXT NOT NULL,
      revoked INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT (datetime('now')));
    """)
    c.commit()
    c.close()


# ---------- departments ----------
def dept_rows():
    init_db()
    c = _con()
    rows = [dict(r) for r in
            c.execute("SELECT * FROM departments ORDER BY id")]
    c.close()
    for r in rows:
        r["pinned"] = json.loads(r["pinned"] or "[]")
    return rows


def create_dept(name, monthly_budget=50.0, auto_approve_below=0.01,
                require_approval_above=0.05, trust_floor=0.0, pinned=None):
    init_db()
    c = _con()
    cur = c.execute(
        "INSERT INTO departments(name, monthly_budget, auto_approve_below,"
        " require_approval_above, trust_floor, pinned)"
        " VALUES(?,?,?,?,?,?)",
        (name, float(monthly_budget), float(auto_approve_below),
         float(require_approval_above), float(trust_floor),
         json.dumps(pinned or [])))
    c.commit()
    i = cur.lastrowid
    c.close()
    return i


def update_dept(dept_id, updates):
    init_db()
    c = _con()
    sets, vals = [], []
    for k in FIELDS:
        if k in updates:
            sets.append(k + "=?")
            vals.append(float(updates[k]))
    if "pinned" in updates:
        sets.append("pinned=?")
        vals.append(json.dumps(updates["pinned"]))
    if "name" in updates:
        sets.append("name=?")
        vals.append(str(updates["name"]))
    if not sets:
        c.close()
        return False
    vals.append(int(dept_id))
    c.execute("UPDATE departments SET " + ", ".join(sets)
              + " WHERE id=?", vals)
    c.commit()
    c.close()
    return True


def delete_dept(dept_id):
    """Removes the department and (by design) revokes nothing silently —
    agents belonging to it are deleted with it."""
    init_db()
    c = _con()
    c.execute("DELETE FROM agents WHERE dept_id=?", (int(dept_id),))
    c.execute("DELETE FROM departments WHERE id=?", (int(dept_id),))
    c.commit()
    c.close()
    return True


# ---------- agents ----------
def agent_rows():
    init_db()
    c = _con()
    rows = [dict(r) for r in c.execute(
        "SELECT a.id, a.name, a.dept_id, d.name AS dept, a.monthly_budget,"
        " a.token_prefix, a.revoked, a.created_at FROM agents a"
        " LEFT JOIN departments d ON d.id=a.dept_id ORDER BY a.id")]
    c.close()
    return rows


def create_agent(name, dept_id, monthly_budget=None):
    """Returns the plain token ONCE; only sha256 is stored."""
    init_db()
    c = _con()
    token = "gk_" + secrets.token_hex(24)
    th = hashlib.sha256(token.encode()).hexdigest()
    prefix = token[:9] + "…" + token[-4:]
    c.execute(
        "INSERT INTO agents(name, dept_id, monthly_budget, token_hash,"
        " token_prefix) VALUES(?,?,?,?,?)",
        (name, int(dept_id), monthly_budget, th, prefix))
    c.commit()
    c.close()
    return token


def revoke_agent(agent_id):
    init_db()
    c = _con()
    c.execute("UPDATE agents SET revoked=1 WHERE id=?", (int(agent_id),))
    c.commit()
    c.close()
    return True


def verify_token(token):
    """Returns joined agent+dept row for a valid, non-revoked token."""
    if not token or not token.startswith("gk_"):
        return None
    th = hashlib.sha256(token.strip().encode()).hexdigest()
    c = _con()
    r = c.execute(
        "SELECT a.name, a.dept_id, a.monthly_budget, a.revoked,"
        " d.name AS dept, d.monthly_budget AS dept_monthly_budget,"
        " d.auto_approve_below, d.require_approval_above,"
        " d.trust_floor, d.pinned"
        " FROM agents a JOIN departments d ON d.id=a.dept_id"
        " WHERE a.token_hash=? AND a.revoked=0", (th,)).fetchone()
    c.close()
    return dict(r) if r is not None else None


def build_policy(arow):
    """ASPL policy dict for gate_policy.evaluate(policy=...)."""
    name = arow["name"]
    budget = arow["monthly_budget"]
    if not budget:
        budget = arow["dept_monthly_budget"]
    pinned = json.loads(arow["pinned"] or "[]")
    return {
        "apiVersion": "aspl/v0.1",
        "kind": "SpendPolicy",
        "budgets": [{"scope": name, "currency": "USD",
                     "monthly": float(budget or 0)}],
        "allowlists": [{"scope": name, "rails": ["x402"],
                        "merchants": {
                            "pinned": [{"domain": d} for d in pinned],
                            "trustRank": {"min": float(arow["trust_floor"] or 0)}}}],
        "thresholds": {
            "autoApproveBelow": float(arow["auto_approve_below"] or 0.01),
            "requireApprovalAbove": float(arow["require_approval_above"] or 0.05)},
    }
