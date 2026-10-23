"""Unit tests for environment-driven configuration: defaults, parsing of
numbers/lists, provider routing helpers, and invalid input handling.
stdlib unittest only; env is patched per test."""

import os
import unittest
from unittest import mock

from gate_n_guard.config import (
    DEFAULT_FALLBACKS,
    DEFAULT_MODEL,
    DEFAULT_OPENROUTER_URL,
    Config,
    _env_float,
    _env_int,
    _parse_fallbacks,
    load_config,
)


def conf(**overrides):
    base = dict(
        model="openrouter:model-a",
        endpoint=None,
        max_tokens=64,
        timeout=5.0,
        retries=0,
        retry_backoff=1.0,
    )
    base.update(overrides)
    return Config(**base)


class HelperTests(unittest.TestCase):
    def test_env_int_reads_and_defaults(self):
        with mock.patch.dict(os.environ, {"X_INT": "12"}, clear=False):
            self.assertEqual(_env_int("X_INT", 3), 12)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_env_int("X_INT", 3), 3)

    def test_env_int_invalid_falls_back(self):
        for raw in ("", "abc", "1.5", "0x10"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"X_INT": raw}, clear=False):
                self.assertEqual(_env_int("X_INT", 7), 7)

    def test_env_float_reads_and_defaults(self):
        with mock.patch.dict(os.environ, {"X_FLOAT": "2.5"}, clear=False):
            self.assertEqual(_env_float("X_FLOAT", 1.0), 2.5)
        with mock.patch.dict(os.environ, {"X_FLOAT": "nope"}, clear=False):
            self.assertEqual(_env_float("X_FLOAT", 1.0), 1.0)

    def test_fallbacks_parsing(self):
        self.assertEqual(_parse_fallbacks(None), DEFAULT_FALLBACKS)
        self.assertEqual(_parse_fallbacks(""), ())
        self.assertEqual(_parse_fallbacks("   "), ())
        self.assertEqual(_parse_fallbacks("a,b c"), ("a", "b", "c"))
        self.assertEqual(_parse_fallbacks(" a , b ,, c "), ("a", "b", "c"))
        # duplicates are preserved: dedupe happens in the client chain
        self.assertEqual(_parse_fallbacks("a a"), ("a", "a"))


class ConfigTests(unittest.TestCase):
    def test_target_prefers_endpoint(self):
        self.assertEqual(conf().target, "openrouter:model-a")
        self.assertEqual(conf(endpoint="https://hf.test/endpoint").target, "https://hf.test/endpoint")

    def test_provider_for_prefix_and_default(self):
        routed = conf(provider="hf")
        self.assertEqual(routed.provider_for("openrouter:x"), "openrouter")
        self.assertEqual(routed.provider_for("hf:org/model"), "hf")
        self.assertEqual(routed.provider_for("bare/model"), "hf")
        self.assertEqual(conf(provider="auto").provider_for("bare"), "auto")

    def test_strip_prefix(self):
        self.assertEqual(Config.strip_prefix("openrouter:a/b"), "a/b")
        self.assertEqual(Config.strip_prefix("hf:a/b"), "a/b")
        self.assertEqual(Config.strip_prefix("a/b"), "a/b")
        # only the leading prefix is stripped
        self.assertEqual(Config.strip_prefix("hf:a/hf:b"), "a/hf:b")

    def test_config_is_frozen(self):
        with self.assertRaises(Exception):
            conf().model = "other"


