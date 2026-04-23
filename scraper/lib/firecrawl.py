"""
Thin wrapper around Firecrawl's v1 /scrape endpoint.

Reads FIRECRAWL_API_KEY from the environment. Raises FirecrawlError on
any failure (non-200, success=false, missing key) so callers can
consistently catch a single exception type.
"""

from __future__ import annotations

import os

import httpx

API_BASE = "https://api.firecrawl.dev/v1"
DEFAULT_TIMEOUT = 60


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
) -> dict:
    """Scrape one URL. Returns the `data` object from the Firecrawl response.

    Keys on success: markdown, html, metadata (each optional depending on
    requested `formats`).
    """
    body = {
        "url": url,
        "formats": formats or ["markdown"],
        "onlyMainContent": only_main_content,
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{API_BASE}/scrape",
                headers={
                    "Authorization": f"Bearer {_api_key()}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.HTTPError as e:
        raise FirecrawlError(f"request failed: {e}") from e

    if resp.status_code != 200:
        raise FirecrawlError(
            f"HTTP {resp.status_code} from Firecrawl: {resp.text[:300]}"
        )

    payload = resp.json()
    if not payload.get("success"):
        raise FirecrawlError(
            f"Firecrawl returned success=false: {payload.get('error', payload)!r}"
        )

    return payload.get("data", {})
