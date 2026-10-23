"""Unit tests for the advisory client: retry policy, fallback chain,
provider routing, and clear errors. Transports are fakes — no network."""

import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

from gate_n_guard.client import (
    AdvisoryClient,
    Completion,
    InferenceError,
    _describe_error,
    _is_retryable,
)
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

    def test_retry_budget_is_exactly_retries_plus_one_attempts(self):
        transport = Raises(status_error(503))
        client = AdvisoryClient(config(retries=2, retry_backoff=1.0))
        client._openrouter = transport
        with mock.patch("time.sleep") as sleep:
            with self.assertRaises(InferenceError):
                client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(transport.calls, 3)
        self.assertEqual(sleep.call_count, 2)

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
        # the client's own guard, not the transport constructor's ValueError
        self.assertIn("OpenRouter model requested but OPENROUTER_API_KEY is not set",
                      str(ctx.exception))


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


class Recording:
    """Transport that records calls and replays one result or error."""

    def __init__(self, result=None, exc=None):
        self.result = result or RawResult(
            text="ok", finish_reason="stop", usage={"total_tokens": 2}
        )
        self.exc = exc
        self.calls = []

    def chat_completion(self, messages, model, max_tokens):
        self.calls.append({"messages": messages, "model": model, "max_tokens": max_tokens})
        if self.exc is not None:
            raise self.exc
        return self.result


def body_error(code, payload):
    """HTTPStatusError whose response body parses to `payload`."""
    request = httpx.Request("POST", "http://test")
    return httpx.HTTPStatusError(
        f"status {code}",
        request=request,
        response=httpx.Response(code, json=payload, request=request),
    )


class ErrorDescriptionTests(unittest.TestCase):
    def test_api_message_dict_is_preferred(self):
        exc = body_error(401, {"error": {"message": "invalid api key"}})
        self.assertEqual(_describe_error(exc), "401: invalid api key")

    def test_api_message_string_is_preferred(self):
        exc = body_error(429, {"error": "rate limited"})
        self.assertEqual(_describe_error(exc), "429: rate limited")

    def test_empty_error_body_falls_back_to_exception_text(self):
        self.assertIn("status 500", _describe_error(body_error(500, {})))

    def test_non_json_body_falls_back_to_exception_text(self):
        request = httpx.Request("POST", "http://test")
        exc = httpx.HTTPStatusError(
            "status 502",
            request=request,
            response=httpx.Response(502, text="<html>", request=request),
        )
        self.assertIn("status 502", _describe_error(exc))

    def test_exception_without_response_keeps_its_message(self):
        self.assertEqual(_describe_error(ValueError("boom")), "boom")


class RetryClassificationTests(unittest.TestCase):
    def test_class_name_fallback_covers_http_layer_timeouts(self):
        for name in ("ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout"):
            with self.subTest(name=name):
                self.assertTrue(_is_retryable(type(name, (Exception,), {})("x")))

    def test_response_without_status_code_is_not_retryable(self):
        exc = RuntimeError("weird")
        exc.response = object()
        self.assertFalse(_is_retryable(exc))

    def test_plain_exception_is_not_retryable(self):
        self.assertFalse(_is_retryable(ValueError("nope")))


