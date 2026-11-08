# -*- coding: utf-8 -*-
"""stdlib-only client — zero dependencies, zero cost, read-only.

All data comes from public endpoints of https://paycensus.com:
  /data/stats.json      aggregate machine-payment census (x402 + MPP; tempo_mpp is a sampled estimate)
  /data/calendar.json   daily calendar (self-contained records, never backfilled)
  /data/audit.json      collection lineage + methodology
  /data/anchoring.json  daily Bitcoin anchoring (OpenTimestamps receipts)
  /data/entities.json   payTo -> domain/title/description (sellers only)
  /data/directory.json  the registry: cross-protocol entity directory (7 categories)
  /data/indicators.json leading indicators (already-collected data, never forecasts)
  /data/leads.json      tier + protocol counters (counts only; contact emails are never published)
  /data/mpp.json        MPP/Tempo monitor: chain samples, offer graph, seller-side protocol census
  /api/seller /api/buyer /api/edges   processed commercial info (who pays whom)

Trust layer (https://trust.paycensus.com — counterparty verification):
  trust_status / trust_factors / trust_founding / trust_revocations
  Deterministic domain-control verification; verification is not a guarantee
  of honesty, solvency, or legality.

The entity layer is protocol-agnostic: protocol source is recorded at the ingest
layer only; buyers, sellers and edges do not care which protocol carried the payment.
"""
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE = "https://paycensus.com"
DEFAULT_TRUST_BASE = "https://trust.paycensus.com"


class PayCensus:
    def __init__(self, base: str = DEFAULT_BASE, timeout: int = 15,
                 user_agent: str = "paycensus/0.5", trust_base: str = DEFAULT_TRUST_BASE):
        self.base = base.rstrip("/")
        self.trust_base = trust_base.rstrip("/")
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}

    def _get(self, path: str, base: str = None, headers: dict = None, **params):
        url = (base or self.base) + path
        if params:
            url += "?" + urlencode(params)
        h = dict(self.headers)
        if headers:
            h.update(headers)
        req = Request(url, headers=h)
        with urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---- aggregate / trust layer -------------------------------------------------
    def stats(self) -> dict:
        """Aggregate census: txs, volume, unique buyers/sellers/edges (per chain),
        plus tempo_mpp — the sampled MPP/Tempo estimate (never merged into counts)."""
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
        """Tier + protocol census counters (counts only). Emails are never published."""
        return self._get("/data/leads.json")

    def mpp(self) -> dict:
        """MPP/Tempo monitor: hourly chain samples, offer graph parsed from
        sellers' 402 challenges, seller-side protocol census (x402/mpp/dual)."""
        return self._get("/data/mpp.json")

    # ---- registry / indicators -----------------------------------------------------
    def directory(self) -> dict:
        """The registry: unified cross-protocol entity directory — x402 attribution
        plus MPP offers, 7 categories. Built on the Bitcoin-anchored census files."""
        return self._get("/data/directory.json")

    def indicators(self) -> dict:
        """Leading indicators — every number is already-collected data, never a
        forecast (median offer price, Tempo/Base ratio, HHI, churn, ...)."""
        return self._get("/data/indicators.json")

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

    def indices(self) -> dict:
        """Daily indices — MPI (machine-payment settlement volume, base-100
        chained to the census start) and TVI (trust-verified coverage of the
        registry). Deterministic ratios of Bitcoin-anchored data; the client
        can re-verify every number against the OTS attestations."""
        return self._get("/data/indices.json")

    # ---- trust layer (counterparty verification) -----------------------------------
    def trust_status(self, addr: str = None, domain: str = None) -> dict:
        """Public verification status of a counterparty (cached ≤60s upstream).
        status ∈ verified · pending · revoked · none. Deterministic facts only."""
        if addr:
            return self._get("/api/status", base=self.trust_base, addr=addr)
        if domain:
            return self._get("/api/status", base=self.trust_base, domain=domain)
        raise ValueError("addr or domain required")

    def trust_factors(self, addr: str, api_key: str = None) -> dict:
        """Metered factor lookup (10 calls/day free per key): attribution,
        on-chain history + volume percentile, offers, contactability.
        Get a key: POST https://trust.paycensus.com/api/keys/create"""
        headers = {"Authorization": "Bearer " + api_key} if api_key else None
        return self._get("/api/factors", base=self.trust_base,
                         headers=headers, addr=addr)

    def trust_founding(self) -> dict:
        """Founding-tier counter: $99/yr locked for life, first 100 subscribers."""
        return self._get("/api/founding", base=self.trust_base)

    def trust_revocations(self) -> dict:
        """Public append-only revocation list (the CRL of machine commerce)."""
        return self._get("/api/revocations", base=self.trust_base)
