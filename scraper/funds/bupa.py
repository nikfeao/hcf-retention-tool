"""
Pass 2 — Bupa hospital pricing via Firecrawl.

Scrapes each of Bupa's 8 hospital plan pages, extracts the displayed
weekly rebated price, back-calculates the base price using the Base Tier
rebate rate, derives couple/family/SPF via community rating, and writes
to products.json + bupa-plans.json.

Why rebated → base:
  The Bupa plan page shows a rebated weekly price by default (e.g.
  "The premium is$28.00"). Under community rating and the 2025-26
  Australian Government rebate for Base Tier under-65, the relationship
  is: base = rebated / (1 - 0.24608). Matches the existing back-
  calculated numbers in products.json exactly.

Why regex instead of Claude:
  The price consistently appears in the exact text "The premium is$X.XX"
  across every Bupa plan page. No ambiguity — regex is simpler, faster,
  and doesn't spend Anthropic tokens.
"""

from __future__ import annotations

import os
import re
import sys
import time

# Make `lib` importable when running this file directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.firecrawl import FirecrawlError, scrape
from lib.products_io import (
    fund_plans_file,
    now_iso,
    read_meta,
    read_products,
    today,
    write_json_atomic,
    write_meta,
    write_products,
)

REBATE_RATE = 0.24608  # Base Tier, under-65, FY 2025-26
BUPA_URL = "https://www.bupa.com.au/health-insurance/cover/{slug}"
PRICE_RE = re.compile(r"The premium is\s*\$(\d+(?:\.\d+)?)")

# Known Bupa hospital plans. Names must match products.json exactly.
BUPA_PLANS = [
    {"slug": "basic-accident-only-hospital",   "name": "Basic Accident Only Hospital",   "tier": "Basic"},
    {"slug": "basic-plus-starter-hospital",    "name": "Basic Plus Starter Hospital",    "tier": "Basic Plus"},
    {"slug": "bronze-hospital",                "name": "Bronze Hospital",                "tier": "Bronze"},
    {"slug": "bronze-plus-select-hospital",    "name": "Bronze Plus Select Hospital",    "tier": "Bronze Plus"},
    {"slug": "bronze-plus-advantage-hospital", "name": "Bronze Plus Advantage Hospital", "tier": "Bronze Plus"},
    {"slug": "silver-plus-classic-hospital",   "name": "Silver Plus Classic Hospital",   "tier": "Silver Plus"},
    {"slug": "silver-plus-advanced-hospital",  "name": "Silver Plus Advanced Hospital",  "tier": "Silver Plus"},
    {"slug": "gold-comprehensive-hospital",    "name": "Gold Comprehensive Hospital",    "tier": "Gold"},
]


def scrape_plan(slug: str) -> dict:
    """Firecrawl → regex extract. Returns {rebated_weekly, url} or {error}."""
    url = BUPA_URL.format(slug=slug)
    try:
        data = scrape(url, formats=["markdown"], only_main_content=True)
    except FirecrawlError as e:
        return {"error": f"firecrawl: {e}"}

    markdown = data.get("markdown") or ""
    match = PRICE_RE.search(markdown)
    if not match:
        return {"error": "price pattern not found in markdown"}

    return {"rebated_weekly": float(match.group(1)), "url": url}


def derive_base_premiums(rebated_weekly: float) -> dict:
    """Rebated weekly → base premiums for single/couple/family/SPF (monthly)."""
    base_weekly = rebated_weekly / (1 - REBATE_RATE)
    base_monthly = round(base_weekly * 52 / 12, 2)
    couple = round(base_monthly * 2, 2)
    return {
        "single": base_monthly,
        "couple": couple,
        "family": couple,
        "single_parent_family": base_monthly,
    }


def update_products(products: dict, results: list[dict]) -> list[dict]:
    """Update products.json[funds.bupa.hospital] in place."""
    hospital = products["funds"]["bupa"]["hospital"]
    by_name = {plan["name"]: plan for plan in hospital}

    out = []
    for r in results:
        if r.get("status") != "success":
            out.append({
                "plan_name": r["name"],
                "tier": r["tier"],
                "status": "failed",
                "reason": r["reason"],
            })
            continue

        target = by_name.get(r["name"])
        if not target:
            out.append({
                "plan_name": r["name"],
                "tier": r["tier"],
                "status": "failed",
                "reason": f"name {r['name']!r} not found in products.json",
            })
            continue

        old_bucket = dict(target.get("premiums", {}).get("750", {}))
        new_bucket = derive_base_premiums(r["rebated_weekly"])

        target.setdefault("premiums", {})
        target["premiums"]["750"] = new_bucket
        target["factsheet_url"] = r["url"]
        target["last_verified"] = today()
        target["source"] = "firecrawl"

        diffs = {
            k: {"old": old_bucket.get(k), "new": new_bucket.get(k)}
            for k in new_bucket
            if old_bucket.get(k) != new_bucket.get(k)
        }
        out.append({
            "plan_name": r["name"],
            "tier": r["tier"],
            "status": "success",
            "rebated_weekly": r["rebated_weekly"],
            "base_monthly_single": new_bucket["single"],
            "diffs": diffs,
        })
    return out


