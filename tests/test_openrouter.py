"""Unit tests for the OpenRouter transport: request shape, headers, response
parsing, and error classification. `httpx.Client` is replaced by a fake —
no network, stdlib unittest only."""

import unittest
from unittest import mock

import httpx

from gate_n_guard.config import Config
from gate_n_guard.openrouter import (
    OpenRouterError,
    OpenRouterTransport,
    RawResult,
    _content_to_text,
)


def config(**overrides):
    base = dict(
        model="openrouter:model-a",
        endpoint=None,
        max_tokens=64,
        timeout=5.0,
        retries=0,
        retry_backoff=1.0,
        provider="openrouter",
        openrouter_api_key="test-key",
    )
    base.update(overrides)
    return Config(**base)


def response(status_code=200, payload=None, js_error=None):
    """A real httpx.Response so HTTPStatusError carries genuine objects."""
    request = httpx.Request("POST", "http://test/chat/completions")
    if js_error is not None:
        payload = js_error
    return httpx.Response(status_code, json=payload, request=request)


class FakeHttp:
    """Stands in for httpx.Client: records the call, replays one response."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def post(self, path, json=None):
        self.calls.append((path, json))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def build(reply=None, **overrides):
    """An OpenRouterTransport whose httpx.Client is replaced by FakeHttp."""
    fake = FakeHttp(reply if reply is not None else response(payload={"choices": []}))
    with mock.patch("gate_n_guard.openrouter.httpx.Client", return_value=fake) as client_cls:
        transport = OpenRouterTransport(config(**overrides))
    return transport, fake, client_cls


def body(content="hello", finish_reason="stop", usage=None):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        **({"usage": usage} if usage is not None else {}),
    }


class ConstructionTests(unittest.TestCase):
    def test_missing_api_key_is_a_clear_value_error(self):
        with mock.patch("gate_n_guard.openrouter.httpx.Client"):
            with self.assertRaises(ValueError) as ctx:
                OpenRouterTransport(config(openrouter_api_key=None))
        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))

    def test_headers_and_client_options(self):
        _, _, client_cls = build()
        kwargs = client_cls.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(kwargs["timeout"], 5.0)
        self.assertEqual(kwargs["headers"], {
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
        })

    def test_optional_referer_and_title_headers(self):
        _, _, client_cls = build(
            openrouter_site_url="https://example.test",
            openrouter_app_name="gate-n-guard",
        )
        headers = client_cls.call_args.kwargs["headers"]
        self.assertEqual(headers["HTTP-Referer"], "https://example.test")
        self.assertEqual(headers["X-Title"], "gate-n-guard")

    def test_custom_base_url(self):
        _, _, client_cls = build(openrouter_base_url="http://localhost:9999/v1")
        self.assertEqual(client_cls.call_args.kwargs["base_url"], "http://localhost:9999/v1")


class RequestTests(unittest.TestCase):
    def test_posts_openai_compatible_body(self):
        transport, fake, _ = build(reply=response(payload=body()))
        messages = [{"role": "user", "content": "hi"}]
        transport.chat_completion(messages, "vendor/model", 128)
        self.assertEqual(fake.calls, [(
            "/chat/completions",
            {"model": "vendor/model", "messages": messages, "max_tokens": 128},
        )])

    def test_success_parses_text_finish_reason_and_usage(self):
        usage = {"prompt_tokens": 3, "completion_tokens": 5}
        transport, _, _ = build(reply=response(payload=body("answer", "length", usage)))
        result = transport.chat_completion([], "m", 10)
        self.assertEqual(result.text, "answer")
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.usage, usage)

    def test_error_status_raises_status_error_with_response(self):
        reply = response(503, payload={"error": "overloaded"})
        transport, _, _ = build(reply=reply)
        with self.assertRaises(httpx.HTTPStatusError) as ctx:
            transport.chat_completion([], "m", 10)
        self.assertEqual(ctx.exception.response.status_code, 503)

    def test_status_boundary_399_is_success_400_is_error(self):
        transport, _, _ = build(reply=response(399, payload=body("ok")))
        self.assertEqual(transport.chat_completion([], "m", 1).text, "ok")
        transport, _, _ = build(reply=response(400, payload=body()))
        with self.assertRaises(httpx.HTTPStatusError):
            transport.chat_completion([], "m", 1)


class ResponseParsingTests(unittest.TestCase):
    def test_content_list_parts_are_concatenated(self):
        transport, _, _ = build(reply=response(payload=body([
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
            "not-a-dict",
            {"type": "text"},
        ])))
        self.assertEqual(transport.chat_completion([], "m", 1).text, "ab")

    def test_content_none_is_empty_string(self):
        transport, _, _ = build(reply=response(payload=body(None)))
        self.assertEqual(transport.chat_completion([], "m", 1).text, "")

    def test_missing_message_is_treated_as_empty(self):
        payload = {"choices": [{"finish_reason": "stop"}]}
        transport, _, _ = build(reply=response(payload=payload))
        result = transport.chat_completion([], "m", 1)
        self.assertEqual(result.text, "")
        self.assertEqual(result.finish_reason, "stop")

    def test_missing_finish_reason_is_none(self):
        payload = {"choices": [{"message": {"content": "x"}}]}
        transport, _, _ = build(reply=response(payload=payload))
        self.assertIsNone(transport.chat_completion([], "m", 1).finish_reason)
        self.assertIsNone(transport.chat_completion([], "m", 1).usage)

    def test_malformed_choices_raise_openrouter_error(self):
        for payload in ({}, {"choices": []}, {"choices": "nope"}, {"choices": ["str"]}):
            with self.subTest(payload=payload):
                transport, _, _ = build(reply=response(payload=payload))
                with self.assertRaises(OpenRouterError) as ctx:
                    transport.chat_completion([], "m", 1)
                self.assertIn("choices", str(ctx.exception))

    def test_usage_is_passed_through_verbatim(self):
        transport, _, _ = build(reply=response(payload=body("x", usage={"total_tokens": 7})))
        self.assertEqual(transport.chat_completion([], "m", 1).usage, {"total_tokens": 7})


class ContentHelperTests(unittest.TestCase):
    def test_string_content(self):
        self.assertEqual(_content_to_text("plain"), "plain")

    def test_list_content(self):
        self.assertEqual(_content_to_text([{"text": "a"}, {}, {"text": "b"}]), "ab")

    def test_non_string_non_list_content(self):
        self.assertEqual(_content_to_text(42), "42")
        self.assertEqual(_content_to_text(None), "")
        self.assertEqual(_content_to_text({}), "")


class RawResultTests(unittest.TestCase):
    def test_defaults(self):
        result = RawResult("text")
        self.assertEqual(result.text, "text")
        self.assertIsNone(result.finish_reason)
        self.assertIsNone(result.usage)

    def test_slots_reject_unknown_attributes(self):
        result = RawResult("text")
        with self.assertRaises(AttributeError):
            result.extra = 1



class MalformedBodyTests(unittest.TestCase):
    def call(self, reply):
        transport, _, _ = build(reply=reply)
        return transport.chat_completion([], "m", 1)

    def test_non_json_success_body_is_an_openrouter_error(self):
        reply = httpx.Response(
            200,
            text="<html>",
            request=httpx.Request("POST", "http://test/chat/completions"),
        )
        with self.assertRaises(OpenRouterError) as ctx:
            self.call(reply)
        self.assertIn("not JSON", str(ctx.exception))

    def test_json_list_success_body_is_an_openrouter_error(self):
        with self.assertRaises(OpenRouterError) as ctx:
            self.call(response(payload=[1, 2]))
        self.assertIn("not an object", str(ctx.exception))

    def test_json_scalar_success_body_is_an_openrouter_error(self):
        with self.assertRaises(OpenRouterError) as ctx:
            self.call(response(payload="hi"))
        self.assertIn("not an object", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
