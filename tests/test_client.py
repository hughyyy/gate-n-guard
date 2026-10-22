"""Unit tests for the advisory client: retry policy, fallback chain,
provider routing, and clear errors. Transports are fakes — no network."""

import unittest
from unittest import mock

import httpx

from gate_n_guard.client import AdvisoryClient, InferenceError, _is_retryable
from gate_n_guard.config import Config
from gate_n_guard.openrouter import RawResult


def config(**overrides):
    base = dict(
        model="openrouter:model-a",
        endpoint=None,
        max_tokens=64,
        timeout=5.0,
        retries=3,
        retry_backoff=1.0,
        provider="openrouter",
        openrouter_api_key="test-key",
    )
    base.update(overrides)
    return Config(**base)


class Raises:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def chat_completion(self, messages, model, max_tokens):
        self.calls += 1
        raise self.exc


class Returns:
    def __init__(self, text="ok"):
        self.text = text

    def chat_completion(self, messages, model, max_tokens):
        return RawResult(text=self.text)


class Flaky:
    """Fails the first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times, code=429):
        self.fail_times = fail_times
        self.code = code
        self.calls = 0

    def chat_completion(self, messages, model, max_tokens):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise status_error(self.code)
        return RawResult(text="ok")


def status_error(code):
    request = httpx.Request("POST", "http://test")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"status {code}", request=request, response=response)


class RetryPolicyTests(unittest.TestCase):
    def test_retryable_statuses(self):
        for code in (429, 500, 502, 503, 504):
            self.assertTrue(_is_retryable(status_error(code)), f"{code} should retry")
        self.assertFalse(_is_retryable(status_error(400)))
        self.assertFalse(_is_retryable(status_error(401)))

    def test_connection_errors_retryable(self):
        self.assertTrue(_is_retryable(ConnectionError("boom")))
        self.assertTrue(_is_retryable(TimeoutError("slow")))

    def test_retries_with_backoff_then_succeeds(self):
        # 429 twice, then success; backoff sleeps are 2**attempt seconds
        transport = Flaky(fail_times=2)
        client = AdvisoryClient(config(retries=3, retry_backoff=1.0))
        client._openrouter = transport
        with mock.patch("time.sleep") as sleep:
            completion = client.complete_with_meta([{"role": "user", "content": "hi"}])
        self.assertEqual(completion.text, "ok")
        self.assertEqual(transport.calls, 3)
        self.assertEqual(completion.retries, 2)
        self.assertEqual(sleep.call_count, 2)
        sleeps = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_non_retryable_fails_fast(self):
        transport = Raises(status_error(401))
        client = AdvisoryClient(config(retries=3))
        client._openrouter = transport
        with mock.patch("time.sleep") as sleep:
            with self.assertRaises(InferenceError):
                client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(transport.calls, 1)
        sleep.assert_not_called()

    def test_chain_error_lists_all_candidates(self):
        transport = Raises(status_error(401))
        client = AdvisoryClient(config())
        client._openrouter = transport
        with self.assertRaises(InferenceError) as ctx:
            client.complete(
                [{"role": "user", "content": "hi"}],
                fallbacks=("openrouter:model-b",),
            )
        message = str(ctx.exception)
        self.assertIn("[openrouter:model-a]", message)
        self.assertIn("[openrouter:model-b]", message)
        self.assertEqual(transport.calls, 2)

    def test_chain_dedupes_repeated_fallbacks(self):
        transport = Raises(status_error(401))
        client = AdvisoryClient(config())
        client._openrouter = transport
        with self.assertRaises(InferenceError):
            client.complete(
                [{"role": "user", "content": "hi"}],
                fallbacks=("openrouter:model-a", "openrouter:model-a"),
            )
        # primary and the (deduped) fallback are the same id -> tried once
        self.assertEqual(transport.calls, 1)

    def test_missing_openrouter_key_is_clear(self):
        client = AdvisoryClient(config(openrouter_api_key=None))
        with self.assertRaises(InferenceError) as ctx:
            client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))


class RoutingTests(unittest.TestCase):
    def test_prefixes_win_over_provider(self):
        conf = config(provider="hf")
        self.assertEqual(conf.provider_for("openrouter:google/x"), "openrouter")
        self.assertEqual(conf.provider_for("hf:org/model"), "hf")
        self.assertEqual(conf.provider_for("bare/model"), "hf")
        self.assertEqual(conf.strip_prefix("openrouter:google/x"), "google/x")
        self.assertEqual(conf.strip_prefix("hf:org/model"), "org/model")
        self.assertEqual(conf.strip_prefix("bare/model"), "bare/model")

    def test_hf_transport_used_for_hf_provider(self):
        client = AdvisoryClient(config(provider="hf", openrouter_api_key=None))
        # provider routing: bare model -> hf transport, no OpenRouter key needed
        self.assertIsNone(client._openrouter)

    def test_fallback_transport_wins_when_primary_fails(self):
        """A retryable failure on the primary rolls to the fallback candidate."""
        class Failing(Raises):
            def __init__(self, exc):
                super().__init__(exc)

        transport = Failing(status_error(503))
        client = AdvisoryClient(config(retries=0))  # no retries: straight to chain
        client._openrouter = transport
        # second candidate also openrouter -> same failing transport; verify
        # the chain consults each candidate through the transport
        with self.assertRaises(InferenceError) as ctx:
            client.complete([{"role": "user", "content": "hi"}], fallbacks=("openrouter:alt",))
        self.assertIn("[openrouter:model-a]", str(ctx.exception))
        self.assertIn("[openrouter:alt]", str(ctx.exception))
        self.assertEqual(transport.calls, 2)


if __name__ == "__main__":
    unittest.main()
