"""
Pass 2 — ahm hospital pricing via Firecrawl.

Strategy:
  ahm publishes all hospital plans on a single /all page with rebated
  weekly prices (single, NSW, age <65, $750 excess, Base Tier rebate).
  ONE Firecrawl call gets every plan; regex extracts the repeating
  tier/price/name/slug block per plan.

Cost per daily run: 1 Firecrawl scrape. No Claude needed.
"""

from __future__ import annotations

import os
import re
import sys

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

LIST_URL = "https://www.ahm.com.au/health-insurance/hospital-cover/all"
REBATE_RATE = 0.24118  # ahm states 24.118% on the page footnote

# Each plan block on the /all page follows the pattern:
#   <tier-line>
#   $<dollars>
#   .<cents>\*/ week
#   **<plan name>**
#   ...
#   [...](https://ahm.com.au/health-insurance/hospital-cover/<slug>)
#
# Whitespace/blank lines between pieces varies. DOTALL + non-greedy
# .*? between sections handles that.
PLAN_RE = re.compile(
    r"(Basic(?:\s+plus)?|Bronze(?:\s+plus)?|Silver(?:\s+plus)?|Gold)\s*\n"
    r".*?"
    r"\$(\d+)\s*\n+\s*\.(\d+)\\?\*/\s*week\s*\n+"
    r"\s*\*\*([^*\n]+?)\*\*"
    r".*?"
    r"\(https://ahm\.com\.au/health-insurance/hospital-cover/([\w\-]+)\)",
    re.DOTALL | re.IGNORECASE,
)

TIER_NORMALIZE = {
    "basic":       "Basic",
    "basic plus":  "Basic Plus",
    "bronze":      "Bronze",
    "bronze plus": "Bronze Plus",
    "silver":      "Silver",
    "silver plus": "Silver Plus",
    "gold":        "Gold",
}


def derive_premiums_from_rebated(rebated_weekly: float) -> dict:
    base_weekly = rebated_weekly / (1 - REBATE_RATE)
    base_monthly = round(base_weekly * 52 / 12, 2)
    couple = round(base_monthly * 2, 2)
    return {
        "single": base_monthly,
        "couple": couple,
        "family": couple,
        "single_parent_family": base_monthly,
    }


def fetch_and_extract() -> list[dict]:
    """One Firecrawl call + regex. Returns list of plan dicts."""
    data = scrape(LIST_URL, formats=["markdown"], only_main_content=True)
    md = data.get("markdown") or ""
    if not md:
        raise FirecrawlError("ahm /all returned empty markdown")

    plans = []
    seen_slugs = set()
    for m in PLAN_RE.finditer(md):
        tier_raw, dollars, cents, name, slug = m.groups()
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        tier = TIER_NORMALIZE.get(tier_raw.strip().lower(), tier_raw.strip())
        rebated_weekly = float(f"{dollars}.{cents}")
        plans.append({
            "tier": tier,
            "name": name.strip(),
            "slug": slug,
            "price_weekly_rebated": rebated_weekly,
        })
    return plans


