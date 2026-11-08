# paycensus

Read-only Python client for the [PayCensus](https://paycensus.com) machine-payment
census data layer — *"The census of the machine economy."*

PayCensus indexes **who pays whom** across machine-payment protocols — **x402**
(EIP-3009 settlements on Base, facilitator programs on Solana) and **MPP**
(Machine Payments Protocol; Tempo chain sampled hourly, offer graph parsed from
sellers' 402 challenges) — per entity, not per protocol. The ingest layer records
the protocol source; the entity layer does not care. A daily calendar is never
backfilled, and a chained digest of every day's snapshot is **Bitcoin-anchored**
via [OpenTimestamps](https://opentimestamps.org) — publicly verifiable, zero cost.

## Install

```bash
pip install paycensus
```

Or from source:

```bash
pip install git+https://github.com/OneLight1688/paycensus.git
```

## Usage

Zero dependencies (stdlib only).

```python
from paycensus import PayCensus

pc = PayCensus()

pc.stats()                          # census totals + tempo_mpp sampled estimate
pc.calendar()                       # daily calendar (never backfilled)
pc.anchoring()                      # Bitcoin-anchored daily digests (verify with ots)
pc.audit()                          # collection lineage + methodology

pc.edges(limit=20, min_volume=10)   # top buyer x seller relationship edges
pc.seller("0x…")                    # who pays this seller, daily series
pc.buyer("0x…")                     # what this buyer pays for

pc.mpp()                            # MPP/Tempo monitor: chain samples, offer graph,
                                    # seller-side protocol census (x402/mpp/dual)

pc.directory()                      # the registry: cross-protocol entity directory
                                    # (x402 attribution + MPP offers, 7 categories)
pc.indicators()                     # leading indicators — already-collected data,
                                    # never a forecast
pc.indices()                        # daily indices — MPI (machine-payment volume,
                                    # base-100 to census start) + TVI (trust-verified
                                    # coverage %); deterministic, Bitcoin-anchored

# ---- Trust layer: counterparty verification before you settle ----
pc.trust_status(addr="0x…")         # verified · pending · revoked · none (public)
pc.trust_status(domain="seller.example.com")
pc.trust_factors("0x…")             # metered factors: attribution, on-chain history
                                    # + volume percentile, offers, contactability
                                    # (10 calls/day free per key; key via POST
                                    #  trust.paycensus.com/api/keys/create)
pc.trust_factors("0x…", api_key="pct_live_…")
pc.trust_revocations()              # public append-only revocation list
pc.trust_founding()                 # founding-tier counter ($99/yr, first 100)
```

Verification is deterministic domain-control checking (3 public DNS resolvers,
2-of-3 must confirm; re-verified every 72h; 3 consecutive failures auto-revoke).
**It states facts only — never a guarantee of honesty, solvency, or legality.**
Badge + seal for your own site: <https://trust.paycensus.com/>

## Protocol coverage

| Protocol | Settlement rail | Observation |
|---|---|---|
| x402 (EIP-3009) | Base USDC (counted), Solana facilitators (counted) | enumerated on-chain |
| MPP | Tempo chain (sampled estimate), Stripe fiat rails (not observable) | hourly 50-block samples + 402-challenge offer graph |

Sampled estimates are never merged into counted totals. When a new payment
protocol with real adoption emerges, it is added at the ingest layer — the
entity graph does not change.

## Data boundaries

- On-chain facts (addresses, amounts, timing) are public data.
- Seller entities (domain/title/description) and 402-challenge quotes are the
  seller's own public storefront info.
- **Contact information is never collected into the public layer and never published.**
- Any seller can be removed from the entity layer within 24h (opt-out).
- Anchoring preserves *integrity*, not *correctness* — correctness comes from the
  public methodology in `/data/audit.json`, which anyone can independently re-run.

## Verify the data yourself (Proof-of-Integrity)

Every day's snapshot (stats + calendar + audit) is hash-chained and **anchored to
Bitcoin** via [OpenTimestamps](https://opentimestamps.org) — a 60-line standard,
no trusted third party, no API key. Verify it independently:

```bash
paycensus-verify --base https://paycensus.com          # chain + snapshot hashes
paycensus-verify --ots                                  # + Bitcoin block attestation (needs `ots`)
```

Or verify manually: download `data/ots/{label}.snapshot` + `{label}.ots` from
paycensus.com and run `ots verify -f snapshot ots`, or upload both at
https://verify.opentimestamps.org. The chain has been unbroken since genesis
(2026-10-31) and is never backfilled.

## License

MIT
