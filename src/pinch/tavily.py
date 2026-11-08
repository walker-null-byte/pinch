"""Tavily `docs_lookup`: a few short, sourced snippets for an unknown API or error message.

Plain httpx against Tavily's search endpoint (base URL configurable so tests use a mock server). Basic depth, at
most 3 results, each snippet cut to about 300 characters, with the source URL. The query must be a single line of at
most 300 characters and should hold error text and library names only, never source code; secrets-looking tokens are
removed. Results are cached for the session and every call's credits are written to the ledger.

It changes nothing locally, so it is a READ tool. It is not offered for confidential repos, and refuses if called.
"""

from __future__ import annotations

import re
from typing import Callable

import httpx

MAX_QUERY = 300
MAX_RESULTS = 3
MAX_SNIPPET = 300
DESCRIPTION = (
    "Look up current library or API documentation, or an error message, on the web. Returns up to 3 short snippets with "
    "source URLs. Use it when you hit an API you do not know or an error you cannot explain. The query must be one line "
    "of error text and library names only. Never include source code, file contents, paths or secrets."
)


def clean_query(query: str) -> str:
    """One line, no code fences, no key-like tokens, at most 300 characters."""
    query = re.sub(r"```.*?```", " ", str(query), flags=re.S)
    query = re.sub(r"\b(sk|tvly|ghp|gho|xox[bp]|AKIA)[-_A-Za-z0-9]{12,}\b|\b[0-9a-fA-F]{32,}\b", "[removed]", query)
    return " ".join(query.split())[:MAX_QUERY]


class DocsLookup:
    def __init__(self, api_key: str, base_url: str = "https://api.tavily.com", enabled: bool = True,
                 on_call: Callable[..., None] = lambda **kw: None, transport: httpx.BaseTransport | None = None):
        self.api_key, self.base_url, self.enabled, self.on_call = api_key, base_url.rstrip("/"), enabled, on_call
        self._cache: dict[str, str] = {}
        self._http = httpx.Client(timeout=20.0, transport=transport)

    def __call__(self, query: str) -> str:
        """Search the docs for an error message or an API name.

        Args:
            query (str): One line of error text and library names. No source code, no paths, no secrets.
        """
        if not self.enabled:
            return "Error: docs_lookup is switched off for this repo."
        cleaned = clean_query(query)
        if not cleaned:
            return "Error: give a short query made of error text and library names."
        if cleaned in self._cache:
            self.on_call(query=cleaned, credits=0, cached=True, results=0)
            return self._cache[cleaned]
        try:
            response = self._http.post(self.base_url + "/search", headers={"Authorization": f"Bearer {self.api_key}"},
                                       json={"query": cleaned, "search_depth": "basic", "max_results": MAX_RESULTS,
                                             "include_answer": False, "include_usage": True})
        except httpx.HTTPError as e:
            return f"Error: could not reach the docs service ({type(e).__name__})."
        if response.status_code != 200:
            return f"Error: the docs service answered HTTP {response.status_code}."
        try:
            data = response.json()
            results = (data.get("results") or [])[:MAX_RESULTS]
            credits = int((data.get("usage") or {}).get("credits", 1))
        except (ValueError, AttributeError, TypeError):
            return "Error: the docs service sent a reply pinch could not read."
        lines = [f"{i}. {r.get('title', '')[:100]}: {' '.join(str(r.get('content', '')).split())[:MAX_SNIPPET]} ({r.get('url', '')})"
                 for i, r in enumerate(results, 1)]
        text = "\n".join(lines) or "No results."
        self._cache[cleaned] = text
        self.on_call(query=cleaned, credits=credits, cached=False, results=len(results))
        return text
