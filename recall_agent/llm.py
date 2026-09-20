"""Minimal Anthropic API client over urllib, with an on-disk replay cache.

No SDK and no third-party packages, matching the rest of the project. The cache
is what makes the demo safe to run on venue wifi: every response is stored by a
hash of the exact request, so a rehearsed demo replays from disk and never opens
a socket.

Prompts and responses are treated as patient data. Nothing here writes them to
the audit log; callers log a digest instead (see recall_agent.audit).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_CACHE = Path("fixtures/demo/llm_cache")

# Headroom, not a target. Both calls here produce a few hundred tokens at most,
# but max_tokens caps everything the model generates. Setting it near the
# expected output length risks the response being cut off before any text block
# exists, which would look exactly like an API failure.
DEFAULT_MAX_TOKENS = 4096

# Drafting a reminder and labelling a reply are short, mechanical tasks that do
# not benefit from reasoning. Disabling it also removes any chance of thinking
# tokens consuming the output budget.
THINKING = {"type": "disabled"}

# Blueprint §7: technical retries per action.
MAX_RETRIES = 2
TIMEOUT_SECONDS = 20

AUTO = "auto"
OFFLINE = "offline"
LIVE = "live"


class LLMUnavailable(RuntimeError):
    """Raised when no response could be obtained. Callers must fall back."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    from_cache: bool
    model: str


def normalise_for_cache(text: str) -> str:
    """Fold trivial phrasing differences together for cache lookup.

    A demo operator types a patient's reply by hand, so "Until March.",
    "  until march  " and "im" for "I'm" should all replay the same cached
    classification rather than silently dropping to the rule-based fallback.
    Apostrophes are dropped because they never carry meaning here; internal
    wording is left alone, so genuinely different replies still miss.
    Only used where the input is free text typed on the day.
    """

    # Apostrophes close up ("I'm" -> "im"); every other punctuation mark becomes
    # a space. Punctuation is dropped wherever it sits, not just at the ends,
    # because the reply is wrapped in tags before it reaches here, so a trailing
    # full stop ends up in the middle of the string being hashed.
    folded = text.lower().replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())


def _cache_key(
    model: str, system: str, user: str, max_tokens: int, normalise: bool = False
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "system": system,
            "user": normalise_for_cache(user) if normalise else user,
            "max_tokens": max_tokens,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _text_from(payload: Mapping[str, Any]) -> str:
    """Extract the response text, or say precisely why there is none.

    An empty string is not a usable answer, and every way of producing one looks
    identical downstream: the draft check rejects it and the template takes
    over. Naming the cause here is the difference between "the model was never
    consulted" and "the model declined" in the audit log.
    """

    stop_reason = payload.get("stop_reason")
    if stop_reason == "refusal":
        details = payload.get("stop_details") or {}
        raise LLMUnavailable(
            f"model declined the request (category: {details.get('category')})"
        )

    text = "".join(
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    ).strip()

    if not text:
        if stop_reason == "max_tokens":
            raise LLMUnavailable(
                "response hit max_tokens before producing any text; raise max_tokens"
            )
        raise LLMUnavailable(f"response contained no text (stop_reason: {stop_reason})")
    return text


class LLMClient:
    def __init__(
        self,
        mode: str = AUTO,
        model: str = DEFAULT_MODEL,
        cache_dir: Path = DEFAULT_CACHE,
        api_key: str | None = None,
    ) -> None:
        if mode not in (AUTO, OFFLINE, LIVE):
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.model = model
        self.cache_dir = cache_dir
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    @property
    def can_call_network(self) -> bool:
        return self.mode != OFFLINE and bool(self.api_key)

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> str | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))["text"]
        except (OSError, ValueError, KeyError):
            return None

    def _write_cache(self, key: str, text: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(key).write_text(
            json.dumps({"text": text, "model": self.model}, indent=2),
            encoding="utf-8",
        )

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        normalise_key: bool = False,
    ) -> LLMResponse:
        key = _cache_key(self.model, system, user, max_tokens, normalise_key)

        if self.mode != LIVE:
            cached = self._read_cache(key)
            if cached is not None:
                return LLMResponse(cached, from_cache=True, model=self.model)

        if not self.can_call_network:
            raise LLMUnavailable(
                "no cached response and "
                + ("offline mode is set" if self.mode == OFFLINE else "ANTHROPIC_API_KEY is not set")
            )

        text = self._call_api(system, user, max_tokens)
        self._write_cache(key, text)
        return LLMResponse(text, from_cache=False, model=self.model)

    def _call_api(self, system: str, user: str, max_tokens: int) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "thinking": THINKING,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            API_URL,
            data=body,
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key or "",
                "anthropic-version": API_VERSION,
            },
            method="POST",
        )

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as handle:
                    payload = json.loads(handle.read().decode("utf-8"))
                return _text_from(payload)
            except urllib.error.HTTPError as error:
                # 4xx other than rate limiting will not improve on retry.
                if error.code not in (408, 429, 500, 502, 503, 529):
                    raise LLMUnavailable(f"API returned {error.code}") from error
                last_error = error
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                last_error = error
            if attempt < MAX_RETRIES:
                time.sleep(0.5 * (attempt + 1))

        raise LLMUnavailable(f"API unreachable after {MAX_RETRIES + 1} attempts: {last_error}")


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Models sometimes wrap JSON in prose or a fenced block. Anything that is not
    a single parseable object is an error, not something to guess at.
    """

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = [
            line
            for line in candidate.splitlines()
            if not line.strip().startswith("```")
        ]
        candidate = "\n".join(lines).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("response contained no JSON object")
    parsed = json.loads(candidate[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response JSON was not an object")
    return parsed