def update_products(products: dict, plans: list[dict]) -> tuple[list[dict], list[dict]]:
    """Update products.json[funds.ahm.hospital] in place.

    Returns (results, new_plan_records). new_plan_records lists plans
    that came from the scrape but weren't already in products.json —
    surfaced in the final report so the user can see additions.
    """
    hospital = products["funds"]["ahm"]["hospital"]
    by_slug = {plan.get("id"): plan for plan in hospital}
    by_name = {plan.get("name", "").lower(): plan for plan in hospital}

    results = []
    new_records = []

    for p in plans:
        name = p.get("name", "").strip()
        slug = p.get("slug", "").strip()
        tier = p.get("tier", "").strip()
        rebated = p.get("price_weekly_rebated")

        if not name or rebated is None:
            results.append({"plan_name": name or "?", "status": "failed",
                            "reason": "missing name or price_weekly_rebated in extracted data"})
            continue

        # Try to match: by slug (id), then by case-insensitive name
        target = by_slug.get(slug) or by_name.get(name.lower())

        new_bucket = derive_premiums_from_rebated(float(rebated))

        if target is None:
            # New plan discovered — add it
            new_plan = {
                "id": slug or name.lower().replace(" ", "-").replace("(", "").replace(")", ""),
                "name": name,
                "tier": tier,
                "status": "current",
                "excess_options": ["750"],
                "premiums": {"750": new_bucket},
                "factsheet_url": f"https://www.ahm.com.au/health-insurance/hospital-cover/{slug}" if slug else None,
                "last_verified": today(),
                "source": "firecrawl",
            }
            hospital.append(new_plan)
            new_records.append({"plan_name": name, "tier": tier, "slug": slug})
            results.append({
                "plan_name": name, "tier": tier, "status": "success",
                "rebated_weekly": rebated, "is_new": True, "diffs": {},
            })
            continue

        old_bucket = dict(target.get("premiums", {}).get("750", {}))
        target.setdefault("premiums", {})["750"] = new_bucket
        if slug:
            target["factsheet_url"] = f"https://www.ahm.com.au/health-insurance/hospital-cover/{slug}"
        if tier:
            target["tier"] = tier
        target["last_verified"] = today()
        target["source"] = "firecrawl"

        diffs = {k: {"old": old_bucket.get(k), "new": new_bucket.get(k)}
                 for k in new_bucket if old_bucket.get(k) != new_bucket.get(k)}
        results.append({
            "plan_name": name, "tier": tier, "status": "success",
            "rebated_weekly": rebated, "is_new": False, "diffs": diffs,
        })

    return results, new_records


def build_sidecar(plans: list[dict]) -> dict:
    return {
        "excess": "750",
        "state": "NSW",
        "rebate": "base_tier_under_65",
        "rebate_rate_used": REBATE_RATE,
        "note": "Scraped from ahm /all page via Firecrawl + Claude extraction. Prices on page are rebated; base back-calculated.",
        "plans": [
            {
                "name": p.get("name"),
                "tier": p.get("tier"),
                "slug": p.get("slug"),
                "price_weekly_rebated": p.get("price_weekly_rebated"),
            }
            for p in plans
        ],
    }


def format_result(r: dict) -> str:
    if r.get("status") == "success":
        marker = "  [NEW] " if r.get("is_new") else "  [OK]  "
        if r["diffs"]:
            parts = [f"{k}: {v['old']}→{v['new']}" for k, v in r["diffs"].items()]
            change_str = f"changed: {', '.join(parts)}"
        elif r["is_new"]:
            change_str = "added"
        else:
            change_str = "no changes"
        return (f"{marker}{r['plan_name']}  ${r['rebated_weekly']}/wk rebated  "
                f"({r['tier']})  |  {change_str}")
    return f"  [FAIL] {r['plan_name']}: {r['reason']}"


def run(dry_run: bool = False) -> dict:
    try:
        plans = fetch_and_extract()
    except (FirecrawlError, ExtractError) as e:
        return {"status": "failed", "plans_found": 0, "plans_failed": 0,
                "error": str(e), "last_updated": now_iso()}

    products = read_products()
    results, new_records = update_products(products, plans)
    for r in results:
        print(format_result(r))

    if not dry_run:
        write_products(products)
        write_json_atomic(fund_plans_file("ahm"), build_sidecar(plans))

    successes = [r for r in results if r["status"] == "success"]
    failures = [r for r in results if r["status"] == "failed"]
    return {
        "status": "success" if not failures else "partial",
        "plans_found": len(successes),
        "plans_failed": len(failures),
        "plans_added": len(new_records),
        "last_updated": now_iso(),
        "source": "firecrawl + haiku",
        "rebate_rate_used": REBATE_RATE,
        "errors": [{"plan": r["plan_name"], "reason": r["reason"]} for r in failures],
        "new_plans": new_records,
    }


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(f"Pass 2 — ahm starting at {now_iso()}"
          f"{' (DRY RUN)' if dry else ''}")
    summary = run(dry_run=dry)
    print(f"\nDone: {summary['plans_found']} updated "
          f"({summary.get('plans_added', 0)} new), "
          f"{summary['plans_failed']} failed")

    if not dry:
        meta = read_meta()
        meta.setdefault("funds", {})["ahm"] = summary
        meta["last_updated"] = now_iso()
        write_meta(meta)
        print("Wrote meta.json")

    sys.exit(0 if summary["status"] == "success" else 1)
