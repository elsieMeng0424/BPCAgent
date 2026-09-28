"""Offline checks for conversation history and real tool execution."""

from copy import deepcopy
import unittest
import os
from unittest.mock import Mock, patch

from bpcagent import BasicAgent, CalculatorTool, Config, MyLLM, ReActAgent, ToolRegistry
from bpcagent.exceptions import ConfigException


class AgentTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def make_model(self, *responses):
        llm = Mock(spec=MyLLM)
        llm.model = "test-model"
        remaining = iter(responses)
        calls = []

        def invoke(messages, **kwargs):
            calls.append(deepcopy(messages))
            return next(remaining)

        llm.invoke.side_effect = invoke
        return llm, calls

    def test_single_question_and_history(self):
        llm, calls = self.make_model("你好！")
        agent = BasicAgent("test", llm, sys_prompt="简洁回答。", enable_tool_calling=False)

        self.assertEqual(agent.run("你好"), "你好！")
        self.assertEqual(calls[0], [
            {"role": "system", "content": "简洁回答。"},
            {"role": "user", "content": "你好"},
        ])
        self.assertEqual([(m.role, m.content) for m in agent.get_history()], [
            ("user", "你好"), ("assistant", "你好！"),
        ])

    def test_next_question_receives_history_and_clear_resets_it(self):
        llm, calls = self.make_model("已记住。", "小明", "不知道")
        agent = BasicAgent("test", llm, enable_tool_calling=False)
        agent.run("我叫小明")
        self.assertEqual(agent.run("我叫什么？"), "小明")
        self.assertEqual(calls[1][1:3], [
            {"role": "user", "content": "我叫小明"},
            {"role": "assistant", "content": "已记住。"},
        ])
        agent.clear_history()
        agent.run("我叫什么？")
        self.assertEqual(len(calls[2]), 2)

    def test_basic_agent_executes_calculator_and_returns_answer(self):
        for parameters in ('2+3*4', '{"input": "2+3*4"}'):
            with self.subTest(parameters=parameters):
                llm, calls = self.make_model(f"[TOOL_CALL:calculate:{parameters}]", "结果是 14。")
                registry = ToolRegistry()
                calculator = CalculatorTool()
                calculator.run = Mock(wraps=calculator.run)
                registry.register_tool(calculator)
                agent = BasicAgent("test", llm, tool_registry=registry)

                self.assertEqual(agent.run("计算 2+3*4"), "结果是 14。")
                calculator.run.assert_called_once_with({"input": "2+3*4"})
                self.assertIn("14", calls[1][-1]["content"])
                self.assertEqual(len(agent.get_history()), 2)

    def test_react_agent_executes_tool_before_finish(self):
        llm, calls = self.make_model(
            "Thought: Use the calculator.\nAction: calculate[2+3*4]",
            "Thought: The result is available.\nAction: Finish[14]",
        )
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        agent = ReActAgent("test", llm, tool_registry=registry)

        self.assertEqual(agent.run("计算 2+3*4"), "14")
        self.assertIn("Observation: 14", calls[1][0]["content"])

    def test_function_tool_description_and_execution(self):
        registry = ToolRegistry()
        registry.register_function("uppercase", "转成大写", str.upper)
        self.assertIn("转成大写", registry.get_descriptions())
        self.assertEqual(registry.execute_tool("uppercase", "hello"), "HELLO")

    def test_basic_iteration_config_and_single_run_override(self):
        for override, expected_iterations in ((None, 2), (1, 1)):
            with self.subTest(override=override):
                llm, _ = self.make_model(*(["[TOOL_CALL:calculate:1+1]"] * expected_iterations), "2")
                registry = ToolRegistry()
                calculator = CalculatorTool()
                calculator.run = Mock(wraps=calculator.run)
                registry.register_tool(calculator)
                agent = BasicAgent("test", llm, config=Config(max_tool_iterations=2),
                                   tool_registry=registry)
                self.assertEqual(agent.run("calculate", max_tool_iterations=override, temperature=0.1), "2")
                self.assertEqual(calculator.run.call_count, expected_iterations)
                # Preserve existing final model call after exhausting tool iterations.
                self.assertEqual(llm.invoke.call_count, expected_iterations + 1)
                self.assertEqual(agent.config.max_tool_iterations, 2)
                for call in llm.invoke.call_args_list:
                    self.assertEqual(call.kwargs, {"temperature": 0.1})

    def test_react_step_config_and_constructor_override(self):
        for override, expected_steps in ((None, 2), (1, 1)):
            with self.subTest(override=override):
                llm, _ = self.make_model(*(["Thought: calculate\nAction: calculate[1+1]"] * expected_steps))
                registry = ToolRegistry()
                registry.register_tool(CalculatorTool())
                config = Config(max_steps=2)
                agent = ReActAgent("test", llm, config=config, max_steps=override, tool_registry=registry)
                agent.run("calculate")
                self.assertEqual(llm.invoke.call_count, expected_steps)
                self.assertEqual(agent.config.max_steps, expected_steps)
                self.assertEqual(config.max_steps, 2)

    def test_agent_environment_defaults_and_validation(self):
        llm, _ = self.make_model("unused")
        with patch.dict(os.environ, {"MAX_TOOL_ITERATIONS": "2", "MAX_STEPS": "4"}):
            basic = BasicAgent("test", llm)
            react = ReActAgent("test", llm)
        self.assertEqual(basic.config.max_tool_iterations, 2)
        self.assertEqual(react.max_steps, 4)
        for invalid in (0, -1, True):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigException):
                basic.run("hello", max_tool_iterations=invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ConfigException):
                ReActAgent("test", llm, max_steps=invalid)
        llm.invoke.assert_not_called()

    def test_agent_config_does_not_modify_supplied_model(self):
        with patch("bpcagent.models.OpenAI"):
            llm = MyLLM(model="test", api_key="test-key", base_url="https://example.invalid", temperature=0.2)
        original = llm.config
        agent = BasicAgent("test", llm, config=Config(temperature=0.9))
        self.assertEqual(agent.config.temperature, 0.9)
        self.assertIs(llm.config, original)
        self.assertEqual(llm.config.temperature, 0.2)


if __name__ == "__main__":
    unittest.main()
