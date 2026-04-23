"""
Thin wrapper around Firecrawl's v1 /scrape endpoint.

Reads FIRECRAWL_API_KEY from the environment. Raises FirecrawlError on
any failure (non-200, success=false, missing key) so callers can
consistently catch a single exception type.
"""

from __future__ import annotations

import os
import time

import httpx

API_BASE = "https://api.firecrawl.dev/v1"
DEFAULT_TIMEOUT = 60
RETRY_STATUSES = {500, 502, 503, 504, 522, 524}
MAX_ATTEMPTS = 3
BACKOFF_BASE = 2.0  # seconds; actual wait = BACKOFF_BASE ** attempt


class FirecrawlError(Exception):
    pass


def _api_key() -> str:
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise FirecrawlError("FIRECRAWL_API_KEY not set in environment")
    return key


def scrape(
    url: str,
    formats: list[str] | None = None,
    only_main_content: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict:
    """Scrape one URL. Retries on transient 5xx + network errors.

    Returns the `data` object from the Firecrawl response. Keys on
    success: markdown, html, metadata (each optional depending on
    requested `formats`). Raises FirecrawlError on final failure.
    """
    body = {
        "url": url,
        "formats": formats or ["markdown"],
        "onlyMainContent": only_main_content,
    }
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }

    last_err: str | None = None
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    f"{API_BASE}/scrape", headers=headers, json=body
                )
        except httpx.HTTPError as e:
            last_err = f"network error: {e}"
            if attempt < max_attempts - 1:
                time.sleep(BACKOFF_BASE ** attempt)
                continue
            raise FirecrawlError(last_err) from e

        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("success"):
                return payload.get("data", {})
            raise FirecrawlError(
                f"Firecrawl returned success=false: {payload.get('error', payload)!r}"
            )

        if resp.status_code in RETRY_STATUSES and attempt < max_attempts - 1:
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            time.sleep(BACKOFF_BASE ** attempt)
            continue

        raise FirecrawlError(
            f"HTTP {resp.status_code} from Firecrawl: {resp.text[:300]}"
        )

    raise FirecrawlError(f"exhausted {max_attempts} attempts; last: {last_err}")
