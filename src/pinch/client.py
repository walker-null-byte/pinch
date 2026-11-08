"""OpenAI-compatible chat-completions client with tool calling (plain httpx).

The base URL, model, gateway key and the header that carries the key all come from config.
Bifrost reads the virtual key from `x-bf-vk` (docs.getbifrost.ai/features/governance/virtual-keys) and tells us
who served a request in `extra_fields.routing_info` and in `x-bifrost-routing-info-*` response headers
(docs.getbifrost.ai/providers/response-headers). Failover between providers is Bifrost's job (its fallback
chains); this client only retries the gateway itself when it is unreachable or answers 429/5xx.

For every response we keep the `usage` block (prompt, completion and cached tokens) and any
provider or model fields the response includes, so a later phase can detect a failover.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx


class ProviderError(Exception):
    """The endpoint answered with an error, or could not be reached.

    `kind` is one of: unreachable, rate_limit, server, budget (HTTP 402, Bifrost's budget_exceeded),
    auth (401/403), tool_use_failed (HTTP 400 whose body says `tool_use_failed`: the model wrote a malformed tool call), other.
    """

    def __init__(self, message: str, status: int | None = None, retry_after: str | None = None, kind: str = "other"):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.kind = kind


def _kind(status: int | None) -> str:
    if status is None:
        return "unreachable"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    if status == 402:
        return "budget"
    if status in (401, 403):
        return "auth"
    return "other"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reported: bool = False  # False when the response had no usage block


@dataclass
class ModelReply:
    message: dict  # the assistant message: content and/or tool_calls
    usage: Usage
    requested_model: str
    served_model: str | None  # `model` field of the response (what actually answered)
    provider: str | None  # who served it: Bifrost's routing_info.provider, else `provider`
    finish_reason: str | None
    latency_ms: int
    extra: dict = field(default_factory=dict)  # provider-ish fields we kept, for failover detection
    is_fallback: bool = False  # Bifrost says a fallback provider answered
    primary_provider: str | None = None  # the provider Bifrost tried first (set with is_fallback)
    attempts: int = 1  # tries needed to reach the gateway (>1 means pinch retried)
    cache_hit: bool = False  # Bifrost answered from its response cache: no provider was called
    embedding_tokens: int = 0  # tokens Bifrost spent embedding the request for the cache (0 for exact-match)


class ModelClient:
    def __init__(self, base_url: str, model: str, gateway_key: str = "", key_header: str = "Authorization",
                 max_tokens: int = 2048, timeout: float = 120.0, transport: httpx.BaseTransport | None = None,
                 retries: int = 1, backoff: float = 1.0, sleep=time.sleep, extra_headers: dict | None = None,
                 tool_retries: int = 2, on_event=None):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.max_tokens = max_tokens
        self.key_header = key_header
        self.retries, self.backoff, self._sleep = max(retries, 1), backoff, sleep  # `retries` = total tries
        self.tool_retries = tool_retries  # extra tries when the model's own tool call was malformed (a provider 400 `tool_use_failed`)
        self.on_event = on_event  # called as on_event("tool_use_failed_retry", attempt=..., ...) so the session can record it
        self.headers = {"Content-Type": "application/json", **(extra_headers or {})}
        self.set_key(gateway_key)
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def set_header(self, name: str, value: str | None) -> None:
        if value:
            self.headers[name] = value
        else:
            self.headers.pop(name, None)

    def set_key(self, gateway_key: str) -> None:
        """Use another virtual key from now on (for example the local-only key)."""
        self.headers.pop(self.key_header, None)
        if gateway_key:
            # "Authorization" gets the usual Bearer prefix; any other header carries the raw key.
            self.headers[self.key_header] = f"Bearer {gateway_key}" if self.key_header.lower() == "authorization" else gateway_key

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ModelReply:
        """One model call. Retries the gateway (not the providers behind it) on unreachable/429/5xx."""
        attempt, malformed = 0, 0
        while True:
            attempt += 1
            try:
                reply = self._chat_once(messages, tools)
                reply.attempts = attempt
                return reply
            except ProviderError as e:
                if e.kind == "tool_use_failed":
                    # The model wrote a broken tool call. Sampling differs from try to try, so ask again (not an outage).
                    malformed += 1
                    if malformed > self.tool_retries:
                        raise
                    if self.on_event:
                        self.on_event("tool_use_failed_retry", attempt=malformed, of=self.tool_retries, error=str(e)[:300])
                    continue
                if e.kind not in ("unreachable", "rate_limit", "server") or attempt - malformed >= self.retries:
                    raise
                self._sleep(_wait(e, self.backoff * 2 ** (attempt - malformed - 1)))

    def _chat_once(self, messages: list[dict], tools: list[dict] | None) -> ModelReply:
        body = {"model": self.model, "max_tokens": self.max_tokens}
        if tools:
            body["tools"] = tools  # tools, then system, then messages: the order providers render the prompt in
        body["messages"] = messages
        started = time.monotonic()
        try:
            response = self._http.post(self.url, json=body, headers=self.headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach the model endpoint {self.url}: {type(e).__name__}", kind="unreachable") from e
        latency = int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            kind = "tool_use_failed" if response.status_code == 400 and "tool_use_failed" in response.text else _kind(response.status_code)
            raise ProviderError(
                f"The model endpoint answered HTTP {response.status_code}: {_error_text(response)}",
                status=response.status_code, retry_after=response.headers.get("retry-after"), kind=kind,
            )
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"The model endpoint sent a reply pinch could not read ({type(e).__name__}).") from e
        return _reply_from(data, choice, message, self.model, latency, response.headers)

    def close(self) -> None:
        self._http.close()


def _wait(error: ProviderError, default: float) -> float:
    """Seconds to wait before the next try: the gateway's Retry-After if it sent one (capped), else backoff."""
    try:
        return min(float(error.retry_after), 10.0) if error.retry_after else default
    except ValueError:
        return default


def _reply_from(data: dict, choice: dict, message: dict, requested: str, latency: int, headers=None) -> ModelReply:
    headers = headers or {}
    raw = data.get("usage")
    usage = Usage()
    if isinstance(raw, dict):
        details = raw.get("prompt_tokens_details") or {}
        usage = Usage(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or raw.get("cached_tokens") or raw.get("cache_read_input_tokens") or 0),
            reported=True,
        )
    extra_fields = data.get("extra_fields") if isinstance(data.get("extra_fields"), dict) else {}
    routing = extra_fields.get("routing_info") if isinstance(extra_fields.get("routing_info"), dict) else {}
    # Who served it. Bifrost: extra_fields.routing_info (or the x-bifrost-routing-info-* headers), then the
    # older extra_fields.provider; other gateways and OpenRouter: a top-level `provider`.
    provider = (routing.get("provider") or headers.get("x-bifrost-routing-info-provider")
                or extra_fields.get("provider") or data.get("provider"))
    served_model = routing.get("model") or headers.get("x-bifrost-routing-info-model") or data.get("model")
    is_fallback = bool(routing.get("is_fallback")) or str(headers.get("x-bifrost-routing-info-is-fallback", "")).lower() == "true"
    primary = routing.get("primary_provider") or headers.get("x-bifrost-routing-info-primary-provider")
    extra = {k: data[k] for k in ("id", "system_fingerprint", "provider") if k in data}
    if extra_fields:
        extra["extra_fields"] = extra_fields
    clean = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        clean["tool_calls"] = message["tool_calls"]
    cache_hit = str(headers.get("x-bifrost-cache-hit", "")).lower() == "true"
    try:
        embedding_tokens = int(headers.get("x-bifrost-cache-input-tokens") or 0)
    except ValueError:
        embedding_tokens = 0
    return ModelReply(clean, usage, requested, served_model, provider, choice.get("finish_reason"), latency, extra,
                      is_fallback, primary if is_fallback else None, cache_hit=cache_hit, embedding_tokens=embedding_tokens)


def _error_text(response: httpx.Response) -> str:
    try:
        err = response.json().get("error", {})
        return (err.get("message") if isinstance(err, dict) else str(err)) or response.reason_phrase
    except (ValueError, AttributeError):
        return response.reason_phrase or "error"
