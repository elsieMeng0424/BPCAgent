"""Check the model adapter without making any network requests."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bpcagent import BasicAgent, MyLLM
from bpcagent.exceptions import LLMException


class LLMTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("bpcagent.models.OpenAI")
        self.client = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.llm = MyLLM(model="test", apiKey="test-key", baseUrl="https://example.invalid/v1")

    def test_invoke_returns_response_text(self):
        self.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))]
        )
        self.assertEqual(self.llm.invoke([{"role": "user", "content": "hi"}]), "hello")

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


if __name__ == "__main__":
    unittest.main()
