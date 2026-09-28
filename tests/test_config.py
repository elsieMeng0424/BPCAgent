"""Configuration precedence, validation, isolation and safe diagnostics."""

import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from bpcagent import Config
from bpcagent.exceptions import ConfigException


class ConfigTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_defaults_and_partial_config_preserve_environment(self):
        config = Config.resolve(Config(max_tokens=500), environ={"TEMPERATURE": "0.2"})
        self.assertEqual(config.temperature, 0.2)
        self.assertEqual(config.max_tokens, 500)
        self.assertEqual(config.timeout, 120)
        self.assertEqual(config.max_tool_iterations, 3)
        self.assertEqual(config.max_steps, 5)
        self.assertIsNone(config.model)

    def test_explicit_config_and_overrides_win(self):
        with patch.dict(os.environ, {"TEMPERATURE": "0.1", "MAX_TOKENS": "100"}):
            self.assertEqual(Config.resolve(Config(temperature=0.7)).temperature, 0.7)
            config = Config.resolve(Config(temperature=0.4), overrides={"temperature": 0})
            self.assertEqual(config.temperature, 0)
            self.assertEqual(Config.resolve(overrides={"max_tokens": 200}).max_tokens, 200)

    def test_empty_mapping_disables_environment_and_snapshot_stays_stable(self):
        with patch.dict(os.environ, {"TEMPERATURE": "0.2"}):
            self.assertEqual(Config.resolve(environ={}).temperature, 0.7)
            snapshot = Config.resolve()
        with patch.dict(os.environ, {"TEMPERATURE": "bad"}):
            updated = Config.resolve(snapshot, overrides={"max_tokens": 50}, environ={})
            self.assertEqual(Config.resolve(snapshot).temperature, 0.2)
        self.assertEqual(updated.temperature, 0.2)
        self.assertIsNone(snapshot.max_tokens)
        with self.assertRaises(ValidationError):
            snapshot.temperature = 1

    def test_environment_fields_and_optional_empty_values(self):
        config = Config.resolve(environ={
            "LLM_MODEL_ID": " test ", "LLM_API_KEY": " test-key ",
            "LLM_BASE_URL": " https://example.invalid/v1 ", "LLM_TIMEOUT": "30",
            "TEMPERATURE": " ", "MAX_TOKENS": "", "MAX_TOOL_ITERATIONS": "2",
            "MAX_STEPS": "4", "LOG_LEVEL": "WARNING",
            "MAX_HISTORY_LENGTH": "10",
        })
        self.assertEqual(config.model, "test")
        self.assertEqual(config.api_key.get_secret_value(), "test-key")
        self.assertEqual(config.base_url, "https://example.invalid/v1")
        self.assertEqual(config.timeout, 30)
        self.assertIsNone(config.temperature)
        self.assertIsNone(config.max_tokens)
        self.assertEqual((config.max_tool_iterations, config.max_steps), (2, 4))
        self.assertEqual((config.log_level, config.max_history_length), ("WARNING", 10))
        self.assertIsNone(Config.resolve(environ={"LLM_MODEL_ID": " "}).model)

    def test_invalid_values_fail_at_config_boundary(self):
        invalid = {
            "temperature": [-1, float("inf"), float("nan"), True],
            "timeout": [0, -1, None, "", True, float("inf")],
            "max_tokens": [0, -1, 1.5, True],
            "max_tool_iterations": [0, True], "max_steps": [0, True],
            "max_history_length": [0],
            "unknown_setting": [1],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ConfigException):
                    Config.resolve(overrides={field: value})
        with self.assertRaises(ConfigException):
            Config.resolve(environ={"LLM_TIMEOUT": ""})

    def test_safe_diagnostics(self):
        config = Config(model="test-model", api_key="private-test-key")
        self.assertNotIn("private-test-key", repr(config))
        self.assertNotIn("private-test-key", str(config.to_dict()))
        with self.assertRaises(ConfigException) as caught:
            Config.resolve(overrides={"api_key": {"bad": "private-test-key"}})
        self.assertNotIn("private-test-key", str(caught.exception))

    def test_credentials_required_only_by_real_clients(self):
        config = Config.resolve(environ={})
        with self.assertRaises(ConfigException):
            config.require_model()
        Config(model="test", api_key="key", base_url="https://example.invalid").require_model()


if __name__ == "__main__":
    unittest.main()
