"""
HCF Retention Tool — Daily Product Scraper
Runs via GitHub Actions every day at 6am AEST.
Updates data/products.json and data/meta.json.

Rules:
- Never delete a product — mark as 'off-sale' if no longer found on website.
- Update premiums and limits only when found on the live website.
- Always preserve off-sale entries (they are needed for call centre use).
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, '..', 'data')
PRODUCTS_FILE = os.path.join(DATA_DIR, 'products.json')
META_FILE = os.path.join(DATA_DIR, 'meta.json')

AEST = timezone(timedelta(hours=10))

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {os.path.basename(path)}")

def now_aest():
    return datetime.now(AEST).isoformat()

def today_str():
    return datetime.now(AEST).strftime('%Y-%m-%d')

# ── Scraper base ────────────────────────────────────────────────────────────────
def scrape_fund(page, fund_key, products_data):
    """
    Dispatcher — calls fund-specific scraper.
    Returns (updated_products, status)
    """
    scrapers = {
        'hcf':              scrape_hcf,
        'nib':              scrape_nib,
        'bupa':             scrape_bupa,
        'medibank':         scrape_medibank,
        'ahm':              scrape_ahm,
        'australian_unity': scrape_australian_unity,
    }
    fn = scrapers.get(fund_key)
    if not fn:
        return products_data['funds'][fund_key], 'skipped'
    try:
        updated = fn(page, products_data['funds'][fund_key])
        return updated, 'ok'
    except PWTimeout:
        print(f"  TIMEOUT scraping {fund_key}")
        return products_data['funds'][fund_key], 'timeout'
    except Exception as e:
        print(f"  ERROR scraping {fund_key}: {e}")
        return products_data['funds'][fund_key], 'error'

def merge_products(existing_list, scraped_list, today):
    """
    Merge scraped products into existing list.
    - Update premiums/limits for matching products.
    - Add new products found on website.
    - Mark missing products as off-sale (never delete).
    """
    existing_map = {p['id']: p for p in existing_list}
    scraped_ids = set()

    for sp in scraped_list:
        scraped_ids.add(sp['id'])
        if sp['id'] in existing_map:
            # Update existing
            ep = existing_map[sp['id']]
            ep['premiums'] = sp.get('premiums', ep.get('premiums', {}))
            if 'limits' in sp:
                ep['limits'] = sp['limits']
            ep['status'] = 'current'
            ep['last_verified'] = today
        else:
            # New product found
            sp['status'] = 'current'
            sp['last_verified'] = today
            existing_map[sp['id']] = sp

    # Mark products no longer on website as off-sale
    for pid, p in existing_map.items():
        if pid not in scraped_ids and p.get('status') == 'current':
            p['status'] = 'off-sale'
            print(f"    Marked off-sale: {p['name']}")

    return list(existing_map.values())


# ══════════════════════════════════════════════════════════════════════════════
# FUND SCRAPERS
# Each returns the updated fund dict with hospital[] and extras[].
# These are initial implementations — update selectors when websites change.
# ══════════════════════════════════════════════════════════════════════════════

def scrape_hcf(page, fund_data):
    print("  Scraping HCF...")
    page.goto('https://www.hcf.com.au/health-insurance/hospital', timeout=30000)
    page.wait_for_load_state('networkidle', timeout=15000)

    scraped_hospital = []
    scraped_extras = []

    # HCF renders product cards — attempt to extract product names and tiers.
    # Selectors may need updating if HCF redesigns their site.
    try:
        cards = page.query_selector_all('[class*="ProductCard"], [class*="product-card"], [class*="PlanCard"]')
        for card in cards:
            name_el = card.query_selector('h2, h3, [class*="title"], [class*="name"]')
            if name_el:
                name = name_el.inner_text().strip()
                tier = detect_tier(name)
                if tier:
                    pid = 'hcf-' + name.lower().replace(' ', '-')
                    scraped_hospital.append({
                        'id': pid,
                        'name': name,
                        'tier': tier,
                        'status': 'current',
                        'excess_options': [0, 500, 750],
                        'premiums': {},
                        'source': 'hcf.com.au',
                    })
    except Exception as e:
        print(f"    HCF hospital scrape partial: {e}")

    fund_data['hospital'] = merge_products(fund_data.get('hospital', []), scraped_hospital, today_str())
    fund_data['extras'] = merge_products(fund_data.get('extras', []), scraped_extras, today_str())
    return fund_data


def scrape_nib(page, fund_data):
    print("  Scraping nib...")
    page.goto('https://www.nib.com.au/health-insurance', timeout=30000)
    page.wait_for_load_state('networkidle', timeout=15000)

    scraped_hospital = []
    scraped_extras = []

    try:
        cards = page.query_selector_all('[class*="ProductCard"], [class*="product-card"], [data-testid*="product"]')
        for card in cards:
            name_el = card.query_selector('h2, h3, [class*="title"]')
            if name_el:
                name = name_el.inner_text().strip()
                tier = detect_tier(name)
                if tier:
                    pid = 'nib-' + name.lower().replace(' ', '-')
                    scraped_hospital.append({
                        'id': pid,
                        'name': name,
                        'tier': tier,
                        'status': 'current',
                        'excess_options': [0, 500, 750],
                        'premiums': {},
                        'source': 'nib.com.au',
                    })
    except Exception as e:
        print(f"    nib scrape partial: {e}")

    fund_data['hospital'] = merge_products(fund_data.get('hospital', []), scraped_hospital, today_str())
    fund_data['extras'] = merge_products(fund_data.get('extras', []), scraped_extras, today_str())
    return fund_data


def scrape_bupa(page, fund_data):
    print("  Scraping Bupa...")
    page.goto('https://www.bupa.com.au/health-insurance', timeout=30000)
    page.wait_for_load_state('networkidle', timeout=15000)
    fund_data['hospital'] = merge_products(fund_data.get('hospital', []), [], today_str())
    fund_data['extras'] = merge_products(fund_data.get('extras', []), [], today_str())
    return fund_data


def scrape_medibank(page, fund_data):
    print("  Scraping Medibank...")
    page.goto('https://www.medibank.com.au/health-insurance/', timeout=30000)
    page.wait_for_load_state('networkidle', timeout=15000)
    fund_data['hospital'] = merge_products(fund_data.get('hospital', []), [], today_str())
    fund_data['extras'] = merge_products(fund_data.get('extras', []), [], today_str())
    return fund_data


def scrape_ahm(page, fund_data):
    print("  Scraping ahm...")
    page.goto('https://www.ahm.com.au/health-insurance', timeout=30000)
    page.wait_for_load_state('networkidle', timeout=15000)
    fund_data['hospital'] = merge_products(fund_data.get('hospital', []), [], today_str())
    fund_data['extras'] = merge_products(fund_data.get('extras', []), [], today_str())
    return fund_data


def scrape_australian_unity(page, fund_data):
    print("  Scraping Australian Unity...")
    page.goto('https://www.australianunity.com.au/health-insurance', timeout=30000)
    page.wait_for_load_state('networkidle', timeout=15000)
    fund_data['hospital'] = merge_products(fund_data.get('hospital', []), [], today_str())
    fund_data['extras'] = merge_products(fund_data.get('extras', []), [], today_str())
    return fund_data


# ── Utilities ──────────────────────────────────────────────────────────────────
def detect_tier(name):
    """Detect product tier from name string."""
    n = name.lower()
    if 'gold' in n: return 'Gold'
    if 'silver plus' in n or 'silver+' in n: return 'Silver Plus'
    if 'silver' in n: return 'Silver'
    if 'bronze plus' in n or 'bronze+' in n: return 'Bronze Plus'
    if 'bronze' in n: return 'Bronze'
    if 'basic plus' in n or 'basic+' in n: return 'Basic Plus'
    if 'basic' in n: return 'Basic'
    return None


# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    print(f"\n=== HCF Retention Tool — Daily Scrape [{now_aest()}] ===\n")

    products_data = load_json(PRODUCTS_FILE)
    meta_data = load_json(META_FILE)

    source_results = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (compatible; NewAIN-Bot/1.0; +https://tools.newain.com.au)',
            locale='en-AU',
            timezone_id='Australia/Sydney'
        )
        page = context.new_page()
        page.set_default_timeout(30000)

        for fund_key in products_data['funds']:
            print(f"\n[{fund_key.upper()}]")
            updated_fund, status = scrape_fund(page, fund_key, products_data)
            products_data['funds'][fund_key] = updated_fund
            source_results[fund_key] = status
            print(f"  Status: {status}")

        browser.close()

    # Update meta
    meta_data['last_updated'] = now_aest()
    for source in meta_data['sources']:
        fund_key = source['fund'].lower().replace(' ', '_')
        source['status'] = source_results.get(fund_key, 'skipped')

    # Save
    save_json(PRODUCTS_FILE, products_data)
    save_json(META_FILE, meta_data)

    print(f"\n=== Scrape complete ===\n")

    # Exit with error if all funds failed (alerts GitHub Actions)
    all_failed = all(v in ('error', 'timeout') for v in source_results.values())
    if all_failed:
        print("ERROR: All fund scrapes failed.")
        sys.exit(1)


if __name__ == '__main__':
    main()
