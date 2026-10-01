"""Check the model adapter without making any network requests."""

from types import SimpleNamespace
import unittest
import os
from unittest.mock import patch

from bpcagent import BasicAgent, Config, MyLLM, LLMResponse
from bpcagent.agents.exceptions import ConfigException, LLMException


def text_response():
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="hello", tool_calls=None, refusal=None), finish_reason="stop")])


class LLMTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        patcher = patch("bpcagent.agents.models.OpenAI")
        self.client = patcher.start().return_value
        self.client.chat.completions.create.return_value = text_response()
        self.addCleanup(patcher.stop)
        self.llm = MyLLM(model="test", api_key="test-key", base_url="https://example.invalid/v1")

    def test_invoke_returns_response_text(self):
        self.assertEqual(self.llm.invoke([{"role": "user", "content": "hi"}]),
                         LLMResponse(content="hello", finish_reason="stop"))

    def test_invoke_failure_raises_llm_exception(self):
        self.client.chat.completions.create.side_effect = RuntimeError("offline failure")
        with self.assertRaises(LLMException):
            self.llm.invoke([{"role": "user", "content": "hi"}])

    def test_agent_stream_forwards_parameters_and_saves_history(self):
        self.client.chat.completions.create.return_value = iter([
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])
            for part in ("你", "好")
        ])
        agent = BasicAgent("test", self.llm, enable_tool_calling=False)
        self.assertEqual("".join(agent.run_using_stream("hi", temperature=0.2, max_tokens=20)), "你好")
        options = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(options["temperature"], 0.2)
        self.assertEqual(options["max_tokens"], 20)
        self.assertTrue(options["stream"])
        self.assertEqual(agent.get_history()[-1].content, "你好")

    def test_stream_failure_raises_llm_exception(self):
        self.client.chat.completions.create.side_effect = RuntimeError("offline failure")
        with self.assertRaises(LLMException):
            list(self.llm.think([{"role": "user", "content": "hi"}]))

    def test_config_precedence_reaches_client_and_request(self):
        with patch.dict(os.environ, {
            "LLM_MODEL_ID": "env-model", "LLM_API_KEY": "env-key",
            "LLM_BASE_URL": "https://example.invalid/v1", "TEMPERATURE": "0.1",
            "MAX_TOKENS": "100", "LLM_TIMEOUT": "90",
        }), patch("bpcagent.agents.models.OpenAI") as factory:
            llm = MyLLM(config=Config(model="config-model", temperature=0.4),
                        model="explicit-model", api_key="explicit-key",
                        base_url="https://explicit.invalid/v1", temperature=0.3,
                        timeout=60, top_p=0.8)
            factory.assert_called_once_with(api_key="explicit-key",
                                            base_url="https://explicit.invalid/v1", timeout=60)
            factory.return_value.chat.completions.create.return_value = text_response()
            llm.invoke([], temperature=0, timeout=10, top_p=0.5)
            options = factory.return_value.chat.completions.create.call_args.kwargs
            self.assertEqual(options, dict(messages=[], model="explicit-model", temperature=0,
                                           timeout=10, max_tokens=100, top_p=0.5, stream=False))
            self.assertEqual(llm.config.temperature, 0.3)
            self.assertEqual(llm.config.timeout, 60)

    def test_none_omits_parameters_for_both_request_modes(self):
        llm = MyLLM(config=Config.resolve(self.llm.config, overrides={"max_tokens": 99}, environ={}),
                    temperature=None, max_tokens=None)
        llm.invoke([])
        options = self.client.chat.completions.create.call_args.kwargs
        self.assertNotIn("temperature", options)
        self.assertNotIn("max_tokens", options)
        self.client.chat.completions.create.return_value = iter([])
        list(self.llm.think([], temperature=None, max_tokens=None))
        options = self.client.chat.completions.create.call_args.kwargs
        self.assertTrue(options["stream"])
        self.assertNotIn("temperature", options)
        self.assertNotIn("max_tokens", options)
        self.assertEqual(self.llm.config.temperature, 0.7)

    def test_requests_reuse_snapshot_and_copy_extra_parameters(self):
        body = {"metadata": {"tag": "original"}}
        llm = MyLLM(config=self.llm.config, extra_body=body)
        body["metadata"]["tag"] = "changed"
        with patch.dict(os.environ, {"TEMPERATURE": "bad", "LLM_TIMEOUT": "0"}):
            llm.invoke([])
        options = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(options["extra_body"]["metadata"]["tag"], "original")
        options["extra_body"]["metadata"]["tag"] = "sdk-mutated"
        llm.invoke([])
        next_options = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(next_options["extra_body"]["metadata"]["tag"], "original")
        self.client.chat.completions.create.return_value = iter([])
        list(llm.think([], extra_body={"metadata": {"tag": "request"}}, timeout=5))
        options = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(options["extra_body"], {"metadata": {"tag": "request"}})
        self.assertEqual(options["timeout"], 5)
        self.assertEqual(llm.config.timeout, 120)

    def test_invalid_request_parameters_never_reach_sdk(self):
        for options in ({"timeout": 0}, {"timeout": None}, {"max_tokens": -1},
                        {"temperature": True}, {"stream": True}, {"model": "other"},
                        {"api_key": "other"}, {"base_url": "other"}, {"max_steps": 1}):
            with self.subTest(options=options):
                with self.assertRaises(ConfigException):
                    self.llm.invoke([], **options)
                with self.assertRaises(ConfigException):
                    list(self.llm.think([], **options))
        self.client.chat.completions.create.assert_not_called()

    def test_invalid_constructor_fails_before_client_creation(self):
        for options in ({"timeout": None}, {"timeout": 0}, {"stream": True}):
            with self.subTest(options=options), patch("bpcagent.agents.models.OpenAI") as factory:
                with self.assertRaises(ConfigException):
                    MyLLM(config=self.llm.config, **options)
                factory.assert_not_called()
        with patch("bpcagent.agents.models.OpenAI") as factory:
            with self.assertRaises(ConfigException):
                MyLLM()
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
