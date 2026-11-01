# x402-census

Read-only Python client for the [PayCensus](https://paycensus.com) machine-payment
census data layer — *"The census of the machine economy."*

PayCensus indexes **who pays whom** in the x402 machine-payment ecosystem
(per-entity, not per-protocol), publishes a daily calendar that is never
backfilled, and anchors a chained digest of every day's snapshot to Bitcoin
via [OpenTimestamps](https://opentimestamps.org) — publicly verifiable, zero cost.

## Install

No PyPI release yet — install from source:

```bash
pip install git+https://github.com/OneLight1688/paycensus-x402-census.git
```

## Usage

Zero dependencies (stdlib only).

```python
from x402_census import PayCensus

pc = PayCensus()

pc.stats()                          # aggregate txs / volume / unique entities
pc.calendar()                       # daily calendar (never backfilled)
pc.anchoring()                      # Bitcoin-anchored daily digests (verify with ots)
pc.audit()                          # collection lineage + methodology

pc.edges(limit=20, min_volume=10)   # top buyer x seller relationship edges
pc.seller("0x…")                    # who pays this seller, daily series
pc.buyer("0x…")                     # what this buyer pays for
```

## Data boundaries

- On-chain facts (addresses, amounts, timing) are public data.
- Seller entities (domain/title/description) are the seller's own public storefront info.
- **Contact information is never collected into the public layer and never published.**
- Any seller can be removed from the entity layer within 24h (opt-out).
- Anchoring preserves *integrity*, not *correctness* — correctness comes from the
  public methodology in `/data/audit.json`, which anyone can independently re-run.

## License

MIT