class LoadConfigTests(unittest.TestCase):
    def test_defaults_with_empty_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.model, DEFAULT_MODEL)
        self.assertEqual(cfg.provider, "openrouter")
        self.assertEqual(cfg.max_tokens, 2048)
        self.assertEqual(cfg.timeout, 60.0)
        self.assertEqual(cfg.retries, 3)
        self.assertEqual(cfg.retry_backoff, 2.0)
        self.assertEqual(cfg.fallbacks, ())
        self.assertEqual(cfg.openrouter_base_url, DEFAULT_OPENROUTER_URL)
        self.assertIsNone(cfg.endpoint)
        self.assertIsNone(cfg.openrouter_api_key)
        self.assertIsNone(cfg.openrouter_site_url)
        self.assertIsNone(cfg.openrouter_app_name)
        self.assertIsNone(cfg.rulebook_path)
        self.assertIsNone(cfg.journal_path)

    def test_full_environment(self):
        env = {
            "GATE_N_GUARD_MODEL": "hf:org/model",
            "HF_ENDPOINT": "https://hf.test/endpoint",
            "GATE_N_GUARD_MAX_TOKENS": "99",
            "GATE_N_GUARD_TIMEOUT": "1.5",
            "GATE_N_GUARD_RETRIES": "0",
            "GATE_N_GUARD_RETRY_BACKOFF": "0.25",
            "GATE_N_GUARD_ENGINE": "hf",
            "GATE_N_GUARD_FALLBACKS": "a, b",
            "OPENROUTER_API_KEY": "key",
            "OPENROUTER_BASE_URL": "http://localhost:1/v1",
            "OPENROUTER_SITE_URL": "https://site.test",
            "OPENROUTER_APP_NAME": "app",
            "GATE_N_GUARD_RULEBOOK": "/tmp/r.json",
            "GATE_N_GUARD_JOURNAL": "/tmp/j.jsonl",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.model, "hf:org/model")
        self.assertEqual(cfg.endpoint, "https://hf.test/endpoint")
        self.assertEqual(cfg.max_tokens, 99)
        self.assertEqual(cfg.timeout, 1.5)
        self.assertEqual(cfg.retries, 0)
        self.assertEqual(cfg.retry_backoff, 0.25)
        self.assertEqual(cfg.provider, "hf")
        self.assertEqual(cfg.fallbacks, ("a", "b"))
        self.assertEqual(cfg.openrouter_api_key, "key")
        self.assertEqual(cfg.openrouter_base_url, "http://localhost:1/v1")
        self.assertEqual(cfg.openrouter_site_url, "https://site.test")
        self.assertEqual(cfg.openrouter_app_name, "app")
        self.assertEqual(cfg.rulebook_path, "/tmp/r.json")
        self.assertEqual(cfg.journal_path, "/tmp/j.jsonl")

    def test_engine_is_normalized_and_validated(self):
        for raw, want in ((" HF ", "hf"), ("AUTO", "auto"), ("OpenRouter", "openrouter")):
            with self.subTest(raw=raw), mock.patch.dict(
                os.environ, {"GATE_N_GUARD_ENGINE": raw}, clear=True
            ):
                self.assertEqual(load_config().provider, want)
        with mock.patch.dict(os.environ, {"GATE_N_GUARD_ENGINE": "bogus"}, clear=True):
            self.assertEqual(load_config().provider, "openrouter")

    def test_empty_optional_strings_are_none(self):
        env = {
            "OPENROUTER_API_KEY": "",
            "HF_ENDPOINT": "",
            "GATE_N_GUARD_RULEBOOK": "",
            "GATE_N_GUARD_JOURNAL": "",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertIsNone(cfg.openrouter_api_key)
        self.assertIsNone(cfg.endpoint)
        self.assertIsNone(cfg.rulebook_path)
        self.assertIsNone(cfg.journal_path)

    def test_invalid_numbers_fall_back_to_defaults(self):
        env = {
            "GATE_N_GUARD_MAX_TOKENS": "many",
            "GATE_N_GUARD_TIMEOUT": "soon",
            "GATE_N_GUARD_RETRIES": "lots",
            "GATE_N_GUARD_RETRY_BACKOFF": "later",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(
            (cfg.max_tokens, cfg.timeout, cfg.retries, cfg.retry_backoff),
            (2048, 60.0, 3, 2.0),
        )

    def test_blank_fallbacks_disables_the_chain(self):
        with mock.patch.dict(os.environ, {"GATE_N_GUARD_FALLBACKS": "   "}, clear=True):
            self.assertEqual(load_config().fallbacks, ())



class ConfigBoundsTests(unittest.TestCase):
    def test_out_of_range_numbers_are_bounded(self):
        env = {
            "GATE_N_GUARD_MAX_TOKENS": "0",
            "GATE_N_GUARD_TIMEOUT": "-5",
            "GATE_N_GUARD_RETRIES": "-1",
            "GATE_N_GUARD_RETRY_BACKOFF": "-2",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.max_tokens, 1)
        self.assertEqual(cfg.timeout, 60.0)
        self.assertEqual(cfg.retries, 0)
        self.assertEqual(cfg.retry_backoff, 0.0)

    def test_helpers_apply_their_minimums(self):
        with mock.patch.dict(
            os.environ, {"X_INT": "-4", "X_FLOAT": "-1.5"}, clear=False
        ):
            self.assertEqual(_env_int("X_INT", 5, minimum=0), 0)
            self.assertEqual(_env_float("X_FLOAT", 2.0, minimum=0.0), 0.0)

if __name__ == "__main__":
    unittest.main()
