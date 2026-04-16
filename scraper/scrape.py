"""
HCF Retention Tool — Haiku-Powered Daily Scraper
Replaces Playwright with Claude Haiku + httpx for smarter, cheaper extraction.

Architecture:
  - httpx:      lightweight HTTP fetching (no browser needed)
  - pdfplumber: extract text from PHIS PDFs cheaply
  - Claude Haiku: parse HTML/text into structured JSON when needed

Data sources:
  - privatehealth.gov.au PHIS PDFs → premiums (all funds, all cover types)
  - HCF product summary PDFs       → extras benefit limits
  - Competitor fund PDFs           → competitor extras limits

Estimated cost: ~$0.03–0.06/run → under $2/month
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import pdfplumber
from anthropic import Anthropic

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(SCRIPT_DIR, '..', 'data')
PRODUCTS_FILE = os.path.join(DATA_DIR, 'products.json')
META_FILE     = os.path.join(DATA_DIR, 'meta.json')

AEST = timezone(timedelta(hours=10))
TODAY = datetime.now(AEST).strftime('%Y-%m-%d')

# ── Clients ──────────────────────────────────────────────────────────────────
anthropic_client = Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

HTTP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; NewAIN-Bot/2.0; +https://tools.newain.com.au)',
    'Accept': 'text/html,application/xhtml+xml,application/pdf,*/*',
}

# ── JSON helpers ─────────────────────────────────────────────────────────────
def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {os.path.basename(path)}")

# ── HTTP fetch ───────────────────────────────────────────────────────────────
def fetch(url: str, timeout: int = 20) -> Optional[bytes]:
    try:
        r = httpx.get(url, headers=HTTP_HEADERS, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"    HTTP error for {url}: {e}")
        return None

# ── PDF text extraction ───────────────────────────────────────────────────────
def pdf_to_text(data: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return '\n'.join(
                page.extract_text() or ''
                for page in pdf.pages
            ).strip()
    except Exception as e:
        print(f"    PDF parse error: {e}")
        return ''

# ── Haiku extraction ──────────────────────────────────────────────────────────
def haiku_extract(system: str, content: str, max_tokens: int = 512) -> dict:
    """
    Call Claude Haiku with a system prompt and content.
    Expects Haiku to return a JSON object.
    Returns {} on failure.
    """
    try:
        msg = anthropic_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=max_tokens,
            system=system,
            messages=[{'role': 'user', 'content': content[:40000]}],
        )
        text = msg.content[0].text.strip()
        # Strip markdown fences if present
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        print(f"    Haiku extract error: {e}")
        return {}

# ════════════════════════════════════════════════════════════════════════════
# PHIS PRODUCT REGISTRY
# Known product codes on privatehealth.gov.au for each fund.
# Format: (product_code, state_cover_code, cover_type)
# cover_type: single | couple | family | single_parent_family
# Source: privatehealth.gov.au (verified April 2026)
#
# To add new codes: search privatehealth.gov.au for the product and note the
# URL pattern: /dynamic/Download/{FUND}/{PRODUCT_CODE}/{STATE_COVER_CODE}
# ════════════════════════════════════════════════════════════════════════════
PHIS_REGISTRY = {

    # ── HCF Hospital ──────────────────────────────────────────────────────
    'hcf': {
        'hospital': [
            # Hospital Optimal Gold $750 excess
            ('H36R', 'SPVD10', 'single',               'Hospital Optimal Gold', 'Gold', 750),
            # Hospital Silver Plus $500 excess
            ('H29I', 'VLEI10', 'single',                'Hospital Silver Plus', 'Silver Plus', 500),
            # Hospital Standard Silver Plus $750 excess
            ('H31K', 'NMDK10', 'single',                'Hospital Standard Silver Plus', 'Silver Plus', 750),
            ('H31G', 'NMAF20', 'couple',                'Hospital Standard Silver Plus', 'Silver Plus', 750),
        ],
        'extras': [
            # Mid Extras
            ('I30', 'NKTX10', 'single',  'Mid Extras'),
            # Starter Extras (with Optical)
            ('I21', 'NGEP10', 'single',  'Starter Extras (with Optical)'),
            # Choose My Extras
            ('I31', 'NPWH10', 'single',  'Choose My Extras'),
        ],
    },

    # ── nib ───────────────────────────────────────────────────────────────
    'nib': {
        'hospital': [],
        'extras':   [],
    },

    # ── Bupa ──────────────────────────────────────────────────────────────
    'bupa': {
        'hospital': [],
        'extras':   [],
    },

    # ── Medibank ──────────────────────────────────────────────────────────
    'medibank': {
        'hospital': [],
        'extras':   [],
    },

    # ── ahm ───────────────────────────────────────────────────────────────
    'ahm': {
        'hospital': [],
        'extras':   [],
    },

    # ── Australian Unity ──────────────────────────────────────────────────
    'australian_unity': {
        'hospital': [],
        'extras':   [],
    },
}

# ── HCF extras benefit limit PDFs ────────────────────────────────────────────
HCF_EXTRAS_PDFS = {
    'hcf-top-extras':     'https://www.hcf.com.au/content/dam/hcf/pdf/brochures/HCF-Top-Extras.pdf',
    'hcf-mid-extras':     'https://www.hcf.com.au/content/dam/hcf/pdf/brochures/HCF-Mid-Extras.pdf',
    'hcf-general-extras': 'https://www.hcf.com.au/content/dam/hcf/pdf/brochures/5-General-Extras.pdf',
}

HAIKU_EXTRAS_SYSTEM = """
You extract health insurance extras benefit limits from a product summary document.
Return ONLY a JSON object with these exact keys (all values are integers in AUD, 0 if not found):
{
  "general_dental": <annual limit per person>,
  "major_dental": <annual limit per person — use combined dental limit if separate major dental not stated>,
  "optical": <annual limit per person>,
  "therapies": <annual limit per person for physio/allied health>,
  "chiro_osteo": <annual limit per person for chiropractic/osteopathy>,
  "orthodontics": <annual limit per person, lifetime limit if only lifetime stated>
}
If a limit increases by year (Year 1/2/3+), return the Year 1 value.
"""

HAIKU_PHIS_SYSTEM = """
You extract health insurance premium information from a Private Health Information Statement (PHIS).
Return ONLY a JSON object:
{
  "product_name": "<exact product name>",
  "monthly_premium": <number — the dollar amount shown, before rebate>,
  "cover_type": "<single|couple|family|single_parent_family>",
  "state": "<NSW|VIC|QLD|SA|WA|TAS|NT|ACT>",
  "excess": <number or 0 if not applicable>,
  "is_closed": <true if closed to new members, false otherwise>
}
Map cover descriptions:
- "one person" → "single"
- "2 adults (and no-one else)" → "couple"
- "two adults & dependants" or "3 or more people, only 2 adults" → "family"
- "one adult & dependants" or "2 or more people, only one adult" → "single_parent_family"
"""

HAIKU_SEARCH_SYSTEM = """
You are extracting health insurance product codes from a government website page.
The page lists health insurance products with links in the format:
/dynamic/Download/{FUND}/{PRODUCT_CODE}/{STATE_COVER_CODE}
or /dynamic/Premium/PHIS/{FUND}/{PRODUCT_CODE}/{STATE_COVER_CODE}

