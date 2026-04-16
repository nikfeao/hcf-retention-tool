"""
HCF Retention Tool — Manual Upload Processor
Called by the process-upload GitHub Actions workflow.

Reads a manually uploaded rate PDF/image from _uploads/, calls Claude Haiku to
extract premiums, and updates data/products.json + data/meta.json.
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone, timedelta

from anthropic import Anthropic

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT     = os.path.join(SCRIPT_DIR, '..')
DATA_DIR      = os.path.join(REPO_ROOT, 'data')
PRODUCTS_FILE = os.path.join(DATA_DIR, 'products.json')
META_FILE     = os.path.join(DATA_DIR, 'meta.json')

AEST  = timezone(timedelta(hours=10))
TODAY = datetime.now(AEST).strftime('%Y-%m-%d')

client = Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

MEDIA_TYPES = {
    '.pdf':  'application/pdf',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
}


def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {os.path.basename(path)}")


def extract_prices(file_path: str, fund_name: str, product_name: str, excess_label: str) -> dict:
    """Call Claude Haiku with the uploaded file and return extracted prices."""
    ext        = os.path.splitext(file_path)[1].lower()
    media_type = MEDIA_TYPES.get(ext, 'image/jpeg')
    is_pdf     = ext == '.pdf'

    with open(file_path, 'rb') as f:
        file_b64 = base64.standard_b64encode(f.read()).decode()

    prompt = f"""This is a health insurance rate document from {fund_name} in Australia.

Find the monthly premium for this product in NSW/ACT:
- Product name: {product_name}
- Excess: {excess_label}
- State: NSW or ACT

Return ONLY a JSON object with these exact keys:
{{
  "single": <monthly $ before rebate as number, or null>,
  "couple": <monthly $ before rebate as number, or null>,
  "family": <monthly $ before rebate as number, or null>,
  "single_parent_family": <monthly $ before rebate as number, or null>,
  "notes": "<brief note on what was found or why a value is null>"
}}

Rules:
- Amounts must be BASE monthly premiums BEFORE any government rebate
- If direct debit discount is included, remove it: divide by 0.96
- Annual prices: divide by 12
- Fortnightly prices: multiply by 26 then divide by 12
- Round to 2 decimal places
- Community rating law: couple = 2×single, family = 2×single, single parent family = single
  (if only single is found, derive the rest; if only couple is found, derive single = couple/2)
- If the product cannot be found in this document, return nulls with a clear notes explanation
"""

    content_block = {
        'type': 'document' if is_pdf else 'image',
        'source': {'type': 'base64', 'media_type': media_type, 'data': file_b64}
    }

    print(f"  Calling Claude Haiku ({media_type}, {os.path.getsize(file_path):,} bytes)...")
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=512,
        messages=[{
            'role': 'user',
            'content': [content_block, {'type': 'text', 'text': prompt}]
        }]
    )

    text = msg.content[0].text.strip()
    if text.startswith('```'):
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
        text = text.split('```')[0]

    return json.loads(text.strip())


def main():
    fund_key    = os.environ['FUND_KEY']
    product_id  = os.environ['PRODUCT_ID']
    excess      = os.environ['EXCESS']
    upload_path = os.environ['UPLOAD_PATH']
    source_note = os.environ.get('SOURCE_NOTE', '').strip() or 'manual upload'

    print(f"\n=== Manual Upload Processor ===")
    print(f"  Fund:        {fund_key}")
    print(f"  Product:     {product_id}")
    print(f"  Excess:      {excess}")
    print(f"  File:        {upload_path}")
    print(f"  Source note: {source_note}")

    products_data = load_json(PRODUCTS_FILE)
    meta_data     = load_json(META_FILE)

    # ── Find product ──────────────────────────────────────────────────────────
    fund = products_data['funds'].get(fund_key)
    if not fund:
        print(f"ERROR: Fund '{fund_key}' not found in products.json")
        sys.exit(1)

    all_products = fund.get('hospital', []) + fund.get('extras', [])
    product = next((p for p in all_products if p['id'] == product_id), None)
    if not product:
        print(f"ERROR: Product '{product_id}' not found under fund '{fund_key}'")
        sys.exit(1)

    print(f"  Matched:     {product['name']}")

    # ── Read uploaded file ────────────────────────────────────────────────────
    file_path = os.path.join(REPO_ROOT, upload_path)
    if not os.path.exists(file_path):
        print(f"ERROR: Upload file not found at {file_path}")
        sys.exit(1)

    # ── Extract prices ────────────────────────────────────────────────────────
    fund_name    = fund.get('name', fund_key)
    excess_label = 'no excess' if excess == 'extras' else f'${excess} excess'

    try:
        extracted = extract_prices(file_path, fund_name, product['name'], excess_label)
    except Exception as e:
        print(f"ERROR: Extraction failed — {e}")
        sys.exit(1)

    print(f"  Extracted: {extracted}")

    single        = float(extracted.get('single')               or 0)
    couple        = float(extracted.get('couple')               or 0)
    family        = float(extracted.get('family')               or 0)
    single_parent = float(extracted.get('single_parent_family') or 0)

    if not any([single, couple, family, single_parent]):
        print(f"WARNING: No prices extracted. Notes: {extracted.get('notes', 'none')}")
        print("Skipping product update — no data to save.")
        sys.exit(0)

    # ── Update product premiums ───────────────────────────────────────────────
    if excess == 'extras':
        # Extras products have flat premiums per cover type
        if single:        product['premiums']['single']               = round(single, 2)
        if couple:        product['premiums']['couple']               = round(couple, 2)
        if family:        product['premiums']['family']               = round(family, 2)
        if single_parent: product['premiums']['single_parent_family'] = round(single_parent, 2)
    else:
        ekey = str(excess)
        if ekey not in product['premiums']:
            product['premiums'][ekey] = {'single': 0, 'couple': 0, 'family': 0, 'single_parent_family': 0}
        if single:        product['premiums'][ekey]['single']               = round(single, 2)
        if couple:        product['premiums'][ekey]['couple']               = round(couple, 2)
        if family:        product['premiums'][ekey]['family']               = round(family, 2)
        if single_parent: product['premiums'][ekey]['single_parent_family'] = round(single_parent, 2)

    product['last_verified'] = TODAY
    product['source']        = f'manual upload: {source_note}'
    product['last_uploaded'] = TODAY
    product['upload_source'] = source_note

    # ── Update meta upload log ────────────────────────────────────────────────
    if 'upload_log' not in meta_data:
        meta_data['upload_log'] = []

    meta_data['upload_log'].insert(0, {
        'date':    TODAY,
        'fund':    fund_name,
        'product': product['name'],
        'excess':  excess_label,
        'single':  round(single, 2) if single else None,
        'couple':  round(couple, 2) if couple else None,
        'source':  source_note,
    })
    meta_data['upload_log'] = meta_data['upload_log'][:50]  # keep last 50

    # ── Save ──────────────────────────────────────────────────────────────────
    save_json(PRODUCTS_FILE, products_data)
    save_json(META_FILE,     meta_data)

    print(f"\n=== Done ===")
    print(f"  {product['name']}: single=${single}, couple=${couple}, family=${family}")
    if extracted.get('notes'):
        print(f"  Notes: {extracted['notes']}")


if __name__ == '__main__':
    main()
