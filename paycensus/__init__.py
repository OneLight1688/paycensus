# -*- coding: utf-8 -*-
"""paycensus: read-only client for the PayCensus machine-payment census data layer
(x402 + MPP and successors — protocol-agnostic entity layer)."""
from .client import PayCensus

__version__ = "0.4.0"
__all__ = ["PayCensus"]