Return ONLY a JSON array of objects:
[
  {
    "product_code": "<e.g. H36R>",
    "state_cover_code": "<e.g. SPVD10>",
    "product_name": "<name if visible>",
    "cover_type": "<single|couple|family|single_parent_family>",
    "fund": "<fund code e.g. HCF>"
  }
]
Only include entries where fund matches what was requested.
Return [] if no products found.
"""

# ════════════════════════════════════════════════════════════════════════════
# PREMIUM FETCHERS
# ════════════════════════════════════════════════════════════════════════════

def parse_phis_text(text: str) -> Optional[dict]:
    """
    Parse a PHIS PDF text without Haiku — these PDFs have a consistent format.
    Extracts: monthly_premium, cover_type, state, product_name, is_closed.
    Returns None if parsing fails (fallback to Haiku).
    """
    # Premium amount
    price_match = re.search(r'\$(\d{1,4}(?:\.\d{2})?)\s*\n.*?before any rebate', text, re.DOTALL)
    if not price_match:
        price_match = re.search(r'\$([\d,]+(?:\.\d{2})?)\s*\n', text)
    if not price_match:
        return None
    try:
        premium = float(price_match.group(1).replace(',', ''))
    except ValueError:
        return None

    # Cover type
    cover_type = 'single'
    t = text[:600]
    if 'two adults & dependants' in t or '3 or more people, only 2' in t:
        cover_type = 'family'
    elif '2 adults (and no-one else)' in t or 'Covers 2 adults' in t:
        cover_type = 'couple'
    elif 'one adult & dependants' in t or '2 or more people, only one' in t:
        cover_type = 'single_parent_family'

    # State
    state = 'NSW'
    for s in ['NSW', 'VIC', 'QLD', 'SA', 'WA', 'TAS', 'NT', 'ACT']:
        if f'Available in {s}' in text or f'Available in {s} &' in text:
            state = s
            break

    # Product name
    name_match = re.search(r'(?:HCF|NIB|BUPA|MEDIBANK|AHM|Australian Unity)\s+([A-Z][^\n]{3,60})\n', text)
    product_name = name_match.group(0).strip() if name_match else ''

    # Excess
    excess_match = re.search(r'excess of \$(\d+)', text)
    excess = int(excess_match.group(1)) if excess_match else 0

    is_closed = 'Closed to new members' in text

    return {
        'monthly_premium': premium,
        'cover_type':      cover_type,
        'state':           state,
        'product_name':    product_name,
        'excess':          excess,
        'is_closed':       is_closed,
    }


def fetch_phis_premium(fund: str, product_code: str, state_cover_code: str) -> Optional[dict]:
    """
    Fetch a PHIS PDF from privatehealth.gov.au, extract premium data.
    Uses regex parsing first (free), falls back to Haiku if needed.
    Returns dict with premium info, or {} on failure.
    """
    url = f'https://www.privatehealth.gov.au/dynamic/Download/{fund.upper()}/{product_code}/{state_cover_code}'
    data = fetch(url)
    if not data:
        return {}
    text = pdf_to_text(data)
    if not text:
        return {}

    # Try cheap regex parse first
    result = parse_phis_text(text)
    if result:
        result['source_url'] = url
        return result

    # Fallback to Haiku
    print(f"    Regex parse failed, using Haiku ...")
    result = haiku_extract(HAIKU_PHIS_SYSTEM, text, max_tokens=256)
    if result:
        result['source_url'] = url
    return result


def discover_phis_codes(fund_code: str, cover_type: str, cover_category: str = 'H') -> list[dict]:
    """
    Try to find product codes for a fund on privatehealth.gov.au by
    fetching the comparison search page and asking Haiku to extract codes.
    cover_category: 'H' = hospital, 'GH' = extras
    Returns list of {product_code, state_cover_code, product_name, cover_type}
    """
    type_map = {
        'single':               'S',
        'couple':               'C',
        'family':               'F',
        'single_parent_family': 'SPF',
    }
    type_param = type_map.get(cover_type, 'S')

    urls_to_try = [
        f'https://www.privatehealth.gov.au/dynamic/search/getquote?State=NSW&Cover={cover_category}&TypeOfCover={type_param}&Fund={fund_code.upper()}&Tier=0&PageSize=50',
        f'https://www.privatehealth.gov.au/healthinsurance/search/{("hospital" if cover_category == "H" else "general")}/?State=NSW&TypeOfCover={type_param}&Fund={fund_code.upper()}',
    ]

    for url in urls_to_try:
        data = fetch(url)
        if not data:
            continue
        try:
            text = data.decode('utf-8', errors='ignore')
        except Exception:
            continue
        if len(text) < 500 or 'privatehealth' not in text.lower():
            continue
        results = haiku_extract(HAIKU_SEARCH_SYSTEM, text, max_tokens=1024)
        if isinstance(results, list) and results:
            print(f"    Discovered {len(results)} product codes via search")
            return results

    return []


# ════════════════════════════════════════════════════════════════════════════
# EXTRAS BENEFIT LIMIT FETCHERS
# ════════════════════════════════════════════════════════════════════════════

def fetch_hcf_extras_limits(product_id: str, pdf_url: str) -> dict:
    """Fetch an HCF extras product summary PDF and extract benefit limits."""
    print(f"    Fetching limits: {pdf_url}")
    data = fetch(pdf_url)
    if not data:
        return {}
    text = pdf_to_text(data)
    if not text:
        return {}
    return haiku_extract(HAIKU_EXTRAS_SYSTEM, text, max_tokens=256)


# ════════════════════════════════════════════════════════════════════════════
# MERGE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def find_hospital_product(fund_data: dict, product_name: str, tier: str, excess: int) -> Optional[dict]:
    """Find a hospital product in fund data by name/tier/excess."""
    for p in fund_data.get('hospital', []):
        if (tier.lower() in p.get('tier', '').lower() or
                product_name.lower() in p.get('name', '').lower()):
            if excess in p.get('excess_options', []):
                return p
    return None


def find_extras_product(fund_data: dict, product_name: str) -> Optional[dict]:
    """Find an extras product in fund data by name."""
    name_lower = product_name.lower()
    for p in fund_data.get('extras', []):
        if name_lower in p.get('name', '').lower() or p.get('name', '').lower() in name_lower:
            return p
    return None


def upsert_hospital_premium(fund_data: dict, phis_result: dict, excess: int, tier: str, product_name: str) -> bool:
    """Update a hospital product's premium for the given cover type. Returns True if updated."""
    cover_type = phis_result.get('cover_type')
    premium    = phis_result.get('monthly_premium')
    if not cover_type or not premium:
        return False

    product = find_hospital_product(fund_data, product_name, tier, excess)
    if not product:
        return False

    excess_key = str(excess)
    if excess_key not in product.get('premiums', {}):
        product.setdefault('premiums', {})[excess_key] = {
            'single': 0, 'couple': 0, 'family': 0, 'single_parent_family': 0
        }
    product['premiums'][excess_key][cover_type] = premium
    product['last_verified'] = TODAY
    product['source'] = 'privatehealth.gov.au'
    return True


