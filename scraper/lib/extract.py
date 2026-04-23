"""
Claude-based structured extraction.

Wraps the Anthropic Messages API to ask Haiku to pull structured JSON
out of arbitrary content (markdown, HTML, plain text). Used by fund
scrapers when regex isn't reliable enough — e.g. plan-list pages where
the structure is consistent but messy.
"""

from __future__ import annotations

import json
import os
import re

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 90


class ExtractError(Exception):
    pass


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ExtractError("ANTHROPIC_API_KEY not set in environment")
    return key


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def extract_json(
    system: str,
    content: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_content_chars: int = 60000,
    timeout: int = DEFAULT_TIMEOUT,
) -> "list | dict":
    """Ask Claude to return structured JSON from `content`.

    `system` should describe the desired output schema clearly. The
    model is instructed to return only JSON (we strip ``` fences just
    in case).

    Returns the parsed JSON object/list. Raises ExtractError on any
    failure (HTTP error, invalid JSON, empty response).
    """
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content[:max_content_chars]}],
    }
    headers = {
        "x-api-key": _api_key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(API_URL, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise ExtractError(f"network error: {e}") from e

    if resp.status_code != 200:
        raise ExtractError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    payload = resp.json()
    blocks = payload.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text:
        raise ExtractError(f"empty response: {payload!r}")

    cleaned = _strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ExtractError(
            f"invalid JSON returned: {e}\nRaw: {cleaned[:500]!r}"
        ) from e
