"""Responses tool-loop tests with real SDK output models, no network."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseReasoningItem

from bpcagent import BasicAgent, CalculatorTool, Config, LLM, ToolRegistry
from bpcagent.agents.exceptions import ConfigException, LLMException
from bpcagent.log_paths import new_log_path


def completed(text="done"):
    return SimpleNamespace(status="completed", output=[ResponseOutputMessage(
        id="msg1", type="message", role="assistant", status="completed",
        content=[{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
    )])


class ResponsesTests(unittest.TestCase):
    def setUp(self):
        stdout = redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)
        patcher = patch("bpcagent.agents.models.OpenAI")
        self.client = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.cfg = Config(model="gpt-6-astra", api_key="test-key", base_url="https://api.openai.com/v1",
                          api_mode="responses", reasoning_effort="medium", max_tokens=4000, max_tool_iterations=4)

    def test_model_effort_and_token_limit_reach_responses(self):
        self.client.responses.create.return_value = completed()
        llm = LLM(config=self.cfg)
        llm.invoke([{"role": "user", "content": "hello"}])
        args = self.client.responses.create.call_args.kwargs
        self.assertEqual(args["model"], "gpt-6-astra")
        self.assertEqual(args["reasoning"], {"effort": "medium"})
        self.assertEqual(args["max_output_tokens"], 4000)
        self.assertFalse(args["store"])
        self.assertNotIn("temperature", args)
        self.assertNotIn("reasoning_effort", args)
        self.client.chat.completions.create.assert_not_called()

    def test_native_reasoning_calls_and_results_preserved_across_turns(self):
        reasoning = ResponseReasoningItem(id="rs1", type="reasoning", summary=[], encrypted_content="opaque")
        call = ResponseFunctionToolCall(id="fc1", type="function_call", call_id="call1",
                                        name="calculate", arguments='{"input":"2+3"}')
        replies = iter([SimpleNamespace(status="completed", output=[reasoning, call]), completed("5")])
        requests = []

        def create(**kwargs):
            requests.append(deepcopy(kwargs))
            return next(replies)

        self.client.responses.create.side_effect = create
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        agent = BasicAgent("responses-test", config=self.cfg, tool_registry=registry)
        with TemporaryDirectory() as temp, patch("bpcagent.agents.agents.new_log_path",
                                                side_effect=lambda name: new_log_path(name, root=temp)):
            result = agent.run("2+3?")
        self.assertTrue(result.finished)
        self.assertEqual(result.output["text"], "5")
        self.assertEqual(requests[0]["tools"][0]["name"], "calculate")
        self.assertNotIn("function", requests[0]["tools"][0])
        replay = requests[1]["input"]
        self.assertIn(reasoning.model_dump(mode="json", exclude_none=True), replay)
        self.assertIn(call.model_dump(mode="json", exclude_none=True), replay)
        outputs = [r for r in replay if r.get("type") == "function_call_output"]
        self.assertEqual(outputs[0]["call_id"], "call1")
        self.assertEqual(len([r for r in replay if r.get("type") == "function_call"]), 1)

    def test_failure_refusal_and_streaming_are_explicit(self):
        llm = LLM(config=self.cfg)
        self.client.responses.create.return_value = SimpleNamespace(status="incomplete")
        with self.assertRaises(LLMException):
            llm.invoke([])
        self.client.responses.create.return_value = SimpleNamespace(status="completed", output=[ResponseOutputMessage(
            id="m1", type="message", role="assistant", status="completed",
            content=[{"type": "refusal", "refusal": "no"}],
        )])
        with self.assertRaises(LLMException):
            llm.invoke([])
        with self.assertRaises(ConfigException):
            list(llm.think([]))
        self.client.responses.create.side_effect = RuntimeError("SECRET")
        with self.assertRaises(LLMException) as error:
            llm.invoke([])
        self.assertNotIn("SECRET", str(error.exception))

    def test_reasoning_effort_reaches_chat_compatible_provider(self):
        cfg = self.cfg.model_copy(update={"api_mode": "chat_completions"})
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=[], refusal=None), finish_reason="stop")])
        LLM(config=cfg).invoke([])
        options = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(options["reasoning_effort"], "medium")
        self.assertEqual(options["max_completion_tokens"], 4000)
        self.assertNotIn("max_tokens", options)

    def test_deepseek_preserves_native_max_tokens_and_reasoning(self):
        cfg = self.cfg.model_copy(update={"default_provider": "deepseek", "api_mode": "chat_completions",
                                          "model": "deepseek-flash", "reasoning_effort": "low"})
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=[], refusal=None, reasoning_content="opaque-reasoning"),
            finish_reason="stop")])
        result = LLM(config=cfg).invoke([])
        options = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(options["max_tokens"], 4000)
        self.assertEqual(options["reasoning_effort"], "low")
        self.assertNotIn("max_completion_tokens", options)
        self.assertEqual(result.reasoning_content, "opaque-reasoning")
