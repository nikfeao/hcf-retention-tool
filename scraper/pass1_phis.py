"""
Pass 1 — PHIS refresher.

Walks products.json, finds every hospital plan with a phis_url, fetches
the privatehealth.gov.au page, extracts the monthly premium + cover
composition, and updates the plan's phis_premiums bucket.

Plain httpx + BeautifulSoup. No Firecrawl, no Anthropic API.
"""

from __future__ import annotations

import re
import sys
import time

import httpx
from bs4 import BeautifulSoup

from lib.products_io import (
    now_iso,
    read_meta,
    read_products,
    today,
    write_meta,
    write_products,
)

USER_AGENT = "HCF-Retention-Tool/1.0 (+https://tools.newain.com.au)"
TIMEOUT_SECONDS = 30


def parse_phis_page(html: str) -> dict:
    """Extract {monthly_premium, composition} from a PHIS HTML page.

    Returns {"error": "..."} on failure.
    """
    soup = BeautifulSoup(html, "html.parser")

    premium_div = soup.select_one("div.premium")
    cover_div = soup.select_one("div.cover")
    if not premium_div or not cover_div:
        return {"error": "div.premium or div.cover not found"}

    h2s = premium_div.find_all("h2")
    if len(h2s) < 2:
        return {"error": "expected >= 2 h2 tags inside div.premium"}

    price_match = re.search(r"\$([\d,]+\.?\d*)", h2s[1].get_text())
    if not price_match:
        return {"error": f"no $ amount in {h2s[1].get_text()!r}"}

    monthly_premium = float(price_match.group(1).replace(",", ""))

    cover_text = cover_div.find("p").get_text().strip().lower()
    has_dependants = "dependant" in cover_text or "dependent children" in cover_text
    two_adults = "two adults" in cover_text or "2 adults" in cover_text
    one_adult_phrase = (
        "one adult " in cover_text
        or "1 adult " in cover_text
        or cover_text.endswith("one adult")
        or cover_text.endswith("1 adult")
    )

    if "only one person" in cover_text:
        composition = "single"
    elif two_adults and has_dependants:
        composition = "family"
    elif one_adult_phrase and has_dependants:
        composition = "single_parent_family"
    elif two_adults:
        composition = "couple"
    else:
        return {"error": f"unknown cover composition: {cover_text!r}"}

    return {"monthly_premium": monthly_premium, "composition": composition}


def derive_all_premiums(monthly_premium: float, composition: str) -> dict:
    """Derive single/couple/family/SPF from one scenario via community rating.

    Couple & family = 2 × single. Single-parent family = single.
    """
    if composition in ("single", "single_parent_family"):
        single = monthly_premium
    elif composition in ("couple", "family"):
        single = monthly_premium / 2
    else:
        raise ValueError(f"unknown composition: {composition}")

    couple = single * 2
    return {
        "single": round(single, 2),
        "couple": round(couple, 2),
        "family": round(couple, 2),
        "single_parent_family": round(single, 2),
    }


def fetch_phis(url: str) -> tuple[str | None, str | None]:
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.text, None
    except httpx.HTTPError as e:
        return None, str(e)


def refresh_plan(plan: dict, fund_key: str) -> dict:
    plan_id = plan.get("id") or plan.get("name", "?")
    url = plan.get("phis_url")
    if not url:
        return {"plan_id": plan_id, "fund": fund_key, "status": "skipped",
                "reason": "no phis_url"}

    html, err = fetch_phis(url)
    if err:
        return {"plan_id": plan_id, "fund": fund_key, "status": "failed",
                "reason": f"fetch: {err}"}

    parsed = parse_phis_page(html)
    if "error" in parsed:
        return {"plan_id": plan_id, "fund": fund_key, "status": "failed",
                "reason": f"parse: {parsed['error']}"}

    phis_premiums = plan.get("phis_premiums")
    if not phis_premiums:
        return {"plan_id": plan_id, "fund": fund_key, "status": "failed",
                "reason": "plan has phis_url but no phis_premiums schema"}

    excess_keys = list(phis_premiums.keys())
    if len(excess_keys) != 1:
        return {"plan_id": plan_id, "fund": fund_key, "status": "failed",
                "reason": f"expected 1 excess key, got {excess_keys}"}

    excess = excess_keys[0]
    old_bucket = dict(phis_premiums[excess])
    new_bucket = derive_all_premiums(parsed["monthly_premium"], parsed["composition"])
    if "_note" in old_bucket:
        new_bucket["_note"] = old_bucket["_note"]

    plan["phis_premiums"][excess] = new_bucket
    plan["phis_last_verified"] = today()

    diffs = {}
    for k in ("single", "couple", "family", "single_parent_family"):
        if old_bucket.get(k) != new_bucket.get(k):
            diffs[k] = {"old": old_bucket.get(k), "new": new_bucket.get(k)}

    return {
        "plan_id": plan_id,
        "fund": fund_key,
        "status": "success",
        "composition": parsed["composition"],
        "monthly_premium": parsed["monthly_premium"],
        "excess": excess,
        "diffs": diffs,
    }


def format_result(r: dict) -> str:
    if r["status"] == "success":
        if r["diffs"]:
            parts = [f"{k}: {v['old']}→{v['new']}" for k, v in r["diffs"].items()]
            change_str = f"changed: {', '.join(parts)}"
        else:
            change_str = "no changes"
        return (f"  [OK]   {r['fund']}/{r['plan_id']}  "
                f"excess {r['excess']}, {r['composition']} ${r['monthly_premium']}  |  "
                f"{change_str}")
    if r["status"] == "failed":
        return f"  [FAIL] {r['fund']}/{r['plan_id']}: {r['reason']}"
    return f"  [SKIP] {r['fund']}/{r['plan_id']}: {r['reason']}"


def run(dry_run: bool = False) -> dict:
    """Main entry. If dry_run, does not write products.json."""
    products = read_products()
    results = []

    for fund_key, fund in products.get("funds", {}).items():
        for plan in fund.get("hospital", []):
            if plan.get("phis_url"):
                result = refresh_plan(plan, fund_key)
                results.append(result)
                print(format_result(result))
                time.sleep(0.5)

    if not dry_run:
        write_products(products)

    successes = [r for r in results if r["status"] == "success"]
    failures = [r for r in results if r["status"] == "failed"]

    return {
        "status": "success" if results and not failures else
                  ("partial" if successes else "failed"),
        "plans_refreshed": len(successes),
        "plans_failed": len(failures),
        "plans_skipped": sum(1 for r in results if r["status"] == "skipped"),
        "errors": [
            {"plan": r["plan_id"], "fund": r["fund"], "reason": r["reason"]}
            for r in failures
        ],
        "last_updated": now_iso(),
    }


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(f"Pass 1 — PHIS refresher starting at {now_iso()}"
          f"{' (DRY RUN — no files written)' if dry else ''}")
    summary = run(dry_run=dry)
    print(f"\nDone: {summary['plans_refreshed']} refreshed, "
          f"{summary['plans_failed']} failed, {summary['plans_skipped']} skipped")

    if not dry:
        meta = read_meta()
        meta["pass1_phis"] = summary
        meta["last_updated"] = now_iso()
        write_meta(meta)
        print("Wrote meta.json")

    sys.exit(0 if summary["status"] == "success" else 1)
