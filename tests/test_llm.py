"""Tests for the Anthropic client and its response cache.

The client had never run against the real API, so these pin the request shape
and — more importantly — the failure modes. Every way of getting no usable text
back used to collapse into an empty string, which the draft check rejected and
the template quietly replaced. On stage that looks like a working demo that has
stopped using the model.

Nothing here touches the network; urlopen is replaced throughout.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from recall_agent import llm
from recall_agent.llm import AUTO, LIVE, OFFLINE, LLMClient, LLMUnavailable


def api_response(
    text: str = "hello", stop_reason: str = "end_turn", **extra: object
) -> dict:
    payload: dict = {
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
    }
    payload.update(extra)
    return payload


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def fake_urlopen(payload: dict):
    def _open(request, timeout=None):  # noqa: ANN001
        _open.last_request = request
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    return _open


class ClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.client = LLMClient(
            mode=AUTO, cache_dir=Path(self.tmp.name), api_key="test-key"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()


class RequestShapeTests(ClientTestCase):
    def _sent_body(self, payload: dict) -> dict:
        opener = fake_urlopen(payload)
        with patch("urllib.request.urlopen", opener):
            self.client.complete("sys", "user")
        return json.loads(opener.last_request.data.decode("utf-8"))

    def test_request_carries_the_required_fields(self) -> None:
        body = self._sent_body(api_response())
        self.assertEqual(body["model"], self.client.model)
        self.assertEqual(body["system"], "sys")
        self.assertEqual(body["messages"], [{"role": "user", "content": "user"}])

    def test_thinking_is_disabled(self) -> None:
        # Left adaptive, thinking tokens count against max_tokens and can
        # consume the whole budget before any text block exists.
        self.assertEqual(self._sent_body(api_response())["thinking"], {"type": "disabled"})

    def test_max_tokens_has_real_headroom(self) -> None:
        body = self._sent_body(api_response())
        self.assertGreaterEqual(body["max_tokens"], 2048)

    def test_required_headers_are_sent(self) -> None:
        opener = fake_urlopen(api_response())
        with patch("urllib.request.urlopen", opener):
            self.client.complete("sys", "user")
        headers = {k.lower(): v for k, v in opener.last_request.headers.items()}
        self.assertEqual(headers["X-api-key".lower()], "test-key")
        self.assertEqual(headers["Anthropic-version".lower()], "2023-06-01")
        self.assertEqual(headers["Content-type".lower()], "application/json")


class FailureReportingTests(ClientTestCase):
    """Every empty response must name its own cause."""

    def _expect_unavailable(self, payload: dict, fragment: str) -> None:
        with patch("urllib.request.urlopen", fake_urlopen(payload)):
            with self.assertRaises(LLMUnavailable) as caught:
                self.client.complete("sys", "user")
        self.assertIn(fragment, str(caught.exception))

    def test_hitting_max_tokens_is_reported_not_swallowed(self) -> None:
        # The bug this whole change exists for: the response is cut off before
        # any text, and the old code returned "" and fell back to a template.
        self._expect_unavailable(
            api_response(text="", stop_reason="max_tokens"), "max_tokens"
        )

    def test_a_refusal_is_reported_with_its_category(self) -> None:
        self._expect_unavailable(
            api_response(text="", stop_reason="refusal", stop_details={"category": "cyber"}),
            "declined",
        )

    def test_an_empty_response_names_the_stop_reason(self) -> None:
        self._expect_unavailable(api_response(text="", stop_reason="end_turn"), "end_turn")

    def test_thinking_blocks_alone_are_not_text(self) -> None:
        payload = {
            "content": [{"type": "thinking", "thinking": "..."}],
            "stop_reason": "end_turn",
        }
        self._expect_unavailable(payload, "no text")

    def test_text_blocks_are_extracted_past_other_block_types(self) -> None:
        payload = {
            "content": [
                {"type": "thinking", "thinking": "..."},
                {"type": "text", "text": "the answer"},
            ],
            "stop_reason": "end_turn",
        }
        with patch("urllib.request.urlopen", fake_urlopen(payload)):
            self.assertEqual(self.client.complete("sys", "user").text, "the answer")

    def test_a_client_error_is_not_retried(self) -> None:
        def _raise(request, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)

        with patch("urllib.request.urlopen", _raise):
            with self.assertRaises(LLMUnavailable) as caught:
                self.client.complete("sys", "user")
        self.assertIn("401", str(caught.exception))


class CacheTests(ClientTestCase):
    def test_a_response_round_trips_through_the_cache(self) -> None:
        with patch("urllib.request.urlopen", fake_urlopen(api_response("first"))):
            first = self.client.complete("sys", "user")
        self.assertFalse(first.from_cache)

        # Second call must not touch the network at all.
        def _explode(request, timeout=None):  # noqa: ANN001
            raise AssertionError("network was used despite a cached response")

        with patch("urllib.request.urlopen", _explode):
            second = self.client.complete("sys", "user")
        self.assertTrue(second.from_cache)
        self.assertEqual(second.text, "first")

    def test_offline_mode_never_opens_a_socket(self) -> None:
        offline = LLMClient(mode=OFFLINE, cache_dir=self.client.cache_dir)

        def _explode(request, timeout=None):  # noqa: ANN001
            raise AssertionError("offline mode opened a socket")

        with patch("urllib.request.urlopen", _explode):
            with self.assertRaises(LLMUnavailable) as caught:
                offline.complete("sys", "never cached")
        self.assertIn("offline", str(caught.exception))

    def test_live_mode_bypasses_the_cache(self) -> None:
        with patch("urllib.request.urlopen", fake_urlopen(api_response("first"))):
            self.client.complete("sys", "user")
        live = LLMClient(mode=LIVE, cache_dir=self.client.cache_dir, api_key="k")
        with patch("urllib.request.urlopen", fake_urlopen(api_response("second"))):
            result = live.complete("sys", "user")
        self.assertFalse(result.from_cache)
        self.assertEqual(result.text, "second")


class CacheKeyTests(unittest.TestCase):
    def test_normalisation_folds_typing_differences_together(self) -> None:
        # The operator types the reply by hand on the day; casing and a full
        # stop must not cost a cache hit.
        variants = [
            "I'm in Australia until March",
            "i'm in australia until march",
            "  I'm in Australia until March.  ",
            "I'm  in  Australia  until  March!",
            "im in australia until march",
            "I’m in Australia until March",
        ]
        keys = {
            llm._cache_key("m", "sys", text, 4096, normalise=True) for text in variants
        }
        self.assertEqual(len(keys), 1)

    def test_normalisation_still_separates_different_replies(self) -> None:
        a = llm._cache_key("m", "sys", "stop messaging me", 4096, normalise=True)
        b = llm._cache_key("m", "sys", "book me in please", 4096, normalise=True)
        self.assertNotEqual(a, b)

    def test_exact_keys_stay_exact(self) -> None:
        # Drafting must never replay one patient's message for another.
        a = llm._cache_key("m", "sys", "Hi Vijay", 4096)
        b = llm._cache_key("m", "sys", "hi vijay", 4096)
        self.assertNotEqual(a, b)

    def test_the_model_is_part_of_the_key(self) -> None:
        a = llm._cache_key("model-a", "sys", "user", 4096)
        b = llm._cache_key("model-b", "sys", "user", 4096)
        self.assertNotEqual(a, b)

    def test_normalise_helper_is_idempotent(self) -> None:
        once = llm.normalise_for_cache("  Until March.  ")
        self.assertEqual(llm.normalise_for_cache(once), once)
        self.assertEqual(once, "until march")

    def test_punctuation_is_folded_even_mid_string(self) -> None:
        # The reply is wrapped in <patient_reply> tags before hashing, so a
        # trailing full stop lands in the middle of the string.
        wrapped = "<patient_reply>\n{}\n</patient_reply>"
        a = llm.normalise_for_cache(wrapped.format("I'm away until March"))
        b = llm.normalise_for_cache(wrapped.format("im away until march."))
        self.assertEqual(a, b)


class ReplyCacheIntegrationTests(unittest.TestCase):
    def test_a_rehearsed_reply_replays_for_a_near_miss(self) -> None:
        # End to end: cache the scripted wording, then type it differently.
        from datetime import date

        from recall_agent.replies import DEFER_WITH_DATE, understand_reply

        with tempfile.TemporaryDirectory() as tmp:
            client = LLMClient(mode=AUTO, cache_dir=Path(tmp), api_key="k")
            classification = json.dumps(
                {"intent": "DEFER_WITH_DATE", "confidence": 0.95, "rationale": "away"}
            )
            with patch("urllib.request.urlopen", fake_urlopen(api_response(classification))):
                understand_reply(
                    "I'm in Australia until March", date(2026, 9, 8), client=client
                )

            offline = LLMClient(mode=OFFLINE, cache_dir=Path(tmp))

            def _explode(request, timeout=None):  # noqa: ANN001
                raise AssertionError("offline replay opened a socket")

            with patch("urllib.request.urlopen", _explode):
                result = understand_reply(
                    "im in australia until march.", date(2026, 9, 8), client=offline
                )

        self.assertEqual(result.intent, DEFER_WITH_DATE)
        self.assertEqual(result.source, "llm_cache")
        self.assertEqual(result.defer_until, date(2027, 3, 1))


if __name__ == "__main__":
    unittest.main()
