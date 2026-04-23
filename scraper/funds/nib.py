"""
Pass 2 — nib.

Fetches current nib hospital prices from their public pricing API and
refreshes products.json[funds.nib.hospital] + data/nib-plans.json.

No Firecrawl, no Claude — nib exposes a structured JSON endpoint that
returns clean data directly. Premiums for couple/family/SPF are derived
from the single premium via community rating.

Ported from scripts/nib-lookup/quote_scraper.py. That file stays in
place (it's used by the CLI /nib-lookup skill) but the hcf-tool GitHub
Action only checks out this repo, so the logic is duplicated here.
"""

from __future__ import annotations

import os
import sys

# Make `lib` importable when running this file directly
# (e.g. `python scraper/funds/nib.py`)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

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

PRICING_API = "https://api-gateway.nib.com.au/pricing-api-lambda/v1/australian-resident"
COLLATERAL_BASE = "https://my.nib.com.au/product-collateral/{}"
TIMEOUT_SECONDS = 15

# products.json entries use these ids (cross-checked against existing data)
NIB_PRODUCTS = {
    2785: {"id": "nib-basic-care-hospital-plus",     "name": "Basic Care Hospital Plus",     "tier": "Basic Plus"},
    2786: {"id": "nib-bronze-protect-hospital-plus", "name": "Bronze Protect Hospital Plus", "tier": "Bronze Plus"},
    2787: {"id": "nib-silver-secure-hospital-plus",  "name": "Silver Secure Hospital Plus",  "tier": "Silver Plus"},
    53:   {"id": "nib-mid-hospital---silver-plus",   "name": "Mid Hospital - Silver Plus",   "tier": "Silver Plus"},
    729:  {"id": "nib-silver-select-hospital-plus",  "name": "Silver Select Hospital Plus",  "tier": "Silver Plus"},
}

HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.nib.com.au",
    "Referer": "https://www.nib.com.au/",
}


def get_nib_quotes(excess: str = "750", state: str = "NSW") -> list[dict]:
    """Call nib's pricing API, return all 5 hospital plans with fresh prices."""
    params = {
        "excess": excess,
        "previousCover": "true",
        "partnerPreviousCover": "true",
        "rebateTier": "0",
        "applyRebate": "false",
        "effectiveDate": today(),
        "rate": "0",
        "paymentFrequency": "Weekly",
        "dob": "1990-01-01",
        "state": state,
        "scale": "Single",
    }
    for i, pid in enumerate(NIB_PRODUCTS.keys()):
        params[f"products[{i}][hospitalProduct]"] = str(pid)

    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        resp = client.get(PRICING_API, params=params, headers=HEADERS)
        resp.raise_for_status()
        payload = resp.json()

    api_data = {
        item["hospital"]["id"]: item["hospital"]["baseRate"]
        for item in payload.get("data", [])
        if "hospital" in item
    }

    quotes = []
    for pid, meta in NIB_PRODUCTS.items():
        base_rate = api_data.get(pid)
        if base_rate is None:
            continue
        weekly = float(base_rate)
        monthly = round(weekly * 52 / 12, 2)
        quotes.append({
            "pid": pid,
            "id": meta["id"],
            "name": meta["name"],
            "tier": meta["tier"],
            "excess": excess,
            "price_weekly": weekly,
            "price_monthly": monthly,
            "factsheet_url": COLLATERAL_BASE.format(pid),
        })
    return quotes


def derive_premiums(monthly_single: float) -> dict:
    """Community rating: couple = family = 2 × single; SPF = single."""
    couple = round(monthly_single * 2, 2)
    return {
        "single": monthly_single,
        "couple": couple,
        "family": couple,
        "single_parent_family": monthly_single,
    }


