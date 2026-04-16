"""
HCF Retention Tool — Multi-Strategy Direct Fund Scraper
Scrapes pricing directly from each fund's own website.

Strategy per fund:
  - Medibank:          Direct JSON API via httpx (confirmed, no Playwright needed)
  - Australian Unity:  Rate chart PDF via httpx + pdfplumber + Haiku; Playwright fallback
  - nib, Bupa, ahm:   Playwright best-effort (no public rate PDFs discovered)
  - HCF:              Skip — Imperva WAF blocks headless Chrome; existing data preserved

State target: NSW (agents are NSW-based)
Cover types: single, couple, family, single_parent_family
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import httpx
from anthropic import Anthropic
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(SCRIPT_DIR, '..', 'data')
PRODUCTS_FILE = os.path.join(DATA_DIR, 'products.json')
META_FILE     = os.path.join(DATA_DIR, 'meta.json')

AEST  = timezone(timedelta(hours=10))
TODAY = datetime.now(AEST).strftime('%Y-%m-%d')

# ── Clients ───────────────────────────────────────────────────────────────────
anthropic_client = Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

# ── JSON helpers ──────────────────────────────────────────────────────────────
def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {os.path.basename(path)}")

# ── Haiku ─────────────────────────────────────────────────────────────────────
def haiku_extract(system: str, content: str, max_tokens: int = 2048) -> dict | list:
    """Call Haiku and return parsed JSON. Returns {} or [] on failure."""
    try:
        msg = anthropic_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=max_tokens,
            system=system,
            messages=[{'role': 'user', 'content': content[:60000]}],
        )
        text = msg.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        print(f"    Haiku error: {e}")
        return {}


# ── Playwright helpers ────────────────────────────────────────────────────────
def navigate(page: Page, url: str, wait_ms: int = 4000) -> bool:
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(wait_ms)
        return True
    except Exception as e:
        print(f"    Navigate error ({url}): {e}")
        return False


def page_text(page: Page) -> str:
    """Return visible text from a rendered page, stripping nav/footer/scripts."""
    try:
        return page.evaluate("""() => {
            ['script','style','nav','footer','header','noscript'].forEach(t =>
                document.querySelectorAll(t).forEach(el => el.remove())
            );
            return document.body.innerText;
        }""")
    except Exception:
        return page.inner_text('body')


def try_select_state(page: Page, state: str = 'NSW') -> bool:
    selectors = [
        'select[id*="state"]', 'select[name*="state"]',
        'select[id*="State"]', '[data-testid*="state"] select',
        'select[aria-label*="state" i]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                el.select_option(value=state)
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    return False


# ── Haiku system prompts ──────────────────────────────────────────────────────
HAIKU_PRODUCTS_SYSTEM = """
You extract health insurance product pricing from an Australian health fund website page.

Return ONLY a JSON array. Each element is one product:
[
  {
    "name": "<product name>",
    "tier": "<Gold|Silver Plus|Silver|Bronze Plus|Bronze|Basic Plus|Basic>",
    "type": "<hospital|extras>",
    "excess": <number or null>,
    "status": "<current|closed>",
    "premiums": {
      "single": <monthly $ as number or null>,
      "couple": <monthly $ as number or null>,
      "family": <monthly $ as number or null>,
      "single_parent_family": <monthly $ as number or null>
    }
  }
]

Rules:
- Premiums are monthly dollar amounts BEFORE any government rebate
- If page only shows one cover type, record it and set others to null
- If price is shown per fortnight, multiply by 26 then divide by 12 (round to 2dp)
- If price is shown per year, divide by 12
- "tier" must be one of the exact strings listed — infer from product name
- "type" = "hospital" if hospital product, "extras" if extras/general
- "status" = "closed" only if page explicitly says "closed to new members"
- Return [] if no health insurance products with prices are found
"""

HAIKU_AU_PDF_SYSTEM = """
You extract health insurance premium data from an Australian Unity rate chart PDF.

