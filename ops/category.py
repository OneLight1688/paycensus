#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayCensus Category Tagger — stage-2 seed: "what do agents pay for" taxonomy.
Classifies attributed entities into product/service categories from their own
public storefront metadata (title/description keywords). Transparent word lists.
Accumulates from day one: category history is unbackfillable once entities churn.
Publishes /data/categories.json: counts + representative attributed entities per
category (public fields only). Internal-only data (contacts/addr_hints) excluded.
Run: python3 category.py    (cron hourly :40, and after classify_v2 in daily chain)
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB = "/var/lib/paycensus/census.db"
OUT = "/www/wwwroot/paycensus/data/categories.json"

# Transparent keyword taxonomy (lowercase substrings). Order = priority (first match wins).
CATEGORY_KW = [
    ("AI/LLM services", ["llm", "gpt", " claude", " ai model", "inference", "ai agent",
                         "openai", "prompt", "embedding", "chatbot", "genai", "generative"]),
    ("Data extraction & conversion", ["markdown", "pdf", "docx", "csv", "extract", "scrape",
                                      "crawl", "html-to", "to-markdown", "parser", "ocr",
                                      "transcribe", "convert"]),
    ("Finance & market data", ["defi", "swap", "token", "wallet", "price feed", "trading",
                               "crypto snapshot", "tvl", "portfolio", "airdrop", "on-chain",
                               "market data", "funding rate", "yield"]),
    ("Search & web intelligence", ["search", "seo", "serp", "web intelligence", "domain intel",
                                   "whois", "dns", "url ", "metadata", "archive"]),
    ("Verification & trust", ["verif", "audit", "notar", "proof", "kyc", "compliance",
                              "signature", "attestation", "risk"]),
    ("Dev tools & infra", ["sdk", "github", "mcp", "toolkit", "boilerplate", "starter",
                           "api gateway", "facilitator", "hosting", "deploy", "monitor",
                           "uptime", "log"]),
    ("Media & content generation", ["image", "video", "tts", "text-to", "voice", "avatar",
                                    "render", "thumbnail", "music", "photo"]),
    ("Comms & notifications", ["email", "notification", "sms", "message", "webhook",
                               "telegram", "discord"]),
    ("E-commerce & listings", ["shop", "product", "cart", "listing", "marketplace",
                               "merchant", "order"]),
    ("Weather & environment", ["weather", "climate", "forecast", "air quality", "geo"]),
]


def classify(text):
    t = text.lower()
    for name, kws in CATEGORY_KW:
        if any(k in t for k in kws):
            return name
    return "Other"


def main():
    conn = sqlite3.connect(DB, timeout=30)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(entities)")]
    if "category" not in cols:
        conn.execute("ALTER TABLE entities ADD COLUMN category TEXT")
        conn.commit()

    rows = conn.execute(
        "SELECT addr, COALESCE(title,''), COALESCE(description,'') FROM entities "
        "WHERE addr IN (SELECT addr FROM sellers)").fetchall()
    counts = {}
    for addr, title, desc in rows:
        cat = classify(f"{title} {desc}")
        counts[cat] = counts.get(cat, 0) + 1
        conn.execute("UPDATE entities SET category=? WHERE addr=?", (cat, addr))
    conn.commit()

    # representative public entities per category (public fields only)
    reps = {}
    for addr, title, desc in rows:
        cat = classify(f"{title} {desc}")
        if cat == "Other" or cat in reps and len(reps[cat]) >= 3:
            continue
        row = conn.execute(
            "SELECT domain, title, first_seen FROM entities WHERE addr=? AND addr IN "
            "(SELECT addr FROM sellers)", (addr,)).fetchone()
        if row and row[0]:
            reps.setdefault(cat, []).append(
                {"domain": row[0][:70], "title": (row[1] or "")[:60], "first_seen": row[2]})

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "note": "Category taxonomy of attributed x402 sellers, from their own public storefront "
                "metadata (transparent keyword lists). Accumulates from day one: category demand "
                "history is never backfilled. Contact information is never published.",
        "method": "first-match keyword priority list at github.com/OneLight1688/paycensus-x402-census",
        "total_attributed": sum(counts.values()),
        "categories": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "representative_entities": {k: v for k, v in sorted(reps.items())},
    }
    tmp = OUT + ".tmp"
    json.dump(out, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    conn.close()
    print(json.dumps(out["categories"], indent=1))


if __name__ == "__main__":
    main()