def upsert_extras_premium(fund_data: dict, phis_result: dict, product_name: str) -> bool:
    """Update an extras product's premium for the given cover type. Returns True if updated."""
    cover_type = phis_result.get('cover_type')
    premium    = phis_result.get('monthly_premium')
    if not cover_type or not premium:
        return False

    product = find_extras_product(fund_data, product_name)
    if not product:
        return False

    product.setdefault('premiums', {})[cover_type] = premium
    product['last_verified'] = TODAY
    product['source'] = 'privatehealth.gov.au'
    return True


# ════════════════════════════════════════════════════════════════════════════
# FUND SCRAPERS
# ════════════════════════════════════════════════════════════════════════════

def scrape_fund_premiums(fund_key: str, fund_data: dict) -> dict:
    """
    Scrape premiums for a fund using:
    1. Known PHIS product codes from registry
    2. Discovered codes (if discovery finds new ones)
    Returns updated fund_data.
    """
    registry = PHIS_REGISTRY.get(fund_key, {'hospital': [], 'extras': []})

    # ── Hospital premiums ──
    updated_h = 0
    for entry in registry['hospital']:
        prod_code, state_code, cover_type, prod_name, tier, excess = entry
        print(f"    PHIS hospital: {prod_name} ({cover_type}) ...")
        result = fetch_phis_premium(fund_key, prod_code, state_code)
        if result:
            if upsert_hospital_premium(fund_data, result, excess, tier, prod_name):
                updated_h += 1
                print(f"      → ${result.get('monthly_premium')}/month {cover_type}")
            else:
                print(f"      → product not found in data (skipped)")
        else:
            print(f"      → fetch failed")

    # Attempt discovery for cover types not in registry
    known_hospital_types = {e[2] for e in registry['hospital']}
    all_types = {'single', 'couple', 'family', 'single_parent_family'}
    missing_types = all_types - known_hospital_types

    if missing_types and registry['hospital']:
        print(f"    Attempting discovery for missing cover types: {missing_types}")
        for cover_type in missing_types:
            discovered = discover_phis_codes(fund_key, cover_type, 'H')
            for d in discovered:
                result = fetch_phis_premium(fund_key, d['product_code'], d['state_cover_code'])
                if result:
                    excess = result.get('excess', 0)
                    tier   = result.get('product_name', '')
                    if upsert_hospital_premium(fund_data, result, excess, tier, d.get('product_name', '')):
                        updated_h += 1
                        print(f"      → Discovered: {d.get('product_name')} {cover_type} ${result.get('monthly_premium')}")

    # ── Extras premiums ──
    updated_e = 0
    for entry in registry['extras']:
        prod_code, state_code, cover_type, prod_name = entry
        print(f"    PHIS extras: {prod_name} ({cover_type}) ...")
        result = fetch_phis_premium(fund_key, prod_code, state_code)
        if result:
            if upsert_extras_premium(fund_data, result, prod_name):
                updated_e += 1
                print(f"      → ${result.get('monthly_premium')}/month {cover_type}")
            else:
                print(f"      → product not found in data (skipped)")

    # Attempt discovery for extras cover types
    known_extras_types = {e[2] for e in registry['extras']}
    missing_extras_types = all_types - known_extras_types
    if missing_extras_types and registry['extras']:
        print(f"    Attempting extras discovery for: {missing_extras_types}")
        for cover_type in missing_extras_types:
            discovered = discover_phis_codes(fund_key, cover_type, 'GH')
            for d in discovered:
                result = fetch_phis_premium(fund_key, d['product_code'], d['state_cover_code'])
                if result:
                    if upsert_extras_premium(fund_data, result, d.get('product_name', '')):
                        updated_e += 1
                        print(f"      → Discovered: {d.get('product_name')} {cover_type} ${result.get('monthly_premium')}")

    print(f"    Updated: {updated_h} hospital premiums, {updated_e} extras premiums")
    return fund_data