def build_sidecar(results: list[dict]) -> dict:
    """Build data/bupa-plans.json sidecar — matches existing file structure."""
    ok = [r for r in results if r.get("status") == "success"]
    plans = []
    for r in ok:
        rebated_weekly = r["rebated_weekly"]
        base_weekly = round(rebated_weekly / (1 - REBATE_RATE), 2)
        base_monthly = round(base_weekly * 52 / 12, 2)
        rebated_monthly = round(rebated_weekly * 52 / 12, 2)
        plans.append({
            "name": r["name"],
            "tier": r["tier"],
            "price_weekly": base_weekly,
            "price_monthly": base_monthly,
            "price_weekly_rebated": rebated_weekly,
            "price_monthly_rebated": rebated_monthly,
            "excess": "750",
            "excess_options": ["500", "750"],
            "factsheet_url": BUPA_URL.format(slug=next(p["slug"] for p in BUPA_PLANS if p["name"] == r["name"])),
        })
    return {
        "excess": "750",
        "state": "NSW",
        "rebate": "base_tier_under_65",
        "rebate_rate_used": REBATE_RATE,
        "note": (
            f"Scraped from Bupa plan pages via Firecrawl. Prices on page are "
            f"rebated; base prices back-calculated using {REBATE_RATE:.3%} rebate."
        ),
        "plans": plans,
    }


def format_result(r: dict) -> str:
    if r.get("status") == "success":
        if r["diffs"]:
            parts = [f"{k}: {v['old']}→{v['new']}" for k, v in r["diffs"].items()]
            change_str = f"changed: {', '.join(parts)}"
        else:
            change_str = "no changes"
        return (f"  [OK]   {r['plan_name']}  "
                f"${r['rebated_weekly']}/wk rebated → ${r['base_monthly_single']}/mo base single "
                f"({r['tier']})  |  {change_str}")
    return f"  [FAIL] {r['plan_name']}: {r['reason']}"


def run(dry_run: bool = False) -> dict:
    scraped = []
    for plan in BUPA_PLANS:
        result = scrape_plan(plan["slug"])
        if "error" in result:
            scraped.append({
                "name": plan["name"], "tier": plan["tier"],
                "status": "failed", "reason": result["error"],
            })
        else:
            scraped.append({
                "name": plan["name"], "tier": plan["tier"],
                "status": "success",
                "rebated_weekly": result["rebated_weekly"],
                "url": result["url"],
            })
        time.sleep(0.5)  # be polite

    products = read_products()
    diffs = update_products(products, scraped)
    for d in diffs:
        print(format_result(d))

    if not dry_run:
        write_products(products)
        write_json_atomic(fund_plans_file("bupa"), build_sidecar(scraped))

    successes = [d for d in diffs if d["status"] == "success"]
    failures = [d for d in diffs if d["status"] == "failed"]
    return {
        "status": "success" if not failures else ("partial" if successes else "failed"),
        "plans_found": len(successes),
        "plans_failed": len(failures),
        "last_updated": now_iso(),
        "source": "firecrawl",
        "rebate_rate_used": REBATE_RATE,
        "errors": [
            {"plan": d["plan_name"], "reason": d["reason"]} for d in failures
        ],
    }


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(f"Pass 2 — Bupa starting at {now_iso()}"
          f"{' (DRY RUN — no files written)' if dry else ''}")

    summary = run(dry_run=dry)
    print(f"\nDone: {summary['plans_found']} updated, "
          f"{summary['plans_failed']} failed")

    if not dry:
        meta = read_meta()
        meta.setdefault("funds", {})["bupa"] = summary
        meta["last_updated"] = now_iso()
        write_meta(meta)
        print("Wrote meta.json")

    sys.exit(0 if summary["status"] == "success" else 1)
