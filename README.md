# paycensus

Read-only Python client for the [PayCensus](https://paycensus.com) machine-payment
census data layer — *"The census of the machine economy."*

PayCensus indexes **who pays whom** across machine-payment protocols — **x402**
(EIP-3009 settlements on Base, facilitator programs on Solana) and **MPP**
(Machine Payments Protocol; Tempo chain sampled hourly, offer graph parsed from
sellers' 402 challenges) — per entity, not per protocol. The ingest layer records
the protocol source; the entity layer does not care. A daily calendar is never
backfilled, and a chained digest of every day's snapshot is anchored to Bitcoin
via [OpenTimestamps](https://opentimestamps.org) — publicly verifiable, zero cost.

## Install

No PyPI release yet — install from source:

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
```

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

## License

MIT