def scrape_hcf_extras_limits(fund_data: dict) -> dict:
    """
    Fetch HCF extras product summary PDFs and update benefit limits.
    Only runs if limits appear stale (zeros present).
    """
    for product_id, pdf_url in HCF_EXTRAS_PDFS.items():
        product = next((p for p in fund_data.get('extras', []) if p['id'] == product_id), None)
        if not product:
            continue

        # Only refetch if KEY limits look stale (orthodontics=0 is legitimate)
        limits = product.get('limits', {})
        key_fields = ['general_dental', 'optical', 'therapies']
        has_zeros = any(limits.get(f, 0) == 0 for f in key_fields)
        if not has_zeros:
            print(f"    Limits OK (skipping fetch): {product['name']}")
            continue

        print(f"    Fetching limits: {product['name']}")
        new_limits = fetch_hcf_extras_limits(product_id, pdf_url)
        if new_limits:
            product['limits'] = new_limits
            product['last_verified'] = TODAY
            product['source'] = pdf_url
            print(f"      → {new_limits}")
        else:
            print(f"      → failed to extract limits")

    return fund_data


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    now_str = datetime.now(AEST).isoformat()
    print(f"\n=== HCF Retention Tool — Daily Scrape [{now_str}] ===\n")

    products_data = load_json(PRODUCTS_FILE)
    meta_data     = load_json(META_FILE)

    source_results = {}

    for fund_key in products_data['funds']:
        print(f"\n[{fund_key.upper()}]")
        try:
            fund_data = products_data['funds'][fund_key]

            # Update premiums from privatehealth.gov.au
            fund_data = scrape_fund_premiums(fund_key, fund_data)

            # For HCF: also update extras benefit limits from PDFs
            if fund_key == 'hcf':
                fund_data = scrape_hcf_extras_limits(fund_data)

            products_data['funds'][fund_key] = fund_data
            source_results[fund_key] = 'ok'

        except Exception as e:
            print(f"  ERROR scraping {fund_key}: {e}")
            source_results[fund_key] = 'error'

    # ── Update meta ──
    meta_data['last_updated'] = now_str
    for source in meta_data['sources']:
        fund_key = source['fund'].lower().replace(' ', '_')
        source['status'] = source_results.get(fund_key, 'skipped')

    save_json(PRODUCTS_FILE, products_data)
    save_json(META_FILE, meta_data)

    print(f"\n=== Scrape complete ===\n")

    all_failed = all(v == 'error' for v in source_results.values())
    if all_failed:
        print("ERROR: All fund scrapes failed.")
        sys.exit(1)


if __name__ == '__main__':
    main()
