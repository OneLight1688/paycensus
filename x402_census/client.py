# -*- coding: utf-8 -*-
"""stdlib-only client — zero dependencies, zero cost, read-only.

All data comes from public endpoints of https://paycensus.com:
  /data/stats.json      aggregate machine-payment stats (per chain, per day)
  /data/calendar.json   daily calendar (self-contained records, never backfilled)
  /data/audit.json      collection lineage + methodology
  /data/anchoring.json  daily Bitcoin anchoring (OpenTimestamps receipts)
  /data/entities.json   payTo -> domain/title/description (sellers only)
  /data/leads.json      tier counters (counts only; contact emails are never published)
  /api/seller /api/buyer /api/edges   processed commercial info (who pays whom)
"""
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE = "https://paycensus.com"


class PayCensus:
    def __init__(self, base: str = DEFAULT_BASE, timeout: int = 15, user_agent: str = "x402-census/0.1"):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}

    def _get(self, path: str, **params):
        url = self.base + path
        if params:
            url += "?" + urlencode(params)
        req = Request(url, headers=self.headers)
        with urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---- aggregate / trust layer -------------------------------------------------
    def stats(self) -> dict:
        """Aggregate stats: txs, volume, unique buyers/sellers/edges (per chain)."""
        return self._get("/data/stats.json")

    def calendar(self) -> dict:
        """Daily calendar — each day is a self-contained record and is never backfilled."""
        return self._get("/data/calendar.json")

    def audit(self) -> dict:
        """Collection lineage: collectors, last runs, methodology, error counts."""
        return self._get("/data/audit.json")

    def anchoring(self) -> dict:
        """Proof-of-Integrity: chained digests + OpenTimestamps Bitcoin receipts."""
        return self._get("/data/anchoring.json")

    # ---- enriched layers ----------------------------------------------------------
    def entities(self) -> dict:
        """payTo -> domain/title/description for on-chain matched sellers only."""
        return self._get("/data/entities.json")

    def leads(self) -> dict:
        """Tier counters (counts only). Contact emails are never published."""
        return self._get("/data/leads.json")

    # ---- query API -----------------------------------------------------------------
    def seller(self, addr: str) -> dict:
        """Seller view: totals, daily series, buyers (where money comes from)."""
        return self._get("/api/seller", addr=addr)

    def buyer(self, addr: str) -> dict:
        """Buyer view: totals, sellers paid (where money goes)."""
        return self._get("/api/buyer", addr=addr)

    def edges(self, limit: int = 50, min_volume: float = 0.0) -> dict:
        """Top buyer x seller relationship edges."""
        return self._get("/api/edges", limit=limit, min_volume=min_volume)