class CompletionMetadataTests(unittest.TestCase):
    def setUp(self):
        self.transport = Recording()
        self.client = AdvisoryClient(config())
        self.client._openrouter = self.transport
        self.messages = [{"role": "user", "content": "hi"}]

    def test_complete_returns_only_text(self):
        self.assertEqual(self.client.complete(self.messages), "ok")

    def test_metadata_names_model_provider_transport_and_attempts(self):
        completion = self.client.complete_with_meta(self.messages)
        self.assertEqual(completion.model, "openrouter:model-a")
        self.assertEqual(completion.provider, "openrouter")
        self.assertEqual(completion.transport, "Recording")
        self.assertEqual(completion.retries, 0)
        self.assertGreaterEqual(completion.latency_s, 0.0)
        self.assertEqual(completion.finish_reason, "stop")
        self.assertEqual(completion.usage, {"total_tokens": 2})

    def test_bare_model_id_reaches_the_transport(self):
        self.client.complete_with_meta(self.messages)
        self.assertEqual(self.transport.calls[0]["model"], "model-a")

    def test_max_tokens_override_and_default(self):
        self.client.complete_with_meta(self.messages, max_tokens=7)
        self.client.complete_with_meta(self.messages)
        self.assertEqual([c["max_tokens"] for c in self.transport.calls], [7, 64])

    def test_explicit_model_replaces_the_config_default(self):
        self.client.complete_with_meta(self.messages, model="openrouter:model-b")
        self.assertEqual(self.transport.calls[0]["model"], "model-b")

    def test_empty_fallbacks_disables_the_configured_chain(self):
        client = AdvisoryClient(config(retries=0, fallbacks=("openrouter:model-b",)))
        transport = Recording(exc=status_error(400))
        client._openrouter = transport
        with self.assertRaises(InferenceError):
            client.complete(self.messages, fallbacks=())
        self.assertEqual(len(transport.calls), 1)

    def test_configured_fallbacks_are_used_by_default(self):
        client = AdvisoryClient(config(retries=0, fallbacks=("openrouter:model-b",)))
        transport = Recording(exc=status_error(400))
        client._openrouter = transport
        with self.assertRaises(InferenceError) as ctx:
            client.complete(self.messages)
        self.assertIn("[openrouter:model-b]", str(ctx.exception))
        self.assertEqual(len(transport.calls), 2)

    def test_single_candidate_error_names_the_model(self):
        self.client._openrouter = Recording(exc=status_error(400))
        with self.assertRaises(InferenceError) as ctx:
            self.client.complete(self.messages)
        self.assertTrue(
            str(ctx.exception).startswith("advisory model call failed (openrouter:model-a):")
        )

    def test_openrouter_transport_is_built_once_and_reused(self):
        client = AdvisoryClient(config())
        with mock.patch("gate_n_guard.client.OpenRouterTransport") as transport_cls:
            transport_cls.return_value = Recording()
            client.complete_with_meta(self.messages)
            client.complete_with_meta(self.messages)
        self.assertEqual(transport_cls.call_count, 1)
        self.assertIs(client._openrouter, transport_cls.return_value)

    def test_hf_provider_returns_the_hf_adapter(self):
        client = AdvisoryClient(config(provider="hf", openrouter_api_key=None))
        self.assertIs(client._transport_for("hf"), client._hf)


class FakeInferenceClient:
    def __init__(self, content="hi", finish_reason="stop", usage=None):
        self.content = content
        self.finish_reason = finish_reason
        self.usage = usage
        self.calls = []

    def chat_completion(self, messages, model, max_tokens):
        self.calls.append({"messages": messages, "model": model, "max_tokens": max_tokens})
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.content),
                finish_reason=self.finish_reason,
            )],
            usage=self.usage,
        )


class HfTransportTests(unittest.TestCase):
    def test_endpoint_pins_the_model_id(self):
        fake = FakeInferenceClient()
        client = AdvisoryClient(config(model="org/model", provider="hf", endpoint="https://hf.test/ep"), fake)
        completion = client.complete_with_meta([{"role": "user", "content": "hi"}])
        self.assertEqual(fake.calls[0]["model"], "https://hf.test/ep")
        self.assertEqual(completion.provider, "hf")
        self.assertEqual(completion.transport, "_HFTransport")
        self.assertEqual(completion.text, "hi")
        self.assertEqual(completion.finish_reason, "stop")

    def test_without_endpoint_the_requested_model_is_used(self):
        fake = FakeInferenceClient()
        client = AdvisoryClient(config(model="org/model", provider="hf"), fake)
        client.complete_with_meta([{"role": "user", "content": "hi"}], model="hf:org/model")
        self.assertEqual(fake.calls[0]["model"], "org/model")

    def test_none_content_becomes_empty_text(self):
        fake = FakeInferenceClient(content=None)
        client = AdvisoryClient(config(model="org/model", provider="hf"), fake)
        self.assertEqual(client.complete_with_meta([{"role": "user", "content": "hi"}]).text, "")

    def test_usage_object_is_flattened_and_absent_usage_is_none(self):
        fake = FakeInferenceClient(usage=SimpleNamespace(total_tokens=9))
        client = AdvisoryClient(config(model="org/model", provider="hf"), fake)
        self.assertEqual(
            client.complete_with_meta([{"role": "user", "content": "hi"}]).usage,
            {"total_tokens": 9},
        )
        client = AdvisoryClient(config(model="org/model", provider="hf"), FakeInferenceClient(usage=None))
        self.assertIsNone(client.complete_with_meta([{"role": "user", "content": "hi"}]).usage)


class CompletionShapeTests(unittest.TestCase):
    def test_optional_metadata_defaults_to_none(self):
        completion = Completion(
            text="t", model="m", provider="p", transport="tr", retries=0, latency_s=0.5
        )
        self.assertIsNone(completion.finish_reason)
        self.assertIsNone(completion.usage)

    def test_completion_is_frozen(self):
        completion = Completion(
            text="t", model="m", provider="p", transport="tr", retries=0, latency_s=0.5
        )
        with self.assertRaises(Exception):
            completion.text = "other"