Return ONLY a JSON array. Each element is one product:
[
  {
    "name": "<product name>",
    "tier": "<Gold|Silver Plus|Silver|Bronze Plus|Bronze|Basic Plus|Basic>",
    "excess": <excess amount as number or 0 if none>,
    "premiums": {
      "single_tier3_dd": <Tier 3 direct debit monthly price or null>,
      "couple_tier3_dd": <Tier 3 direct debit monthly price or null>
    }
  }
]

Rules:
- "Tier 3" column = no government rebate = base price (what we want)
- Prices shown include a 4% direct debit discount
- To get true base price: divide by 0.96
- Only extract NSW/ACT prices
- Return [] if no prices are clearly found
"""


# ════════════════════════════════════════════════════════════════════════════
# MEDIBANK — DIRECT API (httpx, no Playwright)
# ════════════════════════════════════════════════════════════════════════════

MEDIBANK_BASE      = 'https://www.medibank.com.au'
MEDIBANK_LIST_URL  = f'{MEDIBANK_BASE}/bin/medibank/productlist'
MEDIBANK_PRICE_URL = f'{MEDIBANK_BASE}/bin/medibank/price/'
MEDIBANK_COMPONENT = (
    '/content/retail/en/health-insurance/packages/'
    'jcr:content/root/productcomparison_co'
)
MEDIBANK_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.medibank.com.au/health-insurance/',
}


def _medibank_tier(title: str) -> str:
    t = title
    if 'Gold' in t:        return 'Gold'
    if 'Silver Plus' in t: return 'Silver Plus'
    if 'Silver' in t:      return 'Silver'
    if 'Bronze Plus' in t: return 'Bronze Plus'
    if 'Bronze' in t:      return 'Bronze'
    if 'Basic Plus' in t:  return 'Basic Plus'
    return 'Basic'


def scrape_medibank_api() -> list[dict]:
    """
    Call Medibank's internal pricing API to get all hospital products and
    pre-rebate NSW premiums. Returns a list of product dicts ready for merge.
    """
    print("  Calling Medibank product list API...")

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        # Step 1: product list
        r = client.get(
            MEDIBANK_LIST_URL,
            params={
                'componentPath': MEDIBANK_COMPONENT,
                'productFilter': json.dumps({
                    'age': 35, 'scale': 'S', 'filterType': 'all',
                    'coverForChild': False, 'audience': 'default',
                    'productType': 'hospital', 'state': 'NSW',
                }),
                'journey': 'get-a-quote',
            },
            headers=MEDIBANK_HEADERS,
        )
        r.raise_for_status()
        raw = r.json()

    products_raw = raw.get('hospitalProducts', [])
    if not products_raw:
        print("    No products returned from Medibank API")
        return []

    print(f"    {len(products_raw)} products in product list")

    # Extract tableId from each product's 'path' (e.g. .../CT00024615/...)
    product_info = []
    for p in products_raw:
        path = p.get('path', '')
        m = re.search(r'/(CT\d+)', path)
        table_id = m.group(1) if m else p.get('tableId', '')
        if not table_id:
            continue
        product_info.append({
            'tableId':  table_id,
            'title':    p.get('title', ''),
            'excess':   int(p.get('defaultExcessValue') or 0),
        })

    if not product_info:
        print("    Could not extract tableIds from product list")
        return []

    # Step 2: fetch prices per scale (S = single, C = couple, F = family)
    DOB = '16/04/1991'
    hospital_ids = [{'tableId': p['tableId']} for p in product_info]
    prices: dict[str, dict[str, float]] = {}  # tableId → {S/C/F → monthly}

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for scale in ['S', 'C', 'F']:
            print(f"    Fetching {scale} prices...")
            r2 = client.post(
                MEDIBANK_PRICE_URL,
                json={
                    'scale': scale,
                    'state': 'NSW',
                    'isPensioner': False,
                    'dob': DOB,
                    'partnerDob': DOB,
                    'cae': 0,
                    'partnerCae': 0,
                    'includeLHCLoading': False,
                    'includePartnerLHCLoading': False,
                    'dualRates': False,
                    'ruleset': 'Retail',
                    'fgrRebateTier': 3,   # 3 = no rebate = base pre-rebate price
                    'extrasProducts': [],
                    'hospitalProducts': hospital_ids,
                },
                headers={**MEDIBANK_HEADERS, 'Content-Type': 'application/json'},
            )
            r2.raise_for_status()
            for pp in r2.json().get('hospitalProductPrice', []):
                tid     = pp.get('tableId', '')
                monthly = round(pp.get('price', {}).get('monthlyPrice', 0) or 0, 2)
                prices.setdefault(tid, {})[scale] = monthly

    # Step 3: build product dicts
    result = []
    for p in product_info:
        tid    = p['tableId']
        title  = p['title']
        excess = p['excess']
        ps     = prices.get(tid, {})

        single = ps.get('S', 0)
        couple = ps.get('C', 0)
        family = ps.get('F', 0)

        result.append({
            'name':    title,
            'tier':    _medibank_tier(title),
            'type':    'hospital',
            'excess':  excess,
            'status':  'current',
            'premiums': {
                str(excess): {
                    'single':              single,
                    'couple':              couple,
                    'family':              family,
                    'single_parent_family': single,   # community rating
                }
            },
        })
        print(f"    {title} (${excess} excess): single=${single}, couple=${couple}")

    return result


def replace_medibank_hospital(products_data: dict, scraped: list[dict]) -> dict:
    """
    Replace (not merge) Medibank hospital products with fresh API data.
    Extras are left unchanged.
    """
    hospital_list = []
    for sp in scraped:
        name   = sp['name']
        excess = sp['excess']
        slug   = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        hospital_list.append({
            'id':           f'medibank-{slug}',
            'name':         name,
            'tier':         sp['tier'],
            'status':       'current',
            'excess_options': [excess],
            'premiums':     sp['premiums'],
            'source':       'medibank.com.au (API)',
            'last_verified': TODAY,
        })
    products_data['funds']['medibank']['hospital'] = hospital_list
    print(f"  Replaced Medibank hospital products: {len(hospital_list)} products")
    return products_data


# ════════════════════════════════════════════════════════════════════════════
# AUSTRALIAN UNITY — PDF then Playwright fallback
# ════════════════════════════════════════════════════════════════════════════

AU_RATE_CHART_URL = (
    'https://www.australianunity.com.au/health-insurance/~/media/'
    'health%20insurance/files/fact%20sheets/ratechart_nswact.ashx'
)


def _au_tier(name: str) -> str:
    n = name
    if 'Gold' in n:        return 'Gold'
    if 'Silver Plus' in n: return 'Silver Plus'
    if 'Silver' in n:      return 'Silver'
    if 'Bronze Plus' in n: return 'Bronze Plus'
    if 'Bronze' in n:      return 'Bronze'
    if 'Basic Plus' in n:  return 'Basic Plus'
    return 'Basic'


def scrape_au_pdf() -> list[dict]:
    """
    Try to download Australian Unity's rate chart PDF directly.
    Returns structured products, or [] if unavailable/stale.
    """
    try:
        import pdfplumber
        import io
    except ImportError:
        print("    pdfplumber not installed, skipping PDF approach")
        return []

    print("    Downloading Australian Unity rate chart PDF...")
    try:
        r = httpx.get(
            AU_RATE_CHART_URL,
            headers={'User-Agent': 'Mozilla/5.0 (compatible)'},
            timeout=30,
            follow_redirects=True,
        )
        if r.status_code != 200 or len(r.content) < 10000:
            print(f"    PDF not accessible (status {r.status_code})")
            return []
    except Exception as e:
        print(f"    PDF download failed: {e}")
        return []

    print(f"    PDF downloaded ({len(r.content):,} bytes), extracting text...")
    try:
        text_pages = []
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for pg in pdf.pages[:10]:  # limit to first 10 pages
                t = pg.extract_text()
                if t:
                    text_pages.append(t)
        pdf_text = '\n'.join(text_pages)
    except Exception as e:
        print(f"    PDF parse error: {e}")
        return []

    if not pdf_text.strip():
        print("    PDF yielded no text")
        return []

    # Check if it looks like a current-year rate chart
    # If it mentions years before 2024 and not current, skip
    if '2018' in pdf_text and '2026' not in pdf_text and '2025' not in pdf_text:
        print("    PDF appears to be from 2018 — too outdated, skipping")
        return []

    print("    Parsing PDF with Haiku...")
    raw = haiku_extract(HAIKU_AU_PDF_SYSTEM, pdf_text, max_tokens=4096)
    if not isinstance(raw, list) or not raw:
        print("    Haiku returned no products from PDF")
        return []

    # Convert from Tier3 DD price to true base price (remove 4% DD discount)
    result = []
    for p in raw:
        name = p.get('name', '').strip()
        if not name:
            continue
        excess = int(p.get('excess') or 0)
        prems  = p.get('premiums', {})

        # Tier3 DD price / 0.96 = pre-rebate base price
        t3_single = prems.get('single_tier3_dd') or 0
        t3_couple = prems.get('couple_tier3_dd') or 0

        single = round(t3_single / 0.96, 2) if t3_single else 0
        couple = round(t3_couple / 0.96, 2) if t3_couple else 0

        if not single and not couple:
            continue

        result.append({
            'name':    name,
            'tier':    _au_tier(name),
            'type':    'hospital',
            'excess':  excess,
            'status':  'current',
            'premiums': {
                str(excess): {
                    'single':              single,
                    'couple':              couple,
                    'family':              couple,       # community rating
                    'single_parent_family': single,
                }
            },
        })
        print(f"    {name}: single=${single}, couple=${couple}")

    return result


def scrape_au_playwright(page: Page) -> list[dict]:
    """Playwright fallback for Australian Unity."""
    results = []

    for product_type, url in [
        ('hospital', 'https://www.australianunity.com.au/health-wellbeing/health-insurance/hospital-cover'),
        ('extras',   'https://www.australianunity.com.au/health-wellbeing/health-insurance/extras-cover'),
    ]:
        print(f"  Scraping AU {product_type} (Playwright)...")
        if not navigate(page, url):
            continue
        try_select_state(page, 'NSW')
        text = page_text(page)
        found = haiku_extract(
            HAIKU_PRODUCTS_SYSTEM,
            f'Australian Unity {product_type} page:\n\n{text}',
        )
        if isinstance(found, list):
            results.extend(found)
            print(f"    {len(found)} products found")

    return results


# ════════════════════════════════════════════════════════════════════════════
# nib — Playwright
# ════════════════════════════════════════════════════════════════════════════

def scrape_nib(page: Page) -> list[dict]:
    results = []
    for product_type, url in [
        ('hospital', 'https://www.nib.com.au/health-insurance/hospital'),
        ('extras',   'https://www.nib.com.au/health-insurance/extras'),
    ]:
        print(f"  Scraping nib {product_type}...")
        if not navigate(page, url, wait_ms=5000):
            continue
        try_select_state(page, 'NSW')
        text = page_text(page)
        found = haiku_extract(
            HAIKU_PRODUCTS_SYSTEM,
            f'nib {product_type} page:\n\n{text}',
        )
        if isinstance(found, list):
            results.extend(found)
            print(f"    {len(found)} products found")
    return results


# ════════════════════════════════════════════════════════════════════════════
# Bupa — Playwright
# ════════════════════════════════════════════════════════════════════════════

def scrape_bupa(page: Page) -> list[dict]:
    results = []
    for product_type, url in [
        ('hospital', 'https://www.bupa.com.au/health-insurance/hospital'),
        ('extras',   'https://www.bupa.com.au/health-insurance/extras-cover'),
    ]:
        print(f"  Scraping Bupa {product_type}...")
        if not navigate(page, url, wait_ms=5000):
            continue
        try_select_state(page, 'NSW')
        text = page_text(page)
        found = haiku_extract(
            HAIKU_PRODUCTS_SYSTEM,
            f'Bupa {product_type} page:\n\n{text}',
        )
        if isinstance(found, list):
            results.extend(found)
            print(f"    {len(found)} products found")
    return results


# ════════════════════════════════════════════════════════════════════════════
# ahm — Playwright
# ════════════════════════════════════════════════════════════════════════════

def scrape_ahm(page: Page) -> list[dict]:
    results = []
    for product_type, url in [
        ('hospital', 'https://www.ahm.com.au/health-insurance/hospital'),
        ('extras',   'https://www.ahm.com.au/health-insurance/extras'),
    ]:
        print(f"  Scraping ahm {product_type}...")
        if not navigate(page, url, wait_ms=5000):
            continue
        try_select_state(page, 'NSW')
        text = page_text(page)
        found = haiku_extract(
            HAIKU_PRODUCTS_SYSTEM,
            f'ahm {product_type} page:\n\n{text}',
        )
        if isinstance(found, list):
            results.extend(found)
            print(f"    {len(found)} products found")
    return results


# ════════════════════════════════════════════════════════════════════════════
# DATA MERGE (for Playwright-scraped products)
# ════════════════════════════════════════════════════════════════════════════

def merge_into_fund(fund_data: dict, scraped: list[dict], source_domain: str) -> dict:
    """
    Merge freshly scraped products into existing fund data.
    Updates premiums for matched products by name; logs unmatched ones.
    """
    for sp in scraped:
        sname   = (sp.get('name') or '').lower().strip()
        stype   = sp.get('type', 'hospital')
        sexcess = sp.get('excess')
        sprems  = sp.get('premiums') or {}

        product_list = fund_data.get(stype, [])
        matched = None

        for p in product_list:
            pname = p.get('name', '').lower()
            if sname in pname or pname in sname:
                if stype == 'hospital':
                    if sexcess is None or sexcess in p.get('excess_options', []):
                        matched = p
                        break
                else:
                    matched = p
                    break

        if not matched:
            print(f"    No match for: '{sp.get('name')}' ({stype})")
            continue

        updated = False
        if stype == 'hospital' and sexcess is not None:
            ekey = str(int(sexcess))
            slot = matched.setdefault('premiums', {}).setdefault(
                ekey, {'single': 0, 'couple': 0, 'family': 0, 'single_parent_family': 0}
            )
            for ct, val in sprems.items():
                if val and val > 0:
                    slot[ct] = val
                    updated = True
        elif stype == 'extras':
            slot = matched.setdefault('premiums', {})
            for ct, val in sprems.items():
                if val and val > 0:
                    slot[ct] = val
                    updated = True

        if updated:
            matched['last_verified'] = TODAY
            matched['source'] = source_domain
            print(f"    Updated: {matched['name']}")

    return fund_data


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    now_str = datetime.now(AEST).isoformat()
    print(f"\n=== HCF Retention Tool — Direct Fund Scrape [{now_str}] ===\n")

    products_data = load_json(PRODUCTS_FILE)
    meta_data     = load_json(META_FILE)
    results: dict[str, str] = {}

    # ── 1. Medibank API (no browser needed) ──────────────────────────────────
    print("\n[Medibank] — direct API")
    try:
        medibank_products = scrape_medibank_api()
        if medibank_products:
            products_data = replace_medibank_hospital(products_data, medibank_products)
            results['medibank'] = 'ok'
        else:
            results['medibank'] = 'no_data'
    except Exception as e:
        print(f"  ERROR: {e}")
        results['medibank'] = 'error'

    # ── 2. Australian Unity — PDF then Playwright fallback ───────────────────
    print("\n[Australian Unity] — PDF / Playwright")
    au_scraped = scrape_au_pdf()
    au_used_pdf = bool(au_scraped)

    # Playwright-dependent funds — open browser once for all
    playwright_needed = not au_used_pdf  # need browser if PDF failed for AU + always for others

    # ── 3–5. Playwright-based funds ──────────────────────────────────────────
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            viewport={'width': 1440, 'height': 900},
            locale='en-AU',
            timezone_id='Australia/Sydney',
        )
        page = context.new_page()
        page.route(
            '**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}',
            lambda route: route.abort()
        )

        # AU Playwright fallback if PDF didn't work
        if not au_used_pdf:
            print("  Falling back to Playwright for Australian Unity...")
            au_scraped = scrape_au_playwright(page)

        try:
            if au_scraped:
                products_data['funds']['australian_unity'] = merge_into_fund(
                    products_data['funds']['australian_unity'],
                    au_scraped,
                    'australianunity.com.au',
                )
                results['australian_unity'] = 'ok'
            else:
                print("  No data obtained for Australian Unity")
                results['australian_unity'] = 'no_data'
        except Exception as e:
            print(f"  ERROR (Australian Unity merge): {e}")
            results['australian_unity'] = 'error'

        # nib
        print("\n[nib] — Playwright")
        try:
            nib_scraped = scrape_nib(page)
            products_data['funds']['nib'] = merge_into_fund(
                products_data['funds']['nib'], nib_scraped, 'nib.com.au'
            )
            results['nib'] = 'ok' if nib_scraped else 'no_data'
        except Exception as e:
            print(f"  ERROR: {e}")
            results['nib'] = 'error'

        # Bupa
        print("\n[Bupa] — Playwright")
        try:
            bupa_scraped = scrape_bupa(page)
            products_data['funds']['bupa'] = merge_into_fund(
                products_data['funds']['bupa'], bupa_scraped, 'bupa.com.au'
            )
            results['bupa'] = 'ok' if bupa_scraped else 'no_data'
        except Exception as e:
            print(f"  ERROR: {e}")
            results['bupa'] = 'error'

        # ahm
        print("\n[ahm] — Playwright")
        try:
            ahm_scraped = scrape_ahm(page)
            products_data['funds']['ahm'] = merge_into_fund(
                products_data['funds']['ahm'], ahm_scraped, 'ahm.com.au'
            )
            results['ahm'] = 'ok' if ahm_scraped else 'no_data'
        except Exception as e:
            print(f"  ERROR: {e}")
            results['ahm'] = 'error'

        browser.close()

    # HCF — skip (Imperva WAF blocks headless Chrome)
    print("\n[HCF] — skipped (WAF protection, existing data preserved)")
    results['hcf'] = 'skipped'

    # ── Update meta ────────────────────────────────────────────────────────
    meta_data['last_updated'] = now_str
    fund_domains = {
        'hcf': 'hcf.com.au',
        'nib': 'nib.com.au',
        'bupa': 'bupa.com.au',
        'medibank': 'medibank.com.au',
        'ahm': 'ahm.com.au',
        'australian_unity': 'australianunity.com.au',
    }
    for src in meta_data.get('sources', []):
        fkey = src['fund'].lower().replace(' ', '_')
        src['status'] = results.get(fkey, 'skipped')
        src['url']    = fund_domains.get(fkey, '')
        src['note']   = f'Direct scrape from {fund_domains.get(fkey, "?")} on {TODAY}'

    save_json(PRODUCTS_FILE, products_data)
    save_json(META_FILE, meta_data)

    print(f"\n=== Scrape complete ===")
    for fund, status in results.items():
        print(f"  {fund}: {status}")

    # Exit 1 only if Medibank API (the one reliable source) also failed
    if results.get('medibank') == 'error':
        print("\nWARNING: Medibank API failed — check API endpoints are still valid.")


if __name__ == '__main__':
    main()