def update_products(products: dict, quotes: list[dict]) -> list[dict]:
    """Update products.json[funds.nib.hospital] with fresh data."""
    hospital = products["funds"]["nib"]["hospital"]
    by_id = {plan["id"]: plan for plan in hospital}

    results = []
    for q in quotes:
        target = by_id.get(q["id"])
        if not target:
            results.append({
                "plan_id": q["id"],
                "status": "failed",
                "reason": f"id {q['id']!r} not found in products.json nib.hospital",
            })
            continue

        excess_key = q["excess"]
        old_bucket = dict(target.get("premiums", {}).get(excess_key, {}))
        new_bucket = derive_premiums(q["price_monthly"])

        target.setdefault("premiums", {})
        target["premiums"][excess_key] = new_bucket
        target["factsheet_url"] = q["factsheet_url"]
        target["tier"] = q["tier"]
        target["last_verified"] = today()
        target["source"] = "nib-pricing-api"

        diffs = {
            k: {"old": old_bucket.get(k), "new": new_bucket.get(k)}
            for k in new_bucket
            if old_bucket.get(k) != new_bucket.get(k)
        }
        results.append({
            "plan_id": q["id"],
            "status": "success",
            "tier": q["tier"],
            "price_weekly": q["price_weekly"],
            "price_monthly": q["price_monthly"],
            "excess": excess_key,
            "diffs": diffs,
        })
    return results


def build_nib_plans_sidecar(quotes: list[dict], excess: str, state: str) -> dict:
    """Build the data/nib-plans.json sidecar (matches existing structure)."""
    return {
        "plans": [
            {
                "name": q["name"],
                "price_weekly": q["price_weekly"],
                "price_monthly": q["price_monthly"],
                "excess": q["excess"],
                "factsheet_url": q["factsheet_url"],
                "tier": q["tier"],
                "pid": q["pid"],
            }
            for q in quotes
        ],
        "state": state,
        "excess": excess,
    }


def format_result(r: dict) -> str:
    if r["status"] == "success":
        if r["diffs"]:
            parts = [f"{k}: {v['old']}→{v['new']}" for k, v in r["diffs"].items()]
            change_str = f"changed: {', '.join(parts)}"
        else:
            change_str = "no changes"
        return (f"  [OK]   {r['plan_id']}  "
                f"${r['price_weekly']}/wk = ${r['price_monthly']}/mo "
                f"({r['tier']}, excess {r['excess']})  |  {change_str}")
    return f"  [FAIL] {r['plan_id']}: {r['reason']}"


def run(dry_run: bool = False) -> dict:
    excess = "750"
    state = "NSW"

    try:
        quotes = get_nib_quotes(excess=excess, state=state)
    except Exception as e:
        return {
            "status": "failed",
            "plans_found": 0,
            "plans_failed": 0,
            "error": f"quote API: {e}",
            "last_updated": now_iso(),
        }

    if not quotes:
        return {
            "status": "failed",
            "plans_found": 0,
            "plans_failed": 0,
            "error": "nib API returned no quotes",
            "last_updated": now_iso(),
        }

    products = read_products()
    results = update_products(products, quotes)
    for r in results:
        print(format_result(r))

    if not dry_run:
        write_products(products)
        sidecar = build_nib_plans_sidecar(quotes, excess=excess, state=state)
        write_json_atomic(fund_plans_file("nib"), sidecar)

    failures = [r for r in results if r["status"] == "failed"]
    return {
        "status": "success" if not failures else "partial",
        "plans_found": sum(1 for r in results if r["status"] == "success"),
        "plans_failed": len(failures),
        "last_updated": now_iso(),
        "source": "nib-pricing-api",
        "errors": [
            {"plan": r["plan_id"], "reason": r["reason"]} for r in failures
        ],
    }


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(f"Pass 2 — nib starting at {now_iso()}"
          f"{' (DRY RUN — no files written)' if dry else ''}")

    summary = run(dry_run=dry)
    print(f"\nDone: {summary['plans_found']} updated, "
          f"{summary['plans_failed']} failed")

    if not dry:
        meta = read_meta()
        meta.setdefault("funds", {})["nib"] = summary
        meta["last_updated"] = now_iso()
        write_meta(meta)
        print("Wrote meta.json")

    sys.exit(0 if summary["status"] == "success" else 1)