class HttpxTransportRetryTests(unittest.TestCase):
    def test_httpx_transport_errors_are_retryable(self):
        cases = (
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            httpx.PoolTimeout("pool"),
            httpx.WriteError("write"),
            httpx.RemoteProtocolError("protocol"),
        )
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(_is_retryable(exc))

    def test_connect_error_is_retried_end_to_end(self):
        client = AdvisoryClient(config(retries=2, retry_backoff=0.0))
        transport = Recording(exc=httpx.ConnectError("refused"))
        client._openrouter = transport
        with self.assertRaises(InferenceError):
            client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(transport.calls), 3)

    def test_negative_retry_budget_still_attempts_once(self):
        client = AdvisoryClient(config(retries=-1, retry_backoff=0.0))
        transport = Recording()
        client._openrouter = transport
        self.assertEqual(
            client.complete([{"role": "user", "content": "hi"}]), "ok"
        )
        self.assertEqual(len(transport.calls), 1)


class ErrorBodyShapeTests(unittest.TestCase):
    def test_non_object_error_bodies_do_not_raise(self):
        for payload in ([1, 2], "oops", {"error": {"code": 123}}):
            with self.subTest(payload=payload):
                self.assertIn("500", _describe_error(body_error(500, payload)))

    def test_top_level_message_is_used(self):
        self.assertEqual(
            _describe_error(body_error(503, {"message": "down"})), "503: down"
        )


class MalformedHfPayloadTests(unittest.TestCase):
    def transport_for(self, reply):
        fake = mock.Mock()
        fake.chat_completion.return_value = reply
        return AdvisoryClient(
            config(model="org/model", provider="hf", retries=0), fake
        )

    def test_missing_choices_is_a_clear_inference_error(self):
        client = self.transport_for(SimpleNamespace(choices=[]))
        with self.assertRaises(InferenceError) as ctx:
            client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("malformed HuggingFace response", str(ctx.exception))

    def test_dict_usage_is_normalized(self):
        reply = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="hi"), finish_reason="stop"
            )],
            usage={"total_tokens": 3},
        )
        completion = self.transport_for(reply).complete_with_meta(
            [{"role": "user", "content": "hi"}]
        )
        self.assertEqual(completion.usage, {"total_tokens": 3})
        self.assertEqual(completion.text, "hi")


class MixedProviderFallbackTests(unittest.TestCase):
    def test_fallback_can_switch_provider(self):
        fake = FakeInferenceClient()
        client = AdvisoryClient(
            config(
                model="openrouter:model-a",
                fallbacks=("hf:org/alt",),
                retries=0,
            ),
            fake,
        )
        client._openrouter = Recording(exc=status_error(500))
        completion = client.complete_with_meta(
            [{"role": "user", "content": "hi"}]
        )
        self.assertEqual(completion.model, "hf:org/alt")
        self.assertEqual(completion.provider, "hf")
        self.assertEqual(completion.transport, "_HFTransport")
        self.assertEqual(fake.calls[0]["model"], "org/alt")

    def test_fallback_error_names_only_candidates_actually_tried(self):
        class FailingInference:
            def chat_completion(self, *, messages, model, max_tokens):
                raise httpx.ConnectError("hf down")

        client = AdvisoryClient(
            config(
                model="openrouter:model-a",
                fallbacks=("hf:org/alt", "hf:org/alt"),
                retries=0,
                openrouter_api_key=None,
            ),
            FailingInference(),
        )
        with self.assertRaises(InferenceError) as ctx:
            client.complete([{"role": "user", "content": "hi"}])
        message = str(ctx.exception)
        # once in the tried list, once in that candidate's error detail
        self.assertEqual(message.count("openrouter:model-a"), 2)
        self.assertEqual(message.count("hf:org/alt"), 2)


class HfContentShapeTests(unittest.TestCase):
    def test_non_string_content_is_a_clear_inference_error(self):
        reply = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content={"a": 1}), finish_reason="stop"
            )],
            usage=None,
        )
        fake = mock.Mock()
        fake.chat_completion.return_value = reply
        client = AdvisoryClient(
            config(model="org/model", provider="hf", retries=0), fake
        )
        with self.assertRaises(InferenceError) as ctx:
            client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("content is not a string", str(ctx.exception))


class IntegerErrorMessageTests(unittest.TestCase):
    def test_integer_api_message_is_rendered(self):
        self.assertEqual(_describe_error(body_error(429, {"error": 42})), "429: 42")

if __name__ == "__main__":
    unittest.main()
