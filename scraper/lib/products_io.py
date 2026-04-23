"""
products_io — atomic read/write for products.json and meta.json.

Atomic writes use temp file + rename so a crashed run never leaves a
malformed JSON file on disk.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

AEST = timezone(timedelta(hours=10))

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "data"))
PRODUCTS_FILE = os.path.join(DATA_DIR, "products.json")
META_FILE = os.path.join(DATA_DIR, "meta.json")


def fund_plans_file(fund_key: str) -> str:
    return os.path.join(DATA_DIR, f"{fund_key}-plans.json")


def write_json_atomic(path: str, data: dict) -> None:
    _write_json_atomic(path, data)


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: str, data: dict) -> None:
    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp-", suffix=".json", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def read_products() -> dict:
    return _read_json(PRODUCTS_FILE)


def write_products(data: dict) -> None:
    _write_json_atomic(PRODUCTS_FILE, data)


def read_meta() -> dict:
    if not os.path.exists(META_FILE):
        return {}
    return _read_json(META_FILE)


def write_meta(data: dict) -> None:
    _write_json_atomic(META_FILE, data)


def now_iso() -> str:
    return datetime.now(AEST).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(AEST).strftime("%Y-%m-%d")
